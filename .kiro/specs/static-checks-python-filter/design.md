# Design Document

## 1. Overview

`trikon.verify.static_checks.run_static_checks` runs ruff and mypy against `impact.changed_files`. That list is language-agnostic — it comes from `ChangeIntel` and carries every path the change touched: `.py`, `.pyi`, but also `uv.lock`, `pyproject.toml`, `README.md`, images, config, generated artifacts. Both raise sites in `static_checks.py` — `_expand_argv` on the head side (called by `run_static_checks` Step 3) and `_run_baseline_tool_on_host` on the host side — pass this list verbatim into the tool's argv. Ruff then attempts to parse `uv.lock` as Python and emits an `E902 file not found` or a raw syntax error per line, producing thousands of false-positive findings.

Verified in the field: a Kiro-driven `mcp_trikon_trikon_verify` against `C:\Users\surya\trikon-e2e-scratch\click` (base `87f7a31` → head `6aabf09`, 15 changed files) returned `decision: block` with `4252 static errors`, every one sourced from ruff parsing `uv.lock`.

The fix declares, per `StaticTool`, the suffixes that tool can parse (`{".py", ".pyi"}` for both ruff and mypy) and threads a single filter helper through both raise sites. When the filtered list is empty for a given tool, that tool is skipped cleanly — `sandbox.exec` is not called, `subprocess.run` is not called, `tools_run` still records the tool, and no finding is appended.

This is a **bugfix**. The public shapes of `run_static_checks`, `StaticReport`, `StaticTool`, and `DEFAULT_STATIC_TOOLS` remain backward-compatible: `accepted_suffixes` on `StaticTool` gets a default of `frozenset({".py", ".pyi"})` so any downstream construction site that omits the argument keeps working, and any downstream consumer that already imported `StaticTool` sees an additive field.

## 2. Root Cause Diagnosis (verified against the source)

Two raise-site anchors in `trikon/verify/static_checks.py`:

**Anchor 1 — head side, around line 321 inside `run_static_checks`:**

```python
argv = _expand_argv(tool.argv_template, impact.changed_files)
head_result = sandbox.exec(argv)
```

`impact.changed_files` is passed verbatim to `_expand_argv`, which unconditionally substitutes every element for the `{files}` sentinel (see `_expand_argv` at line ~768). Non-`.py` files reach `sandbox.exec` as argv tokens.

**Anchor 2 — baseline side, inside `_run_baseline_tool_on_host` at line ~715:**

```python
existing = [f for f in changed_files if (worktree_dir / f).is_file()]
if not existing:
    return []
argv = _expand_argv(tool.argv_template, existing)
```

The comprehension filters on `is_file()` but not on suffix. A `uv.lock` that exists in the base worktree passes this check and reaches the baseline subprocess argv.

**Third, subsidiary anchor — the `_expand_argv` helper itself:**

```python
def _expand_argv(argv_template, changed_files):
    expanded = []
    for token in argv_template:
        if token == "{files}":
            expanded.extend(changed_files)
        else:
            expanded.append(token)
    return tuple(expanded)
```

`_expand_argv` is a pure mechanical substitution and does not know about tools. The correct architectural move is to leave it that way and enforce the filter at the two call sites, keyed by `tool.accepted_suffixes`.

## 3. Extension Point on StaticTool

Add a single frozen field to the existing frozen, slotted dataclass in `trikon/verify/models.py`:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StaticTool:
    """A pinned static-analysis tool the runner knows how to invoke."""

    name: str
    argv_template: tuple[str, ...]
    version_command: tuple[str, ...]
    parse_json: bool
    accepted_suffixes: frozenset[str] = field(
        default_factory=lambda: frozenset({".py", ".pyi"}),
    )
