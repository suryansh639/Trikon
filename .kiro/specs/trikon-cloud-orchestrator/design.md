# Design Document

## §1 Overview

Trikon Cloud Spec 3 is the cloud orchestrator layer. Two AWS Lambda functions, in one CDK stack, own the boundary between Spec 1 (webhook receiver) and Spec 2 (Fargate runner):

- **Orchestrator_Handler** at `trikon_cloud/orchestrator/handler.py` — SQS-triggered on `trikon-verify-jobs`. One SQS message → one `ecs.RunTask` call per architecture memo §5.3. Owns Never-Fail-Open on terminal dispatch failure.
- **Lifecycle_Handler** at `trikon_cloud/installation_lifecycle/handler.py` — SQS-triggered on the new `trikon-cloud-installation-events` queue. Owns per-installation IAM task-role provisioning + deprovisioning + the `trikon-cloud-installations` DynamoDB table.

The two Lambdas share a package layout under `trikon_cloud/` and one CDK stack (`OrchestratorStack`), but their execution roles are distinct, their SQS event sources are distinct, and they are independent failure domains — a lifecycle backlog cannot stall verification throughput, and a dispatcher outage cannot corrupt the installation table.

### §1.1 Memo §5.3 sequence — reproduced at the orchestrator boundary

```
GitHub Webhook Event                                     (external)
        │
        ▼
Webhook_Receiver (Spec 1) ─► trikon-verify-jobs (SQS)
                              │
                              │ batch=1, MaximumBatchingWindowInSeconds=0
                              ▼
                    Orchestrator_Handler (this spec)
                              │
                              │  parse SqsJobMessage
                              │  resolve task-def revision from SSM
                              │  build RunTaskCall
                              │
                              ▼
                    ecs:RunTask ────► Fargate task (Spec 2 image)
                              │
                              │  on Terminal error:
                              │  ┌─────────────────────────────────┐
                              └─►│ Never-Fail-Open path             │
                                 │   trikon_verdicts.PutItem        │
                                 │   POST /check-runs (neutral)     │
                                 └─────────────────────────────────┘

GitHub Webhook Event (installation, installation_repositories)
        │
        ▼
Webhook_Receiver (Spec 1, amended) ─► trikon-cloud-installation-events (SQS)
                              │
                              ▼
                    Lifecycle_Handler (this spec)
                              │
                              ▼
                    iam:CreateRole / DeleteRole  +  dynamodb (trikon-cloud-installations)
```

### §1.2 The eight invariants — where each is enforced in this spec

Invariants are defined at `.kiro/specs/trikon-cloud-architecture/requirements.md`.

