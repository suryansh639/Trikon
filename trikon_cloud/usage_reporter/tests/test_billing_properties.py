# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin generates,
# not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Property test for billing of interrupted runs (trikon-cloud-cost-cuts, Property 13).

*For any* generated Task_Stopped_Event of a stopped Sandbox_Task, the
Usage_Reporter with a recording client:

* sends no Usage_Report and logs INFO ``usage_report_skipped_spot_interruption``
  (task ARN and Run_Id only) when ``stopCode == "SpotInterruption"``
  (Req 12.1);
* otherwise sends exactly the report the pre-change handler sends (Req 12.3).
  The oracle is a model of the pre-change handler built here from the
  generated values: the ``runner`` override lookup, then
  :func:`meter_stopped_task` on the generated datetimes.

*For any* pair of an interrupted Attempt 1 task and its retry task that share
a Run_Id and a Reservation_Id, handling both events (in either delivery
order) sends exactly one Usage_Report, carrying the shared ids and the retry
task's metered minutes and instants (Req 12.2).

The client is a recording stub injected through :data:`handler._ENV` and
:data:`handler._USAGE_CLIENT`; the caches are restored after every example.
No network, no AWS.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO, Final

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.usage_client.metering import MeteringInputError, meter_stopped_task
from trikon_cloud.usage_client.models import UsageDelivered, UsageRejected, UsageReport
from trikon_cloud.usage_reporter import handler
from trikon_cloud.usage_reporter.handler import UsageEventError, lambda_handler
from trikon_cloud.usage_reporter.models import UsageReporterEnvConfig

# ---------------------------------------------------------------------------
# Fixed values.
# ---------------------------------------------------------------------------

_LOGGER_NAME: Final = "trikon-cloud-usage-reporter"
_REGION: Final = "us-east-1"
_ACCOUNT: Final = "123456789012"
_CLUSTER_ARN: Final = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/trikon-verify-cluster"
_TASK_DEFINITION_ARN: Final = (
    f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task-definition/trikon-verify-runner:7"
)
_GROUP: Final = "family:trikon-verify-runner"
_SPOT: Final = "SpotInterruption"

#: Keys Powertools adds to every record; the rest are the handler's own.
_POWERTOOLS_KEYS: Final = frozenset(
    {"level", "location", "message", "timestamp", "service", "sampling_rate", "xray_trace_id"}
)

# ``model_validate`` validates the dict by alias and skips the settings
# sources, so the process environment is never read.
_ENV_CONFIG: Final = UsageReporterEnvConfig.model_validate(
    {
        "TRIKON_WEB_API_URL": "https://api.trikon.example.test",
        "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME": "/trikon/test/usage-secret",
    }
)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Generated task data and its EventBridge event.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Task:
    """One stopped Sandbox_Task as generated; instants are whole milliseconds."""

    task_arn: str
    run_id: str | None
    reservation_id: str | None
    stop_code: str | None
    capacity_provider_name: str | None
    runner_exit_code: int | None
    attempt: str
    created_at: datetime | None
    pull_started_at: datetime | None
    stopped_at: datetime | None
    event_time: datetime
    sidecar_first: bool


