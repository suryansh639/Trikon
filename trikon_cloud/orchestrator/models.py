# ruff: noqa: N815
# The AWS SQS event envelope and the ``ecs.RunTask`` request body carry
# ``mixedCase`` and ``PascalCase`` field names (``messageId``, ``Records``,
# ``taskDefinition``, ``assignPublicIp`` …). We keep the Python attribute
# names identical to the wire form so ``model_dump(by_alias=True, mode="json")``
# produces the exact boto3 kwargs shape required by design §3.3, and so that
# the handler can access ``record.messageId`` etc. directly per design §4.1.
# Ruff's ``N815`` (mixedCase variable in class scope) is silenced at the
# file level rather than per-field to keep the model bodies readable.
#
# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write — silence it
# at the file level, matching :mod:`trikon_cloud.fargate_runner.models`.
# mypy: disable-error-code="explicit-any"
"""Pydantic v2 models for the Trikon Cloud orchestrator (Spec 3).

Three concern groups, matching design.md's data-contract sections:

* :class:`SqsEventEnvelope`, :class:`SqsRecord`, :class:`SqsRecordAttributes`,
  :class:`SqsMessageAttribute`, :class:`SqsBatchResponse`,
  :class:`BatchItemFailure` — SQS wire shapes
  (design §3.4). The envelope validates the raw AWS Lambda event dict at
  the handler boundary; the batch response is the return type per the
  Lambda batch-item-failure protocol.
* :class:`RunTaskCall`, :class:`NetworkConfiguration`,
  :class:`AwsvpcConfiguration`, :class:`RunTaskOverrides`,
  :class:`ContainerOverride`, :class:`EnvOverride`, :class:`EcsTag` — the
  full ``ecs.RunTask`` request body (design §3.3). Field names match the
  wire form byte-for-byte so ``.model_dump(by_alias=True, mode="json")``
  yields the exact boto3 kwargs dict.
* :class:`OrchestratorEnvConfig` — the environment-variable contract
  (design §2.3, extended by sandbox-minute-plans Req 11.14, 11.15, 11.18,
  11.21). Loaded once per Lambda cold start via
  :class:`pydantic_settings.BaseSettings`; a missing or malformed variable
  raises :class:`pydantic.ValidationError` before the first SQS message is
  consumed (Requirement 8.4, design §2.3).

Every model is frozen. Every model uses ``extra="forbid"`` except:

* :class:`SqsRecordAttributes` (``extra="allow"`` because AWS adds attribute
  keys over time — design §3.4);
* :class:`SqsMessageAttribute` (``extra="ignore"`` because AWS also sends
  ``stringListValues``, ``binaryValue`` and ``binaryListValues`` keys the
  handler does not read);
* :class:`OrchestratorEnvConfig` (``extra="ignore"`` because
  pydantic-settings sees every process env var, not just the aliases we
  care about).

See ``.kiro/specs/trikon-cloud-orchestrator/design.md`` §2.2, §2.3, §3.3,
and §3.4, and ``.kiro/specs/sandbox-minute-plans/design.md`` (Usage client
configuration, SQS record model fix, RunTask changes) for the authoritative
field definitions.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from trikon_cloud.fargate_runner.spot_contract import MAX_ATTEMPTS, Capacity
from trikon_cloud.usage_client.models import ReservationId, https_base_url
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# Ordering mirrors the task's declared ``__all__`` list, which walks
# design.md §3.4 → §3.3 → §2.3. Not isort alphabetical.
__all__ = [  # noqa: RUF022
    "SqsEventEnvelope",
    "SqsRecord",
    "SqsRecordAttributes",
    "SqsMessageAttribute",
    "SqsBatchResponse",
    "BatchItemFailure",
    "VerifyJobMessage",
    "RunTaskCall",
    "CapacityProviderStrategyItem",
    "NetworkConfiguration",
    "AwsvpcConfiguration",
    "RunTaskOverrides",
    "ContainerOverride",
    "EnvOverride",
    "EcsTag",
    "OrchestratorEnvConfig",
    "SpotRetryEnvConfig",
]

#: ``TRIKON_VERIFY_JOBS_QUEUE_URL`` shape, shared by the Orchestrator and the
#: Spot_Retry_Handler.
_VERIFY_JOBS_QUEUE_URL_PATTERN = (
    r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/[A-Za-z0-9_-]{1,80}$"
)


# ---------------------------------------------------------------------------
# §3.4 — SQS event envelope and batch response.
# ---------------------------------------------------------------------------


class SqsRecordAttributes(BaseModel):
    """The ``attributes`` sub-object of an SQS record (design §3.4).

    ``extra="allow"`` because AWS routinely adds attribute keys
    (``SentTimestamp``, ``SenderId``, ``AWSTraceHeader``, …) that this
    handler does not read. The one attribute the handler DOES read —
    ``ApproximateReceiveCount`` — is arrival-count for the current
    delivery, used to correlate log records with SQS-level retry state
    (design §4.1 STEP 1, Requirement 9.2).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    ApproximateReceiveCount: str
    # Unix epoch milliseconds, set by SQS on the first receive. Read as the
    # fallback start of a Job's queue wait (sandbox-minute-plans Req 11.21).
    ApproximateFirstReceiveTimestamp: str | None = None


