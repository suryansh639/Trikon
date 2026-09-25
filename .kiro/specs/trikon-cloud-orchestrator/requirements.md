# Requirements Document

## Introduction

Trikon Cloud Spec 3 — the cloud orchestrator layer. This spec owns two Python 3.11 AWS Lambda functions that together bridge Spec 1 (webhook receiver) and Spec 2 (Fargate runner):

1. **Orchestrator dispatcher** (`trikon_cloud/orchestrator/handler.py`) — SQS-triggered on the existing `trikon-verify-jobs` queue. Translates one `SqsJobMessage` into one `ecs.RunTask` call per architecture memo §5.3.
2. **Installation-lifecycle handler** (`trikon_cloud/installation_lifecycle/handler.py`) — SQS-triggered on a NEW `trikon-cloud-installation-events` queue plus DLQ. Provisions and deprovisions per-installation IAM task roles and tracks installation status in the `trikon-cloud-installations` DynamoDB table.

Cross-cutting invariants (1 through 8) are defined at `.kiro/specs/trikon-cloud-architecture/requirements.md` and are cited by number below. The Spec 1 SQS message shape (`SqsJobMessage`) is inherited verbatim from `trikon_cloud/webhook_receiver/models.py`. The Spec 2 env-var contract (`RunnerEnvConfig` core fields) is inherited from `trikon_cloud/fargate_runner/models.py`. The Spec 2 CDK IAM contract (`FargateRunnerStack.build_task_role_for_installation`) is the byte-match target for this spec's runtime IAM renderer.

### Open items for design phase

**Open Item (a) — Never-Fail-Open reconciliation with DLQ.** Invariant 2 (Never-Fail-Open) requires that a terminal orchestrator failure produce a customer-visible verdict. Two candidate mechanisms exist: (i) grant the Orchestrator_Handler narrow `dynamodb:PutItem` on `trikon_verdicts` with a `LeadingKeys` condition so it writes a synthetic `require_human` row directly, or (ii) route DLQ-bound messages back through Spec 2's Fargate runner with a `TRIKON_SYNTHETIC_REQUIRE_HUMAN=true` env flag so the runner writes the row. Requirements state the outcome; design MUST select the mechanism.

**Open Item (b) — Task-definition revision resolution.** The Orchestrator_Handler MUST pin an active task-definition revision at `ecs.RunTask` time (not use `:LATEST`). Two candidate mechanisms exist: (i) `ecs:DescribeTaskDefinition` at cold start caching the resolved revision in module scope, or (ii) an SSM parameter written by Spec 2's CDK stack and read at cold start. Requirements state the outcome; design MUST select the mechanism.

### External dependency (scope note)

The Spec 1 (`trikon-cloud-webhook-receiver`) webhook receiver Lambda MUST be amended to route `installation` and `installation_repositories` webhook event types to the new `trikon-cloud-installation-events` queue in addition to its existing routing of `pull_request` and `check_run` events to `trikon-verify-jobs`. That amendment (adding an event-type routing branch, an `sqs:SendMessage` IAM grant on the new queue, and a `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` environment variable) is coordinated with this spec but is **not** part of this spec's implementation tasks. This spec depends on the amendment landing before end-to-end lifecycle testing is possible.

## Glossary

