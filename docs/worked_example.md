# Worked Example — "Add Stripe retry-with-backoff"

A concrete walk-through of Trikon against a real-shaped scenario. This is the story we use for design partner demos.

## The setup

- Repo: `acme/payment-service` — a Python 3.11 SaaS backend, ~120K LOC.
- Agent: a nightly Claude Code bot that consumes failed-payment reports and opens PRs to fix them.
- Policy: the default from `examples/policies/default.yaml`.

## The PR

At 03:14 AM, the agent opens **PR #482**:

> **Add retry-with-backoff for failed Stripe charges**
>
> Changes 3 files:
> - `payments/retry.py` (+34 / -2)
> - `payments/gateway.py` (+9 / -1)
> - `tests/test_retry.py` (+8 / -0)

The AI-generated code sleeps 3 seconds between retries and calls `gateway.charge()` up to 5 times.

## Trikon fires

Triggered by the GitHub App webhook. Runs on the Trikon hosted runner.

### Step 1 — Change Intelligence

```
diff_parser         → ChangeSet with 3 FileChanges, 42 lines added.
ast_indexer         → Incremental re-index of the 3 files (rest of repo cached).
symbol_resolver     → Resolves callers:
                       payments.gateway.charge is called by:
                         - payments.worker.PaymentWorker.process
                         - api.payments.endpoint (via services layer)
                         - payments.batch.replay
dep_graph.query     → Transitive dependents (2 hops).
blast_radius        → ImpactSet:
                        changed_files      = [payments/retry.py, payments/gateway.py, tests/test_retry.py]
                        changed_symbols    = [payments.retry.with_backoff, payments.gateway.charge]
                        impacted_modules   = [payments, orders, api, workers]
                        impacted_public_apis = [payments.gateway.charge]
                        impacted_tests     = [
                            tests/test_retry.py,
                            tests/test_gateway.py,
                            tests/test_worker.py,
                            tests/api/test_payments.py,
                        ]
                        blast_radius_numeric = 11.5   (MEDIUM bucket)
```

### Step 2 — Verification Runner

Sandbox spins up from pinned image `python:3.11-slim-trikon`. Repo mounted read-only. Network disabled.

```
pytest (impacted 4 test files, 27 test cases)
    - 25 passed
    - 2 FAILED:
        tests/test_worker.py::test_worker_respects_2s_deadline
          AssertionError: worker exceeded 2000ms deadline (took 6013ms)
          -- the retry loop sleeps 3s three times before giving up.
        tests/api/test_payments.py::test_charge_endpoint_timeout
          TimeoutError: HTTP client timed out after 5s.
    duration: 8.4s

ruff  → clean.
mypy  → clean.
plugin: no_direct_sql → clean.
```

### Step 3 — Policy evaluation

Rules from the default policy, in order:

```
1. "sensitive path requires human"
     → any_path_matches includes payments/**.
     → MATCH. Terminal (require_human).
     → matched_rule captured.

2. "impacted tests failed"
     → tests.status = failed.
     → MATCH. Would emit block, but 'require_human' already decided.
     → recorded as a warning rule result.

3. "new static-analysis errors"      → no match.
4. "large blast radius requires human" → no match (MEDIUM, not HIGH).
5. "green, low-blast auto-allow"       → no match.
6. "default"                           → does not run.
```

### Step 4 — Verdict

```json
{
  "decision": "require_human",
  "reason": "Change touches a sensitive subsystem.",
  "matched_rule": "sensitive path requires human",
  "evidence": {
    "change": { "...": "..." },
    "verification": {
      "tests": {
        "status": "failed",
        "total": 27, "passed": 25, "failed": 2, "skipped": 0,
        "failures": [
          {
            "node_id": "tests/test_worker.py::test_worker_respects_2s_deadline",
            "outcome": "failed",
            "failure_summary": "worker exceeded 2000ms deadline (took 6013ms)"
          },
          {
            "node_id": "tests/api/test_payments.py::test_charge_endpoint_timeout",
            "outcome": "failed",
            "failure_summary": "HTTP client timed out after 5s"
          }
        ]
      },
      "static": { "new_errors": 0, "new_warnings": 0 }
    },
    "policy_results": [
      { "rule_name": "sensitive path requires human", "matched": true, "would_emit": "require_human" },
      { "rule_name": "impacted tests failed", "matched": true, "would_emit": "block" },
      { "rule_name": "new static-analysis errors", "matched": false, "would_emit": null },
      { "rule_name": "large blast radius requires human", "matched": false, "would_emit": null },
      { "rule_name": "green, low-blast auto-allow", "matched": false, "would_emit": null }
    ]
  },
  "audit_id": "01JAGXYZ...",
  "created_at": "2026-09-13T03:14:07Z",
  "schema_version": 1
}
```

### Step 5 — Consumer rendering

**GitHub PR comment (Markdown formatter):**

```
🔍 Trikon: REQUIRE_HUMAN — Change touches a sensitive subsystem.

Focus your review on:
- payments/retry.py — 2 impacted tests failed here.

Impact:
- 3 files changed
- 4 modules impacted (payments, orders, api, workers)
- 1 public API impacted: payments.gateway.charge
- Blast radius: MEDIUM (11.5)

Verification (took 8.4s):
- Tests: 25 passed, 2 FAILED
  ❌ tests/test_worker.py::test_worker_respects_2s_deadline
     worker exceeded 2000ms deadline (took 6013ms)
  ❌ tests/api/test_payments.py::test_charge_endpoint_timeout
     HTTP client timed out after 5s
- Static: clean

Suggested reviewer action:
The retry loop sleeps 3s x 5 attempts, but the worker deadline is 2s. Either
raise the worker deadline or use exponential backoff with a lower base.

Estimated review time: ~8 min.
```

**Autopilot decision (if this were called by Unideploy autopilot instead of GitHub):**

Autopilot sees `decision != "allow"`, does not deploy, and posts to the configured Slack channel with the same summary. Nothing hits production at 03:14 AM.

**Audit log entry:**

Hash-chained record with the full verdict + previous entry's hash. Compliance can export the chain for the quarter without touching the repo.

## Time saved on this specific PR

| Without Trikon | With Trikon |
| --- | --- |
| Human notices the PR at 09:00 AM. | Verdict emitted at 03:14:07 AM. |
| Reads 42 lines of diff. ~5 min. | Reads the summary. ~30 sec. |
| Manually traces `charge` callers. ~10 min. | Skips — impact set already computed. |
| Runs the test suite. ~4 min. | Skips — impacted tests already ran. |
| Notices worker timeout mismatch. Maybe. | Failure surfaced directly with the exact expectation. |
| **Total: ~20 min per PR × 8 nightly PRs = 2.7 hrs/day.** | **Total: ~2 min per PR × 8 = 16 min/day.** |

Order-of-magnitude time saved on this one workflow: **~2.4 engineering-hours per day** for a team running one nightly autofix bot.

## What this example is *not* demonstrating

- Not showing what happens for a genuinely safe change — those get `allow` and are auto-merged. The demo above is intentionally the interesting case.
- Not showing multi-agent scenarios where Trikon verdicts feed back to a supervising agent that retries with different constraints. That is a v1+ scenario worth designing but not building yet.
