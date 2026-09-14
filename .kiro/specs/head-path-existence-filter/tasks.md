# Implementation Plan: head-path-existence-filter

## Overview

Bugfix for Trikon v0.3.4: `trikon/verify/static_checks.py::run_static_checks` Step 3 passes every `.py` / `.pyi` entry from `impact.changed_files` into ruff and mypy argv, including files with `change_kind == "deleted"` at HEAD. Inside the Docker sandbox those paths do not exist and ruff emits `E902: No such file or directory (os error 2)`, one per missing file. Those E902s land in `StaticReport.findings` as new errors and drive the verdict to `decision=block` — a false positive on a "there is nothing to inspect here" situation. The `--no-sandbox` host path silently skips the same files, so the two backends of the same product disagree on the verdict for the same commit range.

The fix has three moving parts:
1. Extend `trikon.evidence.report.ImpactSet` with a new additive `file_changes: list[FileChangeInfo]` field that carries per-file `change_kind` (default `[]` for backward compat).
2. Plumb the `change_kind` value that `trikon.change_intel.diff_parser` already computes through `trikon.change_intel.blast_radius.compute_impact` into the new field.
3. Add a head-existence filter helper `_filter_by_head_existence` in `trikon.verify.static_checks` (mirror of `_filter_by_suffix` from v0.3.1) and compose it with the suffix filter at the head-side raise site inside Step 3 — before argv is built.

`run_static_checks` public signature is unchanged. `StaticReport`, `StaticTool`, and `DEFAULT_STATIC_TOOLS` are unchanged. The `_run_baseline_tool_on_host` baseline-side raise site already covers the mirror case defensively via `(worktree_dir / f).is_file()`; no functional change is needed there.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [ ] 1. Extend the public `ImpactSet` model with per-file change metadata
  - [ ] 1.1 In `trikon/evidence/report.py`, add a module-level `ChangeKind = Literal["added", "modified", "deleted", "renamed"]` type alias (mirror of `trikon.change_intel.models.ChangeKind` — declared duplicate, not imported, per design.md §9). Add a Pydantic `FileChangeInfo(BaseModel)` with fields `path: str`, `change_kind: ChangeKind`, `old_path: str | None = None`. Extend `ImpactSet` with `file_changes: list[FileChangeInfo] = Field(default_factory=list)`. Update the `EMPTY_IMPACT_SET` sentinel to pass `file_changes=[]` explicitly. Export `ChangeKind` and `FileChangeInfo` from the module. Verify with `uv run mypy --strict trikon/evidence/report.py`.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 8.2_

- [ ] 2. Plumb `change_kind` from `diff_parser` through `blast_radius` onto `ImpactSet`
  - [ ] 2.1 In `trikon/change_intel/blast_radius.py`, add a private module-level helper `_public_file_changes_sorted(files: tuple[FileChange, ...]) -> list[FileChangeInfo]` that translates each `FileChange` to a `FileChangeInfo`, copies `path` and `change_kind` verbatim, sets `old_path = fc.old_path if fc.change_kind == "renamed" else None`, and returns the list sorted by `path` (POSIX-lexicographic ascending) so identical inputs produce byte-identical JSON. Import `FileChangeInfo` from `trikon.evidence.report`. Update both `ImpactSet(...)` construction sites — `_compute_python_impact` (around line 297) and `_empty_python_impact` (around line 319) — to pass `file_changes=_public_file_changes_sorted(change_set.files)` as an additional keyword argument. Leave every other field on both constructions unchanged.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 8.1_

- [ ] 3. Add the head-existence filter helper in `static_checks.py`
  - [ ] 3.1 In `trikon/verify/static_checks.py`, add a module-level helper `_filter_by_head_existence(paths: Sequence[str], file_changes: Sequence[FileChangeInfo]) -> tuple[str, ...]` colocated with the existing `_filter_by_suffix`. Import `FileChangeInfo` from `trikon.evidence.report` (import block, not lazy). Behavior: when `file_changes` is empty return `tuple(paths)` unchanged (backward-compat no-op branch, per design.md §5); otherwise build a `dropped: set[str]` by scanning `file_changes` once — add `info.path` when `info.change_kind == "deleted"`, add `info.old_path` when `info.change_kind == "renamed" and info.old_path is not None` — and return `tuple(p for p in paths if p not in dropped)`. The helper is pure — no I/O, no mutation of arguments, order-preserving. Do NOT wire it into `run_static_checks` yet — that is task 4.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 6.1_

