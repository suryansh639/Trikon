# Design Document

## 1. Overview

`trikon.verify.static_checks.run_static_checks` Step 3 passes the head-side changed-file list (after the `.py`/`.pyi` suffix filter added by the `static-checks-python-filter` bugfix) verbatim into `_expand_argv`, which substitutes it for the `{files}` sentinel in `ruff` / `mypy` argv. The list contains one entry per `trikon.change_intel.models.FileChange` in the parsed diff — including entries where `change_kind == "deleted"`. Those paths do not exist at HEAD; inside the Docker sandbox the head bind-mount `/workspace/repo` contains only files that exist at HEAD, so ruff emits `E902: No such file or directory (os error 2)` for every deleted `.py`. Those E902s land in `StaticReport.findings` marked `is_new=True` and drive the verdict to `decision=block`.

Verified in the field on the pallets/click repro (base `87f7a31` → head `6aabf09`, `tests/test_types.py` deleted and replaced with a `tests/test_types/` package): the Docker sandbox path returns `decision=block, matched_rule="new static-analysis errors", new_errors=1`, sourced from ruff's E902 on `tests/test_types.py`. The `--no-sandbox` host path silently skips the same file (host-side ruff's exit-code behavior on missing paths differs enough that the E902 does not surface as a new finding), producing a different verdict on the same commit range — a latent divergence between backends of the same product.

The fix has three parts:

1. **Add per-file `change_kind` metadata to the public `ImpactSet` model** — a new `file_changes: list[FileChangeInfo]` list (Pydantic-validated, mirrors the internal frozen dataclass `trikon.change_intel.models.FileChange` field-for-field on the shape that matters here: `path`, `change_kind`, `old_path`). The list defaults to `[]` so every existing constructor and every existing JSON fixture keeps working (Requirements 1.7, 1.8, 8.3, 8.4).
2. **Plumb `change_kind`** from the already-computed `FileChange` values inside `trikon.change_intel.blast_radius.compute_impact` into the new field. `diff_parser.py` already produces the values; the fix is a two-line addition inside the two `ImpactSet(...)` construction sites in `blast_radius.py` (`_compute_python_impact` and `_empty_python_impact`).
3. **Add a head-existence filter** at the head-side raise site inside `run_static_checks` Step 3. A new module-level helper `_filter_by_head_existence(paths, file_changes) -> tuple[str, ...]` mirrors `_filter_by_suffix` from v0.3.1: same signature shape (`Sequence[str]` in, `tuple[str, ...]` out), same order-preserving purity contract, same "sole gate at the raise site" discipline. The filter drops any path whose FileChangeInfo entry has `change_kind == "deleted"` (and, defensively, any path that appears as a rename-source `old_path`).

