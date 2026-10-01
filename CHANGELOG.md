# Changelog

All notable changes to Trikon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] — 2026-10-01 — Engine Fail-Safe

Changes since 0.4.1. The engine no longer fails open: a Python change is never allowed on zero executed tests, and a change that leaves static imports of a removed module or name is blocked.

### Added
- Collection pass: before any test runs, the runner executes one `pytest --collect-only` over the whole suite in the same sandbox and records the item count in `TestReport.collected`. A collection error is attributable when the failing test file, or a file in its traceback, is touched by the change, or when the test file holds a broken import. Attributable errors are added to `failures` and fail the report; other collection errors only mark it incomplete. A missing or malformed collection report raises the new `CollectionPassError` (a `VerificationRunnerError`), which fails closed to `require_human`.
- Full-suite fallback: a Python change runs the whole suite (`pytest --continue-on-collection-errors`, no node ids) when the selection is empty, or when the selection did not come from a usable coverage map (present, not stale, and used with a caller-supplied base SHA). A change with no Python file and nothing selected starts no test run. The choice is recorded in `TestReport.strategy` (`selected`, `full_suite` or `none`) and `TestReport.strategy_reasons` (`empty_selection`, `coverage_map_missing`, `coverage_map_stale`, `no_base_sha`).
- Collection and test execution together stay within the time left in the verdict deadline. A timeout or a non-attributable collection error sets `TestReport.incomplete`, with `incomplete_reasons` drawn from `collection_timeout`, `execution_timeout` and `collection_error`. A collection timeout skips execution.
- Broken-import detection (`trikon.change_intel.import_check`, run by `check_imports` in `sdk.verify`). It compares the module layout and top-level names before and after the change, then scans every head-tree `.py` file, tests included, for static `import` / `from ... import` statements that refer to a removed module or a removed top-level name. Base content comes from `git show <base_sha>:<path>`, or from reversing the diff hunks when no base SHA is given. Relative imports are resolved and imports at every nesting level are checked. Imports inside a `try` that catches `ImportError`, `ModuleNotFoundError`, `Exception` or everything are skipped, and a module with a module-level `__getattr__` or a star import reports no removed names. Dynamic imports (`importlib.import_module`, `__import__`) are not detected.
- `ImportReport` (`broken`, `incomplete`, `unparsed_files`) and `BrokenImport` (`path`, `line`, `module`, `name`, and `kind`: `removed_module` or `removed_name`), carried on every Verdict as `evidence.verification.imports`, fail-closed Verdicts included. A file the checker cannot read or parse is listed in `unparsed_files`, and the report is marked incomplete when the change touches that file. Failing to run git or to walk the repository raises the new `ImportCheckError` (a `ChangeIntelError`), which fails closed.
- Safety_Floor (`trikon.policy.floor`), applied in `sdk.verify` after policy evaluation and before the audit write. It only changes an `allow`: to `block` with `matched_rule` `safety_floor.broken_imports` when any broken import exists, or to `require_human` with `safety_floor.insufficient_evidence` when a Python change executed zero tests (`passed + failed == 0`) or has incomplete test or import evidence. Broken imports win when both hold. A floored Verdict's `reason` names the floor condition and the original policy decision and rule, and `policy_results` gains a `RuleResult` for the floor rule id. No policy setting disables the floor, and the audit row records the floored decision.
- Policy DSL keys, still under policy `version: 1`: `verification.tests.executed` (`passed + failed`), `verification.tests.total` and `verification.imports.broken` (the number of broken imports), each taking `{eq|gt|lt: int}`; `verification.tests.strategy` (a string); and `verification.tests.incomplete`, `verification.imports.incomplete` and `change.python_change` (YAML `true` / `false` only).
- `any_of: [<when mapping>, ...]` in policy rules adds OR inside a rule. It takes a non-empty list of non-empty mappings, each matched with the usual AND semantics, and may nest. A wrong shape raises `RuleMatchError`.
- Two default-policy rules, in `trikon/policy/default_policy.yaml` (which `trikon init` scaffolds) and `examples/policies/default.yaml`: `broken static imports`, first, decides `block` when `verification.imports.broken` is greater than 0; `insufficient test evidence requires human`, just before `green, low-blast auto-allow`, decides `require_human` for a Python change with zero executed tests or incomplete test or import evidence. The five existing rules and the `default` fall-through to `require_human` are kept, for 8 rules in total.
- New model fields, all with defaults: `TestReport.collected`, `executed`, `strategy`, `strategy_reasons`, `incomplete`, `incomplete_reasons` and `collection_errors` (`CollectionError`: `path`, `message`, `attributable`); `VerificationReport.imports`; `SelectedTests.coverage_map_state` (`missing`, `stale` or `present`; `coverage_map_stale` keeps its meaning and equals `coverage_map_state != "present"`); and `Hunk.source_lines` / `Hunk.target_lines` (excluded from equality, hashing and repr). New helpers `is_python_path` and `is_python_change` in `trikon.evidence.report`.
- `trikon --version` prints the installed version and exits 0. The `trikon version` command is unchanged.
- The sandbox image ships the `trikon` CLI in its own venv at `/opt/trikon`, with dependencies constrained to the versions locked in `uv.lock` (exported at build time; a copy stays at `/opt/trikon/constraints.txt`). It is on PATH as `/usr/local/bin/trikon`, a small wrapper that sets `GIT_PYTHON_REFRESH=quiet` (unless already set) because the image has no git. The system `python`, `pip` and `pytest` that sandboxed tests use are unchanged.
- The `actions/verify` GitHub Action, added to `main` after the 0.4.1 tag, is in a tagged release for the first time.

