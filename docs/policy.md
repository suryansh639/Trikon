# Policy Engine — Engineer's Walkthrough

Phase 3 of Trikon reads your `.trikon/policy.yaml`, grades the change against the evidence Phases 1 and 2 gathered, and emits the terminal `Verdict` — a real `decision`, a real `matched_rule`, a real `reason`, an accumulated `warnings` list, and a decision-based process exit code that a CI job can gate on directly.

For the frozen contract, see [`../.kiro/specs/policy-engine/design.md`](../.kiro/specs/policy-engine/design.md). Phase-1 context (how `ImpactSet` is built) lives in [`change_intel.md`](change_intel.md); Phase-2 context (how `VerificationReport` is built) lives in [`verification.md`](verification.md). The policy engine consumes both verbatim.

## What Phase 3 does

Phase 1 answered *what changed*. Phase 2 answered *whether the change broke anything*. Phase 3 answers *what should we do about it*.

```
ImpactSet + VerificationReport
        │
        ▼
load_policy(repo, policy_path)
        │  (missing → default_policy())
        ▼
       Policy
        │
        ▼
evaluate_policy(policy, change, verification)
        │  first-match-terminal + warn accumulation
        ▼
      Verdict  (decision, matched_rule, reason, warnings, schema_version=2)
        │
        ├──▶ audit_log.record_verdict(conn, verdict)   ── append-only INSERT
        │
        └──▶ format_markdown(verdict)                   ── GitHub-PR-comment string
                    │
                    ▼
              trikon verify  (exit 0 / 1 / 2 by decision)
```

Every stage has a single, well-typed entry point. Every stage can fail; every failure raises a subclass of `PolicyEvaluationError`. `PolicyEvaluationError` is a `TrikonError`, so the SDK boundary already catching `ChangeIntelError` and `VerificationRunnerError` collapses to a single `except TrikonError` clause. On any caught error, `sdk.verify` returns `require_human` with `EMPTY_IMPACT_SET` and `EMPTY_VERIFICATION` sentinels — never `allow`. See [`../.kiro/specs/policy-engine/design.md`](../.kiro/specs/policy-engine/design.md) §10 and §13.

## The policy file

Policies live in `<repo>/.trikon/policy.yaml`. The schema is fixed at `version: 1`:

```yaml
version: 1

# Optional. Overrides the default blast-radius weights (Phase 1).
weights:
  impacted_modules: 1.0
  impacted_public_apis: 3.0
  impacted_test_files: 0.5
  cross_package_hops: 2.0
  sensitive_path_touch: 5.0

# Optional. Feeds the blast-radius score; also usable inside rules via any_path_matches.
sensitive_paths:
  - "payments/**"
  - "auth/**"

# Required. Evaluated top-to-bottom. First rule with a terminal `then` wins.
rules:
  - name: "green, low-blast auto-allow"
    when:
      verification.tests.status: passed
      verification.static.new_errors:
        eq: 0
      change.blast_radius.score: LOW
    then: allow
    reason: "Tests passed, no new static errors, small blast radius."
```

Every rule has a `name` (unique), an optional `when` clause (empty `when` matches every input), a required `then` decision, and an optional `reason` string that flows through to `Verdict.reason` or `Verdict.warnings`.

## The five condition types

Every `when` key routes to one of five inline matchers in `trikon.policy.evaluator._rule_matches`. Multiple keys inside the same `when` are ANDed — every condition must match for the rule to fire (Requirement 1.6). An empty `when` (`{}`) matches unconditionally (Requirement 1.7). Any key outside the five below raises `RuleMatchError`, which fail-closes at the SDK boundary.

### `any_path_matches`

Rule matches when at least one glob matches at least one entry in `impact.changed_files`. Globs are evaluated via `pathlib.PurePosixPath.match`, so `**` is real recursion (not a stdlib `fnmatch` `*` alias).

