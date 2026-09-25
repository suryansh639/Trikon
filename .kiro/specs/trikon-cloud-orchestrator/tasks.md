# Implementation Plan: trikon-cloud-orchestrator

## Overview

M1 Spec 3 of Trikon Cloud — the two SQS-triggered Python 3.11 Lambdas that bridge Spec 1's webhook receiver and Spec 2's Fargate runner. Ships the `trikon_cloud/orchestrator/` package (dispatcher on `trikon-verify-jobs` with Never-Fail-Open on terminal `ecs:RunTask` failure) and the `trikon_cloud/installation_lifecycle/` package (installation-events consumer that provisions per-installation IAM task roles byte-matched against Spec 2's CDK synth), together with one `OrchestratorStack` CDK stack, per `.kiro/specs/trikon-cloud-orchestrator/design.md`.

Execution proceeds in seven waves. **Wave 0** (task 1) lands the two package skeletons and confirms `pyproject.toml` already covers them via Spec 1's `packages = ["trikon", "trikon_cloud"]`. **Wave 1** (tasks 2–6) lands the pure/leaf modules — models and loggers for both packages, plus `iam_template.py` — in parallel. **Wave 2** (tasks 7–10) lands the middle tier — `ecs_dispatcher`, `never_fail_open`, `iam_provisioner`, `dynamodb_writer`. **Wave 3** (tasks 11–12) lands the two Lambda entrypoints. **Wave 4** (tasks 13–15) lands the CDK stack, its `app.py`, and the infra README. **Wave 5** (tasks 16–19) lands the test conftest, per-module unit tests, and the CDK-synth byte-match contract test (task 19). **Wave 6** (task 20) is the checkpoint — mypy `--strict`, ruff clean, `cdk synth` green, coverage floors, and the contract test green.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Land the `trikon_cloud/orchestrator/` and `trikon_cloud/installation_lifecycle/` package skeletons
  - [x] 1.1 Create the two package skeletons plus their `infra/` and `tests/` subpackage markers
    - Create `trikon_cloud/orchestrator/__init__.py` with `__all__: list[str] = []`.
    - Create `trikon_cloud/orchestrator/infra/__init__.py` and `trikon_cloud/orchestrator/tests/__init__.py` as empty package-marker files.
    - Create `trikon_cloud/installation_lifecycle/__init__.py` with `__all__: list[str] = []`.
    - Create `trikon_cloud/installation_lifecycle/tests/__init__.py` as an empty marker file.
    - No concrete `.py` module beyond the four `__init__.py` files at this task — the modules land in Waves 1 through 4.
    - Verify by running `uv run python -c "import trikon_cloud.orchestrator, trikon_cloud.installation_lifecycle"` from the repo root — both packages must import cleanly.
    - _Requirements: 16.3, 17.1, 18.1_
    - _Design: §2.1_

  - [x] 1.2 Confirm `pyproject.toml` package coverage and record the `cloud-runtime` dep intent
    - Do NOT edit `[tool.hatch.build.targets.wheel] packages` — Spec 1 already set it to `["trikon", "trikon_cloud"]`, which covers both new subpackages under `trikon_cloud/`.
    - Do NOT bump `[project] version` (Invariant 7-scoped release discipline).
    - Confirm the existing `cloud` optional-dep group from Spec 1 already declares `aws-lambda-powertools[all]>=3,<4`, `pydantic>=2.9,<3`, `pydantic-settings>=2,<3`, `boto3>=1.35,<2`, `botocore>=1.35,<2`, `httpx>=0.27,<0.29`, `PyJWT[crypto]>=2.9,<3`. If any is missing, extend the existing `cloud` group in place — do not create a new group. The Powertools upper bound (`<4`) satisfies Requirement 16.2's bounded-major constraint.
    - Verify `uv sync --extra cloud` succeeds and `uv.lock` is unchanged if all deps are already present.
    - Verify `uv run mypy --strict trikon_cloud/orchestrator/__init__.py trikon_cloud/installation_lifecycle/__init__.py` exits 0.
    - _Requirements: 16.2, 16.3, 17.1_
    - _Design: §2.1, §2.3_

- [x] 2. Create `trikon_cloud/orchestrator/models.py` — SQS envelope, `RunTaskCall`, and `OrchestratorEnvConfig`
  - Declare `__all__` covering `SqsEventEnvelope`, `SqsRecord`, `SqsRecordAttributes`, `SqsBatchResponse`, `BatchItemFailure`, `RunTaskCall`, `NetworkConfiguration`, `AwsvpcConfiguration`, `RunTaskOverrides`, `ContainerOverride`, `EnvOverride`, `EcsTag`, `OrchestratorEnvConfig`.
  - Every model is a `pydantic.BaseModel` subclass with `model_config = ConfigDict(frozen=True, extra="forbid")` — except `SqsRecordAttributes` which uses `extra="allow"` (AWS may add fields) and `OrchestratorEnvConfig` which is a `pydantic_settings.BaseSettings` subclass with `SettingsConfigDict(extra="ignore", frozen=True)`.
  - `RunTaskCall` types the five top-level fields with `Literal` where the shape is fixed: `cluster: Literal["trikon-verify-cluster"]`, `launchType: Literal["FARGATE"]`, `count: Literal[1]`, plus `taskDefinition: str` (family:revision), `networkConfiguration`, `overrides`, and `tags: tuple[EcsTag, EcsTag, EcsTag]` — exactly three, order-significant per Requirement 3.5.
  - `AwsvpcConfiguration.assignPublicIp: Literal["DISABLED"]`. `ContainerOverride.name: Literal["runner"]`. `ContainerOverride.environment: tuple[EnvOverride, ...]` — the seven-entry constraint is enforced by `build_run_task_call` in task 7, not by the model, so unit tests can construct partial fixtures.
  - `OrchestratorEnvConfig` declares the eleven env-var aliases per design.md §2.3: `TRIKON_AWS_ACCOUNT_ID` (12-digit `str`), `TRIKON_RUNNER_SUBNET_IDS` and `TRIKON_RUNNER_SECURITY_GROUP_IDS` (comma-separated `str` parsed to `tuple[str, ...]` via a `@model_validator(mode="after")`), `TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM` (default `"/trikon/verify-runner/active-revision"`), `TRIKON_VERIFY_JOBS_DLQ_URL`, `TRIKON_VERDICTS_TABLE` (default `"trikon_verdicts"`), `TRIKON_APP_PRIVATE_KEY_SECRET_ARN`, `TRIKON_APP_ID` (`int`, `ge=1`), `TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE` (default `"https://cloud.trikon.dev/audits/{delivery_id}"`), `AWS_REGION` (default `"us-east-1"`), `TRIKON_LOG_LEVEL` (default `"INFO"`).
  - `BatchItemFailure.itemIdentifier: str`. `SqsBatchResponse.batchItemFailures: tuple[BatchItemFailure, ...]`.
  - No `dict[str, Any]`, `list[Any]`, `tuple[Any, ...]`, or `object` on any public signature (Requirement 17.2). Serialization uses `by_alias=True` on the models with hyphen/PascalCase aliases (`AwsvpcConfiguration`, `NetworkConfiguration`) so boto3 kwargs match.
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/models.py` and `uv run ruff check trikon_cloud/orchestrator/models.py` — both exit 0.
  - _Requirements: 3.2, 3.3, 3.4, 3.5, 5.1, 5.2, 5.3, 5.4, 5.5, 17.2, 17.3, 18.1_
  - _Design: §2.2, §2.3, §3.3, §3.4_

- [x] 3. Create `trikon_cloud/orchestrator/logger.py` — Powertools wrapper and `LOGGING_DENYLIST`
  - Declare `__all__ = ["get_logger", "append_job_context", "LOGGING_DENYLIST"]`.
  - Import `aws_lambda_powertools.Logger` and `from trikon_cloud.webhook_receiver.models import SqsJobMessage`.
  - Module-level singleton: `_LOGGER = Logger(service="trikon-cloud-orchestrator", log_uncaught_exceptions=True)`. `get_logger()` returns `_LOGGER`. `service` prefix is the literal `"trikon-cloud"` per Requirement 18.2.
  - `LOGGING_DENYLIST: frozenset[str] = frozenset({"body", "raw_body", "sqs_body", "response", "ecs_response", "secret_value", "app_private_key", "installation_token", "app_jwt", "aws_credentials"})` — the field names that must never appear as log keys per Invariant 6 / Requirement 9.3.
  - `append_job_context(logger: Logger, *, message: SqsJobMessage) -> None` calls `logger.append_keys(installation_id=message.installation_id, repo_full_name=message.repo_full_name, pr_number=message.pr_number, delivery_id=message.delivery_id, event_type=message.event_type)` — the five Structured log field set entries per Requirement 9.2.
  - Do NOT introduce a Powertools log filter that mutates records — the denylist is enforced by convention plus a `test_logger.py` guard that asserts no log site references any key in the denylist (`test_logger.py` grep-scan of the package's `.py` sources).
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/logger.py` and `uv run ruff check trikon_cloud/orchestrator/logger.py`.
  - _Requirements: 9.1, 9.2, 9.3, 17.2, 18.2_
  - _Design: §2.2, §7.4_

- [x] 4. Create `trikon_cloud/installation_lifecycle/models.py` — `InstallationEventMessage`, `LifecycleTableRow`, `LifecycleEnvConfig`
  - Declare `__all__ = ["InstallationEventMessage", "LifecycleTableRow", "LifecycleEnvConfig"]`.
  - `InstallationEventMessage` per design.md §3.2 verbatim: `model_config = ConfigDict(extra="forbid", frozen=True)`; `installation_id: int = Field(ge=1)`; `github_app_id: int = Field(ge=1)`; `event_type: Literal["installation.created", "installation.deleted", "installation_repositories.added", "installation_repositories.removed"]`; `repositories: tuple[str, ...]`; `sent_at: str`; `delivery_id: str`. All required — no defaults.
  - `LifecycleTableRow` — `model_config = ConfigDict(frozen=True, extra="forbid")`; `installation_id: int`; `status: Literal["active", "disabled"]`; `github_app_id: int`; `created_at: str`; `updated_at: str`; `repositories: frozenset[str]`.
  - `LifecycleEnvConfig` — `pydantic_settings.BaseSettings` with `SettingsConfigDict(extra="ignore", frozen=True)`; declares the five aliases per design.md §2.3: `TRIKON_AWS_ACCOUNT_ID: str`, `TRIKON_APP_PRIVATE_KEY_SECRET_ARN: str`, `TRIKON_INSTALLATIONS_TABLE: str = "trikon-cloud-installations"`, `AWS_REGION: str = "us-east-1"`, `TRIKON_LOG_LEVEL: str = "INFO"`.
  - The `InstallationEventMessage` module MUST NOT declare any field typed as `dict[str, Any]`, `list[Any]`, or `object` on any public signature (Requirement 11.4 and 17.2).
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/models.py` and `uv run ruff check trikon_cloud/installation_lifecycle/models.py`.
  - _Requirements: 11.1, 11.2, 11.4, 17.2, 17.4, 18.1_
  - _Design: §2.2, §2.3, §3.2_

- [x] 5. Create `trikon_cloud/installation_lifecycle/logger.py` — Powertools wrapper for the Lifecycle_Handler
  - Declare `__all__ = ["get_logger", "append_lifecycle_context", "LOGGING_DENYLIST"]`.
  - Module-level singleton: `_LOGGER = Logger(service="trikon-cloud-installation-lifecycle", log_uncaught_exceptions=True)`. `get_logger()` returns `_LOGGER`. `service` prefix is `"trikon-cloud"` per Requirement 18.2.
  - `LOGGING_DENYLIST: frozenset[str] = frozenset({"body", "raw_body", "sqs_body", "app_private_key", "aws_credentials"})` — mirror of the orchestrator's denylist, scoped to the lifecycle handler's smaller IO surface.
  - `append_lifecycle_context(logger: Logger, *, message: InstallationEventMessage) -> None` — appends `installation_id`, `event_type`, `delivery_id` per Requirement 15.2.
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/logger.py` and `uv run ruff check trikon_cloud/installation_lifecycle/logger.py`.
  - _Requirements: 15.2, 17.2, 18.2_
  - _Design: §2.2_

- [x] 6. Create `trikon_cloud/installation_lifecycle/iam_template.py` — pure IAM renderer + `canonical_json`
  - Declare `__all__ = ["render_installation_policy_document", "render_installation_assume_role_policy_document", "canonical_json", "InstallationPolicyDocument", "PolicyStatement", "PolicyCondition", "AssumeRolePolicyDocument", "AssumeRolePolicyStatement"]`.
  - Implement the Pydantic model hierarchy per design.md §6.2 verbatim: `PolicyCondition` with `for_all_values_string_equals: dict[str, tuple[str, ...]] = Field(alias="ForAllValues:StringEquals")` and `ConfigDict(frozen=True, extra="forbid", populate_by_name=True)`; `PolicyStatement` with `Sid: str`, `Effect: Literal["Allow"]`, `Action: tuple[str, ...]`, `Resource: str`, `Condition: PolicyCondition | None = None`; `InstallationPolicyDocument` with `Version: Literal["2012-10-17"]` and `Statement: tuple[PolicyStatement, PolicyStatement, PolicyStatement, PolicyStatement]` (fixed-length four, order-significant); `AssumeRolePolicyStatement` with `Effect: Literal["Allow"]`, `Principal: dict[str, str]`, `Action: Literal["sts:AssumeRole"]`; `AssumeRolePolicyDocument` with `Version: Literal["2012-10-17"]` and `Statement: tuple[AssumeRolePolicyStatement]`.
  - `render_installation_policy_document(installation_id: int, *, app_private_key_secret_arn: str, account_id: str, region: str) -> InstallationPolicyDocument` builds the four Statements in the exact order from design.md §6.3: (0) `DynamoDBVerdictsScopedToInstallation` on `arn:aws:dynamodb:{region}:{account_id}:table/trikon_verdicts` with `dynamodb:LeadingKeys=["${aws:PrincipalTag/installation_id}"]`; (1) `DynamoDBPrStateScopedToInstallation` on `arn:aws:dynamodb:{region}:{account_id}:table/trikon_pr_state` with the same LeadingKeys condition; (2) `S3EvidenceSpillScopedToInstallation` on `arn:aws:s3:::trikon-cloud-evidence/${aws:PrincipalTag/installation_id}/*` — NO Condition block, the prefix is baked into the Resource ARN; (3) `SecretsManagerAppPrivateKeyRead` on the passed `app_private_key_secret_arn`.
  - Every `Action` field is `tuple[str, ...]` — single-element on Statements 0 and 3 (serializes to a one-element JSON list, which is what CDK synth emits — byte-match hinges on list form, not string form).
  - `render_installation_assume_role_policy_document()` returns the fixed `sts:AssumeRole` policy for `Principal={"Service": "ecs-tasks.amazonaws.com"}` per design.md §6.2.
  - `canonical_json(doc: InstallationPolicyDocument | AssumeRolePolicyDocument) -> str` returns `json.dumps(json.loads(doc.model_dump_json(by_alias=True)), sort_keys=True, separators=(",", ":"))` per design.md §6.5. Pure function — no IO, no side effects.
  - This module is pure: no `boto3`, no `httpx`, no filesystem, no environment reads.
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/iam_template.py` and `uv run ruff check trikon_cloud/installation_lifecycle/iam_template.py`.
  - _Requirements: 13.1, 13.2, 13.3, 17.4, 18.1_
  - _Design: §6.1, §6.2, §6.3, §6.4, §6.5_

- [x] 7. Create `trikon_cloud/orchestrator/ecs_dispatcher.py` — `build_run_task_call`, `submit_run_task`, `TaskDefinitionResolver`, `classify_client_error`
  - Declare `__all__` covering `build_run_task_call`, `submit_run_task`, `classify_client_error`, `TaskDefinitionResolver`, `RunTaskDispatchResult`, `RunTaskResponse`, `SsmGetParameterResponse`, `EcsClientProtocol`, `SsmClientProtocol`, `TransientDispatchError`, `TerminalDispatchError`.
  - `build_run_task_call(message: SqsJobMessage, *, env: OrchestratorEnvConfig, task_definition: str) -> RunTaskCall` is PURE — no IO. Populates every field of `RunTaskCall` per the concrete payload in design.md §3.3: `cluster="trikon-verify-cluster"`, `launchType="FARGATE"`, `count=1`, `taskDefinition=task_definition` (fully qualified `family:revision` from the resolver), `networkConfiguration` from `env.trikon_runner_subnet_ids` and `env.trikon_runner_security_group_ids`, `overrides.taskRoleArn=f"arn:aws:iam::{env.trikon_aws_account_id}:role/trikon-verify-task-role-{message.installation_id}"`, `overrides.containerOverrides` with exactly one entry `name="runner"` whose `environment` is the seven-entry `tuple[EnvOverride, ...]` in the fixed order `TRIKON_INSTALLATION_ID`, `TRIKON_REPO_FULL_NAME`, `TRIKON_PR_NUMBER`, `TRIKON_HEAD_SHA`, `TRIKON_BASE_SHA`, `TRIKON_EVENT_TYPE`, `TRIKON_DELIVERY_ID` — integer fields serialized via `str(...)`; string fields copied byte-for-byte with no normalization (Requirement 5.4).
  - `tags` is a `tuple[EcsTag, EcsTag, EcsTag]` in the fixed order: `installation_id`, `repo`, `pr` — Requirement 3.5.
  - `submit_run_task(call: RunTaskCall, *, ecs_client: EcsClientProtocol) -> RunTaskDispatchResult` invokes `ecs_client.run_task(**call.model_dump(by_alias=True, mode="json"))` exactly once (Requirement 3.1) and returns `RunTaskDispatchResult(task_arn=response.tasks[0].taskArn)`. `ClientError` from the boto3 client propagates unhandled — the handler catches and classifies.
  - `classify_client_error(err: ClientError) -> Literal["transient", "terminal"]` per design.md §4.2: `_TERMINAL_CODES = frozenset({"InvalidParameterException", "AccessDeniedException", "ClusterNotFoundException", "TaskDefinitionNotFound", "NoSuchEntity"})`; `_TRANSIENT_CODES = frozenset({"ThrottlingException", "RequestLimitExceeded", "ServiceUnavailable", "InternalFailure"})`; unknown codes classify as `"transient"` (Requirement 6.4 fail-safe default).
  - `TaskDefinitionResolver` per design.md §4.3: caches the resolved `family:revision` string in a module-scope `_cache: str | None` attribute for the container lifetime (single cold-start read, no refresh). `resolve(*, ssm_client: SsmClientProtocol) -> str` calls `ssm_client.get_parameter(Name=self._param_name)`, validates via `SsmGetParameterResponse.model_validate`, parses `Parameter.Value` as `int`, raises `ValueError` on `< 1`, and returns `f"trikon-verify-runner:{revision}"`. Never returns a value containing `:LATEST` (Requirement 4.2).
  - `EcsClientProtocol` and `SsmClientProtocol` are `typing.Protocol` subclasses with the minimal typed method signatures — no `Any`. `RunTaskResponse` and `SsmGetParameterResponse` are `BaseModel` subclasses with `extra="allow"` so AWS response drift is non-breaking.
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/ecs_dispatcher.py` and `uv run ruff check trikon_cloud/orchestrator/ecs_dispatcher.py`.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.4, 17.2, 17.3_
  - _Design: §2.2, §3.3, §4.2, §4.3_

- [x] 8. Create `trikon_cloud/orchestrator/never_fail_open.py` — synthetic verdict writer + neutral Check Run poster
  - Declare `__all__ = ["write_orchestrator_failure_verdict", "build_synthetic_verdict_row", "OrchestratorGithubClient", "OrchestratorGithubClientError"]`.
  - Import `from trikon_cloud.fargate_runner.models import VerdictRow` — reuse Spec 2's model verbatim per design.md §3.5. Do NOT define a new verdict model.
  - `build_synthetic_verdict_row(*, sqs_message: SqsJobMessage, error_class: str, error_code: str) -> VerdictRow` is PURE. Populates every field per the table in design.md §3.5: `installation_id=sqs_message.installation_id`, `sk=f"{sqs_message.sent_at}#{sqs_message.delivery_id}"`, `decision="require_human"`, `matched_rule="orchestrator terminal failure"`, `blast_radius_score=0`, `new_errors=0`, `new_warnings=0`, `preexisting_errors=0`, `fargate_task_arn="n/a-orchestrator-terminal"`, `schema_version=2`, `evidence_blob=gzip(json.dumps({"error_class": error_class, "error_code": error_code, "delivery_id": sqs_message.delivery_id}, sort_keys=True, separators=(",", ":")).encode("utf-8"))`, `evidence_s3_key=None`, `risk_bucket_sk=f"0000#{sqs_message.sent_at}"`. `duration_ms` is passed by the caller.
  - `write_orchestrator_failure_verdict(*, sqs_message: SqsJobMessage, error_class: str, error_code: str, boto3_session: boto3.session.Session, github_client: OrchestratorGithubClient, env: OrchestratorEnvConfig) -> None` executes the six-step body from design.md §7.2: (1) build the row, (2) `dynamodb.put_item(TableName=env.trikon_verdicts_table, Item=<coerced>, ConditionExpression="attribute_not_exists(installation_id) AND attribute_not_exists(sk)")` — Requirement 7.4, byte-identical to Spec 2's writer; (3) on `ConditionalCheckFailedException` log INFO `verdict_already_exists` and return (idempotency success); on any other `ClientError` propagate; (4) log INFO `orchestrator_verdict_written`; (5) call `github_client.create_neutral_check_run(installation_id=..., repo_full_name=..., head_sha=..., details_url=env.trikon_check_run_details_url_template.format(delivery_id=sqs_message.delivery_id))`; (6) log INFO `orchestrator_failure_resolved`.
  - Check Run POST failures (retry-budget exhaustion or 4xx) MUST be caught and logged at ERROR without re-raising — the DynamoDB write already satisfies Invariant 2 (Requirement 7.2 partial-success semantics per design.md §4.5 and §7.1).
  - `_to_dynamodb_item(row: VerdictRow) -> dict[str, dict[str, str | int | bytes]]` is a private helper — scoped to method bodies, not exported. Maps `VerdictRow` fields to the boto3 `{"S":…, "N":…, "B":…}` attribute-value shape.
  - `OrchestratorGithubClient` per design.md §7.3: `__init__(*, app_id: int, app_private_key_secret_arn: str, secrets_client: SecretsClientProtocol, http_client: httpx.Client)`. Method `create_neutral_check_run(*, installation_id: int, repo_full_name: str, head_sha: str, details_url: str) -> None` mints an installation token via `_get_installation_token(installation_id)` (cached in a module-scope `_TOKEN_CACHE: dict[int, tuple[str, datetime]]` with 5-minute safety margin), then `POST /repos/{repo_full_name}/check-runs` with body `{"name": "Trikon", "head_sha": head_sha, "status": "completed", "conclusion": "neutral", "output": {"title": "Trikon Cloud verification unavailable", "summary": <template>, "text": None}, "details_url": details_url}`. `name` is exactly `"Trikon"` per Invariant 7.
  - Retry policy on the Check Run POST: 3 attempts, exponential backoff 0.5/1/2 seconds with jitter, 30-second total budget. Retry on 500/502/503/504/429. On 4xx or budget exhaustion raise `OrchestratorGithubClientError`.
  - The module MUST NOT log the App private key PEM, the App JWT, the installation token, the Secrets Manager response body, or the raw `ecs.RunTask` response body at any level (Invariant 6).
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/never_fail_open.py` and `uv run ruff check trikon_cloud/orchestrator/never_fail_open.py`.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 17.2, 18.2_
  - _Design: §3.5, §4.4, §7.1, §7.2, §7.3_

- [x] 9. Create `trikon_cloud/installation_lifecycle/iam_provisioner.py` — wraps `iam:CreateRole` / `PutRolePolicy` / `DeleteRole`
  - Declare `__all__ = ["IamProvisioner", "IamClientProtocol", "ProvisionResult", "DeprovisionResult"]`.
  - `IamClientProtocol` is a `typing.Protocol` with typed `create_role`, `put_role_policy`, `delete_role_policy`, `delete_role`, `get_role` method signatures — no `Any` return types.
  - `ProvisionResult` is `BaseModel` with `model_config = ConfigDict(frozen=True, extra="forbid")`; `role_arn: str`; `already_existed: bool`.
  - `DeprovisionResult` is `BaseModel` with `role_name: str`; `already_absent: bool`.
  - `IamProvisioner.__init__(*, iam_client: IamClientProtocol)`.
  - `provision(installation_id: int, *, policy: InstallationPolicyDocument, assume_role_policy: AssumeRolePolicyDocument) -> ProvisionResult` per design.md §5.2 idempotency semantics: calls `iam_client.create_role(RoleName=f"trikon-verify-task-role-{installation_id}", AssumeRolePolicyDocument=canonical_json(assume_role_policy), Tags=[{"Key": "installation_id", "Value": str(installation_id)}])`; on `ClientError` with code `EntityAlreadyExistsException` sets `already_existed=True` and continues (Requirement 14.2); on any other code re-raises. Then calls `iam_client.put_role_policy(RoleName=..., PolicyName="TrikonInstallationPolicy", PolicyDocument=canonical_json(policy))` — inline policy name is fixed to `"TrikonInstallationPolicy"` per design.md §5.3.
  - `deprovision(installation_id: int) -> DeprovisionResult` per design.md §5.3: calls `iam_client.delete_role_policy(RoleName=..., PolicyName="TrikonInstallationPolicy")` — order matters, inline policy MUST be deleted before the role or `iam:DeleteRole` fails with `DeleteConflict`; catches `NoSuchEntity` as `already_absent` on either leg; then calls `iam_client.delete_role(RoleName=...)` with the same idempotency handling. Returns `DeprovisionResult(role_name=..., already_absent=<both_absent>)` per Requirement 14.4.
  - The provisioner never assumes or passes the created roles (Requirement 15.5).
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/iam_provisioner.py` and `uv run ruff check trikon_cloud/installation_lifecycle/iam_provisioner.py`.
  - _Requirements: 12.1, 12.2, 13.5, 14.2, 14.4, 15.5, 17.2, 18.4_
  - _Design: §2.2, §5.2, §5.3, §5.5_

- [x] 10. Create `trikon_cloud/installation_lifecycle/dynamodb_writer.py` — CRUD on `trikon-cloud-installations`
  - Declare `__all__ = ["InstallationsTableWriter", "DynamoDbClientProtocol", "UpsertResult"]`.
  - `DynamoDbClientProtocol` is a `typing.Protocol` with typed `put_item`, `get_item`, `update_item`, `delete_item` method signatures.
  - `UpsertResult` is `BaseModel` with `model_config = ConfigDict(frozen=True, extra="forbid")`; `already_active: bool`.
  - `InstallationsTableWriter.__init__(*, ddb_client: DynamoDbClientProtocol, table_name: str)`.
  - `upsert_active(installation_id: int, *, github_app_id: int, repositories: frozenset[str], now_iso: str) -> UpsertResult` uses `put_item(TableName=self._table_name, Item={...}, ConditionExpression="attribute_not_exists(installation_id) OR #s <> :active", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":active": {"S": "active"}})`; on `ConditionalCheckFailedException` return `UpsertResult(already_active=True)` per design.md §5.2 and Requirement 14.3; on any other `ClientError` re-raise.
  - `mark_disabled(installation_id: int, *, now_iso: str) -> None` uses `update_item(TableName=..., Key={"installation_id": {"N": str(installation_id)}}, UpdateExpression="SET #s = :disabled, updated_at = :now", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":disabled": {"S": "disabled"}, ":now": {"S": now_iso}})` — no `ConditionExpression`, so an update on a missing key creates a "disabled" sentinel row (Requirement 14.4 semantics per design.md §5.3).
  - `add_repositories(installation_id: int, *, repositories: frozenset[str], now_iso: str) -> None` uses `update_item(..., UpdateExpression="ADD repositories :repos SET updated_at = :now", ExpressionAttributeValues={":repos": {"SS": sorted(repositories)}, ":now": {"S": now_iso}})`. DynamoDB `ADD` on a set is idempotent per Requirement 14.5 and design.md §5.4.
  - `remove_repositories(installation_id: int, *, repositories: frozenset[str], now_iso: str) -> None` uses `update_item(..., UpdateExpression="DELETE repositories :repos SET updated_at = :now", ExpressionAttributeValues={":repos": {"SS": sorted(repositories)}, ":now": {"S": now_iso}})`. DynamoDB `DELETE` on a set is idempotent per Requirement 14.5.
  - The internal DynamoDB attribute-value dicts (`{"S":…, "N":…, "SS":…}`) are scoped to method bodies — they never cross a module boundary (Invariant 8 / Requirement 17.2).
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/dynamodb_writer.py` and `uv run ruff check trikon_cloud/installation_lifecycle/dynamodb_writer.py`.
  - _Requirements: 12.3, 12.4, 14.1, 14.3, 14.5, 17.2_
  - _Design: §2.2, §5.2, §5.3, §5.4, §5.5_

- [x] 11. Create `trikon_cloud/orchestrator/handler.py` — `lambda_handler` with the eight-step flow
  - Declare `__all__ = ["lambda_handler"]`. Import every sibling module's public symbols plus `SqsJobMessage` from `trikon_cloud.webhook_receiver.models`.
  - Module-scope cold-start caches per design.md §4.1: `_ENV: OrchestratorEnvConfig | None = None`, `_RESOLVER: TaskDefinitionResolver | None = None`, `_BOTO_SESSION: boto3.session.Session | None = None`, `_GITHUB_CLIENT: OrchestratorGithubClient | None = None`. Lazy-initialized inside `lambda_handler` (or via a private `_bootstrap()` helper) — first invocation pays the init cost, subsequent invocations reuse.
  - `lambda_handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]` implements the eight steps from design.md §4.1 verbatim: (1) attach `sqs_message_id` and `approximate_receive_count` to Powertools log context BEFORE any parse so payload-gate failures still carry SQS context; (2) payload-size gate — if `len(record.body.encode("utf-8")) > 262_144` log `payload_exceeds_256kb` ERROR and return `{"batchItemFailures": []}` (Requirement 2.1 — Terminal, delete from queue); (3) parse `SqsJobMessage.model_validate_json(record.body)` — on `ValidationError` log `malformed_sqs_body` ERROR with `errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]` and return `{"batchItemFailures": []}` (Requirement 2.2); (4) `append_job_context(logger, message=message)` — Requirement 9.2; (5) resolve task-def revision via `_RESOLVER.resolve(ssm_client=...)` — Requirement 4.1 open-item-b resolution; (6) `call = build_run_task_call(message, env=env, task_definition=task_definition)` — pure; (7) `try: result = submit_run_task(call, ecs_client=...)` — on `ClientError` classify via `classify_client_error`; on Transient log `run_task_transient_failure` WARN and `raise TransientDispatchError` (Requirement 6.1); on Terminal log `run_task_terminal_failure` ERROR and invoke `write_orchestrator_failure_verdict(...)`, then return `{"batchItemFailures": []}`; (8) on success log INFO `run_task_dispatched` with `dispatched_task_arn=result.task_arn` (Requirement 9.4) and return `{"batchItemFailures": []}`.
  - Batch size 1 assertion: `assert len(envelope.Records) == 1` per Requirement 1.1.
  - `_try_extract_delivery_id(body: str) -> str | None` — best-effort JSON decode for the payload-gate branch when structured parsing has not yet run, so the `payload_exceeds_256kb` log record can still carry `delivery_id` if extractable (Requirement 2.1).
  - The handler MUST NOT log the raw `record.body`, the `ecs.RunTask` response body, or any credential material at any level (Requirement 9.3 / Invariant 6).
  - The handler MUST NOT introduce a separate `audit_id` — `delivery_id` is the single correlation key at this layer (Requirement 1.4).
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/handler.py` and `uv run ruff check trikon_cloud/orchestrator/handler.py`.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 3.1, 4.1, 4.2, 4.3, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 7.4, 8.1, 9.1, 9.2, 9.3, 9.4, 16.3, 17.1, 17.2_
  - _Design: §1.1, §1.3, §4.1, §4.2, §4.3, §4.4, §4.5_

