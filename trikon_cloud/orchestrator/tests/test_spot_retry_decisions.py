"""Examples for the Spot_Retry_Handler's pure decision logic (cost-cuts task 14.1).

Covers :func:`decide_stopped_task` row by row (Req 9.2, 9.6, 11.2, 11.3,
11.4), :func:`build_retry_job`, :func:`retry_sent_at` and the override
parsers. The decision-table and round-trip properties (Properties 7 and 9)
live in ``test_spot_retry_properties.py``.
"""

from __future__ import annotations

import logging
from typing import Final

import pytest

from trikon_cloud.orchestrator.ecs_dispatcher import build_run_task_call
from trikon_cloud.orchestrator.models import OrchestratorEnvConfig, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import FailClosedTarget
from trikon_cloud.orchestrator.spot_retry import (
    FailClosed,
    Ignore,
    Requeue,
    Unroutable,
    _parse_row_message,
    _parse_target,
    build_retry_job,
    decide_stopped_task,
    retry_sent_at,
)
from trikon_cloud.orchestrator.tests.conftest import (
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    make_sqs_job_message,
)
from trikon_cloud.usage_reporter.models import EcsTaskStateChangeEvent

_REGION: Final = "us-east-1"
_ACCOUNT: Final = "123456789012"
_CLUSTER_ARN: Final = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/trikon-verify-cluster"
_TASK_ARN: Final = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task/trikon-verify-cluster/0123abcd"
_GROUP: Final = "family:trikon-verify-runner"
_RESERVATION_ID: Final = "3f1c2a9e-7b4d-4e8a-9c21-5d6e7f8a9b0c"
_STOPPED_AT: Final = "2024-05-01T10:02:30.123456Z"
_STOPPED_AT_MS: Final = "2024-05-01T10:02:30.123Z"
_EVENT_TIME: Final = "2024-05-01T10:02:31Z"

_UNSET: Final = object()


def _runner_env(**changes: str | None) -> dict[str, str]:
    """The ten Runner_Overrides of an Attempt 1 Spot task; ``None`` drops a key."""
    env: dict[str, str] = {
        "TRIKON_INSTALLATION_ID": str(CANONICAL_INSTALLATION_ID),
        "TRIKON_REPO_FULL_NAME": CANONICAL_REPO_FULL_NAME,
        "TRIKON_PR_NUMBER": str(CANONICAL_PR_NUMBER),
        "TRIKON_HEAD_SHA": CANONICAL_HEAD_SHA,
        "TRIKON_BASE_SHA": CANONICAL_BASE_SHA,
        "TRIKON_EVENT_TYPE": CANONICAL_EVENT_TYPE,
        "TRIKON_DELIVERY_ID": CANONICAL_DELIVERY_ID,
        "TRIKON_RESERVATION_ID": _RESERVATION_ID,
        "TRIKON_ATTEMPT": "1",
        "TRIKON_CAPACITY": "FARGATE_SPOT",
    }
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _event(
    *,
    env: dict[str, str] | None = None,
    stop_code: str | None = "SpotInterruption",
    exit_code: int | None = 0,
    cluster_arn: str = _CLUSTER_ARN,
    group: str | None = _GROUP,
    last_status: str = "STOPPED",
    stopped_at: str | None = _STOPPED_AT,
    environment: list[dict[str, str]] | object = _UNSET,
) -> EcsTaskStateChangeEvent:
    """Parse a Task_Stopped_Event the way the handler will (``None`` omits a key)."""
    entries = (
        environment
        if environment is not _UNSET
        else [
            {"name": name, "value": value}
            for name, value in (env if env is not None else _runner_env()).items()
        ]
    )
    container: dict[str, object] = {"name": "runner", "lastStatus": last_status}
    if exit_code is not None:
        container["exitCode"] = exit_code
    detail: dict[str, object] = {
        "taskArn": _TASK_ARN,
        "clusterArn": cluster_arn,
        "lastStatus": last_status,
        "containers": [container],
        "overrides": {"containerOverrides": [{"name": "runner", "environment": entries}]},
        "capacityProviderName": "FARGATE_SPOT",
    }
    optional: dict[str, object] = {
        "group": group,
        "stopCode": stop_code,
        "stoppedAt": stopped_at,
    }
    detail.update({key: value for key, value in optional.items() if value is not None})
    return EcsTaskStateChangeEvent.model_validate(
        {
            "version": "0",
            "detail-type": "ECS Task State Change",
            "source": "aws.ecs",
            "time": _EVENT_TIME,
            "detail": detail,
        }
    )


_TARGET: Final = FailClosedTarget(
    installation_id=CANONICAL_INSTALLATION_ID,
    repo_full_name=CANONICAL_REPO_FULL_NAME,
    head_sha=CANONICAL_HEAD_SHA,
    pr_number=CANONICAL_PR_NUMBER,
)