- **Orchestrator_Handler** — The AWS Lambda function at `trikon_cloud/orchestrator/handler.py`. SQS event source is `trikon-verify-jobs` with batch size 1. Runtime is Python 3.11.
- **Lifecycle_Handler** — The AWS Lambda function at `trikon_cloud/installation_lifecycle/handler.py`. SQS event source is `trikon-cloud-installation-events` with batch size 1. Runtime is Python 3.11.
- **Runtime_IAM_Renderer** — The pure function `render_installation_policy_document(installation_id: int, app_private_key_secret_arn: str) -> InstallationPolicyDocument` at `trikon_cloud/installation_lifecycle/iam_template.py`. Returns a typed Pydantic model — no `dict[str, Any]`.
- **SqsJobMessage** — The Pydantic model at `trikon_cloud/webhook_receiver/models.py`. Fields: `installation_id: int`, `repo_full_name: str`, `pr_number: int`, `head_sha: str`, `base_sha: str`, `event_type: str`, `sent_at: str`, `delivery_id: str`. All required. `extra` is not permitted.
- **InstallationEventMessage** — The Pydantic model this spec defines at `trikon_cloud/installation_lifecycle/models.py`. All fields required, `extra` not permitted, `frozen=True`.
- **RunnerEnvConfig core fields** — The seven `TRIKON_*` fields on `trikon_cloud/fargate_runner/models.py::RunnerEnvConfig` populated via `ecs.RunTask` overrides: `TRIKON_INSTALLATION_ID`, `TRIKON_REPO_FULL_NAME`, `TRIKON_PR_NUMBER`, `TRIKON_HEAD_SHA`, `TRIKON_BASE_SHA`, `TRIKON_EVENT_TYPE`, `TRIKON_DELIVERY_ID`.
- **Terminal error** — An error class the Orchestrator_Handler classifies as non-retryable. Membership: `boto3` `ClientError` with `Code` in {`InvalidParameterException`, `AccessDeniedException`, `ClusterNotFoundException`, `TaskDefinitionNotFound`, `NoSuchEntity`}; `pydantic.ValidationError` on the SQS message; payload larger than 256 KB.
- **Transient error** — An error class the Orchestrator_Handler classifies as retryable. Membership: `boto3` `ClientError` with `Code` in {`ThrottlingException`, `RequestLimitExceeded`, `ServiceUnavailable`, `InternalFailure`}; socket-level or DNS errors from `botocore`; HTTP 5xx from AWS APIs.
- **Trikon Cloud Installations Table** — The DynamoDB table `trikon-cloud-installations` this spec owns. Partition key `installation_id: N`. Attributes: `status: S` ("active" or "disabled"), `github_app_id: N`, `created_at: S` (ISO-8601), `updated_at: S` (ISO-8601), `repositories: SS` (set of `owner/repo` strings).
- **Structured log field set** — The JSON fields the Powertools `Logger` emits on every log record: `installation_id`, `repo_full_name`, `pr_number`, `delivery_id`, `event_type`, plus Powertools defaults (`level`, `message`, `timestamp`, `service`, `function_name`, `function_request_id`, `xray_trace_id`).
- **§5.3** — Section 5.3 of `.kiro/specs/trikon-cloud-architecture/design.md`. Authoritative contract for the `ecs.RunTask` call shape.

## Requirements

---

## Section A — Orchestrator Dispatcher Lambda

### Requirement 1: SQS event source and message deserialization

**User Story:** As the platform, I want the Orchestrator_Handler to consume `trikon-verify-jobs` one message at a time and parse each body into a typed `SqsJobMessage`, so that downstream code operates on a validated Pydantic model rather than a raw dictionary.

#### Acceptance Criteria

1. THE Orchestrator_Handler SHALL be configured as an SQS event source mapping on the queue `trikon-verify-jobs` with batch size 1 and maximum batching window 0 seconds.
2. WHEN the Orchestrator_Handler receives an SQS event, THE Orchestrator_Handler SHALL parse the message body as JSON and validate the result against `SqsJobMessage` (from `trikon_cloud/webhook_receiver/models.py`).
3. THE Orchestrator_Handler SHALL propagate the `delivery_id` field from `SqsJobMessage` unchanged through every downstream operation (log field, `ecs.RunTask` env override, DLQ metadata).
4. THE Orchestrator_Handler SHALL NOT introduce a separate identifier named `audit_id` at the orchestrator layer; `delivery_id` is the single correlation key at this layer.

---

### Requirement 2: Defensive payload-size and validation gate

**User Story:** As the platform, I want oversized or malformed SQS messages to fail closed at the orchestrator boundary, so that no invalid job reaches `ecs.RunTask`.

#### Acceptance Criteria

