# Requirements Document

## Introduction

Verification Runner is Phase 2 of Trikon. It consumes the `ImpactSet` produced by Change Intelligence and turns it into a `VerificationReport` — the actual pytest results, ruff/mypy diagnostics, and custom-plugin findings from executing the impacted checks inside an isolated sandbox.

Phase 2 is what makes `sdk.verify()` useful. Without it, the SDK has no verification evidence to attach to a Verdict and must return `require_human` on every change on principle. With it, Trikon crosses from "we know what changed" to "we know whether the change broke anything" — the hinge the reliability story is built on. The policy engine (Phase 3) grades the report; the runner is the source of the observations that engine trusts.

In non-technical terms: this is the part of Trikon that actually runs the tests and lints on an AI-generated change and reports whether they passed. The requirements below fix the observable behavior of that pipeline. Design decisions (Docker exec vs `docker-py`, coverage-map schema layout, plugin loader mechanics) live in `design.md`.

## Glossary

- **Verification_Runner**: The `trikon/verify/` subsystem, addressed at its public entry point `run_verification(repo_path: Path, impact: ImpactSet) -> VerificationReport`.
- **VerificationReport**: The Pydantic model returned by `run_verification`, already defined in `trikon/evidence/report.py`; carries `tests: TestReport`, `static: StaticReport`, `plugins: list[PluginResult]`, and sandbox metadata.
- **TestReport**: Existing sub-model of VerificationReport holding the pytest outcome — `status`, `results: list[TestResult]`, `coverage_map_stale`, and duration.
- **TestResult**: Existing sub-model — one entry per executed test node, carrying `node_id`, `outcome`, and `failure_summary`.
- **StaticReport**: Existing sub-model holding ruff and mypy findings; each finding carries `is_new` marking whether it is absent from the baseline run.
- **PluginResult**: Existing sub-model — one entry per `.trikon/checks/*.py` plugin, carrying the plugin's returned findings and an optional `error` field for load or execution failures.
- **LocalDockerSandbox**: The sole sandbox backend in Phase 2, backed by the local Docker daemon and the pinned `trikon/sandbox:0.1.0` image. Warden-backed remote execution is deferred to Phase 5.
- **CoverageMap**: The `(qualified_name, tests: list[str])` mapping persisted in the `coverage_map` table of `.trikon/state.db`, alongside sibling tables `tests_seen` and `static_baseline`.
- **VerificationRunnerError**: The exception base class for every error raised inside `trikon/verify/**`. Subclasses include `SandboxUnavailableError`, `PluginLoadError`, `CoverageBuildError`, and others introduced during design.
- **EMPTY_VERIFICATION**: The sentinel `VerificationReport` with `tests.status = "skipped"`, no static findings, and no plugin results, emitted at the SDK boundary whenever a `VerificationRunnerError` escapes.

## Requirements

### Requirement 1: Select impacted tests using a coverage map

**User Story:** As Trikon, I want the coverage map to tell me exactly which tests exercise the impacted symbols, so that only the relevant subset runs on every verdict.

#### Acceptance Criteria

1. WHEN `run_verification` is called with an ImpactSet whose `changed_symbols` map to entries in the CoverageMap, THE Verification_Runner SHALL include exactly those test node IDs in the selected test set passed to pytest.
2. WHEN the CoverageMap has no entry for one or more impacted symbols, THE Verification_Runner SHALL derive test node IDs for those specific symbols from the filename heuristic (`tests/test_{leaf}.py` and `tests/{pkg}/test_{leaf}.py`) and add them to the selected test set.
3. WHEN the CoverageMap's `built_at` is more than 7 days behind the current wall-clock or its `built_against_sha` differs from the ChangeSet's `base_sha`, THE Verification_Runner SHALL set `coverage_map_stale = true` on the returned TestReport and derive impacted test IDs from the filename heuristic for the remainder of the call.

### Requirement 2: Execute selected tests inside a Docker sandbox

**User Story:** As a platform team, I want tests to run in an isolated container with no network access, so that unattended agent verdicts cannot exfiltrate secrets or reach production systems.

#### Acceptance Criteria

1. WHEN Verification_Runner executes pytest for a verdict, THE Verification_Runner SHALL run the pytest process inside a Docker container built from the pinned image `trikon/sandbox:0.1.0`, with the repository mounted read-only, a `tmpfs` volume for outputs, and the container network mode set to `none`.
2. IF a single sandbox execution exceeds 5 minutes of wall-clock time (the Team-tier per-verdict ceiling from `PRICING.md` §3), THEN THE Verification_Runner SHALL terminate the container, populate the returned TestReport with `status = "failed"` and a single synthesized TestResult whose `outcome = "errored"` and `failure_summary = "sandbox exceeded 5-minute deadline"`, and return without raising.
3. WHEN a policy grants network access to a specific host or CIDR for the current verdict, THE Verification_Runner SHALL enable outbound egress for exactly that allowlist and SHALL block every other outbound destination at the container network layer.

### Requirement 3: Run static checks (ruff + mypy) and diff findings against a baseline

**User Story:** As Trikon, I want static-analysis findings on changed files reported as new versus pre-existing, so that a change is only blocked for problems it introduced.

#### Acceptance Criteria

1. WHEN Verification_Runner runs ruff and mypy inside the sandbox against the files changed at `head_sha`, THE Verification_Runner SHALL populate `StaticReport.findings` with `is_new = true` for exactly those findings absent from the baseline run captured at `base_sha`.
2. WHEN a `(base_sha, tool, tool_version)` triple already exists in the `static_baseline` table of `.trikon/state.db`, THE Verification_Runner SHALL reuse the cached baseline findings and SHALL NOT re-execute the tool against `base_sha`.
3. WHEN the pinned version of a static tool declared in `pyproject.toml` changes, THE Verification_Runner SHALL treat every `static_baseline` cache entry for that tool as invalidated on the next lookup and SHALL capture a fresh baseline run under the new tool version.

