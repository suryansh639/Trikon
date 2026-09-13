# Requirements Document

## Introduction

Policy Engine is Phase 3 of Trikon. It consumes the `ImpactSet` produced by Change Intelligence (Phase 1) and the `VerificationReport` produced by Verification Runner (Phase 2), evaluates the repo's `.trikon/policy.yaml` against them, and produces the terminal `decision` (`allow` | `block` | `require_human`) plus a `warnings` list attached to the Verdict.

Phase 3 is what turns Phases 1 and 2 into an actionable answer. Phase 1 answered "what changed." Phase 2 answered "did it break." Without Phase 3, every verdict is hardcoded to `require_human` and Trikon is a diagnostic, not a product — CI has nothing to gate on. With Phase 3, `trikon verify` emits a real decision, a rule name, a reason, and a decision-based process exit code that a `if:` block in a GitHub Actions job can read directly.

In non-technical terms: this is the part of Trikon that reads your policy and decides whether an AI-authored change is safe to merge automatically, needs a human reviewer, or must be blocked outright. The requirements below fix the observable behavior of that pipeline. Design decisions (rule-dispatch layout, audit connection lifecycle, formatter internals) live in `design.md`.

## Glossary

- **Policy_Engine**: The `trikon/policy/` subsystem, addressed at its public entry points `trikon.policy.evaluator.evaluate_policy(policy, change, verification) -> Verdict` and `trikon.policy.loader.load_policy(repo_path, policy_path) -> Policy`.
- **Policy**: The Pydantic model in `trikon/policy/dsl.py` frozen in Phase 0; carries `rules: list[Rule]` and version metadata.
- **Rule**: Sub-model of Policy — `name: str`, `when: dict[str, Any]`, `then: Decision`, `reason: str | None`.
- **Decision**: The four-value string literal `"allow" | "block" | "require_human" | "warn"`. `allow`, `block`, and `require_human` are terminal; `warn` is non-terminal and only contributes to `Verdict.warnings`.
- **RuleResult**: The Pydantic model in `trikon/evidence/report.py` — one entry per rule evaluated, carrying `matched: bool`, `would_emit: Literal["allow", "block", "require_human", "warn"] | None`, and `reason: str | None`. `would_emit` is widened in Phase 3 from the Phase-2 three-value type; schema_version bumps `1` → `2`.
- **Verdict.warnings**: The new `list[str]` field on Verdict, added by Phase 3, populated with each matched `warn` rule's reason in rule-declaration order.
- **Audit_Log**: The new SQLite table `audit_log` in `.trikon/state.db`, colocated with the Phase-1/2 tables. Append-only by construction — the `trikon/audit_log/` module emits only `INSERT` statements after the initial `CREATE TABLE IF NOT EXISTS`.
- **PolicyEvaluationError**: The exception base class for every error raised inside `trikon/policy/**` and `trikon/audit_log/**`. Subclasses are `PolicyLoadError`, `RuleMatchError`, and `AuditLogError`; the base itself is raisable for evaluator-internal failures. All subclass `trikon.exceptions.TrikonError`.
- **default_policy**: The Pydantic Policy loaded from `trikon/policy/default_policy.yaml` shipped inside the wheel, resolved via `importlib.resources.files("trikon.policy") / "default_policy.yaml"`.
- **EMPTY_IMPACT_SET**: The sentinel `ImpactSet` from Phase 1 with `blast_radius_score = "HIGH"` and empty file/module/symbol lists, emitted at the SDK boundary whenever a `TrikonError` escapes any upstream phase.
- **EMPTY_VERIFICATION**: The sentinel `VerificationReport` from Phase 2 with `tests.status = "skipped"`, no static findings, no plugin results, emitted at the SDK boundary whenever a `TrikonError` escapes.

## Requirements

### Requirement 1: Evaluate all five condition types against real evidence

