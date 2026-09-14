# Requirements Document

## Introduction

Trikon v0.3.4 runs `ruff` and `mypy` against every entry in `impact.changed_files` that survives the `.py`/`.pyi` suffix filter added by the `static-checks-python-filter` bugfix. That suffix filter is per-tool but blind to whether the file *exists at HEAD*. When a change **deletes** a Python file, or **replaces a Python file with a package** (`tests/test_types.py` → `tests/test_types/`, which git records as a delete-at-head plus one-or-more adds-at-head), the deleted path is still a `.py` and still ends up in the head-side argv. Inside the Docker sandbox — whose head bind-mount contains only files that exist at HEAD — ruff cannot find the path and emits `E902: No such file or directory (os error 2)`, one per missing file. Those E902s land in `StaticReport.findings` as `is_new=True` errors and drive the verdict to `decision=block, matched_rule="new static-analysis errors"` on what is actually a "there is nothing to inspect here" situation.

Verified in the field on the pallets/click test repro (base `87f7a31` → head `6aabf09`): `tests/test_types.py` was replaced with a `tests/test_types/` package. The Docker sandbox path (`create_sandbox(no_sandbox=False)`) emits E902 → `decision=block, new_errors=1`; the `--no-sandbox` host path (`create_sandbox(no_sandbox=True)`) silently skips it because host-side ruff handles the missing file differently. The two paths of the same product therefore disagree on the verdict for the same commit range — a latent correctness divergence.

The root cause, verified by reading the source: `ImpactSet.changed_files` is a flat `list[str]` and carries no `change_kind` metadata. `trikon.change_intel.diff_parser.parse_diff` internally computes `change_kind` on every `FileChange` (`added` / `modified` / `deleted` / `renamed`) but the value stops at the change-intel module boundary — `blast_radius.py` reduces `change_set.files` to `sorted(fc.path for fc in change_set.files)` and drops the kind. Downstream code in `run_static_checks` therefore cannot tell a "modified `.py`" apart from a "deleted `.py`" and passes both into argv.

The fix has three moving parts:

1. **Extend `ImpactSet`** with per-file `change_kind` metadata (a new `file_changes: list[FileChangeInfo]` list, additive and default-empty so every existing constructor keeps working).
2. **Plumb `change_kind`** from `diff_parser.py` through `blast_radius.py` into the new field on `ImpactSet`.
3. **Filter in `run_static_checks` Step 3** — a new module-level helper `_filter_by_head_existence(paths, file_changes) -> tuple[str, ...]` (mirror of `_filter_by_suffix` from v0.3.1) drops any path whose `change_kind == "deleted"` before argv is built. The filter fires at the head-side raise site only; the baseline-side raise site already handles the mirror case defensively via `(worktree_dir / f).is_file()` and needs no functional change.

This is a **bugfix**. The public shape of `run_static_checks`, `StaticReport`, `StaticTool`, and `DEFAULT_STATIC_TOOLS` is unchanged. `ImpactSet` gains one additive Pydantic field with a default — every existing positional and keyword constructor keeps working, every existing consumer that only reads `changed_files` keeps working, and every fixture file (`tests/fixtures/expected_impact/*.json`) round-trips through `ImpactSet.model_validate` because the new field defaults to `[]` when the JSON omits it.

## Glossary

