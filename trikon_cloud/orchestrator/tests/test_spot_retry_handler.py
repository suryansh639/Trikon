"""Focused unit tests for :mod:`trikon_cloud.orchestrator.spot_retry_handler`.

Feature: trikon-cloud-cost-cuts, Requirements 9.3, 9.6, 9.8, 11.1-11.4, 11.6, 11.7.

Exercises the handler flow with small in-memory DynamoDB and SQS doubles and
a recording GitHub client, so each branch (send, duplicate, in-flight, stale
claim, send failure, publish failure, best-effort mark) is pinned without
moto. The moto-backed example suite lives in ``test_spot_retry.py``.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO, Final

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trikon_cloud.orchestrator import spot_retry_handler
from trikon_cloud.orchestrator.models import SpotRetryEnvConfig, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import (
    CheckRunConclusion,
    OrchestratorGithubClient,
    OrchestratorGithubClientError,
)
from trikon_cloud.orchestrator.spot_retry import SpotEventError
from trikon_cloud.orchestrator.spot_retry_claims import CLAIM_STALE_MS, SpotRetryClaims
from trikon_cloud.orchestrator.spot_retry_handler import (
    SpotRetryInFlightError,
    lambda_handler,
)
from trikon_cloud.orchestrator.spot_retry_logger import LOGGING_DENYLIST, get_logger
from trikon_cloud.orchestrator.tests.conftest import (
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_VERIFY_JOBS_QUEUE_URL,
)

_TASK_ARN: Final = "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/0123abcd"
_CLUSTER_ARN: Final = "arn:aws:ecs:us-east-1:000000000000:cluster/trikon-verify-cluster"
_RESERVATION_ID: Final = "3f1c2a9e-7b4d-4e8a-9c21-5d6e7f8a9b0c"
_NOW: Final = 1_714_557_751_000
_CLAIMS_TABLE: Final = "trikon-cloud-spot-retries"
_PR_STATE_TABLE: Final = "trikon_pr_state"
_VERDICTS_TABLE: Final = "trikon_verdicts"
_ALLOWED_LOG_KEYS: Final = frozenset(
    {"task_arn", "run_id", "reason", "aws_error_code"}
    | {"level", "location", "message", "timestamp", "service", "xray_trace_id"}
)


# ---------------------------------------------------------------------------
# Doubles.
# ---------------------------------------------------------------------------


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "stubbed"}}, operation)


class _FakeDynamoDb:
    """Claims (conditional on ``task_arn``), verdict rows and ``trikon_pr_state``."""

    def __init__(self) -> None:
        self.claims: dict[str, dict[str, Mapping[str, object]]] = {}
        self.verdicts: list[Mapping[str, Mapping[str, object]]] = []
        self.pr_state: dict[str, dict[str, Mapping[str, object]]] = {}
        self.fail_mark = False

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Item: Mapping[str, Mapping[str, object]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object:
        if TableName == _CLAIMS_TABLE:
            key = str(Item["task_arn"]["S"])
            if key in self.claims:
                raise _client_error("ConditionalCheckFailedException", "PutItem")
            self.claims[key] = dict(Item)
        else:
            assert TableName == _VERDICTS_TABLE
            self.verdicts.append(Item)
        return {}

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        ConsistentRead: bool,  # noqa: N803
    ) -> dict[str, object]:
        assert ConsistentRead
        if TableName == _CLAIMS_TABLE:
            item = self.claims.get(Key["task_arn"]["S"])
        else:
            assert TableName == _PR_STATE_TABLE
            item = self.pr_state.get(Key["pr_key"]["S"])
        return {} if item is None else {"Item": item}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeNames: Mapping[str, str],  # noqa: N803
        ExpressionAttributeValues: Mapping[str, Mapping[str, str]],  # noqa: N803
    ) -> object:
        if self.fail_mark:
            raise _client_error("ProvisionedThroughputExceededException", "UpdateItem")
        assert TableName == _CLAIMS_TABLE
        self.claims[Key["task_arn"]["S"]]["status"] = ExpressionAttributeValues[":s"]
        return {}

    def claim_status(self) -> object:
        return self.claims[_TASK_ARN]["status"]["S"]


class _FakeSqs:
    def __init__(self, *, error: ClientError | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._error = error

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        if self._error is not None:
            raise self._error
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "m-1"}


class _RecordingGithub(OrchestratorGithubClient):
    """Records Check Run calls; ``fail`` makes every call raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[tuple[str, CheckRunConclusion]] = []
        self.updated: list[tuple[int, CheckRunConclusion]] = []
        self._fail = fail

    def create_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        if self._fail:
            raise OrchestratorGithubClientError("github request returned 502 after 4 attempts")
        self.created.append((head_sha, conclusion))

    def update_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        check_run_id: int,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        if self._fail:
            raise OrchestratorGithubClientError("github request returned 502 after 4 attempts")
        self.updated.append((check_run_id, conclusion))


