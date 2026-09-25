# Trikon Cloud — Cross-Cutting Invariants

> **Scope.** This is **not** the EARS-framed acceptance-criteria set that other Trikon specs carry. It is a set of cross-cutting invariants that every downstream Trikon Cloud spec (`trikon-cloud-webhook-receiver`, `trikon-cloud-fargate-runner`, `trikon-cloud-orchestrator`, `trikon-cloud-dashboard`) MUST honor. A downstream spec that violates any of these invariants is rejected in design review regardless of how well it satisfies its own local requirements.
>
> **Anchor.** `design.md` is the primary reference. Every invariant here has a corresponding section in the design memo that establishes the mechanism; this document names the invariant so downstream specs can cite it by number.

## Invariant 1: Tenant isolation

Two installations MUST NOT be able to read or write each other's DynamoDB rows, Secrets Manager secrets, S3 objects, or CloudWatch log streams. Enforcement is **at the IAM boundary, not at the application layer** — a compromised Fargate container running with installation A's task role MUST be unable to `dynamodb:GetItem` on a row where `installation_id = B`.

Mechanism (`design.md` §6, §5.3): every Fargate task assumes a per-installation `taskRoleArn`. The role's IAM policy uses `Condition: dynamodb:LeadingKeys ["${aws:PrincipalTag/installation_id}"]` on every DynamoDB action and `Condition: StringEquals { s3:prefix: "${aws:PrincipalTag/installation_id}/*" }` on every S3 action (for evidence-blob spill). Tenant isolation is a property of the IAM policy, not of application code — an application-layer bug that constructs the wrong partition key surfaces as an `AccessDenied` error, not as a cross-tenant read.

Downstream specs MUST NOT introduce any code path that reads or writes DynamoDB / S3 / Secrets Manager on behalf of an installation without going through the per-installation task role. In particular, the Dashboard API Lambda MUST resolve `installation_id` from the JWT authorizer claim and MUST NOT accept `installation_id` as a query parameter or request body field.

## Invariant 2: Never-fail-open

A broken run — a Fargate task that crashes, a `sdk.verify(...)` invocation that raises, a DynamoDB write that fails, a GitHub API call that returns 5xx — MUST surface as either (a) a `require_human` verdict posted to the PR, or (b) a persisted DLQ entry with enough context for a human to triage. It MUST NOT silently produce an `allow` verdict or a missing verdict.

This matches the Trikon SDK's Never-Fail-Open discipline (`static_checks.py` and `local_sandbox.py` module docstrings). The App is a wrapper around the SDK — it inherits the SDK's discipline and extends it end-to-end.

Concrete rules for downstream specs:

- The Fargate runner catches every exception thrown by `sdk.verify(...)`. If the verify succeeded but the DynamoDB write failed, the runner posts a `require_human` Check Run with reason `"internal error persisting verdict — see audit_id"`. If the verify itself raised, the runner posts a `require_human` Check Run with reason `"verify raised: <exception class>"` and includes the delivery_id for log correlation.
- The orchestrator Lambda's `on_failure` behavior (SQS message that hit max receive count) MUST be to write a synthetic `require_human` verdict row to `trikon_verdicts` **and** to a DLQ, not just to the DLQ. Rationale: the DLQ is invisible to the customer; the verdict row is visible in the M2 dashboard and (via Check Run) on the PR.
- The webhook receiver's `on_failure` (HMAC mismatch, malformed body) MUST return the appropriate 4xx to GitHub. It MUST NOT enqueue a message it couldn't validate.

## Invariant 3: Idempotency on the natural key

A repeat webhook delivery for the same `(installation_id, repo_full_name, pr_number, head_sha)` tuple MUST NOT produce:

- A second row in `trikon_verdicts`.
- A second Check Run on the PR (existing Check Run is edited via `PATCH /check-runs/{id}`).
- A second PR comment (existing comment is edited via `PATCH /issues/comments/{id}`).

The natural dedup key is `head_sha`. Any GitHub webhook re-delivery, any SQS message re-drive, any Fargate task retry against the same `head_sha` MUST converge on the same PR-side state.

Mechanism (`design.md` §5.5, §4.3):

- `trikon_verdicts` PutItem uses `ConditionExpression: attribute_not_exists(installation_id) AND attribute_not_exists(sk)`. If the write fires twice with the same `sk`, the second call raises `ConditionalCheckFailedException` and the runner logs + returns cleanly (no PR-side action, since the first run already posted).
- `trikon_pr_state` stores `last_head_sha`. If the runner sees `last_head_sha == TRIKON_HEAD_SHA` on the incoming task, it MAY short-circuit — either edit the existing artifacts with an "unchanged since last verify" note, or skip the GitHub calls entirely (the spec authors this decision).
- The `sk` for `trikon_verdicts` includes the `audit_id` (a UUID), so two verifies of the same `head_sha` from two separate SQS deliveries produce two distinct `audit_id`s but the ConditionExpression on the first PutItem still wins — the second is a no-op.

Downstream specs MUST cite this invariant when defining their retry semantics.

## Invariant 4: Public API shape (SDK boundary)

Trikon Cloud MUST NOT change the signature of `trikon.sdk.verify(...)` or the shape of `Verdict`. The Fargate runner is a **thin caller** — it receives job parameters, calls `sdk.verify(...)`, and consumes the returned `Verdict`. Any new field the runner needs on the way in becomes an environment variable in §5.4; any new field the runner needs on the way out becomes a value it computes locally, not a new field on `Verdict`.