- [x] 12. Create `trikon_cloud/installation_lifecycle/handler.py` — `lambda_handler` with the five-step flow
  - Declare `__all__ = ["lambda_handler", "handle_installation_created", "handle_installation_deleted", "handle_repositories_added", "handle_repositories_removed"]`.
  - Module-scope cold-start caches: `_ENV: LifecycleEnvConfig | None = None`, `_IAM_CLIENT`, `_DDB_CLIENT` — lazy-initialized per design.md §5.1.
  - `lambda_handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]` implements the five steps from design.md §5.1: (1) parse `InstallationEventMessage.model_validate_json(record.body)` — on `ValidationError` log `installation_event_rejected` ERROR with `reason="malformed_installation_event"` and return `{"batchItemFailures": []}` (Requirement 11.3); (2) `append_lifecycle_context(logger, message=message)` — Requirement 15.2; (3) dispatch by `message.event_type` via `match` statement to one of `handle_installation_created`, `handle_installation_deleted`, `handle_repositories_added`, `handle_repositories_removed` (Requirement 12); (4) catch `ClientError` — on any AWS-API failure log `lifecycle_terminal_failure` ERROR with `error_class="terminal"` and re-raise so SQS returns the message and the DLQ absorbs it after `maxReceiveCount=3` (Requirement 15.3); (5) on success log `lifecycle_event_processed` INFO and return `{"batchItemFailures": []}`.
  - `handle_installation_created(message, *, iam_prov, ddb, env, now_iso)` per design.md §5.2: renders `policy = render_installation_policy_document(message.installation_id, app_private_key_secret_arn=env.trikon_app_private_key_secret_arn, account_id=env.trikon_aws_account_id, region=env.aws_region)` and `assume_role_policy = render_installation_assume_role_policy_document()`; calls `iam_prov.provision(...)` — on `already_existed` log `installation_already_provisioned` INFO (Requirement 14.2); calls `ddb.upsert_active(...)` — on `already_active` log `installation_already_active` INFO and return (Requirement 14.3); on success log `installation_provisioned` INFO.
  - `handle_installation_deleted(message, *, iam_prov, ddb, now_iso)` per design.md §5.3: calls `iam_prov.deprovision(...)` — on `already_absent` log `installation_role_already_absent` INFO; calls `ddb.mark_disabled(...)`; log `installation_disabled` INFO.
  - `handle_repositories_added(message, *, ddb, now_iso)` and `handle_repositories_removed(message, *, ddb, now_iso)` per design.md §5.4: call `ddb.add_repositories(...)` / `ddb.remove_repositories(...)` with `frozenset(message.repositories)`; log `repositories_added` / `repositories_removed` INFO with `count=len(message.repositories)`.
  - `_now_iso_utc_ms() -> str` returns `datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")` — captured once per invocation.
  - Verify with `uv run mypy --strict trikon_cloud/installation_lifecycle/handler.py` and `uv run ruff check trikon_cloud/installation_lifecycle/handler.py`.
  - _Requirements: 10.1, 10.2, 11.3, 12.1, 12.2, 12.3, 12.4, 13.5, 14.2, 14.3, 14.4, 14.5, 15.1, 15.2, 15.3, 16.3, 17.1, 17.2_
  - _Design: §5.1, §5.2, §5.3, §5.4, §5.5_

