# Design Document

## 1. Overview

Trikon Cloud is the hosted GitHub App tier of Trikon. This spec is Spec 1 of three M1 implementation specs (the others: `trikon-cloud-fargate-runner` = verify runtime, `trikon-cloud-orchestrator` = SQS → ECS glue) grounded on the architecture memo at `.kiro/specs/trikon-cloud-architecture/`. Its concrete deliverable is the **Webhook_Receiver** — an AWS Lambda function fronted by an API Gateway HTTP API that receives `POST /webhooks/github` from GitHub, verifies HMAC-SHA256, routes on event type, and enqueues a job on the `trikon-verify-jobs` SQS queue. The receiver has no DynamoDB access, no S3 access, and never imports the Trikon SDK — it is a pure, thin ingress with a single write side effect.

The design is deliberately compact. Six small Python modules under a new `trikon_cloud/webhook_receiver/` package, one CDK stack under `infra/`, and a unit-test suite backed by `moto` + `hypothesis`. Total code footprint is expected under 800 lines Python + under 300 lines CDK. The whole spec fits inside the M1 budget of a single Fargate-hour and a single API Gateway request tier — well inside the memo §7.2 target of $30-50/month at 100 PRs/day.

Three architectural pins from the memo drive every decision below. **First**, control-plane compute is Lambda, not a container service (memo §3.2) — GitHub's 10-second webhook timeout maps naturally onto Lambda's cold-start-plus-invocation budget with SnapStart. **Second**, the receiver's only cross-service write is `sqs.SendMessage` on a standard queue (memo §3.3) — no FIFO, no dedup ID, idempotency is entirely delegated downstream. **Third**, the ingress path never enters a VPC (memo §3.9) — the API Gateway HTTP API terminates on AWS's public backbone, IAM is the sole authorizer, and no NAT / PrivateLink cost accrues.

Three M1-blocking decisions inherited from the memo and confirmed in the dispatch prompt's Clarify phase are pinned here so downstream specs do not re-litigate them: (a) marketplace listing name is `Trikon` (not `Trikon Cloud`); the App slug is `trikon`; the Check Run `name` field rendered by Spec 2 is `Trikon`; (b) `check_run.rerequested` support is **deferred to M2** — this spec routes only `pull_request.opened` and `pull_request.synchronize`; every other event type receives a 200 (`ping` → pong body) or 204 without an SQS message; (c) the ECS cluster shape is a shared cluster (`trikon-verify-cluster`), owned by Spec 3, not created by this spec — the receiver writes only to SQS.

## 2. External Contracts

This spec is a net-new component; there is no bug to root-cause. In place of the root-cause section a bugfix would carry, this section names every external boundary the Webhook_Receiver touches. Each is a contract this spec must match — a change on either side is a coordinated versioning event (Requirement 16.3).

**Boundary A — GitHub HTTPS ingress (external).** GitHub POSTs the webhook to `/webhooks/github`. The request carries three headers this spec reads (`X-GitHub-Event`, `X-Hub-Signature-256`, `X-GitHub-Delivery`), and a JSON body whose subset per memo §5.1 the receiver extracts into `GithubWebhookPayload`. Contract owner: GitHub (external). Change cadence: rare; GitHub adds fields to the payload over time, but the subset in memo §5.1 has been stable for years. Failure mode: HMAC mismatch → 401; malformed body → 400.

**Boundary B — API Gateway HTTP API (internal, AWS).** GitHub's HTTPS terminates at API Gateway; API Gateway forwards to the Lambda via a proxy integration with a 10-second integration timeout. Contract owner: this spec (via the CDK stack in `infra/webhook_receiver_stack.py`). Change cadence: whenever the endpoint shape changes. Failure mode: API Gateway 5xx surfaces to GitHub if the Lambda times out or fails to respond.

**Boundary C — AWS Secrets Manager `GetSecretValue` (internal, AWS).** The Lambda reads `trikon-cloud/github-app-webhook-secret` once per cold start via `secretsmanager:GetSecretValue`. The secret is versioned; the receiver reads the current version (`AWSCURRENT` staging label). Contract owner: shared with Spec 2 (the runner also reads a different secret under the `trikon-cloud/` prefix). Change cadence: quarterly rotation. Failure mode: `AccessDenied` or `ResourceNotFound` → 500 (Requirement 7.2).

**Boundary D — AWS SQS `SendMessage` on `trikon-verify-jobs` (internal, AWS).** The Lambda writes exactly one JSON-body message per accepted request. The queue is a standard queue (memo §3.3); visibility timeout 15 minutes, max receive count 3, DLQ `trikon-verify-jobs-dlq`. Contract owner: this spec creates the queue via CDK; Spec 3 (the orchestrator) is the reader. The **SQS message body schema** (memo §5.2) is the internal contract with Spec 3 — a change to the body shape is a coordinated release. Change cadence: rare; the shape is pinned by the memo. Failure mode: `SendMessage` failure → 502 (Requirement 5.5).