1. IF the raw SQS message body exceeds 262144 bytes (256 KB), THEN THE Orchestrator_Handler SHALL classify the message as a Terminal error and route the message to the `trikon-verify-jobs` DLQ with a structured log record at ERROR level containing `reason="payload_exceeds_256kb"`, the byte count, and `delivery_id` if extractable.
2. IF `SqsJobMessage` validation raises `pydantic.ValidationError`, THEN THE Orchestrator_Handler SHALL classify the message as a Terminal error and route the message to the `trikon-verify-jobs` DLQ with a structured log record at ERROR level containing `reason="malformed_sqs_body"` and the Pydantic error location list.
3. THE Orchestrator_Handler SHALL NOT emit a synthetic `allow` or `require_human` verdict from within the payload-gate branch; verdict emission on terminal failure is governed by Requirement 8.

---

### Requirement 3: `ecs.RunTask` call shape per §5.3

**User Story:** As the platform, I want the Orchestrator_Handler to emit exactly the `ecs.RunTask` call shape defined in architecture memo §5.3, so that Spec 2's Fargate runner starts with the correct cluster, network, tags, and task-role binding.

#### Acceptance Criteria

1. WHEN the Orchestrator_Handler dispatches a validated `SqsJobMessage`, THE Orchestrator_Handler SHALL issue exactly one `ecs.RunTask` API call per received SQS message (no retries at the Lambda-code layer beyond the SQS redrive policy defined in Requirement 7).
2. THE Orchestrator_Handler SHALL populate `cluster="trikon-verify-cluster"`, `launchType="FARGATE"`, and `count=1` on every `ecs.RunTask` call.
3. THE Orchestrator_Handler SHALL populate `networkConfiguration.awsvpcConfiguration` with the subnet IDs, security group IDs, and `assignPublicIp="DISABLED"` supplied by the environment variables `TRIKON_RUNNER_SUBNET_IDS` and `TRIKON_RUNNER_SECURITY_GROUP_IDS`.
4. THE Orchestrator_Handler SHALL populate `overrides.taskRoleArn` with the value `arn:aws:iam::{account_id}:role/trikon-verify-task-role-{installation_id}` where `{account_id}` is read once at cold start from the environment variable `TRIKON_AWS_ACCOUNT_ID` and `{installation_id}` is the value from the current `SqsJobMessage`.
5. THE Orchestrator_Handler SHALL populate the `tags` array on every `ecs.RunTask` call with exactly three entries: `{key: "installation_id", value: str(installation_id)}`, `{key: "repo", value: repo_full_name}`, and `{key: "pr", value: str(pr_number)}`.

---

### Requirement 4: Task-definition active-revision pinning [OPEN ITEM b]

**User Story:** As the platform, I want the Orchestrator_Handler to pin an active, immutable task-definition revision at each `ecs.RunTask` call, so that a mid-flight task-definition update does not silently change runner behavior for in-flight jobs.

#### Acceptance Criteria

1. THE Orchestrator_Handler SHALL populate `taskDefinition` on every `ecs.RunTask` call with a fully qualified `family:revision` string (for example `trikon-verify-runner:17`).
2. THE Orchestrator_Handler SHALL NOT populate `taskDefinition` with the family name alone, with `:LATEST`, or with any value that defers revision resolution to ECS.
3. THE Orchestrator_Handler SHALL resolve the active revision integer once per Lambda cold start and cache the resolved value in module scope for the container lifetime.
4. **Design open item:** the mechanism by which the Orchestrator_Handler obtains the active revision at cold start (candidates: `ecs:DescribeTaskDefinition` on the family name, or a CDK-written SSM parameter such as `/trikon/verify-runner/active-revision`) SHALL be selected in the design phase. Requirements phase locks the outcome (a fully qualified revision at `ecs.RunTask` time), not the mechanism.

---

### Requirement 5: `containerOverrides.environment` must carry the seven `TRIKON_*` core fields

**User Story:** As the Fargate runner (Spec 2), I want the seven env vars my `RunnerEnvConfig` core-field aliases expect, so that my `pydantic.BaseSettings` validation succeeds at process start.

#### Acceptance Criteria

