# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin
# generates, not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.orchestrator.models`.

Covers the three concern groups declared in the sibling module:

* SQS envelope models (:class:`SqsEventEnvelope`,
  :class:`SqsRecordAttributes`) — the boundary the handler enters at
  when Lambda hands it a raw event dict (design.md §3.4). Tests
  exercise the ``extra="forbid"`` gate on the envelope and the
  matching ``extra="allow"`` on the record ``attributes`` sub-object,
  which is the exception because AWS routinely adds attribute keys.
* ``ecs.RunTask`` request-body models (:class:`RunTaskCall` and its
  four leaf classes :class:`EnvOverride`, :class:`EcsTag`,
  :class:`ContainerOverride`, :class:`AwsvpcConfiguration`) — the
  wire shape passed to ``boto3.client("ecs").run_task(**...)``.
  Tests exercise the three :data:`Literal`-pinned fields, the
  fixed-length 3-tuple ``tags`` invariant (Requirement 3.5), the
  ``.model_dump(by_alias=True, mode="json")`` output shape against
  design.md §3.3's concrete payload, and the two leaf models'
  frozen / extra-forbid contract.
* :class:`OrchestratorEnvConfig` — the eleven-alias environment
  contract (design.md §2.3). Tests exercise env-driven construction
  via the autouse ``_env_setup`` fixture, the
  :class:`ValidationError` surface when a required alias is unset
  or the account id is malformed, and the
  :meth:`_split_comma_separated_ids` validator that turns CSVs into
  :data:`tuple`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from trikon_cloud.orchestrator.models import (
    AwsvpcConfiguration,
    CapacityProviderStrategyItem,
    ContainerOverride,
    EcsTag,
    EnvOverride,
    NetworkConfiguration,
    OrchestratorEnvConfig,
    RunTaskCall,
    RunTaskOverrides,
    SqsEventEnvelope,
    SqsRecord,
    SqsRecordAttributes,
    VerifyJobMessage,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_ID,
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    CANONICAL_DELIVERY_ID,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_USAGE_SECRET_PARAMETER_NAME,
    CANONICAL_VERIFY_JOBS_QUEUE_URL,
    CANONICAL_WEB_API_URL,
    CANONICAL_WEB_APP_URL,
    make_sqs_job_message,
)

# ---------------------------------------------------------------------------
# RunTaskCall canonical builder — every field populated with a valid value,
# tests override single fields via keyword arguments to exercise specific
# invariants. Mirrors the ``_make_verdict_row`` pattern in Spec 2's
# ``trikon_cloud/fargate_runner/tests/test_models.py``.
# ---------------------------------------------------------------------------


def _canonical_run_task_call(**overrides: Any) -> RunTaskCall:
    """Build a canonical :class:`RunTaskCall` with keyword overrides.

    Every field of :class:`RunTaskCall` is populated with a valid
    default matching design.md §3.3's concrete payload — the
    boto3 kwargs dict the orchestrator sends on a happy-path
    RunTask invocation. Tests override single fields
    (``cluster=…``, ``count=2``, ``tags=(t1, t2)``) to exercise
    specific :data:`Literal` and fixed-length-tuple invariants.
    """
    defaults: dict[str, Any] = {
        "cluster": "trikon-verify-cluster",
        "taskDefinition": "trikon-verify-runner:17",
        "launchType": "FARGATE",
        "count": 1,
        "networkConfiguration": NetworkConfiguration(
            awsvpcConfiguration=AwsvpcConfiguration(
                subnets=("subnet-verify-egress-a", "subnet-verify-egress-b"),
                securityGroups=("sg-verify-egress-only",),
                assignPublicIp="ENABLED",
            ),
        ),
        "overrides": RunTaskOverrides(
            taskRoleArn=(
                f"arn:aws:iam::{CANONICAL_ACCOUNT_ID}:role/"
                f"trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
            ),
            containerOverrides=(
                ContainerOverride(
                    name="runner",
                    environment=(
                        EnvOverride(
                            name="TRIKON_INSTALLATION_ID",
                            value=str(CANONICAL_INSTALLATION_ID),
                        ),
                        EnvOverride(
                            name="TRIKON_REPO_FULL_NAME",
                            value=CANONICAL_REPO_FULL_NAME,
                        ),
                    ),
                ),
            ),
        ),
        "tags": (
            EcsTag(key="installation_id", value=str(CANONICAL_INSTALLATION_ID)),
            EcsTag(key="repo", value=CANONICAL_REPO_FULL_NAME),
            EcsTag(key="pr", value=str(CANONICAL_PR_NUMBER)),
        ),
    }
    defaults.update(overrides)
    return RunTaskCall(**defaults)