**Boundary E — CloudWatch Logs `PutLogEvents` (internal, AWS).** The Lambda emits structured JSON log records to `/aws/lambda/trikon-cloud-webhook-receiver`. 30-day retention. Contract owner: this spec (via the CDK stack). Change cadence: whenever the logging schema (§3.5 below) changes. Failure mode: `PutLogEvents` failure surfaces via the Lambda runtime, not by application code.

**No other AWS boundaries.** The Lambda role does not grant `dynamodb:*` (Invariant 1 enforcement), does not grant `s3:*`, does not grant `kms:*` beyond what Secrets Manager's default `AWSCURRENT` decrypt requires, does not grant `ec2:*` or `vpc:*` (no VPC attachment, per Requirement 10.3). The IAM policy is minimal by design.

## 3. Chosen Tech Stack

### §3.1 Runtime — Python 3.11 on AWS Lambda

- **What.** Python 3.11 Lambda function (`x86_64` architecture, 512 MB memory, no VPC attachment, SnapStart enabled where available).
- **Why.** Python 3.11 matches Lambda's supported runtimes and the trikon SDK's `requires-python = ">=3.11"` — a single Python-version pin across SDK and Cloud codebases keeps CI clean and lets a solo maintainer test the packages against the same interpreter. 512 MB balances cold-start CPU allocation (Lambda's CPU scales linearly with memory) against baseline cost; empirically the receiver's cold-start-including-init lands under 1.5 s at this memory setting and warm-invocation under 100 ms.
- **Alternative rejected — Python 3.12.** Newer, but AWS Lambda's Python 3.12 support at the time of writing lags Python 3.11 in tooling maturity (some AWS Lambda Powertools features were 3.11-first). Post-M1 upgrade is trivial; not worth breaking symmetry with the SDK at M1.
- **Alternative rejected — Node.js.** Would give the fastest cold start on Lambda, but the team's Python-first discipline (SDK is Python, CDK stack is Python) makes cross-language ingress ergonomically expensive.
- **Cost implication.** Under $1/month at MVP volumes (memo §7.2).
- **MVP-vs-post-MVP nuance.** M1 uses the default `x86_64` architecture. Post-M1 we may switch to `arm64` (Graviton) for a ~20% cost reduction on the Fargate side; the Lambda side is cheap enough that architecture choice is aesthetic.

### §3.2 Framework — `aws-lambda-powertools[all]` with `APIGatewayHttpResolver`

- **What.** The HTTP router is `aws_lambda_powertools.event_handler.APIGatewayHttpResolver`. Route decorator `@app.post("/webhooks/github")` handles the single endpoint. The Logger (`aws_lambda_powertools.Logger`) is the structured-logging surface. Tracing is provided by powertools' default X-Ray integration.
- **Why.** Powertools is what AWS's Serverless team ships as the idiomatic pattern for Python Lambda; it delivers native structured logging with JSON output, request-scoped correlation IDs, native tracing, and a typed HTTP router without the bundle cost of a full web framework. Cold-start under 500 ms warm / ~1.5 s cold with SnapStart. Type-friendly (every powertools event class is a typed model).
- **Alternative rejected — FastAPI via Mangum.** FastAPI is the go-to Python web framework, and Mangum adapts it to Lambda. Rejected because Mangum adds ~10 MB to the deployment bundle for a single-endpoint receiver (Starlette's routing engine is overkill), and the FastAPI + Mangum + Pydantic combination has a measurable cold-start penalty vs powertools' lean event handler.
- **Alternative rejected — AWS Chalice.** Fine framework, but less type-friendly than powertools (Chalice's `Blueprint` and `Response` are dynamically typed) and its release cadence has been slower than powertools' since 2023.
- **Alternative rejected — Plain `def lambda_handler(event, context)`.** Zero framework, minimum bundle. Rejected because a plain handler forces per-endpoint boilerplate for headers, HTTP method routing, JSON body parsing, and error handling — every one of which powertools makes typed and cold-start-cheap.
- **Cost implication.** Zero (the framework is free; the bundle-size penalty is under 5 MB).
- **MVP-vs-post-MVP nuance.** M1 uses powertools' default Logger, Tracer, and Metrics disabled. Post-M1 we may enable powertools Metrics (custom CloudWatch metrics for accepted / rejected / 5xx counts) once the operational needs surface.

### §3.3 Validation — `pydantic>=2` (v2)

- **What.** Every value that crosses a module boundary is a `pydantic.BaseModel` subclass. `GithubWebhookPayload` for the memo §5.1 payload subset, `SqsJobMessage` for the memo §5.2 SQS body, `ReceiverEnvConfig` (a `pydantic_settings.BaseSettings` subclass) for the Lambda's environment-variable contract.
- **Why.** Matches Invariant 8 (no `dict[str, Any]` on public surfaces) directly. Pydantic v2's `model_dump_json()` is faster than `json.dumps(model.model_dump())` and produces byte-consistent output (relevant for Requirement 5.3). `extra="allow"` on `GithubWebhookPayload` lets GitHub add fields without breaking validation. The dependency is already in the Trikon SDK's `pyproject.toml` (`pydantic>=2.9`) — no new top-level dependency for the SDK.
- **Alternative rejected — TypedDict.** Would give static typing without a runtime validator. Rejected because HMAC-verified input from an external source (GitHub) needs runtime validation — a TypedDict trusts the input, Pydantic checks it.
- **Alternative rejected — `attrs` or `dataclasses`.** Both are lighter than Pydantic but neither ships a native JSON validator; adding one would duplicate Pydantic's core value proposition.
- **Cost implication.** ~3 MB in the deployment bundle for `pydantic-core`.
- **MVP-vs-post-MVP nuance.** None; Pydantic v2 is the sole validator throughout M1 and beyond.

### §3.4 Runtime dependencies

- `aws-lambda-powertools[all]>=3,<4` — HTTP resolver, structured logger, tracer.
- `pydantic>=2.9,<3` — validation models (matches the SDK's pin exactly).
- `pydantic-settings>=2,<3` — env-var config loader.
- `boto3>=1.35,<2` — AWS SDK (SQS + Secrets Manager clients).
- `botocore>=1.35,<2` — pulled transitively by `boto3`; pinned explicitly for the retry-config surface.

**Not included.** The Lambda does NOT import from `trikon` — the SDK is a heavy dependency (jedi, libcst, gitpython, docker) whose surface is entirely irrelevant to a webhook receiver. Decoupling the receiver from the SDK keeps the Lambda bundle under 20 MB (Requirement 15.4) and lets the two codebases release on independent cadences.

### §3.5 Testing — `pytest` + `moto` + `hypothesis` + `pytest-cov`

- **What.** Unit tests under `trikon_cloud/webhook_receiver/tests/`. `moto` provides in-process AWS service mocks (SQS + Secrets Manager). `hypothesis` drives the HMAC property tests (Property 1: verifier is total; Property 3: verifier is constant-time). `pytest-cov` enforces the 90% branch-coverage floor (Requirement 13).
- **Why.** These are the SDK's testing tools already (`hypothesis>=6.100,<7`, `pytest-cov>=6.0` in the SDK's `dev` extra). No new dev dependency. Moto's SQS mock is fast, in-process, and does not require a running LocalStack container.
- **Discipline.** Unit tests never call live AWS; integration tests (out of this spec's scope) live under `tests/integration/` and are flagged as a follow-up.
- **Cost implication.** Zero (all dev-only).
- **MVP-vs-post-MVP nuance.** M1 ships unit tests only. Integration tests against a live AWS account are a follow-up.

### §3.6 IaC — AWS CDK (Python)

- **What.** A single CDK stack class `WebhookReceiverStack` in `trikon_cloud/webhook_receiver/infra/webhook_receiver_stack.py`. The stack deploys API Gateway HTTP API + Lambda + SQS queue + DLQ + IAM role for the Lambda + CloudWatch log-group retention. The Secrets Manager entries (`trikon-cloud/github-app-webhook-secret` and the App private key from Spec 2) are **referenced by name / ARN**, not created — the secret's material is populated out-of-band.
- **Why.** CDK Python matches the runtime language, gives typed constructs, and is what a solo maintainer can maintain without a second language. The stack fits in under 300 lines.
- **Alternative rejected — AWS SAM.** SAM is YAML-only and has no static typing surface — a typo in a resource reference surfaces at deploy time, not at synth time. CDK's Python constructs surface every misconfiguration at synth (`cdk synth`).
- **Alternative rejected — Terraform.** Well-known, well-supported, and a valid choice for a larger org. Rejected because it introduces a second toolchain (Terraform CLI + provider plugins) and a state-file discipline (S3 backend + DynamoDB lock table) that a solo maintainer would inherit as operational burden.
- **Cost implication.** Zero (CDK is free; deployed resources are in scope of Requirement 10 and memo §7).
- **MVP-vs-post-MVP nuance.** M1's stack is a single monolith. If we later break Spec 2 (the runner) and Spec 3 (the orchestrator) into their own stacks, this stack's cross-stack references are done via `ssm.StringParameter` value exports keyed on stack-name — no `Export`/`ImportValue` global namespace pollution.

### §3.7 Type checking — `uv run mypy --strict`

- **What.** Every file in `trikon_cloud/webhook_receiver/` passes `uv run mypy --strict` on its own path.
- **Why.** Matches Invariant 8. Consistent with the SDK's `trikon/verify/` and `trikon/policy/` discipline. Catches shape drift between `GithubWebhookPayload` fields and their read sites at review time, not runtime.
- **Cost implication.** Zero.

### §3.8 Lint — `uv run ruff check`

- **What.** Every file passes `uv run ruff check`. `ruff` is already the SDK's linter (see the SDK's `dev` extra pin `ruff>=0.7`).
- **Why.** Consistent with SDK. Fast (rust-implemented). Catches common bugs (unused imports, unreachable code, common footguns) at review time.
- **Cost implication.** Zero.

## 4. Module Layout

New top-level package `trikon_cloud/` inside the Trikon monorepo. Sibling of the existing `trikon/` SDK package. Same repo, same git history, same `uv.lock` — a single `git clone` + `uv sync --extra cloud` bootstraps both surfaces. When the codebases stabilize post-M2, `trikon_cloud/` can be extracted to its own repo without changing the module boundaries.

```
Trikon/                                       (repo root)
├── pyproject.toml                             (extended with new `cloud` optional-dep group)
├── trikon/                                    (existing SDK package — untouched by this spec)
│   ├── sdk.py
│   ├── verify/
│   └── ...
└── trikon_cloud/                              (NEW package this spec creates)
    ├── __init__.py                            (empty; namespace package marker)
    └── webhook_receiver/                      (NEW subpackage — this spec's deliverable)
        ├── __init__.py                        (empty; declares __all__ = [])
        ├── README.md                          (Lambda + CDK deploy quickstart)
        ├── handler.py                         (Lambda entry point; powertools router)
        ├── models.py                          (Pydantic v2 models: payload, SQS body, env config)
        ├── hmac_verifier.py                   (constant-time HMAC-SHA256 verifier)
        ├── sqs_writer.py                      (boto3 SQS client + retries + typed error)
        ├── logger.py                          (powertools Logger + PII-redaction filter)
        ├── infra/
        │   ├── __init__.py
        │   ├── app.py                         (CDK app entry — `cdk deploy` calls into here)
        │   ├── webhook_receiver_stack.py      (WebhookReceiverStack construct)
        │   └── README.md                      (deploy runbook)
        └── tests/
            ├── __init__.py
            ├── conftest.py                    (shared pytest fixtures; moto session-scoped mocks)
            ├── test_handler.py                (routing + Property 2 + payload extraction end-to-end)
            ├── test_hmac_verifier.py          (Property 1 + Property 3 + edge cases)
            ├── test_sqs_writer.py             (moto-backed SQS send + retry + failure)
            ├── test_models.py                 (Pydantic round-trip + extra=allow behavior)
            └── fixtures/
                └── (empty at first; fixture data lives inline in conftest.py as constants,
                     NOT as top-level .json files — per the "no throwaway scratch files" rule)
```

**Module responsibilities.**

- `handler.py` — Lambda entry point. Instantiates a module-level `APIGatewayHttpResolver`. Registers one route: `@app.post("/webhooks/github")`. Exports `handler(event, context)` as the Lambda-runtime callable. Composes HMAC_Verifier → event routing → payload extraction → SQS_Writer. Owns the response-shape decisions (200 pong, 202 accepted, 204 non-enqueue, 400 malformed, 401 HMAC-mismatch, 5xx SQS-failure).
- `models.py` — Pydantic v2 models. `GithubWebhookPayload` (memo §5.1 subset with `extra="allow"`), `SqsJobMessage` (memo §5.2 shape, all-required), `ReceiverEnvConfig` (env-var loader via `pydantic_settings.BaseSettings`).
- `hmac_verifier.py` — Pure function `verify_signature(body: bytes, signature_header: str | None, secret: bytes) -> bool`. Uses `hmac.new(secret, body, hashlib.sha256).hexdigest()` and `hmac.compare_digest`. No side effects. No logging.
- `sqs_writer.py` — Class `SqsWriter` with method `send_job(message: SqsJobMessage) -> None`. Module-level `boto3.client("sqs")` (reused across warm invocations for latency). Raises `SqsWriteError` (a concrete `Exception` subclass in this module) on failure.
- `logger.py` — Instantiates the module-level `Logger(service="trikon-cloud-webhook-receiver")`. Registers a pre-emit filter that runs two regex substitutions: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` → `<email>` and `-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----` → `<private-key>`.
- `infra/webhook_receiver_stack.py` — CDK stack. Creates API Gateway HTTP API, Lambda function, SQS queue + DLQ, IAM role, CloudWatch log group. References the Secrets Manager secret by ARN passed via the constructor.
- `infra/app.py` — CDK app entry. Reads the AWS account + region from CDK environment; instantiates `WebhookReceiverStack` with the secret ARN sourced from an `ssm.StringParameter` lookup.

**Call graph.**

```
AWS Lambda runtime
  │
  ▼
handler.handler(event, context)
  │
  ├──► APIGatewayHttpResolver.resolve(event, context)
  │       │
  │       ▼
  │     handler.on_github_webhook(request)          (@app.post("/webhooks/github"))
  │       │
  │       ├──► logger.get_logger()                  (bind delivery_id, event_type)
  │       │
  │       ├──► _load_webhook_secret()                (module-level cache; secretsmanager.get_secret_value)
  │       │
  │       ├──► hmac_verifier.verify_signature(body, sig_header, secret)
  │       │       │
  │       │       └──► returns bool  (on False → 401 + WARNING log; RETURN)
  │       │
  │       ├──► _route_event(event_type, action)     (pure function on headers/action)
  │       │       │
  │       │       └──► returns Enqueue | Pong | NonEnqueue | BadEvent
  │       │
  │       ├──► models.GithubWebhookPayload.model_validate_json(body)   (on Enqueue branch)
  │       │       │
  │       │       └──► on ValidationError → 400 + WARNING log; RETURN
  │       │
  │       ├──► _build_sqs_message(payload, event_type, delivery_id)     (pure)
  │       │       │
  │       │       └──► models.SqsJobMessage
  │       │
  │       ├──► sqs_writer.SqsWriter().send_job(message)
  │       │       │
  │       │       └──► on SqsWriteError → 502 + ERROR log; RETURN
  │       │
  │       └──► return 202 {"status": "accepted", "delivery_id": ...}
```

## 5. Data Structures

### §5.1 `GithubWebhookPayload` (models.py)

Pydantic v2 model capturing the memo §5.1 subset. `extra="allow"` so unknown GitHub fields do not reject validation.

```python
from pydantic import BaseModel, ConfigDict, Field


class InstallationRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int


class RepositoryRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    full_name: str
    default_branch: str


class CommitRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")


class PullRequestRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    number: int = Field(ge=1)
    head: CommitRef
    base: CommitRef


class SenderRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    login: str


class GithubWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str
    installation: InstallationRef
    repository: RepositoryRef
    pull_request: PullRequestRef
    sender: SenderRef
```

### §5.2 `SqsJobMessage` (models.py)

Pydantic v2 model for the SQS body. All fields required. `model_config` empty (no `extra="allow"` — the message is our schema, not GitHub's).

```python
from pydantic import BaseModel, Field


class SqsJobMessage(BaseModel):
    installation_id: int = Field(ge=1)
    repo_full_name: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    event_type: str  # e.g., "pull_request.opened", "pull_request.synchronize"
    sent_at: str  # ISO 8601 UTC with millisecond precision, e.g., "2024-11-14T12:34:56.789Z"
    delivery_id: str  # verbatim X-GitHub-Delivery header
```

### §5.3 `ReceiverEnvConfig` (models.py)

Pydantic settings model for Lambda environment variables. Loaded at cold start; a missing / malformed variable raises `pydantic.ValidationError` at process start (Never_Fail_Open_Contract via Invariant 8's `require_human` propagation).

```python
from pydantic import Field
from pydantic_settings import BaseSettings


class ReceiverEnvConfig(BaseSettings):
    webhook_secret_arn: str = Field(alias="TRIKON_WEBHOOK_SECRET_ARN")
    verify_jobs_queue_url: str = Field(alias="TRIKON_VERIFY_JOBS_QUEUE_URL")
    log_level: str = Field(default="INFO", alias="TRIKON_LOG_LEVEL")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
```

The two `TRIKON_*` variables are provided by the CDK stack. `AWS_REGION` is provided by the Lambda runtime.

### §5.4 Response bodies

| Case                            | Status | Body                                                                                         |
|---------------------------------|-------:|----------------------------------------------------------------------------------------------|
| Successful enqueue              |    202 | `{"status": "accepted", "delivery_id": "<X-GitHub-Delivery value>"}`                          |
| GitHub `ping` event             |    200 | `{"status": "pong"}`                                                                          |
| Non-enqueue event               |    204 | (empty)                                                                                       |
| HMAC signature missing/mismatch |    401 | `{"error": "hmac_verification_failed"}`                                                       |
| Malformed JSON body             |    400 | `{"error": "malformed_json"}`                                                                 |
| Payload schema violation        |    400 | `{"error": "malformed_payload", "detail": "<pydantic error summary, PII-redacted>"}`          |
| Missing / empty X-GitHub-Event  |    400 | `{"error": "missing_event_header"}`                                                           |
| Secrets Manager read failure    |    500 | `{"error": "internal_error"}`                                                                 |
| SQS send failure                |    502 | `{"error": "enqueue_failed"}`                                                                 |
| Unhandled exception (fallback)  |    500 | `{"error": "internal_error"}`                                                                 |

## 6. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Three properties. Each is universally quantified and each corresponds to a specific downstream test task in `tasks.md`.

### Property 1: HMAC verification is total and correct

*For any* triple `(body, signature_header, secret)` where `body: bytes`, `signature_header: str | None`, and `secret: bytes` (all with `len(secret) > 0`), the function `hmac_verifier.verify_signature(body, signature_header, secret)` returns `True` if and only if `signature_header` is exactly `"sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()`, and `False` in every other case (including `signature_header is None`, empty string, missing `sha256=` prefix, wrong-length hex digest, and non-hex characters in the digest).

The property is total: for any legal input triple, the function returns a `bool` — it does not raise, does not block, does not consume network. Encoded as a hypothesis property test: hypothesis generates `body` from `st.binary(min_size=0, max_size=10_000)`, `secret` from `st.binary(min_size=1, max_size=256)`, and a boolean coin flip that selects "matching signature" (compute the correct signature) or "corrupted signature" (compute the correct signature then randomly mutate one hex char or the `sha256=` prefix). The test asserts the coin flip agrees with the function's return value.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

### Property 2: Every 202 response has a preceding successful SQS send

*For any* accepted webhook request (HMAC-valid, event-type routed to enqueue, payload schema-valid), the sequence of side effects observed by the outside world is exactly: (a) exactly one `sqs.SendMessage` call succeeds on `trikon-verify-jobs`, then (b) a 202 response is emitted. There is no code path from request-arrival to a 202 response that does not pass through a successful SQS send. Contrapositive: if the SQS send fails (any exception, any retry outcome), the response is a 5xx (specifically 502 per Requirement 5.5) and no 202 is emitted.

The property is verified by test observation: the test wraps the SQS client with a spy that records the outcome of every `send_message` call in a list, invokes the handler with a valid webhook, and asserts (a) `len(spy.calls) == 1`, (b) `spy.calls[0].outcome == "success"`, (c) the response status is 202, (d) `spy.calls[0]` happened before the response was emitted (verified via a captured wall-clock timestamp on each event). A separate test forces the spy to raise `botocore.exceptions.ClientError` on `send_message`, invokes the handler with the same valid webhook, and asserts (a) `spy.calls == [failure]`, (b) the response status is 502, (c) no 202 was emitted.

**Validates: Requirements 1.1, 5.1, 5.5, 7.1**

### Property 3: HMAC verification runs in constant time (compare_digest, not `==`)

*For any* pair of hex-digest strings of equal length, the `hmac_verifier` module's comparison of the parsed header digest against the computed digest MUST use `hmac.compare_digest`, not the `==` operator. This is a static property of the source code, not a timing measurement — a `==` comparison on a hex digest leaks byte-by-byte match information to an attacker measuring RTT, whereas `hmac.compare_digest` is designed to run in time independent of the comparison outcome.

Encoded as a code-review-style AST check. The test parses `hmac_verifier.py` via `ast.parse`, walks the tree, asserts (a) the module contains an `import hmac` or `from hmac import compare_digest`, (b) every `ast.Compare` node inside the `verify_signature` function whose left or right operand is a hex-string-shaped variable is a call to `hmac.compare_digest`, not an `Eq` operator, (c) no `Eq` operator appears between the parsed header digest and the computed digest (this can be verified structurally by asserting the sole cross-comparison in `verify_signature` is a `Call` node with `func` = `Attribute(hmac, "compare_digest")` or `Name("compare_digest")` when imported directly). If a future refactor accidentally re-introduces a `==` comparison, the test fails.

**Validates: Requirement 2.2, Invariant 6 (secrets handling — a timing side-channel would leak a signature bit)**

### Property reflection

The three properties are non-redundant: Property 1 governs the verifier's return value across every input; Property 2 governs the atomicity of the enqueue path; Property 3 governs a structural (source-level) invariant that Property 1 cannot detect at the function-return level (Property 1 samples inputs; a timing side-channel is not observable by sampling in a hypothesis harness). No property implies another. All three land as pytest tasks in `tasks.md`.

## 7. Never-Fail-Open Discipline

Invariant 2 (never-fail-open) is the load-bearing invariant of the whole receiver. Every code path from request-arrival to a response is annotated with its status-code outcome. There is exactly ONE path to 200 (`ping` event) and exactly ONE path to 202 (successful enqueue). Every other path returns a 4xx or 5xx.

**Path enumeration.**

1. Path arrives → HTTP method != POST → **405** (framework-level).
2. Path arrives → path != `/webhooks/github` → **404** (framework-level).
3. Method POST + path match → `X-GitHub-Event` header missing / empty → **400** (`missing_event_header`).
4. `X-GitHub-Event` = `ping` → **200** (`{"status": "pong"}`) — the only 2xx that does not enqueue.
5. `X-GitHub-Event` valued, but Secrets Manager read fails → **500** (`internal_error`) + ERROR log.
6. Secrets read ok → `X-Hub-Signature-256` header absent / malformed → **401** (`hmac_verification_failed`) + WARNING log.
7. `X-Hub-Signature-256` present but digest mismatches → **401** + WARNING log.
8. HMAC verifies → `X-GitHub-Event` = `pull_request` + action not in `{opened, synchronize}` → **204**.
9. HMAC verifies → `X-GitHub-Event` in `{check_run, push, installation, installation_repositories, issue_comment, check_suite, ...}` → **204**.
10. HMAC verifies → `X-GitHub-Event` = `pull_request` + action in `{opened, synchronize}` → payload parse fails (`ValidationError` or malformed JSON) → **400** + WARNING log.
11. HMAC verifies → payload parses → SQS send fails after retries → **502** (`enqueue_failed`) + ERROR log.
12. HMAC verifies → payload parses → SQS send succeeds → **202** (`{"status": "accepted", ...}`) + INFO log.
13. Any unhandled exception → framework fallback → **500** (`internal_error`) + ERROR log with traceback.

**Invariants over the path enumeration.**

- The only 202 is path 12. The only 200 is path 4 (which does not enqueue).
- SQS send happens exclusively on path 12. If path 12's SQS call fails, path 11 fires — no fallthrough to 202.
- No path returns the exception's traceback or `str(exception)` in the response body (Requirement 7.5).
- Every 4xx / 5xx path emits a structured log record; success paths emit an INFO record.

**Failure-mode audit.** A code review that adds a new event type (Requirement 3.4 mentions this is inevitable — GitHub adds event types over time) MUST route the new type to path 9 (the 204 default) unless a spec explicitly extends this list. The routing default is fail-closed: `return 204` rather than `return 202` for any unknown event type.

## 8. Testing Strategy

**Unit tests.** All under `trikon_cloud/webhook_receiver/tests/`. Every test is fully local — no network, no live AWS, no Docker. The testing dependencies (`pytest`, `moto`, `hypothesis`, `pytest-cov`) are already in the SDK's `dev` optional-dep group.

- `test_hmac_verifier.py` — encodes **Property 1** (verifier is total, hypothesis-driven, `max_examples=200`) and **Property 3** (AST check on `hmac_verifier.py` — no `==` between parsed digest and computed digest). Plus edge cases: `signature_header is None`, empty string, missing `sha256=` prefix, wrong-length digest, non-hex characters, unicode-in-header.
- `test_handler.py` — encodes **Property 2** (every 202 has a preceding successful SQS send). Plus per-branch tests for each of the 13 paths in §7's path enumeration. Uses `moto`'s `@mock_aws` decorator to mock SQS + Secrets Manager. Constructs API Gateway HTTP API event payloads via a helper in `conftest.py`.
- `test_sqs_writer.py` — Moto-backed tests for `SqsWriter.send_job`. Success path, retry-on-throttle path, permanent-failure-raises-SqsWriteError path.
- `test_models.py` — Pydantic round-trip tests. `GithubWebhookPayload.model_validate_json` on a canonical fixture, `SqsJobMessage.model_dump_json` produces the memo §5.2 field order, `ReceiverEnvConfig` raises `ValidationError` when required env vars are missing.

**Coverage floor.** 90% branch coverage on `handler.py`, `hmac_verifier.py`, `models.py`, `sqs_writer.py`. 80% branch coverage on `logger.py`. CDK stack code is excluded from the coverage measurement — synth is validated at the checkpoint via `cdk synth`. Configured via a `[tool.coverage.run]` section that omits `trikon_cloud/webhook_receiver/infra/**` and `trikon_cloud/webhook_receiver/tests/**`.

**Integration tests.** Out of this spec's scope. Flagged as a follow-up under `tests/integration/webhook_receiver/` that (a) deploys the CDK stack to a scratch AWS account, (b) POSTs a fixture webhook, (c) asserts the SQS queue received the expected message body, (d) tears down the stack. The follow-up spec's name (tentative): `trikon-cloud-webhook-receiver-integration-tests`.

**No throwaway scratch files.** Fixture data — canonical webhook payloads, expected SQS message bodies, sample HMAC signatures — lives inline in `conftest.py` as Python constants, NOT as top-level `.json` files in the tests directory (per the "no scratch files" rule in the dispatch prompt).

## 9. Public API Surface

Three items only (per Requirement 16.1):

1. **`POST /webhooks/github` HTTP endpoint contract.** External consumer: GitHub. Headers: `X-Hub-Signature-256` (required), `X-GitHub-Event` (required), `X-GitHub-Delivery` (required). Body: JSON matching memo §5.1. Response: 202 on enqueue, 200 on `ping`, 204 on non-enqueue event, 401 on HMAC failure, 400 on payload failure, 5xx on internal failure.
2. **`SqsJobMessage` body schema on `trikon-verify-jobs`.** Internal consumer: Spec 3 (`trikon-cloud-orchestrator`). Fields: `installation_id: int`, `repo_full_name: str`, `pr_number: int`, `head_sha: str` (40-char hex), `base_sha: str` (40-char hex), `event_type: str`, `sent_at: str`, `delivery_id: str`. Serialized via Pydantic v2's `model_dump_json()`.
3. **`WebhookReceiverStack` CDK class.** Internal consumer: the CDK app entry (`infra/app.py`) and the release engineer. Constructor: `WebhookReceiverStack(scope: Construct, id: str, *, webhook_secret_arn: str, dlq_arn: str | None = None)`. Attribute: `queue_url: str` — the URL of the `trikon-verify-jobs` queue, exported for Spec 3 to consume.

The `trikon_cloud.webhook_receiver` Python package exports NO public functions or classes for Python callers. Every module declares `__all__ = []` at import; every function that is not the Lambda handler is prefixed with `_` or is imported into `handler.py` for internal use only. The receiver is a Lambda function, not a library — its consumers are AWS Lambda's runtime and Spec 3's SQS reader, both invoked by AWS's infrastructure and not by user Python code.

## 10. Rejected Alternatives

Five alternatives considered and rejected. Each captures a load-bearing decision that a future reviewer might otherwise revisit without context.

- **FastAPI on Lambda via Mangum.** FastAPI is the natural Python web-framework pick and Mangum adapts it to Lambda's event-context contract. Rejected because Mangum + Starlette + FastAPI adds ~10 MB to the deployment bundle for a single-endpoint receiver, and every FastAPI feature this spec would use (typed request body, response model, dependency injection) is already covered by powertools' `APIGatewayHttpResolver` + Pydantic without the framework tax. Cold-start penalty vs powertools measured at +200-400 ms in similar internal benchmarks.
- **AWS Chalice.** Fine framework, similar shape to powertools' HTTP resolver. Rejected because Chalice's `Blueprint` and `Response` are dynamically typed (weaker mypy support) and its release cadence has been slower than powertools' since 2023. Neither is disqualifying, but powertools' AWS-native release cadence and its native structured-logging story tip the balance.
- **Plain `def lambda_handler(event, context)` — zero framework.** Would minimize the deployment bundle to under 10 MB and cold-start to under 800 ms. Rejected because a plain handler forces per-endpoint boilerplate for headers, HTTP method routing, JSON body parsing, and error handling — every one of which powertools makes typed and cold-start-cheap. The framework tax is small (~150 ms cold-start, ~4 MB bundle) and the ergonomic win (typed routing, structured logging) is large.
- **EventBridge instead of SQS between receiver and orchestrator.** EventBridge is a fine fit for the fan-out shape and gives us native archive + replay for post-hoc debugging. Rejected because EventBridge's retry semantics overlap with SQS's and the SQS → Lambda event-source mapping is the pattern the AWS console gives you when you draw this flow on a napkin. Simpler is better at MVP. Post-M2, if we ever fan out a single webhook to multiple downstream consumers (analytics tap, audit stream), EventBridge revisits as a candidate — but at M1 the fan-out is one, not many.
- **Kinesis Data Streams for chunked ingest.** Kinesis would give ordered, replayable streams with a 24-hour retention window. Rejected because Kinesis's shard-based cost model is punishing at low volumes (~$11/month baseline for one shard) and the ordered-delivery guarantee is irrelevant here — every webhook is independent, and cross-webhook ordering is not a requirement anywhere in the pipeline. SQS standard queue at zero cold-storage baseline is the right shape.
- **JWT-based receiver authentication instead of HMAC.** GitHub's webhook contract is HMAC-SHA256 (`X-Hub-Signature-256`); GitHub does not offer a JWT alternative for webhook signing. This is not a decision the receiver can override — it is dictated by the external contract. Called out here so a future reviewer does not propose it.
- **Cloudflare Workers as edge HMAC-verification layer in front of the Lambda.** Would give the fastest cold-start at the receiver edge (~5 ms) and DDoS protection out of the box. Rejected because it adds an inter-cloud hop for SQS enqueue and a second vendor to manage secrets + IAM against. The single-vendor AWS story is simpler at MVP. Post-M2, if webhook DDoS becomes a real concern, we can front the AWS-side receiver with a Cloudflare Worker for edge HMAC verification without changing the Lambda's code — this is a pluggable enhancement, not a hard revisit.

## 11. Release Note

**This spec ships no Trikon SDK version bump.** It ships a NEW deployment (AWS resources: API Gateway HTTP API, Lambda function, SQS queue, DLQ, IAM role, CloudWatch log group) and a NEW Python module (`trikon_cloud/webhook_receiver/`). The Trikon SDK's version stays at whatever the release engineer next bumps it to for the M1 GA release, independent of this spec's implementation-task PRs.

**The M1 GA depends on Spec 2 (`trikon-cloud-fargate-runner`) and Spec 3 (`trikon-cloud-orchestrator`) landing alongside this spec.** Spec 1 alone is not shippable end-to-end — a customer's webhook would land in an SQS queue that nobody reads from. The M1 milestone is green when all three specs are code-complete, unit-test-clean, mypy-strict-clean, and the CDK stacks deploy cleanly to a staging AWS account.

**Release plumbing is explicitly out of scope for this spec's implementation tasks** (Requirement 17). The M1 GA release cadence — version bumps in `pyproject.toml`, `CHANGELOG.md` entries, git commits, git tags, coordinated CDK deploys to the production account — is the release engineer's responsibility, not this spec's. This spec's implementation-task PRs land the code and IaC in the working tree; the release engineer decides when to ship.

**Follow-up specs referenced from here:**
- `trikon-cloud-installation-lifecycle` (deferred from this spec) — handles `installation`, `installation_repositories`, and any other event that affects the `trikon_installations` row lifecycle. Requirement 14.2.
- `trikon-cloud-check-run-rerequest` (deferred to M2) — handles `check_run.rerequested` to enable the GitHub UI "re-run" button. Requirement 14.1.
- `trikon-cloud-webhook-receiver-integration-tests` (out of unit-test scope) — deploys the CDK stack to a scratch AWS account and runs end-to-end webhook tests against live infrastructure. §8 (Testing Strategy).
- `trikon-cloud-webhook-receiver-waf-hardening` (post-M1 operational hardening) — adds a WAF rule allow-listing GitHub's published webhook IP ranges. Requirement 14.3.