- **Static_Checks_Module**: The `trikon.verify.static_checks` module — the head-side entry point `run_static_checks` and its private helpers `_filter_by_suffix`, `_expand_argv`, `_run_baseline_tool_on_host`, `_resolve_base_keys`.
- **Head_Existence_Filter**: The new module-level helper `_filter_by_head_existence(paths: Sequence[str], file_changes: Sequence[FileChangeInfo]) -> tuple[str, ...]` that both drops paths whose `change_kind == "deleted"` (and, defensively, any rename-source path present in `paths`) before argv is built.
- **Suffix_Filter**: The existing `_filter_by_suffix` helper introduced by the `static-checks-python-filter` bugfix (v0.3.1).
- **Argv_Expander**: The private helper `_expand_argv(argv_template, changed_files) -> tuple[str, ...]` that substitutes the `{files}` sentinel.
- **Baseline_Runner**: The private helper `_run_baseline_tool_on_host(*, tool, worktree_dir, changed_files)` that invokes the pinned dev-dependency tool on the host against the base worktree.
- **ImpactSet_Model**: The public Pydantic model `trikon.evidence.report.ImpactSet` carrying the outputs of the change-intel pipeline.
- **FileChangeInfo_Model**: The new public Pydantic model `trikon.evidence.report.FileChangeInfo` carrying per-file `path`, `change_kind`, and `old_path` metadata.
- **Public_Change_Kind**: The public boundary `Literal["added", "modified", "deleted", "renamed"]` type alias exported from `trikon.evidence.report`, mirroring the internal `ChangeKind` literal in `trikon.change_intel.models`.
- **Diff_Parser**: The `trikon.change_intel.diff_parser.parse_diff` entry point that produces `ChangeSet` from a `(base_sha, head_sha)` pair or a raw unified-diff string. Already computes `change_kind` per `FileChange` internally.
- **Blast_Radius_Orchestrator**: The `trikon.change_intel.blast_radius.compute_impact` function that constructs `ImpactSet` from `ChangeSet`. This is the plumbing point where `change_kind` must ride onto the public boundary.
- **Head-side raise site**: The call site inside `run_static_checks` Step 3 where `_expand_argv` is invoked and `sandbox.exec(argv)` runs.
- **Baseline-side raise site**: The call site inside `_run_baseline_tool_on_host` where `_expand_argv` is invoked and `subprocess.run(argv, ...)` runs against the base worktree.
- **Docker_Sandbox_Path**: The verification path taken when the caller passes `no_sandbox=False` (default) — `create_sandbox` returns a `LocalDockerSandbox` and the head repo is bind-mounted read-only at `/workspace/repo`; files not present at HEAD are absent from that mount.
- **No_Sandbox_Path**: The verification path taken when the caller passes `no_sandbox=True` — `create_sandbox` returns a `LocalSubprocessSandbox` and the head repo is used directly from the host filesystem.
- **Click_Repro**: The verified reproducer for this bug — the `pallets/click` repository at base `87f7a31` and head `6aabf09`, where `tests/test_types.py` was replaced with a `tests/test_types/` package. On the pre-fix codebase this produces `decision=block` on the Docker_Sandbox_Path and (silently) `decision=allow` on the No_Sandbox_Path.

## Requirements

### Requirement 1: `ImpactSet` carries per-file `change_kind` metadata

**User Story:** As a Trikon maintainer, I want `ImpactSet` to carry the `change_kind` of every changed file, so that downstream stages (static checks, policy engine, plugins) can distinguish a modified file from a deleted one without re-parsing the diff.

#### Acceptance Criteria

1. THE ImpactSet_Model SHALL expose a `file_changes` field typed as `list[FileChangeInfo]`.
2. WHERE an ImpactSet_Model is constructed without an explicit `file_changes` argument, THE ImpactSet_Model SHALL default that field to the empty list `[]`.
3. THE FileChangeInfo_Model SHALL expose a `path` field typed as `str` carrying the POSIX-relative path recorded on the internal `trikon.change_intel.models.FileChange.path`.
4. THE FileChangeInfo_Model SHALL expose a `change_kind` field typed as the Public_Change_Kind literal `Literal["added", "modified", "deleted", "renamed"]`.
5. THE FileChangeInfo_Model SHALL expose an `old_path` field typed as `str | None` that is populated with the pre-rename path when `change_kind == "renamed"` and left as `None` for every other value of `change_kind`.
6. THE Public_Change_Kind literal SHALL be exported from `trikon.evidence.report` so downstream consumers can import it without reaching into the internal `trikon.change_intel.models` module.
7. WHERE an existing consumer constructs `ImpactSet(changed_files=..., changed_symbols=..., impacted_modules=..., impacted_public_apis=..., impacted_tests=..., blast_radius_score=..., blast_radius_numeric=...)` positionally or by keyword without supplying `file_changes`, THE ImpactSet_Model SHALL accept that construction and materialize `file_changes == []`.
8. WHERE an existing `ImpactSet` JSON fixture on disk omits the `file_changes` key entirely, THE `ImpactSet.model_validate` call SHALL accept the payload and materialize `file_changes == []` (JSON-shape backward-compatibility).