# ---------------------------------------------------------------------------
# Requeue (Req 9.2).
# ---------------------------------------------------------------------------


def test_spot_interruption_on_attempt_one_requeues_retry_job() -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.2."""
    decision = decide_stopped_task(_event())

    assert isinstance(decision, Requeue)
    row = make_sqs_job_message(sent_at=_STOPPED_AT_MS)
    assert decision.row_message == row
    assert decision.target == _TARGET
    assert decision.job == VerifyJobMessage(
        **row.model_dump(), attempt=2, reservation_id=_RESERVATION_ID
    )
    assert decision.job.delivery_id == CANONICAL_DELIVERY_ID


def test_retry_job_round_trips_overrides_from_build_run_task_call() -> None:
    """The dispatcher's overrides rebuild the original job, as Attempt 2 (Req 9.2)."""
    message = VerifyJobMessage.model_validate(make_sqs_job_message().model_dump())
    call = build_run_task_call(
        message,
        env=OrchestratorEnvConfig(),
        task_definition="trikon-verify-runner:7",
        reservation_id=_RESERVATION_ID,
        attempt=1,
        capacity="FARGATE_SPOT",
    )
    environment = [
        entry.model_dump() for entry in call.overrides.containerOverrides[0].environment
    ]
    event = _event(environment=environment)

    decision = decide_stopped_task(event)

    assert isinstance(decision, Requeue)
    expected = message.model_copy(
        update={"attempt": 2, "reservation_id": _RESERVATION_ID, "sent_at": retry_sent_at(event)}
    )
    assert decision.job == expected
    assert VerifyJobMessage.model_validate_json(decision.job.model_dump_json()) == decision.job


def test_decision_is_deterministic() -> None:
    event = _event()
    assert decide_stopped_task(event) == decide_stopped_task(event)


# ---------------------------------------------------------------------------
# Ignore (Req 9.6).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            _event(cluster_arn=f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/other"),
            id="other-cluster",
        ),
        pytest.param(_event(group="family:other"), id="other-group"),
        pytest.param(_event(group=None), id="no-group"),
        pytest.param(_event(last_status="RUNNING"), id="not-stopped"),
    ],
)
def test_non_sandbox_task_is_ignored(event: EcsTaskStateChangeEvent) -> None:
    assert decide_stopped_task(event) == Ignore("not_sandbox_task")