### Changed
- `Verdict.schema_version` is now `3`. Stored v2 documents, including `audit_log.verdict_json` rows written by 0.4.1, still parse, keep `schema_version: 2` and take defaults for every new field. No field was renamed or retyped.
- `sensitive path requires human` moved from position 1 to 4 in the default policy, after the three block rules. A sensitive change that fails tests, adds static errors or breaks imports now gets `block` instead of `require_human`. A sensitive change that passes every check still gets `require_human`.
- An execution timeout now gives status `skipped` plus `incomplete` (`execution_timeout`) unless a test had already failed, so the default policy returns `require_human` instead of `block`. It used to give `failed`. On the Docker backend the timeout also kills the container, so the static stage after it still fails closed to `require_human`, as before.
- Previous-release engines fail closed on 0.5.0 policies: the new keys are unknown to them and raise `RuleMatchError`, so every verdict becomes `require_human`. This includes a `.trikon/policy.yaml` scaffolded by the 0.5.0 `trikon init`. Upgrade the engine before adopting the new policy.
- `sdk.verify` passes the parsed base and head SHAs and the `ImportReport` to `run_verification`, which gains an `imports` keyword. A base SHA counts as supplied only when the caller gives one; the runner's `HEAD~1` fallback never makes a coverage-map selection usable.
- The default sandbox image is `suryansh639/trikon:0.5.0`, defined once as `trikon.verify.sandbox.DEFAULT_SANDBOX_IMAGE`.
- The `actions/verify` GitHub Action's `no-sandbox` input now defaults to `"true"`. On GitHub-hosted runners sandbox mode bind-mounts `/github/workspace`, a path that does not exist on the Docker host, so with the old `"false"` default every run failed closed to `require_human`. Tests and static checks now run as subprocesses in the ephemeral action container, which already runs the consumer's `pip install -e .` as root. Self-hosted runners or container jobs where that path resolves on the Docker host can opt back in with `no-sandbox: "false"`.
- CI: the `integration` job and the nightly `perf-nightly` and `integration-verify` jobs build the sandbox image locally from `Dockerfile.sandbox`, tagged with the `pyproject.toml` version, so they never depend on the tag being on Docker Hub. The nightly jobs no longer call the nonexistent `scripts/build_sandbox_image.sh`. The PR `perf` benchmark on the `bad_retry` sample times `compute_impact` alone instead of the whole `sdk.verify` pipeline.

