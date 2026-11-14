# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin generates,
# not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Property test for the RunTask call (trikon-cloud-cost-cuts, Property 6).

Folds in sandbox-minute-plans Property 27 (reservation id and public IP).

For any valid :class:`VerifyJobMessage`, Reservation_Id, Spot_Flag value
``f``, Attempt ``n`` in {1, 2} and subnet / security-group configuration,
the call that :func:`build_run_task_call` builds and
:func:`submit_run_task` sends:

* has no ``launchType`` key and no ``None`` value anywhere;
* has ``capacityProviderStrategy == [{"capacityProvider": c, "weight": 1}]``
  with ``c == "FARGATE_SPOT"`` exactly when ``f`` and ``n == 1``;
* has one ``runner`` override whose environment is the eight existing
  entries (seven message fields, then ``TRIKON_RESERVATION_ID``) in fixed
  order, followed by ``TRIKON_ATTEMPT=str(n)`` and ``TRIKON_CAPACITY=c``;
* has ``assignPublicIp == "ENABLED"`` and unchanged tags.

Any ``assignPublicIp`` other than ``ENABLED`` is rejected by
:class:`AwsvpcConfiguration`, and :class:`RunTaskCall` validates if and
only if exactly one of ``launchType`` and ``capacityProviderStrategy`` is
present.

The expected request is rebuilt here from the inputs, not read back from
``ecs_dispatcher``, so ordering, naming or capacity drift fails here.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import Attempt, select_capacity
from trikon_cloud.orchestrator.ecs_dispatcher import (
    build_run_task_call,
    submit_run_task,
)
from trikon_cloud.orchestrator.models import (
    AwsvpcConfiguration,
    OrchestratorEnvConfig,
    RunTaskCall,
    VerifyJobMessage,
)

# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdef"
_SHAS = st.text(alphabet=_HEX, min_size=40, max_size=40)
_NAME_PART = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1,
    max_size=39,
)
# Always "owner/name": a "/" means a repo name can never equal a UUID, so the
# "tags don't carry the reservation id" check is not vacuous by coincidence.
_REPO_FULL_NAMES = st.builds(lambda owner, name: f"{owner}/{name}", _NAME_PART, _NAME_PART)
_EVENT_TYPES = st.one_of(
    st.sampled_from(
        ["pull_request.opened", "pull_request.synchronize", "pull_request.reopened"]
    ),
    st.text(min_size=1, max_size=40),
)
_DELIVERY_IDS = st.one_of(st.uuids().map(str), st.text(min_size=1, max_size=64))
_SENT_ATS = st.datetimes(timezones=st.just(UTC)).map(
    lambda dt: dt.isoformat(timespec="milliseconds")
)

# Reservation ids are UUIDs; accept either case and pass through verbatim.
_RESERVATION_IDS = st.builds(
    lambda u, upper: str(u).upper() if upper else str(u),
    st.uuids(),
    st.booleans(),
)

_ATTEMPTS: st.SearchStrategy[Attempt] = st.sampled_from((1, 2))


@st.composite
def _jobs(draw: st.DrawFn) -> tuple[VerifyJobMessage, str, Attempt]:
    """A valid Job_Message, the Reservation_Id it dispatches with, and its Attempt.

    Attempt 1 carries no reservation id (the Quota_Endpoint supplies one);
    a Retry_Job (Attempt 2) carries the original one, which the
    Orchestrator dispatches with.
    """
    attempt = draw(_ATTEMPTS)
    reservation_id = draw(_RESERVATION_IDS)
    message = VerifyJobMessage(
        installation_id=draw(st.integers(min_value=1, max_value=2**53)),
        repo_full_name=draw(_REPO_FULL_NAMES),
        pr_number=draw(st.integers(min_value=1, max_value=10**9)),
        head_sha=draw(_SHAS),
        base_sha=draw(_SHAS),
        event_type=draw(_EVENT_TYPES),
        sent_at=draw(_SENT_ATS),
        delivery_id=draw(_DELIVERY_IDS),
        attempt=attempt,
        reservation_id=reservation_id if attempt == 2 else None,
    )
    return message, reservation_id, attempt