**User Story:** As a policy author, I want every condition type in the DSL to work against real evidence, so that my `.trikon/policy.yaml` rules actually match.

#### Acceptance Criteria

1. WHEN a rule's `when` clause contains `any_path_matches: [glob, ...]`, THE Policy_Engine SHALL match iff at least one glob matches at least one entry in `impact.changed_files` under `fnmatch.fnmatchcase` semantics.
2. WHEN a rule's `when` clause contains `no_path_matches: [glob, ...]`, THE Policy_Engine SHALL match iff no glob matches any entry in `impact.changed_files` under `fnmatch.fnmatchcase` semantics.
3. WHEN a rule's `when` clause contains `change.blast_radius.score: LOW | MEDIUM | HIGH`, THE Policy_Engine SHALL match iff `impact.blast_radius_score` equals the value literally.
4. WHEN a rule's `when` clause contains `verification.tests.status: passed | failed | skipped`, THE Policy_Engine SHALL match iff `verification.tests.status` equals the value literally.
5. WHEN a rule's `when` clause contains `verification.static.new_errors` with operator `{eq: N} | {gt: N} | {lt: N}`, THE Policy_Engine SHALL match iff the count of `verification.static.findings` with `is_new = true` satisfies the operator against `N`.
6. WHEN a rule's `when` clause contains multiple conditions, THE Policy_Engine SHALL AND them — every condition must match for the rule to match.
7. WHEN a rule's `when` clause is empty (`{}`), THE Policy_Engine SHALL treat the rule as unconditionally matching.

### Requirement 2: Load and validate policy files

**User Story:** As a customer, I want `.trikon/policy.yaml` loaded and validated at every verdict, so that malformed policies fail fast rather than silently producing wrong decisions.

#### Acceptance Criteria

1. WHEN `load_policy(repo_path, policy_path)` is called and the resolved file exists, THE Policy_Engine SHALL `yaml.safe_load` the file contents, pass the parsed mapping to `Policy.model_validate`, and return the validated `Policy` object.
2. WHEN the resolved policy file does not exist, THE Policy_Engine SHALL return the value of `default_policy()` — the same 6-rule policy shipped in `trikon/policy/default_policy.yaml`.
3. IF the resolved file exists but fails Pydantic validation (unknown field, wrong type, missing required `rules`), THEN THE Policy_Engine SHALL raise `PolicyLoadError` with the underlying `ValidationError` attached to `__cause__`.
4. IF the resolved file exists but is not valid YAML, THEN THE Policy_Engine SHALL raise `PolicyLoadError` with the underlying `yaml.YAMLError` attached to `__cause__`.
5. WHEN `default_policy()` is called, THE Policy_Engine SHALL return a `Policy` object equivalent to loading `trikon/policy/default_policy.yaml` from the installed package via `importlib.resources.files("trikon.policy") / "default_policy.yaml"`.

### Requirement 3: Emit a Verdict with real decision, matched_rule, reason, and warnings

**User Story:** As Trikon, I want `sdk.verify()` to return the real evaluated decision instead of a hardcoded `require_human`, so downstream consumers see the actual policy outcome.

#### Acceptance Criteria

1. WHEN `evaluate_policy(policy, change, verification)` is called and one or more rules with a terminal `then` (`allow`, `block`, `require_human`) match, THE Policy_Engine SHALL emit a Verdict whose `decision` equals the `then` value of the first matching terminal rule in declaration order, whose `matched_rule` equals that rule's `name`, and whose `reason` equals that rule's `reason` or a synthesized default when `reason` is `None`.
2. WHEN no rule with a terminal `then` matches, THE Policy_Engine SHALL emit a Verdict whose `decision` equals `"require_human"`, whose `matched_rule` equals `None`, and whose `reason` equals the default fall-through message.
3. WHEN one or more rules with `then == "warn"` match during evaluation, THE Policy_Engine SHALL append each such rule's `reason` (or a synthesized default when `reason` is `None`) to `Verdict.warnings` in rule-declaration order, and a `warn`-only match SHALL NOT be treated as a terminal decision.
4. WHEN `evaluate_policy` returns, THE returned `Evidence.policy_results` SHALL contain exactly one `RuleResult` per rule in the input `Policy.rules` in declaration order, with `matched`, `would_emit`, and `reason` populated for every rule regardless of whether that rule fired.