### Fixed
- The Docker sandbox ran pytest without a writable `TMPDIR`. The root filesystem and the repo mount are read-only, so pytest crashed at startup for every non-empty test selection and the verdict fell back to `require_human`. Every Docker exec now gets `TMPDIR=/workspace/tmp`; a caller-supplied env still wins.
- `parse_diff` classified a `--unified=0` modification whose only hunk removes lines from line 1 as `deleted`, and an insertion at the top of a file as `added`. Added and deleted now come only from a `/dev/null` side or git's `new file mode` / `deleted file mode` headers.
- A change that deletes a module that tests still import is now blocked. It used to be allowed, as in the sample repo's `deleted_file` scenario.
- A Python change that ran zero tests no longer counts as passed. An empty selection used to produce an all-passed `TestReport(total=0)`, which the default policy allowed.
- Every `static_baseline` cache miss leaked a git worktree. `git worktree remove` ran in the process's working directory instead of the repository, exited 128 and left a `trikon-worktree-*` directory in the temp dir plus a stale `.git/worktrees` entry. It now runs in the repository, like `git worktree add`. A failed removal is still only logged and never changes the verdict.
- The GitHub Action image (`actions/verify/Dockerfile`) installs git, uses the `trikon` CLI bundled in the sandbox image instead of installing trikon a second time with pip, and configures git `safe.directory`.
- The `trikon_cloud/fargate_runner` image installs git, which the sandbox base image does not include.
- The nightly `perf-nightly` CI job no longer uses a benchmark selector that matched no tests.

## [0.3.0] — 2026-09-13 — Phase 3: Policy Engine + Local-Dev Ergonomics

### Added
- `trikon doctor` command that reports Python / git / Docker / packaged-policy readiness with actionable hints.
- `--no-sandbox` opt-in flag on `trikon verify` and `trikon debug verify` for local-dev use when Docker is unavailable. Emits a prominent security warning banner to stderr while active.
- `LocalSubprocessSandbox` — host-process sandbox backend used when `--no-sandbox` is set. Prepends the running Python's bin directory to subprocess PATH so venv-installed tools resolve without external venv activation.
- `trikon verify` CLI command — runs the full change-intel + verification + policy pipeline against a repo/base/head triple and prints the resulting `Verdict`. Exits with decision-based codes: `0` on `allow`, `1` on `block`, `2` on `require_human`. Supports `--output markdown|json` (default `markdown`), `--policy <path>` to point at a non-default policy file, and `--diff-file <path>` to feed a pre-computed unified diff instead of running `git diff`.
- `trikon init` CLI command — scaffolds `.trikon/policy.yaml` in the target repo by copying the packaged `trikon/policy/default_policy.yaml` verbatim. Refuses to overwrite an existing file unless `--force` is passed.
- `sdk.verify(...)` now returns a real, fully-evaluated `Verdict` with populated `decision`, `matched_rule`, `reason`, and `warnings` — replacing the Phase-2 hardcoded `decision="require_human"` placeholder.
- New `audit_log` SQLite table in `.trikon/state.db` — append-only, one row per emitted `Verdict`, with columns `audit_id`, `created_at`, `decision`, `matched_rule`, `reason`, and `verdict_json` (lossless serialization of the full Verdict at `schema_version=2`).
- `PolicyEvaluationError` hierarchy under `trikon.policy.errors` — four classes (`PolicyEvaluationError` base plus `PolicyLoadError`, `RuleMatchError`, `AuditLogError`), all rooted at `trikon.exceptions.TrikonError` and re-exported from `trikon.policy`.
- Packaged `trikon/policy/default_policy.yaml` — 6-rule conservative default (sensitive-path require-human, impacted-tests-failed block, new-errors block, green low-blast auto-allow, high-blast warn, fall-through require-human) shipped inside the wheel via `[tool.hatch.build.targets.wheel.force-include]`.
- `format_markdown` — 6-block GitHub-flavored PR-comment layout (Header, Focus your review on, Impact, Verification, Warnings, Footer), pure-function no-I/O, safely renders `EMPTY_IMPACT_SET` / `EMPTY_VERIFICATION` fail-closed verdicts without crashing.