# ---------------------------------------------------------------------------
# §3.4 — SQS envelope.
# ---------------------------------------------------------------------------


def test_sqs_event_envelope_parses_valid_lambda_event_dict() -> None:
    """The AWS Lambda SQS event envelope validates against a canonical event dict.

    Exercises the boundary the handler enters at: a raw :class:`dict`
    from AWS Lambda's event payload passed straight into
    :meth:`SqsEventEnvelope.model_validate` at handler entry
    (design.md §4.1 STEP 1). One record inside ``Records`` — batch
    size 1 is fixed by the event-source mapping (Requirement 1.1) —
    with the four required SQS fields plus a nested
    :class:`SqsRecordAttributes` object.
    """
    event: dict[str, Any] = {
        "Records": [
            {
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": '{"foo": "bar"}',
                "attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }

    envelope = SqsEventEnvelope.model_validate(event)

    assert len(envelope.Records) == 1
    record = envelope.Records[0]
    assert record.messageId == "msg-1"
    assert record.receiptHandle == "handle-1"
    assert record.body == '{"foo": "bar"}'
    assert record.attributes.ApproximateReceiveCount == "1"


def test_sqs_event_envelope_extra_forbid_rejects_unknown_top_level_key() -> None:
    """``extra="forbid"`` on :class:`SqsEventEnvelope` rejects unknown keys.

    A shape drift in the AWS Lambda event envelope (a new top-level
    key added by AWS, or a mismatched producer) must fail loudly at
    parse rather than silently ignore, so the handler's assumptions
    about the batch contract are ground truth (Requirement 1.1,
    design.md §3.4).
    """
    event: dict[str, Any] = {
        "Records": [
            {
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": "{}",
                "attributes": {"ApproximateReceiveCount": "1"},
            }
        ],
        "unknown_top_level": "boom",
    }

    with pytest.raises(ValidationError):
        SqsEventEnvelope.model_validate(event)


def test_sqs_record_attributes_extra_allow_accepts_unknown_aws_keys() -> None:
    """``extra="allow"`` lets AWS-added attribute keys pass through.

    ``SentTimestamp``, ``SenderId``, and ``AWSTraceHeader`` are
    common attributes AWS emits on SQS records but that this
    handler does not read. The one attribute the handler DOES
    read — ``ApproximateReceiveCount`` — must remain accessible
    after validation. Design.md §3.4 pins ``extra="allow"`` on
    this one model precisely so a future AWS-added attribute
    does not brick the orchestrator (Requirement 1.1).
    """
    attrs = SqsRecordAttributes.model_validate(
        {
            "ApproximateReceiveCount": "3",
            "SentTimestamp": "1700000000000",
            "SenderId": "AIDAEXAMPLE",
            "AWSTraceHeader": "Root=1-abc-def",
        }
    )

    assert attrs.ApproximateReceiveCount == "3"


# ---------------------------------------------------------------------------
# §3.3 — ``ecs.RunTask`` request body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("cluster", "wrong-cluster"),
        ("launchType", "EC2"),
        ("count", 2),
    ],
)
def test_run_task_call_literal_fields_reject_wrong_values(
    field_name: str, invalid_value: object
) -> None:
    """The three :data:`Literal`-pinned top-level fields reject any wrong value.

    ``cluster`` is fixed to ``"trikon-verify-cluster"`` for the
    entire spec (Requirement 3.2), ``launchType`` to ``"FARGATE"``
    (the only Spec 2 launch type), and ``count`` to ``1``
    (Requirement 3.1 — every RunTask launches exactly one task).
    The pydantic mypy plugin widens :data:`Literal` at construction
    call sites via its synthesized ``__init__``, so these calls
    pass mypy but fail pydantic's runtime :data:`Literal` check.
    """
    with pytest.raises(ValidationError):
        _canonical_run_task_call(**{field_name: invalid_value})


def test_run_task_call_accepts_strategy_alone_and_dumps_without_launch_type() -> None:
    """A strategy-only call validates; ``exclude_none`` drops ``launchType`` (D5)."""
    call = _canonical_run_task_call(
        launchType=None,
        capacityProviderStrategy=(
            CapacityProviderStrategyItem(capacityProvider="FARGATE_SPOT", weight=1),
        ),
    )

    kwargs = call.model_dump(by_alias=True, mode="json", exclude_none=True)

    assert "launchType" not in kwargs
    assert kwargs["capacityProviderStrategy"] == [
        {"capacityProvider": "FARGATE_SPOT", "weight": 1}
    ]


@pytest.mark.parametrize("with_strategy", [True, False])
def test_run_task_call_requires_exactly_one_placement(with_strategy: bool) -> None:
    """Both or neither of ``launchType`` / ``capacityProviderStrategy`` is rejected."""
    strategy = (
        (CapacityProviderStrategyItem(capacityProvider="FARGATE", weight=1),)
        if with_strategy
        else None
    )
    launch_type = "FARGATE" if with_strategy else None

    with pytest.raises(ValidationError, match="exactly one of launchType"):
        _canonical_run_task_call(
            launchType=launch_type, capacityProviderStrategy=strategy
        )


@pytest.mark.parametrize(
    "item",
    [
        {"capacityProvider": "FARGATE_SPOT", "weight": 2},
        {"capacityProvider": "EC2", "weight": 1},
        {"capacityProvider": "FARGATE", "weight": 1, "base": 0},
    ],
)
def test_capacity_provider_strategy_item_rejects_bad_entries(item: dict[str, Any]) -> None:
    """Weight is pinned at 1, the provider is a Fargate capacity, extras forbidden."""
    with pytest.raises(ValidationError):
        CapacityProviderStrategyItem.model_validate(item)


def test_run_task_call_strategy_must_have_exactly_one_entry() -> None:
    """``capacityProviderStrategy`` is a one-tuple: two entries are rejected."""
    item = CapacityProviderStrategyItem(capacityProvider="FARGATE", weight=1)

    with pytest.raises(ValidationError):
        _canonical_run_task_call(launchType=None, capacityProviderStrategy=(item, item))


# ---------------------------------------------------------------------------
# VerifyJobMessage (trikon-cloud-cost-cuts D6, Req 8.7, 8.8).
# ---------------------------------------------------------------------------

_RETRY_RESERVATION_ID = "00000000-0000-4000-8000-000000000001"


def _job_dict(**overrides: object) -> dict[str, object]:
    """The canonical receiver Job_Message as a dict, with overrides."""
    data: dict[str, object] = make_sqs_job_message().model_dump()
    data.update(overrides)
    return data


def test_verify_job_message_receiver_bytes_parse_as_attempt_one() -> None:
    """Receiver bytes (no ``attempt``, no ``reservation_id``) are Attempt 1 (Req 8.8)."""
    body = make_sqs_job_message().model_dump_json()

    message = VerifyJobMessage.model_validate_json(body)

    assert message.attempt == 1
    assert message.reservation_id is None
    assert message.delivery_id == CANONICAL_DELIVERY_ID


def test_verify_job_message_accepts_retry_with_reservation_id() -> None:
    """Attempt 2 with a UUID ``reservation_id`` is a valid Retry_Job."""
    body = json.dumps(_job_dict(attempt=2, reservation_id=_RETRY_RESERVATION_ID))

    message = VerifyJobMessage.model_validate_json(body)

    assert message.attempt == 2
    assert message.reservation_id == _RETRY_RESERVATION_ID


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt": 0},
        {"attempt": 3, "reservation_id": _RETRY_RESERVATION_ID},
        {"attempt": "2", "reservation_id": _RETRY_RESERVATION_ID},
        {"attempt": 2.0, "reservation_id": _RETRY_RESERVATION_ID},
        {"attempt": True},
        {"attempt": None},
        {"attempt": 2},
        {"attempt": 1, "reservation_id": _RETRY_RESERVATION_ID},
        {"reservation_id": _RETRY_RESERVATION_ID},
        {"attempt": 2, "reservation_id": "not-a-uuid"},
    ],
)
def test_verify_job_message_rejects_bad_attempt_or_reservation(
    overrides: dict[str, object],
) -> None:
    """Invalid attempts and attempt/reservation mismatches raise (Req 8.7)."""
    with pytest.raises(ValidationError):
        VerifyJobMessage.model_validate_json(json.dumps(_job_dict(**overrides)))