### Requirement 2: `diff_parser.py` populates `change_kind` from the parsed diff

**User Story:** As a Trikon maintainer, I want the change kind that `diff_parser.py` already computes to ride onto the public `ImpactSet`, so that no consumer downstream of `parse_diff` has to re-derive it.

#### Acceptance Criteria

1. WHEN Blast_Radius_Orchestrator constructs an ImpactSet_Model from a `ChangeSet`, THE Blast_Radius_Orchestrator SHALL populate `ImpactSet.file_changes` with one FileChangeInfo_Model entry per `FileChange` in `change_set.files`.
2. WHEN Blast_Radius_Orchestrator translates a `trikon.change_intel.models.FileChange` into a FileChangeInfo_Model, THE Blast_Radius_Orchestrator SHALL copy `FileChange.path` verbatim into `FileChangeInfo.path`.
3. WHEN Blast_Radius_Orchestrator translates a `FileChange` into a FileChangeInfo_Model, THE Blast_Radius_Orchestrator SHALL copy `FileChange.change_kind` verbatim into `FileChangeInfo.change_kind`.
4. WHEN Blast_Radius_Orchestrator translates a `FileChange` with `change_kind == "renamed"`, THE Blast_Radius_Orchestrator SHALL copy `FileChange.old_path` verbatim into `FileChangeInfo.old_path`.
5. WHEN Blast_Radius_Orchestrator translates a `FileChange` with any `change_kind` other than `"renamed"`, THE Blast_Radius_Orchestrator SHALL set `FileChangeInfo.old_path` to `None`.
6. THE Blast_Radius_Orchestrator SHALL sort the resulting `file_changes` list by `path` (POSIX-lexicographic ascending) so identical inputs produce byte-identical JSON, matching the existing determinism contract for `changed_files`.
7. WHERE Blast_Radius_Orchestrator takes the empty-Python-change branch (currently `_empty_python_impact`), THE Blast_Radius_Orchestrator SHALL still populate `file_changes` from `change_set.files` — the empty-Python-change branch differs from the full-Python branch in symbol computation only, not in file-metadata plumbing.
8. THE EMPTY_IMPACT_SET sentinel exported from `trikon.evidence.report` SHALL carry `file_changes == []`.

### Requirement 3: A single head-existence filter helper gates the head-side raise site

**User Story:** As a Trikon maintainer, I want a single filter helper that drops "not at HEAD" paths, so that the head-side raise site cannot pass a deleted or rename-source path into ruff or mypy argv.

#### Acceptance Criteria

1. THE Static_Checks_Module SHALL expose a helper `_filter_by_head_existence(paths, file_changes)` that accepts a `Sequence[str]` and a `Sequence[FileChangeInfo]` and returns a `tuple[str, ...]`.
2. WHEN Head_Existence_Filter receives a path `p` and finds a FileChangeInfo_Model entry with `path == p` and `change_kind == "deleted"`, THE Head_Existence_Filter SHALL exclude `p` from its output.
3. WHEN Head_Existence_Filter receives a path `p` and finds a FileChangeInfo_Model entry with `change_kind == "renamed"` and `old_path == p`, THE Head_Existence_Filter SHALL exclude `p` from its output (defensive: rename-source paths do not exist at HEAD).
4. WHEN Head_Existence_Filter receives a path `p` and no matching FileChangeInfo_Model entry describes it as `deleted` or as a rename-source, THE Head_Existence_Filter SHALL include `p` in its output.
5. THE Head_Existence_Filter SHALL preserve the relative order of the paths that pass the filter (the output is a subsequence of `paths`).
6. WHERE `file_changes` is empty, THE Head_Existence_Filter SHALL return `tuple(paths)` unchanged — an empty `file_changes` is the backward-compat signal that per-file metadata was not supplied and the filter has no information to act on. In this branch the filter is a no-op and the downstream behavior matches the pre-fix code path exactly.
7. THE Head_Existence_Filter SHALL be pure — same inputs SHALL produce the same output, with no I/O and no mutation of its arguments.