```

Design decisions:

- **`frozenset[str]`**, not `tuple[str, ...]` and not `set[str]`. `frozenset` is hashable and immutable — the `StaticTool` dataclass is `frozen=True, slots=True` and used as a hash key in test fixtures and cache-lookup shapes elsewhere in the codebase; a mutable `set` would break the frozen contract. A `tuple` would work for hashability but membership tests on a `tuple` are O(n), and the filter runs this check inside a hot per-file loop.
- **Default `frozenset({".py", ".pyi"})`** via `default_factory` (a per-instance factory), not a shared literal — dataclass-mutable-default rules require this even for `frozenset` (it is immutable but the guardrail is set-typed). The default matches the parseable set for both ruff and mypy.
- **Suffix strings include the leading dot** — matches `pathlib.PurePosixPath.suffix` semantics exactly and avoids a per-call normalization step in the filter.
- **The type is `frozenset[str]`, not `frozenset[str] | None`** — the field is always set. `None` would force a downstream `is None` check at every raise site and invite the "None means accept everything" footgun.
- **The field is additive** — `DEFAULT_STATIC_TOOLS` gets updated to declare `accepted_suffixes` explicitly (best practice, self-documenting), but any construction site outside the codebase that instantiates `StaticTool` positionally keeps working because the new field is at the tail with a default.

Updated `DEFAULT_STATIC_TOOLS` in `trikon/verify/models.py`:

```python
DEFAULT_STATIC_TOOLS: tuple[StaticTool, ...] = (
    StaticTool(
        name="ruff",
        argv_template=("ruff", "check", "--output-format=json", "{files}"),
        version_command=("ruff", "--version"),
        parse_json=True,
        accepted_suffixes=frozenset({".py", ".pyi"}),
    ),
    StaticTool(
        name="mypy",
        argv_template=("mypy", "--no-color-output", "--show-column-numbers", "{files}"),
        version_command=("mypy", "--version"),
        parse_json=False,
        accepted_suffixes=frozenset({".py", ".pyi"}),
    ),
)
```

## 4. Filter Helper

New module-level helper in `trikon/verify/static_checks.py`, colocated with `_expand_argv`:

```python
from collections.abc import Sequence
from pathlib import PurePosixPath


def _filter_by_suffix(
    paths: Sequence[str],
    accepted_suffixes: frozenset[str],
) -> tuple[str, ...]:
    """Return the subsequence of ``paths`` whose suffix is in ``accepted_suffixes``.

    The filter is the sole gate between ``impact.changed_files`` and the two
    tool-invocation raise sites (``_expand_argv`` at the head side,
    ``_run_baseline_tool_on_host`` at the baseline side). Anything downstream of
    this helper trusts that every path it sees has a suffix the tool can parse.

    Suffix semantics match ``pathlib.PurePosixPath.suffix``:
    ``_filter_by_suffix(["foo.py"], frozenset({".py"}))`` includes ``foo.py``;
    ``_filter_by_suffix(["Makefile"], frozenset({".py"}))`` excludes ``Makefile``
    because its suffix is the empty string; ``_filter_by_suffix(["foo.tar.gz"],
    frozenset({".gz"}))`` includes ``foo.tar.gz`` because ``PurePosixPath.suffix``
    is the last suffix component.

    Args:
        paths: The file paths to filter. Order is preserved.
        accepted_suffixes: The suffix set to keep. Each string SHOULD include the
            leading dot (``".py"``, not ``"py"``); a bare ``"py"`` will never match
            any real path suffix. An empty frozenset yields an empty output tuple.

    Returns:
        A tuple whose elements are a subsequence of ``paths`` in original order,
        containing exactly those paths whose ``PurePosixPath.suffix`` is a member
        of ``accepted_suffixes``.
    """
    if not accepted_suffixes:
        return ()
    return tuple(p for p in paths if PurePosixPath(p).suffix in accepted_suffixes)