- [ ] 4. Wire the head-existence filter into the head-side raise site
  - [ ] 4.1 In `trikon/verify/static_checks.py`, modify `run_static_checks` Step 3 (currently around line 358 in the per-tool loop, immediately after the `_resolve_base_keys` call). Replace the current `filtered = _filter_by_suffix(...)` / `if not filtered: continue` / `argv = _expand_argv(...)` block with a composed-filter block: (1) `filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)`; (2) `filtered = _filter_by_head_existence(filtered, impact.file_changes)`; (3) `if not filtered: continue` — the `tools_run.append(tool.name)` at the top of the loop already ran, so skipping here correctly records "tool ran, zero findings, zero counter increments"; (4) `argv = _expand_argv(tool.argv_template, filtered)`; (5) `head_result = sandbox.exec(argv)` — unchanged. Do NOT change the order (suffix first, head-existence second — Requirement 4.2). Do NOT modify `_resolve_base_keys`, `_run_baseline_tool_on_host`, the ruff/mypy parsers, the `is_new` diff, the counter update, or the module docstring in this task.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.2, 6.3_

- [ ] 5. Update the module docstring to document the head-existence filter
  - [ ] 5.1 In `trikon/verify/static_checks.py`, extend the module docstring's "Suffix filter" section (already added in v0.3.1) with a "Head-existence filter" companion section that describes: (a) the filter fires at the head-side raise site only, immediately after the suffix filter and before argv is built; (b) `impact.file_changes` (new on v0.3.4) is the metadata source and `_filter_by_head_existence` is the helper; (c) any path whose FileChangeInfo entry has `change_kind == "deleted"` (or `change_kind == "renamed"` with the path as `old_path`) is dropped from the head-side argv; (d) when `impact.file_changes` is empty (backward-compat with a pre-v0.3.4 producer), the filter is a no-op; (e) the baseline-side raise site does NOT apply this filter — it continues to rely on the `(worktree_dir / f).is_file()` intersection against the base worktree, which correctly rejects paths absent from BASE (design.md §7). Also update Step 3's algorithm entry to name the composed filter explicitly ("filter `impact.changed_files` through `_filter_by_suffix` then `_filter_by_head_existence`, skip cleanly if the composed output is empty …").
    - _Requirements: 4.1, 4.2, 5.1, 5.2_

- [ ] 6. Unit tests for the new model, plumbing, filter, and head-side skip path
  - [ ]* 6.1 Add unit tests in `tests/unit/verify/test_static_checks_head_existence.py` (new file, following the style of `tests/unit/verify/test_static_checks_smoke.py`) covering: (a) `_filter_by_head_existence` edge cases — empty `file_changes` returns `tuple(paths)` unchanged; only-`deleted` `file_changes` drops every matching path; `renamed` entry with `old_path == p` drops `p`; `renamed` entry with `old_path is None` drops nothing; `added` and `modified` entries drop nothing; input order preserved; input list not mutated; calling twice yields identical output; unicode paths handled; (b) head-side skip on all-deleted change — feed `run_static_checks` an `ImpactSet` with three `.py` `changed_files` all marked `deleted` in `file_changes`, use a fake `Sandbox` that raises on `exec`, assert no `exec` was called, assert `StaticReport.tools_run == [t.name for t in DEFAULT_STATIC_TOOLS]`, assert `StaticReport.findings == []`, assert `new_errors == new_warnings == preexisting_errors == 0`; (c) head-side partial filter — mix `deleted` and `modified` `.py` in one `ImpactSet`, use a fake `Sandbox` that captures argv, assert every substituted argv token is a modified path and no deleted path; (d) empty-`file_changes` backward-compat — feed an `ImpactSet` with the pre-v0.3.4 shape (`file_changes=[]`), assert the head-side argv contains every `.py` in `changed_files` (identity fallback). Also add unit tests in `tests/unit/change_intel/test_blast_radius_file_changes.py` (new file) covering: (e) `_public_file_changes_sorted` output is sorted by `path`; (f) rename `FileChange.old_path` is preserved on the `FileChangeInfo`; (g) non-rename entries carry `old_path is None`; (h) both `_compute_python_impact` and `_empty_python_impact` populate `file_changes` from `change_set.files`. Finally add unit tests in `tests/unit/test_report_models.py` (extend existing file or create it) covering: (i) constructing `ImpactSet` without `file_changes` yields `file_changes == []`; (j) `ImpactSet.model_validate` on a JSON payload lacking the `file_changes` key materializes `file_changes == []`; (k) `EMPTY_IMPACT_SET.file_changes == []`; (l) JSON round-trip: `ImpactSet.model_validate_json(impact.model_dump_json()).file_changes == impact.file_changes` for each of the four `ChangeKind` values.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.3, 4.4, 4.5, 4.6, 6.1, 6.3, 8.1, 8.2, 8.3, 8.4_