```yaml
- name: "sensitive path requires human"
  when:
    any_path_matches:
      - "payments/**"
      - "auth/**"
      - "**/migrations/**"
  then: require_human
  reason: "Change touches a sensitive subsystem."
```

### `no_path_matches`

Dual of `any_path_matches`. Rule matches when no glob matches any entry in `impact.changed_files`. Useful to gate a rule on "everything outside a safe zone":

```yaml
- name: "outside safe zone requires human"
  when:
    no_path_matches:
      - "docs/**"
      - "**/*.md"
  then: require_human
  reason: "Change touches code outside the docs safe zone."
```

### `change.blast_radius.score`

Literal string equality against `impact.blast_radius_score` (`LOW` | `MEDIUM` | `HIGH`). Buckets are computed by [`compute_impact`](change_intel.md) from the `BlastWeights` and the impacted set. The `EMPTY_IMPACT_SET` sentinel used on the fail-closed path has `blast_radius_score == "HIGH"`, so a policy that keys any allow rule on `LOW` or `MEDIUM` cannot be tricked into allowing when Phase 1 raised.

```yaml
- name: "large blast radius requires human"
  when:
    change.blast_radius.score: HIGH
  then: require_human
  reason: "Blast radius is HIGH; a human should review scope."
```

### `verification.tests.status`

Literal string equality against `verification.tests.status` (`passed` | `failed` | `skipped`). The `EMPTY_VERIFICATION` sentinel has `tests.status == "skipped"`, so a fail-closed evidence set never trips a `passed`-gated allow rule.

```yaml
- name: "impacted tests failed"
  when:
    verification.tests.status: failed
  then: block
  reason: "One or more impacted tests failed."
```

### `verification.static.new_errors`

Counts entries in `verification.static.findings` with `is_new == true` (findings that did not exist against the cached `static_baseline` at `base_sha`; see [`verification.md`](verification.md)) and compares that count to `N` under exactly one of three operators: `eq`, `gt`, or `lt`. The operator dict must contain exactly one key; a multi-key mapping or an unknown operator raises `RuleMatchError`.

```yaml
- name: "new static-analysis errors"
  when:
    verification.static.new_errors:
      gt: 0
  then: block
  reason: "Change introduced new static-analysis errors."
```

## The four `then` values

Three terminal, one non-terminal:

| `then` | Terminal? | Effect |
| ------ | --------- | ------ |
| `allow` | yes | First `allow` match wins the whole `Verdict`. Only path to CI exit code `0`. |
| `block` | yes | First `block` match wins. CI exit code `1`. |
| `require_human` | yes | First `require_human` match wins. CI exit code `2`. Also the fall-through when no terminal rule matches. |
| `warn` | no | Rule's `reason` is appended to `Verdict.warnings`; evaluation continues to the next rule. |

**First-match-terminal semantics.** Rules are read top-to-bottom. The first rule that both matches its `when` AND emits a terminal `then` wins — subsequent matching rules never overwrite the winner, though they still leave a `RuleResult` trace in `evidence.policy_results` for audit. Order your rules from most-specific to most-general.

**Warn accumulation.** A matching `warn` rule appends `rule.reason` (or a synthesized `"Rule '<name>' warned."` when `reason` is `None`) to `Verdict.warnings` in rule-declaration order and falls through. A `warn`-only match leaves `decision` unset, so the fall-through resolves to `require_human` — a `warn` alone never becomes an allow.

**Fall-through.** When no terminal rule matches, `decision = "require_human"`, `matched_rule = None`, and `reason = "No rule matched; defaulting to require_human."`. The shipped default policy ends with an unconditional `require_human` rule so this fall-through only fires against custom policies missing a terminal.

**Every rule leaves a trace.** `verdict.evidence.policy_results` contains exactly one `RuleResult` per rule in the input `Policy.rules`, in declaration order — `matched`, `would_emit`, and `reason` populated for every rule regardless of whether it fired (Requirement 3.4).