```

Design decisions:

- **`PurePosixPath`**, not `PurePath` or `Path`. `impact.changed_files` carries git-relative paths that always use forward slashes regardless of host OS (git normalizes). `PurePath` on Windows would treat `foo.py` inside a `dir\subdir/` fragment as a mixed-separator path; `PurePosixPath` is unambiguous. Using `PurePath` risks a Windows-specific bug where `.suffix` is computed incorrectly on backslash-containing paths coming from a caller who did not normalize.
- **Pure function, no I/O.** Existence checks belong on the baseline side (`worktree_dir / f).is_file()`), not in the filter — the head side is a sandbox and the local filesystem view is irrelevant.
- **Empty `accepted_suffixes` short-circuits** to `()` — trivially correct and avoids running the generator.
- **Returns `tuple[str, ...]`**, not `list`. Callers pass it into `_expand_argv` which itself returns a tuple; using `tuple` throughout keeps the argv shape consistent and mypy-strict clean.
- **Order preserving.** Ruff and mypy emit findings in the order they process files; preserving argv order preserves the finding order in `StaticReport.findings`.

## 5. Threading the Filter Through the Head-Side Raise Site

Inside `run_static_checks` Step 3 (currently around line 321):

```python
# --- current ---
argv = _expand_argv(tool.argv_template, impact.changed_files)
head_result = sandbox.exec(argv)

parsed_head: list[dict[str, str | int]]
if tool.parse_json:
    parsed_head = _parse_ruff_json(head_result.stdout)
else:
    parsed_head = _parse_mypy_text(head_result.stdout)

# --- proposed ---
filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)
if not filtered:
    # No files this tool can parse. Record the tool ran with zero findings
    # and skip the sandbox exec. tools_run.append(tool.name) already ran at
    # the top of the per-tool loop, so no further bookkeeping is needed.
    continue

argv = _expand_argv(tool.argv_template, filtered)
head_result = sandbox.exec(argv)

parsed_head: list[dict[str, str | int]]
if tool.parse_json:
    parsed_head = _parse_ruff_json(head_result.stdout)
else:
    parsed_head = _parse_mypy_text(head_result.stdout)
```

Behavioral notes:

- `tools_run.append(tool.name)` runs unconditionally at the top of the per-tool loop (already the case in the current source at line ~303), so a `continue` on the empty-filter branch correctly preserves the "tool ran, zero findings" contract of Requirement 3.3.
- Step 2 (`_resolve_base_keys`, which populates `base_keys` for the `is_new` diff) still runs even when the head-side filtered list is empty. That is intentional: `_resolve_base_keys` also passes `impact.changed_files` to `_run_baseline_tool_on_host`, and the baseline-side filter (Section 6) is what protects the baseline invocation. `base_keys` on an all-non-Python change reduces to `frozenset()`, which is cheap.
  - **Alternative considered**: short-circuit `_resolve_base_keys` when the filtered head-side list is empty. Rejected because it entangles two concerns (baseline resolve and head-side skip) and because `_resolve_base_keys` performs its own tool-scoped cache lookup that we do not want to short-circuit on a policy that lives at the head-side raise site.
- The `continue` skips Step 4 (`is_new` diff) and Step 5 (aggregate counter update) — correct, since there are no head findings to diff or count.

## 6. Threading the Filter Through the Baseline-Side Raise Site

Inside `_run_baseline_tool_on_host` (currently around line 715):

```python
# --- current ---
existing = [f for f in changed_files if (worktree_dir / f).is_file()]
if not existing:
    return []
argv = _expand_argv(tool.argv_template, existing)

# --- proposed ---
filtered = _filter_by_suffix(changed_files, tool.accepted_suffixes)
if not filtered:
    return []
existing = [f for f in filtered if (worktree_dir / f).is_file()]
if not existing:
    return []
