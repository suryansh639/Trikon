# Implementation Plan: static-checks-python-filter

## Overview

Bugfix for Trikon v0.3.1: `trikon/verify/static_checks.py` currently runs ruff and mypy against every entry in `impact.changed_files`, including non-Python files (`uv.lock`, `pyproject.toml`, `README.md`, images) — producing thousands of false-positive findings and blocking clean commits with `decision: block`.

The fix declares `accepted_suffixes: frozenset[str]` on `StaticTool` (default `frozenset({".py", ".pyi"})`), introduces a single filter helper `_filter_by_suffix`, and threads it through both raise sites (`_expand_argv` call site in `run_static_checks` Step 3, and `_run_baseline_tool_on_host` before the existence intersection). When the filtered list is empty for a given tool, that tool is skipped cleanly — `tools_run` still records the tool, no findings appended, no counters incremented.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Extend `StaticTool` descriptor with `accepted_suffixes`
  - [x] 1.1 In `trikon/verify/models.py`, add `accepted_suffixes: frozenset[str] = field(default_factory=lambda: frozenset({".py", ".pyi"}))` to `StaticTool`, and update both entries of `DEFAULT_STATIC_TOOLS` (ruff and mypy) to declare `accepted_suffixes=frozenset({".py", ".pyi"})` explicitly. Keep the dataclass `frozen=True, slots=True`. Import `field` from `dataclasses`. Verify with `uv run mypy --strict trikon/verify/models.py`.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Add suffix filter helper and wire into the head-side raise site
  - [x] 2.1 In `trikon/verify/static_checks.py`, add module-level helper `_filter_by_suffix(paths: Sequence[str], accepted_suffixes: frozenset[str]) -> tuple[str, ...]` using `pathlib.PurePosixPath` (add the import). Return `()` when `accepted_suffixes` is empty; otherwise return a tuple of paths whose `PurePosixPath(p).suffix in accepted_suffixes`, preserving input order. In `run_static_checks` Step 3 (currently around line 321), replace `argv = _expand_argv(tool.argv_template, impact.changed_files)` with a filter-first flow: compute `filtered = _filter_by_suffix(impact.changed_files, tool.accepted_suffixes)`; if `filtered` is empty, `continue` (skipping `sandbox.exec`, Step 4 diff, Step 5 counter updates) — `tools_run.append(tool.name)` at the top of the loop already ran, so tools_run is correctly populated; otherwise pass `filtered` to `_expand_argv` and proceed as before.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 5.1, 5.3_

- [x] 3. Wire filter into the baseline-side raise site
  - [x] 3.1 In `trikon/verify/static_checks.py`, modify `_run_baseline_tool_on_host` (currently around line 715). Before the existing `existing = [f for f in changed_files if (worktree_dir / f).is_file()]` comprehension, insert `filtered = _filter_by_suffix(changed_files, tool.accepted_suffixes)` and `if not filtered: return []`. Change the existence comprehension to iterate over `filtered` instead of `changed_files`. The order-of-operations invariant (suffix filter before I/O) is what Requirement 4.2 pins down. Leave the PATH augmentation, `shutil.which`, `subprocess.run`, timeout wrapping, and parser dispatch below the argv construction unchanged.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.2_

- [ ] 4. Unit tests
  - [ ]* 4.1 Add unit tests in `tests/unit/verify/test_static_checks_suffix_filter.py` (new file, following the style of `tests/unit/verify/test_static_checks_smoke.py`). Cover: (a) `_filter_by_suffix` edge cases — empty `accepted_suffixes` returns `()`, paths with no suffix are excluded, paths with mixed-case suffix are excluded (`.PY` does not match `.py`), unicode filenames are handled, input order is preserved, calling twice yields identical output, input list is not mutated; (b) `StaticTool` default — constructing `StaticTool(...)` without `accepted_suffixes` yields `frozenset({".py", ".pyi"})`, and mutating the field raises `FrozenInstanceError`; (c) `DEFAULT_STATIC_TOOLS` — both ruff and mypy declare `accepted_suffixes == frozenset({".py", ".pyi"})`; (d) head-side skip — feed `run_static_checks` a changed_files list of only non-`.py` entries with a fake sandbox that records `exec` calls, assert `sandbox.exec` was not called, `StaticReport.tools_run` contains both tool names, `StaticReport.findings` is empty, all three counters are 0; (e) baseline-side skip — call `_run_baseline_tool_on_host` with non-`.py`-only `changed_files`, monkeypatch `subprocess.run` to raise `AssertionError` if called, assert `[]` returned and no `AssertionError`; (f) filter-first ordering on baseline — pass a mix of `.py` and `.lock` paths where the `.lock` path would trigger an `is_file()` I/O error if it reached the existence check, assert no error surfaces.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.2, 3.3, 3.4, 4.2, 4.3, 4.4, 5.1_