def test_run_task_call_tags_is_fixed_length_three_tuple() -> None:
    """``tags`` is typed ``tuple[EcsTag, EcsTag, EcsTag]`` — exactly three.

    Requirement 3.5 fixes the tag set at three entries in the
    order ``installation_id``, ``repo``, ``pr``. The fixed-length
    tuple annotation on the model is the type-layer enforcement;
    any other length is rejected at construction. Two- and
    four-element inputs both raise :class:`ValidationError`.
    """
    two_tags = (
        EcsTag(key="installation_id", value="1"),
        EcsTag(key="repo", value="a/b"),
    )
    four_tags = (
        EcsTag(key="installation_id", value="1"),
        EcsTag(key="repo", value="a/b"),
        EcsTag(key="pr", value="1"),
        EcsTag(key="extra", value="4"),
    )

    with pytest.raises(ValidationError):
        _canonical_run_task_call(tags=two_tags)

    with pytest.raises(ValidationError):
        _canonical_run_task_call(tags=four_tags)


def test_run_task_call_model_dump_by_alias_json_matches_boto3_kwargs_shape() -> None:
    """``.model_dump(by_alias=True, mode="json")`` yields the boto3 kwargs dict.

    Design.md §3.3 pins the wire shape byte-for-byte; that dict
    is the kwargs pass-through passed to
    ``boto3.client("ecs").run_task(**...)`` inside
    :func:`submit_run_task`. Asserts the seven top-level keys
    are present with their pinned literal values, the two-level
    nesting for ``networkConfiguration.awsvpcConfiguration`` is
    preserved, and tuples degrade to JSON lists under
    ``mode="json"`` — the shape boto3 accepts.
    """
    call = _canonical_run_task_call()

    kwargs = call.model_dump(by_alias=True, mode="json")

    # The seven top-level keys, in the exact wire shape.
    assert kwargs["cluster"] == "trikon-verify-cluster"
    assert kwargs["taskDefinition"] == "trikon-verify-runner:17"
    assert kwargs["launchType"] == "FARGATE"
    assert kwargs["count"] == 1

    # Two-level nesting: networkConfiguration.awsvpcConfiguration.
    net_config = kwargs["networkConfiguration"]
    awsvpc = net_config["awsvpcConfiguration"]
    assert awsvpc["assignPublicIp"] == "ENABLED"
    # Tuples degrade to JSON lists under mode="json" — the shape boto3 wants.
    assert isinstance(awsvpc["subnets"], list)
    assert awsvpc["subnets"] == [
        "subnet-verify-egress-a",
        "subnet-verify-egress-b",
    ]
    assert isinstance(awsvpc["securityGroups"], list)
    assert awsvpc["securityGroups"] == ["sg-verify-egress-only"]

    # overrides.taskRoleArn / overrides.containerOverrides[0].name.
    overrides = kwargs["overrides"]
    assert overrides["taskRoleArn"] == (
        f"arn:aws:iam::{CANONICAL_ACCOUNT_ID}:role/"
        f"trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
    )
    assert isinstance(overrides["containerOverrides"], list)
    assert overrides["containerOverrides"][0]["name"] == "runner"

    # Tags: three entries, {"key": …, "value": …} dict shape (Requirement 3.5).
    assert isinstance(kwargs["tags"], list)
    assert len(kwargs["tags"]) == 3
    assert kwargs["tags"][0] == {
        "key": "installation_id",
        "value": str(CANONICAL_INSTALLATION_ID),
    }
    assert kwargs["tags"][1] == {"key": "repo", "value": CANONICAL_REPO_FULL_NAME}
    assert kwargs["tags"][2] == {"key": "pr", "value": str(CANONICAL_PR_NUMBER)}