class SqsMessageAttribute(BaseModel):
    """One entry of an SQS record's ``messageAttributes`` map.

    ``extra="ignore"`` because AWS also sends ``stringListValues``,
    ``binaryValue`` and ``binaryListValues`` keys that the handler does not
    read. The busy re-queue path reads the ``Number`` attribute
    ``trikon_first_received_at`` (sandbox-minute-plans Req 11.21).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    dataType: str
    stringValue: str | None = None


class SqsRecord(BaseModel):
    """One record inside an AWS SQS Lambda event (design §3.4).

    ``body`` is the raw JSON string written by Spec 1's webhook receiver;
    STEP 3 of the handler parses it into
    :class:`trikon_cloud.webhook_receiver.models.SqsJobMessage`. ``messageId``
    and ``receiptHandle`` are AWS-assigned per-delivery identifiers, echoed
    onto the log context via :func:`append_job_context`
    (design §4.1 STEP 1).

    The remaining fields are the other standard keys of a Lambda SQS event
    record. They are declared so that a real event validates while
    ``extra="forbid"`` still rejects anything else (sandbox-minute-plans,
    SQS record model fix). ``messageAttributes`` defaults to an empty map.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messageId: str
    receiptHandle: str
    body: str
    attributes: SqsRecordAttributes
    messageAttributes: dict[str, SqsMessageAttribute] = Field(default_factory=dict)
    md5OfBody: str | None = None
    md5OfMessageAttributes: str | None = None
    eventSource: str | None = None
    eventSourceARN: str | None = None
    awsRegion: str | None = None


