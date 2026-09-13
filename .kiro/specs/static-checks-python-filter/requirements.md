# Requirements Document

## Introduction

Trikon v0.3.1 runs `ruff` and `mypy` against every entry in `impact.changed_files`. That list is language-agnostic — it includes `uv.lock`, `pyproject.toml`, `README.md`, images, and any other file the change touched — and it is passed verbatim to both the head-side sandbox invocation (via `_expand_argv`) and the host-side baseline invocation (via `_run_baseline_tool_on_host`). The result is thousands of false-positive findings whenever a change touches a non-Python file: ruff fails to parse `uv.lock` as Python and emits an E902 (or a syntax error) per line. This blocks clean commits with `decision: block`.

The fix declares, per `StaticTool`, the file suffixes that tool can parse (`{".py", ".pyi"}` for both ruff and mypy), and filters `impact.changed_files` down to that set at both raise sites before argv is built. When the filtered list is empty, the tool is skipped cleanly — no findings, no error, `tools_run` still records that the tool ran with zero findings.

This is a bugfix. The public shape of `run_static_checks`, `StaticReport`, `StaticTool`, and `DEFAULT_STATIC_TOOLS` remains backward-compatible (`accepted_suffixes` defaults to the Python-parseable set so existing construction sites keep working).

## Glossary

- **Static_Checks_Module**: The `trikon.verify.static_checks` module — the head-side entry point `run_static_checks` and its private helpers `_expand_argv`, `_run_baseline_tool_on_host`, `_resolve_base_keys`.
- **StaticTool_Descriptor**: The frozen, slotted dataclass `trikon.verify.models.StaticTool` — the value object carrying `name`, `argv_template`, `version_command`, `parse_json`, and (after this bugfix) `accepted_suffixes`.
- **Filter_Helper**: The new module-level function `_filter_by_suffix(paths, accepted_suffixes) -> tuple[str, ...]` that both raise sites call to reduce `impact.changed_files` to the tool-parseable subset.
- **Argv_Expander**: The private helper `_expand_argv(argv_template, changed_files) -> tuple[str, ...]` that substitutes the `{files}` sentinel.
- **Baseline_Runner**: The private helper `_run_baseline_tool_on_host(*, tool, worktree_dir, changed_files) -> list[dict[str, str | int]]` that invokes the pinned dev-dependency tool on the host against the base worktree.
- **Accepted_Suffixes**: The `frozenset[str]` field on `StaticTool_Descriptor` naming the file suffixes the tool can parse. For ruff and mypy the default is `frozenset({".py", ".pyi"})`.
- **StaticReport**: The public Pydantic model `trikon.evidence.report.StaticReport` carrying `tools_run`, `new_errors`, `new_warnings`, `preexisting_errors`, `findings`.
- **Head-side raise site**: The call site inside `run_static_checks` Step 3 where `_expand_argv` is invoked and `sandbox.exec(argv)` runs.
- **Baseline-side raise site**: The call site inside `_run_baseline_tool_on_host` where `_expand_argv` is invoked and `subprocess.run(argv, ...)` runs against the base worktree.

## Requirements

### Requirement 1: StaticTool declares which suffixes it can parse

**User Story:** As a Trikon maintainer, I want each `StaticTool` to declare the file suffixes it can parse, so that the runner can filter out non-Python files before invocation.

#### Acceptance Criteria

1. THE StaticTool_Descriptor SHALL expose an `accepted_suffixes` field typed as `frozenset[str]`.
2. WHERE a StaticTool_Descriptor is constructed without an explicit `accepted_suffixes` argument, THE StaticTool_Descriptor SHALL default that field to `frozenset({".py", ".pyi"})`.
3. THE default ruff StaticTool_Descriptor in `DEFAULT_STATIC_TOOLS` SHALL declare `accepted_suffixes = frozenset({".py", ".pyi"})`.
4. THE default mypy StaticTool_Descriptor in `DEFAULT_STATIC_TOOLS` SHALL declare `accepted_suffixes = frozenset({".py", ".pyi"})`.
5. THE StaticTool_Descriptor SHALL remain a frozen, slotted dataclass with the same equality and hash semantics as before this change.

### Requirement 2: A single suffix filter helper gates both raise sites

**User Story:** As a Trikon maintainer, I want a single filter function that both raise sites call, so that the head-side and baseline paths share identical filter semantics and cannot drift.

#### Acceptance Criteria