Concrete rules:

- The runner MUST NOT monkeypatch, subclass, or shadow anything in `trikon.sdk`, `trikon.verify`, `trikon.evidence.report`, or `trikon.policy`.
- If a Trikon Cloud requirement demands a new field on `Verdict`, the change lands in a Trikon SDK release (bumping `Verdict.schema_version`), not in the runner. The `Verdict.schema_version` bump propagates to consumers via the normal SDK release cadence — see `trikon/evidence/report.py` for the current version discipline (v0.3.6 sits at `schema_version = 2`).
- The Dashboard API MUST render whatever `Verdict.schema_version` value it finds on a row. It MUST NOT reject rows whose `schema_version` is higher than the version it knows about (forward-compat behavior — new fields are optional).

## Invariant 5: Cost-per-verdict ceiling

The infrastructure MUST refuse to unbounded-scale on any single verdict. Concrete caps:

- **Fargate wall time.** Every task MUST have a task-level timeout of 10 minutes (`ecs.run_task` `overrides.taskRoleArn` combined with a stop-task signal from a sidecar or from the runner's own `signal.alarm(600)` at start). A task that runs past 10 minutes is killed and the failure is treated per Invariant 2 (`require_human` verdict with reason `"exceeded 10-minute cap"`).
- **DynamoDB PutItem.** Every verdict write MUST fit in a single `PutItem` call (no `BatchWriteItem`, no transactions). If a `PutItem` fails on size (`ValidationException: Item size has exceeded the maximum allowed size`), the runner spills `evidence_blob` to S3 (Invariant 6 for the S3 IAM rules) and retries with a smaller payload.
- **Evidence blob.** Gzipped `Verdict.evidence` MUST fit under DynamoDB's **400 KB item limit**. If it doesn't, the runner writes the gzipped blob to `s3://trikon-cloud-evidence/<installation_id>/<audit_id>.json.gz`, sets `evidence_blob = null` and `evidence_s3_key = "<installation_id>/<audit_id>.json.gz"` on the row, and never retries the oversized inline write. The 400 KB limit is DynamoDB's, not ours — we cannot raise it.
- **Fargate task memory.** Default 2 GiB. If a runner OOMs on a large repo, the fix is a task-definition variant (small / monorepo — `design.md` §3.1), not a per-verdict memory boost. A task that OOMs MUST surface as `require_human` per Invariant 2.
- **GitHub API rate limit.** Two calls per verdict (Check Run + PR comment). We MUST NOT add a third for informational purposes without a rate-limit budget analysis first.

## Invariant 6: Secrets handling

No plaintext secret — GitHub App private key, webhook secret, Stripe API key, installation token — appears in any:

- Log line (structured or unstructured).
- Environment variable that is echoed by a diagnostic endpoint.
- DynamoDB row.
- S3 object.
- CloudWatch metric dimension.
- HTTP request or response body (outside the one signed request that fetches the installation token from GitHub).

The GitHub App private key lives in Secrets Manager only. Installation tokens are fetched JIT and live only in the requesting process's memory. The pre-emit log filter (`design.md` §6) rewrites `-----BEGIN [A-Z ]+ PRIVATE KEY-----` blocks to `<private-key>` before the log line reaches CloudWatch.

Downstream specs MUST include a code-review checkbox that verifies no logging statement in the spec's new code interpolates a secret-bearing variable.

## Invariant 7: Product name

The product is **Trikon**. The commercial tier is **Trikon Cloud**. Neither is:

- AgentGuard.
- Trikon Cloud Enterprise (the enterprise tier, if we ship one, will be named separately when it exists).
- Any name with a hyphen, underscore, or camel-case variation (`trikon-cloud`, `TrikonCloud`, `trikon_cloud` are acceptable as identifiers only — never in user-facing copy).

The Docker image name (`suryansh639/trikon:0.3.6`), the PyPI package name (`trikon`), the GitHub App's marketplace listing, the Check Run's `name` field, the PR comment's first line, and the dashboard's page title all render `Trikon` or `Trikon Cloud` per §11 open question 2's resolution.

## Invariant 8: Type safety

All new Python code shipped as part of a Trikon Cloud spec MUST pass `uv run mypy --strict` on its own module. The public API surface of any new module MUST NOT use `dict[str, Any]`, `list[Any]`, or `object` — use `TypedDict`, `pydantic.BaseModel`, or a named `@dataclass(frozen=True)` per the SDK convention.

This matches the SDK's discipline — `trikon/verify/` and `trikon/policy/` already pass `--strict` and the App code has no reason to be laxer. In particular:

- SQS message bodies deserialize into a `TypedDict` or Pydantic model, not into a raw `dict`.
- GitHub webhook payloads deserialize into a Pydantic model that captures the subset in `design.md` §5.1. Unknown fields on the payload are allowed (`extra = "allow"`), but every field the receiver reads is typed.
- The Fargate runner's env-var contract (§5.4) is loaded into a `pydantic.BaseSettings` subclass; missing or malformed vars raise `pydantic.ValidationError` at process start, which triggers Invariant 2's `require_human` path (with reason `"malformed job context"`).

Downstream specs MUST cite this invariant when defining new module boundaries.