- [ ] 5. Property tests
  - [ ]* 5.1 Add hypothesis-based property tests in `tests/unit/verify/test_static_checks_property.py` (new file). Use `hypothesis.strategies.text()` composed into path-shaped strings and `hypothesis.strategies.frozensets(st.sampled_from([".py", ".pyi", ".lock", ".toml", ".md", ".json", ""]))` for accepted-suffix sets. Cover **Property 1: Filter Helper contract** — for any sequence of paths and any accepted-suffix frozenset, `_filter_by_suffix` output is a subsequence of the input and every element has a suffix in the accepted set; empty accepted set yields `()`. Cover **Property 2: No non-accepted-suffix path reaches ruff or mypy argv** — for every `StaticTool` in `DEFAULT_STATIC_TOOLS` and any sequence of paths, no argv token substituted for the `{files}` sentinel in `_expand_argv(t.argv_template, _filter_by_suffix(paths, t.accepted_suffixes))` has a suffix outside `t.accepted_suffixes` (this covers both raise sites since both call `_filter_by_suffix` with the same key). Configure both tests with `@settings(max_examples=100)` at minimum. Tag each property test docstring with `Feature: static-checks-python-filter, Property N: <property text>`.
    - **Property 1: Filter Helper contract — subsequence with in-accepted suffixes**
    - **Property 2: No non-accepted-suffix path reaches ruff or mypy argv**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.5, 4.1, 5.1**

- [x] 6. Update module docstring
  - [x] 6.1 In `trikon/verify/static_checks.py`, extend the module docstring to document the suffix filter. Insert a "Suffix filter" section between the "Signature" block and the "Algorithm (5 steps, design.md §7)" block, describing: (a) the filter fires at both raise sites before argv is built, (b) `StaticTool.accepted_suffixes` is the discriminator and `_filter_by_suffix` is the helper, (c) ruff and mypy declare `frozenset({".py", ".pyi"})`, (d) an empty filtered list skips the tool cleanly with `tools_run` recorded and zero findings appended. Also update the Step 3 bullet in the algorithm enumeration to name the filter step explicitly ("filter `impact.changed_files` through `_filter_by_suffix` against `tool.accepted_suffixes`, skip cleanly if empty, otherwise expand the `{files}` sentinel …").
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 7. Checkpoint — verify all tests pass and mypy --strict is clean
  - Run `uv run pytest tests/unit/verify/` and `uv run mypy --strict trikon/verify/static_checks.py trikon/verify/models.py`. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP, but the property test on task 5.1 is the deliverable-named invariant ("no non-`.py` file ever reaches ruff/mypy argv") and is strongly recommended even for MVP.
- Each task references specific requirements for traceability.
- Task 1 (models.py) must complete before task 2 (which imports `tool.accepted_suffixes`).
- Tasks 2, 3, and 6 all edit `trikon/verify/static_checks.py`; they are placed in different waves to avoid write conflicts.
- Tasks 4 and 5 are test-only and can run in parallel once the source-side tasks (1-3, 6) are done.
- No git operations are performed in this plan — the parent Trikon repo's commit/push cadence is the user's responsibility, as required by the bugfix constraints.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["6.1"] },
    { "id": 4, "tasks": ["4.1", "5.1"] }
  ]
}
```