1. THE Static_Checks_Module SHALL expose a helper `_filter_by_suffix(paths, accepted_suffixes)` that accepts a `Sequence[str]` and a `frozenset[str]` and returns a `tuple[str, ...]`.
2. WHEN Filter_Helper receives a path whose suffix is not in `accepted_suffixes`, THE Filter_Helper SHALL exclude that path from its output.
3. WHEN Filter_Helper receives a path whose `pathlib.PurePosixPath(path).suffix` is the empty string, THE Filter_Helper SHALL exclude that path from its output.
4. THE Filter_Helper SHALL preserve the relative order of the paths that pass the filter (the output is a subsequence of the input).
5. IF `accepted_suffixes` is an empty `frozenset`, THEN THE Filter_Helper SHALL return an empty tuple regardless of the input paths.
6. THE Filter_Helper SHALL be pure — same inputs SHALL produce the same output, with no I/O and no mutation of its arguments.

### Requirement 3: Head-side sandbox invocation applies the filter

**User Story:** As a Trikon user, I want the head-side sandbox invocation to skip non-Python files, so that ruff and mypy never receive `uv.lock`, `pyproject.toml`, `README.md`, or similar paths as argv tokens.

#### Acceptance Criteria

1. WHEN `run_static_checks` prepares the head-side argv for a StaticTool_Descriptor, THE Static_Checks_Module SHALL apply Filter_Helper to `impact.changed_files` using `tool.accepted_suffixes` before calling Argv_Expander.
2. IF the suffix-filtered head-side file list is empty, THEN THE Static_Checks_Module SHALL skip the call to `sandbox.exec` for the current tool.
3. WHEN a StaticTool is skipped because its suffix-filtered head-side file list is empty, THE Static_Checks_Module SHALL still append the tool's name to `StaticReport.tools_run`.
4. WHEN a StaticTool is skipped, THE Static_Checks_Module SHALL NOT append any findings for that tool to `StaticReport.findings`, and SHALL NOT increment `new_errors`, `new_warnings`, or `preexisting_errors` on account of that tool.
5. WHEN Argv_Expander substitutes the `{files}` sentinel, THE Argv_Expander SHALL emit zero argv tokens whose suffix is not in the accepted set (guaranteed by the call site, not by Argv_Expander itself).

### Requirement 4: Host-side baseline invocation applies the filter

**User Story:** As a Trikon user, I want the host-side baseline subprocess to skip non-Python files, so that the baseline finding set is computed only against files ruff and mypy can parse.

#### Acceptance Criteria

1. WHEN Baseline_Runner prepares the subprocess argv, THE Baseline_Runner SHALL apply Filter_Helper to `changed_files` using `tool.accepted_suffixes` before the worktree-existence intersection currently performed by `(worktree_dir / f).is_file()`.
2. THE Baseline_Runner SHALL apply the suffix filter first and the existence intersection second, so a non-Python path is rejected on suffix without ever touching the base worktree filesystem.
3. IF the suffix-filtered and existence-intersected baseline file list is empty, THEN THE Baseline_Runner SHALL return an empty finding list without invoking the tool subprocess.
4. WHEN Baseline_Runner returns due to an empty filtered list, THE Baseline_Runner SHALL NOT raise, SHALL NOT log at WARNING or higher, and SHALL NOT record a subprocess timing measurement.

### Requirement 5: The filter is the sole gate — never fail open

**User Story:** As a Trikon maintainer, I want a filter miss to yield zero findings rather than silent pass-through of a non-Python file, so that a filter regression can never let ruff or mypy emit false-positive findings on a lockfile or a README.

#### Acceptance Criteria

1. WHEN Filter_Helper is invoked with a path whose suffix is not in `accepted_suffixes`, THE Filter_Helper SHALL exclude that path from its output regardless of any other property of the path (length, encoding, leading/trailing whitespace, mixed case).
2. WHERE both raise sites call Filter_Helper, THE Static_Checks_Module SHALL enforce the filter at the raise site — downstream code (Argv_Expander, `sandbox.exec`, `subprocess.run`) SHALL NOT be relied upon to reject a non-Python path.
3. WHERE a StaticTool is skipped due to a zero-length filtered file list, THE Static_Checks_Module SHALL record that fact deterministically — the same `(impact.changed_files, tool)` input SHALL yield the same `tools_run` entry, zero findings for that tool, and zero counter increments for that tool.

### Requirement 6: Module docstring documents the filter step

**User Story:** As a Trikon maintainer reading the `static_checks.py` module docstring, I want the algorithm block to describe the suffix filter step, so that the module's documented algorithm matches its implemented behavior.

#### Acceptance Criteria

1. THE `trikon.verify.static_checks` module docstring SHALL name the suffix filter as a documented step of the algorithm block that currently enumerates Steps 1-5.
2. THE module docstring SHALL name `StaticTool.accepted_suffixes` as the discriminator and `_filter_by_suffix` as the helper.
3. THE module docstring SHALL note that a tool whose suffix-filtered file list is empty is skipped, still records into `tools_run`, and produces zero findings.
