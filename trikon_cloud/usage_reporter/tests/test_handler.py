"""Example tests for :mod:`trikon_cloud.usage_reporter.handler` (sandbox-minute-plans task 25.3).

Each test feeds :func:`lambda_handler` one realistic EventBridge "ECS Task
State Change" event (with the extra keys AWS sends, such as ``containers``
and ``attachments``) and checks what the handler sends and logs:

* a stopped Sandbox_Task sends one Usage_Report with the metered minutes and
  the exact instants used (Req 12.2, 12.5, 12.11);
* ``TaskFailedToStart`` sends 0 minutes with both timestamps (Req 12.3);
* no ``stoppedAt`` uses the event time (Req 12.4);
* no Reservation_Id logs WARNING ``usage_report_skipped`` and sends nothing
  (Req 12.6);
* a rejection logs ERROR with the status and returns normally (Req 12.10);
* an undelivered report raises (Req 12.8), as do a missing start time and a
  Reservation_Id without a valid Run_Id;
* events for other tasks, clusters, groups, or statuses are ignored;
* a Spot_Interruption sends nothing and logs INFO
  ``usage_report_skipped_spot_interruption``, and its retry sends exactly one
  report with the retry's minutes (trikon-cloud-cost-cuts Req 12.1-12.4).

The Usage_API client is a recording stub injected through the module caches
:data:`handler._ENV` and :data:`handler._USAGE_CLIENT`. The one cold-start
test builds the real client without calling it. No network, no AWS.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import IO, Final

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from trikon_cloud.usage_client.client import QUOTA_PATH, USAGE_PATH, UsageApiClient
from trikon_cloud.usage_client.metering import MeteringInputError
from trikon_cloud.usage_client.models import (
    UsageDelivered,
    UsageOutcome,
    UsageRejected,
    UsageReport,
    UsageReportUndeliveredError,
)
from trikon_cloud.usage_client.secret import UsageSecretReader
from trikon_cloud.usage_reporter import handler
from trikon_cloud.usage_reporter.handler import UsageEventError, lambda_handler
from trikon_cloud.usage_reporter.models import (
    EcsContainer,
    EcsTaskDetail,
    EcsTaskStateChangeEvent,
    UsageReporterEnvConfig,
)

# ---------------------------------------------------------------------------
# Fixed values.
# ---------------------------------------------------------------------------

_LOGGER_NAME: Final = "trikon-cloud-usage-reporter"

_REGION: Final = "us-east-1"
_ACCOUNT: Final = "123456789012"
_CLUSTER_ARN: Final = f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/trikon-verify-cluster"
_TASK_ARN: Final = (
    f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task/trikon-verify-cluster/0f1e2d3c4b5a69788796a5b4c3d2e1f0"
)
_TASK_DEFINITION_ARN: Final = (
    f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task-definition/trikon-verify-runner:7"
)
_GROUP: Final = "family:trikon-verify-runner"

_RUN_ID: Final = "8a1c3f20-5b7e-11ef-9d2a-0242ac120002"
_RESERVATION_ID: Final = "3f1c2a9e-7b4d-4e8a-9c21-5d6e7f8a9b0c"

_API_URL: Final = "https://api.trikon.example.test"
_SECRET_PARAMETER_NAME: Final = "/trikon/test/usage-secret"

_UNSET: Final = object()


# ---------------------------------------------------------------------------
# Event builder.
# ---------------------------------------------------------------------------


def _runner_override(env: dict[str, str]) -> dict[str, object]:
    return {
        "name": "runner",
        "environment": [{"name": name, "value": value} for name, value in env.items()],
    }


def _default_runner_env() -> dict[str, str]:
    return {
        "TRIKON_INSTALLATION_ID": "41234567",
        "TRIKON_DELIVERY_ID": _RUN_ID,
        "TRIKON_RESERVATION_ID": _RESERVATION_ID,
        "TRIKON_REPOSITORY": "octo-org/octo-repo",
    }


def _event(
    *,
    last_status: str = "STOPPED",
    cluster_arn: str = _CLUSTER_ARN,
    group: str | None = _GROUP,
    stop_code: str | None = "EssentialContainerExited",
    created_at: str | None = "2024-05-01T09:59:58.412Z",
    pull_started_at: str | None = "2024-05-01T10:00:00Z",
    stopped_at: str | None = "2024-05-01T10:02:30Z",
    event_time: str = "2024-05-01T10:02:31Z",
    container_overrides: list[dict[str, object]] | object | None = _UNSET,
    task_arn: str = _TASK_ARN,
    capacity_provider_name: str | None = None,
    runner_exit_code: int | None = 0,
) -> dict[str, object]:
    """Build an EventBridge "ECS Task State Change" event as AWS sends it.

    A ``None`` timestamp, ``group``, ``stop_code``, ``capacity_provider_name``
    or ``runner_exit_code`` omits the key. A ``None`` ``container_overrides``
    omits ``overrides`` entirely; the default is the Orchestrator's single
    ``runner`` override.
    """
    overrides: object
    if container_overrides is _UNSET:
        overrides = {"containerOverrides": [_runner_override(_default_runner_env())]}
    elif container_overrides is None:
        overrides = None
    else:
        overrides = {"containerOverrides": container_overrides}

    container: dict[str, object] = {
        "containerArn": f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:container/abc",
        "lastStatus": last_status,
        "name": "runner",
        "image": f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/trikon-runner:0.5.0",
        "taskArn": task_arn,
        "networkInterfaces": [{"privateIpv4Address": "10.0.1.23"}],
        "cpu": "1024",
        "memory": "2048",
    }
    if runner_exit_code is not None:
        container["exitCode"] = runner_exit_code

    detail: dict[str, object] = {
        "attachments": [
            {
                "id": "a1b2c3d4-0000-4000-8000-000000000001",
                "type": "eni",
                "status": "DELETED",
                "details": [
                    {"name": "subnetId", "value": "subnet-0a1b2c3d"},
                    {"name": "privateIPv4Address", "value": "10.0.1.23"},
                ],
            }
        ],
        "attributes": [{"name": "ecs.cpu-architecture", "value": "x86_64"}],
        "availabilityZone": f"{_REGION}a",
        "clusterArn": cluster_arn,
        "connectivity": "CONNECTED",
        "containers": [container],
        "cpu": "1024",
        "desiredStatus": "STOPPED",
        "ephemeralStorage": {"sizeInGiB": 20},
        "launchType": "FARGATE",
        "lastStatus": last_status,
        "memory": "2048",
        "platformVersion": "1.4.0",
        "startedBy": "trikon-cloud-orchestrator",
        "stoppedReason": "Essential container in task exited",
        "taskArn": task_arn,
        "taskDefinitionArn": _TASK_DEFINITION_ARN,
        "updatedAt": event_time,
        "version": 6,
    }
    optional: dict[str, object] = {
        "group": group,
        "stopCode": stop_code,
        "createdAt": created_at,
        "pullStartedAt": pull_started_at,
        "pullStoppedAt": pull_started_at,
        "startedAt": pull_started_at,
        "stoppingAt": stopped_at,
        "executionStoppedAt": stopped_at,
        "stoppedAt": stopped_at,
        "overrides": overrides,
        "capacityProviderName": capacity_provider_name,
    }
    detail.update({key: value for key, value in optional.items() if value is not None})

    return {
        "version": "0",
        "id": "6f8e2b1a-0c9d-4e7f-8a6b-5c4d3e2f1a0b",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": _ACCOUNT,
        "time": event_time,
        "region": _REGION,
        "resources": [task_arn],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Stub Usage_API client and fixtures.
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Records every Usage_Report and answers with ``result`` (or raises it)."""

    def __init__(self, result: UsageDelivered | UsageRejected | Exception) -> None:
        self.result = result
        self.reports: list[UsageReport] = []

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        self.reports.append(report)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _context() -> LambdaContext:
    # The handler discards the context, so an empty one is enough.
    return LambdaContext()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the Lambda environment and start every test with empty caches."""
    monkeypatch.setenv("TRIKON_WEB_API_URL", _API_URL)
    monkeypatch.setenv("TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME", _SECRET_PARAMETER_NAME)
    monkeypatch.setenv("TRIKON_LOG_LEVEL", "INFO")
    # monkeypatch restores the original (empty) caches on teardown.
    monkeypatch.setattr(handler, "_ENV", None)
    monkeypatch.setattr(handler, "_USAGE_CLIENT", None)


def _install(
    monkeypatch: pytest.MonkeyPatch, result: UsageDelivered | UsageRejected | Exception
) -> _RecordingClient:
    client = _RecordingClient(result)
    monkeypatch.setattr(handler, "_ENV", UsageReporterEnvConfig())
    monkeypatch.setattr(handler, "_USAGE_CLIENT", client)
    return client


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> _RecordingClient:
    return _install(monkeypatch, UsageDelivered(outcome="recorded"))


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's stream to a :class:`io.StringIO`.

    Same pattern as the orchestrator tests: the Powertools logger attaches a
    :class:`logging.StreamHandler` bound to ``sys.stdout`` at import time, so
    its stream is swapped for the test and restored on teardown.
    """
    buf = io.StringIO()
    stdlib_logger = logging.getLogger(_LOGGER_NAME)
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for log_handler in stdlib_logger.handlers:
        if isinstance(log_handler, logging.StreamHandler):
            original_streams.append((log_handler, log_handler.stream))
            log_handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for log_handler, stream in original_streams:
            log_handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


