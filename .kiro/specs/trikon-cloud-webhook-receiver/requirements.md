# Requirements Document

## Introduction

Trikon Cloud is the hosted GitHub App tier of Trikon. Its M1 milestone ships three implementation specs — this spec, `trikon-cloud-fargate-runner`, and `trikon-cloud-orchestrator` — that together deliver the write path from a GitHub PR event to a persisted `Verdict` row plus a Check Run and PR comment on the PR. This spec is Spec 1 of the three: the **GitHub webhook receiver**. Every downstream spec inherits from the reference memo at `.kiro/specs/trikon-cloud-architecture/`; this spec inherits specifically from memo §3.2 (control-plane compute), §3.3 (SQS shape), §3.5 (secrets), §3.8 (region), §3.9 (networking), §5.1 (webhook payload subset), §5.2 (SQS job message body), §6 (security model), plus every cross-cutting invariant in the memo's `requirements.md` (Invariants 1, 2, 3, 6, 7, 8 apply here — Invariants 4 and 5 do not, since the receiver never imports the SDK and never runs verify wall-clock).

The Webhook_Receiver is an AWS Lambda function fronted by an API Gateway HTTP API. Its single external surface is `POST /webhooks/github`. On every request it (a) verifies the `X-Hub-Signature-256` HMAC-SHA256 signature against the webhook secret stored in AWS Secrets Manager, (b) reads `X-GitHub-Event`, `X-GitHub-Delivery`, and the JSON payload, (c) routes on the event type — `pull_request.opened` and `pull_request.synchronize` produce an SQS message on `trikon-verify-jobs`; every other event is a no-enqueue response — and (d) returns to GitHub inside the 10-second webhook timeout. It has zero DynamoDB access, zero S3 access, and never invokes the Trikon SDK. Its sole write side effect is `sqs.SendMessage` on `trikon-verify-jobs`.