def _iso(value: datetime) -> str:
    """ECS form: UTC with milliseconds and a ``Z`` suffix."""
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(task: _Task) -> dict[str, object]:
    """Build the EventBridge "ECS Task State Change" event for ``task``.

    A ``None`` field omits its key. A sidecar override carrying decoy ids sits
    next to the ``runner`` override, before or after it.
    """
    runner_env = [
        {"name": "TRIKON_INSTALLATION_ID", "value": "41234567"},
        {"name": "TRIKON_REPOSITORY", "value": "octo-org/octo-repo"},
        {"name": "TRIKON_ATTEMPT", "value": task.attempt},
    ]
    if task.run_id is not None:
        runner_env.append({"name": "TRIKON_DELIVERY_ID", "value": task.run_id})
    if task.reservation_id is not None:
        runner_env.append({"name": "TRIKON_RESERVATION_ID", "value": task.reservation_id})
    runner = {"name": "runner", "environment": runner_env}
    sidecar = {
        "name": "log-router",
        "environment": [
            {"name": "TRIKON_DELIVERY_ID", "value": "decoy-run"},
            {"name": "TRIKON_RESERVATION_ID", "value": "00000000-0000-0000-0000-000000000000"},
        ],
    }
    overrides = [sidecar, runner] if task.sidecar_first else [runner, sidecar]

    container: dict[str, object] = {"name": "runner", "lastStatus": "STOPPED"}
    if task.runner_exit_code is not None:
        container["exitCode"] = task.runner_exit_code

    detail: dict[str, object] = {
        "taskArn": task.task_arn,
        "clusterArn": _CLUSTER_ARN,
        "taskDefinitionArn": _TASK_DEFINITION_ARN,
        "group": _GROUP,
        "lastStatus": "STOPPED",
        "desiredStatus": "STOPPED",
        "launchType": "FARGATE",
        "containers": [container],
        "overrides": {"containerOverrides": overrides},
    }
    optional: dict[str, object | None] = {
        "stopCode": task.stop_code,
        "capacityProviderName": task.capacity_provider_name,
        "createdAt": None if task.created_at is None else _iso(task.created_at),
        "pullStartedAt": None if task.pull_started_at is None else _iso(task.pull_started_at),
        "stoppedAt": None if task.stopped_at is None else _iso(task.stopped_at),
    }
    detail.update({key: value for key, value in optional.items() if value is not None})
    return {
        "version": "0",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": _ACCOUNT,
        "time": _iso(task.event_time),
        "region": _REGION,
        "resources": [task.task_arn],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdef"
_TASK_ARNS = st.text(alphabet=_HEX, min_size=32, max_size=32).map(
    lambda task_id: f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task/trikon-verify-cluster/{task_id}"
)
_RUN_IDS = st.one_of(
    st.uuids().map(str),
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=1,
        max_size=128,
    ),
)
_RESERVATION_IDS = st.builds(
    lambda u, upper: str(u).upper() if upper else str(u), st.uuids(), st.booleans()
)
_NON_SPOT_STOP_CODES = st.one_of(
    st.none(),
    st.sampled_from(
        [
            "EssentialContainerExited",
            "TaskFailedToStart",
            "UserInitiated",
            "ServiceSchedulerInitiated",
            "TerminationNotice",
        ]
    ),
)
_CAPACITY = st.one_of(st.none(), st.sampled_from(["FARGATE", "FARGATE_SPOT"]))
_EXIT_CODES = st.one_of(st.none(), st.sampled_from([0, 1, 2, 75, 137, 143]))

# 2020-01-01 to 2030-01-01 in epoch milliseconds.
_BASE_MS = st.integers(min_value=1_577_836_800_000, max_value=1_893_456_000_000)