_SUBNETS = st.lists(
    st.text(alphabet=_HEX, min_size=8, max_size=17).map(lambda s: f"subnet-{s}"),
    min_size=1,
    max_size=6,
    unique=True,
).map(tuple)
_SECURITY_GROUPS = st.lists(
    st.text(alphabet=_HEX, min_size=8, max_size=17).map(lambda s: f"sg-{s}"),
    min_size=1,
    max_size=5,
    unique=True,
).map(tuple)
_TASK_DEFINITIONS = st.integers(min_value=1, max_value=10**6).map(
    lambda rev: f"trikon-verify-runner:{rev}"
)
_NON_ENABLED = st.one_of(
    st.sampled_from(["DISABLED", "enabled", "Enabled", "ENABLED ", " ENABLED", ""]),
    st.text(max_size=20),
).filter(lambda value: value != "ENABLED")

_ENV_NAMES = (
    "TRIKON_INSTALLATION_ID",
    "TRIKON_REPO_FULL_NAME",
    "TRIKON_PR_NUMBER",
    "TRIKON_HEAD_SHA",
    "TRIKON_BASE_SHA",
    "TRIKON_EVENT_TYPE",
    "TRIKON_DELIVERY_ID",
    "TRIKON_RESERVATION_ID",
    "TRIKON_ATTEMPT",
    "TRIKON_CAPACITY",
)

_TASK_ARN = "arn:aws:ecs:us-east-1:123456789012:task/trikon-verify-cluster/0123abcd"