1. THE Orchestrator_Handler SHALL populate `overrides.containerOverrides` with exactly one entry whose `name` field equals `"runner"`.
2. THE Orchestrator_Handler SHALL populate `overrides.containerOverrides[0].environment` with exactly seven entries, one per RunnerEnvConfig core field: `TRIKON_INSTALLATION_ID`, `TRIKON_REPO_FULL_NAME`, `TRIKON_PR_NUMBER`, `TRIKON_HEAD_SHA`, `TRIKON_BASE_SHA`, `TRIKON_EVENT_TYPE`, `TRIKON_DELIVERY_ID`.
3. THE Orchestrator_Handler SHALL serialize the two integer fields (`installation_id`, `pr_number`) as their base-10 string form when populating the corresponding `TRIKON_INSTALLATION_ID` and `TRIKON_PR_NUMBER` environment entries.
4. THE Orchestrator_Handler SHALL copy the `head_sha`, `base_sha`, `repo_full_name`, `event_type`, and `delivery_id` string fields byte-for-byte from `SqsJobMessage` to the corresponding environment entries without normalization, truncation, or case conversion.
5. THE Orchestrator_Handler SHALL NOT populate `overrides.containerOverrides[0].environment` with any additional entry beyond the seven fields enumerated in criterion 2.

---

### Requirement 6: Transient vs Terminal error classification

**User Story:** As the platform operator, I want the Orchestrator_Handler to distinguish transient AWS-API errors from terminal configuration errors, so that transient failures retry via the SQS redrive policy while terminal failures land in the DLQ on first occurrence.

#### Acceptance Criteria

1. WHEN a Transient error occurs during the `ecs.RunTask` call, THE Orchestrator_Handler SHALL raise the exception out of the Lambda handler so that AWS Lambda returns the message to the source queue for redrive.
2. WHEN a Terminal error occurs during the `ecs.RunTask` call, THE Orchestrator_Handler SHALL emit a structured ERROR-level log record containing the AWS error code, the `delivery_id`, the `installation_id`, and the field `error_class="terminal"`, then delete the SQS message via the batch-item-failure protocol so that the message does NOT return to the source queue for redrive.
3. THE `trikon-verify-jobs` queue's redrive policy SHALL be configured with `maxReceiveCount=3` so that a Transient error causing three consecutive failures moves the message to the DLQ.
4. THE Orchestrator_Handler SHALL classify each `boto3.exceptions.ClientError` by its `error.response["Error"]["Code"]` value against the Transient error / Terminal error membership lists in the Glossary; any error code not present in either list SHALL be treated as Transient (fail-safe default).

---

### Requirement 7: Never-Fail-Open on terminal orchestrator failure [OPEN ITEM a]

**User Story:** As a Trikon Cloud customer, I want every SQS-triggered verification to end with a customer-visible verdict on my PR — even when the orchestrator itself fails terminally — so that the platform never silently drops a job (Invariant 2).

#### Acceptance Criteria

1. WHEN a Terminal error occurs on a valid `SqsJobMessage` (that is, the message parsed successfully but `ecs.RunTask` failed terminally), THE Orchestrator_Handler SHALL ensure exactly one row is written to `trikon_verdicts` for the natural key `(installation_id, repo_full_name, pr_number, head_sha)` with `decision="require_human"` and `matched_rule="orchestrator terminal failure"`.
2. WHEN a Terminal error occurs, THE Orchestrator_Handler SHALL ensure a corresponding GitHub Check Run with `conclusion="neutral"` is created on the PR's `head_sha`, per Invariant 2's customer-visible-verdict requirement.
3. THE requirement in criterion 1 SHALL be satisfied without violating Invariant 1 (tenant isolation) — any IAM grant used by the Orchestrator_Handler SHALL be scoped to the `installation_id` from the current `SqsJobMessage`.
4. THE requirement in criterion 1 SHALL be satisfied without violating Invariant 3 (idempotency) — the write SHALL use the same `attribute_not_exists(installation_id) AND attribute_not_exists(sk)` ConditionExpression that Spec 2's Fargate runner uses, and a conditional-check-failure SHALL be treated as success (the row already exists).
5. **Design open item:** the mechanism by which criterion 1 and criterion 2 are satisfied — candidates: (i) Orchestrator_Handler writes the row and creates the Check Run directly with narrow IAM grants, or (ii) Orchestrator_Handler routes the failing message to a synthetic-verdict path via Spec 2's Fargate runner with a `TRIKON_SYNTHETIC_REQUIRE_HUMAN=true` env flag — SHALL be selected in the design phase. Requirements phase locks the outcome (customer-visible `require_human` verdict), not the mechanism.