def _log_records(log_stream: io.StringIO) -> list[dict[str, object]]:
    """Parse the captured Powertools JSON lines into a list of dicts."""
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _only_record(log_stream: io.StringIO) -> dict[str, object]:
    records = _log_records(log_stream)
    assert len(records) == 1, records
    return records[0]


# ---------------------------------------------------------------------------
# Delivered reports (Req 12.2, 12.5, 12.11).
# ---------------------------------------------------------------------------


def test_stopped_task_sends_one_metered_report(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    lambda_handler(_event(), _context())

    # pullStartedAt 10:00:00 to stoppedAt 10:02:30 is 150 s, billed as 3 minutes.
    assert delivered.reports == [
        UsageReport(
            run_id=_RUN_ID,
            reservation_id=_RESERVATION_ID,
            billed_minutes=3,
            started_at="2024-05-01T10:00:00.000Z",
            stopped_at="2024-05-01T10:02:30.000Z",
        )
    ]
    assert delivered.reports[0].request_version == 1

    record = _only_record(log_stream)
    assert record["level"] == "INFO"
    assert record["message"] == "usage_report_delivered"
    assert record["task_arn"] == _TASK_ARN
    assert record["run_id"] == _RUN_ID
    assert _RESERVATION_ID not in log_stream.getvalue()


@pytest.mark.parametrize("outcome", ["recorded", "duplicate"])
def test_both_delivered_outcomes_log_delivered(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO, outcome: UsageOutcome
) -> None:
    client = _install(monkeypatch, UsageDelivered(outcome=outcome))

    lambda_handler(_event(), _context())

    assert len(client.reports) == 1
    assert _only_record(log_stream)["message"] == "usage_report_delivered"


def test_start_falls_back_to_created_at_without_pull_started_at(
    delivered: _RecordingClient,
) -> None:
    event = _event(
        created_at="2024-05-01T10:00:00.250Z",
        pull_started_at=None,
        stopped_at="2024-05-01T10:01:00.250Z",
    )

    lambda_handler(event, _context())

    (report,) = delivered.reports
    assert report.started_at == "2024-05-01T10:00:00.250Z"
    assert report.stopped_at == "2024-05-01T10:01:00.250Z"
    assert report.billed_minutes == 1


def test_long_run_bills_the_per_run_cap(delivered: _RecordingClient) -> None:
    lambda_handler(_event(stopped_at="2024-05-01T10:25:00Z"), _context())

    (report,) = delivered.reports
    assert report.billed_minutes == 10
    assert report.stopped_at == "2024-05-01T10:25:00.000Z"


def test_task_failed_to_start_sends_zero_minutes_with_timestamps(
    delivered: _RecordingClient,
) -> None:
    event = _event(
        stop_code="TaskFailedToStart",
        created_at="2024-05-01T10:00:00Z",
        pull_started_at=None,
        stopped_at="2024-05-01T10:00:41.007Z",
    )

    lambda_handler(event, _context())

    assert delivered.reports == [
        UsageReport(
            run_id=_RUN_ID,
            reservation_id=_RESERVATION_ID,
            billed_minutes=0,
            started_at="2024-05-01T10:00:00.000Z",
            stopped_at="2024-05-01T10:00:41.007Z",
        )
    ]


def test_missing_stopped_at_uses_the_event_time(delivered: _RecordingClient) -> None:
    event = _event(stopped_at=None, event_time="2024-05-01T10:03:01Z")

    lambda_handler(event, _context())

    (report,) = delivered.reports
    assert report.started_at == "2024-05-01T10:00:00.000Z"
    assert report.stopped_at == "2024-05-01T10:03:01.000Z"
    assert report.billed_minutes == 4


def test_runner_override_is_found_among_other_containers(
    delivered: _RecordingClient,
) -> None:
    sidecar = {
        "name": "log-router",
        "environment": [{"name": "TRIKON_RESERVATION_ID", "value": "not-the-runner"}],
    }
    event = _event(container_overrides=[sidecar, _runner_override(_default_runner_env())])

    lambda_handler(event, _context())

    (report,) = delivered.reports
    assert report.reservation_id == _RESERVATION_ID
    assert report.run_id == _RUN_ID


# ---------------------------------------------------------------------------
# Skipped: no Reservation_Id (Req 12.6).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "container_overrides",
    [
        pytest.param(None, id="no-overrides"),
        pytest.param([], id="no-container-overrides"),
        pytest.param(
            [_runner_override({"TRIKON_DELIVERY_ID": _RUN_ID})], id="runner-without-reservation"
        ),
        pytest.param(
            [
                {
                    "name": "sidecar",
                    "environment": [
                        {"name": "TRIKON_DELIVERY_ID", "value": _RUN_ID},
                        {"name": "TRIKON_RESERVATION_ID", "value": _RESERVATION_ID},
                    ],
                }
            ],
            id="reservation-only-on-another-container",
        ),
    ],
)
def test_missing_reservation_id_warns_and_sends_nothing(
    delivered: _RecordingClient,
    log_stream: io.StringIO,
    container_overrides: list[dict[str, object]] | None,
) -> None:
    lambda_handler(_event(container_overrides=container_overrides), _context())

    assert delivered.reports == []
    record = _only_record(log_stream)
    assert record["level"] == "WARNING"
    assert record["message"] == "usage_report_skipped"
    assert record["task_arn"] == _TASK_ARN
    assert "run_id" not in record
    assert _RESERVATION_ID not in log_stream.getvalue()