- [x] 13. Create `trikon_cloud/orchestrator/infra/orchestrator_stack.py` — one CDK stack, both Lambdas
  - Import `aws_cdk as cdk`, `from aws_cdk import (Stack, Duration, RemovalPolicy, aws_lambda as _lambda, aws_lambda_event_sources as lambda_events, aws_sqs as sqs, aws_dynamodb as dynamodb, aws_iam as iam, aws_ssm as ssm)`, `from constructs import Construct`.
  - Declare `class OrchestratorStack(Stack)` with `__init__(self, scope: Construct, construct_id: str, *, app_private_key_secret_arn: str, github_app_id: int, runner_subnet_ids: str, runner_security_group_ids: str, **kwargs: object) -> None`.
  - Reference existing (Spec 1-owned) resources per design.md §8.1: `sqs.Queue.from_queue_arn(self, "VerifyJobsQueue", ...)` on `arn:aws:sqs:us-east-1:{account}:trikon-verify-jobs`; `sqs.Queue.from_queue_arn(self, "VerifyJobsDlq", ...)` on `arn:aws:sqs:us-east-1:{account}:trikon-verify-jobs-dlq`; `ssm.StringParameter.from_string_parameter_name(self, "VerifyRunnerActiveRevisionParam", string_parameter_name="/trikon/verify-runner/active-revision")` — the Spec 2 amendment writes this parameter per design.md §9.2.
  - Provision new (this-spec-owned) resources: (a) `lifecycle_dlq = sqs.Queue(self, "InstallationEventsDlq", queue_name="trikon-cloud-installation-events-dlq", retention_period=Duration.days(14))`; (b) `lifecycle_queue = sqs.Queue(self, "InstallationEventsQueue", queue_name="trikon-cloud-installation-events", visibility_timeout=Duration.minutes(2), dead_letter_queue=sqs.DeadLetterQueue(queue=lifecycle_dlq, max_receive_count=3))` — Requirement 10.2 and 10.3; (c) `installations_table = dynamodb.TableV2(self, "InstallationsTable", table_name="trikon-cloud-installations", partition_key=dynamodb.Attribute(name="installation_id", type=dynamodb.AttributeType.NUMBER), billing=dynamodb.Billing.on_demand(), point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True), removal_policy=RemovalPolicy.RETAIN)` — Requirement 14.1.
  - Provision the Orchestrator_Handler Lambda per design.md §8.1 and Requirement 8.1: `runtime=Runtime.PYTHON_3_11`, `architecture=Architecture.ARM_64`, `memory_size=512`, `timeout=Duration.seconds(30)`, `reserved_concurrent_executions=10`, `handler="trikon_cloud.orchestrator.handler.lambda_handler"`, `function_name="trikon-cloud-orchestrator"`, environment dict populated from the constructor kwargs and `self.account`. Event source mapping: `batch_size=1`, `max_batching_window=Duration.seconds(0)`, `report_batch_item_failures=True` — Requirement 1.1.
  - Provision the Lifecycle_Handler Lambda per design.md §8.1 and Requirement 15.1: same runtime/arch/memory/timeout, `reserved_concurrent_executions=5`, `handler="trikon_cloud.installation_lifecycle.handler.lambda_handler"`, `function_name="trikon-cloud-installation-lifecycle"`. Event source mapping: `batch_size=1`, `max_batching_window=Duration.seconds(0)`, `report_batch_item_failures=True` — Requirement 10.1.
  - Implement `_grant_orchestrator_permissions` per design.md §8.2 — the eight grants: (1) `ecs:RunTask` on `arn:aws:ecs:us-east-1:{account}:task-definition/trikon-verify-runner:*` (Requirement 8.2); (2) `iam:PassRole` on `arn:aws:iam::{account}:role/trikon-verify-task-role-*` with `iam:PassedToService=ecs-tasks.amazonaws.com` condition (Requirement 8.3); (3) `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on `trikon-verify-jobs` via `verify_jobs_queue.grant_consume_messages(role)` (Requirement 8.4); (4) `sqs:SendMessage` on `trikon-verify-jobs-dlq` via `verify_jobs_dlq.grant_send_messages(role)` (Requirement 8.5); (5) `ssm:GetParameter` on the active-revision parameter via `active_revision_param.grant_read(role)` (Open Item b resolution); (6) `dynamodb:PutItem` on `arn:aws:dynamodb:us-east-1:{account}:table/trikon_verdicts` with `ForAllValues:StringLike dynamodb:LeadingKeys=["*"]` (Never-Fail-Open grant per design.md §8.2, Grant 6); (7) `secretsmanager:GetSecretValue` on the passed `app_private_key_secret_arn`.
  - Implement `_grant_lifecycle_permissions` per design.md §8.3: (1) `iam:CreateRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:DeleteRole`, `iam:GetRole`, `iam:TagRole` on `arn:aws:iam::{account}:role/trikon-verify-task-role-*` — Requirement 15.4; explicitly NO `iam:PassRole` (Requirement 15.5); (2) `dynamodb:PutItem`, `GetItem`, `UpdateItem`, `DeleteItem` on the `installations_table` via `installations_table.grant(role, ...)`; (3) `lifecycle_queue.grant_consume_messages(role)`; (4) `lifecycle_dlq.grant_send_messages(role)`.
  - Do NOT create the App private-key Secrets Manager secret (out of scope — passed in by ARN).
  - Both Lambdas MUST be in region `us-east-1` (Requirement 16.1) — controlled by the `env` on the CDK `App` in task 14.
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/infra/orchestrator_stack.py` and `uv run ruff check trikon_cloud/orchestrator/infra/orchestrator_stack.py`.
  - _Requirements: 1.1, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 10.1, 10.2, 10.3, 14.1, 15.1, 15.4, 15.5, 16.1, 16.3, 16.4, 17.1_
  - _Design: §8.1, §8.2, §8.3, §8.4_