class _Wiring:
    def __init__(self, ddb: _FakeDynamoDb, sqs: _FakeSqs, github: _RecordingGithub) -> None:
        self.ddb = ddb
        self.sqs = sqs
        self.github = github


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> _Wiring:
    """Inject the doubles into the handler's module caches."""
    ddb = _FakeDynamoDb()
    sqs = _FakeSqs()
    github = _RecordingGithub()
    _install(monkeypatch, ddb=ddb, sqs=sqs, github=github)
    return _Wiring(ddb, sqs, github)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ddb: _FakeDynamoDb,
    sqs: _FakeSqs,
    github: _RecordingGithub,
) -> None:
    monkeypatch.setattr(spot_retry_handler, "_ENV", SpotRetryEnvConfig())
    monkeypatch.setattr(
        spot_retry_handler,
        "_CLAIMS",
        SpotRetryClaims(table_name=_CLAIMS_TABLE, ddb_client=ddb),
    )
    monkeypatch.setattr(spot_retry_handler, "_DDB_CLIENT", ddb)
    monkeypatch.setattr(spot_retry_handler, "_SQS_CLIENT", sqs)
    monkeypatch.setattr(spot_retry_handler, "_GITHUB_CLIENT", github)
    monkeypatch.setattr(spot_retry_handler, "_NOW_MS", lambda: _NOW)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Capture the Spot_Retry_Handler logger's JSON records."""
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-spot-retry")
    original_level = stdlib_logger.level
    original: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            original.append((handler, handler.stream))
            handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in original:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


def _records(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _assert_published(ddb: _FakeDynamoDb, buf: io.StringIO, reason: str) -> None:
    """One Spot verdict row was written and the publish log names ``reason``."""
    assert len(ddb.verdicts) == 1
    assert ddb.verdicts[0]["matched_rule"] == {"S": "spot interruption not retried"}
    published = [r for r in _records(buf) if r["message"] == "spot_fail_closed_published"]
    assert [r["reason"] for r in published] == [reason]


# ---------------------------------------------------------------------------
# Event builder.
# ---------------------------------------------------------------------------


def _runner_env(**changes: str | None) -> dict[str, str]:
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
    stop_code: str = "SpotInterruption",
    exit_code: int = 0,
) -> dict[str, object]:
    environment = [
        {"name": name, "value": value}
        for name, value in (env if env is not None else _runner_env()).items()
    ]
    return {
        "version": "0",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "time": "2024-05-01T10:02:31Z",
        "detail": {
            "taskArn": _TASK_ARN,
            "clusterArn": _CLUSTER_ARN,
            "group": "family:trikon-verify-runner",
            "lastStatus": "STOPPED",
            "stopCode": stop_code,
            "stoppedAt": "2024-05-01T10:02:30.123456Z",
            "capacityProviderName": "FARGATE_SPOT",
            "containers": [{"name": "runner", "lastStatus": "STOPPED", "exitCode": exit_code}],
            "overrides": {
                "containerOverrides": [{"name": "runner", "environment": environment}]
            },
        },
    }


def _invoke(event: dict[str, object]) -> None:
    lambda_handler(event, LambdaContext())


# ---------------------------------------------------------------------------
# Requeue and dedupe (Req 9.3).
# ---------------------------------------------------------------------------