The baseline-side raise site inside `_run_baseline_tool_on_host` already covers the mirror case defensively — `(worktree_dir / f).is_file()` against the base worktree correctly rejects any path that does not exist at BASE (paths added at HEAD, paths deleted from BASE by a prior commit that this hunk doesn't touch, etc.). No new logic is added there; the design keeps the baseline path exactly as-is (Requirement 5).

This is a **bugfix**. `run_static_checks` public signature is unchanged. `StaticReport`, `StaticTool`, and `DEFAULT_STATIC_TOOLS` are unchanged. `ImpactSet` gains one additive field with a default — a backward-compatible extension of the public boundary.

## 2. Root Cause Diagnosis (verified against the source)

Three anchors in the source tree that establish the bug:

**Anchor 1 — the public boundary is flat**, `trikon/evidence/report.py::ImpactSet`:

```python
class ImpactSet(BaseModel):
    """The set of things a change affects."""

    changed_files: list[str]
    changed_symbols: list[SymbolRef]
    impacted_modules: list[str]
    impacted_public_apis: list[SymbolRef]
    impacted_tests: list[str]
    blast_radius_score: BlastBucket
    blast_radius_numeric: float
```

`changed_files` is a flat `list[str]`. No `change_kind`, no `old_path`, no per-file metadata. Every consumer downstream of `ImpactSet` sees only the flat list.

**Anchor 2 — the plumbing drops `change_kind`**, `trikon/change_intel/blast_radius.py::_compute_python_impact` (around line 297):

```python
return ImpactSet(
    changed_files=sorted(fc.path for fc in change_set.files),
    changed_symbols=_public_refs_sorted(changed_symbols, repo_path=repo_path),
    impacted_modules=impacted_modules,
    ...
)
```

The comprehension `sorted(fc.path for fc in change_set.files)` throws away everything else on the `FileChange` (`change_kind`, `old_path`, `hunks`). The same drop happens in `_empty_python_impact` (around line 319). This is the plumbing point where `change_kind` must ride onto the public boundary.

**Anchor 3 — the head-side raise site trusts the flat list**, `trikon/verify/static_checks.py` inside `run_static_checks` Step 3 (currently around line 358):

```python
filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)
if not filtered:
    continue
argv = _expand_argv(tool.argv_template, filtered)
head_result = sandbox.exec(argv)
```

`_filter_by_suffix` gates on `.py` / `.pyi` — a deleted `.py` passes this filter. The `filtered` tuple then goes into `_expand_argv` and out to `sandbox.exec` as argv tokens. Inside the Docker sandbox, the head bind-mount does not contain the deleted file → E902.

`trikon/change_intel/models.py` already carries the value we need:

```python
ChangeKind = Literal["added", "modified", "deleted", "renamed"]

@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    change_kind: ChangeKind
    old_path: str | None
    hunks: tuple[Hunk, ...]
```

And `trikon/change_intel/diff_parser.py::_classify_change_kind` populates it from unidiff's `is_added_file` / `is_removed_file` / `is_rename` predicates. The fix is entirely about plumbing this existing value to the raise site.

## 3. Extension Point on `ImpactSet`

Add a single Pydantic model and a single additive field. The public boundary in `trikon/evidence/report.py`:

```python
from typing import Literal

# Mirror of trikon.change_intel.models.ChangeKind. Duplicated here (not imported)
# because trikon.evidence.report must not depend on trikon.change_intel — the
# evidence module is the SDK's public boundary; the change-intel module is an
# internal producer. See design.md §9 for the boundary discipline.
ChangeKind = Literal["added", "modified", "deleted", "renamed"]


class FileChangeInfo(BaseModel):
    """Per-file change metadata carried on :class:`ImpactSet`.

    Mirrors the internal :class:`trikon.change_intel.models.FileChange`
    on the fields the public boundary needs — ``path``, ``change_kind``,
    ``old_path`` — and deliberately omits ``hunks`` (the unified-diff
    line-index sets), which are an internal-only detail of the change-
    intel pipeline and would inflate every ``ImpactSet`` JSON payload
    without carrying value at the SDK boundary.

    Field semantics
    ---------------
    ``path``: POSIX-relative path, same value as the internal
    ``FileChange.path``. For an added file this is the new path;
    for a modified file the unchanged path; for a deleted file the
    pre-deletion path (the file does not exist at HEAD); for a
    renamed file the post-rename path.

    ``change_kind``: One of the four :data:`ChangeKind` literals.

    ``old_path``: The pre-rename path when ``change_kind == "renamed"``,
    ``None`` for every other value of ``change_kind``. Callers that want
    to know "did this rename move a file from X to Y" read
    ``(old_path, path)`` when ``change_kind == "renamed"``.
    """

    path: str
    change_kind: ChangeKind
    old_path: str | None = None


class ImpactSet(BaseModel):
    """The set of things a change affects."""

    changed_files: list[str]
    changed_symbols: list[SymbolRef]
    impacted_modules: list[str]
    impacted_public_apis: list[SymbolRef]
    impacted_tests: list[str]
    blast_radius_score: BlastBucket
    blast_radius_numeric: float
    # Additive field. Defaults to [] so every existing positional /
    # keyword construction site keeps working and every existing JSON
    # fixture round-trips through model_validate without change
    # (Requirement 1.7, 1.8, 8.3, 8.4).
    file_changes: list[FileChangeInfo] = Field(default_factory=list)
```

Design decisions:

- **New Pydantic model, not a bare `tuple[str, ChangeKind, str | None]`.** The public boundary is Pydantic-validated JSON. A bare tuple loses field names, breaks JSON-schema generation, and forces downstream consumers to index into positions. The extra allocation cost is trivial — the count of `file_changes` entries is O(changed files), typically single-digit to low-hundreds.
- **`list[FileChangeInfo]`, not `dict[str, FileChangeInfo]`.** A list preserves order (matches the POSIX-sorted determinism contract from Requirement 2.6). A dict keyed on `path` would break in the presence of a rename where the old path and new path both need to be representable — even if the current design uses a single entry with `old_path`, a future extension that emits both sides would break the dict-uniqueness invariant.
- **Field name `file_changes`, not `changes` or `changed_files_metadata`.** `file_changes` reads naturally next to the existing `changed_files` field ("here are the files that changed; here are the per-file details of each change") and matches the internal name `change_set.files → FileChange`.
- **`ChangeKind` re-declared, not imported from `trikon.change_intel.models`.** `trikon.evidence.report` is the public SDK boundary and must not depend on internal-only modules. The literal is trivially copied (four string values) and the values are pinned by the requirement (Requirement 1.4). If the two ever drift, the divergence will surface immediately in the `_to_public_file_change_info` bridge below.
- **`old_path` default `None`, not required-when-renamed at the type level.** Pydantic's `Literal` discriminator support with `discriminator="change_kind"` could enforce "`old_path` is `str` when `change_kind == "renamed"` and `None` otherwise" at validation time, but that requires a two-model split (`RenamedFileChangeInfo` vs the other three). The extra shape complexity is not worth it — Requirement 2.5 and the `_to_public_file_change_info` bridge (Section 4) enforce the invariant at the sole producer site.
- **`default_factory=list`, not `default=[]`.** Pydantic tolerates `default=[]` for lists but a per-instance factory is the safer form (dataclass-mutable-default guardrail; matches the pattern already used for `PluginResult.findings`, `RuleResult`-family lists, and `Verdict.warnings`).
- **The type is `list[FileChangeInfo]`, not `list[FileChangeInfo] | None`.** A missing value is the empty list, not `None`. `None` would force a `is None` check at every consumer and invite the "None means we do not know" footgun.

### `EMPTY_IMPACT_SET` sentinel update

The never-fail-open sentinel `EMPTY_IMPACT_SET` in `trikon/evidence/report.py` gets one additional keyword argument:

```python
EMPTY_IMPACT_SET: ImpactSet = ImpactSet(
    changed_files=[],
    changed_symbols=[],
    impacted_modules=[],
    impacted_public_apis=[],
    impacted_tests=[],
    blast_radius_score="HIGH",
    blast_radius_numeric=0.0,
    file_changes=[],
)
```

The `file_changes=[]` line is redundant with the field default but is written explicitly because the sentinel documents the fail-closed shape by example — every field is listed, every default is confirmed at the construction site (Requirement 8.2).

## 4. Plumbing `change_kind` Through `blast_radius.py`

Two construction sites inside `trikon/change_intel/blast_radius.py`. Both get a new argument line. Neither construction site's other logic changes.

New helper adjacent to the existing `_public_refs_sorted` (private, module-scoped):

```python
from trikon.evidence.report import FileChangeInfo


def _public_file_changes_sorted(
    files: tuple[FileChange, ...],
) -> list[FileChangeInfo]:
    """Translate ``ChangeSet.files`` into the public :class:`FileChangeInfo` list.

    Deterministic ordering: sorted by ``path`` POSIX-lexicographic ascending,
    matching the ordering used for :attr:`ImpactSet.changed_files` so identical
    inputs produce byte-identical JSON downstream.

    Rename semantics: for a :class:`FileChange` with ``change_kind == "renamed"``,
    ``FileChangeInfo.old_path`` is copied from ``FileChange.old_path``; for every
    other value of ``change_kind``, ``FileChangeInfo.old_path`` is ``None``. The
    internal :class:`FileChange` already respects this invariant
    (``diff_parser._rename_source_path`` is called only on the rename branch);
    the helper preserves it verbatim at the public boundary.
    """
    return sorted(
        (
            FileChangeInfo(
                path=fc.path,
                change_kind=fc.change_kind,
                old_path=fc.old_path if fc.change_kind == "renamed" else None,
            )
            for fc in files
        ),
        key=lambda info: info.path,
    )
```

Wiring inside `_compute_python_impact` (around line 297):

```python
return ImpactSet(
    changed_files=sorted(fc.path for fc in change_set.files),
    changed_symbols=_public_refs_sorted(changed_symbols, repo_path=repo_path),
    impacted_modules=impacted_modules,
    impacted_public_apis=_public_refs_sorted(impacted_public_apis, repo_path=repo_path),
    impacted_tests=impacted_tests,
    blast_radius_score=bucket(score, weights=weights),
    blast_radius_numeric=score,
    file_changes=_public_file_changes_sorted(change_set.files),  # NEW
)
```

Wiring inside `_empty_python_impact` (around line 319):

```python
return ImpactSet(
    changed_files=sorted(fc.path for fc in change_set.files),
    changed_symbols=[],
    impacted_modules=[],
    impacted_public_apis=[],
    impacted_tests=[],
    blast_radius_score=bucket(score, weights=weights),
    blast_radius_numeric=score,
    file_changes=_public_file_changes_sorted(change_set.files),  # NEW
)
```

Design decisions:

- **Sort by `path`, not by `(change_kind, path)`.** Consumers read `file_changes` alongside `changed_files`; keeping the sort key identical means `file_changes[i].path` and `changed_files[i]` are in the same order (though not necessarily at the same index — a rename that is not detected as such would emit a `deleted` and one-or-more `added` entries with distinct paths).
- **Helper is a `list`, not a `tuple`.** `ImpactSet` fields are Pydantic `list[...]`; matching that shape avoids a Pydantic coercion at construction time.
- **Two separate wiring sites**, one line each. Not extracted into a helper because there is no coupling to abstract — the addition is literal.
- **`_empty_python_impact` gets the same treatment.** Requirement 2.7: the empty-Python-change branch (no `.py` in the diff, e.g., a YAML-only change) still surfaces `file_changes` from `change_set.files`. Downstream consumers get consistent metadata regardless of which branch produced the `ImpactSet`.

## 5. Head-Existence Filter Helper

New module-level helper in `trikon/verify/static_checks.py`, colocated with `_filter_by_suffix`:

```python
from collections.abc import Sequence

from trikon.evidence.report import FileChangeInfo


def _filter_by_head_existence(
    paths: Sequence[str],
    file_changes: Sequence[FileChangeInfo],
) -> tuple[str, ...]:
    """Return the subsequence of ``paths`` that exist in the HEAD tree.

    A path is dropped from the output when the accompanying
    ``file_changes`` list contains an entry describing it as absent
    from HEAD. Two cases are dropped:

    1. ``change_kind == "deleted"`` — the file was deleted between
       BASE and HEAD; it does not exist in the HEAD checkout / head
       bind-mount, so passing it as an argv token to ruff / mypy
       produces ``E902: No such file`` on the Docker sandbox path.
    2. ``change_kind == "renamed"`` and ``old_path == p`` — the
       rename-source path does not exist at HEAD (only the rename
       target does). ``impact.changed_files`` today does not emit
       rename-source paths, so this branch fires only defensively;
       it exists so a future change that starts emitting them cannot
       reintroduce the E902.

    Empty-``file_changes`` semantics
    --------------------------------
    When ``file_changes`` is empty, the filter is a no-op — it returns
    ``tuple(paths)`` unchanged. This is the backward-compatibility
    branch: an out-of-tree consumer that constructs ``ImpactSet``
    without supplying ``file_changes`` (Requirement 1.7) or a
    deserialized JSON payload lacking the ``file_changes`` key
    (Requirement 1.8) falls into this branch and behaves exactly like
    the pre-fix code path. In-tree the branch is unreachable — the
    blast-radius orchestrator always populates ``file_changes``
    (Requirement 2.1, 2.7).

    Args:
        paths: The file paths to filter. Order is preserved in the
            output (the output is a subsequence of ``paths``). The
            input sequence is not mutated.
        file_changes: The per-file change metadata carried on
            :attr:`ImpactSet.file_changes`. An entry with ``path == p,
            change_kind == "deleted"`` causes ``p`` to be dropped;
            an entry with ``change_kind == "renamed", old_path == p``
            also causes ``p`` to be dropped.

    Returns:
        A tuple whose elements are a subsequence of ``paths`` in
        original order, containing exactly those paths that are not
        classified as deleted-at-head or rename-source. Pure function:
        same inputs produce the same output, no I/O, no mutation of
        arguments.
    """
    if not file_changes:
        return tuple(paths)

    dropped: set[str] = set()
    for info in file_changes:
        if info.change_kind == "deleted":
            dropped.add(info.path)
        elif info.change_kind == "renamed" and info.old_path is not None:
            dropped.add(info.old_path)

    return tuple(p for p in paths if p not in dropped)
```

Design decisions:

- **Set-based membership**, not per-path linear scan. `file_changes` can have hundreds of entries on a large change; scanning it once into a `dropped: set[str]` and then testing each path against the set is O(F + P) instead of O(F * P).
- **`Sequence[str]` in, `tuple[str, ...]` out.** Matches `_filter_by_suffix` exactly. Callers pass the tuple into `_expand_argv` which itself returns a tuple; the type shape is consistent throughout.
- **`file_changes` typed as `Sequence[FileChangeInfo]`**, not `list[FileChangeInfo]`. `Sequence` lets tests pass a tuple, matches the "read-only view" intent, and is the same protocol width used by every other filter helper in this module.
- **Empty `file_changes` short-circuits to `tuple(paths)`**, not to `()`. The two `_filter_by_suffix` and `_filter_by_head_existence` helpers are semantically distinct: `_filter_by_suffix`'s empty-`accepted_suffixes` case means "no suffix is accepted, drop everything"; `_filter_by_head_existence`'s empty-`file_changes` case means "no metadata was supplied, drop nothing" (Requirement 3.6). The asymmetry is documented at both helpers.
- **The filter is pure.** No I/O. `PurePosixPath.suffix` was the only stdlib touch inside `_filter_by_suffix`; this helper does not even need that. `set` membership is deterministic and hash-only.
- **Rename-source defensive branch.** `impact.changed_files` today lists only `fc.path` (post-rename path for renames). But `_filter_by_head_existence` is written to also drop `old_path` for renames — defensive posture for a possible future extension that starts emitting both sides. The branch is cheap (adds one string to `dropped` per rename) and closes a latent regression channel.

## 6. Threading the Filter Through the Head-Side Raise Site

Inside `run_static_checks` Step 3 (currently around line 358, immediately after Step 2's `_resolve_base_keys` call):

```python
# --- current (post-v0.3.1) ---
filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)
if not filtered:
    continue
argv = _expand_argv(tool.argv_template, filtered)
head_result = sandbox.exec(argv)

# --- proposed ---
filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)
filtered = _filter_by_head_existence(filtered, impact.file_changes)
if not filtered:
    # Nothing to inspect at HEAD after both filters. Record the tool
    # ran with zero findings (tools_run.append(tool.name) at the top
    # of the loop already did this) and skip the sandbox exec, the
    # is_new diff (Step 4), and the counter update (Step 5).
    continue
argv = _expand_argv(tool.argv_template, filtered)
head_result = sandbox.exec(argv)
```

Behavioral notes:

- **Filter order is fixed: suffix first, head-existence second.** Requirement 4.2 pins this down. The suffix filter is dependency-free; the head-existence filter needs `impact.file_changes` which is O(F) to scan. Running the cheaper filter first can shrink the input to the second, so the ordering is also a small optimization — but its primary reason is determinism.
- **The two filters compose as a plain function chain.** No merged "one big filter" helper — each filter has one job (Requirement 6.2), and the composition is the raise-site's business. This keeps the two filters testable in isolation and mirrors the v0.3.1 pattern where `_filter_by_suffix` was introduced as a discrete helper.
- **`_resolve_base_keys` (Step 2) still runs even when the head-side composed filter output is empty.** `_resolve_base_keys` is a per-tool call that materializes the baseline finding set for the `is_new` diff. Short-circuiting it based on the head-side filter output would entangle two concerns (baseline resolve and head-side skip) and would break the "cache miss materializes and persists a baseline" contract of Requirement 3.2 from the v0.3.1 spec. `_resolve_base_keys` doing its full work on a change that happens to have zero head-side files is cheap: the baseline tool subprocess is skipped because `_run_baseline_tool_on_host` filters on `is_file()` against the base worktree, and the SQLite cache row (if written) is harmless.
- **The `continue` skips Step 4 (is_new diff) and Step 5 (counter update).** Correct — there are no head findings to diff or count.
- **`tools_run.append(tool.name)` at the top of the per-tool loop is unchanged.** It runs before the filter composition, so the tool is recorded as having "run" (with zero findings) whether or not the composed filter empties the list.

## 7. Threading Through the Baseline-Side Raise Site

**No change is required at the baseline-side raise site.** The current code inside `_run_baseline_tool_on_host` (currently around line 715) already handles the mirror case:

```python
filtered = _filter_by_suffix(changed_files, tool.accepted_suffixes)
if not filtered:
    return []
existing = [f for f in filtered if (worktree_dir / f).is_file()]
if not existing:
    return []
argv = _expand_argv(tool.argv_template, existing)
```

`(worktree_dir / f).is_file()` rejects any path that does not exist in the base worktree — including:

- Paths that were `added` at HEAD (they don't exist at BASE).
- Paths that are the rename target of a rename at HEAD (they don't exist at BASE; only the rename source does).
- Paths that were deleted from BASE by an earlier commit that this diff doesn't touch.

The `is_file()` intersection is precisely the baseline-side mirror of the head-existence filter — the two paths use different mechanisms (filesystem `is_file()` on the base worktree; metadata lookup on `file_changes` at head) because at BASE we have the whole tree on disk (fast to `stat`) and at HEAD we do not always have the head tree in-process (the sandbox owns the mount). Introducing the head-existence filter on the baseline side would be a category error — `impact.file_changes` describes BASE-to-HEAD movement, not the state of the BASE tree.

Requirement 5 makes this explicit: the baseline path stays as-is, and Requirement 5.4 confirms that "added-at-HEAD" paths are already correctly rejected by `is_file()`.

## 8. Cache-Key Discipline

`_resolve_base_keys` looks up the `static_baseline` SQLite row keyed on `(base_sha, tool.name, tool_version)` (Requirement 3.3 from the v0.3.1 spec). This bugfix does not change the cache key.

Consequences:

- A prior cache row written on the pre-fix code path may contain baseline findings for `.py` files that the head side now excludes (deleted-at-head paths). Those cache-row entries are harmless: the head side never emits a finding for a deleted path (the filter drops it), so no head triple `(path, line, rule_id)` will match a baseline triple pointing at a deleted path. Stale rows do not affect the `is_new` diff.
- No cache invalidation on this bugfix alone is required. A future ruff / mypy version bump (which does bump the cache key's `tool_version` component) will replace the stale rows organically.

## 9. Public-Boundary Discipline

`trikon.evidence.report` remains the SDK's public boundary. Two invariants hold after this bugfix:

- **`trikon.evidence.report` still does not import from `trikon.change_intel.*`.** The `ChangeKind` literal is duplicated (four string values, pinned by Requirement 1.4), not imported. If the internal `trikon.change_intel.models.ChangeKind` ever grows a fifth value, the two literals will diverge and the plumbing site (`_public_file_changes_sorted` in `blast_radius.py`) will fail mypy `--strict` because the internal `ChangeKind` value will not be a member of the public `ChangeKind` literal. That is the intended forcing function: the public boundary is an intentional contract, not an accidental one.
- **`trikon.change_intel.blast_radius` now imports `FileChangeInfo` from `trikon.evidence.report`.** This is already the direction the module import DAG runs (`blast_radius` → `evidence.report`, via the existing `ImpactSet` and `SymbolRef` imports through `_public_refs_sorted`). No new import cycle.

## 10. Error Handling

- `_filter_by_head_existence` never raises. Every branch is total over its input types (empty-list branch, non-empty branch). The set-membership tests inside the generator expression cannot raise on any string.
- `_public_file_changes_sorted` never raises. Pydantic's `FileChangeInfo(...)` construction is total for the four `ChangeKind` values and the three field types.
- `run_static_checks` continues to raise `StaticCheckError` on all documented failure modes. The new `continue` on empty composed filter output does not raise; it is a normal successful skip and is identical in shape to the existing skip introduced by v0.3.1's `_filter_by_suffix`.
- `ImpactSet.model_validate` on a payload missing the `file_changes` key does not raise — the default `Field(default_factory=list)` supplies `[]` (Requirement 1.8, 8.4).

## 11. Testing Strategy

**Unit tests** (in `tests/unit/verify/` and `tests/unit/change_intel/`):

- `_filter_by_head_existence` — edge cases: empty `file_changes` → identity; only `deleted` entries → drop all matching paths; only `renamed` entries with `old_path` → drop matching rename-source paths; mixed kinds; input order preservation; input not mutated; calling twice yields identical output.
- `FileChangeInfo` — Pydantic construction with each of the four `ChangeKind` values; `old_path=None` default; `old_path` non-`None` accepted when `change_kind == "renamed"`; JSON round-trip via `model_dump_json` and `model_validate`.
- `ImpactSet` backward-compat — constructing without `file_changes` yields `file_changes == []`; deserializing a JSON payload without the `file_changes` key yields `file_changes == []`; the `EMPTY_IMPACT_SET` sentinel carries `file_changes == []`.
- `_public_file_changes_sorted` — output is sorted by `path`; rename `FileChange.old_path` is preserved on the FileChangeInfo; non-rename entries carry `old_path is None`.
- Head-side skip on all-deleted change — feed `run_static_checks` an `ImpactSet` where every `changed_files` entry has a matching `deleted` `file_changes` entry; assert `sandbox.exec` is not called for either tool; assert `StaticReport.tools_run == ["ruff", "mypy"]`; assert `StaticReport.findings == []`; assert all three counters are 0.
- Head-side partial filter — mix `deleted` and `modified` `.py` entries; assert `sandbox.exec` is called with argv tokens that exclude the deleted paths.

**Property tests** (using hypothesis, sibling module `tests/unit/verify/test_static_checks_head_existence_property.py`):

- **Property 1** — filter contract: `_filter_by_head_existence` output is a subsequence of the input paths, and no output path has a matching `deleted` or rename-source `file_changes` entry.
- **Property 2** — no argv token names a deleted-at-head path: for any `impact.changed_files` and any `impact.file_changes`, no argv token substituted for the `{files}` sentinel in `_expand_argv(t.argv_template, _filter_by_head_existence(_filter_by_suffix(paths, t.accepted_suffixes), file_changes))` names a path whose `file_changes` entry has `change_kind == "deleted"`.

**Integration checkpoint** (in `tests/integration/verify/`):

- Click_Repro test — `sdk.verify(...)` twice against the click repo at base `87f7a31` and head `6aabf09`, once with `no_sandbox=False` and once with `no_sandbox=True`. Assert `verdict.decision` matches across both invocations; assert `verdict.evidence.verification.static.new_errors` matches; assert no finding in either invocation has `rule_id == "E902"` on a path whose `file_changes` entry has `change_kind == "deleted"`. This is the deliverable-named property from Requirement 7.

## 12. Alternatives Considered

- **Alternative A: Include a five-value `Literal["added", "modified", "deleted", "renamed_from", "renamed_to"]` on the public boundary and emit two `FileChangeInfo` entries per rename (one at `old_path` with kind `renamed_from`, one at `path` with kind `renamed_to`).** Rejected. The internal `ChangeKind` is four-valued; mirroring it keeps the public and internal shapes 1:1 aligned. A five-value split doubles the cardinality of `file_changes` for rename-heavy diffs and requires a two-way mapping at the plumbing site (`_public_file_changes_sorted`). The current design carries the rename-source path in the `old_path` field of the single `renamed` entry — same information, half the cardinality, one canonical source of truth per rename. The head-existence filter still drops rename-source paths defensively (Section 5), so Requirement 3.3 is satisfied without the alphabet expansion.
- **Alternative B: Filter at `_expand_argv` by having it accept `impact.file_changes` and drop deleted paths internally.** Rejected. `_expand_argv` is a pure mechanical substitution; coupling it to `ImpactSet` semantics creates a filter-of-filter concern that the two-helper design cleanly separates. Both `_filter_by_suffix` (v0.3.1) and `_filter_by_head_existence` (this bugfix) are called explicitly at the raise site; `_expand_argv` remains a `{files}`-substitution primitive.
- **Alternative C: Filter at the sandbox layer by having `sandbox.exec` skip nonexistent files.** Rejected. Two problems: (1) the sandbox does not know which of its argv tokens is meant to be a file path — argv can contain flags, sentinels, or unrelated arguments; (2) the sandbox is polymorphic (`LocalDockerSandbox` and `LocalSubprocessSandbox`) and duplicating the filter across backends is exactly the divergence this bugfix is fixing. The filter belongs at the callsite that knows both the tool and the change metadata.
- **Alternative D: Add a `read_bytes(path) -> bytes | None` capability to the sandbox and skip files that the sandbox reports as missing.** Rejected. It would require an extra sandbox call per file, add a new capability to the sandbox contract, and still not distinguish "deleted at HEAD" from "existed at HEAD but the mount is broken" — the second case wants to fail loudly, and this alternative fail-opens on both. The metadata-based filter is O(F) with no sandbox round-trips.
- **Alternative E: Fix host-side ruff to also emit E902, so the two paths converge on `decision=block` instead of on `decision=allow`.** Rejected. That converges by making both paths wrong — a spurious ruff finding on a nonexistent file is not a real static-check error. The filter at the callsite is the honest fix.

## 13. Out of Scope

- **Renaming the internal `trikon.change_intel.models.ChangeKind` alphabet.** The internal `Literal["added", "modified", "deleted", "renamed"]` stays as-is. The public `trikon.evidence.report.ChangeKind` is a duplicate declaration by design (§9).
- **`Hunk` metadata on the public boundary.** `FileChange.hunks` is internal — not carried on `FileChangeInfo`. Downstream consumers that need line-level info would have to re-parse the diff or hit a future public helper; both are future work.
- **Baseline-side cache invalidation.** Stale `static_baseline` rows from the pre-fix code path are harmless (§8). No migration required.
- **Runner / CLI / SDK surface.** No changes to `run_verification`, the CLI, or the top-level `sdk.verify`.
- **Windows path normalization inside `_filter_by_head_existence`.** `impact.changed_files` and `impact.file_changes` are already POSIX-normalized upstream in `trikon.change_intel.diff_parser` (unidiff strips the `b/` prefix and git normalizes to forward slashes). The filter compares strings verbatim; if the two ever drift, the divergence surfaces immediately at the plumbing site, not silently at the filter.

## 14. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Head-existence filter contract — subsequence with only head-existent paths

*For any* sequence of paths `P` and any sequence of FileChangeInfo entries `F`, the tuple `_filter_by_head_existence(P, F)` is a subsequence of `P` in original order, and for every element `p` in the output there is no entry in `F` with either (`path == p` and `change_kind == "deleted"`) or (`change_kind == "renamed"` and `old_path == p`). When `F` is empty, the output equals `tuple(P)` (no-op backward-compatibility branch).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 6.1**

### Property 2: No argv token ever names a deleted-at-head path

*For any* `StaticTool` `T` in `DEFAULT_STATIC_TOOLS` and *for any* `ImpactSet` `impact` (with any valid `changed_files` list and any valid `file_changes` list), no argv token substituted for the `{files}` sentinel at the head-side raise site inside `run_static_checks` Step 3 — the composition `_expand_argv(T.argv_template, _filter_by_head_existence(_filter_by_suffix(impact.changed_files, T.accepted_suffixes), impact.file_changes))` — names a path `p` for which `impact.file_changes` contains an entry with `path == p, change_kind == "deleted"` or an entry with `change_kind == "renamed", old_path == p`.

**Validates: Requirements 4.1, 4.2, 4.3, 4.6, 6.1, 6.2**

### Property 3: Docker sandbox and `--no-sandbox` paths agree on the verdict

*For any* `(repo_path, base_sha, head_sha)` triple (in particular, for the Click_Repro at base `87f7a31` and head `6aabf09`), invoking `trikon.sdk.verify(...)` twice with all inputs held constant except `no_sandbox=False` on one call and `no_sandbox=True` on the other yields two `Verdict` values whose `decision`, `matched_rule`, and `evidence.verification.static.{new_errors, new_warnings, preexisting_errors}` fields compare equal. Furthermore, neither verdict's `evidence.verification.static.findings` list contains an entry whose `rule_id == "E902"` and whose `path` matches a `file_changes` entry with `change_kind == "deleted"`.

**Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5**

### Property 4: `_filter_by_head_existence` is pure

*For any* sequence of paths `P` and any sequence of FileChangeInfo entries `F`, calling `_filter_by_head_existence(P, F)` twice returns tuples that compare equal, and neither call mutates `P` or `F`.

**Validates: Requirement 3.7**

### Property 5: `ImpactSet.file_changes` round-trips through JSON

*For any* `ImpactSet` instance `impact` produced by `trikon.change_intel.blast_radius.compute_impact`, `ImpactSet.model_validate(json.loads(impact.model_dump_json()))` produces an `ImpactSet` whose `file_changes` field compares equal to `impact.file_changes`, and an `ImpactSet` JSON payload that omits the `file_changes` key deserializes to an instance whose `file_changes == []` (backward-compat).

**Validates: Requirements 1.7, 1.8, 8.3, 8.4**
