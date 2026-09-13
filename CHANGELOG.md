# Changelog

All notable changes to Trikon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