---

### Requirement 8: Orchestrator Lambda runtime configuration and IAM policy

**User Story:** As the platform operator, I want the Orchestrator_Handler's runtime resources and IAM permissions bounded to what the dispatcher actually needs, so that a compromised orchestrator has minimum blast radius (Invariant 1) and a hung dispatcher does not accumulate cost (Invariant 5).

#### Acceptance Criteria

1. THE Orchestrator_Handler SHALL be deployed with runtime `python3.11`, memory 512 MB, timeout 30 seconds, architecture `arm64`, and reserved concurrency 10.
2. THE Orchestrator_Handler's execution role SHALL grant `ecs:RunTask` scoped to the resource `arn:aws:ecs:us-east-1:{account_id}:task-definition/trikon-verify-runner:*`.
3. THE Orchestrator_Handler's execution role SHALL grant `iam:PassRole` scoped to the resource pattern `arn:aws:iam::{account_id}:role/trikon-verify-task-role-*` with a `PassedToService` condition equal to `ecs-tasks.amazonaws.com`.
4. THE Orchestrator_Handler's execution role SHALL grant `sqs:ReceiveMessage`, `sqs:DeleteMessage`, and `sqs:GetQueueAttributes` scoped to the `trikon-verify-jobs` queue ARN.
5. THE Orchestrator_Handler's execution role SHALL grant `sqs:SendMessage` scoped to the `trikon-verify-jobs-dlq` queue ARN.
6. THE Orchestrator_Handler's execution role SHALL NOT grant any additional IAM action beyond the actions listed in criteria 2 through 5 plus any additional grants required by the design-phase resolution of Open Item (a).

---

### Requirement 9: Structured logging via `aws-lambda-powertools`

**User Story:** As the platform operator, I want every Orchestrator_Handler log record to carry a fixed field set including `installation_id`, `delivery_id`, and `repo_full_name`, so that log queries by natural key succeed without cross-referencing multiple record shapes.

#### Acceptance Criteria

1. THE Orchestrator_Handler SHALL use `aws_lambda_powertools.Logger` with `service="trikon-cloud-orchestrator"` for every log emission.
2. WHEN the Orchestrator_Handler successfully parses `SqsJobMessage`, THE Orchestrator_Handler SHALL append `installation_id`, `repo_full_name`, `pr_number`, `delivery_id`, and `event_type` to the Powertools log context via `logger.append_keys(...)` so that every subsequent log record within the same invocation carries the Structured log field set.
3. THE Orchestrator_Handler SHALL NOT log the raw `SqsJobMessage` body, the `ecs.RunTask` response body, or any AWS credential material at any log level, per Invariant 6.
4. THE Orchestrator_Handler SHALL emit exactly one INFO-level record per successful dispatch containing `event="run_task_dispatched"` and the ECS task ARN returned by `ecs.RunTask`.

---

## Section B — Installation-Lifecycle Handler Lambda

### Requirement 10: SQS event source for `trikon-cloud-installation-events`

**User Story:** As the platform, I want the Lifecycle_Handler to consume installation-lifecycle events from a dedicated queue separate from the verify-jobs queue, so that installation churn does not compete with verification throughput and so that lifecycle failures have independent DLQ semantics.

#### Acceptance Criteria

1. THE Lifecycle_Handler SHALL be configured as an SQS event source mapping on the queue `trikon-cloud-installation-events` with batch size 1 and maximum batching window 0 seconds.
2. THE `trikon-cloud-installation-events` queue's redrive policy SHALL be configured with `maxReceiveCount=3` and a DLQ named `trikon-cloud-installation-events-dlq`.
3. THE `trikon-cloud-installation-events` queue and its DLQ SHALL be created by this spec's CDK stack in region `us-east-1`.
4. THE Spec 1 webhook-receiver amendment (adding a routing branch for `installation` and `installation_repositories` events plus an `sqs:SendMessage` IAM grant on `trikon-cloud-installation-events`) SHALL be flagged as an external dependency of this spec and SHALL NOT be an implementation task of this spec.

---

### Requirement 11: Typed message model for installation events