argv = _expand_argv(tool.argv_template, existing)
```

Design decisions:

- **Filter first, existence second.** Ordering matters for two reasons: (1) a non-`.py` path never touches the filesystem via `is_file()`, which is what Requirement 4.2 pins down; (2) if the filter alone empties the list, we return without spawning the tool subprocess even in the rare case where the worktree does not exist yet.
- **Two `if not ...: return []` gates**, not one merged expression. Splitting them makes the log-and-return semantics of Requirement 4.4 (no raise, no WARNING, no timing measurement) trivially inspectable at the raise site.
- The existing PATH augmentation, `shutil.which`, and `subprocess.run` block below the argv construction is unchanged.

## 7. Threading the Filter Through `_resolve_base_keys`

`_resolve_base_keys` calls `_run_baseline_tool_on_host` with `changed_files=impact.changed_files`. No change is required at this call site — the filter fires inside `_run_baseline_tool_on_host` (Section 6). This keeps the responsibility at the raise site.

`_resolve_base_keys` also performs the SQLite cache lookup on `(base_sha, tool.name, tool_version)`. That key is unchanged. The cache stores the parsed findings for the base; a cache hit on a prior run that (buggy) included non-`.py` findings would return them. Two mitigations:
- The cache is keyed by `tool_version`; a Trikon version bump that ships this fix will typically coincide with a ruff/mypy version bump in `pyproject.toml`, invalidating stale rows.
- The head-side filter guarantees no head finding has a non-`.py` path, so a stale non-`.py` entry in `base_keys` cannot produce a spurious `is_new=False` on any head finding — the head side simply never emits that path.

Cache invalidation on this bugfix alone is **not required**. The stale rows are harmless: they inflate `base_keys` slightly but never match any head finding.

## 8. Module Docstring Update

The `trikon/verify/static_checks.py` module docstring currently enumerates a five-step algorithm (Version capture → Baseline resolve → Head-side run → is_new diff → Report assembly). Insert a "Step 0" or fold into Step 3 the explicit mention of the suffix filter, name the discriminator (`StaticTool.accepted_suffixes`) and the helper (`_filter_by_suffix`), and note the skip-on-empty behavior.

Proposed insertion (fold into the intro block before the algorithm enumeration and re-emphasize inside Step 3):

```
Suffix filter
-------------

Before every tool invocation — head side and baseline side alike — the
head-side changed-file list from ``impact.changed_files`` is filtered
through :func:`_filter_by_suffix` against ``tool.accepted_suffixes``.
Ruff and mypy both declare ``frozenset({".py", ".pyi"})``; any file
whose suffix is outside that set (``uv.lock``, ``pyproject.toml``,
``README.md``, images, generated artifacts) is dropped before argv is
built. When the filtered list is empty for a given tool, that tool is
skipped cleanly — ``sandbox.exec`` is not called, ``subprocess.run``
is not called, ``tools_run`` still records the tool ran, and no
finding is appended.

This is the sole gate. Downstream code (``_expand_argv``,
``sandbox.exec``, ``subprocess.run``) trusts that every path it sees
has a suffix the tool can parse.
```

And in Step 3's algorithm entry:

```
3. **Head-side run** — for each tool, filter ``impact.changed_files``
   through :func:`_filter_by_suffix` against ``tool.accepted_suffixes``,
   skip cleanly if the filtered list is empty, otherwise expand the
   ``{files}`` sentinel in ``argv_template`` with the filtered list and
   invoke it inside the sandbox. …