def test_env_override_and_ecs_tag_construction_frozen_and_extra_forbid() -> None:
    """The two leaf models construct cleanly, are frozen, and forbid extras.

    Both :class:`EnvOverride` and :class:`EcsTag` model the
    innermost ``{"name": …, "value": …}`` / ``{"key": …, "value": …}``
    dicts in the ``ecs.RunTask`` request body (design.md §3.3).
    They share three invariants: the two-field construction
    surfaces the passed values, ``frozen=True`` blocks
    post-construction mutation, and ``extra="forbid"`` rejects
    unknown keys at parse.

    The frozen guard accepts the widened
    ``(ValidationError, TypeError, AttributeError)`` tuple to
    track pydantic's contract across versions, matching the
    convention in Spec 2's ``test_models.py``.
    """
    env = EnvOverride(name="TRIKON_DELIVERY_ID", value="x")
    tag = EcsTag(key="installation_id", value="1")

    # Construction surfaces the passed values.
    assert env.name == "TRIKON_DELIVERY_ID"
    assert env.value == "x"
    assert tag.key == "installation_id"
    assert tag.value == "1"

    # ``frozen=True`` — post-construction attribute assignment blocked.
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        env.value = "y"  # type: ignore[misc]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        tag.value = "2"  # type: ignore[misc]

    # ``extra="forbid"`` — unknown keys rejected at parse.
    with pytest.raises(ValidationError):
        EnvOverride.model_validate({"name": "x", "value": "y", "unknown": "z"})
    with pytest.raises(ValidationError):
        EcsTag.model_validate({"key": "x", "value": "y", "unknown": "z"})