def _at(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


@st.composite
def _tasks(
    draw: st.DrawFn,
    *,
    stop_codes: st.SearchStrategy[str | None],
    run_id: str | None = None,
    reservation_id: str | None = None,
    ids_may_be_missing: bool = False,
    capacity: st.SearchStrategy[str | None] = _CAPACITY,
    attempt: str = "1",
    require_start: bool = False,
) -> _Task:
    """A stopped Sandbox_Task. Durations cover sub-minute, capped and negative spans."""
    base = draw(_BASE_MS)
    pull_offset = draw(st.integers(min_value=0, max_value=120_000))
    duration = draw(st.integers(min_value=-5_000, max_value=1_200_000))
    event_lag = draw(st.integers(min_value=-1_000, max_value=5_000))
    has_created = draw(st.booleans())
    has_pull = draw(st.booleans())
    if require_start and not (has_created or has_pull):
        has_pull = True
    start_ms = base + pull_offset if has_pull else base
    stop_ms = start_ms + duration

    if run_id is None:
        run_id = draw(_RUN_IDS)
        if ids_may_be_missing and draw(st.booleans()):
            run_id = None
    if reservation_id is None:
        reservation_id = draw(_RESERVATION_IDS)
        if ids_may_be_missing and draw(st.booleans()):
            reservation_id = None

    return _Task(
        task_arn=draw(_TASK_ARNS),
        run_id=run_id,
        reservation_id=reservation_id,
        stop_code=draw(stop_codes),
        capacity_provider_name=draw(capacity),
        runner_exit_code=draw(_EXIT_CODES),
        attempt=attempt,
        created_at=_at(base) if has_created else None,
        pull_started_at=_at(base + pull_offset) if has_pull else None,
        stopped_at=_at(stop_ms) if draw(st.booleans()) else None,
        event_time=_at(stop_ms + event_lag),
        sidecar_first=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# Oracle: the pre-change handler, modelled from the generated values.
# ---------------------------------------------------------------------------


def _pre_change_report(task: _Task) -> UsageReport | type[Exception] | None:
    """What the pre-change handler does with ``task``.

    Returns the one report it sends, ``None`` when it sends nothing (no
    Reservation_Id), or the exception type it raises.
    """
    if task.reservation_id is None:
        return None
    if task.run_id is None:
        return UsageEventError
    try:
        usage = meter_stopped_task(
            created_at=task.created_at,
            pull_started_at=task.pull_started_at,
            stopped_at=task.stopped_at,
            event_time=task.event_time,
            stop_code=task.stop_code,
        )
    except MeteringInputError:
        return MeteringInputError
    return UsageReport(
        run_id=task.run_id,
        reservation_id=task.reservation_id,
        billed_minutes=usage.billed_minutes,
        started_at=usage.started_at,
        stopped_at=usage.stopped_at,
    )


# ---------------------------------------------------------------------------
# Harness: recording client, cache injection and log capture.
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Records every Usage_Report and answers ``recorded``."""

    def __init__(self) -> None:
        self.reports: list[UsageReport] = []

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        self.reports.append(report)
        return UsageDelivered(outcome="recorded")


@contextmanager
def _harness() -> Iterator[tuple[_RecordingClient, io.StringIO]]:
    """Inject a fresh recording client and capture the logger's JSON lines.

    Restores the module caches and the logger's streams and level on exit,
    so every Hypothesis example starts clean.
    """
    client = _RecordingClient()
    saved_env, saved_client = handler._ENV, handler._USAGE_CLIENT
    handler._ENV, handler._USAGE_CLIENT = _ENV_CONFIG, client

    buf = io.StringIO()
    stdlib_logger = logging.getLogger(_LOGGER_NAME)
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for log_handler in stdlib_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            original_streams.append((log_handler, log_handler.stream))
            log_handler.setStream(buf)
    stdlib_logger.setLevel(logging.INFO)
    try:
        yield client, buf
    finally:
        for log_handler, stream in original_streams:
            log_handler.setStream(stream)
        stdlib_logger.setLevel(original_level)
        handler._ENV, handler._USAGE_CLIENT = saved_env, saved_client


def _records(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _handle(task: _Task) -> None:
    lambda_handler(_event(task), LambdaContext())


# ---------------------------------------------------------------------------
# Property 13.
# ---------------------------------------------------------------------------


# Feature: trikon-cloud-cost-cuts, Property 13: Only the retry is billed
@settings(max_examples=100, deadline=None)
@given(task=_tasks(stop_codes=st.just(_SPOT), ids_may_be_missing=True))
def test_spot_interruption_sends_no_report(task: _Task) -> None:
    """**Validates: Requirements 12.1**"""
    with _harness() as (client, buf):
        _handle(task)

    assert client.reports == []
    (record,) = _records(buf)
    assert record["level"] == "INFO"
    assert record["message"] == "usage_report_skipped_spot_interruption"
    assert record["task_arn"] == task.task_arn
    # Powertools drops ``None`` keys, so a missing Run_Id leaves only the ARN.
    expected_keys = {"task_arn"} if task.run_id is None else {"task_arn", "run_id"}
    assert set(record) - _POWERTOOLS_KEYS == expected_keys
    if task.run_id is not None:
        assert record["run_id"] == task.run_id
    if task.reservation_id is not None:
        assert task.reservation_id not in buf.getvalue()


# Feature: trikon-cloud-cost-cuts, Property 13: Only the retry is billed
@settings(max_examples=100, deadline=None)
@given(task=_tasks(stop_codes=_NON_SPOT_STOP_CODES, ids_may_be_missing=True))
def test_non_spot_stop_sends_exactly_the_pre_change_report(task: _Task) -> None:
    """**Validates: Requirements 12.3**"""
    expected = _pre_change_report(task)

    with _harness() as (client, _buf):
        if isinstance(expected, type):
            with pytest.raises(expected):
                _handle(task)
        else:
            _handle(task)

    if isinstance(expected, UsageReport):
        assert client.reports == [expected]
    else:
        assert client.reports == []


# Feature: trikon-cloud-cost-cuts, Property 13: Only the retry is billed
@settings(max_examples=100, deadline=None)
@given(
    data=st.data(),
    run_id=_RUN_IDS,
    reservation_id=_RESERVATION_IDS,
    retry_first=st.booleans(),
)
def test_interrupted_attempt_and_retry_send_one_report_for_the_retry(
    data: st.DataObject, run_id: str, reservation_id: str, retry_first: bool
) -> None:
    """**Validates: Requirements 12.1, 12.2**"""
    interrupted = data.draw(
        _tasks(
            stop_codes=st.just(_SPOT),
            run_id=run_id,
            reservation_id=reservation_id,
            capacity=st.just("FARGATE_SPOT"),
            attempt="1",
        ),
        label="interrupted",
    )
    retry = data.draw(
        _tasks(
            stop_codes=_NON_SPOT_STOP_CODES,
            run_id=run_id,
            reservation_id=reservation_id,
            capacity=st.just("FARGATE"),
            attempt="2",
            require_start=True,
        ).filter(lambda task: task.task_arn != interrupted.task_arn),
        label="retry",
    )
    expected = _pre_change_report(retry)
    assert isinstance(expected, UsageReport)

    with _harness() as (client, _buf):
        for task in (retry, interrupted) if retry_first else (interrupted, retry):
            _handle(task)

    assert client.reports == [expected]
    (report,) = client.reports
    assert report.run_id == run_id
    assert report.reservation_id == reservation_id