- [x] 14. Create `trikon_cloud/orchestrator/infra/app.py` — CDK entrypoint
  - Import `aws_cdk as cdk` and `from trikon_cloud.orchestrator.infra.orchestrator_stack import OrchestratorStack`.
  - Read the four required context values via `app.node.try_get_context(...)`: `app_private_key_secret_arn`, `github_app_id` (parsed to `int`), `runner_subnet_ids`, `runner_security_group_ids`. Read `aws_account_id` from `os.environ["CDK_DEFAULT_ACCOUNT"]` or the `aws_account_id` context.
  - Instantiate the stack with `env=cdk.Environment(account=<account>, region="us-east-1")` — Requirement 16.1.
  - Stack construct id is exactly `"TrikonCloudOrchestratorStack"` per design.md §8.5.
  - Call `app.synth()` at the bottom.
  - Verify with `uv run mypy --strict trikon_cloud/orchestrator/infra/app.py`, `uv run ruff check trikon_cloud/orchestrator/infra/app.py`, and `uv run cdk synth -c app_private_key_secret_arn=arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-mock -c github_app_id=999999 -c runner_subnet_ids=subnet-a,subnet-b -c runner_security_group_ids=sg-verify -c aws_account_id=000000000000 --app "python -m trikon_cloud.orchestrator.infra.app"` — synth must exit 0 and emit `cdk.out/TrikonCloudOrchestratorStack.template.json`.
  - _Requirements: 16.1, 16.3, 17.1_
  - _Design: §8.5_