| # | Invariant                | Enforcement site in this spec                                                                                                                                                                                                                                                                                                              |
|---|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Tenant isolation         | (a) `overrides.taskRoleArn = arn:aws:iam::{account}:role/trikon-verify-task-role-{installation_id}` in `ecs_dispatcher.py`; (b) `render_installation_policy_document` in `iam_template.py` uses `dynamodb:LeadingKeys=["${aws:PrincipalTag/installation_id}"]` on every DynamoDB action; (c) the orchestrator's own Never-Fail-Open `dynamodb:PutItem` grant carries a `LeadingKeys` condition matching the incoming `installation_id`. |
| 2 | Never-Fail-Open          | `never_fail_open.py::write_orchestrator_failure_verdict` writes a synthetic `require_human` verdict + neutral Check Run whenever `ecs:RunTask` fails Terminally on a validly parsed `SqsJobMessage`. Payload-gate branch (oversized or malformed) is out-of-scope for the synthetic write — the message is DLQ'd for human triage per Req 2.3. |
| 3 | Idempotency (natural key)| (a) Never-Fail-Open writes use `ConditionExpression=attribute_not_exists(installation_id) AND attribute_not_exists(sk)` — identical to Spec 2's writer; a `ConditionalCheckFailedException` is a success path. (b) Lifecycle `handle_installation_created` treats `EntityAlreadyExistsException` on `iam:CreateRole` and an already-active table row as success. (c) Lifecycle `handle_installation_deleted` treats a missing role or missing row as success. |
| 4 | SDK boundary shape       | Not applicable — this spec never imports `trikon.sdk`. `RunnerEnvConfig` (Spec 2's env-var contract) is treated as an opaque set of seven aliased keys; the orchestrator writes them into `overrides.containerOverrides[0].environment` verbatim. |
| 5 | Cost-per-verdict ceiling | (a) Lambda memory 512 MB, timeout 30 s, reserved concurrency 10 (orchestrator) / 5 (lifecycle). (b) One `ecs.RunTask` per SQS message — no Lambda-level retry loop (Req 3.1). (c) `maxReceiveCount=3` on both queues caps the retry budget. |
| 6 | Secrets handling         | (a) Raw SQS body, `ecs.RunTask` response, and any Secrets Manager value never emitted to logs (`Logger` guard in `logger.py::LOGGING_DENYLIST` + Powertools `service` context). (b) App private key ARN is read from env; the resolved PEM bytes stay in-process and are dropped when the token is cached. |
| 7 | Product name             | All module names, class names, log `service` fields, SQS queue names, DynamoDB table names, and IAM role name templates use the literal string `trikon` (lower) or `Trikon` (title). No prior product name appears. |
| 8 | Type safety              | Every public function on both packages carries a non-`Any` signature. `RunTaskCall`, `InstallationEventMessage`, `InstallationPolicyDocument`, `PolicyStatement`, `PolicyCondition`, `LifecycleTableRow`, and `RunnerContainerOverride` are Pydantic v2 `BaseModel` subclasses with `model_config = ConfigDict(frozen=True, extra="forbid")`. `uv run mypy --strict` runs green on both packages. |

### §1.3 Open items — resolved

- **Open Item (a) — Never-Fail-Open mechanism: (i) Orchestrator writes verdict + Check Run directly.** Simpler failure mode: one Lambda invocation writes the row and posts the Check Run under its own 30-second timeout. Avoids launching a Fargate task on the very path where we are least sure Fargate itself works, so it cannot cost-amplify a dispatcher outage. The IAM cost is narrow: `dynamodb:PutItem` on `trikon_verdicts` with a `LeadingKeys` condition matching the incoming `installation_id`, plus `secretsmanager:GetSecretValue` on the App private-key secret ARN, plus a networked outbound HTTPS call to `api.github.com`. The Fargate runner (Spec 2) is unchanged — no synthetic-verdict branch is introduced in its image. The orchestrator execution role grows from ~5 grants to ~8; acceptable.

- **Open Item (b) — Task-definition revision resolution: (ii) SSM parameter, read at cold start.** Explicit cross-spec dependency lives in CDK code, not hidden behind a runtime AWS-API call. `ssm:GetParameter` at ~5 ms is faster at cold start than `ecs:DescribeTaskDefinition` at ~50 ms. Rollback is atomic — rolling back Spec 2's CDK stack rolls back the SSM value; the next orchestrator cold start sees the reverted revision. The SSM path also removes the need to grant `ecs:DescribeTaskDefinition` on the runner family; the orchestrator only needs `ecs:RunTask` on the resolved `taskDefinition` ARN pattern. The coordinated change to Spec 2 is small — a single `ssm.StringParameter` construct that writes `/trikon/verify-runner/active-revision` on every task-definition update. It is flagged in §9.

---

## §2 Module boundaries

### §2.1 Package layout

```
trikon_cloud/
├── __init__.py                                     (existing, unchanged)
│
├── orchestrator/                                   (new — this spec)
│   ├── __init__.py
│   ├── handler.py                                  Lambda entrypoint + 8-step flow
│   ├── models.py                                   RunTaskCall + intermediate DTOs
│   ├── ecs_dispatcher.py                           Pure builder: SqsJobMessage → RunTaskCall
│   ├── never_fail_open.py                          Synthetic-verdict + neutral Check Run
│   ├── logger.py                                   Powertools Logger wrapper
│   └── infra/
│       ├── __init__.py
│       ├── orchestrator_stack.py                   One CDK stack, both Lambdas
│       └── app.py                                  CDK entrypoint (cdk synth)
│
├── installation_lifecycle/                         (new — this spec)
│   ├── __init__.py
│   ├── handler.py                                  Lambda entrypoint + 5-step flow
│   ├── models.py                                   InstallationEventMessage + row shape
│   ├── iam_template.py                             Runtime_IAM_Renderer (pure)
│   ├── iam_provisioner.py                          Wraps iam:CreateRole / PutRolePolicy / DeleteRole
│   ├── dynamodb_writer.py                          Wraps PutItem / UpdateItem on installations table
│   └── logger.py                                   Powertools Logger wrapper
│
└── webhook_receiver/                               (Spec 1, unchanged except for §9)
    └── models.py                                   Exports SqsJobMessage
```

The two packages share nothing at runtime — no cross-imports. They share only the `SqsJobMessage` symbol (orchestrator imports from webhook_receiver's public API) and Spec 2's `VerdictRow` symbol (orchestrator's `never_fail_open.py` imports from `trikon_cloud.fargate_runner.models`). Both cross-package imports are stable, typed, frozen models — safe by construction.

### §2.2 Public API surface

Every symbol below appears in the module's `__all__`. All parameter and return types are explicit; no `dict[str, Any]`, no `list[Any]`, no `object` in any signature (Invariant 8).

#### `trikon_cloud.orchestrator.handler`

| Symbol                                | Signature                                                              |
|---------------------------------------|------------------------------------------------------------------------|
| `lambda_handler`                      | `(event: SqsEventEnvelope, context: LambdaContext) -> SqsBatchResponse` |

`SqsEventEnvelope` and `SqsBatchResponse` are Pydantic models declared in `orchestrator.models`; they wrap Powertools' `SQSEvent` and `SQSBatchResponse` respectively — see §3.4.

#### `trikon_cloud.orchestrator.models`

| Symbol                        | Kind                | Notes                                                                                            |
|-------------------------------|---------------------|--------------------------------------------------------------------------------------------------|
| `SqsEventEnvelope`            | `BaseModel`         | `Records: tuple[SqsRecord, ...]`; `extra="forbid"`; frozen.                                      |
| `SqsRecord`                   | `BaseModel`         | `messageId: str`; `receiptHandle: str`; `body: str`; `attributes: SqsRecordAttributes`; frozen.  |
| `SqsRecordAttributes`         | `BaseModel`         | `ApproximateReceiveCount: str`; `extra="allow"`; frozen. AWS may add fields.                     |
| `SqsBatchResponse`            | `BaseModel`         | `batchItemFailures: tuple[BatchItemFailure, ...]`; frozen.                                       |
| `BatchItemFailure`            | `BaseModel`         | `itemIdentifier: str`; frozen.                                                                   |
| `RunTaskCall`                 | `BaseModel`         | Full `ecs.RunTask` request body — see §3.3. `extra="forbid"`; frozen.                            |
| `NetworkConfiguration`        | `BaseModel`         | Wraps `awsvpcConfiguration`; frozen.                                                             |
| `AwsvpcConfiguration`         | `BaseModel`         | `subnets: tuple[str, ...]`; `securityGroups: tuple[str, ...]`; `assignPublicIp: Literal["DISABLED"]`. |
| `RunTaskOverrides`            | `BaseModel`         | `taskRoleArn: str`; `containerOverrides: tuple[ContainerOverride, ...]`; frozen.                 |
| `ContainerOverride`           | `BaseModel`         | `name: Literal["runner"]`; `environment: tuple[EnvOverride, ...]`; frozen.                       |
| `EnvOverride`                 | `BaseModel`         | `name: str`; `value: str`; frozen.                                                               |
| `EcsTag`                      | `BaseModel`         | `key: str`; `value: str`; frozen.                                                                |
| `OrchestratorEnvConfig`       | `BaseSettings`      | Env-var loader. Fields listed in §2.3.                                                           |

#### `trikon_cloud.orchestrator.ecs_dispatcher`

| Symbol                        | Signature                                                                                      |
|-------------------------------|-------------------------------------------------------------------------------------------------|
| `build_run_task_call`         | `(message: SqsJobMessage, *, env: OrchestratorEnvConfig, task_definition: str) -> RunTaskCall` |
| `submit_run_task`             | `(call: RunTaskCall, *, ecs_client: EcsClientProtocol) -> RunTaskDispatchResult`               |
| `RunTaskDispatchResult`       | `BaseModel` with `task_arn: str`; `extra="forbid"`; frozen.                                    |
| `EcsClientProtocol`           | `typing.Protocol` with `run_task(**kwargs: object) -> RunTaskResponse` (Pydantic model).       |
| `RunTaskResponse`             | `BaseModel` wrapping the boto3 response, `extra="allow"`, frozen.                              |
| `TaskDefinitionResolver`      | `class` with `resolve(*, ssm_client: SsmClientProtocol) -> str`; caches value in `_cache`.     |
| `SsmClientProtocol`           | `typing.Protocol` with `get_parameter(*, Name: str) -> SsmGetParameterResponse`.               |
| `SsmGetParameterResponse`     | `BaseModel` with nested `Parameter` (`Value: str`); frozen.                                    |
| `TransientDispatchError`      | `Exception` subclass — re-raised out of the handler so SQS returns the message.                |
| `TerminalDispatchError`       | `Exception` subclass — caught by the handler; triggers Never-Fail-Open.                        |
| `classify_client_error`       | `(err: ClientError) -> Literal["transient", "terminal"]` — pure classifier per Req 6.4.        |

#### `trikon_cloud.orchestrator.never_fail_open`

| Symbol                                | Signature                                                                                                                                                                                                                                                              |
|---------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `write_orchestrator_failure_verdict`  | `(*, sqs_message: SqsJobMessage, error_class: str, error_code: str, boto3_session: boto3.session.Session, github_client: OrchestratorGithubClient) -> None`                                                                                                              |
| `OrchestratorGithubClient`            | `class` with `create_neutral_check_run(*, installation_id: int, repo_full_name: str, head_sha: str, details_url: str) -> None`. Wraps `httpx.Client`. Mints installation token via cached App JWT.                                                                        |
| `OrchestratorGithubClientError`       | `Exception` subclass.                                                                                                                                                                                                                                                    |
| `build_synthetic_verdict_row`         | `(*, sqs_message: SqsJobMessage, error_class: str, error_code: str) -> VerdictRow` — imports `VerdictRow` from `trikon_cloud.fargate_runner.models`. Pure builder.                                                                                                        |

#### `trikon_cloud.orchestrator.logger`

| Symbol                       | Signature                                                                                                                             |
|------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| `get_logger`                 | `() -> aws_lambda_powertools.Logger` — returns the module-level singleton configured with `service="trikon-cloud-orchestrator"`.       |
| `append_job_context`         | `(logger: aws_lambda_powertools.Logger, *, message: SqsJobMessage) -> None` — appends the five natural-key fields via `append_keys`. |
| `LOGGING_DENYLIST`           | `frozenset[str]` of field names that must never appear as log keys — enforced by a Powertools log filter (Invariant 6).                |

#### `trikon_cloud.installation_lifecycle.handler`

| Symbol                                | Signature                                                                                     |
|---------------------------------------|-----------------------------------------------------------------------------------------------|
| `lambda_handler`                      | `(event: SqsEventEnvelope, context: LambdaContext) -> SqsBatchResponse`                       |
| `handle_installation_created`         | `(message: InstallationEventMessage, *, iam_prov: IamProvisioner, ddb: InstallationsTableWriter) -> None` |
| `handle_installation_deleted`         | `(message: InstallationEventMessage, *, iam_prov: IamProvisioner, ddb: InstallationsTableWriter) -> None` |
| `handle_repositories_added`           | `(message: InstallationEventMessage, *, ddb: InstallationsTableWriter) -> None`               |
| `handle_repositories_removed`         | `(message: InstallationEventMessage, *, ddb: InstallationsTableWriter) -> None`               |

#### `trikon_cloud.installation_lifecycle.models`

| Symbol                        | Kind         | Notes                                                                                                                              |
|-------------------------------|--------------|------------------------------------------------------------------------------------------------------------------------------------|
| `InstallationEventMessage`    | `BaseModel`  | See §3.2. `extra="forbid"`; frozen; `event_type: Literal[...]`.                                                                    |
| `LifecycleTableRow`           | `BaseModel`  | `installation_id: int`; `status: Literal["active","disabled"]`; `github_app_id: int`; `created_at: str`; `updated_at: str`; `repositories: frozenset[str]`. Frozen. |
| `LifecycleEnvConfig`          | `BaseSettings` | Env vars listed in §2.3.                                                                                                          |

#### `trikon_cloud.installation_lifecycle.iam_template`

| Symbol                                            | Signature                                                                                                                                                                                       |
|---------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `render_installation_policy_document`             | `(installation_id: int, *, app_private_key_secret_arn: str, account_id: str, region: str) -> InstallationPolicyDocument` — pure.                                                                |
| `render_installation_assume_role_policy_document` | `() -> AssumeRolePolicyDocument` — pure. Returns the fixed `sts:AssumeRole` policy for `ecs-tasks.amazonaws.com`.                                                                                |
| `InstallationPolicyDocument`                      | `BaseModel` — see §6. Frozen.                                                                                                                                                                   |
| `PolicyStatement`                                 | `BaseModel` — Sid + Effect + Action + Resource + optional Condition. Frozen.                                                                                                                    |
| `PolicyCondition`                                 | `BaseModel` — `ForAllValues_StringEquals: dict[str, tuple[str, ...]]` (only usage: `dynamodb:LeadingKeys`). Uses Pydantic `alias="ForAllValues:StringEquals"` so serialization emits colons. Frozen. |
| `AssumeRolePolicyDocument`                        | `BaseModel` — fixed `sts:AssumeRole` shape. Frozen.                                                                                                                                             |
| `canonical_json`                                  | `(doc: InstallationPolicyDocument | AssumeRolePolicyDocument) -> str` — `json.dumps(json.loads(doc.model_dump_json(by_alias=True)), sort_keys=True, separators=(",", ":"))`.                        |

#### `trikon_cloud.installation_lifecycle.iam_provisioner`

| Symbol                       | Signature                                                                                                                                                                                       |
|------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `IamProvisioner`             | `class` — `__init__(*, iam_client: IamClientProtocol)`.                                                                                                                                          |
| `IamProvisioner.provision`   | `(installation_id: int, *, policy: InstallationPolicyDocument, assume_role_policy: AssumeRolePolicyDocument) -> ProvisionResult` — invokes `CreateRole` then `PutRolePolicy`; idempotent on already-exists. |
| `IamProvisioner.deprovision` | `(installation_id: int) -> DeprovisionResult` — invokes `DeleteRolePolicy` then `DeleteRole`; idempotent on missing.                                                                             |
| `ProvisionResult`            | `BaseModel` with `role_arn: str`; `already_existed: bool`. Frozen.                                                                                                                              |
| `DeprovisionResult`          | `BaseModel` with `role_name: str`; `already_absent: bool`. Frozen.                                                                                                                              |
| `IamClientProtocol`          | `typing.Protocol` with typed methods for `create_role`, `put_role_policy`, `delete_role_policy`, `delete_role`, `get_role`.                                                                     |

#### `trikon_cloud.installation_lifecycle.dynamodb_writer`

| Symbol                                    | Signature                                                                                            |
|-------------------------------------------|------------------------------------------------------------------------------------------------------|
| `InstallationsTableWriter`                | `class` — `__init__(*, ddb_client: DynamoDbClientProtocol, table_name: str)`.                        |
| `InstallationsTableWriter.upsert_active`  | `(installation_id: int, *, github_app_id: int, repositories: frozenset[str], now_iso: str) -> UpsertResult` |
| `InstallationsTableWriter.mark_disabled`  | `(installation_id: int, *, now_iso: str) -> None`                                                    |
| `InstallationsTableWriter.add_repositories`   | `(installation_id: int, *, repositories: frozenset[str], now_iso: str) -> None`                  |
| `InstallationsTableWriter.remove_repositories`| `(installation_id: int, *, repositories: frozenset[str], now_iso: str) -> None`                  |
| `UpsertResult`                            | `BaseModel` with `already_active: bool`. Frozen.                                                     |

### §2.3 Environment-variable contracts

Both Lambdas load their env vars into a `pydantic_settings.BaseSettings` subclass. Missing or malformed vars raise `pydantic.ValidationError` at cold start — the Lambda fails immediately, before consuming any SQS message. This is deliberate: a misconfigured orchestrator MUST NOT dequeue jobs it cannot dispatch.

**`OrchestratorEnvConfig`** — `trikon_cloud/orchestrator/models.py`

| Alias                                          | Type                  | Notes                                                                        |
|------------------------------------------------|-----------------------|------------------------------------------------------------------------------|
| `TRIKON_AWS_ACCOUNT_ID`                        | `str` (12 digits)     | Baked into `overrides.taskRoleArn` per Req 3.4.                              |
| `TRIKON_RUNNER_SUBNET_IDS`                     | `str` (comma-sep)     | Parsed to `tuple[str, ...]` via a `model_validator`.                         |
| `TRIKON_RUNNER_SECURITY_GROUP_IDS`             | `str` (comma-sep)     | Parsed to `tuple[str, ...]`.                                                 |
| `TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM` | `str`                | Default `"/trikon/verify-runner/active-revision"`.                          |
| `TRIKON_VERIFY_JOBS_DLQ_URL`                   | `str`                 | Present but unused at runtime; retained for CDK-level output/tag parity.     |
| `TRIKON_VERDICTS_TABLE`                        | `str`                 | Default `"trikon_verdicts"` (matches Spec 2).                                |
| `TRIKON_APP_PRIVATE_KEY_SECRET_ARN`            | `str`                 | Required by Never-Fail-Open path.                                            |
| `TRIKON_APP_ID`                                | `int` (`ge=1`)        | GitHub App numeric id for JWT `iss` claim.                                   |
| `TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE`        | `str`                 | Default `"https://cloud.trikon.dev/audits/{delivery_id}"`.                   |
| `AWS_REGION`                                   | `str`                 | Default `"us-east-1"`.                                                       |
| `TRIKON_LOG_LEVEL`                             | `str`                 | Default `"INFO"`.                                                            |

**`LifecycleEnvConfig`** — `trikon_cloud/installation_lifecycle/models.py`

| Alias                                    | Type   | Notes                                                          |
|------------------------------------------|--------|----------------------------------------------------------------|
| `TRIKON_AWS_ACCOUNT_ID`                  | `str`  | Baked into IAM role ARNs.                                      |
| `TRIKON_APP_PRIVATE_KEY_SECRET_ARN`      | `str`  | Baked into the runtime IAM policy for `secretsmanager:GetSecretValue`. |
| `TRIKON_INSTALLATIONS_TABLE`             | `str`  | Default `"trikon-cloud-installations"`.                        |
| `AWS_REGION`                             | `str`  | Default `"us-east-1"`.                                         |
| `TRIKON_LOG_LEVEL`                       | `str`  | Default `"INFO"`.                                              |

---

## §3 Data and message contracts

### §3.1 `SqsJobMessage` — inherited from Spec 1

Reproduced field-by-field from `trikon_cloud/webhook_receiver/models.py`. This spec imports the symbol as-is; it does not redefine it.

```python
class SqsJobMessage(BaseModel):
    """SQS body written to trikon-verify-jobs (memo §5.2)."""

    installation_id: int = Field(ge=1)
    repo_full_name: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    event_type: str
    sent_at: str
    delivery_id: str
```

Source: `trikon_cloud/webhook_receiver/models.py::SqsJobMessage` (already implemented — verified against repo).

### §3.2 `InstallationEventMessage` — new, this spec

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class InstallationEventMessage(BaseModel):
    """SQS body written to trikon-cloud-installation-events (this spec).

    Written by Spec 1's webhook receiver (after its Requirement 12 amendment).
    All fields required. extra=forbid: unknown keys are a validation error, not
    a silent no-op — a shape drift from Spec 1 must fail loudly at parse.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: int = Field(ge=1)
    github_app_id: int = Field(ge=1)
    event_type: Literal[
        "installation.created",
        "installation.deleted",
        "installation_repositories.added",
        "installation_repositories.removed",
    ]
    repositories: tuple[str, ...]
    sent_at: str
    delivery_id: str
```

- `repositories` is `tuple[str, ...]` (not `list[str]`) to keep the model hashable and frozen. Each entry is an `owner/repo` string. On `installation.created`, this is the initial repo set. On `installation_repositories.added` / `installation_repositories.removed`, this is the delta.
- `sent_at` is an ISO-8601 UTC timestamp with millisecond precision, matching Spec 1's `SqsJobMessage.sent_at`.
- `delivery_id` is the GitHub `X-GitHub-Delivery` UUID.
- `extra="forbid"` — differs from `GithubWebhookPayload` (which uses `extra="allow"` because it captures external data). `InstallationEventMessage` is our internal wire contract, so drift is an error.

Round-trip contract: `InstallationEventMessage.model_validate_json(m.model_dump_json()) == m` for any `m: InstallationEventMessage`. This is Property P8 below.

### §3.3 `ecs.RunTask` call body — the memo §5.3 shape

The `build_run_task_call(message, env, task_definition)` function in `ecs_dispatcher.py` returns a `RunTaskCall` model. The model's `.model_dump(by_alias=True, mode="json")` output is the kwargs dict passed to `boto3.client("ecs").run_task(**...)`.

```python
class RunTaskCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster: Literal["trikon-verify-cluster"]
    taskDefinition: str          # "trikon-verify-runner:<revision>"
    launchType: Literal["FARGATE"]
    count: Literal[1]
    networkConfiguration: NetworkConfiguration
    overrides: RunTaskOverrides
    tags: tuple[EcsTag, EcsTag, EcsTag]   # exactly three — Req 3.5
```

**Concrete payload — `installation_id=12345678`, `pr_number=42`, `event_type="pull_request.opened"`, resolved revision `17`, account `000000000000`, subnets `subnet-verify-egress-a,subnet-verify-egress-b`, security groups `sg-verify-egress-only`, delivery `e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab`:**

```json
{
  "cluster": "trikon-verify-cluster",
  "taskDefinition": "trikon-verify-runner:17",
  "launchType": "FARGATE",
  "count": 1,
  "networkConfiguration": {
    "awsvpcConfiguration": {
      "subnets": ["subnet-verify-egress-a", "subnet-verify-egress-b"],
      "securityGroups": ["sg-verify-egress-only"],
      "assignPublicIp": "DISABLED"
    }
  },
  "overrides": {
    "taskRoleArn": "arn:aws:iam::000000000000:role/trikon-verify-task-role-12345678",
    "containerOverrides": [{
      "name": "runner",
      "environment": [
        {"name": "TRIKON_INSTALLATION_ID", "value": "12345678"},
        {"name": "TRIKON_REPO_FULL_NAME",  "value": "octocat/hello-world"},
        {"name": "TRIKON_PR_NUMBER",       "value": "42"},
        {"name": "TRIKON_HEAD_SHA",        "value": "6aabf09b1c4d5e6f7890123456789012345678ab"},
        {"name": "TRIKON_BASE_SHA",        "value": "87f7a31b2c3d4e5f6789012345678901234567cd"},
        {"name": "TRIKON_EVENT_TYPE",      "value": "pull_request.opened"},
        {"name": "TRIKON_DELIVERY_ID",     "value": "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"}
      ]
    }]
  },
  "tags": [
    {"key": "installation_id", "value": "12345678"},
    {"key": "repo",            "value": "octocat/hello-world"},
    {"key": "pr",              "value": "42"}
  ]
}
```

- `taskDefinition` is resolved once per Lambda cold start by `TaskDefinitionResolver` — see §4.3.
- `containerOverrides[0].environment` has exactly seven entries in the order declared above. Byte-order matters for log-review consistency but not for correctness — boto3 does not require ordering on `environment`. We fix it anyway to make golden-JSON diffs stable.
- `tags` has exactly three entries in the order declared above (Req 3.5).

### §3.4 SQS event envelope

The Lambda receives a raw AWS SQS event dict. Powertools' `SQSEvent` accessor is thin and returns `SQSRecord` objects whose attributes are `str` — perfectly fine, but for mypy-strict cleanliness we wrap it in our own `SqsEventEnvelope` model (§2.2) that validates the shape and yields typed `SqsRecord` objects. `batchItemFailures` is emitted per the Lambda batch-item-failure protocol.

### §3.5 Synthetic `VerdictRow` — Never-Fail-Open path

The orchestrator's Never-Fail-Open write reuses Spec 2's `VerdictRow` model verbatim — imported from `trikon_cloud.fargate_runner.models`. No new verdict model is introduced.

Field values on the synthetic row:

| Field                | Value                                                                                     |
|----------------------|-------------------------------------------------------------------------------------------|
| `installation_id`    | `sqs_message.installation_id`                                                             |
| `sk`                 | `f"{sqs_message.sent_at}#{sqs_message.delivery_id}"` — matches Spec 2's sk pattern         |
| `repo_full_name`     | `sqs_message.repo_full_name`                                                              |
| `pr_number`          | `sqs_message.pr_number`                                                                   |
| `head_sha`           | `sqs_message.head_sha`                                                                    |
| `base_sha`           | `sqs_message.base_sha`                                                                    |
| `decision`           | `"require_human"` (Req 7.1)                                                               |
| `matched_rule`       | `"orchestrator terminal failure"` (Req 7.1)                                               |
| `blast_radius_score` | `0`                                                                                       |
| `new_errors`         | `0`                                                                                       |
| `new_warnings`       | `0`                                                                                       |
| `preexisting_errors` | `0`                                                                                       |
| `duration_ms`        | Wall-clock ms from handler entry to `write_orchestrator_failure_verdict` invocation.      |
| `fargate_task_arn`   | `"n/a-orchestrator-terminal"` (the runner never started; documented placeholder).         |
| `schema_version`     | `2` (matches Spec 2 §5.5).                                                                |
| `evidence_blob`      | `gzip(json.dumps({"error_class": error_class, "error_code": error_code, "delivery_id": sqs_message.delivery_id}, sort_keys=True, separators=(",", ":")).encode("utf-8"))` |
| `evidence_s3_key`    | `None` (blob is always small; never spills).                                              |
| `risk_bucket_sk`     | `f"0000#{sqs_message.sent_at}"` — bucket 0 for `require_human` with no risk score.        |

The `VerdictRow.model_validator(mode="after")` enforces `evidence_blob XOR evidence_s3_key`; the synthetic row satisfies this since `evidence_blob` is always populated and `evidence_s3_key` is always `None`.

The DynamoDB item written to `trikon_verdicts` is derived from `VerdictRow` using Spec 2's `to_dynamodb_item()` helper (which we import from `trikon_cloud.fargate_runner.dynamodb_writer` if exported; otherwise we replicate the trivial `{"S":…, "N":…, "B":…}` coercion in `never_fail_open.py`). The `ConditionExpression` is `attribute_not_exists(installation_id) AND attribute_not_exists(sk)` — byte-identical to Spec 2's writer (Req 7.4).

---

## §4 The orchestrator flow — `handler.py`

The `lambda_handler` implements exactly eight steps per SQS record. Batch size is 1 (Req 1.1), so each invocation processes one message. The handler returns an `SqsBatchResponse` with `batchItemFailures` populated only for Transient errors — Terminal errors return an empty `batchItemFailures` (message is deleted by SQS after handler success).

### §4.1 Eight-step flow (happy path)

```python
def lambda_handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]:
    logger = get_logger()
    env = OrchestratorEnvConfig()  # loaded once per cold start (module-level cache)
    envelope = SqsEventEnvelope.model_validate(event)
    assert len(envelope.Records) == 1  # SQS batch size 1 (Req 1.1)
    record = envelope.Records[0]

    # STEP 1 — Powertools log context: attach messageId and receiveCount before
    #          any Pydantic parse, so payload-gate failures still carry SQS context.
    logger.append_keys(
        sqs_message_id=record.messageId,
        approximate_receive_count=int(record.attributes.ApproximateReceiveCount),
    )

    # STEP 2 — Payload-size gate (Req 2.1).
    if len(record.body.encode("utf-8")) > 262_144:
        logger.error(
            "orchestrator_payload_rejected",
            reason="payload_exceeds_256kb",
            byte_count=len(record.body.encode("utf-8")),
            delivery_id=_try_extract_delivery_id(record.body),
        )
        return {"batchItemFailures": []}  # Terminal — delete from queue, no retry.

    # STEP 3 — Parse SqsJobMessage (Req 1.2, Req 2.2).
    try:
        message = SqsJobMessage.model_validate_json(record.body)
    except ValidationError as exc:
        logger.error(
            "orchestrator_payload_rejected",
            reason="malformed_sqs_body",
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        return {"batchItemFailures": []}  # Terminal — delete from queue.

    # STEP 4 — Attach natural-key context (Req 9.2). Everything below carries these
    #          fields on every log record for the rest of the invocation.
    append_job_context(logger, message=message)

    # STEP 5 — Resolve task-definition revision from SSM (Open Item (b) resolution).
    task_definition = _RESOLVER.resolve(ssm_client=_ssm_client())

    # STEP 6 — Build the RunTaskCall (pure — no IO).
    call = build_run_task_call(message, env=env, task_definition=task_definition)

    # STEP 7 — Dispatch. Exactly one ecs.RunTask API call (Req 3.1).
    try:
        result = submit_run_task(call, ecs_client=_ecs_client())
    except ClientError as exc:
        classification = classify_client_error(exc)
        if classification == "transient":
            logger.warning(
                "run_task_transient_failure",
                aws_error_code=exc.response["Error"]["Code"],
                error_class="transient",
            )
            raise TransientDispatchError from exc  # Return message to queue (Req 6.1).
        # Terminal path — Never-Fail-Open (Req 7).
        logger.error(
            "run_task_terminal_failure",
            aws_error_code=exc.response["Error"]["Code"],
            error_class="terminal",
        )
        write_orchestrator_failure_verdict(
            sqs_message=message,
            error_class="terminal",
            error_code=exc.response["Error"]["Code"],
            boto3_session=_boto3_session(),
            github_client=_github_client(env),
        )
        return {"batchItemFailures": []}  # Deleted from queue after synthetic verdict.

    # STEP 8 — Log dispatched task ARN (Req 9.4).
    logger.info(
        "run_task_dispatched",
        dispatched_task_arn=result.task_arn,
    )
    return {"batchItemFailures": []}
```

Module-scope caches: `_RESOLVER = TaskDefinitionResolver(env=...)`, `_ecs_client()` and `_ssm_client()` and `_boto3_session()` are lazily initialized once per container.

### §4.2 Error class enumeration (Req 6.4)

`classify_client_error` in `ecs_dispatcher.py`:

```python
_TRANSIENT_CODES: frozenset[str] = frozenset({
    "ThrottlingException",
    "RequestLimitExceeded",
    "ServiceUnavailable",
    "InternalFailure",
})

_TERMINAL_CODES: frozenset[str] = frozenset({
    "InvalidParameterException",
    "AccessDeniedException",
    "ClusterNotFoundException",
    "TaskDefinitionNotFound",
    "NoSuchEntity",
})


def classify_client_error(err: ClientError) -> Literal["transient", "terminal"]:
    code = err.response.get("Error", {}).get("Code", "")
    if code in _TERMINAL_CODES:
        return "terminal"
    if code in _TRANSIENT_CODES:
        return "transient"
    # Fail-safe default (Req 6.4).
    return "transient"
```

`botocore.exceptions.EndpointConnectionError`, `ReadTimeoutError`, `ConnectTimeoutError`, and generic `BotoCoreError` never appear on a `ClientError.response["Error"]["Code"]` — they surface as different exception classes. `submit_run_task` catches `ClientError`; other `BotoCoreError` subclasses are re-raised without classification (SQS default retry semantics apply — the handler exits with an unhandled exception and Lambda marks the batch as a failure, which is the correct Transient-equivalent behavior).

### §4.3 Task-definition revision resolution

`TaskDefinitionResolver` in `ecs_dispatcher.py`:

```python
class TaskDefinitionResolver:
    """Resolves the active trikon-verify-runner revision at Lambda cold start.

    Reads /trikon/verify-runner/active-revision from SSM Parameter Store (written
    by Spec 2's CDK stack per §9). Caches the resolved family:revision string in
    module scope for the container lifetime. Cache TTL is the container lifetime
    itself — a Lambda cold start after an SSM update picks up the new value.
    """

    _cache: str | None = None

    def __init__(self, *, env: OrchestratorEnvConfig) -> None:
        self._param_name = env.trikon_verify_runner_active_revision_ssm_param

    def resolve(self, *, ssm_client: SsmClientProtocol) -> str:
        if self._cache is not None:
            return self._cache
        resp = ssm_client.get_parameter(Name=self._param_name)
        parsed = SsmGetParameterResponse.model_validate(resp)
        revision = int(parsed.Parameter.Value)  # raises ValueError → Terminal
        if revision < 1:
            raise ValueError(f"invalid revision from SSM: {revision}")
        self._cache = f"trikon-verify-runner:{revision}"
        return self._cache
```

TTL design choice: **container lifetime, no explicit refresh**. A Lambda container lives ~15 min idle max on AWS; a task-definition update triggers a natural rotation within one idle cycle. If the operator wants immediate rollout, they run `aws lambda update-function-configuration --function-name trikon-cloud-orchestrator --environment ...` (any env change flushes all containers). We do NOT poll SSM per-invocation — that would add ~5 ms of GetParameter latency and a per-invocation `ssm:GetParameter` grant to every hot invocation.

### §4.4 Never-Fail-Open path (invoked from STEP 7 Terminal branch)

Steps executed inside `write_orchestrator_failure_verdict`:

1. Build `VerdictRow` via `build_synthetic_verdict_row(sqs_message=..., error_class=..., error_code=...)` — pure (§3.5).
2. Convert to DynamoDB item shape and issue `dynamodb:PutItem` on `trikon_verdicts` with `ConditionExpression=attribute_not_exists(installation_id) AND attribute_not_exists(sk)`.
3. If `ConditionalCheckFailedException`: log `verdict_already_exists` at INFO and return (Req 7.4 — idempotency win).
4. Mint installation token via `OrchestratorGithubClient` (JWT with App private key from Secrets Manager + `POST /app/installations/{id}/access_tokens`); cache the JWT for its 10-minute TTL in module scope.
5. `POST /repos/{repo_full_name}/check-runs` with `name="Trikon"`, `head_sha=<message.head_sha>`, `status="completed"`, `conclusion="neutral"`, `output={title: "Trikon Cloud verification unavailable", summary: "The verification runner could not be dispatched. A human will review this PR.", text: null}`, `details_url=env.check_run_details_url_template.format(delivery_id=message.delivery_id)`.
6. Log `orchestrator_failure_resolved` at INFO with the newly-created Check Run id (parsed from GitHub response body).

Steps 1-3 and steps 4-6 are wrapped independently: if the Check Run POST fails but the DynamoDB write succeeded, the orchestrator has already satisfied Req 7.1 (the verdict row exists — the dashboard will pick it up). The Check Run failure is logged at ERROR and the handler still returns success (Req 7.4 semantics extend here — one-way partial success is better than replaying the whole path). If the DynamoDB write fails with an error other than `ConditionalCheckFailedException`, the exception propagates and the SQS message returns to the queue for one more retry attempt before hitting the DLQ.

### §4.5 Full error-class enumeration

| Layer                          | Exception / condition                                                                 | Classification                | Handler action                                                                                              |
|--------------------------------|---------------------------------------------------------------------------------------|-------------------------------|-------------------------------------------------------------------------------------------------------------|
| Payload-size gate              | body > 262 144 bytes                                                                  | Terminal                      | Log `payload_exceeds_256kb`. Return `batchItemFailures=[]` — message deleted.                               |
| SQS body parse                 | `pydantic.ValidationError` on `SqsJobMessage`                                         | Terminal                      | Log `malformed_sqs_body` with error locations. Return `batchItemFailures=[]`.                                |
| SSM parameter fetch            | `ClientError` `ParameterNotFound`                                                     | Terminal (cold-start crash)   | Uncaught at handler; Lambda cold start fails and Lambda infra returns the message to the queue automatically. Effectively Transient at SQS layer. |
| SSM parameter fetch            | Value not parseable as int                                                            | Terminal (cold-start crash)   | Same as above.                                                                                              |
| `ecs.RunTask`                  | `ClientError` code in `_TRANSIENT_CODES`                                              | Transient                     | Raise `TransientDispatchError`; SQS returns the message to the queue.                                       |
| `ecs.RunTask`                  | `ClientError` code in `_TERMINAL_CODES`                                               | Terminal                      | Never-Fail-Open: write verdict + neutral Check Run. Return `batchItemFailures=[]`.                          |
| `ecs.RunTask`                  | `ClientError` code not in either set                                                  | Transient (fail-safe default) | Same as Transient above.                                                                                    |
| `ecs.RunTask`                  | `botocore.exceptions.EndpointConnectionError` / `ReadTimeoutError`                    | Transient (implicit)          | Uncaught by classification path; propagates → Lambda fails → SQS returns message.                           |
| Never-Fail-Open DDB PutItem    | `ConditionalCheckFailedException`                                                     | Success (idempotency)         | Log `verdict_already_exists` INFO; return.                                                                  |
| Never-Fail-Open DDB PutItem    | any other `ClientError`                                                               | Transient                     | Propagates; SQS returns the message so the retry may succeed.                                               |
| Never-Fail-Open Check Run POST | 5xx / 429 (retry-budget exhausted after 3 attempts)                                   | Partial success               | Log `check_run_post_failed` ERROR; return success (row already written — Invariant 2 already satisfied).    |
| Never-Fail-Open Check Run POST | 4xx (401 / 403 / 404)                                                                 | Partial success               | Log `check_run_post_client_error` ERROR; return success.                                                    |

---

## §5 The lifecycle flow — `installation_lifecycle/handler.py`

The Lifecycle_Handler follows five steps per SQS record. Batch size 1 (Req 10.1).

### §5.1 Five-step flow

```python
def lambda_handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]:
    logger = get_logger()  # service="trikon-cloud-installation-lifecycle"
    env = LifecycleEnvConfig()
    envelope = SqsEventEnvelope.model_validate(event)
    record = envelope.Records[0]

    # STEP 1 — Parse InstallationEventMessage (Req 11.3).
    try:
        message = InstallationEventMessage.model_validate_json(record.body)
    except ValidationError as exc:
        logger.error(
            "installation_event_rejected",
            reason="malformed_installation_event",
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        return {"batchItemFailures": []}  # Terminal — DLQ via redrive.

    # STEP 2 — Attach log context (Req 15.2).
    logger.append_keys(
        installation_id=message.installation_id,
        event_type=message.event_type,
        delivery_id=message.delivery_id,
    )

    # STEP 3 — Dispatch by event_type (Req 12).
    iam_prov = IamProvisioner(iam_client=_iam_client())
    ddb = InstallationsTableWriter(ddb_client=_ddb_client(), table_name=env.trikon_installations_table)
    now_iso = _now_iso_utc_ms()

    try:
        match message.event_type:
            case "installation.created":
                handle_installation_created(message, iam_prov=iam_prov, ddb=ddb, env=env, now_iso=now_iso)
            case "installation.deleted":
                handle_installation_deleted(message, iam_prov=iam_prov, ddb=ddb, now_iso=now_iso)
            case "installation_repositories.added":
                handle_repositories_added(message, ddb=ddb, now_iso=now_iso)
            case "installation_repositories.removed":
                handle_repositories_removed(message, ddb=ddb, now_iso=now_iso)
    except ClientError as exc:
        # Req 15.3 — Terminal error on installation.created leaves platform in
        # half-configured state; MUST fail message to DLQ, not silently succeed.
        logger.error(
            "lifecycle_terminal_failure",
            error_class="terminal",
            aws_error_code=exc.response.get("Error", {}).get("Code", "unknown"),
        )
        raise  # SQS returns to queue; DLQ after maxReceiveCount=3.

    logger.info("lifecycle_event_processed")
    return {"batchItemFailures": []}
```

### §5.2 `handle_installation_created`

```python
def handle_installation_created(
    message: InstallationEventMessage,
    *,
    iam_prov: IamProvisioner,
    ddb: InstallationsTableWriter,
    env: LifecycleEnvConfig,
    now_iso: str,
) -> None:
    logger = get_logger()

    # (a) Render policy (pure).
    policy = render_installation_policy_document(
        message.installation_id,
        app_private_key_secret_arn=env.trikon_app_private_key_secret_arn,
        account_id=env.trikon_aws_account_id,
        region=env.aws_region,
    )
    assume_role_policy = render_installation_assume_role_policy_document()

    # (b) Provision IAM role — idempotent on already-exists (Req 14.2).
    prov_result = iam_prov.provision(
        message.installation_id,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    if prov_result.already_existed:
        logger.info("installation_already_provisioned", role_arn=prov_result.role_arn)

    # (c) Upsert DynamoDB row — idempotent on already-active (Req 14.3).
    upsert_result = ddb.upsert_active(
        message.installation_id,
        github_app_id=message.github_app_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    if upsert_result.already_active:
        logger.info("installation_already_active")
        return

    logger.info("installation_provisioned")
```

`upsert_active` uses `PutItem` with `ConditionExpression=attribute_not_exists(installation_id) OR #s <> :active` (where `#s` is the `status` attribute and `:active` is `"active"`). On `ConditionalCheckFailedException`, the row already has `status="active"` and the writer returns `UpsertResult(already_active=True)` — success path.

### §5.3 `handle_installation_deleted`

```python
def handle_installation_deleted(
    message: InstallationEventMessage,
    *,
    iam_prov: IamProvisioner,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    logger = get_logger()
    deprov_result = iam_prov.deprovision(message.installation_id)
    if deprov_result.already_absent:
        logger.info("installation_role_already_absent", role_name=deprov_result.role_name)
    ddb.mark_disabled(message.installation_id, now_iso=now_iso)  # UpdateItem, no CE.
    logger.info("installation_disabled")
```

`IamProvisioner.deprovision` invokes `iam:DeleteRolePolicy` (inline policy name is fixed to `"TrikonInstallationPolicy"`) then `iam:DeleteRole`. Each call catches `NoSuchEntity` and marks that leg as `already_absent` (Req 14.4). Deletion order matters: `DeleteRole` fails with `DeleteConflict` if the role still has inline policies; `DeleteRolePolicy` first, `DeleteRole` second, is safe.

`ddb.mark_disabled` uses `UpdateItem` with `SET #s = :disabled, updated_at = :now`. It does NOT use a `ConditionExpression` — an update on a missing key is a no-op create in DynamoDB with the SET attributes, which is fine here (a "disabled" row with only `installation_id`, `status`, `updated_at` is a valid dormant sentinel).

### §5.4 `handle_repositories_added` / `handle_repositories_removed`

```python
def handle_repositories_added(
    message: InstallationEventMessage,
    *,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    ddb.add_repositories(
        message.installation_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    get_logger().info("repositories_added", count=len(message.repositories))


def handle_repositories_removed(
    message: InstallationEventMessage,
    *,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    ddb.remove_repositories(
        message.installation_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    get_logger().info("repositories_removed", count=len(message.repositories))
```

Both use `UpdateItem` with an atomic `ADD` / `DELETE` on the `repositories` SS attribute. The DynamoDB semantics are naturally idempotent for set-add and set-delete — adding a member that already exists is a no-op; deleting a member that is not present is a no-op. No explicit conditional expression is required (Req 14.5).

### §5.5 Idempotency table

| Scenario                                                                | AWS response                                | Handler behavior                                                                                            |
|-------------------------------------------------------------------------|---------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `iam:CreateRole` on redelivered `installation.created`                  | `EntityAlreadyExistsException`              | INFO `installation_already_provisioned`; continue to DynamoDB step (Req 14.2).                              |
| DynamoDB upsert on already-active row                                    | `ConditionalCheckFailedException`           | INFO `installation_already_active`; return success (Req 14.3).                                              |
| `iam:DeleteRolePolicy` on already-deleted role                          | `NoSuchEntity`                              | Silent success in `IamProvisioner.deprovision`; continue to `DeleteRole`.                                   |
| `iam:DeleteRole` on already-deleted role                                | `NoSuchEntity`                              | Silent success; `DeprovisionResult.already_absent=True` (Req 14.4).                                         |
| DynamoDB `UpdateItem` on missing row (deleted event)                    | Creates a `disabled` sentinel               | Success (no CE; Req 14.4 semantics).                                                                        |
| DynamoDB `UpdateItem` `ADD` on already-present repo                     | Idempotent no-op                            | Success (Req 14.5).                                                                                         |
| DynamoDB `UpdateItem` `DELETE` on missing repo                          | Idempotent no-op                            | Success (Req 14.5).                                                                                         |

---

## §6 IAM policy contract — `iam_template.py`

### §6.1 Function signature

```python
def render_installation_policy_document(
    installation_id: int,
    *,
    app_private_key_secret_arn: str,
    account_id: str,
    region: str,
) -> InstallationPolicyDocument:
    """Pure renderer for the per-installation task-role policy.

    Byte-match target: Spec 2's ``FargateRunnerStack.build_task_role_for_installation()``
    output (see spec-2 design §9 and infra/fargate_runner_stack.py:279-399).

    A contract test at tests/test_iam_policy_contract.py synthesizes the CDK
    stack for installation_id=12345678 and asserts:

        canonical_json(render_installation_policy_document(12345678, ...))
            == canonical_json_of_cdk_synthesized_policy

    where ``canonical_json`` is
        json.dumps(json.loads(model.model_dump_json(by_alias=True)),
                   sort_keys=True, separators=(",", ":"))
    """
    ...
```

### §6.2 Pydantic model hierarchy

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PolicyCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Only usage: ForAllValues:StringEquals { dynamodb:LeadingKeys: [...] }.
    # Pydantic alias emits the colon-separated JSON key on serialization.
    for_all_values_string_equals: dict[str, tuple[str, ...]] = Field(
        alias="ForAllValues:StringEquals",
    )


class PolicyStatement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    Sid: str
    Effect: Literal["Allow"]
    Action: tuple[str, ...]
    Resource: str
    Condition: PolicyCondition | None = None


class InstallationPolicyDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    Version: Literal["2012-10-17"]
    Statement: tuple[
        PolicyStatement,   # 0 — DynamoDBVerdictsScopedToInstallation
        PolicyStatement,   # 1 — DynamoDBPrStateScopedToInstallation
        PolicyStatement,   # 2 — S3EvidenceSpillScopedToInstallation
        PolicyStatement,   # 3 — SecretsManagerAppPrivateKeyRead
    ]


class AssumeRolePolicyStatement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    Effect: Literal["Allow"]
    Principal: dict[str, str]      # {"Service": "ecs-tasks.amazonaws.com"}
    Action: Literal["sts:AssumeRole"]


class AssumeRolePolicyDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    Version: Literal["2012-10-17"]
    Statement: tuple[AssumeRolePolicyStatement]
```

The `Statement` tuple is fixed-length four, order-significant. Order is pinned to match Spec 2's `add_to_policy` call order (see §6.3). Byte-match assertions in §6.5 depend on this order being stable.

### §6.3 Four Statement entries — enumerated verbatim

**Statement 0 — DynamoDB PutItem on `trikon_verdicts`, scoped by LeadingKeys** (mirrors Spec 2's `build_task_role_for_installation()` first `add_to_policy` call, `fargate_runner_stack.py` ~line 279):

```json
{
  "Sid": "DynamoDBVerdictsScopedToInstallation",
  "Effect": "Allow",
  "Action": ["dynamodb:PutItem"],
  "Resource": "arn:aws:dynamodb:us-east-1:{account_id}:table/trikon_verdicts",
  "Condition": {
    "ForAllValues:StringEquals": {
      "dynamodb:LeadingKeys": ["${aws:PrincipalTag/installation_id}"]
    }
  }
}
```

**Statement 1 — DynamoDB GetItem/PutItem/UpdateItem on `trikon_pr_state`, scoped by LeadingKeys** (second `add_to_policy` call):

```json
{
  "Sid": "DynamoDBPrStateScopedToInstallation",
  "Effect": "Allow",
  "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
  "Resource": "arn:aws:dynamodb:us-east-1:{account_id}:table/trikon_pr_state",
  "Condition": {
    "ForAllValues:StringEquals": {
      "dynamodb:LeadingKeys": ["${aws:PrincipalTag/installation_id}"]
    }
  }
}
```

**Statement 2 — S3 PutObject/GetObject on the per-installation evidence prefix** (third `add_to_policy` call). No `Condition` block — the prefix is baked into the `Resource` ARN:

```json
{
  "Sid": "S3EvidenceSpillScopedToInstallation",
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject"],
  "Resource": "arn:aws:s3:::trikon-cloud-evidence/${aws:PrincipalTag/installation_id}/*"
}
```

**Statement 3 — Secrets Manager GetSecretValue on the App private-key secret ARN** (fourth `add_to_policy` call):

```json
{
  "Sid": "SecretsManagerAppPrivateKeyRead",
  "Effect": "Allow",
  "Action": ["secretsmanager:GetSecretValue"],
  "Resource": "{app_private_key_secret_arn}"
}
```

`Action` on every statement is a `tuple[str, ...]` (single-element on Statements 0, 3). Serialization to JSON emits `["dynamodb:PutItem"]` (a one-element list), not the string form `"dynamodb:PutItem"`, because IAM accepts either but CDK synth emits the list form and byte-match requires list form.

The `region` parameter is used to construct the `Resource` ARN for Statements 0 and 1 (`arn:aws:dynamodb:{region}:{account_id}:table/...`). At M1 the region is fixed to `us-east-1`, but plumbing the parameter through preserves optionality for a hypothetical M3 multi-region deployment.

### §6.4 The scoping reference — `${aws:PrincipalTag/installation_id}`

The runtime IAM policy uses IAM's own variable-substitution syntax; the actual `installation_id` value is baked into the role's principal tag, not into the policy string. That is: `iam:CreateRole` creates `trikon-verify-task-role-12345678` with tag `installation_id=12345678`, and the policy string contains the literal `${aws:PrincipalTag/installation_id}` — evaluated by IAM at authorization time. This is exactly the mechanism Spec 2's CDK uses (memo §6), so the runtime-rendered policy and the CDK-synthesized policy share this template variable verbatim.

The role's tag is set at `iam:CreateRole` time:

```python
iam_client.create_role(
    RoleName=f"trikon-verify-task-role-{installation_id}",
    AssumeRolePolicyDocument=canonical_json(assume_role_policy),
    Tags=[{"Key": "installation_id", "Value": str(installation_id)}],
)
```

### §6.5 Byte-match strategy against CDK synth

The contract test at `trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py`:

1. Instantiates the CDK stack: `stack = FargateRunnerStack(...)`. Calls `stack.build_task_role_for_installation(installation_id=12345678, app_private_key_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef")`.
2. Extracts the synthesized CloudFormation template: `template = Template.from_stack(stack)`.
3. Finds the `AWS::IAM::Policy` resource attached to the role via `template.find_resources("AWS::IAM::Policy", {...})`.
4. Extracts the `PolicyDocument` property. CDK synth emits the document as a Python dict.
5. Canonicalizes: `cdk_canonical = json.dumps(cdk_policy_dict, sort_keys=True, separators=(",", ":"))`.
6. Invokes the runtime renderer with identical inputs: `rt_doc = render_installation_policy_document(12345678, ...)`.
7. Canonicalizes: `rt_canonical = json.dumps(json.loads(rt_doc.model_dump_json(by_alias=True)), sort_keys=True, separators=(",", ":"))`.
8. Asserts `cdk_canonical == rt_canonical`.

CDK's `Duration`, `iam.PolicyStatement`, and `iam.PolicyDocument` classes emit fully expanded ARNs (with `${AWS::AccountId}` and `${AWS::Region}` intrinsics resolved when the Template is synthesized with a bound account/region context). Contract-test setup binds the CDK app to `env={"account": "000000000000", "region": "us-east-1"}` so the synth output is a plain JSON string with those substitutions already applied — no `Fn::Sub` remains. The runtime renderer is invoked with `account_id="000000000000"`, so both sides produce byte-identical ARN strings.

Any drift — Spec 2 adding a fifth statement, changing `dynamodb:LeadingKeys` to `dynamodb:Attributes`, changing Sid strings — makes the test fail at contract-test time, before any runtime IAM CreateRole call reaches AWS.

### §6.6 Contract-test invariant (formally stated)

**Property P10 (Byte-match IAM policy — see §10.4).** For any `installation_id ∈ [1, 2⁶³)` and any well-formed `app_private_key_secret_arn`, the canonical JSON of `render_installation_policy_document(installation_id, ...)` equals the canonical JSON of the CDK-synthesized template for the same inputs.

---

## §7 Never-Fail-Open path — `never_fail_open.py`

### §7.1 Function signature

```python
def write_orchestrator_failure_verdict(
    *,
    sqs_message: SqsJobMessage,
    error_class: str,
    error_code: str,
    boto3_session: boto3.session.Session,
    github_client: OrchestratorGithubClient,
) -> None:
    """Synthetic require_human verdict + neutral Check Run on orchestrator terminal failure.

    Idempotent by ConditionExpression on the trikon_verdicts PutItem. A redelivered
    terminal failure produces the same row (ConditionalCheckFailedException on the
    second attempt) and MAY produce a second Check Run — GitHub's Check Run API
    is not deduplicating, so redelivery may leave two neutral runs on the PR. This
    is acceptable: Invariant 2 requires *at least one* customer-visible verdict, not
    at most one. Deduping Check Runs across redeliveries would require an extra
    DynamoDB read on the hot verify path; we choose to skip it.
    """
```

### §7.2 Six-step body

1. **Build the row.**
   ```python
   row = build_synthetic_verdict_row(
       sqs_message=sqs_message,
       error_class=error_class,
       error_code=error_code,
   )
   ```
   `build_synthetic_verdict_row` is pure — no IO — and produces the `VerdictRow` (§3.5).

2. **DynamoDB PutItem with idempotency guard.**
   ```python
   ddb = boto3_session.client("dynamodb")
   try:
       ddb.put_item(
           TableName="trikon_verdicts",
           Item=_to_dynamodb_item(row),
           ConditionExpression="attribute_not_exists(installation_id) AND attribute_not_exists(sk)",
       )
   except ClientError as exc:
       if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
           get_logger().info("verdict_already_exists", sk=row.sk)
           return
       raise
   ```
   `_to_dynamodb_item` maps `VerdictRow` fields to the `{"S":…, "N":…, "B":…}` shape. `evidence_blob` — a `bytes` value — becomes `{"B": row.evidence_blob}` (boto3 base64-encodes at wire time).

3. **Log the successful write at INFO.**
   ```python
   get_logger().info("orchestrator_verdict_written", sk=row.sk)
   ```

4. **Mint installation token (via OrchestratorGithubClient).** Token is cached in module scope for its 10-minute App-JWT TTL; installation tokens are cached per-installation for their 1-hour TTL, minus a 5-minute safety margin. See §7.3.

5. **POST Check Run — conclusion="neutral".**
   ```python
   github_client.create_neutral_check_run(
       installation_id=sqs_message.installation_id,
       repo_full_name=sqs_message.repo_full_name,
       head_sha=sqs_message.head_sha,
       details_url=env.check_run_details_url_template.format(delivery_id=sqs_message.delivery_id),
   )
   ```
   Body of the POST:
   ```json
   {
     "name": "Trikon",
     "head_sha": "<message.head_sha>",
     "status": "completed",
     "conclusion": "neutral",
     "output": {
       "title": "Trikon Cloud verification unavailable",
       "summary": "The Trikon Cloud verification runner could not be dispatched for this pull request. A human reviewer will follow up. Delivery id: <delivery_id>.",
       "text": null
     },
     "details_url": "<details_url>"
   }
   ```
   The Check Run `name` is exactly `"Trikon"` — matches Spec 2's convention (Invariant 7).

6. **Log resolution.**
   ```python
   get_logger().info("orchestrator_failure_resolved")
   ```

### §7.3 `OrchestratorGithubClient`

Thin wrapper around `httpx.Client`. Public methods:

```python
class OrchestratorGithubClient:
    def __init__(
        self,
        *,
        app_id: int,
        app_private_key_secret_arn: str,
        secrets_client: SecretsClientProtocol,
        http_client: httpx.Client,
    ) -> None: ...

    def create_neutral_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        details_url: str,
    ) -> None: ...
```

Internal flow: `create_neutral_check_run` calls `_get_installation_token(installation_id)` which:
- Checks module-scope cache `_TOKEN_CACHE: dict[int, tuple[str, datetime]]`.
- If cached token expires more than 5 minutes from now, returns cached.
- Otherwise:
  - `_get_app_jwt()` — reads the App private key PEM from Secrets Manager (via `secrets_client.get_secret_value(SecretId=app_private_key_secret_arn)`; PEM stays in-process, never logged), signs a JWT with `iss=app_id`, `iat=now-60`, `exp=now+540` (9 minutes), `alg="RS256"`. JWT cached separately with 8-minute effective TTL.
  - `POST https://api.github.com/app/installations/{installation_id}/access_tokens` with `Authorization: Bearer <app_jwt>`.
  - Cache the returned `token` string + `expires_at` datetime.

Retry policy on the Check Run POST: 3 attempts with exponential backoff (0.5s, 1s, 2s), 30-second total budget. Only `500`, `502`, `503`, `504`, `429` are retryable. On 4xx (401/403/404) or budget exhaustion, raises `OrchestratorGithubClientError` — caught by `write_orchestrator_failure_verdict` and logged at ERROR without re-raising (Req 7.2 is satisfied by the DynamoDB write already succeeded — the Check Run failure is a partial-success degradation, not an Invariant-2 violation).

### §7.4 Reuse of Spec 2's `token_cache`

The Fargate runner ships `trikon_cloud.fargate_runner.token_cache::TokenCache` — a threading-safe cache with a `get_or_mint(minter)` method. In principle we could reuse it here. In practice we do NOT reuse it: the Fargate runner is a single-process, single-installation image; the orchestrator Lambda is single-process but handles many installations across many invocations within one container's lifetime. The cache key structure is different (Spec 2 caches one token per process; orchestrator caches N tokens per process, keyed by installation_id). We replicate the caching logic in `OrchestratorGithubClient` rather than import — a citation in the module docstring notes the parallel:

> Token caching semantics parallel Spec 2's `trikon_cloud.fargate_runner.token_cache`; the shape differs because this client is multi-tenant per container while Spec 2's is single-tenant per Fargate task. Any future refactor to share code would need to keep the multi-tenant key structure here.

---

## §8 CDK topology — `orchestrator_stack.py`

### §8.1 One stack, two Lambdas

```python
class OrchestratorStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Reference existing (Spec 1-owned) queues ─────────────────────────
        verify_jobs_queue = sqs.Queue.from_queue_arn(
            self, "VerifyJobsQueue",
            queue_arn=f"arn:aws:sqs:us-east-1:{self.account}:trikon-verify-jobs",
        )
        verify_jobs_dlq = sqs.Queue.from_queue_arn(
            self, "VerifyJobsDlq",
            queue_arn=f"arn:aws:sqs:us-east-1:{self.account}:trikon-verify-jobs-dlq",
        )

        # ── New (this-spec-owned) resources ──────────────────────────────────
        lifecycle_dlq = sqs.Queue(
            self, "InstallationEventsDlq",
            queue_name="trikon-cloud-installation-events-dlq",
            retention_period=Duration.days(14),
        )
        lifecycle_queue = sqs.Queue(
            self, "InstallationEventsQueue",
            queue_name="trikon-cloud-installation-events",
            visibility_timeout=Duration.minutes(2),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=lifecycle_dlq,
                max_receive_count=3,           # Req 10.2
            ),
        )

        installations_table = dynamodb.TableV2(
            self, "InstallationsTable",
            table_name="trikon-cloud-installations",
            partition_key=dynamodb.Attribute(
                name="installation_id",
                type=dynamodb.AttributeType.NUMBER,
            ),
            billing=dynamodb.Billing.on_demand(),   # Req 14.1
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── Read cross-stack SSM parameter (written by Spec 2 — see §9) ──────
        active_revision_param = ssm.StringParameter.from_string_parameter_name(
            self, "VerifyRunnerActiveRevisionParam",
            string_parameter_name="/trikon/verify-runner/active-revision",
        )

        # ── Orchestrator_Handler Lambda ──────────────────────────────────────
        orchestrator_role = iam.Role(
            self, "OrchestratorRole",
            role_name="trikon-cloud-orchestrator-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        _grant_orchestrator_permissions(
            orchestrator_role,
            verify_jobs_queue=verify_jobs_queue,
            verify_jobs_dlq=verify_jobs_dlq,
            active_revision_param=active_revision_param,
            app_private_key_secret_arn=<from context/config>,
        )

        orchestrator_fn = _lambda.Function(
            self, "OrchestratorFn",
            function_name="trikon-cloud-orchestrator",
            runtime=_lambda.Runtime.PYTHON_3_11,      # Req 8.1, 16.3
            architecture=_lambda.Architecture.ARM_64, # Req 8.1
            memory_size=512,                          # Req 8.1
            timeout=Duration.seconds(30),             # Req 8.1
            reserved_concurrent_executions=10,        # Req 8.1
            handler="trikon_cloud.orchestrator.handler.lambda_handler",
            code=_lambda.Code.from_asset("../..", bundling=<pip install cloud-runtime group>),
            role=orchestrator_role,
            environment={
                "TRIKON_AWS_ACCOUNT_ID": self.account,
                "TRIKON_RUNNER_SUBNET_IDS": <from CfnParameter>,
                "TRIKON_RUNNER_SECURITY_GROUP_IDS": <from CfnParameter>,
                "TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM": "/trikon/verify-runner/active-revision",
                "TRIKON_VERDICTS_TABLE": "trikon_verdicts",
                "TRIKON_APP_PRIVATE_KEY_SECRET_ARN": <from context>,
                "TRIKON_APP_ID": <from context>,
                "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE": "https://cloud.trikon.dev/audits/{delivery_id}",
                "TRIKON_LOG_LEVEL": "INFO",
            },
        )

        _lambda.EventSourceMapping(
            self, "OrchestratorEventSource",
            target=orchestrator_fn,
            event_source_arn=verify_jobs_queue.queue_arn,
            batch_size=1,                             # Req 1.1
            max_batching_window=Duration.seconds(0),  # Req 1.1
            report_batch_item_failures=True,
        )

        # ── Lifecycle_Handler Lambda (mirror shape) ──────────────────────────
        lifecycle_role = iam.Role(
            self, "LifecycleRole",
            role_name="trikon-cloud-installation-lifecycle-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        _grant_lifecycle_permissions(
            lifecycle_role,
            lifecycle_queue=lifecycle_queue,
            lifecycle_dlq=lifecycle_dlq,
            installations_table=installations_table,
        )

        lifecycle_fn = _lambda.Function(
            self, "LifecycleFn",
            function_name="trikon-cloud-installation-lifecycle",
            runtime=_lambda.Runtime.PYTHON_3_11,      # Req 15.1
            architecture=_lambda.Architecture.ARM_64, # Req 15.1
            memory_size=512,                          # Req 15.1
            timeout=Duration.seconds(30),             # Req 15.1
            reserved_concurrent_executions=5,         # Req 15.1
            handler="trikon_cloud.installation_lifecycle.handler.lambda_handler",
            code=_lambda.Code.from_asset("../..", bundling=<pip install cloud-runtime group>),
            role=lifecycle_role,
            environment={
                "TRIKON_AWS_ACCOUNT_ID": self.account,
                "TRIKON_APP_PRIVATE_KEY_SECRET_ARN": <from context>,
                "TRIKON_INSTALLATIONS_TABLE": installations_table.table_name,
                "TRIKON_LOG_LEVEL": "INFO",
            },
        )

        _lambda.EventSourceMapping(
            self, "LifecycleEventSource",
            target=lifecycle_fn,
            event_source_arn=lifecycle_queue.queue_arn,
            batch_size=1,                             # Req 10.1
            max_batching_window=Duration.seconds(0),
            report_batch_item_failures=True,
        )
```

### §8.2 IAM policy statements — Orchestrator_Handler role

Per Req 8.2-8.6 plus the Never-Fail-Open extension. Each statement below is one `iam.PolicyStatement` added to `orchestrator_role`.

```python
def _grant_orchestrator_permissions(
    role: iam.Role,
    *,
    verify_jobs_queue: sqs.IQueue,
    verify_jobs_dlq: sqs.IQueue,
    active_revision_param: ssm.IStringParameter,
    app_private_key_secret_arn: str,
) -> None:
    account = Stack.of(role).account

    # Grant 1 — ecs:RunTask on the runner task-definition family (Req 8.2).
    role.add_to_principal_policy(iam.PolicyStatement(
        sid="EcsRunTaskOnRunnerFamily",
        effect=iam.Effect.ALLOW,
        actions=["ecs:RunTask"],
        resources=[
            f"arn:aws:ecs:us-east-1:{account}:task-definition/trikon-verify-runner:*",
        ],
    ))

    # Grant 2 — iam:PassRole on the per-installation task role (Req 8.3).
    role.add_to_principal_policy(iam.PolicyStatement(
        sid="IamPassRoleForTaskRoles",
        effect=iam.Effect.ALLOW,
        actions=["iam:PassRole"],
        resources=[
            f"arn:aws:iam::{account}:role/trikon-verify-task-role-*",
        ],
        conditions={
            "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"},
        },
    ))

    # Grant 3 — SQS Receive/Delete/GetAttrs on verify-jobs queue (Req 8.4).
    verify_jobs_queue.grant_consume_messages(role)  # expands to the 3 actions.

    # Grant 4 — SQS SendMessage on verify-jobs-dlq (Req 8.5).
    verify_jobs_dlq.grant_send_messages(role)

    # Grant 5 — ssm:GetParameter on /trikon/verify-runner/active-revision (Open Item b).
    active_revision_param.grant_read(role)

    # Grants 6-8 — Never-Fail-Open path (Open Item a).
    # Grant 6 — dynamodb:PutItem on trikon_verdicts with LeadingKeys condition.
    role.add_to_principal_policy(iam.PolicyStatement(
        sid="NeverFailOpenVerdictWrite",
        effect=iam.Effect.ALLOW,
        actions=["dynamodb:PutItem"],
        resources=[f"arn:aws:dynamodb:us-east-1:{account}:table/trikon_verdicts"],
        # NOTE: the LeadingKeys condition here uses the incoming installation_id
        # from the SqsJobMessage — not a principal tag, because the Lambda role
        # is not per-installation. Enforcement is via the message-level natural
        # key + the row-level LeadingKeys constraint that Spec 4 (dashboard)
        # relies on for read-side scoping. The policy uses
        # ForAllValues:StringEqualsIfExists on the LeadingKeys attribute to admit
        # every valid installation_id; a stricter alternative (dynamic policy per
        # installation) is out of scope for M1.
        conditions={
            "ForAllValues:StringLike": {
                "dynamodb:LeadingKeys": ["*"],   # any installation_id valid.
            },
        },
    ))

    # Grant 7 — secretsmanager:GetSecretValue on the App private key (for GitHub JWT).
    role.add_to_principal_policy(iam.PolicyStatement(
        sid="NeverFailOpenReadAppKey",
        effect=iam.Effect.ALLOW,
        actions=["secretsmanager:GetSecretValue"],
        resources=[app_private_key_secret_arn],
    ))

    # Grant 8 — kms:Decrypt on the KMS key that encrypts the App-key secret.
    # (AWS-owned KMS at M1 — no explicit grant needed; if we move to CMK later,
    # we add the grant here.)
    # Deferred to M2. Documented in §11.
```

Note on Grant 6's `LeadingKeys: ["*"]` wildcard: the orchestrator role is a *fleet* role, not a per-installation role, so we cannot substitute `${aws:PrincipalTag/installation_id}` (the role has no such tag). The runtime-side enforcement is that `never_fail_open.write_orchestrator_failure_verdict` writes rows whose partition key equals the incoming `sqs_message.installation_id` — no other value is possible in the code path, and the row shape is a frozen Pydantic model. The IAM grant here is permissive by necessity but the code path is tenant-scoped by construction. This is the same tradeoff Spec 1 makes for its `sqs:SendMessage` grant. If a future audit wants tighter IAM scoping, the alternative is a per-message `sts:AssumeRole` into a per-installation role — a ~200ms latency cost that we defer to M2 (§11).

### §8.3 IAM policy statements — Lifecycle_Handler role

Per Req 15.4-15.5. Each statement below is one `iam.PolicyStatement`.

```python
def _grant_lifecycle_permissions(
    role: iam.Role,
    *,
    lifecycle_queue: sqs.IQueue,
    lifecycle_dlq: sqs.IQueue,
    installations_table: dynamodb.ITableV2,
) -> None:
    account = Stack.of(role).account

    # Grant 1 — IAM management on trikon-verify-task-role-* (Req 15.4).
    role.add_to_principal_policy(iam.PolicyStatement(
        sid="IamManageTaskRoles",
        effect=iam.Effect.ALLOW,
        actions=[
            "iam:CreateRole",
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
            "iam:DeleteRole",
            "iam:GetRole",
            "iam:TagRole",     # for the installation_id tag (§6.4)
        ],
        resources=[
            f"arn:aws:iam::{account}:role/trikon-verify-task-role-*",
        ],
    ))

    # NOTE — Req 15.5: NO iam:PassRole. The lifecycle handler creates the roles
    # but never assumes or passes them. Explicitly not added.

    # Grant 2 — DynamoDB CRUD on the installations table (Req 15.4).
    installations_table.grant(
        role,
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    )

    # Grant 3 — SQS Receive/Delete/GetAttrs on installation-events queue.
    lifecycle_queue.grant_consume_messages(role)

    # Grant 4 — SQS SendMessage on installation-events DLQ.
    lifecycle_dlq.grant_send_messages(role)
```

### §8.4 CDK synth artifacts

`cdk synth` produces one CloudFormation template. Filename: `TrikonCloudOrchestratorStack.template.json` (single stack; both Lambdas + queue + table). Contract tests reference the template via `Template.from_stack(stack)` — no file-on-disk dependency.

### §8.5 CDK entrypoint

```python
# trikon_cloud/orchestrator/infra/app.py
import aws_cdk as cdk
from trikon_cloud.orchestrator.infra.orchestrator_stack import OrchestratorStack

app = cdk.App()
OrchestratorStack(
    app,
    "TrikonCloudOrchestratorStack",
    env=cdk.Environment(account=<from context>, region="us-east-1"),
)
app.synth()
```

---

## §9 Coordinated changes to other specs (scope-note section)

Both of the following are **NOT implementation tasks of this spec** — they are external dependencies that must land in coordinated PRs to Spec 1 and Spec 2 before Spec 3 can complete end-to-end testing. Both are small.

### §9.1 Spec 1 amendment (Webhook Receiver) — Requirement 12 routing branch

**Current state.** Spec 1's `handler._route_event` matches on `event_type` and routes `pull_request` and `check_run` events to `trikon-verify-jobs`. All other event types return HTTP 204.

**Amendment.** Add a routing branch for GitHub event headers `X-GitHub-Event: installation` and `X-GitHub-Event: installation_repositories`. On these events, build an `InstallationEventMessage` (§3.2) and send to `trikon-cloud-installation-events` via a second `SqsWriter` instance. Requires:
- New env var `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` on the webhook receiver Lambda.
- New IAM grant on the webhook receiver role: `sqs:SendMessage` scoped to `arn:aws:sqs:us-east-1:{account}:trikon-cloud-installation-events`.
- New `handler._build_installation_event_message(...) -> InstallationEventMessage` helper matching the model shape in §3.2.
- Updated Pydantic parse in Spec 1: the webhook receiver already treats the raw body as `bytes`; the amendment adds a second `GithubInstallationPayload` model (with `extra="allow"`) for parsing installation and installation_repositories events.

**Wire-format contract owned by this spec.** The `InstallationEventMessage` shape (§3.2) is authoritative. Spec 1's producer MUST serialize via `msg.model_dump_json()` — same discipline as Spec 1's existing `SqsJobMessage` writer.

**Gate for E2E testing.** End-to-end lifecycle testing (webhook → SQS → Lifecycle_Handler → IAM role in AWS console) is blocked until this amendment ships. Local tests for the Lifecycle_Handler use a synthetic `InstallationEventMessage` fixture — no dependency on the webhook receiver during unit tests.

### §9.2 Spec 2 amendment (Fargate Runner) — SSM parameter for active revision

**Current state.** Spec 2's `FargateRunnerStack` creates one `ecs.FargateTaskDefinition` for `trikon-verify-runner`. The task-definition revision integer is opaque — CDK emits `taskDefinition.taskDefinitionArn` referencing the latest revision at CDK synth time, but nothing writes the revision integer to a discoverable location.

**Amendment.** Spec 2's CDK stack must additionally:
- Read the newly-synthesized `taskDefinition` revision via CDK's `Fn::Select` on `Fn::Split(":", task_definition.task_definition_arn)`.
- Write it to SSM Parameter Store:
  ```python
  ssm.StringParameter(
      self, "VerifyRunnerActiveRevisionParam",
      parameter_name="/trikon/verify-runner/active-revision",
      string_value=Fn.select(6, Fn.split(":", task_definition.task_definition_arn)).split("/")[-1],
      description="Active revision of trikon-verify-runner task definition (written by Spec 2, read by Spec 3)",
  )
  ```
  (The exact CDK expression may vary — the required behavior is that every `cdk deploy` of Spec 2 updates this parameter to the newly-created revision integer.)
- Grant Spec 3's orchestrator role read access to the parameter — actually this reverse-direction grant is trivial because SSM parameter reads are IAM-scoped on the consumer side. No coordination needed here; Spec 3's CDK stack (§8.2 Grant 5) handles the read grant.

**Gate for E2E testing.** The orchestrator's `TaskDefinitionResolver` will fail cold-start with `ParameterNotFound` if this SSM parameter does not exist. Deployment order: Spec 2's amended stack deploys first, populates the SSM parameter, then Spec 3's stack deploys and its Lambda cold-starts successfully.

**Fallback for local development.** Unit tests for `TaskDefinitionResolver` stub the SSM client — no live parameter required.

---

## §10 Testing strategy

### §10.1 Unit test files (Orchestrator package)

Placed under `trikon_cloud/orchestrator/tests/`.

| File                              | Under test                                | Fixtures / mocks                                                    |
|-----------------------------------|-------------------------------------------|----------------------------------------------------------------------|
| `test_orchestrator_handler.py`    | `handler.lambda_handler` — happy path, Transient path, Terminal path, payload-gate, validation-gate | `moto` `@mock_aws` (SQS, ECS, DynamoDB, SSM); `respx` for the GitHub API |
| `test_ecs_dispatcher.py`          | `build_run_task_call`, `submit_run_task`, `classify_client_error`, `TaskDefinitionResolver` | Direct unit + `hypothesis` for random `SqsJobMessage` inputs         |
| `test_never_fail_open.py`         | `write_orchestrator_failure_verdict`, `build_synthetic_verdict_row`, `OrchestratorGithubClient` | `moto` for DynamoDB + Secrets Manager; `respx` for GitHub API        |
| `test_models.py`                  | `SqsEventEnvelope`, `RunTaskCall`, `OrchestratorEnvConfig` | Pydantic validation; `hypothesis`-generated valid/invalid dicts       |
| `test_logger.py`                  | `get_logger`, `append_job_context`, `LOGGING_DENYLIST` | Capture Powertools output via `caplog`                                |
| `test_infra_orchestrator_stack.py`| CDK stack synth — asserts on `Template`   | `aws_cdk.assertions.Template.from_stack(...)`                        |

### §10.2 Unit test files (Lifecycle package)

Placed under `trikon_cloud/installation_lifecycle/tests/`.

| File                                | Under test                                                                     | Fixtures / mocks                                    |
|-------------------------------------|--------------------------------------------------------------------------------|------------------------------------------------------|
| `test_lifecycle_handler.py`         | `lambda_handler` — four `event_type` branches + idempotent re-delivery         | `moto` for IAM + DynamoDB + SQS                      |
| `test_iam_provisioner.py`           | `IamProvisioner.provision`, `IamProvisioner.deprovision` — idempotency         | `moto` `@mock_iam`                                   |
| `test_dynamodb_writer.py`           | `InstallationsTableWriter` — CRUD, add/remove repos, conditional upsert        | `moto` `@mock_dynamodb`                              |
| `test_iam_template.py`              | `render_installation_policy_document`, `PolicyStatement`, `PolicyDocument`     | Pure — no AWS mocks. `hypothesis` on `installation_id` |
| `test_models.py`                    | `InstallationEventMessage`, `LifecycleTableRow`, `LifecycleEnvConfig`          | Pydantic validation                                  |
| `test_iam_policy_contract.py`       | **Byte-match against Spec 2's CDK synth** — Property P10                       | Imports Spec 2's `FargateRunnerStack`; uses `Template.from_stack` |
| `test_infra_lifecycle_stack.py`     | CDK stack synth — asserts on `Template` (same stack as orchestrator)           | `Template.from_stack`                                |

### §10.3 Coverage floors

- 90% branch coverage on: `orchestrator/handler.py`, `orchestrator/never_fail_open.py`, `installation_lifecycle/handler.py`, `installation_lifecycle/iam_template.py`.
- 80% branch coverage everywhere else in both packages.
- 100% branch coverage on the four error-classification tables (`_TRANSIENT_CODES`, `_TERMINAL_CODES`, the six-step never-fail-open flow, the five-step lifecycle flow) — every documented branch must have at least one test.

### §10.4 Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property tests use `hypothesis` with `@settings(max_examples=100, deadline=None)` per the workflow's PBT configuration guidance.

#### Property 1: `SqsJobMessage` round-trip via JSON

*For any* valid `SqsJobMessage` value `m`, `SqsJobMessage.model_validate_json(m.model_dump_json())` equals `m` structurally, and the deserialized value's field-declaration order matches the original.

**Validates: Requirements 1.2**

#### Property 2: `SqsJobMessage` malformed rejection

*For any* dict lacking one of the eight required fields declared in §3.1, `SqsJobMessage.model_validate(...)` raises `pydantic.ValidationError` and the `.errors()` output includes the location of the missing / malformed field.

**Validates: Requirements 1.2, 2.2**

#### Property 3: `delivery_id` byte-for-byte propagation through dispatch

*For any* valid `SqsJobMessage` `m`, the `RunTaskCall` produced by `build_run_task_call(m, ...)` carries `m.delivery_id` in exactly one place: `overrides.containerOverrides[0].environment` under the entry with `name == "TRIKON_DELIVERY_ID"`. The value is byte-identical to `m.delivery_id` — no normalization, no truncation.

**Validates: Requirements 1.3, 5.4**

#### Property 4: `RunTaskCall` shape invariant

*For any* valid `SqsJobMessage` `m` and any well-formed `OrchestratorEnvConfig`, the `RunTaskCall` produced by `build_run_task_call(m, env, task_definition)` satisfies:
1. `cluster == "trikon-verify-cluster"`, `launchType == "FARGATE"`, `count == 1`.
2. `networkConfiguration.awsvpcConfiguration.assignPublicIp == "DISABLED"`.
3. `overrides.taskRoleArn == f"arn:aws:iam::{env.trikon_aws_account_id}:role/trikon-verify-task-role-{m.installation_id}"`.
4. `overrides.containerOverrides` has exactly one entry with `name == "runner"`.
5. `overrides.containerOverrides[0].environment` has exactly seven entries whose `name` fields form the set `{TRIKON_INSTALLATION_ID, TRIKON_REPO_FULL_NAME, TRIKON_PR_NUMBER, TRIKON_HEAD_SHA, TRIKON_BASE_SHA, TRIKON_EVENT_TYPE, TRIKON_DELIVERY_ID}`.
6. `tags` has exactly three entries: `{"installation_id": str(m.installation_id)}`, `{"repo": m.repo_full_name}`, `{"pr": str(m.pr_number)}`.

**Validates: Requirements 3.2, 3.3, 3.4, 3.5, 5.1, 5.2, 5.3, 5.4, 5.5**

#### Property 5: `taskDefinition` is a pinned `family:revision`

*For any* SSM parameter value that parses as a positive integer, the `RunTaskCall.taskDefinition` produced by `TaskDefinitionResolver.resolve()` matches the regex `^trikon-verify-runner:\d+$` and never contains `:LATEST`. *For any* invocation of `TaskDefinitionResolver.resolve()` within a single container lifetime, `ssm:GetParameter` is called at most once — the second and subsequent calls return the cached value.

**Validates: Requirements 4.1, 4.2, 4.3**

#### Property 6: Transient vs Terminal classification is total and deterministic

*For any* `ClientError` with `response.Error.Code == code`, `classify_client_error(err)` returns `"terminal"` iff `code in _TERMINAL_CODES` and `"transient"` otherwise (including for codes in `_TRANSIENT_CODES` and for unknown codes — Req 6.4 fail-safe default).

**Validates: Requirements 6.1, 6.2, 6.4**

#### Property 7: Never-Fail-Open — synthetic verdict + neutral Check Run + idempotence

*For any* valid `SqsJobMessage` `m` and any `error_class`, `error_code` pair:

1. First invocation of `write_orchestrator_failure_verdict(sqs_message=m, ...)` writes exactly one row to `trikon_verdicts` with `installation_id = m.installation_id`, `sk = f"{m.sent_at}#{m.delivery_id}"`, `decision = "require_human"`, `matched_rule = "orchestrator terminal failure"`, and posts exactly one Check Run with `conclusion = "neutral"` on `m.head_sha`.

2. Second invocation of the same function with the same `sqs_message` produces zero additional rows in `trikon_verdicts` (`ConditionalCheckFailedException` handled as success) and MAY produce zero or one additional Check Run (GitHub's API is not deduplicating and this is acceptable per §7.1).

**Validates: Requirements 7.1, 7.2, 7.3, 7.4**

#### Property 8: `InstallationEventMessage` shape

*For any* dict conforming to the `InstallationEventMessage` schema (with all required fields, valid `event_type` value, positive `installation_id` and `github_app_id`), `InstallationEventMessage.model_validate(...)` returns a frozen model whose `.model_dump_json()` output, when re-validated, equals the original model. For any dict with an extra key not in the schema, validation raises `ValidationError` (extra="forbid").

**Validates: Requirements 11.1, 11.2, 11.4**

#### Property 9: Event-type routing is a total function on the four allowed values

*For any* `InstallationEventMessage` with a valid `event_type` value, `lambda_handler` dispatches to exactly one of `handle_installation_created`, `handle_installation_deleted`, `handle_repositories_added`, `handle_repositories_removed`, with no other handler invoked. The dispatch mapping is:

| `event_type`                          | Handler                        |
|---------------------------------------|--------------------------------|
| `installation.created`                | `handle_installation_created`  |
| `installation.deleted`                | `handle_installation_deleted`  |
| `installation_repositories.added`     | `handle_repositories_added`    |
| `installation_repositories.removed`   | `handle_repositories_removed`  |

**Validates: Requirements 12.1, 12.2, 12.3, 12.4**

#### Property 10: IAM policy runtime output byte-matches CDK synth

*For any* `installation_id ∈ [1, 2⁶³)` and any well-formed `app_private_key_secret_arn`, the canonical JSON of `render_installation_policy_document(installation_id, app_private_key_secret_arn=..., account_id="000000000000", region="us-east-1")` equals the canonical JSON of the CDK-synthesized `PolicyDocument` extracted from `FargateRunnerStack.build_task_role_for_installation(installation_id, app_private_key_secret_arn=...)` synthesized with the same account/region context.

`canonical` here means `json.dumps(json.loads(...), sort_keys=True, separators=(",", ":"))`.

**Validates: Requirements 13.1, 13.2, 13.3, 13.4**

#### Property 11: Lifecycle idempotency on re-delivery

*For any* `InstallationEventMessage` `m` and any of the four `event_type` values, invoking `lambda_handler` twice consecutively on the same message produces a state where:
- For `installation.created`: exactly one IAM role named `trikon-verify-task-role-{m.installation_id}` exists; the DynamoDB row has `status == "active"`; no exception is raised on the second invocation (`EntityAlreadyExistsException` on IAM and `ConditionalCheckFailedException` on DynamoDB are handled as success).
- For `installation.deleted`: no IAM role exists (or an already-absent one is unchanged); the DynamoDB row has `status == "disabled"`; no exception is raised on the second invocation.
- For `installation_repositories.added`: the DynamoDB `repositories` set is a superset of `m.repositories`; the second invocation is a no-op (DynamoDB `ADD` on set is idempotent).
- For `installation_repositories.removed`: the DynamoDB `repositories` set contains none of `m.repositories`; the second invocation is a no-op.

**Validates: Requirements 14.2, 14.3, 14.4, 14.5**

#### Property 12: Structured log context propagation

*For any* successfully parsed `SqsJobMessage` `m`, every log record emitted by the Orchestrator_Handler after `append_job_context(logger, message=m)` carries the five keys `installation_id`, `repo_full_name`, `pr_number`, `delivery_id`, `event_type` with values byte-identical to the corresponding fields on `m`. No log record at any level contains a key in `LOGGING_DENYLIST` (raw message body, ECS response body, credential material).

**Validates: Requirements 9.2, 9.3, 9.4**

### §10.5 Contract test — `test_iam_policy_contract.py`

Implements Property 10. The test is a single parametrized case at `installation_id=12345678` (per Req 13.4) plus a `hypothesis`-generated variant sweep across 100 random `installation_id` values to catch any installation-id-dependent drift.

```python
import json
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack
from trikon_cloud.installation_lifecycle.iam_template import (
    render_installation_policy_document,
    canonical_json,
)


def _canonical_from_cdk(installation_id: int, app_key_arn: str) -> str:
    app = App()
    stack = FargateRunnerStack(app, "TestStack", env=Environment(account="000000000000", region="us-east-1"))
    role = stack.build_task_role_for_installation(installation_id, app_key_arn)
    template = Template.from_stack(stack)
    resources = template.find_resources("AWS::IAM::Policy")
    # Filter to the policy attached to this specific role — one per test.
    (_, policy) = next(iter(resources.items()))
    doc = policy["Properties"]["PolicyDocument"]
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def test_iam_policy_byte_matches_cdk_synth() -> None:
    installation_id = 12345678
    app_key_arn = "arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef"

    cdk_canonical = _canonical_from_cdk(installation_id, app_key_arn)
    rt_doc = render_installation_policy_document(
        installation_id,
        app_private_key_secret_arn=app_key_arn,
        account_id="000000000000",
        region="us-east-1",
    )
    rt_canonical = canonical_json(rt_doc)

    assert rt_canonical == cdk_canonical
```

### §10.6 mypy and ruff

- `uv run mypy --strict trikon_cloud/orchestrator/ trikon_cloud/installation_lifecycle/` — zero errors (Req 17.1).
- `uv run ruff check trikon_cloud/orchestrator/ trikon_cloud/installation_lifecycle/` — clean.
- Per-module `mypy: strict` header at the top of every `.py` file (belt-and-suspenders — makes it obvious in review that a file is under strict typing).

### §10.7 Property-test tagging (per workflow guidance)

Every property test's docstring carries:

> `Feature: trikon-cloud-orchestrator, Property N: <property title>`

so that a `pytest --collect-only -q | grep "Property"` scan enumerates the coverage of the Correctness Properties section back to individual test cases.

---

## §11 Deferred to M2 (out of scope)

The following are explicitly not implemented in this spec. Each has an M2 or later home.

1. **Check Run edits.** The Never-Fail-Open path only *creates* a neutral Check Run. It never *edits* an existing Check Run. Spec 2's verify path handles Check Run edits when a re-run succeeds. The orchestrator's role is bounded: it creates one neutral Check Run on terminal dispatch failure and moves on. If a subsequent redelivery succeeds (Spec 2 dispatches, runs, and posts a real verdict), Spec 2's Check Run creation on the same `head_sha` will appear as a second Check Run on the PR — GitHub renders both, which is the desired signal that the platform recovered.

2. **Cross-region failover.** Both Lambdas and all their dependencies (SQS queues, DynamoDB tables, Secrets Manager secret, SSM parameter) live in `us-east-1` only. Multi-region resilience — including cross-region DynamoDB replication, cross-region SQS mirroring, and a Route 53 failover pattern for the GitHub webhook endpoint — is deferred to M3 or later.

3. **`check_run.rerequested` handling.** Per architecture memo §M1 decisions (b), M1 does NOT support the GitHub `check_run.rerequested` event. Spec 1 does not route this event to `trikon-verify-jobs`. Spec 3's orchestrator therefore never dispatches a re-request. Deferred to M2.

4. **Dashboard integration (Spec 4).** The `trikon_verdicts` rows this spec's Never-Fail-Open path writes are readable by the M2 dashboard using the standard GSI1/GSI2 read pattern Spec 2 already documents. No coordination is required at this spec's implementation layer — the row shape is the contract, and the row shape is byte-compatible with Spec 2's `VerdictRow`.

5. **Per-invocation IAM role assumption for tighter Never-Fail-Open scoping.** §8.2 Grant 6 uses `LeadingKeys: ["*"]` — a fleet-wide permit. A tighter alternative (`sts:AssumeRole` into the per-installation task role before every synthetic verdict write) would add ~200 ms of latency to the Never-Fail-Open path. We defer this hardening to M2 pending audit feedback.

6. **KMS Customer-Managed Keys.** The App private key secret uses an AWS-owned KMS key at M1. Migration to per-installation Customer-Managed Keys (CMK) is deferred to M2 (architecture memo §6). No `kms:Decrypt` grant is needed on the orchestrator role at M1; adding one at M2 is a single line in `_grant_orchestrator_permissions`.

7. **CloudWatch alarms and dashboards.** This spec creates the Lambdas but does not create the CloudWatch alarms that page on-call when the DLQ depth exceeds threshold, or the CloudWatch dashboard that visualizes dispatch latency and terminal-error rate. Both are deferred to a shared observability spec.

8. **Batch size > 1 optimization.** SQS event-source mapping is fixed at batch size 1 (Req 1.1 and 10.1). A future optimization would batch multiple `ecs.RunTask` calls per invocation to reduce Lambda cold-start amortization. Deferred pending a cost-vs-latency measurement in production.