**User Story:** As the Lifecycle_Handler, I want every inbound SQS message to deserialize into a strict Pydantic model with all fields required, so that runtime code operates on validated data (Invariant 8) and no `dict[str, Any]` appears on the module's public API.

#### Acceptance Criteria

1. THE `InstallationEventMessage` model SHALL be defined at `trikon_cloud/installation_lifecycle/models.py` as a `pydantic.BaseModel` subclass with `model_config = ConfigDict(extra="forbid", frozen=True)`.
2. THE `InstallationEventMessage` model SHALL declare the fields `installation_id: int` (with `Field(ge=1)`), `github_app_id: int` (with `Field(ge=1)`), `event_type: Literal["installation.created", "installation.deleted", "installation_repositories.added", "installation_repositories.removed"]`, `repositories: tuple[str, ...]`, `sent_at: str`, and `delivery_id: str` — all required, no default values, no `extra="allow"`.
3. IF an inbound SQS message body fails to validate against `InstallationEventMessage`, THEN THE Lifecycle_Handler SHALL emit a structured ERROR-level log with `reason="malformed_installation_event"` and route the message to `trikon-cloud-installation-events-dlq`.
4. THE `InstallationEventMessage` module SHALL pass `uv run mypy --strict` per Invariant 8, and its public API SHALL NOT declare any field typed as `dict[str, Any]`, `list[Any]`, or `object`.

---

### Requirement 12: Event-type routing for installation lifecycle

**User Story:** As the platform, I want each of the four installation-lifecycle event types routed to a distinct handler function, so that provisioning, deprovisioning, and repository-set changes each have a single owner.

#### Acceptance Criteria

1. WHEN the Lifecycle_Handler receives an `InstallationEventMessage` with `event_type="installation.created"`, THE Lifecycle_Handler SHALL invoke `handle_installation_created(message)` which provisions the per-installation IAM task role per Requirement 13 and inserts a Trikon Cloud Installations Table row with `status="active"`.
2. WHEN the Lifecycle_Handler receives an `InstallationEventMessage` with `event_type="installation.deleted"`, THE Lifecycle_Handler SHALL invoke `handle_installation_deleted(message)` which deletes the per-installation IAM task role via `iam:DeleteRolePolicy` followed by `iam:DeleteRole` and updates the Trikon Cloud Installations Table row to `status="disabled"`.
3. WHEN the Lifecycle_Handler receives an `InstallationEventMessage` with `event_type="installation_repositories.added"`, THE Lifecycle_Handler SHALL invoke `handle_repositories_added(message)` which appends the entries of `message.repositories` to the Trikon Cloud Installations Table row's `repositories` set via a `dynamodb:UpdateItem` with an `ADD` action.
4. WHEN the Lifecycle_Handler receives an `InstallationEventMessage` with `event_type="installation_repositories.removed"`, THE Lifecycle_Handler SHALL invoke `handle_repositories_removed(message)` which removes the entries of `message.repositories` from the Trikon Cloud Installations Table row's `repositories` set via a `dynamodb:UpdateItem` with a `DELETE` action.

---

### Requirement 13: Runtime IAM role provisioning with byte-matched policy

**User Story:** As the security auditor, I want the runtime IAM policy the Lifecycle_Handler installs on each per-installation task role to be byte-identical to the CDK policy Spec 2 emits, so that the runtime path and the infrastructure-as-code path can never drift.

#### Acceptance Criteria