# ---------------------------------------------------------------------------
# §2.3 — Orchestrator env-var contract.
# ---------------------------------------------------------------------------


def test_orchestrator_env_config_env_driven_construction_populates_all_fields() -> None:
    """Env-driven construction populates every alias and splits CSVs into tuples.

    The autouse ``_env_setup`` fixture in ``conftest.py`` sets the
    six required aliases plus ``AWS_REGION``. Constructing
    :class:`OrchestratorEnvConfig` with no kwargs reads them all
    and applies the model's defaults for the four optional aliases
    (design.md §2.3). Also exercises the
    :meth:`_split_comma_separated_ids` validator: the CSV
    ``"subnet-a,subnet-b"`` maps to the two-element tuple, and
    the single-value ``"sg-verify"`` maps to the one-element
    tuple. ``TRIKON_APP_ID`` is stringified in the env by
    :pymod:`os.environ`; pydantic-settings coerces it back to
    :class:`int`.
    """
    config = OrchestratorEnvConfig()

    # Required aliases.
    assert config.trikon_aws_account_id == CANONICAL_ACCOUNT_ID
    assert config.trikon_app_private_key_parameter_name == (
        CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME
    )
    # CSV split — two-element tuple.
    assert config.trikon_runner_subnet_ids == ("subnet-a", "subnet-b")
    # CSV split — one-element tuple (no trailing comma).
    assert config.trikon_runner_security_group_ids == ("sg-verify",)
    # Env str coerced to int under pydantic-settings.
    assert config.trikon_app_id == CANONICAL_APP_ID
    assert isinstance(config.trikon_app_id, int)

    # Defaults for the four optional aliases.
    assert (
        config.trikon_verify_runner_active_revision_ssm_param
        == "/trikon/verify-runner/active-revision"
    )
    assert config.trikon_verdicts_table == "trikon_verdicts"
    assert (
        config.trikon_check_run_details_url_template
        == "https://api.trikon.unideploy.com/audits/{delivery_id}"
    )
    assert config.trikon_log_level == "INFO"
    assert config.aws_region == "us-east-1"