@pytest.mark.parametrize("stop_code", ["EssentialContainerExited", "UserInitiated", None])
@pytest.mark.parametrize("exit_code", [0, 1, 137, None])
def test_normal_stop_without_interim_exit_is_ignored(
    stop_code: str | None, exit_code: int | None
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.6."""
    event = _event(stop_code=stop_code, exit_code=exit_code)
    assert decide_stopped_task(event) == Ignore("not_interrupted")


def test_normal_stop_is_ignored_even_with_unparseable_overrides() -> None:
    assert decide_stopped_task(
        _event(stop_code="EssentialContainerExited", environment=[])
    ) == Ignore("not_interrupted")


# ---------------------------------------------------------------------------
# FailClosed (Req 11.2, 11.3, 11.4).
# ---------------------------------------------------------------------------


def test_interim_exit_without_spot_interruption_fails_closed() -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.3."""
    decision = decide_stopped_task(_event(stop_code="EssentialContainerExited", exit_code=75))
    assert decision == FailClosed(
        "interim_without_retry", _TARGET, make_sqs_job_message(sent_at=_STOPPED_AT_MS)
    )


def test_spot_interruption_on_attempt_two_fails_closed() -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.2."""
    event = _event(env=_runner_env(TRIKON_ATTEMPT="2", TRIKON_CAPACITY="FARGATE"))
    decision = decide_stopped_task(event)
    assert decision == FailClosed(
        "retry_exhausted", _TARGET, make_sqs_job_message(sent_at=_STOPPED_AT_MS)
    )


@pytest.mark.parametrize("attempt", [None, "3", "0", " 1", "01", "one"])
def test_spot_interruption_with_invalid_attempt_fails_closed(attempt: str | None) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.4."""
    decision = decide_stopped_task(_event(env=_runner_env(TRIKON_ATTEMPT=attempt)))
    assert isinstance(decision, FailClosed)
    assert decision.reason == "retry_job_malformed"
    assert decision.target == _TARGET


@pytest.mark.parametrize("reservation_id", [None, "", "not-a-uuid"])
def test_spot_interruption_without_valid_reservation_fails_closed(
    reservation_id: str | None,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.4."""
    decision = decide_stopped_task(_event(env=_runner_env(TRIKON_RESERVATION_ID=reservation_id)))
    assert decision == FailClosed(
        "retry_job_malformed", _TARGET, make_sqs_job_message(sent_at=_STOPPED_AT_MS)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"TRIKON_BASE_SHA": None},
        {"TRIKON_BASE_SHA": "XYZ"},
        {"TRIKON_EVENT_TYPE": None},
        {"TRIKON_DELIVERY_ID": None},
    ],
)
def test_spot_interruption_without_row_fields_fails_closed_without_row(
    changes: dict[str, str | None],
) -> None:
    """Identity known, Job_Message not rebuildable: fail closed with no row (Req 11.4)."""
    decision = decide_stopped_task(_event(env=_runner_env(**changes)))
    assert decision == FailClosed("retry_job_malformed", _TARGET, None)


def test_invalid_pr_number_keeps_target_without_pr_and_fails_closed() -> None:
    decision = decide_stopped_task(_event(env=_runner_env(TRIKON_PR_NUMBER="abc")))
    assert decision == FailClosed(
        "retry_job_malformed",
        FailClosedTarget(
            installation_id=CANONICAL_INSTALLATION_ID,
            repo_full_name=CANONICAL_REPO_FULL_NAME,
            head_sha=CANONICAL_HEAD_SHA,
            pr_number=None,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Unroutable (Req 11.4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"TRIKON_INSTALLATION_ID": None},
        {"TRIKON_INSTALLATION_ID": "0"},
        {"TRIKON_INSTALLATION_ID": "+12"},
        {"TRIKON_INSTALLATION_ID": " 12"},
        {"TRIKON_INSTALLATION_ID": "1_000"},
        {"TRIKON_REPO_FULL_NAME": None},
        {"TRIKON_REPO_FULL_NAME": ""},
        {"TRIKON_HEAD_SHA": None},
        {"TRIKON_HEAD_SHA": CANONICAL_HEAD_SHA.upper()},
        {"TRIKON_HEAD_SHA": CANONICAL_HEAD_SHA[:39]},
    ],
)
@pytest.mark.parametrize(
    ("stop_code", "exit_code"),
    [("SpotInterruption", 0), ("EssentialContainerExited", 75)],
)
def test_unknown_identity_is_unroutable(
    changes: dict[str, str | None], stop_code: str, exit_code: int
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.4 (identity unknown)."""
    event = _event(env=_runner_env(**changes), stop_code=stop_code, exit_code=exit_code)
    assert decide_stopped_task(event) == Unroutable()


def test_missing_runner_override_is_unroutable() -> None:
    assert decide_stopped_task(_event(environment=[])) == Unroutable()


# ---------------------------------------------------------------------------
# Helpers: retry_sent_at, build_retry_job, parsers.
# ---------------------------------------------------------------------------


def test_retry_sent_at_uses_stopped_at_then_event_time() -> None:
    assert retry_sent_at(_event()) == _STOPPED_AT_MS
    assert retry_sent_at(_event(stopped_at=None)) == "2024-05-01T10:02:31.000Z"
    assert retry_sent_at(_event(stopped_at="2024-05-01T12:02:30.5+02:00")) == (
        "2024-05-01T10:02:30.500Z"
    )


def test_build_retry_job_sets_attempt_two_and_reservation() -> None:
    row = make_sqs_job_message()
    job = build_retry_job(row, reservation_id=_RESERVATION_ID)
    assert job is not None
    assert job.attempt == 2
    assert job.reservation_id == _RESERVATION_ID
    assert job.model_dump(exclude={"attempt", "reservation_id"}) == row.model_dump()


@pytest.mark.parametrize("reservation_id", [None, "", "3F1C2A9E-7B4D-4E8A-9C21", "x" * 36])
def test_build_retry_job_rejects_missing_or_invalid_reservation(
    reservation_id: str | None,
) -> None:
    assert build_retry_job(make_sqs_job_message(), reservation_id=reservation_id) is None


def test_parsers_read_the_runner_override_only() -> None:
    event = _event()
    assert _parse_target(event.detail) == _TARGET
    assert _parse_row_message(event.detail, sent_at="s") == make_sqs_job_message(sent_at="s")


def test_decisions_log_nothing_and_never_echo_override_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pydantic error text from bad overrides never reaches a log (Req 9.8)."""
    secret = "s3cr3t-value-in-overrides"
    caplog.set_level(logging.DEBUG)
    for changes in (
        {"TRIKON_BASE_SHA": secret},
        {"TRIKON_RESERVATION_ID": secret},
        {"TRIKON_ATTEMPT": secret},
    ):
        decision = decide_stopped_task(_event(env=_runner_env(**changes)))
        assert isinstance(decision, FailClosed)
        assert secret not in repr(decision)
    assert caplog.records == []