## Missing-file fallback

When `<repo>/.trikon/policy.yaml` does not exist, `load_policy` returns `default_policy()` — the same 6-rule policy shipped inside the wheel at `trikon/policy/default_policy.yaml`, resolved via `importlib.resources.files("trikon.policy") / "default_policy.yaml"`. Requirement 2.2 makes this a fall-through, not an error — a repo that has not run `trikon init` still gets a conservative policy, not a raise.

The 6 rules, in declaration order:

1. **`sensitive path requires human`** — `any_path_matches: [auth/**, billing/**, payments/**, **/migrations/**]` → `require_human`.
2. **`impacted tests failed`** — `verification.tests.status: failed` → `block`.
3. **`new static-analysis errors`** — `verification.static.new_errors: {gt: 0}` → `block`.
4. **`large blast radius requires human`** — `change.blast_radius.score: HIGH` → `require_human`.
5. **`green, low-blast auto-allow`** — `passed` tests + zero new static errors + `LOW` blast → `allow`.
6. **`default`** — empty `when`, unconditional `require_human`.

Malformed YAML, an empty file, a `pydantic.ValidationError`, or an `OSError` on read all surface as `PolicyLoadError` with the original exception on `__cause__` — the SDK boundary translates that into `require_human` and writes an audit row before returning.

## Expected `Verdict` for sample scenarios

All three scenarios below run against [`../examples/sample_repo/`](../examples/sample_repo/) with its policy at [`../examples/sample_repo/.trikon/policy.yaml`](../examples/sample_repo/.trikon/policy.yaml). Patches live in [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/).

### `bad_retry` → `block`

Retiming `payments.retry.with_backoff` fans out to the whole payments/orders/api graph. Tests fail, blast is `HIGH`. Rule 1 (`sensitive path requires human`) is scoped to `payments/**` in this repo's policy, which does not match `src/payments/retry.py` under `PurePosixPath.match` right-anchor semantics; rule 2 (`impacted tests failed`) fires first:

```json
{
  "decision": "block",
  "matched_rule": "impacted tests failed",
  "reason": "One or more impacted tests failed.",
  "warnings": [],
  "schema_version": 2
}
```

Process exit code: `1`.

### `clean_refactor` → `allow`

Extracting a private helper in `orders.worker`. `LOW` blast, tests pass, no new static errors. Rules 1-4 miss; rule 5 (`green, low-blast auto-allow`) fires:

```json
{
  "decision": "allow",
  "matched_rule": "green, low-blast auto-allow",
  "reason": "Tests passed, no new static errors, small blast radius.",
  "warnings": [],
  "schema_version": 2
}
```

Process exit code: `0`.

### `sensitive` (patch under `payments/`) → `require_human`

Any change touching a file under the repo's `sensitive_paths` glob — e.g. adding a `currency` field to `payments.gateway.charge` — matches rule 1 before any verification-based rule can:

```json
{
  "decision": "require_human",
  "matched_rule": "sensitive path requires human",
  "reason": "Change touches a sensitive subsystem.",
  "warnings": [],
  "schema_version": 2
}
```

Process exit code: `2`. The rule fires regardless of `verification.tests.status` — a green sensitive-path change still requires a human, and that is by design.

## Audit log

Every verdict — happy-path or fail-closed — is written to the `audit_log` table in `<repo>/.trikon/state.db` before `sdk.verify` returns. The table sits alongside the Phase-1 change-intel tables and the Phase-2 verify tables in the same SQLite database.

Schema:

```sql
CREATE TABLE IF NOT EXISTS audit_log (
  audit_id      TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  decision      TEXT NOT NULL CHECK(decision IN ('allow','block','require_human','warn')),
  matched_rule  TEXT,
  reason        TEXT NOT NULL,
  verdict_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_decision   ON audit_log(decision);
```