### Requirement 4: Persist an audit row per Verdict

**User Story:** As a compliance owner, I want every verdict — successful or fail-closed — recorded in an append-only audit log, so that after-the-fact review is always possible.

#### Acceptance Criteria

1. WHEN `sdk.verify()` produces a Verdict on either the success path or the fail-closed path, THE Trikon SDK SHALL insert exactly one row into `audit_log` with columns `audit_id UUID PRIMARY KEY, created_at TEXT NOT NULL, decision TEXT NOT NULL, matched_rule TEXT NULL, reason TEXT NOT NULL, verdict_json TEXT NOT NULL`.
2. WHEN a Verdict is written to `audit_log`, THE `verdict_json` column SHALL contain the output of `verdict.model_dump_json()` — the full Verdict serialization at schema_version 2.
3. THE `trikon/audit_log/` module SHALL emit only `INSERT` statements against `audit_log` after the initial `CREATE TABLE IF NOT EXISTS`; no `UPDATE`, no `DELETE`, and no `DROP` SHALL be issued by any function in the module.
4. WHEN `ensure_audit_tables(conn)` is called against a connection to `.trikon/state.db`, THE Policy_Engine SHALL create the `audit_log` table if absent and SHALL leave any existing rows and schema untouched, mirroring the contract of `ensure_verify_tables(conn)`.
5. IF the audit write itself fails (SQLite error, disk full, schema mismatch), THEN THE Policy_Engine SHALL raise `AuditLogError` at the SDK boundary — an audit failure is a hard failure, since a verdict without an audit trail SHALL NOT be silently returned.

### Requirement 5: Wire `sdk.verify` and the `trikon verify` CLI end-to-end

**User Story:** As a developer running Trikon in CI, I want `trikon verify --base HEAD~1 --head HEAD` to emit a decision-based exit code so CI can gate on it directly.

#### Acceptance Criteria

1. WHEN `sdk.verify(repo_path, base_sha, head_sha)` is called after Phase 3 lands, THE Trikon SDK SHALL invoke the pipeline `parse_diff → compute_impact → run_verification → load_policy → evaluate_policy → audit_log.record` in that order and SHALL return the Verdict emitted by `evaluate_policy` (or the fail-closed Verdict when any `TrikonError` escapes).
2. WHEN the CLI command `trikon verify --repo <path> --base <sha> --head <sha>` is invoked with no `--output` flag or `--output markdown`, THE Trikon CLI SHALL call `sdk.verify()`, render the returned Verdict as GitHub-flavored Markdown via `format_markdown`, print the result to stdout, and exit with process code `0` for `decision == "allow"`, `1` for `decision == "block"`, and `2` for `decision == "require_human"`.
3. WHEN `trikon verify` is invoked with `--output json`, THE Trikon CLI SHALL print `verdict.model_dump_json(indent=2)` to stdout in place of the Markdown, and SHALL preserve the decision-based exit codes defined in 5.2.
4. WHEN the CLI command `trikon init` is invoked, THE Trikon CLI SHALL copy `trikon/policy/default_policy.yaml` (resolved via `importlib.resources`) into `<repo>/.trikon/policy.yaml`, creating the parent directory if missing, and SHALL refuse to overwrite an existing file unless `--force` is supplied.
5. WHEN the CLI command `trikon debug verify` is invoked after Phase 3 lands, THE Trikon CLI SHALL continue to render the Phase-2 human-readable summary via the existing formatter and SHALL always exit `0`, remaining the developer diagnostic surface distinct from `trikon verify`.