- [x] 15. Create `trikon_cloud/orchestrator/infra/README.md` — deploy runbook
  - Short quickstart Markdown document covering: (a) purpose of the stack (one paragraph naming Spec 3 of Trikon Cloud M1, both Lambdas, and cross-references to design.md §8 and §9); (b) prerequisite context values (`app_private_key_secret_arn`, `github_app_id`, `runner_subnet_ids`, `runner_security_group_ids`, `aws_account_id`) and how they map to CDK context flags; (c) coordinated deploy order — Spec 1's webhook receiver amendment (per design.md §9.1) MUST ship before end-to-end lifecycle testing, and Spec 2's CDK stack MUST populate `/trikon/verify-runner/active-revision` in SSM before Spec 3's orchestrator cold-starts (per design.md §9.2); (d) local `cdk synth` invocation with a mock-context command line (matches the verify command in task 14); (e) manual deploy gate command `cdk deploy TrikonCloudOrchestratorStack --context ...` — flagged as a release-engineer action, not an implementation task; (f) mypy/ruff/pytest invocations for the two packages.
  - The README MUST render the product name as `Trikon` or `Trikon Cloud` throughout (Requirement 18.1, 18.3); no prior product name may appear.
  - _Requirements: 16.4, 18.1, 18.3, 18.4_
  - _Design: §8.5, §9.1, §9.2_