`verdict_json` is `verdict.model_dump_json()` at `schema_version=2` — the lossless serialization. Given an `audit_id`, `SELECT verdict_json FROM audit_log WHERE audit_id = ?` reproduces the exact returned Verdict.

**Append-only by construction.** The `trikon.audit_log` module exports exactly two names — `ensure_audit_tables` and `record_verdict` — and only ever emits `INSERT` after the initial `CREATE TABLE IF NOT EXISTS`. No `UPDATE`, no `DELETE`, no `DROP`, no `ALTER`, no `TRUNCATE`. A static AST scan in `tests/unit/audit_log/test_append_only_closure.py` pins this closure.

**Hard failure on audit-write.** If the `INSERT` itself fails (disk full, WAL corruption, primary-key collision), `record_verdict` raises `AuditLogError` and the SDK re-raises past `sdk.verify` — a verdict without an audit trail is never silently returned (Requirement 4.5). Every other `PolicyEvaluationError` subclass fail-closes to `require_human` with an audit row written; only `AuditLogError` escalates.

Retention, hash chaining, structured export, and out-of-band audit sinks are Phase 4 material. See [`../.kiro/specs/policy-engine/design.md`](../.kiro/specs/policy-engine/design.md) §18.

## `trikon init`

Scaffolds a starter `.trikon/policy.yaml`:

```bash
trikon init                    # writes .trikon/policy.yaml under CWD
trikon init --repo path/to/repo
trikon init --force            # overwrite an existing policy
```

The command reads the packaged `default_policy.yaml` (via `importlib.resources`) and writes it byte-identical to `<repo>/.trikon/policy.yaml`, creating `.trikon/` if needed.

| Condition | Exit code |
| --------- | --------- |
| Success — new file, or `--force` overwrote | 0 |
| Existing file, no `--force` | 1 |
| Missing packaged resource (broken wheel) | 1 |
| Typer usage error | 2 |

A fresh `trikon init` followed by `trikon verify` runs the same policy the missing-file fallback would have — the two paths converge on the same 6 rules.

## `trikon verify`

The top-level command every CI job calls:

```bash
trikon verify --repo <path> --base <sha> --head <sha> \
              [--diff-file <path>] [--policy <path>] \
              [--output markdown|json]
```

Default output is the 7-block GitHub-flavored Markdown (`format_markdown`) — header + focus + impact + verification + optional warnings + footer. Passing `--output json` prints `verdict.model_dump_json(indent=2)` instead, useful for piping into `jq` or persisting a raw record.

Decision-based exit codes (unchanged between output formats — Requirement 5.3):

| Decision | Exit code | CI semantics |
| -------- | --------- | ------------ |
| `allow` | `0` | Merge or continue. |
| `block` | `1` | Explicit rejection — job fails. |
| `require_human` | `2` | Needs review — merge queue holds. |

A GitHub Actions gate reads directly from those codes:

```yaml
- name: Trikon verify
  run: trikon verify --repo . --base ${{ github.event.pull_request.base.sha }} \
                                --head ${{ github.event.pull_request.head.sha }}
```

The Phase-2 diagnostic surface `trikon debug verify` is preserved and always exits `0` — it stays the "print the Verdict for a human to eyeball" command, distinct from the CI gate.

## Where to go from here

- [`../.kiro/specs/policy-engine/design.md`](../.kiro/specs/policy-engine/design.md) — the frozen contract (data models, SQLite DDL, error hierarchy, correctness properties).
- [`change_intel.md`](change_intel.md) — how the `ImpactSet` the policy engine grades is produced.
- [`verification.md`](verification.md) — how the `VerificationReport` the policy engine grades is produced.
- [`policy_dsl.md`](policy_dsl.md) — the compact DSL reference for authoring `.trikon/policy.yaml`.
- [`../examples/sample_repo/.trikon/policy.yaml`](../examples/sample_repo/.trikon/policy.yaml) — the reference policy exercised by the integration tests.
- [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/) — the reference patches that drive the three scenarios above.