class _RecordingEcsClient:
    """Captures the kwargs :func:`submit_run_task` sends; returns one task."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_task(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"tasks": [{"taskArn": _TASK_ARN}], "failures": []}


def _env_config(
    subnets: tuple[str, ...], security_groups: tuple[str, ...], *, use_spot: bool
) -> OrchestratorEnvConfig:
    """The conftest autouse env, with generated subnets, groups and Spot_Flag.

    Validated through the alias names so the ``_split_comma_separated_ids``
    path for tuple input runs too.
    """
    data = OrchestratorEnvConfig().model_dump(by_alias=True)
    data["TRIKON_RUNNER_SUBNET_IDS"] = subnets
    data["TRIKON_RUNNER_SECURITY_GROUP_IDS"] = security_groups
    data["TRIKON_USE_FARGATE_SPOT"] = use_spot
    return OrchestratorEnvConfig.model_validate(data)


def _contains_none(value: object) -> bool:
    """True if ``value`` is ``None`` or holds ``None`` at any depth."""
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_none(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_none(item) for item in value)
    return False


# ---------------------------------------------------------------------------
# Property 6 (folds in sandbox-minute-plans Property 27).
# ---------------------------------------------------------------------------


# Feature: trikon-cloud-cost-cuts, Property 6: RunTask capacity selection and request shape
@given(
    job=_jobs(),
    use_spot=st.booleans(),
    subnets=_SUBNETS,
    security_groups=_SECURITY_GROUPS,
    task_definition=_TASK_DEFINITIONS,
    bad_assign_public_ip=_NON_ENABLED,
    has_launch_type=st.booleans(),
    has_strategy=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_property_run_task_capacity_selection_and_request_shape(
    job: tuple[VerifyJobMessage, str, Attempt],
    use_spot: bool,
    subnets: tuple[str, ...],
    security_groups: tuple[str, ...],
    task_definition: str,
    bad_assign_public_ip: str,
    has_launch_type: bool,
    has_strategy: bool,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 6: RunTask capacity selection and request shape.

    Also sandbox-minute-plans Property 27: the RunTask call carries the
    reservation and a public IP (Requirements 11.3, 14.5, 14.6 of that spec).

    **Validates: Requirements 8.3, 8.4, 8.5, 8.6, 8.9**
    """
    message, reservation_id, attempt = job
    env = _env_config(subnets, security_groups, use_spot=use_spot)

    # Capacity selection, as the handler does it (Req 8.3-8.5): Spot only
    # for Attempt 1 with the Spot_Flag on, on-demand otherwise.
    expected_capacity = "FARGATE_SPOT" if use_spot and attempt == 1 else "FARGATE"
    capacity = select_capacity(use_spot=env.trikon_use_fargate_spot, attempt=attempt)
    assert capacity == expected_capacity

    call = build_run_task_call(
        message,
        env=env,
        task_definition=task_definition,
        reservation_id=reservation_id,
        attempt=attempt,
        capacity=capacity,
    )

    ecs = _RecordingEcsClient()
    result = submit_run_task(call, ecs_client=ecs)
    assert result.task_arn == _TASK_ARN
    assert len(ecs.calls) == 1
    sent = ecs.calls[0]

    expected_env = [
        {"name": "TRIKON_INSTALLATION_ID", "value": str(message.installation_id)},
        {"name": "TRIKON_REPO_FULL_NAME", "value": message.repo_full_name},
        {"name": "TRIKON_PR_NUMBER", "value": str(message.pr_number)},
        {"name": "TRIKON_HEAD_SHA", "value": message.head_sha},
        {"name": "TRIKON_BASE_SHA", "value": message.base_sha},
        {"name": "TRIKON_EVENT_TYPE", "value": message.event_type},
        {"name": "TRIKON_DELIVERY_ID", "value": message.delivery_id},
        {"name": "TRIKON_RESERVATION_ID", "value": reservation_id},
        {"name": "TRIKON_ATTEMPT", "value": str(attempt)},
        {"name": "TRIKON_CAPACITY", "value": expected_capacity},
    ]
    expected_tags = [
        {"key": "installation_id", "value": str(message.installation_id)},
        {"key": "repo", "value": message.repo_full_name},
        {"key": "pr", "value": str(message.pr_number)},
    ]

    # The wire request: one-entry strategy, no launchType, no None values
    # (boto3 rejects None parameters) — cost-cuts D5, Req 8.3-8.5.
    assert "launchType" not in sent
    assert not _contains_none(sent)
    assert sent["capacityProviderStrategy"] == [
        {"capacityProvider": expected_capacity, "weight": 1}
    ]
    assert sent["cluster"] == "trikon-verify-cluster"
    assert sent["count"] == 1
    assert sent["taskDefinition"] == task_definition

    # Public IP, subnets and security groups (minute-plans Req 14.5, 14.6).
    assert sent["networkConfiguration"] == {
        "awsvpcConfiguration": {
            "subnets": list(subnets),
            "securityGroups": list(security_groups),
            "assignPublicIp": "ENABLED",
        }
    }

    # One "runner" override: the eight existing entries in fixed order, then
    # TRIKON_ATTEMPT and TRIKON_CAPACITY (minute-plans Req 11.3, cost-cuts
    # Req 8.6). The reservation id is verbatim (case preserved).
    assert sent["overrides"] == {
        "taskRoleArn": (
            f"arn:aws:iam::{env.trikon_aws_account_id}"
            f":role/trikon-verify-task-role-{message.installation_id}"
        ),
        "containerOverrides": [{"name": "runner", "environment": expected_env}],
    }
    environment = call.overrides.containerOverrides[0].environment
    assert tuple(entry.name for entry in environment) == _ENV_NAMES

    # Tags are unchanged and never carry the reservation id.
    assert sent["tags"] == expected_tags
    assert reservation_id not in {tag["value"] for tag in expected_tags}

    # Any assignPublicIp other than ENABLED is rejected (minute-plans Req 14.5).
    with pytest.raises(ValidationError):
        AwsvpcConfiguration.model_validate(
            {
                "subnets": subnets,
                "securityGroups": security_groups,
                "assignPublicIp": bad_assign_public_ip,
            }
        )

    # RunTaskCall validates iff exactly one placement key is present (D5).
    data = {
        key: value
        for key, value in sent.items()
        if key not in {"launchType", "capacityProviderStrategy"}
    }
    if has_launch_type:
        data["launchType"] = "FARGATE"
    if has_strategy:
        data["capacityProviderStrategy"] = sent["capacityProviderStrategy"]
    if has_launch_type != has_strategy:
        RunTaskCall.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            RunTaskCall.model_validate(data)