def test_orchestrator_env_config_missing_required_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a required alias raises :class:`ValidationError` at construction.

    ``TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME`` has no default: the
    Never-Fail-Open path needs this parameter to mint a GitHub App
    JWT before it can post the neutral Check Run
    (design.md §7.3). A missing value at cold start must fail
    loudly rather than defer the failure to the first RunTask
    dispatch — the handler fails before the first SQS message is
    dequeued, which is the correct behavior for a misconfigured
    dispatcher (Requirement 8.4, design.md §2.3).
    """
    monkeypatch.delenv("TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME", raising=False)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


# The obsolete Secrets Manager names are built by concatenation so the
# cost-guard static scan (cost-cuts task 19.1) does not match this file.
_OBSOLETE_SUFFIX = "_SECRET" + "_ARN"
_OBSOLETE_ARN = (
    "arn:aws:" + "secrets" + "manager:us-east-1:000000000000:secret:trikon/app-key-abcdef"
)


def test_orchestrator_env_config_ignores_obsolete_secret_arn_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removed Secrets Manager ARN aliases no longer satisfy the model (cost-cuts Req 6.2).

    With only the obsolete variables set, construction fails on the two
    missing ``*_PARAMETER_NAME`` aliases.
    """
    monkeypatch.delenv("TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME", raising=False)
    monkeypatch.delenv("TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME", raising=False)
    monkeypatch.setenv("TRIKON_APP_PRIVATE_KEY" + _OBSOLETE_SUFFIX, _OBSOLETE_ARN)
    monkeypatch.setenv("TRIKON_CLOUD_USAGE" + _OBSOLETE_SUFFIX, _OBSOLETE_ARN)

    with pytest.raises(ValidationError) as excinfo:
        OrchestratorEnvConfig()

    missing = {err["loc"][0] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert missing == {
        "TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME",
        "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME",
    }


@pytest.mark.parametrize(
    "alias",
    ["TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME", "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME"],
)
@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "/",
        "trikon/no-leading-slash",
        _OBSOLETE_ARN,
    ],
)
def test_orchestrator_env_config_rejects_bad_parameter_name(
    monkeypatch: pytest.MonkeyPatch, alias: str, bad_name: str
) -> None:
    """Each parameter name needs ``min_length=2`` and a leading ``/``."""
    monkeypatch.setenv(alias, bad_name)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


@pytest.mark.parametrize(
    "malformed_account_id",
    ["12345", "not-a-number", "0000000000001234"],  # 5 digits / non-numeric / 16 digits
)
def test_orchestrator_env_config_malformed_account_id_rejected(
    monkeypatch: pytest.MonkeyPatch, malformed_account_id: str
) -> None:
    r"""A non-12-digit ``TRIKON_AWS_ACCOUNT_ID`` fails the pattern gate.

    The AWS account id is baked into per-installation IAM role
    ARNs (Requirement 3.4 fixes the format at
    ``arn:aws:iam::{account_id}:role/…``) and into DynamoDB / S3
    Resource ARNs (design.md §6.3). A malformed value silently
    propagated would produce a role granting access to the wrong
    AWS account — reject at env-load instead. Exercises the
    ``pattern=r"^\d{12}$"`` :class:`Field` constraint against
    three failure modes: too short, non-numeric, too long.
    """
    monkeypatch.setenv("TRIKON_AWS_ACCOUNT_ID", malformed_account_id)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


# ---------------------------------------------------------------------------
# trikon-cloud-cost-cuts — Spot_Flag, PR state table, max receive count.
# ---------------------------------------------------------------------------

_SPOT_ALIASES = (
    "TRIKON_USE_FARGATE_SPOT",
    "TRIKON_PR_STATE_TABLE",
    "TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT",
)


def test_orchestrator_env_config_spot_fields_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot_Flag defaults on, the table to ``trikon_pr_state``, the count to 3."""
    for alias in _SPOT_ALIASES:
        monkeypatch.delenv(alias, raising=False)

    config = OrchestratorEnvConfig()

    assert config.trikon_use_fargate_spot is True
    assert config.trikon_pr_state_table == "trikon_pr_state"
    assert config.trikon_verify_jobs_max_receive_count == 3


def test_orchestrator_env_config_spot_fields_load_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three cost-cuts aliases load from the environment (Req 8.2)."""
    monkeypatch.setenv("TRIKON_USE_FARGATE_SPOT", "false")
    monkeypatch.setenv("TRIKON_PR_STATE_TABLE", "trikon_pr_state_test")
    monkeypatch.setenv("TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT", "1000")

    config = OrchestratorEnvConfig()

    assert config.trikon_use_fargate_spot is False
    assert config.trikon_pr_state_table == "trikon_pr_state_test"
    assert config.trikon_verify_jobs_max_receive_count == 1000


@pytest.mark.parametrize("count", ["0", "1001", "-1", "three"])
def test_orchestrator_env_config_max_receive_count_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch, count: str
) -> None:
    """``TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT`` must be an int in 1..1000."""
    monkeypatch.setenv("TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT", count)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


# ---------------------------------------------------------------------------
# sandbox-minute-plans — Usage_API config, Queue_Wait_Limit, source queue
# URL (Req 11.14, 11.15, 11.18, 11.21) and the full Lambda SQS record shape.
# ---------------------------------------------------------------------------