### Requirement 4: Run repo-defined check plugins

**User Story:** As a customer, I want to write custom checks in `.trikon/checks/*.py` and have Trikon execute them alongside built-in checks, so that repo-specific policies (no direct SQL, no secrets in strings) run on every verdict.

#### Acceptance Criteria

1. WHEN a repository contains one or more `.trikon/checks/*.py` modules each exporting `check(context: CheckContext) -> list[Finding]`, THE Verification_Runner SHALL import each plugin inside the sandbox, invoke its `check()` function once per verdict, and append every returned Finding to the corresponding PluginResult in the VerificationReport.
2. IF a plugin module fails to import or its `check()` function raises, THEN THE Verification_Runner SHALL record a PluginResult for that plugin with the failure captured in the `error` field and SHALL continue executing the remaining plugins in the current verdict.
3. WHEN a plugin's `check()` function is declared with `async def`, THE Verification_Runner SHALL raise a subclass of VerificationRunnerError at plugin load time and SHALL record the affected plugin as a failed PluginResult in the returned VerificationReport.

### Requirement 5: Build and maintain the coverage map

**User Story:** As a developer onboarding Trikon, I want a single command that builds the coverage map by running my full test suite once, so that subsequent verdicts can select tests precisely.

#### Acceptance Criteria

1. WHEN the CLI command `trikon coverage build --repo <path>` is invoked, THE Verification_Runner SHALL run the full pytest suite inside the sandbox with `coverage.py` instrumentation, persist `symbol → set(test_ids)` mappings into the `coverage_map` table of `.trikon/state.db`, and persist `test_id → last_seen` rows into the `tests_seen` table of the same database.
2. WHEN a coverage-map build completes successfully, THE Verification_Runner SHALL record its `built_at` timestamp and `built_against_sha` value in the `coverage_map` table so subsequent staleness checks have a reference point.
3. IF a coverage-map build fails during test collection or sandbox execution, THEN THE Verification_Runner SHALL leave the previously-persisted `coverage_map` and `tests_seen` rows untouched and SHALL surface the failure to the caller as a subclass of VerificationRunnerError.

### Requirement 6: Never fail-open

**User Story:** As a platform team, I want any internal error inside the verification runner to produce `require_human`, never `allow`, so unattended agents cannot be tricked into merging by a broken sandbox.

#### Acceptance Criteria

1. THE Verification_Runner SHALL raise only subclasses of VerificationRunnerError at every raise site inside `trikon/verify/**`; no bare `Exception`, `ValueError`, `subprocess.CalledProcessError`, or `docker.errors.*` SHALL escape the module boundary.
2. IF any VerificationRunnerError propagates to `sdk.verify`, THEN THE Trikon SDK SHALL return a Verdict whose `decision` equals `"require_human"` and whose `evidence.verification` equals the EMPTY_VERIFICATION sentinel with `tests.status = "skipped"`.
3. WHEN the Docker daemon is unreachable because its socket is missing, its process is down, or the current user lacks permission to connect, THE Verification_Runner SHALL raise `SandboxUnavailableError` (a subclass of VerificationRunnerError) whose diagnostic message includes the socket path that was attempted.

### Requirement 7: Wire `sdk.verify` and add the `trikon debug verify` CLI

**User Story:** As a developer using Trikon locally, I want a CLI command that runs the whole change-intel and verification pipeline and prints a human-readable summary of the outcome.

#### Acceptance Criteria

1. WHEN `sdk.verify(repo_path, base_sha, head_sha)` is called after Phase 2 lands, THE Trikon SDK SHALL invoke `compute_impact` followed by `run_verification`, populate `evidence.verification` with the returned VerificationReport, and return a Verdict whose `decision` equals `"require_human"` (policy engine ships in Phase 3).
2. WHEN the CLI command `trikon debug verify --repo <path> --base <sha> --head <sha>` is invoked, THE Verification_Runner CLI SHALL print a human-readable summary of the VerificationReport to stdout including pass/fail counts, the first 5 failing test node IDs by name, the new-versus-baseline static-finding delta, and the sandbox wall-clock duration; the process exit code SHALL be 0 on success and 1 on any uncaught error.
3. WHEN `trikon debug verify` is invoked with the `--json` flag, THE Verification_Runner CLI SHALL print the full Verdict as JSON (via `verdict.model_dump_json(indent=2)`) to stdout in place of the human-readable summary.

### Requirement 8: Meet performance targets

**User Story:** As a developer running Trikon in an editor MCP flow, I want verification on a typical 3-file change to complete in under 30 seconds cold and 5 seconds warm.

#### Acceptance Criteria

1. WHEN Verification_Runner executes a first-ever verdict against `examples/sample_repo/` under the `bad_retry` scenario on a modern developer laptop (Ryzen-7-tier CPU, NVMe SSD, Docker Desktop running), THE Verification_Runner SHALL complete in 60 seconds of wall-clock time or less.
2. WHEN Verification_Runner executes a second verdict against the same repository and the same `(base_sha, head_sha)` pair with the `static_baseline` cache warm and a CoverageMap hit, THE Verification_Runner SHALL complete in 15 seconds of wall-clock time or less.
3. WHEN the CLI command `trikon coverage build` is invoked against `examples/sample_repo/`, THE Verification_Runner SHALL complete the coverage-map build in 30 seconds of wall-clock time or less.