1. THE Runtime_IAM_Renderer SHALL be defined at `trikon_cloud/installation_lifecycle/iam_template.py` as a pure function with signature `render_installation_policy_document(installation_id: int, *, app_private_key_secret_arn: str, account_id: str, region: str) -> InstallationPolicyDocument`.
2. THE `InstallationPolicyDocument` return type SHALL be a Pydantic model (or a nested tree of Pydantic models) that serializes to a JSON object with the top-level shape `{"Version": "2012-10-17", "Statement": [...]}` and SHALL NOT be typed as `dict[str, Any]` on the function signature.
3. THE `InstallationPolicyDocument` returned by the Runtime_IAM_Renderer SHALL contain exactly four `Statement` entries corresponding to the four `add_to_policy` calls in `FargateRunnerStack.build_task_role_for_installation()`: `dynamodb:PutItem` on `trikon_verdicts` with a `LeadingKeys` condition; `dynamodb:GetItem`/`PutItem`/`UpdateItem` on `trikon_pr_state` with a `LeadingKeys` condition; `s3:PutObject`/`GetObject` on `arn:aws:s3:::trikon-cloud-evidence/{installation_id}/*`; and `secretsmanager:GetSecretValue` on the App private key ARN.
4. A contract test at `tests/test_iam_policy_contract.py` in this spec's test tree SHALL synthesize the CDK `FargateRunnerStack.build_task_role_for_installation(installation_id=12345678, app_private_key_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef")` output, invoke the Runtime_IAM_Renderer with identical inputs, and assert byte-equivalence after canonical JSON serialization (`json.dumps(..., sort_keys=True, separators=(",", ":"))`).
5. WHEN the Lifecycle_Handler executes `handle_installation_created(message)`, THE Lifecycle_Handler SHALL invoke `iam:CreateRole` with `RoleName=f"trikon-verify-task-role-{installation_id}"` and an assume-role policy allowing the `ecs-tasks.amazonaws.com` service principal, then `iam:PutRolePolicy` with the JSON output of the Runtime_IAM_Renderer.

---

### Requirement 14: `trikon-cloud-installations` table and idempotent re-delivery

**User Story:** As the platform, I want the Lifecycle_Handler to succeed silently on re-delivery of a lifecycle event, so that a duplicate SQS message never fails a lifecycle transition (Invariant 3).

#### Acceptance Criteria

1. THE Trikon Cloud Installations Table SHALL be created by this spec's CDK stack with billing mode `PAY_PER_REQUEST`, point-in-time recovery enabled, and the schema declared in the Glossary.
2. WHEN `handle_installation_created` observes that an IAM role named `trikon-verify-task-role-{installation_id}` already exists (`iam:CreateRole` returns `EntityAlreadyExistsException`), THE Lifecycle_Handler SHALL log an INFO-level record with `event="installation_already_provisioned"` and continue to the DynamoDB write step without raising.
3. WHEN `handle_installation_created` observes that the Trikon Cloud Installations Table row for the given `installation_id` already exists with `status="active"`, THE Lifecycle_Handler SHALL log an INFO-level record with `event="installation_already_active"` and treat the message as successfully processed.
4. WHEN `handle_installation_deleted` observes that either the IAM role or the DynamoDB row is already absent, THE Lifecycle_Handler SHALL treat the message as successfully processed without raising.
5. THE Lifecycle_Handler SHALL NOT rely on any at-most-once dedup token; idempotency at this layer SHALL be derived from the natural key `installation_id` combined with the AWS-API conditional-write and `EntityAlreadyExistsException` handling described in criteria 2 through 4.

---

### Requirement 15: Lifecycle Lambda runtime, logging, and Never-Fail-Open

**User Story:** As the platform operator, I want the Lifecycle_Handler's runtime bounds, structured logging, and Never-Fail-Open behavior to match the Orchestrator_Handler's, so that both Lambdas share one operational model.

#### Acceptance Criteria

1. THE Lifecycle_Handler SHALL be deployed with runtime `python3.11`, memory 512 MB, timeout 30 seconds, architecture `arm64`, and reserved concurrency 5.
2. THE Lifecycle_Handler SHALL use `aws_lambda_powertools.Logger` with `service="trikon-cloud-installation-lifecycle"` for every log emission and SHALL append `installation_id`, `event_type`, and `delivery_id` to the log context after successful `InstallationEventMessage` parse.
3. IF the Lifecycle_Handler encounters a Terminal error on a valid `InstallationEventMessage` for `event_type="installation.created"` (for example, `iam:CreateRole` returns `AccessDenied` or `LimitExceeded`), THEN THE Lifecycle_Handler SHALL emit a structured ERROR-level log with `error_class="terminal"` and SHALL fail the message so it moves to `trikon-cloud-installation-events-dlq` on `maxReceiveCount` exhaustion — a failed provision SHALL NOT leave the platform in a half-configured state where a subsequent verify job would attempt `ecs.RunTask` with a non-existent task role.
4. THE Lifecycle_Handler's execution role SHALL grant exactly: `iam:CreateRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:DeleteRole`, and `iam:GetRole` scoped to the resource pattern `arn:aws:iam::{account_id}:role/trikon-verify-task-role-*`; `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem`, and `dynamodb:DeleteItem` scoped to the Trikon Cloud Installations Table ARN; `sqs:ReceiveMessage`, `sqs:DeleteMessage`, and `sqs:GetQueueAttributes` scoped to the `trikon-cloud-installation-events` queue ARN; and `sqs:SendMessage` scoped to the `trikon-cloud-installation-events-dlq` queue ARN.
5. THE Lifecycle_Handler's execution role SHALL NOT grant `iam:PassRole` on `trikon-verify-task-role-*`, because the Lifecycle_Handler creates the roles but never assumes or passes them.