### Requirement 4: Head-side sandbox invocation applies the head-existence filter

**User Story:** As a Trikon user, I want the head-side sandbox invocation to skip deleted-at-head files, so that ruff and mypy never receive a nonexistent path as an argv token and the Docker_Sandbox_Path never blocks a verdict on a spurious E902.

#### Acceptance Criteria

1. WHEN `run_static_checks` prepares the head-side argv for a StaticTool, THE Static_Checks_Module SHALL apply Head_Existence_Filter to the output of Suffix_Filter (not to `impact.changed_files` directly), using `impact.file_changes` as the second argument.
2. THE Static_Checks_Module SHALL apply Suffix_Filter first and Head_Existence_Filter second, so a non-`.py` file is rejected on suffix and a deleted `.py` is rejected on head-existence. Ordering is fixed for determinism.
3. IF the composed filter output (suffix-then-head-existence) is empty, THEN THE Static_Checks_Module SHALL skip the call to `sandbox.exec` for the current tool.
4. WHEN a StaticTool is skipped because the composed filter output is empty, THE Static_Checks_Module SHALL still append the tool's name to `StaticReport.tools_run` (as it does today via the unconditional `tools_run.append(tool.name)` at the top of the per-tool loop).
5. WHEN a StaticTool is skipped, THE Static_Checks_Module SHALL NOT append any findings for that tool to `StaticReport.findings`, and SHALL NOT increment `new_errors`, `new_warnings`, or `preexisting_errors` on account of that tool.
6. WHEN Argv_Expander substitutes the `{files}` sentinel at the head-side raise site, THE Argv_Expander SHALL emit zero argv tokens naming a path whose FileChangeInfo_Model entry has `change_kind == "deleted"` (guaranteed by the composed filter, not by Argv_Expander itself).

### Requirement 5: Baseline-side invocation retains the existing existence intersection

**User Story:** As a Trikon maintainer, I want the baseline-side invocation to continue relying on the `is_file()` intersection against the base worktree, so that the mirror case ("path does not exist at BASE") stays defensively covered and no new failure mode is introduced.

#### Acceptance Criteria

1. THE Baseline_Runner SHALL continue to apply the existing filesystem-existence intersection `(worktree_dir / f).is_file()` against the base worktree, exactly as it does today.
2. THE Baseline_Runner SHALL NOT invoke Head_Existence_Filter — the head-existence classification is about the HEAD tree and does not describe the BASE tree.
3. IF the baseline-side filesystem-existence intersection produces an empty list, THEN THE Baseline_Runner SHALL return an empty finding list without invoking the tool subprocess, matching its current behavior.
4. WHERE a path is `"added"` at HEAD (absent from BASE), THE Baseline_Runner SHALL correctly reject it via `is_file()` because the path does not exist in the base worktree; no new logic is required at the baseline side to make this work.

### Requirement 6: The filter is the sole gate — never fail open

**User Story:** As a Trikon maintainer, I want a filter miss to yield zero head-side findings rather than silent pass-through of a deleted-at-head path, so that a filter regression can never let ruff or mypy emit a false-positive E902 on a nonexistent file.

#### Acceptance Criteria