- [x] 16. Create the shared `tests/conftest.py` for both packages — moto/respx fixtures + canonical constants
  - Create one `conftest.py` at `trikon_cloud/orchestrator/tests/conftest.py` and one at `trikon_cloud/installation_lifecycle/tests/conftest.py`. Both import from a common `trikon_cloud/_test_fixtures.py` module (private, underscore-prefixed, not exported) that houses the shared canonical constants and factory helpers so each `conftest.py` stays thin.
  - `_test_fixtures.py` declares canonical fixture constants matching the design.md §3.3 concrete payload: `CANONICAL_INSTALLATION_ID = 12345678`, `CANONICAL_PR_NUMBER = 42`, `CANONICAL_HEAD_SHA = "6aabf09b1c4d5e6f7890123456789012345678ab"`, `CANONICAL_BASE_SHA = "87f7a31b2c3d4e5f6789012345678901234567cd"`, `CANONICAL_DELIVERY_ID = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"`, `CANONICAL_REPO_FULL_NAME = "octocat/hello-world"`, `CANONICAL_EVENT_TYPE = "pull_request.opened"`, `CANONICAL_SENT_AT = "2025-01-15T12:34:56.789Z"`, `CANONICAL_ACCOUNT_ID = "000000000000"`, `CANONICAL_APP_KEY_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef"`.
  - Factory helpers: `make_sqs_job_message(**overrides) -> SqsJobMessage`, `make_installation_event(*, event_type: str, **overrides) -> InstallationEventMessage`, `make_orchestrator_env() -> OrchestratorEnvConfig`, `make_lifecycle_env() -> LifecycleEnvConfig`. Each returns a fully-populated model using the canonical constants unless the caller overrides.
  - Pytest fixtures declared in each `conftest.py`: `moto_aws` (context manager wrapping `moto.mock_aws()` — sets AWS creds to fake values via `monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")` etc.); `sqs_client`, `ecs_client`, `dynamodb_client`, `iam_client`, `secrets_client`, `ssm_client`, `s3_client` — all boto3 clients constructed after `moto_aws` activates; `respx_mock` (fixture wrapping `respx.mock`); `github_endpoints` (fixture that pre-registers the five GitHub API URLs — `POST /app/installations/{id}/access_tokens`, `POST /repos/{full}/check-runs`, `PATCH /repos/{full}/check-runs/{id}`, `POST /repos/{full}/issues/{n}/comments`, `PATCH /repos/{full}/issues/comments/{id}` — with canonical 200 responses).
  - The verdicts table + pr-state table + evidence bucket + installations table + verify-jobs queue + installation-events queue + trikon_verdicts DynamoDB item schema are pre-created inside a `pytest.fixture` named `provisioned_aws` that composes `moto_aws` + resource creation, so per-test setup is one fixture arg.
  - No scratch `.py` files outside of `conftest.py` and `_test_fixtures.py` — every test constant/factory lives here (matches Spec 2's "no scratch files" rule).
  - Verify with `uv run pytest trikon_cloud/orchestrator/tests/ --collect-only -q` and `uv run pytest trikon_cloud/installation_lifecycle/tests/ --collect-only -q` — both must succeed and collect zero tests at this task's completion (test modules land in tasks 17-19).
  - _Requirements: 17.1, 17.2, 18.1_
  - _Design: §10.1, §10.2_

- [x] 17. Land the orchestrator unit tests — one sub-task per module under `trikon_cloud/orchestrator/tests/`
  - [x] 17.1 Create `test_models.py` — `SqsEventEnvelope`, `RunTaskCall`, `OrchestratorEnvConfig`
    - Cover Property 1 (`SqsJobMessage` round-trip via JSON) with `@hypothesis.given(sqs_message_strategy())` — for every valid `SqsJobMessage m`, `SqsJobMessage.model_validate_json(m.model_dump_json()) == m` structurally. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 1: SqsJobMessage round-trip via JSON`.
    - Cover Property 2 — for every dict lacking one of the eight required fields, `SqsJobMessage.model_validate(...)` raises `ValidationError` and `.errors()` includes the missing field's location.
    - Unit tests for `OrchestratorEnvConfig` — `TRIKON_RUNNER_SUBNET_IDS="subnet-a,subnet-b"` parses to `("subnet-a", "subnet-b")`; missing required env raises `ValidationError`; the default `TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM` equals `"/trikon/verify-runner/active-revision"`.
    - Unit tests for `RunTaskCall` — `model_config` is `frozen=True`, `extra="forbid"`; mutation raises `ValidationError`; extra fields raise `ValidationError`.
    - _Requirements: 1.2, 2.2, 5.1, 5.2, 5.3, 5.4, 5.5, 11.1, 17.2, 17.3_
    - _Design: §3.1, §3.3, §3.4, §10.1, §10.4 Property 1, §10.4 Property 2_

  - [x] 17.2 Create `test_logger.py` — `get_logger`, `append_job_context`, `LOGGING_DENYLIST`
    - Cover Property 12 (Structured log context propagation) — after `append_job_context(logger, message=m)`, every subsequent log record carries the five natural-key fields byte-identical to `m`; no record carries a key in `LOGGING_DENYLIST`. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation`.
    - AST-scan guard test — walk every `.py` under `trikon_cloud/orchestrator/` and assert no log-call keyword argument name is in `LOGGING_DENYLIST` (grep-scan implementation is acceptable; AST walk is preferred). This catches accidental `logger.error("...", body=raw_body)` regressions at test time (Requirement 9.3).
    - _Requirements: 9.1, 9.2, 9.3, 18.2_
    - _Design: §2.2, §7.4, §10.1, §10.4 Property 12_

  - [x] 17.3 Create `test_ecs_dispatcher.py` — `build_run_task_call`, `submit_run_task`, `TaskDefinitionResolver`, `classify_client_error`
    - Cover Property 3 (`delivery_id` byte-for-byte propagation through dispatch) with `@hypothesis.given(sqs_message_strategy())` — for every `m`, the `RunTaskCall` built by `build_run_task_call(m, ...)` contains `m.delivery_id` in exactly one place: `overrides.containerOverrides[0].environment[<idx>].value` where `<idx>` is the entry whose `name == "TRIKON_DELIVERY_ID"`. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 3: delivery_id byte-for-byte propagation`.
    - Cover Property 4 (`RunTaskCall` shape invariant) — for every valid `m` and `env`, the built `RunTaskCall` satisfies the six shape constraints from design.md §10.4 Property 4. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant`.
    - Cover Property 5 (`taskDefinition` pinned family:revision) — `TaskDefinitionResolver.resolve()` returns a string matching `^trikon-verify-runner:\d+$`, never contains `:LATEST`, and `ssm.get_parameter` is called at most once per container lifetime. Use a spy counting `ssm_client.get_parameter` invocations. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 5: taskDefinition pinned family:revision`.
    - Cover Property 6 (Transient vs Terminal classification is total and deterministic) — parametrize `classify_client_error` over every code in `_TERMINAL_CODES`, every code in `_TRANSIENT_CODES`, plus five arbitrary unknown codes; assert the fail-safe default returns `"transient"` for unknown codes. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification`.
    - Unit tests via `@mock_aws` (moto ECS + SSM): `submit_run_task` invokes `ecs_client.run_task` exactly once and returns `RunTaskDispatchResult(task_arn=<mocked>)`.
    - Unit test that `submit_run_task` re-raises a `ClientError` unhandled — the handler catches, not the dispatcher.
    - _Requirements: 1.3, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.4_
    - _Design: §3.3, §4.2, §4.3, §10.1, §10.4 Properties 3-6_

  - [x] 17.4 Create `test_never_fail_open.py` — `write_orchestrator_failure_verdict`, `build_synthetic_verdict_row`, `OrchestratorGithubClient`
    - Cover Property 7 (Never-Fail-Open — synthetic verdict + neutral Check Run + idempotence) — first invocation writes exactly one `trikon_verdicts` row with `decision="require_human"`, `matched_rule="orchestrator terminal failure"`, `sk=f"{m.sent_at}#{m.delivery_id}"`, and posts exactly one Check Run with `conclusion="neutral"`. Second invocation with the same `sqs_message` produces zero additional rows (`ConditionalCheckFailedException` handled as success) and MAY produce zero or one additional Check Run (GitHub API is not deduplicating). Docstring tag: `Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence`.
    - Unit test that `build_synthetic_verdict_row(...)` is pure — same inputs produce byte-identical output across N calls, no IO.
    - Unit test that Check Run POST failure (respx returns 500 on all 3 retries) is caught and logged at ERROR without re-raising — the DynamoDB write already satisfied Invariant 2 (Requirement 7.2 partial-success semantics).
    - Unit test that the Check Run body has `name == "Trikon"` exactly — Invariant 7 assertion.
    - Unit test that the App private key PEM, App JWT, and installation token never appear in any log record captured by `caplog` across the six-step body — Invariant 6.
    - Unit test that `OrchestratorGithubClient._TOKEN_CACHE` caches the installation token for its TTL minus 5 minutes; a second call within the TTL window does NOT re-hit the mint endpoint.
    - Fixture: pre-populate `trikon_verdicts` with a matching row for the redelivery test (asserts `ConditionalCheckFailedException` is treated as success).
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 9.3_
    - _Design: §3.5, §7.1, §7.2, §7.3, §10.1, §10.4 Property 7_

  - [x] 17.5 Create `test_orchestrator_handler.py` — end-to-end coverage of the eight-step flow
    - Happy path: parametrize a full SQS envelope with the canonical `SqsJobMessage`; under `@mock_aws()` (SQS + ECS + SSM + DynamoDB) and `@respx.mock` for GitHub, invoke `lambda_handler(event, context)`; assert (a) return value equals `{"batchItemFailures": []}`; (b) `ecs.run_task` was called exactly once with kwargs byte-matching design.md §3.3's concrete payload; (c) log stream contains one `run_task_dispatched` INFO record with `dispatched_task_arn` set (Requirement 9.4).
    - Payload-size gate: body of 262_145 bytes → return `{"batchItemFailures": []}`, log `payload_exceeds_256kb` ERROR, no `ecs.run_task` call (Requirement 2.1).
    - Validation gate: malformed JSON body → return `{"batchItemFailures": []}`, log `malformed_sqs_body` ERROR with `errors` list (Requirement 2.2).
    - Transient path: patch `ecs_client.run_task` to raise `ClientError` with `Code="ThrottlingException"` → `lambda_handler` raises `TransientDispatchError`; assert no `trikon_verdicts` row written; assert log record `run_task_transient_failure` WARN.
    - Terminal path (Never-Fail-Open): patch `ecs_client.run_task` to raise `ClientError` with `Code="TaskDefinitionNotFound"` → assert (a) return equals `{"batchItemFailures": []}`; (b) exactly one row in `trikon_verdicts` with `decision="require_human"`; (c) respx `create_neutral_check_run` was called once with `conclusion="neutral"` and `name="Trikon"`; (d) log record `run_task_terminal_failure` ERROR with `error_class="terminal"`.
    - Unknown error-code path: patch `ecs_client.run_task` to raise `ClientError` with `Code="SomeUnknownAwsError"` → classified as Transient per fail-safe default (Requirement 6.4).
    - Cover Requirement 1.4 explicitly: assert no log record in any of the above paths contains a key literally named `audit_id`.
    - Cover Requirement 9.3 explicitly: assert no log record in any path contains the raw `record.body`, the `ecs.run_task` response, or any credential material.
    - Batch-size assertion: send an event with 2 records → assertion fails (Requirement 1.1 — batch size 1 is enforced by the event-source mapping in task 13, but the handler defensively asserts too).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 3.1, 4.1, 6.1, 6.2, 6.4, 7.1, 7.2, 7.3, 7.4, 9.1, 9.2, 9.3, 9.4_
    - _Design: §4.1, §4.2, §4.5, §10.1_

- [x] 18. Land the installation-lifecycle unit tests — one sub-task per module under `trikon_cloud/installation_lifecycle/tests/`
  - [x] 18.1 Create `test_models.py` — `InstallationEventMessage`, `LifecycleTableRow`, `LifecycleEnvConfig`
    - Cover Property 8 (`InstallationEventMessage` shape) with `@hypothesis.given(installation_event_strategy())` — for every valid dict, `.model_validate(...).model_dump_json()` re-validates to an equal model; for every dict with an extra key not in the schema, validation raises `ValidationError` (extra="forbid"). Docstring tag: `Feature: trikon-cloud-orchestrator, Property 8: InstallationEventMessage shape`.
    - Unit test that `event_type` outside the four literal values raises `ValidationError`.
    - Unit test that `installation_id=0` and `github_app_id=0` both raise `ValidationError` (Field ge=1 constraint).
    - Unit test that `LifecycleTableRow.status` outside `{"active", "disabled"}` raises `ValidationError`.
    - Unit test that `LifecycleEnvConfig` defaults resolve to `TRIKON_INSTALLATIONS_TABLE="trikon-cloud-installations"`, `AWS_REGION="us-east-1"`, `TRIKON_LOG_LEVEL="INFO"`.
    - _Requirements: 11.1, 11.2, 11.4, 17.4_
    - _Design: §3.2, §10.2, §10.4 Property 8_

  - [x] 18.2 Create `test_logger.py` — `get_logger`, `append_lifecycle_context`, `LOGGING_DENYLIST`
    - Unit test that `_LOGGER.service` equals `"trikon-cloud-installation-lifecycle"` — Requirement 18.2 prefix.
    - Unit test that `append_lifecycle_context(logger, message=m)` propagates `installation_id`, `event_type`, `delivery_id` on every subsequent log record — Requirement 15.2.
    - AST-scan guard test mirroring the orchestrator's — no log-call kwarg name may be in `LOGGING_DENYLIST`.
    - _Requirements: 15.2, 17.2, 18.2_
    - _Design: §2.2, §10.2_

  - [x] 18.3 Create `test_iam_template.py` — `render_installation_policy_document`, `render_installation_assume_role_policy_document`, `canonical_json`
    - Purity: same inputs produce byte-identical `model_dump_json(by_alias=True)` output across N calls. No IO — assert with `respx.mock.assert_all_called()` on an empty respx that no HTTP call occurred.
    - Structural assertions on the returned `InstallationPolicyDocument`: `Version == "2012-10-17"`; `len(Statement) == 4`; Statement 0's `Sid == "DynamoDBVerdictsScopedToInstallation"`; Statement 1's `Sid == "DynamoDBPrStateScopedToInstallation"`; Statement 2's `Sid == "S3EvidenceSpillScopedToInstallation"` and `Condition is None`; Statement 3's `Sid == "SecretsManagerAppPrivateKeyRead"` and `Resource == passed_app_key_arn`.
    - Assert `Action` on every Statement is a `tuple[str, ...]` — never a bare `str` — and single-element on Statements 0 and 3 (byte-match requires list form on the JSON side).
    - Assert `canonical_json` output is sorted-keys, no whitespace: `json.loads(canonical_json(doc)) == doc.model_dump(by_alias=True, mode="json")` structurally.
    - Assert the aliased key `"ForAllValues:StringEquals"` appears in the JSON output on Statements 0 and 1 (Pydantic alias serialization).
    - Hypothesis sweep: `@hypothesis.given(installation_id=st.integers(min_value=1, max_value=2**63 - 1))` — for every `installation_id`, the rendered policy has valid structure (all four Sids present, Version correct, Resource for Statement 3 equals passed arn).
    - _Requirements: 13.1, 13.2, 13.3, 17.4_
    - _Design: §6.1, §6.2, §6.3, §6.4, §10.2_

  - [x] 18.4 Create `test_iam_provisioner.py` — `IamProvisioner.provision`, `IamProvisioner.deprovision`
    - Under `@mock_aws()`: `provision(installation_id=12345678, policy=..., assume_role_policy=...)` creates a role named `trikon-verify-task-role-12345678` with the `installation_id` tag; a second invocation returns `ProvisionResult(already_existed=True)` and does NOT re-raise (Requirement 14.2 idempotency).
    - Under `@mock_aws()`: `deprovision(installation_id=12345678)` deletes the role and inline policy; a second invocation returns `DeprovisionResult(already_absent=True)` on both legs (Requirement 14.4).
    - Assert inline policy name is exactly `"TrikonInstallationPolicy"` per design.md §5.3.
    - Assert `iam:DeleteRolePolicy` is called BEFORE `iam:DeleteRole` — deletion order matters (design.md §5.3).
    - Assert that after `provision`, `iam.get_role(RoleName=...)` succeeds with `Tags` containing `[{"Key": "installation_id", "Value": "12345678"}]` (design.md §6.4).
    - _Requirements: 13.5, 14.2, 14.4, 15.5_
    - _Design: §5.2, §5.3, §5.5, §6.4, §10.2_

  - [x] 18.5 Create `test_dynamodb_writer.py` — `InstallationsTableWriter` CRUD
    - Under `@mock_aws()`: `upsert_active(installation_id=1, github_app_id=99, repositories=frozenset({"a/b"}), now_iso=...)` creates a row with `status="active"`; a second invocation returns `UpsertResult(already_active=True)` per Requirement 14.3.
    - `mark_disabled` updates an existing row's `status` to `"disabled"`; on a missing row, creates a sentinel row (Requirement 14.4 semantics — no ConditionExpression).
    - `add_repositories(installation_id=1, repositories=frozenset({"a/b", "c/d"}), now_iso=...)` — the row's `repositories` SS attribute now contains both entries; a second invocation with the same set is a no-op (DynamoDB ADD idempotency, Requirement 14.5).
    - `remove_repositories(installation_id=1, repositories=frozenset({"a/b"}), now_iso=...)` — the row's `repositories` set no longer contains `"a/b"`; a second invocation is a no-op.
    - Assert `updated_at` is refreshed on every write.
    - _Requirements: 12.3, 12.4, 14.1, 14.3, 14.5_
    - _Design: §5.2, §5.3, §5.4, §5.5, §10.2_

  - [x] 18.6 Create `test_lifecycle_handler.py` — end-to-end coverage of the five-step flow
    - Cover Property 9 (Event-type routing is a total function on the four allowed values) — parametrize over `["installation.created", "installation.deleted", "installation_repositories.added", "installation_repositories.removed"]`; assert exactly one handler is invoked per `event_type`, no other handler runs. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 9: Event-type routing`.
    - Cover Property 11 (Lifecycle idempotency on re-delivery) — for each of the four event types, invoke `lambda_handler` twice consecutively on the same message; assert the second invocation raises no exception and leaves the state consistent per the design.md §5.5 idempotency table. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency on re-delivery`.
    - Malformed-event path: SQS body missing `installation_id` → return `{"batchItemFailures": []}`, log `installation_event_rejected` ERROR with `reason="malformed_installation_event"` (Requirement 11.3).
    - Terminal-failure path on `installation.created`: patch `iam_client.create_role` to raise `ClientError` with `Code="AccessDenied"` → `lambda_handler` re-raises (Requirement 15.3); assert log record `lifecycle_terminal_failure` ERROR with `error_class="terminal"`; assert NO DynamoDB write occurred (half-configured state avoided).
    - `installation.created` happy path: assert `iam:CreateRole` was called with `RoleName="trikon-verify-task-role-{installation_id}"` and `Tags` set (design.md §6.4); assert `iam:PutRolePolicy` was called with `PolicyName="TrikonInstallationPolicy"`; assert the `trikon-cloud-installations` row has `status="active"`.
    - `installation.deleted` happy path: assert both `iam:DeleteRolePolicy` and `iam:DeleteRole` were called (in that order); assert the row's `status="disabled"`.
    - `installation_repositories.added` and `installation_repositories.removed`: assert the row's `repositories` SS set reflects the delta.
    - _Requirements: 10.1, 10.2, 11.3, 12.1, 12.2, 12.3, 12.4, 14.2, 14.3, 14.4, 14.5, 15.1, 15.2, 15.3_
    - _Design: §5.1, §5.2, §5.3, §5.4, §5.5, §10.2, §10.4 Properties 9, 11_

- [x] 19. Create `tests/test_iam_policy_contract.py` — byte-match against Spec 2's CDK synth (Property 10)
  - Place the file at `trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py`. This is the highest-value integration test in the spec — it locks the runtime IAM renderer to Spec 2's CDK output so drift is caught at test time, before any live `iam:PutRolePolicy` call.
  - Import `from aws_cdk import App, Environment` and `from aws_cdk.assertions import Template`; import `from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack`; import `from trikon_cloud.installation_lifecycle.iam_template import render_installation_policy_document, canonical_json`.
  - Implement `_canonical_from_cdk(installation_id: int, app_key_arn: str) -> str` per design.md §10.5: instantiate `FargateRunnerStack` bound to `env=Environment(account="000000000000", region="us-east-1")`; call `stack.build_task_role_for_installation(installation_id, app_key_arn)`; extract the `AWS::IAM::Policy` resource attached to the role via `Template.from_stack(stack).find_resources("AWS::IAM::Policy", {...})`; return `json.dumps(policy["Properties"]["PolicyDocument"], sort_keys=True, separators=(",", ":"))`.
  - `test_iam_policy_byte_matches_cdk_synth_at_canonical_id`: parametrized single case at `installation_id=12345678`, `app_key_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef"` (per Requirement 13.4). Compute `cdk_canonical` and `rt_canonical` via `canonical_json(rt_doc)`; assert `rt_canonical == cdk_canonical`. Docstring tag: `Feature: trikon-cloud-orchestrator, Property 10: IAM policy runtime output byte-matches CDK synth`.
  - `test_iam_policy_byte_matches_cdk_synth_hypothesis_sweep`: `@hypothesis.given(installation_id=st.integers(min_value=1, max_value=2**63 - 1))` `@settings(max_examples=100, deadline=None)` — for every `installation_id`, `canonical_json(render_installation_policy_document(installation_id, ...))` equals `_canonical_from_cdk(installation_id, ...)`.
  - On failure: the test emits a unified `difflib.ndiff` of the two canonical JSON strings so the reviewer can see exactly which byte drifted (Sid rename, Action re-order, LeadingKeys change, missing Statement, etc.).
  - This test is Property 10 — the single guardrail against Spec 2 / Spec 3 IAM drift.
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 17.4_
  - _Design: §6.5, §6.6, §10.2, §10.4 Property 10, §10.5_

- [x] 20. Checkpoint — pytest + coverage floors, mypy `--strict`, ruff, `cdk synth`, contract test green
  - Perform five validation sub-checks from the repo root. This is the completion checkpoint for the spec. Do NOT proceed to release plumbing (version bumps, commits, tags, pushes, `cdk deploy`) — those remain out of scope.
  - **Sub-check 20.a — local unit tests + coverage.** Run `uv sync --extra cloud` to install both packages' runtime deps. Run `uv run pytest trikon_cloud/orchestrator/tests/ trikon_cloud/installation_lifecycle/tests/ -v --cov=trikon_cloud/orchestrator --cov=trikon_cloud/installation_lifecycle --cov-branch --cov-report=term-missing --cov-fail-under=80`. Assert (a) every test in tasks 17-19 passes; (b) branch coverage on `orchestrator/handler.py`, `orchestrator/never_fail_open.py`, `installation_lifecycle/handler.py`, `installation_lifecycle/iam_template.py` each reaches at least 90% (design.md §10.3 floors); (c) branch coverage on every other production module in both packages reaches at least 80%; (d) CDK stack code under `infra/` is excluded from the coverage measurement.
  - **Sub-check 20.b — mypy `--strict` on both packages.** Run `uv run mypy --strict trikon_cloud/orchestrator/ trikon_cloud/installation_lifecycle/`. Assert exit code 0 with zero errors (Requirement 17.1). The strict pass applies to every file — production modules, test modules, CDK stack, and app entry.
  - **Sub-check 20.c — ruff clean on both packages.** Run `uv run ruff check trikon_cloud/orchestrator/ trikon_cloud/installation_lifecycle/`. Assert exit code 0.
  - **Sub-check 20.d — CDK synth green.** Run `uv run cdk synth -c app_private_key_secret_arn=arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-mock -c github_app_id=999999 -c runner_subnet_ids=subnet-a,subnet-b -c runner_security_group_ids=sg-verify -c aws_account_id=000000000000 --app "python -m trikon_cloud.orchestrator.infra.app"`. Assert (a) synth exits 0; (b) the emitted CloudFormation template under `cdk.out/TrikonCloudOrchestratorStack.template.json` contains: two `AWS::Lambda::Function` resources (orchestrator + lifecycle), two `AWS::Lambda::EventSourceMapping` resources with `BatchSize: 1` and `MaximumBatchingWindowInSeconds: 0`, one `AWS::SQS::Queue` named `trikon-cloud-installation-events`, one `AWS::SQS::Queue` named `trikon-cloud-installation-events-dlq`, one `AWS::DynamoDB::Table` named `trikon-cloud-installations` with `DeletionPolicy: Retain`; (c) both Lambda functions have `Runtime: python3.11` and `Architectures: [arm64]` (Requirements 8.1, 15.1, 16.3); (d) the orchestrator role does NOT grant `iam:PassRole` on any resource other than `trikon-verify-task-role-*` (Requirement 8.3); (e) the lifecycle role does NOT grant `iam:PassRole` at all (Requirement 15.5).
  - **Sub-check 20.e — contract test 19 green.** Run `uv run pytest trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py -v`. Assert both the canonical-id case and the 100-example hypothesis sweep pass (Property 10 / Requirement 13.4). A failure here means Spec 2's CDK IAM policy has drifted from this spec's runtime renderer — do NOT paper over by editing the renderer to match the drift; return to task 6 and reconcile, or coordinate with Spec 2 to revert the drift.
  - If any of Sub-checks 20.a-20.e fail, do NOT proceed to the manual deploy gate — return to the failing Wave and diagnose. Ensure all tests pass, ask the user if questions arise.
  - _Requirements: 4.1, 8.1, 13.4, 15.1, 15.5, 16.1, 16.3, 16.4, 17.1, 17.2_
  - _Design: §10.3, §10.5, §10.6_

## Notes

- Wave 0 (task 1) lands the two package skeletons and confirms `pyproject.toml` package coverage — no new deps land in this spec since Spec 1's `cloud` optional-dep group already covers `aws-lambda-powertools[all]`, `pydantic`, `pydantic-settings`, `boto3`, `botocore`, `httpx`, `PyJWT[crypto]`.
- Wave 1 (tasks 2-6) lands the five leaf modules — `orchestrator/models.py`, `orchestrator/logger.py`, `installation_lifecycle/models.py`, `installation_lifecycle/logger.py`, `installation_lifecycle/iam_template.py` — in parallel; each is independent of the others.
- Wave 2 (tasks 7-10) lands the middle-tier modules that depend on Wave 1's models and loggers: `orchestrator/ecs_dispatcher.py`, `orchestrator/never_fail_open.py`, `installation_lifecycle/iam_provisioner.py`, `installation_lifecycle/dynamodb_writer.py`. All four can run in parallel — they share no imports between them.
- Wave 3 (tasks 11-12) lands the two Lambda entrypoints. Each depends on ALL modules in its package's Wave 1 + Wave 2. The two entrypoints can run in parallel — they share no imports.
- Wave 4 (tasks 13-15) lands the single CDK stack (both Lambdas), its `app.py`, and the infra README. `orchestrator_stack.py` must complete before `app.py` (task 14 imports the stack). README (task 15) is independent.
- Wave 5 (tasks 16-19) lands the tests. `conftest.py` + `_test_fixtures.py` (task 16) is the leaf; the seven `test_*.py` sub-tasks under 17 and 18 depend on task 16 AND on the production modules from Waves 1-3; task 19 (the contract test) depends on task 6 AND on Spec 2's `FargateRunnerStack` (which is already implemented — this spec's contract test IMPORTS it).
- Wave 6 (task 20) is the checkpoint. Passing it means the spec is code-complete; failing means returning to a prior Wave.
- Product name is **Trikon** throughout. Check Run `name: "Trikon"` (rendered by `never_fail_open.py` — task 8). Lambda function names `trikon-cloud-orchestrator` and `trikon-cloud-installation-lifecycle`. Powertools `service` fields prefixed `trikon-cloud`. SQS queue names, DynamoDB table names, IAM role name templates all begin `trikon-` or `trikon_`. No prior product name appears in any deliverable (Requirement 18).
- **No `dict[str, Any]`** on any public surface introduced by this spec (Requirement 17.2). Every model that crosses a module boundary is a Pydantic v2 `BaseModel` with `ConfigDict(frozen=True, extra="forbid")`. Internal DynamoDB attribute-value dicts and internal boto3-response dicts are scoped to method bodies and never cross a module boundary.
- **mypy `--strict` clean** on every touched Python file — including tests and CDK code (Requirement 17.1). Ruff clean on the same set. This matches Invariant 8 and the SDK's `trikon/verify/` discipline.
- **Coverage floors** (design.md §10.3): 90% branch on `handler.py` (both), `never_fail_open.py`, `iam_template.py`; 80% branch elsewhere. Contract test 19 is a separate gate — no coverage threshold applies to it, but it MUST pass.
- **The IAM byte-match invariant (Property 10) is the tightest constraint on this spec.** Task 19's contract test is the single automated guardrail that Spec 2's CDK-emitted per-installation policy and this spec's runtime-rendered policy remain byte-identical. Any drift — statement re-order, Sid rename, condition key change, action-list edit — fails task 19. Reconcile at Spec 2 (revert the drift) or at task 6 (update the renderer to match Spec 2), but never silence the test.
- **External dependencies not implemented by this spec** (design.md §9): (1) Spec 1's webhook receiver amendment adding a routing branch for `installation` and `installation_repositories` events to `trikon-cloud-installation-events`, plus the `sqs:SendMessage` IAM grant on that queue, plus the `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` env var. (2) Spec 2's `FargateRunnerStack` amendment writing `/trikon/verify-runner/active-revision` to SSM Parameter Store on every `cdk deploy`. Both amendments are gates for end-to-end deployment testing but NOT tasks in this spec. Unit tests use synthetic `InstallationEventMessage` fixtures and stub SSM clients — no live dependency during test-time.
- **NO git operations against the Trikon parent repository** are performed by these tasks. No `git add`, `git commit`, `git push`, `git tag`, `git branch`, or `git checkout`. All file edits stage in the working tree; commit/tag/push cadence for the M1 GA release is the release engineer's responsibility.
- **NO deploy to AWS** is performed by these tasks. Task 20's Sub-check 20.d runs `cdk synth` (offline, no AWS API calls), NOT `cdk deploy`. The manual `cdk deploy TrikonCloudOrchestratorStack` gate is a release-engineer action.
- **NO Trikon SDK version bump** and **NO `pyproject.toml [project] version` edit**. This spec adds no new deps to `pyproject.toml` — Spec 1's `cloud` optional-dep group already covers everything this spec imports.
- **NO throwaway `.sh` / `.py` / `_*.py` scratch files**. All test fixture data lives inline in `tests/conftest.py` and the private `_test_fixtures.py` module.
- **Follow-up specs referenced by this spec's deferred scope** (design.md §11): `trikon-cloud-check-run-rerequest` (M2 — GitHub UI re-run button, currently unrouted), `trikon-cloud-dashboard` (M2 Spec 4 — reads `trikon_verdicts` rows via GSI1/GSI2), a shared observability spec (M2 — CloudWatch alarms + dashboards), and a M2 hardening spec for tighter Never-Fail-Open IAM scoping via per-invocation `sts:AssumeRole`. None is a task in this spec.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2", "3", "4", "5", "6"] },
    { "id": 2, "tasks": ["7", "8", "9", "10"] },
    { "id": 3, "tasks": ["11", "12"] },
    { "id": 4, "tasks": ["13", "14", "15"] },
    { "id": 5, "tasks": ["16", "17.1", "17.2", "17.3", "17.4", "17.5", "18.1", "18.2", "18.3", "18.4", "18.5", "18.6", "19"] },
    { "id": 6, "tasks": ["20"] }
  ]
}
```