def test_spot_interruption_sends_one_retry_job_and_dedupes(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.3."""
    _invoke(_event())
    _invoke(_event())

    assert len(wiring.sqs.sent) == 1
    queue_url, body = wiring.sqs.sent[0]
    assert queue_url == CANONICAL_VERIFY_JOBS_QUEUE_URL
    job = VerifyJobMessage.model_validate_json(body)
    assert job.attempt == 2
    assert job.reservation_id == _RESERVATION_ID
    assert job.delivery_id == CANONICAL_DELIVERY_ID
    assert wiring.ddb.claim_status() == "sent"
    assert wiring.github.created == []
    assert wiring.github.updated == []


def test_non_interruption_is_a_no_op(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.6."""
    _invoke(_event(stop_code="EssentialContainerExited"))

    assert wiring.sqs.sent == []
    assert wiring.ddb.claims == {}
    assert wiring.github.created == []


def test_in_flight_claim_raises_without_sending(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.3."""
    wiring.ddb.claims[_TASK_ARN] = {
        "task_arn": {"S": _TASK_ARN},
        "status": {"S": "claimed"},
        "claimed_at_ms": {"N": str(_NOW - 1_000)},
    }
    with pytest.raises(SpotRetryInFlightError):
        _invoke(_event())
    assert wiring.sqs.sent == []
    assert wiring.github.created == []


def test_stale_claim_fails_closed_without_sending(
    wiring: _Wiring, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirements 9.3, 11.7."""
    wiring.ddb.claims[_TASK_ARN] = {
        "task_arn": {"S": _TASK_ARN},
        "status": {"S": "claimed"},
        "claimed_at_ms": {"N": str(_NOW - CLAIM_STALE_MS)},
    }
    _invoke(_event())

    assert wiring.sqs.sent == []
    assert wiring.github.created == [(CANONICAL_HEAD_SHA, "action_required")]
    assert len(wiring.ddb.verdicts) == 1
    _assert_published(wiring.ddb, log_stream, "retry_claim_stale")
    assert wiring.ddb.claim_status() == "failed_closed"


# ---------------------------------------------------------------------------
# Fail-closed paths (Req 11.1-11.4, 11.6).
# ---------------------------------------------------------------------------


def test_send_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.1."""
    ddb = _FakeDynamoDb()
    github = _RecordingGithub()
    _install(
        monkeypatch,
        ddb=ddb,
        sqs=_FakeSqs(error=_client_error("AccessDenied", "SendMessage")),
        github=github,
    )
    _invoke(_event())

    assert github.created == [(CANONICAL_HEAD_SHA, "action_required")]
    _assert_published(ddb, log_stream, "retry_send_failed")
    assert ddb.claim_status() == "failed_closed"


def test_attempt_two_interruption_fails_closed_with_no_message(
    wiring: _Wiring, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.2."""
    _invoke(_event(env=_runner_env(TRIKON_ATTEMPT="2", TRIKON_CAPACITY="FARGATE")))

    assert wiring.sqs.sent == []
    assert wiring.github.created == [(CANONICAL_HEAD_SHA, "action_required")]
    _assert_published(wiring.ddb, log_stream, "retry_exhausted")


def test_interim_run_is_completed_when_pr_state_points_at_it(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirements 11.3, 11.7."""
    pr_key = f"{CANONICAL_INSTALLATION_ID}#{CANONICAL_REPO_FULL_NAME}#{CANONICAL_PR_NUMBER}"
    wiring.ddb.pr_state[pr_key] = {
        "pr_key": {"S": pr_key},
        "last_head_sha": {"S": CANONICAL_HEAD_SHA},
        "last_check_run_id": {"N": "777"},
    }
    _invoke(_event(stop_code="EssentialContainerExited", exit_code=75))

    assert wiring.github.updated == [(777, "action_required")]
    assert wiring.github.created == []


def test_unknown_identity_raises_without_claiming(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.4."""
    with pytest.raises(SpotEventError):
        _invoke(_event(env=_runner_env(TRIKON_HEAD_SHA=None)))
    assert wiring.ddb.claims == {}
    assert wiring.sqs.sent == []


def test_unparseable_event_raises_without_pydantic_context(wiring: _Wiring) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.8."""
    with pytest.raises(SpotEventError) as excinfo:
        _invoke({"detail": {"taskArn": 7}})
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_publish_failure_raises_and_leaves_claim_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.6."""
    ddb = _FakeDynamoDb()
    _install(monkeypatch, ddb=ddb, sqs=_FakeSqs(), github=_RecordingGithub(fail=True))

    with pytest.raises(OrchestratorGithubClientError):
        _invoke(_event(env=_runner_env(TRIKON_ATTEMPT="2")))
    assert ddb.claim_status() == "claimed"


def test_mark_failure_after_send_does_not_raise(
    wiring: _Wiring, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.3."""
    wiring.ddb.fail_mark = True
    _invoke(_event())

    assert len(wiring.sqs.sent) == 1
    assert wiring.ddb.claim_status() == "claimed"
    messages = [record["message"] for record in _records(log_stream)]
    assert messages == ["spot_retry_requeued", "spot_retry_claim_update_failed"]


# ---------------------------------------------------------------------------
# Logging (Req 9.8).
# ---------------------------------------------------------------------------


def test_log_records_carry_only_the_four_keys(
    wiring: _Wiring, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 9.8."""
    _invoke(_event())
    _invoke(_event())
    _invoke(_event(stop_code="EssentialContainerExited"))

    records = _records(log_stream)
    assert [record["message"] for record in records] == [
        "spot_retry_requeued",
        "spot_retry_duplicate",
        "spot_event_ignored",
    ]
    for record in records:
        assert set(record) <= _ALLOWED_LOG_KEYS, record
        assert record["service"] == "trikon-cloud-spot-retry"
        assert _RESERVATION_ID not in json.dumps(record)


def test_logger_is_a_singleton() -> None:
    assert get_logger() is get_logger()


def test_no_spot_retry_log_site_uses_denylisted_kwarg() -> None:
    """Grep guard: no ``log.<level>(..., <denylisted>=...)`` in the handler."""
    alternation = "|".join(sorted(LOGGING_DENYLIST))
    pattern = re.compile(
        rf"(?:log|get_logger\(\))\.(?:debug|info|warning|error|critical)\([^)]*\b({alternation})="
    )
    source = (Path(__file__).resolve().parent.parent / "spot_retry_handler.py").read_text(
        encoding="utf-8"
    )
    assert pattern.findall(source) == []