---

## Section C — Cross-cutting

### Requirement 16: Region, runtime, and Powertools dependency

**User Story:** As the platform operator, I want both Lambdas pinned to `us-east-1`, Python 3.11, and a bounded major version of `aws-lambda-powertools`, so that operational surface stays uniform across Spec 1, Spec 2, and Spec 3.

#### Acceptance Criteria

1. THE Orchestrator_Handler and THE Lifecycle_Handler SHALL be deployed only in the AWS region `us-east-1` — no multi-region deployment is in scope for this spec.
2. THE Orchestrator_Handler and THE Lifecycle_Handler SHALL declare a runtime dependency on `aws-lambda-powertools[all]>=3,<4` in the shared `pyproject.toml` under a dependency group named `cloud-runtime`.
3. THE Orchestrator_Handler and THE Lifecycle_Handler SHALL run on the AWS Lambda `python3.11` managed runtime; no custom runtime layer is in scope for this spec.
4. THE deployment package for each Lambda SHALL exclude test files, development-only dependencies, and CDK synth output.

---

### Requirement 17: Type safety per Invariant 8

**User Story:** As a downstream module author, I want every public function this spec exports to carry a typed signature that `mypy --strict` accepts, so that Invariant 8 holds across the module boundary.

#### Acceptance Criteria

1. THE `trikon_cloud/orchestrator/` package and THE `trikon_cloud/installation_lifecycle/` package SHALL each pass `uv run mypy --strict` on the package's `src` tree with zero errors.
2. THE public API surface of each package SHALL NOT declare `dict[str, Any]`, `list[Any]`, `tuple[Any, ...]`, or `object` on any parameter or return type — public API means every symbol exported through `__all__` or reachable without a leading underscore.
3. THE `RunTaskCall` intermediate model — a Pydantic model this spec defines to represent the `ecs.RunTask` request payload before it is serialized for `boto3` — SHALL be declared with `model_config = ConfigDict(frozen=True, extra="forbid")` and SHALL type every field per the shape in Requirement 3.
4. THE `InstallationPolicyDocument` model and any nested `PolicyStatement` / `PolicyCondition` models SHALL be declared with `model_config = ConfigDict(frozen=True, extra="forbid")` and SHALL serialize via `.model_dump(mode="json", by_alias=True)` to the exact JSON shape that IAM expects.

---

### Requirement 18: Product-name discipline per Invariant 7

**User Story:** As the Trikon brand steward, I want every identifier, log field, and inline comment this spec introduces to render the product name as `Trikon` (or `Trikon Cloud` where appropriate), so that no artifact of a prior product name survives in this spec's surface (Invariant 7).

#### Acceptance Criteria

1. THE module names, class names, function names, and CloudFormation logical IDs this spec introduces SHALL render the product name as either `Trikon` or `trikon` (case-appropriate for the identifier convention).
2. THE Powertools `service` field on both Lambdas SHALL contain the literal string `trikon-cloud` as a prefix.
3. THE inline comments and docstrings in the files this spec authors SHALL NOT contain any prior product name that Invariant 7 forbids.
4. THE Lambda function names, IAM role names, SQS queue names, and DynamoDB table names this spec introduces SHALL each begin with the literal prefix `trikon-` (kebab-case) or `trikon_` (snake_case) as appropriate for the AWS resource-name convention.