### Requirement 6: Render the Verdict as GitHub-flavored Markdown

**User Story:** As a reviewer opening a GitHub PR, I want the Trikon verdict rendered as a scannable Markdown comment that names the specific tests that failed and the impacted modules.

#### Acceptance Criteria

1. WHEN `format_markdown(verdict)` is called, THE Markdown output SHALL contain a header line naming the decision with an icon (`✅` for `allow`, `🛑` for `block`, `🔍` for `require_human`), the `matched_rule` name, and the `reason`.
2. THE Markdown output SHALL contain a "Focus your review on" section listing up to 5 file paths drawn from `evidence.change.changed_files` in list order.
3. THE Markdown output SHALL contain an "Impact" section reporting the count of changed files, the count of impacted modules, the count of impacted public APIs, and the blast-radius bucket together with its numeric score.
4. THE Markdown output SHALL contain a "Verification" section reporting the pytest pass, fail, and skip counts, and up to 5 failing test node IDs (each rendered together with its `failure_summary`).
5. WHEN `verdict.warnings` is non-empty, THE Markdown output SHALL contain a "Warnings" section listing every warning string verbatim in list order.
6. THE Markdown output SHALL be returned as a single `str`; the formatter SHALL NOT invoke any template engine, SHALL NOT perform any I/O, and SHALL have no runtime dependency outside the Python standard library and the already-imported Pydantic models.

### Requirement 7: Never fail-open across the whole SDK boundary

**User Story:** As a platform team, I want any internal error inside the policy engine or audit log to still produce `require_human` — never `allow` — so unattended agents cannot be tricked into merging by a broken evaluator.

#### Acceptance Criteria

1. THE Policy_Engine SHALL raise only subclasses of `PolicyEvaluationError` at every raise site inside `trikon/policy/**` and `trikon/audit_log/**`; no bare `Exception`, `ValueError`, `KeyError`, `yaml.YAMLError`, `pydantic.ValidationError`, or `sqlite3.Error` SHALL escape either module boundary.
2. IF any `PolicyEvaluationError` (or any other `TrikonError` subclass from Phase 1 or Phase 2) propagates to `sdk.verify`, THEN THE Trikon SDK SHALL return a Verdict whose `decision` equals `"require_human"`, whose `evidence.change` equals `EMPTY_IMPACT_SET`, and whose `evidence.verification` equals `EMPTY_VERIFICATION`.
3. WHEN the SDK boundary catches any `TrikonError` subclass on the fail-closed path, THE Trikon SDK SHALL still write the resulting Verdict to `audit_log` before returning, so that no verdict — successful or fail-closed — is ever lost from the audit trail.
4. THE Trikon SDK SHALL never emit `decision == "allow"` on any error path; the `EMPTY_IMPACT_SET` sentinel's `blast_radius_score == "HIGH"` guarantees the default policy's fall-through resolves to `require_human` rather than `allow`.

### Requirement 8: Meet performance targets

**User Story:** As a developer running Trikon on every PR, I want policy evaluation to complete in well under a second so it never dominates the total verdict time.

#### Acceptance Criteria

1. WHEN `evaluate_policy(policy, change, verification)` is called against the 6-rule default policy with `change` and `verification` derived from `examples/sample_repo/` on a modern developer laptop (Ryzen-7-tier CPU, NVMe SSD), THE Policy_Engine SHALL complete in 100 ms of wall-clock time or less.
2. WHEN `load_policy(repo_path, policy_path)` is called against a real on-disk `.trikon/policy.yaml` up to 10 KB in size, THE Policy_Engine SHALL complete parse and validation in 50 ms of wall-clock time or less.
3. WHEN `format_markdown(verdict)` is called against a fully-populated Verdict (matched rule, non-empty warnings, non-empty impact set, non-empty verification report), THE Markdown formatter SHALL complete in 10 ms of wall-clock time or less.