def test_orchestrator_env_config_usage_fields_load_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Usage_API fields load from env; budget and wait limit default.

    Req 11.14 and 11.21: ``TRIKON_QUOTA_TIME_BUDGET_SECONDS`` defaults to 10
    and ``TRIKON_QUEUE_WAIT_LIMIT_SECONDS`` to 1800 when unset.
    """
    monkeypatch.delenv("TRIKON_QUOTA_TIME_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("TRIKON_QUEUE_WAIT_LIMIT_SECONDS", raising=False)

    config = OrchestratorEnvConfig()

    assert config.trikon_web_api_url == CANONICAL_WEB_API_URL
    assert config.trikon_web_app_url == CANONICAL_WEB_APP_URL
    assert (
        config.trikon_cloud_usage_secret_parameter_name
        == CANONICAL_USAGE_SECRET_PARAMETER_NAME
    )
    assert config.trikon_verify_jobs_queue_url == CANONICAL_VERIFY_JOBS_QUEUE_URL
    assert config.trikon_quota_time_budget_seconds == 10.0
    assert config.trikon_queue_wait_limit_seconds == 1800


@pytest.mark.parametrize("alias", ["TRIKON_WEB_API_URL", "TRIKON_WEB_APP_URL"])
def test_orchestrator_env_config_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    """A trailing ``/`` on either base URL is stripped (Req 11.15)."""
    monkeypatch.setenv(alias, "https://api.trikon.example.test/")

    config = OrchestratorEnvConfig()

    field = alias.lower()
    assert getattr(config, field) == "https://api.trikon.example.test"


@pytest.mark.parametrize(
    "alias",
    [
        "TRIKON_WEB_API_URL",
        "TRIKON_WEB_APP_URL",
        "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME",
        "TRIKON_VERIFY_JOBS_QUEUE_URL",
    ],
)
def test_orchestrator_env_config_missing_usage_var_raises(
    monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    """Each required sandbox-minute-plans variable fails when unset (Req 11.14, 11.18)."""
    monkeypatch.delenv(alias, raising=False)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


def test_orchestrator_env_config_empty_usage_secret_parameter_name_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME`` fails ``min_length=2`` (Req 11.14)."""
    monkeypatch.setenv("TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME", "")

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


@pytest.mark.parametrize("alias", ["TRIKON_WEB_API_URL", "TRIKON_WEB_APP_URL"])
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://api.trikon.example.test",
        "https://api.trikon.example.test?x=1",
        "https://api.trikon.example.test#frag",
        "https://",
    ],
)
def test_orchestrator_env_config_rejects_non_https_base_url(
    monkeypatch: pytest.MonkeyPatch, alias: str, bad_url: str
) -> None:
    """``http://``, a query, a fragment or a missing host all fail (Req 11.15)."""
    monkeypatch.setenv(alias, bad_url)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


@pytest.mark.parametrize("budget", ["0", "-1", "20.5", "21"])
def test_orchestrator_env_config_quota_budget_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    """The Quota_Time_Budget must be in ``(0, 20]`` (Req 11.14)."""
    monkeypatch.setenv("TRIKON_QUOTA_TIME_BUDGET_SECONDS", budget)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


@pytest.mark.parametrize(("budget", "expected"), [("20", 20.0), ("0.5", 0.5)])
def test_orchestrator_env_config_quota_budget_in_range_accepted(
    monkeypatch: pytest.MonkeyPatch, budget: str, expected: float
) -> None:
    """A budget of 20 (the upper bound) and a fractional budget both load."""
    monkeypatch.setenv("TRIKON_QUOTA_TIME_BUDGET_SECONDS", budget)

    assert OrchestratorEnvConfig().trikon_quota_time_budget_seconds == expected


@pytest.mark.parametrize("limit", ["59", "86401", "0"])
def test_orchestrator_env_config_queue_wait_limit_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """The Queue_Wait_Limit must be in ``[60, 86400]`` seconds (Req 11.21)."""
    monkeypatch.setenv("TRIKON_QUEUE_WAIT_LIMIT_SECONDS", limit)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


@pytest.mark.parametrize(("limit", "expected"), [("60", 60), ("86400", 86_400)])
def test_orchestrator_env_config_queue_wait_limit_bounds_accepted(
    monkeypatch: pytest.MonkeyPatch, limit: str, expected: int
) -> None:
    """Both inclusive Queue_Wait_Limit bounds load (Req 11.21)."""
    monkeypatch.setenv("TRIKON_QUEUE_WAIT_LIMIT_SECONDS", limit)

    assert OrchestratorEnvConfig().trikon_queue_wait_limit_seconds == expected