1. WHEN Head_Existence_Filter is invoked with a path `p` for which `impact.file_changes` contains an entry with `path == p, change_kind == "deleted"`, THE Head_Existence_Filter SHALL exclude `p` from its output regardless of any other property of the path (length, encoding, mixed case, leading/trailing whitespace).
2. WHERE the head-side raise site calls Head_Existence_Filter, THE Static_Checks_Module SHALL enforce the filter at the raise site — downstream code (Argv_Expander, `sandbox.exec`, ruff, mypy) SHALL NOT be relied upon to reject a deleted-at-head path.
3. WHERE a StaticTool is skipped due to an empty composed filter output, THE Static_Checks_Module SHALL record that fact deterministically — the same `(impact.changed_files, impact.file_changes, tool)` input SHALL yield the same `tools_run` entry, zero findings for that tool, and zero counter increments for that tool.

### Requirement 7: Docker sandbox and `--no-sandbox` paths produce identical verdicts

**User Story:** As a Trikon user, I want the Docker_Sandbox_Path and the No_Sandbox_Path to reach the same decision on any given diff, so that the choice of backend never changes the answer for the same commit range.

#### Acceptance Criteria

1. WHEN `sdk.verify(...)` is invoked twice on the same `(repo_path, base_sha, head_sha)` — once with `no_sandbox=False` and once with `no_sandbox=True` — against the Click_Repro (base `87f7a31` → head `6aabf09`) and every other input is held constant, THE `verdict.decision` field SHALL compare equal across the two invocations.
2. WHEN the two invocations are compared as in criterion 7.1 against the Click_Repro, THE `verdict.matched_rule` field SHALL compare equal across the two invocations.
3. WHEN the two invocations are compared as in criterion 7.1 against the Click_Repro, THE `verdict.evidence.verification.static.new_errors` counter SHALL compare equal across the two invocations, and SHALL NOT be inflated by a spurious `E902 No such file` on any path whose FileChangeInfo_Model entry has `change_kind == "deleted"`.
4. WHEN the two invocations are compared as in criterion 7.1 against the Click_Repro, THE `verdict.evidence.verification.static.findings` list SHALL contain no entry whose `rule_id == "E902"` and whose `path` matches a FileChangeInfo_Model entry with `change_kind == "deleted"`.
5. WHERE any other input diff produces a divergent verdict between the two paths, THE divergence SHALL be traceable to a difference outside the static-check module — the static-check contribution to `new_errors`, `new_warnings`, `preexisting_errors`, and `findings` SHALL be identical across the two paths for the same input.

### Requirement 8: Backward compatibility of existing `ImpactSet` construction sites

**User Story:** As a downstream consumer of the Trikon SDK, I want my existing `ImpactSet` construction and validation calls to keep working after this bugfix, so that a Trikon patch bump does not force me to touch call sites unrelated to the bug.

#### Acceptance Criteria

1. WHERE an existing construction site inside `trikon.change_intel.blast_radius` (the `_compute_python_impact` and `_empty_python_impact` returns) omits `file_changes`, THE construction site SHALL be updated to supply `file_changes` explicitly. No other in-tree construction site of `ImpactSet` exists apart from `EMPTY_IMPACT_SET` in `trikon.evidence.report`.
2. WHERE the `EMPTY_IMPACT_SET` sentinel in `trikon.evidence.report` is constructed, THE construction SHALL be updated to supply `file_changes=[]` explicitly.
3. WHERE an out-of-tree consumer constructs `ImpactSet` by keyword argument without supplying `file_changes`, THE ImpactSet_Model SHALL accept the construction and materialize `file_changes == []` via the field default.
4. WHERE an out-of-tree consumer deserializes an `ImpactSet` JSON payload that omits the `file_changes` key, THE `ImpactSet.model_validate` call SHALL accept the payload and materialize `file_changes == []`.
5. THE existing `tests/fixtures/expected_impact/*.json` fixtures SHALL be updated in the same commit as the code change so the `test_end_to_end_sample_repo.py` integration test compares against fixtures that carry the new `file_changes` field. If the fixture files are regenerated by an authoring-time script, THE regenerated JSON SHALL sort `file_changes` by `path` (matching Requirement 2.6) so the byte-identical-comparison contract in the integration test continues to hold.