- [ ] 7. Property tests for the filter contract and the "no deleted path in argv" invariant
  - [ ]* 7.1 Add hypothesis-based property tests in `tests/unit/verify/test_static_checks_head_existence_property.py` (new file). Use `hypothesis.strategies.text()` composed into path-shaped strings and `hypothesis.strategies.builds(FileChangeInfo, ...)` for the `file_changes` generator (with `change_kind` drawn from `st.sampled_from(["added", "modified", "deleted", "renamed"])` and `old_path` conditioned on `change_kind == "renamed"` via `st.just(None)` vs a path strategy).
    - **Property 1: Head-existence filter contract — subsequence with only head-existent paths**
    - **Property 2: No argv token ever names a deleted-at-head path** — for every `StaticTool` in `DEFAULT_STATIC_TOOLS`, no argv token substituted for the `{files}` sentinel in `_expand_argv(t.argv_template, _filter_by_head_existence(_filter_by_suffix(paths, t.accepted_suffixes), file_changes))` names a path whose FileChangeInfo entry has `change_kind == "deleted"` or (`change_kind == "renamed"` and `old_path == token`).
    - **Property 4: `_filter_by_head_existence` is pure** — calling it twice yields equal tuples and does not mutate its arguments.
    - Configure `@settings(max_examples=100)` at minimum on each property. Tag each property test docstring with `Feature: head-path-existence-filter, Property N: <property text>`.
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.6, 6.1, 6.2**

- [ ] 8. Integration checkpoint — end-to-end verification on the click repro
  - [ ]* 8.1 Add an integration test in `tests/integration/verify/test_click_repro_head_existence.py` (new file, marked `@pytest.mark.integration`). The test acquires a shallow clone of `pallets/click` pinned to a repo-local fixture (or skips with a clear reason if the fixture is unavailable in CI), checks out the base `87f7a31` and head `6aabf09` SHAs, and invokes `trikon.sdk.verify(repo_path=<click clone>, base_sha="87f7a31", head_sha="6aabf09")` twice — once with `no_sandbox=False` (Docker path, skip the test with a clear reason if Docker is not reachable via `create_sandbox`'s daemon check) and once with `no_sandbox=True` (host path). Assert: (a) `docker_verdict.decision == host_verdict.decision`; (b) `docker_verdict.matched_rule == host_verdict.matched_rule`; (c) `docker_verdict.evidence.verification.static.new_errors == host_verdict.evidence.verification.static.new_errors`; (d) neither verdict's `static.findings` contains an entry with `rule_id == "E902"` on a path whose `file_changes` entry has `change_kind == "deleted"`; (e) both verdicts contain a non-empty `evidence.change.file_changes` list (sanity check that the plumbing from tasks 1-2 landed).
    - **Property 3: Docker sandbox and `--no-sandbox` paths agree on the verdict**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5**

- [ ] 9. Checkpoint — verify all tests pass and mypy --strict is clean
  - Run `uv run pytest tests/unit/verify/ tests/unit/change_intel/ tests/unit/test_report_models.py`, `uv run mypy --strict trikon/verify/static_checks.py trikon/evidence/report.py trikon/change_intel/blast_radius.py`, and (if Docker is available) `uv run pytest -m integration tests/integration/verify/test_click_repro_head_existence.py`. Update every checked-in fixture under `tests/fixtures/expected_impact/*.json` to include the new `file_changes` field (regenerate via the existing authoring-time helper if one exists, otherwise hand-edit to add `"file_changes": [...]` sorted by `path`, matching Requirement 2.6 / 8.5). Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP, but task 7.1 encodes the deliverable-named property (**Property 2: no argv token ever names a deleted-at-head path**) and task 8.1 encodes the other deliverable-named property (**Property 3: Docker and `--no-sandbox` paths agree on the verdict**). Both are strongly recommended even for MVP.
- Each task references specific requirements for traceability.
- Task 1 (`report.py`) must complete before task 2 (`blast_radius.py` imports `FileChangeInfo` from `report.py`) and before task 3 (`static_checks.py` imports `FileChangeInfo` from `report.py`).
- Tasks 2 and 3 both consume the new `FileChangeInfo` model from task 1 but do not depend on each other — they touch different files (`blast_radius.py` vs `static_checks.py`) and can run in parallel.
- Tasks 4 (wire the filter) and 5 (docstring update) both edit `trikon/verify/static_checks.py`; they are placed in different waves to avoid write conflicts.
- Task 5 (docstring) depends on task 4 (implementation) because the docstring describes the composed-filter algorithm the implementation now runs.
- Tasks 6, 7, 8 are test-only and can run in parallel once tasks 1-5 are done. Task 8's integration test depends on the fixture-update work at the end of task 9, so it is placed after task 7 in the dependency graph and consolidated with the checkpoint.
- No git operations are performed in this plan against the Trikon parent repo — the parent Trikon repo's commit / push cadence is the user's responsibility, as required by the bugfix constraints ("NO git operations against the Trikon parent repo — no commits, pushes, resets, stashes"). The optional integration test in task 8.1 may clone `pallets/click` into a temporary directory owned by the test, but only for its own scratch area; it must NOT touch the Trikon repo's git state.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["4.1"] },
    { "id": 3, "tasks": ["5.1"] },
    { "id": 4, "tasks": ["6.1", "7.1"] },
    { "id": 5, "tasks": ["8.1"] }
  ]
}
```