```

## 9. Out of Scope

- **Deleted files on the head side.** The bug ticket notes that files with `change_kind == "deleted"` can trigger `E902 file not found` on the head-side sandbox invocation. `impact.changed_files` today does not carry `change_kind`, so this cannot be addressed at the raise-site anchor without changing the `ImpactSet` contract. Deferred: a follow-up spec should thread `change_kind` through `ImpactSet` and add a "must exist in head tree" filter at the head-side raise site.
- **`_expand_argv` signature.** `_expand_argv` remains a pure mechanical substitution. Pushing the filter into `_expand_argv` would require it to know about `StaticTool`, which creates a coupling not warranted for a bugfix. The two call sites do the filter; `_expand_argv` does the substitution.
- **Baseline cache invalidation.** The `static_baseline` cache key is unchanged. Stale rows containing non-`.py` findings are harmless (Section 7).
- **Runner / SDK / CLI surface.** No changes.

## 10. Error Handling

- `_filter_by_suffix` never raises. `PurePosixPath(p).suffix` is total over all string inputs (empty string → empty suffix → excluded when non-empty accepted set).
- `_run_baseline_tool_on_host` continues to wrap the subprocess in the existing `try/except TimeoutExpired/FileNotFoundError` block. The new early return on empty filtered list precedes that block, so no exception path changes.
- `run_static_checks` continues to raise `StaticCheckError` on all documented failure modes. The new `continue` on empty head-side filtered list does not raise; it is a normal successful skip.

## 11. Testing Strategy

**Unit tests** (in `tests/unit/verify/`, following the pattern in `test_static_checks_smoke.py`):

- `_filter_by_suffix` — edge cases: empty accepted set, path with no suffix, path with unicode filename, path with mixed case suffix (`.PY` should not match `.py` — deliberate, matches `PurePosixPath.suffix` semantics), input order preservation, `frozenset` argument.
- Head-side skip path — feed a `run_static_checks` invocation a changed_files list of only non-`.py` entries with a fake sandbox that records `exec` calls; assert `sandbox.exec` was not called, `StaticReport.tools_run` contains every tool name, `StaticReport.findings` is empty, all three counters are 0.
- Baseline-side skip path — call `_run_baseline_tool_on_host` with non-`.py`-only `changed_files`, monkeypatch `subprocess.run` to raise if called; assert `[]` returned and `subprocess.run` not called.

**Property tests** (using hypothesis, one file per property or grouped by property in a sibling module):

- **Property 1 test** — generate `Sequence[str]` of file-path-shaped strings (mixing accepted suffixes, non-accepted suffixes, no suffix, unicode, long paths) and arbitrary `frozenset[str]` accepted-suffix sets; assert `_filter_by_suffix` output is a subsequence of input and every element has an in-accepted suffix.
- **Property 2 test** — the deliverable-named property. Generate `Sequence[str]` of file-path-shaped strings; for every `StaticTool` in `DEFAULT_STATIC_TOOLS`, build the head-side argv (`_expand_argv(t.argv_template, _filter_by_suffix(paths, t.accepted_suffixes))`) and the would-be baseline argv (same path, since `_run_baseline_tool_on_host` uses the same filter before existence intersection); assert no token substituted for `{files}` has a suffix outside `t.accepted_suffixes`.

Both property tests run at hypothesis's default of 100 examples per configuration; both tests are optional sub-tasks (marked `*` in `tasks.md`) but recommended.

## 12. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Filter Helper contract — subsequence with in-accepted suffixes

*For any* sequence of paths `P` and any frozenset of accepted suffixes `S`, the tuple `_filter_by_suffix(P, S)` is a subsequence of `P` in original order, and every element `p` in the output satisfies `PurePosixPath(p).suffix in S`. When `S` is the empty frozenset, the output is the empty tuple.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 5.1**

### Property 2: No non-accepted-suffix path reaches ruff or mypy argv

*For any* `StaticTool` `T` in `DEFAULT_STATIC_TOOLS` and *for any* sequence of paths `P` passed as `impact.changed_files`, no argv token substituted for the `{files}` sentinel — at either the head-side raise site inside `run_static_checks` Step 3 or the baseline-side raise site inside `_run_baseline_tool_on_host` — has a `PurePosixPath.suffix` outside `T.accepted_suffixes`.

**Validates: Requirements 3.1, 3.5, 4.1**

### Property 3: Filter Helper is pure

*For any* sequence of paths `P` and any frozenset of accepted suffixes `S`, calling `_filter_by_suffix(P, S)` twice returns tuples that compare equal, and the call does not mutate `P` or `S`.

**Validates: Requirement 2.6**