# ---------------------------------------------------------------------------
# Rejected (Req 12.10) and undelivered (Req 12.8).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 409])
def test_rejected_report_logs_an_error_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO, status: int
) -> None:
    client = _install(monkeypatch, UsageRejected(status=status))

    # Returns normally, so Lambda does not retry the event.
    lambda_handler(_event(), _context())

    assert len(client.reports) == 1
    record = _only_record(log_stream)
    assert record["level"] == "ERROR"
    assert record["message"] == "usage_report_rejected"
    assert record["task_arn"] == _TASK_ARN
    assert record["run_id"] == _RUN_ID
    assert record["status"] == status
    output = log_stream.getvalue()
    assert _RESERVATION_ID not in output
    assert _SECRET_PARAMETER_NAME not in output
    assert "billed_minutes" not in output


def test_undelivered_report_propagates(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    error = UsageReportUndeliveredError("usage report not delivered")
    client = _install(monkeypatch, error)

    with pytest.raises(UsageReportUndeliveredError) as excinfo:
        lambda_handler(_event(), _context())

    assert excinfo.value is error
    assert len(client.reports) == 1
    assert _log_records(log_stream) == []


# ---------------------------------------------------------------------------
# Other failures that propagate.
# ---------------------------------------------------------------------------


def test_no_start_time_raises_metering_input_error(delivered: _RecordingClient) -> None:
    with pytest.raises(MeteringInputError):
        lambda_handler(_event(created_at=None, pull_started_at=None), _context())

    assert delivered.reports == []


def test_reservation_id_without_run_id_raises(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    event = _event(
        container_overrides=[_runner_override({"TRIKON_RESERVATION_ID": _RESERVATION_ID})]
    )

    with pytest.raises(UsageEventError):
        lambda_handler(event, _context())

    assert delivered.reports == []
    assert _RESERVATION_ID not in log_stream.getvalue()


@pytest.mark.parametrize(
    ("run_id", "reservation_id"),
    [
        pytest.param("run id with spaces", _RESERVATION_ID, id="malformed-run-id"),
        pytest.param("", _RESERVATION_ID, id="empty-run-id"),
        pytest.param(_RUN_ID, "not-a-uuid-secret-ish-value", id="malformed-reservation-id"),
        pytest.param(_RUN_ID, "", id="empty-reservation-id"),
    ],
)
def test_malformed_ids_raise_without_echoing_them(
    delivered: _RecordingClient, run_id: str, reservation_id: str
) -> None:
    env = {"TRIKON_DELIVERY_ID": run_id, "TRIKON_RESERVATION_ID": reservation_id}
    event = _event(container_overrides=[_runner_override(env)])

    with pytest.raises(UsageEventError) as excinfo:
        lambda_handler(event, _context())

    assert delivered.reports == []
    message = str(excinfo.value)
    for value in (run_id, reservation_id):
        if value:
            assert value not in message
    # Raised ``from None``: the pydantic error, which echoes the input, is dropped.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_event_that_does_not_parse_raises(delivered: _RecordingClient) -> None:
    event = _event()
    del event["detail"]

    with pytest.raises(ValidationError):
        lambda_handler(event, _context())

    assert delivered.reports == []


# ---------------------------------------------------------------------------
# Spot_Interruption and its retry (trikon-cloud-cost-cuts Req 12.1-12.4).
# ---------------------------------------------------------------------------

_RETRY_TASK_ARN: Final = (
    f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:task/trikon-verify-cluster/1a2b3c4d5e6f708192a3b4c5d6e7f809"
)

#: Keys Powertools adds to every record; the rest are the handler's own.
_POWERTOOLS_KEYS: Final = frozenset(
    {"level", "location", "message", "timestamp", "service", "sampling_rate", "xray_trace_id"}
)


def _attempt_env(*, attempt: str, capacity: str) -> dict[str, str]:
    return {**_default_runner_env(), "TRIKON_ATTEMPT": attempt, "TRIKON_CAPACITY": capacity}


def _spot_interruption_event() -> dict[str, object]:
    """Attempt 1 on FARGATE_SPOT, reclaimed after 4m10s; the runner has no exit code."""
    return _event(
        stop_code="SpotInterruption",
        capacity_provider_name="FARGATE_SPOT",
        runner_exit_code=None,
        pull_started_at="2024-05-01T10:00:00Z",
        stopped_at="2024-05-01T10:04:10Z",
        event_time="2024-05-01T10:04:11Z",
        container_overrides=[_runner_override(_attempt_env(attempt="1", capacity="FARGATE_SPOT"))],
    )


def _retry_event() -> dict[str, object]:
    """Attempt 2 on FARGATE with the same Run_Id and Reservation_Id; runs 90 s."""
    return _event(
        task_arn=_RETRY_TASK_ARN,
        stop_code="EssentialContainerExited",
        capacity_provider_name="FARGATE",
        created_at="2024-05-01T10:04:58Z",
        pull_started_at="2024-05-01T10:05:00Z",
        stopped_at="2024-05-01T10:06:30Z",
        event_time="2024-05-01T10:06:31Z",
        container_overrides=[_runner_override(_attempt_env(attempt="2", capacity="FARGATE"))],
    )


def test_spot_interruption_sends_nothing_and_logs_the_fixed_record(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    lambda_handler(_spot_interruption_event(), _context())

    assert delivered.reports == []
    record = _only_record(log_stream)
    assert record["level"] == "INFO"
    assert record["message"] == "usage_report_skipped_spot_interruption"
    assert record["task_arn"] == _TASK_ARN
    assert record["run_id"] == _RUN_ID
    # The task ARN and the Run_Id only (Req 12.1).
    assert set(record) - _POWERTOOLS_KEYS == {"task_arn", "run_id"}
    assert _RESERVATION_ID not in log_stream.getvalue()


def test_spot_interruption_then_retry_sends_exactly_one_report_for_the_retry(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    lambda_handler(_spot_interruption_event(), _context())
    lambda_handler(_retry_event(), _context())

    # Only the retry's minutes: 10:05:00 to 10:06:30 is 90 s, billed as 2 minutes
    # (the interrupted attempt's 4m10s would have been 5).
    assert delivered.reports == [
        UsageReport(
            run_id=_RUN_ID,
            reservation_id=_RESERVATION_ID,
            billed_minutes=2,
            started_at="2024-05-01T10:05:00.000Z",
            stopped_at="2024-05-01T10:06:30.000Z",
        )
    ]
    skipped, reported = _log_records(log_stream)
    assert skipped["message"] == "usage_report_skipped_spot_interruption"
    assert skipped["task_arn"] == _TASK_ARN
    assert reported["message"] == "usage_report_delivered"
    assert reported["task_arn"] == _RETRY_TASK_ARN
    assert reported["run_id"] == _RUN_ID


def test_spot_interruption_of_a_non_sandbox_task_is_ignored(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    # The sandbox filter runs first, so an unrelated task logs usage_event_ignored.
    event = _event(stop_code="SpotInterruption", group="family:some-other-task")

    lambda_handler(event, _context())

    assert delivered.reports == []
    assert _only_record(log_stream)["message"] == "usage_event_ignored"


def test_on_demand_task_with_a_runner_exit_code_is_metered_as_before(
    delivered: _RecordingClient,
) -> None:
    # Req 12.3: a non-Spot stop (here the Runner's interim exit 75) keeps its metering.
    event = _event(capacity_provider_name="FARGATE", runner_exit_code=75)

    lambda_handler(event, _context())

    (report,) = delivered.reports
    assert report.billed_minutes == 3


def test_task_detail_exposes_capacity_and_runner_exit_code() -> None:
    spot = EcsTaskStateChangeEvent.model_validate(_spot_interruption_event()).detail
    retry = EcsTaskStateChangeEvent.model_validate(
        _event(runner_exit_code=75, capacity_provider_name="FARGATE")
    ).detail

    assert spot.capacityProviderName == "FARGATE_SPOT"
    assert spot.runner_exit_code() is None
    assert spot.runner_env("TRIKON_ATTEMPT") == "1"
    assert spot.is_stopped_sandbox_task()
    assert retry.capacityProviderName == "FARGATE"
    assert retry.runner_exit_code() == 75
    assert retry.containers == (EcsContainer(name="runner", exitCode=75),)


def test_runner_exit_code_reads_only_the_runner_container() -> None:
    detail = EcsTaskDetail(
        taskArn=_TASK_ARN,
        clusterArn=_CLUSTER_ARN,
        lastStatus="STOPPED",
        containers=(EcsContainer(name="log-router", exitCode=1),),
    )

    assert detail.runner_exit_code() is None
    assert detail.capacityProviderName is None


# ---------------------------------------------------------------------------
# Ignored events.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_event(last_status="RUNNING", stopped_at=None), id="running"),
        pytest.param(_event(last_status="DEPROVISIONING"), id="deprovisioning"),
        pytest.param(
            _event(cluster_arn=f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/other-cluster"),
            id="wrong-cluster",
        ),
        pytest.param(
            _event(cluster_arn=f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/trikon-verify-cluster-2"),
            id="cluster-name-prefix",
        ),
        pytest.param(_event(group="family:some-other-task"), id="wrong-group"),
        pytest.param(_event(group="service:trikon-verify-runner"), id="service-group"),
        pytest.param(_event(group=None), id="no-group"),
    ],
)
def test_non_runner_events_are_ignored(
    delivered: _RecordingClient, log_stream: io.StringIO, event: dict[str, object]
) -> None:
    lambda_handler(event, _context())

    assert delivered.reports == []
    record = _only_record(log_stream)
    assert record["level"] == "INFO"
    assert record["message"] == "usage_event_ignored"
    assert record["task_arn"] == _TASK_ARN
    assert "run_id" not in record
    assert _RESERVATION_ID not in log_stream.getvalue()


def test_bare_cluster_name_is_accepted(delivered: _RecordingClient) -> None:
    lambda_handler(_event(cluster_arn="trikon-verify-cluster"), _context())

    assert len(delivered.reports) == 1


def test_log_keys_do_not_carry_over_between_invocations(
    delivered: _RecordingClient, log_stream: io.StringIO
) -> None:
    lambda_handler(_event(), _context())
    lambda_handler(_event(last_status="RUNNING", stopped_at=None), _context())

    first, second = _log_records(log_stream)
    assert first["message"] == "usage_report_delivered"
    assert first["run_id"] == _RUN_ID
    assert second["message"] == "usage_event_ignored"
    assert "run_id" not in second


# ---------------------------------------------------------------------------
# Cold-start bootstrap.
# ---------------------------------------------------------------------------


def test_bootstrap_builds_and_caches_the_client_without_calling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Building a boto3 client needs a region but makes no request.
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    env, client = handler._bootstrap()

    assert env.trikon_web_api_url == _API_URL
    assert env.trikon_cloud_usage_secret_parameter_name == _SECRET_PARAMETER_NAME
    assert isinstance(client, UsageApiClient)
    assert repr(client) == (
        f"UsageApiClient(quota_url='{_API_URL}{QUOTA_PATH}', usage_url='{_API_URL}{USAGE_PATH}')"
    )
    # The Usage_Secret is read lazily on the first report, not at cold start.
    reader = client._secret_reader
    assert isinstance(reader, UsageSecretReader)
    assert reader._secret is None
    # The reader is wired to its own SSM client and the parameter name from the env.
    assert reader._parameter_name == _SECRET_PARAMETER_NAME
    # botocore names the generated client class after the service.
    assert type(reader._ssm_client).__name__ == "SSM"

    again_env, again_client = handler._bootstrap()
    assert again_env is env
    assert again_client is client
    assert handler._ENV is env
    assert handler._USAGE_CLIENT is client

    handler._reset_bootstrap()
    assert handler._ENV is None
    assert handler._USAGE_CLIENT is None


def test_bootstrap_keeps_an_injected_client(delivered: _RecordingClient) -> None:
    env, client = handler._bootstrap()

    assert client is delivered
    assert env is handler._ENV


def test_bootstrap_rejects_a_non_https_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIKON_WEB_API_URL", "http://api.trikon.example.test")

    with pytest.raises(ValidationError):
        lambda_handler(_event(), _context())

    assert handler._USAGE_CLIENT is None