Three decisions from the M1 architecture memo are pinned in this spec so downstream specs need not re-litigate them. **First**, the GitHub App marketplace listing name is `Trikon` (not `Trikon Cloud`); the App identifier / slug is `trikon`; the Check Run `name` field (rendered by Spec 2) is `Trikon`. **Second**, `check_run.rerequested` support is deferred to M2 — this spec routes `pull_request.opened` and `pull_request.synchronize` only. Any other `X-GitHub-Event` header value (including `check_run`, `push`, `installation`, `installation_repositories`, `issue_comment`, `ping`) receives an appropriate response (200 for `ping` with `{"status": "pong"}` per GitHub's App-ping convention; 204 for everything else) and does NOT produce an SQS message. **Third**, `installation` and `installation_repositories` event handling — the `trikon_installations` row lifecycle — is deferred to a follow-up spec (`trikon-cloud-installation-lifecycle`) to keep this spec's scope tight. This spec is a pure webhook receiver, not a lifecycle manager.

Idempotency (Invariant 3) is deliberately delegated downstream. GitHub retries webhooks that don't return 2xx within 10 seconds; the natural dedup key `(installation_id, repo_full_name, pr_number, head_sha)` is enforced at the Fargate runner's DynamoDB conditional `PutItem` (memo §5.5). The receiver therefore MAY enqueue duplicate SQS messages on a GitHub re-delivery — the invariant is satisfied at the single downstream site, per Invariant 3. The receiver preserves the `X-GitHub-Delivery` header verbatim as the SQS message's `delivery_id` field so log correlation and downstream dedup share the same identifier.

Never-fail-open (Invariant 2) is baked into every exception path. Any exception on the enqueue path — SQS unreachable, IAM `AccessDenied`, Secrets Manager throttled, malformed payload — surfaces as a 4xx or 5xx to GitHub and a structured WARNING log record naming the `delivery_id` and exception class. The receiver never returns 200 or 202 when the SQS write failed, and never returns 401 when HMAC verification succeeded. The only path to a 202 response is a successful `sqs.SendMessage` return.

This spec ships **no** Trikon SDK version bump. It ships a **new** Python package at `trikon_cloud/webhook_receiver/` inside the Trikon monorepo (packaged under a new optional-dependency group `cloud` in `pyproject.toml`) and a **new** AWS CDK stack. The M1 GA depends on all three implementation specs landing — Spec 1 alone is not shippable end-to-end. Any release plumbing (version bumps, commits, tags, pushes) is out of this spec's scope and is the release engineer's responsibility.

## Glossary

- **Trikon**: The product this codebase implements — the verification layer for autonomous AI coding agents. Consistent product name across every requirement (never AgentGuard, never `trikon-cloud` in user-facing copy — `trikon-cloud` / `trikon_cloud` are acceptable as identifiers only, per Invariant 7).
- **Trikon_Cloud**: The commercial hosted-App tier of Trikon. The M1 milestone ships webhook receiver → SQS → orchestrator → Fargate runner → Check Run + PR comment. M2 adds the analytics dashboard.
- **Webhook_Receiver**: The AWS Lambda function that owns the `POST /webhooks/github` endpoint. Deployment target of this spec. Python 3.11 runtime, 512 MB memory, no VPC attachment, SnapStart enabled where available (per memo §3.2).
- **API_Gateway**: The AWS API Gateway HTTP API resource that fronts the Webhook_Receiver. Regional endpoint, us-east-1 (per memo §3.8), 10-second hard integration timeout matching GitHub's webhook timeout.
- **HMAC_Verifier**: The pure function `verify_signature(body: bytes, signature_header: str, secret: bytes) -> bool` inside `trikon_cloud/webhook_receiver/hmac_verifier.py`. Computes HMAC-SHA256 over the raw request body and compares constant-time with `hmac.compare_digest` against the parsed `X-Hub-Signature-256` header value.
- **SQS_Writer**: The module `trikon_cloud/webhook_receiver/sqs_writer.py` that owns the single `sqs.SendMessage` call site. Wraps `boto3` with typed retries and a single-line failure log record.
- **Verify_Jobs_Queue**: The AWS SQS standard queue named `trikon-verify-jobs`. Visibility timeout 15 minutes, max receive count 3, DLQ `trikon-verify-jobs-dlq` (per memo §3.3). This spec's IaC references the queue by name; the queue itself is created by this spec's CDK stack (since it is the queue's writer and the memo assigns queue creation to whichever spec lands first).
- **Github_Webhook_Payload**: The Pydantic v2 model `GithubWebhookPayload` in `trikon_cloud/webhook_receiver/models.py` capturing the subset in memo §5.1: `action`, `installation.id`, `repository.full_name`, `repository.default_branch`, `pull_request.number`, `pull_request.head.sha`, `pull_request.base.sha`, `sender.login`. `model_config = ConfigDict(extra="allow")` so unknown fields do not reject the payload.
- **Sqs_Job_Message**: The Pydantic v2 model `SqsJobMessage` in `trikon_cloud/webhook_receiver/models.py` capturing the memo §5.2 SQS body shape: `installation_id: int`, `repo_full_name: str`, `pr_number: int`, `head_sha: str`, `base_sha: str`, `event_type: str`, `sent_at: str` (ISO 8601 UTC with millisecond precision), `delivery_id: str`.
- **Webhook_Secret**: The bytes value stored under the Secrets Manager secret name `trikon-cloud/github-app-webhook-secret` (per memo §3.5). Versioned. Only the Webhook_Receiver's Lambda role can read it. The secret material itself is populated out-of-band — the CDK stack references the secret by name / ARN, does NOT create it with a value.
- **Github_Delivery_Id**: The value of the `X-GitHub-Delivery` HTTP header. A UUID assigned by GitHub per webhook delivery attempt; identical across retries of the same delivery. Preserved verbatim in `Sqs_Job_Message.delivery_id`.
- **Github_Event_Type**: The value of the `X-GitHub-Event` HTTP header. Handled values in M1: `pull_request` (with action `opened` or `synchronize`) → enqueue; `ping` → 200 pong; everything else → 204.
- **Structured_Logger**: The `aws-lambda-powertools` Logger instance configured in `trikon_cloud/webhook_receiver/logger.py`. Emits structured JSON log records with the powertools standard fields (`timestamp`, `level`, `location`, `service`, `xray_trace_id`, `cold_start`, `function_name`, `function_memory_size`) plus the request-scoped fields (`delivery_id`, `event_type`, `installation_id`, `repo_full_name`, `pr_number`). PII patterns are redacted at emit per memo §6.
- **Webhook_Receiver_Stack**: The AWS CDK stack class `WebhookReceiverStack` in `trikon_cloud/webhook_receiver/infra/webhook_receiver_stack.py`. Deploys the API Gateway HTTP API, the Lambda function, the SQS queue + DLQ, the IAM role for the Lambda, and CloudWatch log-group retention. Does NOT create the Secrets Manager secret — the secret's value is populated out-of-band.
- **Never_Fail_Open_Contract**: The Trikon-wide invariant that a broken enqueue path surfaces as an error past the module boundary, never as a silent 2xx. Codified in the memo's Invariant 2. This spec applies the contract to every exception path in the receiver: HMAC failure → 401, malformed body → 400, SQS send failure → 5xx, Secrets Manager read failure → 5xx. Never 200 or 202 on an exception.
- **Public_API_Surface**: The set of contracts consumers depend on across releases. For this spec: (a) the `POST /webhooks/github` HTTP endpoint's request contract with GitHub (external), (b) the `Sqs_Job_Message` body schema on `Verify_Jobs_Queue` (internal contract with Spec 3, the orchestrator), (c) the CDK stack class name `WebhookReceiverStack` (internal contract with the release engineer). Nothing else is exposed. The `trikon_cloud.webhook_receiver` Python package deliberately exports NO public functions or classes — its only entry point is the Lambda handler, invoked by the AWS runtime.
- **Cross_Cutting_Invariants**: The set of invariants declared in `.kiro/specs/trikon-cloud-architecture/requirements.md`. This spec is bound by Invariants 1, 2, 3, 6, 7, 8 — each explicitly cited in the requirements below. Invariant 4 (SDK boundary) is trivially satisfied since the receiver does not import `trikon`. Invariant 5 (cost-per-verdict) applies downstream to the runner, not to the receiver.

## Requirements

### Requirement 1: Endpoint contract

**User Story:** As GitHub, I want to POST a webhook payload to `/webhooks/github` and receive an acceptance response inside 10 seconds, so that my delivery is not marked as failed.

#### Acceptance Criteria

1. WHEN GitHub sends a POST to `/webhooks/github` with a valid `X-Hub-Signature-256` header, `X-GitHub-Event: pull_request` header, `X-GitHub-Delivery` header, and a JSON body whose `action` field is `opened` or `synchronize`, THE Webhook_Receiver SHALL enqueue exactly one message on Verify_Jobs_Queue matching the Sqs_Job_Message shape (per memo §5.2) and SHALL return 202 Accepted to GitHub.
2. THE Webhook_Receiver SHALL complete the entire request-handling path — HMAC verification, payload parse, SQS enqueue, response emission — within 10 seconds of the request's arrival at API_Gateway.
3. WHERE the request path is any value other than `/webhooks/github`, THE Webhook_Receiver SHALL return 404 Not Found without invoking HMAC verification or SQS write.
4. WHERE the HTTP method is any value other than POST on `/webhooks/github`, THE Webhook_Receiver SHALL return 405 Method Not Allowed without invoking HMAC verification or SQS write.
5. WHEN a request is accepted (SQS write succeeds) AND enqueued, THE Webhook_Receiver SHALL return the JSON body `{"status": "accepted", "delivery_id": "<X-GitHub-Delivery value>"}` on the 202 response.

### Requirement 2: HMAC-SHA256 verification

**User Story:** As Trikon Cloud, I want to reject any webhook request whose signature does not match the shared webhook secret, so that an attacker who guesses the endpoint URL cannot inject fake PR events.

#### Acceptance Criteria

1. THE HMAC_Verifier SHALL compute the digest over the raw request body bytes (not the JSON-parsed body) using HMAC-SHA256 and the Webhook_Secret value read from Secrets Manager.
2. THE HMAC_Verifier SHALL parse the `X-Hub-Signature-256` header, expected format `sha256=<hex-digest>`, and compare the parsed hex-digest against the computed digest via `hmac.compare_digest` (constant-time comparison).
3. IF the `X-Hub-Signature-256` header is absent, empty, or missing the `sha256=` prefix, THEN THE Webhook_Receiver SHALL return 401 Unauthorized and SHALL NOT invoke SQS_Writer.
4. IF the hex-digest parsed from the `X-Hub-Signature-256` header does not equal the computed digest under `hmac.compare_digest`, THEN THE Webhook_Receiver SHALL return 401 Unauthorized and SHALL NOT invoke SQS_Writer.
5. WHEN HMAC verification succeeds, THE Webhook_Receiver SHALL proceed to event routing (Requirement 3).
6. THE Webhook_Receiver SHALL cache the Webhook_Secret value in Lambda's execution-context memory across warm invocations, and SHALL re-fetch on cold start via `secretsmanager:GetSecretValue`.
7. THE HMAC_Verifier SHALL NOT log the Webhook_Secret value, the parsed hex-digest, or the computed digest at any log level (per Invariant 6).

### Requirement 3: Event routing

**User Story:** As Trikon Cloud, I want to enqueue verify jobs only for PR events that actually need verification, so that non-PR events do not spuriously drive Fargate task launches.

#### Acceptance Criteria

1. WHEN `X-GitHub-Event` equals `pull_request` AND the parsed payload's `action` field equals `opened` OR `synchronize`, THE Webhook_Receiver SHALL enqueue a Sqs_Job_Message.
2. WHEN `X-GitHub-Event` equals `pull_request` AND the parsed payload's `action` field is any value other than `opened` or `synchronize`, THE Webhook_Receiver SHALL return 204 No Content and SHALL NOT invoke SQS_Writer.
3. WHEN `X-GitHub-Event` equals `ping`, THE Webhook_Receiver SHALL return 200 OK with JSON body `{"status": "pong"}` (per GitHub's App-ping convention) and SHALL NOT invoke SQS_Writer.
4. WHEN `X-GitHub-Event` has any value other than `pull_request` or `ping` — including `check_run`, `push`, `installation`, `installation_repositories`, `issue_comment`, `check_suite`, `pull_request_review`, `pull_request_review_comment`, and every future event GitHub may introduce — THE Webhook_Receiver SHALL return 204 No Content and SHALL NOT invoke SQS_Writer.
5. IF the `X-GitHub-Event` header is absent or empty, THEN THE Webhook_Receiver SHALL return 400 Bad Request and SHALL NOT invoke SQS_Writer.
6. WHERE M1 defers `check_run.rerequested` support, THE Webhook_Receiver SHALL treat `check_run` events per Acceptance Criterion 3.4 (204 No Content). The re-request capability will land in a follow-up M2 spec.

### Requirement 4: Payload extraction

**User Story:** As Trikon Cloud, I want the receiver to extract exactly the field subset defined in memo §5.1 from the webhook payload, so that the SQS message body is a byte-consistent function of the input.

#### Acceptance Criteria

1. WHEN HMAC verification succeeds AND event routing selects an enqueue-worthy event, THE Webhook_Receiver SHALL parse the request body as JSON and validate it against the Github_Webhook_Payload Pydantic v2 model.
2. THE Github_Webhook_Payload model SHALL capture exactly these fields (per memo §5.1): `action: str`, `installation.id: int`, `repository.full_name: str`, `repository.default_branch: str`, `pull_request.number: int`, `pull_request.head.sha: str`, `pull_request.base.sha: str`, `sender.login: str`.
3. THE Github_Webhook_Payload model SHALL configure `extra="allow"` so unknown fields on the payload do not cause validation to fail (GitHub adds fields over time; the receiver reads only the fields it knows about).
4. IF the request body is not valid JSON, THEN THE Webhook_Receiver SHALL return 400 Bad Request with body `{"error": "malformed_json"}` and SHALL NOT invoke SQS_Writer.
5. IF the request body parses as JSON but fails Github_Webhook_Payload schema validation (missing required field, wrong type), THEN THE Webhook_Receiver SHALL return 400 Bad Request with body `{"error": "malformed_payload", "detail": "<pydantic validation summary>"}` and SHALL NOT invoke SQS_Writer.
6. THE Webhook_Receiver SHALL NOT log the full request body at INFO level; the body is only reachable at DEBUG level and is redacted per Invariant 6.

### Requirement 5: SQS write

**User Story:** As Spec 3 (the orchestrator Lambda), I want every enqueued SQS message to carry the memo §5.2 body shape exactly, so that my Pydantic deserialization on the read side matches the write side byte-for-byte.

#### Acceptance Criteria

1. THE SQS_Writer SHALL send exactly one message per accepted request via `sqs.SendMessage` on Verify_Jobs_Queue.
2. THE Sqs_Job_Message body SHALL match memo §5.2 verbatim: `installation_id: int`, `repo_full_name: str`, `pr_number: int`, `head_sha: str` (40-char hex), `base_sha: str` (40-char hex), `event_type: str` (formatted as `"pull_request.opened"` or `"pull_request.synchronize"` — the header value dotted with the action), `sent_at: str` (ISO 8601 UTC timestamp with millisecond precision — e.g., `"2024-11-14T12:34:56.789Z"`), `delivery_id: str` (the verbatim `X-GitHub-Delivery` header value).
3. THE SQS_Writer SHALL serialize Sqs_Job_Message via `pydantic.BaseModel.model_dump_json()` — not via `json.dumps(model.model_dump())` — so the JSON output is byte-consistent with the model's declared field order.
4. WHERE the `sqs.SendMessage` call fails with a retryable error (`ThrottlingException`, `ServiceUnavailable`), THE SQS_Writer SHALL retry via the boto3 default retry configuration (adaptive mode, max 3 attempts).
5. IF `sqs.SendMessage` fails after all retries with any exception, THEN THE Webhook_Receiver SHALL return 502 Bad Gateway to GitHub AND SHALL emit a WARNING-level log record on Structured_Logger naming the `delivery_id` and the exception class (per Invariant 2). THE Webhook_Receiver SHALL NOT return 200 or 202 on this path.
6. THE SQS_Writer SHALL NOT set a `MessageDeduplicationId` or `MessageGroupId` on the send call (Verify_Jobs_Queue is a standard queue, not FIFO, per memo §3.3).

### Requirement 6: Idempotency delegation

**User Story:** As Trikon Cloud, I want GitHub's automatic webhook re-deliveries to converge on a single verdict downstream, so that a design partner sees exactly one Check Run and one PR comment per `head_sha`.

#### Acceptance Criteria

1. WHERE GitHub re-delivers a webhook with an identical `X-GitHub-Delivery` header value, THE Webhook_Receiver MAY enqueue a duplicate Sqs_Job_Message on Verify_Jobs_Queue.
2. THE Webhook_Receiver SHALL NOT itself dedupe on `X-GitHub-Delivery` — it has no persistent state store, and adding one would violate the memo §3.9 no-VPC constraint and inflate cold-start latency.
3. THE downstream Fargate runner's DynamoDB conditional `PutItem` on the natural key (per memo §5.5) SHALL be the sole idempotency site (per Invariant 3). This requirement documents the delegation; the enforcement lives in Spec 2 (`trikon-cloud-fargate-runner`).
4. THE Webhook_Receiver SHALL preserve the `X-GitHub-Delivery` header value verbatim in Sqs_Job_Message.delivery_id, so downstream dedup and log correlation share the same identifier.

### Requirement 7: Never-fail-open discipline

**User Story:** As Trikon Cloud, I want every failure inside the receiver to surface as a non-2xx response to GitHub and a WARNING log line, so that no PR is silently dropped by a broken receiver.

#### Acceptance Criteria

1. THE Webhook_Receiver SHALL return a non-2xx status code on every exception path (per Invariant 2). The path from request-arrival to a 200 or 202 response passes ONLY through: (a) successful HMAC verification, (b) successful event routing, (c) successful payload extraction, (d) successful SQS send.
2. IF `secretsmanager:GetSecretValue` on Webhook_Secret fails with any exception (`AccessDenied`, `ResourceNotFound`, `ThrottlingException`), THEN THE Webhook_Receiver SHALL return 500 Internal Server Error AND SHALL emit a WARNING-level log record naming the exception class. THE Webhook_Receiver SHALL NOT return 200 or 202 on this path.
3. IF the JSON payload parses but `installation.id` or `repository.full_name` or `pull_request.number` or `pull_request.head.sha` or `pull_request.base.sha` is missing after Pydantic validation, THEN THE Webhook_Receiver SHALL return 400 Bad Request per Requirement 4.5 (this is a payload-shape failure, not an internal failure).
4. IF an unhandled exception escapes the handler function, THEN THE aws-lambda-powertools default exception handler SHALL emit a JSON log record with `level=ERROR`, correlation via `delivery_id` if available, and return 500 Internal Server Error to API_Gateway.
5. THE Webhook_Receiver SHALL NOT return the exception's traceback or `str(exception)` in any response body — internal error surfaces are opaque to GitHub.

### Requirement 8: Structured logging

**User Story:** As a Trikon operator debugging a webhook failure, I want every log line to be structured JSON with a stable field set, so that I can filter CloudWatch on `delivery_id` or `installation_id` without regex-parsing free-text.

#### Acceptance Criteria

1. THE Structured_Logger SHALL emit every log record as a single-line JSON object with the aws-lambda-powertools standard fields (`timestamp`, `level`, `location`, `service`, `function_name`, `function_memory_size`, `xray_trace_id`, `cold_start`) plus the request-scoped fields defined in Acceptance Criterion 8.2.
2. THE Structured_Logger SHALL inject these request-scoped fields on every log record emitted during a single request: `delivery_id: str | null`, `event_type: str | null`, `installation_id: int | null`, `repo_full_name: str | null`, `pr_number: int | null`. Values are `null` when the corresponding datum has not yet been parsed at emit time (e.g., a HMAC-failure log record emitted before payload extraction has `installation_id=null`).
3. THE Structured_Logger SHALL redact PII patterns per memo §6 before emission: any substring matching the regex `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` is replaced with `<email>`; any `-----BEGIN [A-Z ]+ PRIVATE KEY-----` block is replaced with `<private-key>`. The redaction is a pre-emit filter registered on the powertools Logger.
4. WHEN Structured_Logger emits an ERROR or WARNING record, THE record SHALL NOT interpolate the Webhook_Secret value, the parsed HMAC digest, or any Secrets Manager response body (per Invariant 6).
5. THE Structured_Logger SHALL log at INFO level on the successful-enqueue path (one record: `"webhook accepted"` with `delivery_id`, `event_type`, `installation_id`, `repo_full_name`, `pr_number`); at INFO level on the non-enqueue paths (`"non-enqueue event"` for 204 responses, `"ping"` for the 200 pong); at WARNING level on the HMAC-failure and payload-failure paths; at ERROR level on the SQS-failure and Secrets-Manager-failure paths.
6. THE Structured_Logger SHALL NOT log the full request body at INFO level; the body content is reachable only at DEBUG level.

### Requirement 9: Unknown installation handling

**User Story:** As Trikon Cloud, I want the receiver to enqueue jobs for installations whose `trikon_installations` row is missing rather than silently drop them, so that a race condition or manual uninstall does not swallow the PR event.

#### Acceptance Criteria

1. THE Webhook_Receiver SHALL NOT read `trikon_installations` on any code path. Enforcement: the Lambda's IAM role does not grant any `dynamodb:*` action.
2. WHERE an incoming webhook references an `installation.id` value with no corresponding `trikon_installations` row (installation was uninstalled after this webhook was queued at GitHub, or a race condition on setup), THE Webhook_Receiver SHALL enqueue the Sqs_Job_Message anyway and defer the missing-row diagnosis to Spec 3 (the orchestrator).
3. THE downstream Fargate runner SHALL, per Invariant 2, surface a `require_human` verdict with reason `"installation not registered"` if the row is genuinely missing when the task attempts to fetch the installation token.

### Requirement 10: Region and deployment target

**User Story:** As a Trikon operator, I want the receiver deployed in the single memo-pinned region, so that latency to GitHub's US-East-heavy egress stays under 100 ms round-trip (per memo §3.8).

#### Acceptance Criteria

1. THE Webhook_Receiver_Stack SHALL deploy every resource — API_Gateway HTTP API, Lambda function, Verify_Jobs_Queue, DLQ, Lambda execution IAM role — into region `us-east-1`.
2. THE Webhook_Receiver_Stack SHALL reference the Secrets Manager secret `trikon-cloud/github-app-webhook-secret` by name; the secret material is populated out-of-band and is NOT created by this stack.
3. THE Webhook_Receiver_Stack SHALL NOT attach the Lambda function to a VPC (per memo §3.9).

### Requirement 11: Cross-cutting invariant compliance

**User Story:** As the Trikon Cloud architecture review, I want this spec to name every applicable cross-cutting invariant from the memo, so that no downstream spec re-litigates a decision the memo already resolved.

#### Acceptance Criteria

1. THE Webhook_Receiver SHALL NOT read or write DynamoDB, S3, or any per-installation Secrets Manager secret (per Invariant 1, tenant isolation). Enforcement: the Lambda's IAM role grants only `sqs:SendMessage` on Verify_Jobs_Queue, `secretsmanager:GetSecretValue` on Webhook_Secret, and `logs:*` on its own CloudWatch log group.
2. THE Webhook_Receiver SHALL surface every enqueue-path exception as a non-2xx response (per Invariant 2, never-fail-open). See Requirement 7 for the explicit exception matrix.
3. THE Webhook_Receiver SHALL delegate idempotency to the downstream runner's DynamoDB `ConditionExpression` (per Invariant 3, idempotency on the natural key). See Requirement 6.
4. THE Webhook_Receiver SHALL NOT emit any log record containing the Webhook_Secret value, a parsed HMAC digest, an installation token, or a Secrets Manager response body (per Invariant 6, secrets handling). See Requirement 8.3 and Requirement 8.4.
5. THE Webhook_Receiver SHALL render the product name `Trikon` in every user-facing string (per Invariant 7, product name). Identifiers may use `trikon-cloud` (e.g., the Secrets Manager secret name `trikon-cloud/github-app-webhook-secret`) or `trikon_cloud` (Python package name). No user-facing copy in this spec surfaces AgentGuard or any camel-case variation.
6. THE `trikon_cloud/webhook_receiver/` package SHALL pass `uv run mypy --strict` on every module (per Invariant 8, type safety). No public API surface in this package uses `dict[str, Any]`, `list[Any]`, or `object` — the payload model and SQS body model are Pydantic v2 BaseModel subclasses.

### Requirement 12: Type-safety bake-in

**User Story:** As a downstream reader of `trikon_cloud/webhook_receiver/`, I want every value crossing a module boundary to have a concrete type, so that a refactor that changes a field shape surfaces as a mypy error, not a runtime `KeyError`.

#### Acceptance Criteria

1. THE Github_Webhook_Payload, Sqs_Job_Message, and env-var config models SHALL be `pydantic.BaseModel` subclasses (Pydantic v2).
2. THE HMAC_Verifier's public function SHALL be typed `def verify_signature(body: bytes, signature_header: str | None, secret: bytes) -> bool`.
3. THE SQS_Writer's public function SHALL be typed `def send_job(message: SqsJobMessage, queue_url: str) -> None` and SHALL raise a concrete exception subclass (`SqsWriteError`) on failure — not a bare `Exception`.
4. THE Structured_Logger SHALL be a `aws_lambda_powertools.Logger` instance whose `service` field is set to `trikon-cloud-webhook-receiver`.
5. THE Webhook_Receiver package SHALL define an `__all__` on every module that exports public symbols and SHALL declare no `dict[str, Any]` on any exported function signature.
6. THE Lambda handler SHALL be typed `def handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]` per the AWS-provided event shape — this is the only `object`-typed surface, and it is the AWS SDK's own contract, not one this spec introduces.

### Requirement 13: Testing coverage bar

**User Story:** As a Trikon Cloud release engineer, I want a minimum branch-coverage floor on the receiver's logic, so that a regression that skips HMAC verification is caught before it ships.

#### Acceptance Criteria

1. THE unit test suite for `trikon_cloud/webhook_receiver/` SHALL achieve 90% branch coverage on `handler.py`, `hmac_verifier.py`, `models.py`, and `sqs_writer.py`.
2. THE unit test suite SHALL achieve 80% branch coverage on `logger.py`.
3. THE CDK stack code (`infra/webhook_receiver_stack.py`, `infra/app.py`) SHALL be excluded from the coverage measurement (CDK synth is validated at the checkpoint, not by unit tests).
4. THE unit test suite SHALL NOT make any live AWS API call. SQS and Secrets Manager are mocked via `moto`; the powertools Logger is exercised against `caplog`.
5. THE unit test suite SHALL include hypothesis property tests for the HMAC_Verifier (Property 1 in design.md — verifier is total) and for the enqueue-atomicity of the handler (Property 2 in design.md — every 202 has a preceding successful SQS send).

### Requirement 14: Deferred scope

**User Story:** As a Trikon Cloud release engineer, I want the M1 scope of this spec to be tight, so that the M1 GA milestone is not blocked by capabilities that belong in M2 or a follow-up spec.

#### Acceptance Criteria

1. THE Webhook_Receiver SHALL NOT handle `check_run.rerequested` events in M1 — the `check_run` event type is routed to the 204 path per Requirement 3.4. This capability is deferred to an M2 spec.
2. THE Webhook_Receiver SHALL NOT handle `installation` or `installation_repositories` events in M1 — both event types are routed to the 204 path per Requirement 3.4. The `trikon_installations` row lifecycle is deferred to a follow-up spec (`trikon-cloud-installation-lifecycle`).
3. THE Webhook_Receiver SHALL NOT implement rate limiting at the API Gateway layer in M1 (no WAF rule, no throttling). Post-M1 hardening — a WAF rule that restricts source IPs to GitHub's published webhook IP allowlist — is deferred to an operational hardening spec.
4. THE Webhook_Receiver SHALL NOT publish to any observability sink other than CloudWatch Logs in M1 — no CloudWatch Metrics custom metrics, no X-Ray custom subsegments beyond the powertools default. This may be extended post-M1 if operational needs surface.

### Requirement 15: Cold-start and warm-invocation latency

**User Story:** As Trikon Cloud, I want the receiver's cold-start p99 to fit inside GitHub's 10-second webhook timeout with room for the SQS round-trip, so that no delivery attempt fails on a cold Lambda.

#### Acceptance Criteria

1. THE Webhook_Receiver Lambda's cold-start-including-init latency SHALL be under 2 seconds at p99. Enforcement: SnapStart is enabled where available; the Lambda deployment package targets 15-20 MB unzipped (well inside the 50 MB zip limit and the 250 MB uncompressed function-code limit).
2. THE Webhook_Receiver Lambda's warm-invocation latency SHALL be under 200 milliseconds at p50 and under 500 milliseconds at p99. Enforcement: the Webhook_Secret is cached in Lambda execution-context memory across warm invocations (Requirement 2.6); the SQS_Writer reuses a module-level `boto3` client instance.
3. THE Webhook_Receiver_Stack SHALL configure the Lambda function with 512 MB memory (per memo §3.2). The memory setting balances cold-start CPU allocation (Lambda's CPU scales linearly with memory) against baseline cost.
4. THE Webhook_Receiver package's deployment size SHALL be under 20 MB unzipped. Enforcement: the dependency set is `aws-lambda-powertools[all]`, `pydantic>=2`, `boto3`, `botocore` — the receiver does NOT import from `trikon` (SDK decoupling).

### Requirement 16: Public API surface

**User Story:** As a downstream consumer of this spec, I want a compact, explicit list of the interfaces this spec exposes, so that a change to any of them is understood as a versioning event.

#### Acceptance Criteria

1. THE Public_API_Surface of this spec SHALL be exactly three items:
   (a) The `POST /webhooks/github` HTTP endpoint's request contract — headers `X-Hub-Signature-256`, `X-GitHub-Event`, `X-GitHub-Delivery`; body per memo §5.1. External consumer: GitHub.
   (b) The `Sqs_Job_Message` body schema on Verify_Jobs_Queue — the eight fields per memo §5.2. Internal consumer: Spec 3 (`trikon-cloud-orchestrator`).
   (c) The CDK stack class name `WebhookReceiverStack` and its constructor signature `(scope: Construct, id: str, *, webhook_secret_arn: str, dlq_arn: str | None = None)`. Internal consumer: the CDK app entry (`infra/app.py`) and the release engineer.
2. THE `trikon_cloud.webhook_receiver` Python package SHALL export NO public functions or classes for external Python callers. The Lambda handler function is invoked by the AWS runtime by its fully-qualified path `trikon_cloud.webhook_receiver.handler.handler`; every other symbol in the package is private (module-scoped or prefixed with `_`).
3. A future change to any of the three Public_API_Surface items SHALL trigger a versioning event: (a) a coordinated release of Spec 3 for a change to the SQS body; (b) a coordinated GitHub App marketplace update for a change to the endpoint contract; (c) a CDK stack rev for a change to the stack constructor.

### Requirement 17: Release plumbing scope

**User Story:** As a Trikon Cloud release engineer, I want release plumbing to be explicitly out of the implementation-task scope of this spec, so that the M1 milestone's version bumps and coordinated deploys are handled at the release-engineer level, not baked into individual implementation-task PRs.

#### Acceptance Criteria

1. THE implementation tasks of this spec SHALL NOT bump the `pyproject.toml` `version` field. The Trikon SDK's version is unchanged by this spec; the M1 GA release commit (owned by the release engineer) may bump the version and add a `CHANGELOG.md` entry, but that commit is not one of this spec's tasks.
2. THE implementation tasks of this spec SHALL NOT run any `git` operation against the Trikon parent repository (no `git add`, `git commit`, `git push`, `git tag`, `git branch`, `git checkout`). All file edits are staged in the working tree; commit / tag / push cadence is the release engineer's responsibility.
3. THE implementation tasks of this spec SHALL NOT deploy to AWS. The CDK stack code is authored under `infra/`, and the manual deploy gate (`cdk deploy WebhookReceiverStack`) is the release engineer's responsibility, not this spec's.
4. THE implementation tasks of this spec SHALL NOT create the Secrets Manager secret `trikon-cloud/github-app-webhook-secret`. The CDK stack references the secret by name / ARN; the secret's material is populated out-of-band by whoever registers the GitHub App on the marketplace.