### Changed
- `sdk.verify` now preserves partial pipeline evidence in the fail-closed verdict. When Phase 1 change-intel succeeded and Phase 2 verification later failed, `evidence.change` reflects the real `ImpactSet` rather than the empty sentinel. `decision` remains `require_human` — the safety invariant is unchanged, only the evidence tells the truth.
- `trikon debug impact` no longer requires Docker. It now calls `parse_diff` + `compute_impact` directly, so the Phase 1 diagnostic works on any host.
- `SandboxUnavailableError` message is now actionable: it names both remediation paths (start Docker, or pass `--no-sandbox`).
- `debug verify` output no longer claims "torn down cleanly" when the sandbox never started; the line reads "Sandbox: not started" in the fail-closed shape.
- `Decision` literal widened from three values to four — adds `"warn"` alongside `"allow"`, `"block"`, `"require_human"`. `Verdict.schema_version` default bumped `1 → 2` to signal the shape change to downstream consumers.
- `Verdict.warnings: list[str]` field added — accumulates reasons from every warn rule that matched, in declaration order; never `None`, defaults to `[]`.
- `sdk.verify(...)` fail-closed `except` clause widened to catch the full `TrikonError` hierarchy — unifies Phase 1 (`ChangeIntelError`), Phase 2 (`VerificationRunnerError`), and Phase 3 (`PolicyEvaluationError` except `AuditLogError`) subsystem errors into a single `require_human` fallback with `EMPTY_IMPACT_SET` + `EMPTY_VERIFICATION` evidence.

### Notes
- **Audit-log write is a hard failure.** `AuditLogError` from `record_verdict` does *not* fail-close — it propagates out of `sdk.verify` so operators see the persistence failure rather than a silent verdict emission with no audit trail.
- **Warn rules accumulate reasons; they are never terminal.** A warn rule that fires appends its `reason` to `Verdict.warnings` and evaluation continues; the terminal `decision` is set by the first subsequent rule with a terminal `then` (`allow`, `block`, `require_human`), or falls through to `require_human` when no terminal rule matches.
- **The `audit_log` table is append-only.** `trikon/audit_log/**` emits only `INSERT` statements after the initial `CREATE TABLE IF NOT EXISTS` — no `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `TRUNCATE` appears anywhere in the subpackage, enforced by an AST-scan test.

See [docs/policy.md](docs/policy.md) for the end-user policy DSL guide and worked examples.

## [0.2.0] — Phase 2: Verification Runner

### Added
- `trikon debug verify` CLI command — runs the full change-intel + verification pipeline and prints a human-readable verdict summary. Supports `--json` for machine output.
- `trikon coverage build` CLI command — builds the coverage map by running the full pytest suite in a sandbox, persisted to `.trikon/state.db`.
- `sdk.verify(...)` now returns populated `evidence.verification` (real `VerificationReport`), replacing the Phase-1 `EMPTY_VERIFICATION` stub.
- Plugin API: repo-defined `.trikon/checks/*.py` scripts with `check(context: CheckContext) -> list[Finding]` signature.
- Pinned Docker sandbox image `trikon/sandbox:0.1.0` with Python 3.11 + pytest/ruff/mypy/coverage.
- Three new SQLite tables in `.trikon/state.db`: `coverage_map`, `tests_seen`, `static_baseline`.
- `VerificationRunnerError` hierarchy (8 classes) rooted at the new `TrikonError` base class in `trikon.exceptions`.
- `Dockerfile.sandbox` at repo root + `scripts/build_sandbox_image.sh`.

### Changed
- `trikon.change_intel.errors.ChangeIntelError` re-parented under `trikon.exceptions.TrikonError`.
- `sdk.verify()` fail-closed `except` clause widened to catch both `ChangeIntelError` and `VerificationRunnerError`.

### Notes
- The policy engine (Phase 3) has not shipped yet — Phase 2 verdicts always return `decision="require_human"` with real evidence attached.
- Network allowlist enforcement is stubbed in Phase 2 (falls back to `network_mode="none"` with a warning); full iptables egress control lands in Phase 3.
- Baseline static-check tool execution runs on the host, not in the sandbox, due to the single-mount limit of the Phase-2 `LocalDockerSandbox`. Multi-mount support lands in Phase 3.

See [docs/verification.md](docs/verification.md) for details.