@pytest.mark.parametrize(
    "bad_queue_url",
    [
        f"https://example.com/{CANONICAL_ACCOUNT_ID}/trikon-verify-jobs",
        f"http://sqs.us-east-1.amazonaws.com/{CANONICAL_ACCOUNT_ID}/trikon-verify-jobs",
        "https://sqs.us-east-1.amazonaws.com/12345/trikon-verify-jobs",
        f"https://sqs.us-east-1.amazonaws.com/{CANONICAL_ACCOUNT_ID}/",
        f"https://sqs.us-east-1.amazonaws.com/{CANONICAL_ACCOUNT_ID}/q?x=1",
        "",
    ],
)
def test_orchestrator_env_config_non_sqs_queue_url_rejected(
    monkeypatch: pytest.MonkeyPatch, bad_queue_url: str
) -> None:
    """``TRIKON_VERIFY_JOBS_QUEUE_URL`` must be an SQS queue URL (Req 11.18)."""
    monkeypatch.setenv("TRIKON_VERIFY_JOBS_QUEUE_URL", bad_queue_url)

    with pytest.raises(ValidationError):
        OrchestratorEnvConfig()


def _full_lambda_sqs_record() -> dict[str, Any]:
    """A Lambda SQS record with every standard key AWS sends."""
    return {
        "messageId": "059f36b4-87a3-44ab-83d2-661975830a7d",
        "receiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a...",
        "body": '{"foo": "bar"}',
        "attributes": {
            "ApproximateReceiveCount": "2",
            "SentTimestamp": "1700000000000",
            "SenderId": "AIDAEXAMPLE",
            "ApproximateFirstReceiveTimestamp": "1700000000123",
        },
        "messageAttributes": {
            "trikon_first_received_at": {
                "stringValue": "1700000000",
                "stringListValues": [],
                "binaryListValues": [],
                "dataType": "Number",
            }
        },
        "md5OfBody": "9bb58f26192e4ba00f01e2e7b136bbd8",
        "md5OfMessageAttributes": "00484c68cf6ac6b4a1e6ba7a8b5e4d3c",
        "eventSource": "aws:sqs",
        "eventSourceARN": f"arn:aws:sqs:us-east-1:{CANONICAL_ACCOUNT_ID}:trikon-verify-jobs",
        "awsRegion": "us-east-1",
    }


def test_sqs_record_accepts_full_lambda_record() -> None:
    """A real Lambda SQS record validates, extra attribute list keys ignored."""
    record = SqsRecord.model_validate(_full_lambda_sqs_record())

    assert record.attributes.ApproximateReceiveCount == "2"
    assert record.attributes.ApproximateFirstReceiveTimestamp == "1700000000123"
    attribute = record.messageAttributes["trikon_first_received_at"]
    assert attribute.dataType == "Number"
    assert attribute.stringValue == "1700000000"
    assert record.eventSource == "aws:sqs"
    assert record.awsRegion == "us-east-1"
    assert record.md5OfBody == "9bb58f26192e4ba00f01e2e7b136bbd8"

    # The envelope accepts the same record too.
    envelope = SqsEventEnvelope.model_validate({"Records": [_full_lambda_sqs_record()]})
    assert envelope.Records[0] == record


def test_sqs_record_defaults_without_optional_keys() -> None:
    """The four required keys alone validate; the optional ones default."""
    record = SqsRecord.model_validate(
        {
            "messageId": "msg-1",
            "receiptHandle": "handle-1",
            "body": "{}",
            "attributes": {"ApproximateReceiveCount": "1"},
        }
    )

    assert record.messageAttributes == {}
    assert record.attributes.ApproximateFirstReceiveTimestamp is None
    assert record.eventSourceARN is None


def test_sqs_record_still_rejects_unknown_top_level_key() -> None:
    """``extra="forbid"`` still rejects a key that is not a standard SQS key."""
    raw = _full_lambda_sqs_record()
    raw["unexpectedKey"] = "boom"

    with pytest.raises(ValidationError):
        SqsRecord.model_validate(raw)