class SqsEventEnvelope(BaseModel):
    """The raw AWS Lambda SQS event envelope (design §3.4).

    Batch size is fixed at 1 by the event-source-mapping configuration
    (Requirement 1.1), so ``Records`` always contains exactly one entry
    at runtime. The tuple type keeps the shape open at the model layer;
    the handler asserts ``len(envelope.Records) == 1`` on entry
    (design §4.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    Records: tuple[SqsRecord, ...]


class BatchItemFailure(BaseModel):
    """One entry of the Lambda batch-item-failure response (design §3.4).

    Only ``itemIdentifier`` — the ``messageId`` of the failing record —
    appears on the wire. The handler emits a
    :class:`SqsBatchResponse` whose ``batchItemFailures`` is empty on
    success or Terminal errors (message deleted from queue), populated
    only on Transient errors (message returned for redrive)
    (design §4.5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    itemIdentifier: str


class SqsBatchResponse(BaseModel):
    """The Lambda return value per the batch-item-failure protocol (design §3.4).

    A ``SqsBatchResponse`` with an empty ``batchItemFailures`` tuple tells
    SQS to delete every record in the received batch; a populated tuple
    tells SQS to redrive the listed records back to the source queue.
    See design §4.5 for the full classification table.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    batchItemFailures: tuple[BatchItemFailure, ...]


# ---------------------------------------------------------------------------
# Job_Message as the Orchestrator reads it (trikon-cloud-cost-cuts D6).
# ---------------------------------------------------------------------------


class VerifyJobMessage(SqsJobMessage):
    """A Job_Message as the Orchestrator reads it (cost-cuts Req 8.7, 8.8).

    :class:`SqsJobMessage` (written by the Webhook_Receiver) has neither
    field, so receiver bytes are unchanged and parse here as Attempt 1
    (Req 8.8). A Retry_Job (Attempt 2) is written only by the
    Spot_Retry_Handler and carries the original Reservation_Id.

    ``StrictInt`` rejects ``"2"``, ``2.0`` and ``true``. Any attempt other
    than 1 or 2, or a ``reservation_id`` that does not match the attempt,
    raises :class:`pydantic.ValidationError`, which the handler routes
    through the existing ``malformed_sqs_body`` path (Req 8.7).
    """

    attempt: Annotated[StrictInt, Field(ge=1, le=MAX_ATTEMPTS)] = 1
    reservation_id: ReservationId | None = None

    @model_validator(mode="after")
    def _retry_carries_reservation(self) -> VerifyJobMessage:
        """Attempt 2 requires ``reservation_id``; Attempt 1 forbids it."""
        if (self.attempt == MAX_ATTEMPTS) != (self.reservation_id is not None):
            raise ValueError("attempt 2 requires reservation_id; attempt 1 forbids it")
        return self


# ---------------------------------------------------------------------------
# §3.3 — ``ecs.RunTask`` request body.
# ---------------------------------------------------------------------------


class EnvOverride(BaseModel):
    """One entry of ``containerOverrides[0].environment`` (design §3.3).

    Both fields are ``str`` on the wire. The handler serializes the two
    integer :class:`SqsJobMessage` fields (``installation_id``,
    ``pr_number``) via ``str(...)`` when building the
    ``TRIKON_INSTALLATION_ID`` / ``TRIKON_PR_NUMBER`` entries — see
    Requirement 5.3 and :func:`build_run_task_call` in task 7.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: str


class ContainerOverride(BaseModel):
    """The single container-override entry emitted per RunTask call (design §3.3).

    ``name`` is typed :data:`Literal["runner"]` because the Spec 2
    task-definition declares exactly one container named ``runner``;
    any other name would fail at ``ecs.RunTask`` time. The eight-entry
    ``environment`` layout (Requirement 5.2's seven, then
    ``TRIKON_RESERVATION_ID``) is enforced by
    :func:`build_run_task_call`, NOT by this model — leaving
    the tuple open here so unit tests can construct partial fixtures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["runner"]
    environment: tuple[EnvOverride, ...]


class RunTaskOverrides(BaseModel):
    """The ``overrides`` sub-object of the RunTask body (design §3.3).

    ``taskRoleArn`` binds the per-installation IAM role provisioned by
    the Lifecycle_Handler — Requirement 3.4 fixes the format at
    ``arn:aws:iam::{account_id}:role/trikon-verify-task-role-{installation_id}``.
    ``containerOverrides`` is emitted as a single-element tuple per
    Requirement 5.1 by :func:`build_run_task_call`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    taskRoleArn: str
    containerOverrides: tuple[ContainerOverride, ...]


class AwsvpcConfiguration(BaseModel):
    """The ``awsvpcConfiguration`` sub-object (design §3.3).

    ``assignPublicIp`` is fixed at :data:`Literal["ENABLED"]`: runner tasks
    run in public subnets with no NAT gateway and reach the internet through
    the internet gateway, behind an egress-only security group with zero
    ingress rules (sandbox-minute-plans Req 14.5, 14.6). ``subnets`` and
    ``securityGroups`` are populated from
    :attr:`OrchestratorEnvConfig.trikon_runner_subnet_ids` and
    :attr:`OrchestratorEnvConfig.trikon_runner_security_group_ids`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subnets: tuple[str, ...]
    securityGroups: tuple[str, ...]
    assignPublicIp: Literal["ENABLED"]


class NetworkConfiguration(BaseModel):
    """The ``networkConfiguration`` sub-object of the RunTask body (design §3.3).

    ECS' RunTask API wraps :class:`AwsvpcConfiguration` under this one-field
    envelope; the extra level of nesting is required by boto3 even though
    the awsvpc launch type is the only one this spec uses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    awsvpcConfiguration: AwsvpcConfiguration


class EcsTag(BaseModel):
    """One entry of the ``tags`` array on the RunTask body (design §3.3).

    Requirement 3.5 fixes the tag set at exactly three entries in the order
    ``installation_id``, ``repo``, ``pr`` — enforced by
    :func:`build_run_task_call`'s use of :attr:`RunTaskCall.tags` typed as
    a fixed-length 3-tuple.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: str


class CapacityProviderStrategyItem(BaseModel):
    """One entry of the RunTask ``capacityProviderStrategy`` (cost-cuts D5).

    ``capacityProvider`` is ``FARGATE_SPOT`` or ``FARGATE``; the weight is
    pinned at 1 because the strategy always has exactly one entry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capacityProvider: Capacity
    weight: Literal[1]


class RunTaskCall(BaseModel):
    """The full ``ecs.RunTask`` request body (design §3.3, cost-cuts D5).

    ``.model_dump(by_alias=True, mode="json", exclude_none=True)`` on an
    instance yields the exact kwargs dict passed to
    ``boto3.client("ecs").run_task(**...)``. Pinned fields:

    * ``cluster`` is ``"trikon-verify-cluster"`` for the entire spec
      (Requirement 3.2, design §3.3).
    * Exactly one of ``launchType`` (``"FARGATE"``) and
      ``capacityProviderStrategy`` (one entry) is set, so a request can
      never fall back to the cluster's default strategy. The
      Orchestrator always sends the strategy (cost-cuts D5).
    * ``count`` is ``1`` — every RunTask call launches exactly one task
      instance (Requirement 3.1).
    * ``tags`` is a fixed-length ``tuple[EcsTag, EcsTag, EcsTag]`` — the
      three-tag contract from Requirement 3.5; the order-significant
      layout is enforced at :func:`build_run_task_call`.

    ``taskDefinition`` is the fully qualified ``family:revision`` string
    resolved once per cold start by :class:`TaskDefinitionResolver` — see
    design §4.3 and Requirement 4.1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster: Literal["trikon-verify-cluster"]
    taskDefinition: str
    launchType: Literal["FARGATE"] | None = None
    capacityProviderStrategy: tuple[CapacityProviderStrategyItem] | None = None
    count: Literal[1]
    networkConfiguration: NetworkConfiguration
    overrides: RunTaskOverrides
    tags: tuple[EcsTag, EcsTag, EcsTag]

    @model_validator(mode="after")
    def _exactly_one_placement(self) -> RunTaskCall:
        """Require exactly one of ``launchType`` and ``capacityProviderStrategy``."""
        if (self.launchType is None) == (self.capacityProviderStrategy is None):
            raise ValueError("exactly one of launchType and capacityProviderStrategy")
        return self


# ---------------------------------------------------------------------------
# §2.3 — Orchestrator Lambda environment-variable contract.
# ---------------------------------------------------------------------------


class OrchestratorEnvConfig(BaseSettings):
    """Orchestrator Lambda environment variables (design §2.3).

    The aliases below are populated by the CDK stack's Lambda
    environment block, except ``AWS_REGION`` which the Lambda runtime
    injects. ``TRIKON_QUOTA_TIME_BUDGET_SECONDS`` and
    ``TRIKON_QUEUE_WAIT_LIMIT_SECONDS`` are normally left to their
    defaults. A missing or malformed variable raises
    :class:`pydantic.ValidationError` at cold start — the handler fails
    before the first SQS message is dequeued, which is the correct
    behavior for a misconfigured dispatcher (Requirement 8.4, design §2.3).

    The two comma-separated aliases ``TRIKON_RUNNER_SUBNET_IDS`` and
    ``TRIKON_RUNNER_SECURITY_GROUP_IDS`` are exposed as
    ``tuple[str, ...]`` on the Python API. The raw env value is a
    single string; :class:`pydantic_settings.NoDecode` skips the default
    JSON pre-decode so :meth:`_split_comma_separated_ids` receives the
    raw CSV and splits it into a tuple.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    trikon_aws_account_id: str = Field(
        alias="TRIKON_AWS_ACCOUNT_ID", pattern=r"^\d{12}$"
    )
    trikon_runner_subnet_ids: Annotated[tuple[str, ...], NoDecode] = Field(
        alias="TRIKON_RUNNER_SUBNET_IDS"
    )
    trikon_runner_security_group_ids: Annotated[tuple[str, ...], NoDecode] = Field(
        alias="TRIKON_RUNNER_SECURITY_GROUP_IDS"
    )
    trikon_verify_runner_active_revision_ssm_param: str = Field(
        alias="TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM",
        default="/trikon/verify-runner/active-revision",
    )
    trikon_verify_jobs_dlq_url: str = Field(alias="TRIKON_VERIFY_JOBS_DLQ_URL")
    trikon_verdicts_table: str = Field(
        alias="TRIKON_VERDICTS_TABLE", default="trikon_verdicts"
    )
    # SSM SecureString parameter name of the GitHub App private key.
    trikon_app_private_key_parameter_name: str = Field(
        alias="TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME", min_length=2, pattern=r"^/"
    )
    trikon_app_id: int = Field(alias="TRIKON_APP_ID", ge=1)
    trikon_check_run_details_url_template: str = Field(
        alias="TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE",
        default="https://api.trikon.unideploy.com/audits/{delivery_id}",
    )
    aws_region: str = Field(alias="AWS_REGION", default="us-east-1")
    trikon_log_level: str = Field(alias="TRIKON_LOG_LEVEL", default="INFO")
    # Usage_API base URL and Trikon_Web_App_URL (sandbox-minute-plans
    # Req 11.14, 11.15). https only; trailing "/" stripped.
    trikon_web_api_url: str = Field(alias="TRIKON_WEB_API_URL")
    trikon_web_app_url: str = Field(alias="TRIKON_WEB_APP_URL")
    # SSM SecureString parameter name of the Usage_Secret.
    trikon_cloud_usage_secret_parameter_name: str = Field(
        alias="TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME", min_length=2, pattern=r"^/"
    )
    # Quota_Time_Budget: total time for one Quota_Endpoint call, retries included.
    trikon_quota_time_budget_seconds: float = Field(
        alias="TRIKON_QUOTA_TIME_BUDGET_SECONDS", default=10.0, gt=0, le=20
    )
    # Queue_Wait_Limit (Req 11.20, 11.21).
    trikon_queue_wait_limit_seconds: int = Field(
        alias="TRIKON_QUEUE_WAIT_LIMIT_SECONDS", default=1800, ge=60, le=86_400
    )
    # The orchestrator's own source queue, for busy copies (Req 11.18); CDK
    # sets it from the imported queue.
    trikon_verify_jobs_queue_url: str = Field(
        alias="TRIKON_VERIFY_JOBS_QUEUE_URL",
        pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/[A-Za-z0-9_-]{1,80}$",
    )
    # Spot_Flag (cost-cuts Req 8.2-8.4): Attempt 1 runs on FARGATE_SPOT when on.
    trikon_use_fargate_spot: bool = Field(alias="TRIKON_USE_FARGATE_SPOT", default=True)
    # The Runner's per-PR state table, read for the Interim_Check_Run id (Req 11).
    trikon_pr_state_table: str = Field(
        alias="TRIKON_PR_STATE_TABLE", default="trikon_pr_state"
    )
    # The verify-jobs queue's maxReceiveCount (Req 11.5).
    trikon_verify_jobs_max_receive_count: int = Field(
        alias="TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT", default=3, ge=1, le=1000
    )

    @field_validator("trikon_web_api_url", "trikon_web_app_url")
    @classmethod
    def _https_base_url(cls, value: str) -> str:
        """Require an https base URL and strip any trailing ``/`` (Req 11.15)."""
        return https_base_url(value)

    @field_validator(
        "trikon_runner_subnet_ids",
        "trikon_runner_security_group_ids",
        mode="before",
    )
    @classmethod
    def _split_comma_separated_ids(
        cls, value: str | tuple[str, ...] | list[str]
    ) -> tuple[str, ...]:
        """Parse a comma-separated env value into ``tuple[str, ...]``.

        Runs in ``mode="before"`` at the field level so the raw CSV
        string reaches this method (thanks to
        :class:`pydantic_settings.NoDecode` bypassing the default JSON
        pre-decode). Direct :class:`OrchestratorEnvConfig` construction
        under test may also pass a ``tuple`` or ``list`` — both are
        normalized to ``tuple[str, ...]``. Empty CSV fragments (e.g.
        ``"a,,b"``) are dropped, so an accidental trailing comma does
        not yield an empty subnet id.
        """
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(value)


# ---------------------------------------------------------------------------
# Spot_Retry_Handler Lambda environment (trikon-cloud-cost-cuts Req 9, 11).
# ---------------------------------------------------------------------------


class SpotRetryEnvConfig(BaseSettings):
    """Spot_Retry_Handler Lambda environment variables (cost-cuts design).

    Populated by the CDK stack's Lambda environment block, except
    ``AWS_REGION`` which the Lambda runtime injects. A missing or malformed
    variable raises :class:`pydantic.ValidationError` at cold start, before
    the first Task_Stopped_Event is handled. The queue URL pattern, the
    details-URL template default and the App key parameter rules match
    :class:`OrchestratorEnvConfig`.

    ``extra="ignore"`` because pydantic-settings sees every process env var.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    trikon_app_id: int = Field(alias="TRIKON_APP_ID", ge=1)
    # SSM SecureString parameter name of the GitHub App private key.
    trikon_app_private_key_parameter_name: str = Field(
        alias="TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME", min_length=2, pattern=r"^/"
    )
    # Destination of the Retry_Job (Req 9.3).
    trikon_verify_jobs_queue_url: str = Field(
        alias="TRIKON_VERIFY_JOBS_QUEUE_URL",
        pattern=_VERIFY_JOBS_QUEUE_URL_PATTERN,
    )
    trikon_verdicts_table: str = Field(
        alias="TRIKON_VERDICTS_TABLE", default="trikon_verdicts"
    )
    # The Runner's per-PR state table, read for the Interim_Check_Run id.
    trikon_pr_state_table: str = Field(
        alias="TRIKON_PR_STATE_TABLE", default="trikon_pr_state"
    )
    # The dedupe claim store (cost-cuts D3).
    trikon_spot_retries_table: str = Field(
        alias="TRIKON_SPOT_RETRIES_TABLE", default="trikon-cloud-spot-retries"
    )
    trikon_check_run_details_url_template: str = Field(
        alias="TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE",
        default="https://api.trikon.unideploy.com/audits/{delivery_id}",
    )
    trikon_log_level: str = Field(alias="TRIKON_LOG_LEVEL", default="INFO")
    aws_region: str = Field(alias="AWS_REGION", default="us-east-1")
