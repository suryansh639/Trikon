"""Property 33 and examples for the orchestrator's ``busy`` quota outcome.

Feature: sandbox-minute-plans, Property 33: The busy outcome follows the
Queue_Wait_Limit and never fails the record (design.md, "Busy Jobs";
Req 11.18, 11.19, 11.20, 11.21, 16.15).

* The property drives :func:`handle_busy` directly with in-memory SQS, ECS,
  DynamoDB and GitHub stubs and an explicit clock value. Exactly one thing
  happens: one delayed copy, one Fail_Closed_Result, or (only when
  ``SendMessage`` fails) the record listed in ``batchItemFailures``.
* The examples go through :func:`lambda_handler` with a Usage_API stub that
  answers ``busy`` and an injected ``_NOW_MS`` clock:

  - within the limit: one copy, no SSM read, no RunTask, no Check Run, no
    verdict row, empty ``batchItemFailures``;
  - each copy fed back as the next record (receive count 1) for more cycles
    than ``maxReceiveCount = 3`` never fails or raises, so nothing reaches
    the DLQ; the cycle after the limit fails closed;
  - past the limit: no copy and one busy-timeout Fail_Closed_Result;
  - a ``SendMessage`` failure lists the record;
  - a record carrying every Lambda SQS key parses.

No network and no real AWS: every client is an in-memory stub.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.orchestrator import handler as handler_module
from trikon_cloud.orchestrator.busy_requeue import FIRST_RECEIVED_AT_ATTRIBUTE
from trikon_cloud.orchestrator.ecs_dispatcher import RunTaskDispatchResult
from trikon_cloud.orchestrator.handler import handle_busy, lambda_handler
from trikon_cloud.orchestrator.models import (
    OrchestratorEnvConfig,
    RunTaskCall,
    SqsRecord,
)
from trikon_cloud.orchestrator.never_fail_open import OrchestratorGithubClient
from trikon_cloud.usage_client.models import (
    QuotaBusy,
    QuotaDecision,
    QuotaRequest,
    UsageDelivered,
    UsageRejected,
    UsageReport,
)
from trikon_cloud.webhook_receiver.models import SqsJobMessage

from .conftest import (
    CANONICAL_DELIVERY_ID,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_VERIFY_JOBS_QUEUE_URL,
    CANONICAL_WEB_APP_URL,
    make_sqs_job_message,
)

_EVENT_SOURCE_ARN = "arn:aws:sqs:us-east-1:000000000000:trikon-verify-jobs"
_UPGRADE_URL = f"{CANONICAL_WEB_APP_URL}/account"
_BUSY_TITLE = "PR check couldn't start: too many checks in progress"
_BUSY_RULE = "concurrency limit reached"
#: The source queue's redrive policy (webhook receiver stack).
_MAX_RECEIVE_COUNT = 3
_T0_MS = 1_700_000_000_000
_DEFAULT_WAIT_LIMIT_SECONDS = 1800


# ---------------------------------------------------------------------------
# In-memory stubs.
# ---------------------------------------------------------------------------


class _StubSqs:
    """Records ``send_message`` / ``change_message_visibility``; may fail the send."""

    def __init__(self, *, send_error: Exception | None = None) -> None:
        self.send_error = send_error
        self.attempts: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self.visibility_changes: list[dict[str, object]] = []

    def send_message(
        self,
        *,
        QueueUrl: str,  # noqa: N803 -- boto3 API contract
        MessageBody: str,  # noqa: N803
        DelaySeconds: int,  # noqa: N803
        MessageAttributes: object,  # noqa: N803
    ) -> object:
        request: dict[str, object] = {
            "QueueUrl": QueueUrl,
            "MessageBody": MessageBody,
            "DelaySeconds": DelaySeconds,
            "MessageAttributes": MessageAttributes,
        }
        self.attempts.append(request)
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(request)
        return {"MessageId": f"copy-{len(self.sent)}"}

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,  # noqa: N803 -- boto3 API contract
        ReceiptHandle: str,  # noqa: N803
        VisibilityTimeout: int,  # noqa: N803
    ) -> object:
        self.visibility_changes.append(
            {
                "QueueUrl": QueueUrl,
                "ReceiptHandle": ReceiptHandle,
                "VisibilityTimeout": VisibilityTimeout,
            }
        )
        return {}


class _StubDynamoDb:
    """Records ``put_item`` calls (the Fail_Closed_Result verdict row)."""

    def __init__(self) -> None:
        self.puts: list[dict[str, object]] = []

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- boto3 API contract
        Item: dict[str, dict[str, str | int | bytes]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object:
        self.puts.append(
            {"TableName": TableName, "Item": Item, "ConditionExpression": ConditionExpression}
        )
        return {}


class _StubEcs:
    """Records ``run_task``; the busy path must never reach it."""

    def __init__(self) -> None:
        self.run_task_calls: list[dict[str, object]] = []

    def run_task(self, **kwargs: object) -> object:
        self.run_task_calls.append(dict(kwargs))
        return {"tasks": [], "failures": []}


class _StubSsm:
    """Records ``get_parameter``; the busy path must never reach it."""

    def __init__(self) -> None:
        self.reads: list[dict[str, object]] = []

    def get_parameter(self, **kwargs: object) -> object:
        self.reads.append(dict(kwargs))
        return {"Parameter": {"Value": "17"}}


class _StubBotoSession:
    """Hands out the stub clients and records which services were asked for."""

    def __init__(self) -> None:
        self.dynamodb = _StubDynamoDb()
        self.ecs = _StubEcs()
        self.ssm = _StubSsm()
        self.requested: list[str] = []

    def client(self, service_name: str) -> object:
        self.requested.append(service_name)
        clients: dict[str, object] = {
            "dynamodb": self.dynamodb,
            "ecs": self.ecs,
            "ssm": self.ssm,
        }
        return clients[service_name]


class _StubGithub:
    """Records Check Run POSTs instead of calling GitHub."""

    def __init__(self) -> None:
        self.check_runs: list[dict[str, object]] = []
        self.neutral_check_runs: list[dict[str, object]] = []

    def create_check_run(self, **kwargs: object) -> None:
        self.check_runs.append(dict(kwargs))

    def create_neutral_check_run(self, **kwargs: object) -> None:
        self.neutral_check_runs.append(dict(kwargs))


class _BusyUsageClient:
    """Usage_API stub whose every Quota_Request answers ``busy``."""

    def __init__(self, decision: QuotaBusy) -> None:
        self.decision = decision
        self.quota_requests: list[QuotaRequest] = []
        self.reports: list[UsageReport] = []

    def check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision:
        del budget_seconds
        self.quota_requests.append(request)
        return self.decision

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        self.reports.append(report)
        return UsageDelivered(outcome="recorded")


class _RecordingResolver:
    """Stands in for :class:`TaskDefinitionResolver`; counts SSM resolves."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, *, ssm_client: object) -> str:
        del ssm_client
        self.calls += 1
        return "trikon-verify-runner:17"


class _Clock:
    """The injected ``_NOW_MS`` clock (Unix ms)."""

    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.now_ms


# ---------------------------------------------------------------------------
# SQS record builders.
# ---------------------------------------------------------------------------


def _wire_attribute(data_type: str, string_value: str | None) -> dict[str, object]:
    """One Lambda ``messageAttributes`` entry, with the list keys AWS adds."""
    entry: dict[str, object] = {
        "dataType": data_type,
        "stringListValues": [],
        "binaryListValues": [],
    }
    if string_value is not None:
        entry["stringValue"] = string_value
    return entry


def _original_record(
    *,
    body: str,
    first_ms: int,
    message_id: str = "sqs-msg-1",
    receive_count: str = "1",
) -> dict[str, object]:
    """The webhook receiver's message as Lambda delivers it (no message attributes)."""
    return {
        "messageId": message_id,
        "receiptHandle": f"rh-{message_id}",
        "body": body,
        "attributes": {
            "ApproximateReceiveCount": receive_count,
            "ApproximateFirstReceiveTimestamp": str(first_ms),
            "SentTimestamp": str(first_ms),
        },
        "messageAttributes": {},
        "md5OfBody": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": _EVENT_SOURCE_ARN,
        "awsRegion": "us-east-1",
    }


def _delivered_copy(
    request: dict[str, object], *, cycle: int, approximate_first_ms: int
) -> dict[str, object]:
    """The Lambda record SQS would deliver for a sent copy.

    A new message: new ids, receive count 1, and a fresh
    ``ApproximateFirstReceiveTimestamp``. ``MessageAttributes`` come back as
    ``messageAttributes`` with lower-camel ``dataType``/``stringValue``.
    """
    body = request["MessageBody"]
    attributes = request["MessageAttributes"]
    assert isinstance(body, str)
    assert isinstance(attributes, dict)
    message_attributes: dict[str, object] = {}
    for name, value in attributes.items():
        assert isinstance(value, dict)
        message_attributes[name] = _wire_attribute(value["DataType"], value["StringValue"])
    return {
        "messageId": f"copy-{cycle}",
        "receiptHandle": f"rh-copy-{cycle}",
        "body": body,
        "attributes": {
            "ApproximateReceiveCount": "1",
            "ApproximateFirstReceiveTimestamp": str(approximate_first_ms),
            "SentTimestamp": str(approximate_first_ms),
        },
        "messageAttributes": message_attributes,
        "md5OfBody": "0" * 32,
        "md5OfMessageAttributes": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": _EVENT_SOURCE_ARN,
        "awsRegion": "us-east-1",
    }


def _event(record: dict[str, object]) -> dict[str, object]:
    return {"Records": [record]}


def _expected_copy(*, body: str, delay_seconds: int, first_ms: int) -> dict[str, object]:
    return {
        "QueueUrl": CANONICAL_VERIFY_JOBS_QUEUE_URL,
        "MessageBody": body,
        "DelaySeconds": delay_seconds,
        "MessageAttributes": {
            FIRST_RECEIVED_AT_ATTRIBUTE: {"DataType": "Number", "StringValue": str(first_ms)}
        },
    }


def _limit_text(concurrency_limit: int) -> str:
    return "1 PR check" if concurrency_limit == 1 else f"{concurrency_limit} PR checks"


def _evidence(item: dict[str, dict[str, str | int | bytes]]) -> dict[str, object]:
    blob = item["evidence_blob"]["B"]
    assert isinstance(blob, bytes)
    decoded = json.loads(gzip.decompress(blob))
    assert isinstance(decoded, dict)
    return decoded


def _lambda_context() -> LambdaContext:
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-orchestrator-test"
    return cast(LambdaContext, ctx)


_SEND_ERRORS: list[Exception] = [
    ClientError(
        {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue", "Message": "x"}},
        "SendMessage",
    ),
    EndpointConnectionError(endpoint_url="https://sqs.us-east-1.amazonaws.com/"),
]


# ---------------------------------------------------------------------------
# Property 33.
# ---------------------------------------------------------------------------


@st.composite
def _busy_records(draw: st.DrawFn) -> tuple[dict[str, object], int]:
    """A Lambda record whose first receive time is ``first_ms``.

    Either an original (``ApproximateFirstReceiveTimestamp`` only) or a busy
    copy (the ``trikon_first_received_at`` attribute, sometimes with the
    copy's own later SQS first-receive time, which must be ignored).
    """
    first_ms = draw(st.integers(min_value=1, max_value=10**13))
    attributes: dict[str, object] = {
        "ApproximateReceiveCount": draw(st.sampled_from(["1", "2", "3"])),
        "SentTimestamp": str(first_ms),
    }
    message_attributes: dict[str, object] = {}
    if draw(st.booleans()):
        message_attributes[FIRST_RECEIVED_AT_ATTRIBUTE] = _wire_attribute(
            "Number", str(first_ms)
        )
        if draw(st.booleans()):
            later = first_ms + draw(st.integers(min_value=0, max_value=10**9))
            attributes["ApproximateFirstReceiveTimestamp"] = str(later)
    else:
        attributes["ApproximateFirstReceiveTimestamp"] = str(first_ms)
    body = make_sqs_job_message(delivery_id=str(draw(st.uuids()))).model_dump_json()
    record: dict[str, object] = {
        "messageId": draw(st.uuids()).hex,
        "receiptHandle": "rh-" + draw(st.uuids()).hex,
        "body": body,
        "attributes": attributes,
        "messageAttributes": message_attributes,
        "md5OfBody": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": _EVENT_SOURCE_ARN,
        "awsRegion": "us-east-1",
    }
    return record, first_ms


@given(
    record_and_first=_busy_records(),
    position=st.sampled_from(["before", "at", "past"]),
    offset_ms=st.integers(min_value=0, max_value=10**9),
    skew_ms=st.integers(min_value=0, max_value=10**7),
    retry_after_seconds=st.integers(min_value=1, max_value=900),
    concurrency_limit=st.integers(min_value=1, max_value=100),
    wait_limit_seconds=st.integers(min_value=60, max_value=86_400),
    send_error=st.one_of(st.none(), st.sampled_from(_SEND_ERRORS)),
)
@settings(max_examples=200, deadline=None)
def test_property_busy_outcome_follows_the_wait_limit(
    record_and_first: tuple[dict[str, object], int],
    position: str,
    offset_ms: int,
    skew_ms: int,
    retry_after_seconds: int,
    concurrency_limit: int,
    wait_limit_seconds: int,
    send_error: Exception | None,
) -> None:
    """Feature: sandbox-minute-plans, Property 33: The busy outcome follows the Queue_Wait_Limit and never fails the record.

    **Validates: Requirements 11.18, 11.19, 11.20, 11.21, 16.15**
    """
    raw, first_ms = record_and_first
    limit_ms = wait_limit_seconds * 1000
    if position == "before":
        # Anywhere from a skewed future first receive up to just under the limit.
        now_ms = max(0, first_ms - skew_ms + offset_ms % limit_ms)
    elif position == "at":
        now_ms = first_ms + limit_ms
    else:
        now_ms = first_ms + limit_ms + 1 + offset_ms
    timed_out = max(0, now_ms - first_ms) > limit_ms

    record = SqsRecord.model_validate(raw)
    message = SqsJobMessage.model_validate_json(record.body)
    env = OrchestratorEnvConfig().model_copy(
        update={"trikon_queue_wait_limit_seconds": wait_limit_seconds}
    )
    sqs = _StubSqs(send_error=send_error)
    session = _StubBotoSession()
    github = _StubGithub()
    run_task_calls: list[RunTaskCall] = []

    def _no_run_task(call: RunTaskCall, *, ecs_client: object) -> RunTaskDispatchResult:
        del ecs_client
        run_task_calls.append(call)
        raise AssertionError("the busy path must never call ecs.RunTask")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler_module, "submit_run_task", _no_run_task)
        result = handle_busy(
            record,
            QuotaBusy(retry_after_seconds=retry_after_seconds, concurrency_limit=concurrency_limit),
            env,
            sqs,
            now_ms,
            message=message,
            boto_session=session,
            github_client=cast(OrchestratorGithubClient, github),
        )

    # Never RunTask, never an SSM read, never a neutral Check Run.
    assert run_task_calls == []
    assert session.ecs.run_task_calls == []
    assert "ecs" not in session.requested
    assert "ssm" not in session.requested
    assert github.neutral_check_runs == []

    copied = len(sqs.sent) == 1
    failed_closed = len(session.dynamodb.puts) == 1 and len(github.check_runs) == 1
    failures = result["batchItemFailures"]
    assert isinstance(failures, list)
    listed = failures == [{"itemIdentifier": record.messageId}]
    assert [copied, failed_closed, listed].count(True) == 1
    # batchItemFailures is non-empty only when SendMessage failed.
    assert (failures != []) == (not timed_out and send_error is not None)

    if timed_out:
        assert sqs.attempts == []
        assert result == {"batchItemFailures": []}
        item = session.dynamodb.puts[0]["Item"]
        assert isinstance(item, dict)
        assert item["matched_rule"] == {"S": _BUSY_RULE}
        assert item["decision"] == {"S": "require_human"}
        check_run = github.check_runs[0]
        assert check_run["conclusion"] == "action_required"
        assert check_run["details_url"] == _UPGRADE_URL
        assert check_run["title"] == _BUSY_TITLE
        summary = check_run["summary"]
        assert isinstance(summary, str)
        assert f"runs {_limit_text(concurrency_limit)} at a time" in summary
    else:
        # Exactly one copy attempt, carrying the first receive time.
        assert sqs.attempts == [
            _expected_copy(
                body=record.body, delay_seconds=retry_after_seconds, first_ms=first_ms
            )
        ]
        assert session.dynamodb.puts == []
        assert github.check_runs == []
        assert "dynamodb" not in session.requested
        if send_error is None:
            assert result == {"batchItemFailures": []}
            assert sqs.visibility_changes == []
        else:
            assert sqs.visibility_changes == [
                {
                    "QueueUrl": CANONICAL_VERIFY_JOBS_QUEUE_URL,
                    "ReceiptHandle": record.receiptHandle,
                    "VisibilityTimeout": retry_after_seconds,
                }
            ]


# ---------------------------------------------------------------------------
# lambda_handler examples: harness and log capture.
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    sqs: _StubSqs
    session: _StubBotoSession
    github: _StubGithub
    usage: _BusyUsageClient
    resolver: _RecordingResolver
    clock: _Clock
    env: OrchestratorEnvConfig
    run_task_calls: list[RunTaskCall] = field(default_factory=list)

    def answer(self, *, retry_after_seconds: int, concurrency_limit: int) -> None:
        self.usage.decision = QuotaBusy(
            retry_after_seconds=retry_after_seconds, concurrency_limit=concurrency_limit
        )

    def assert_no_dispatch(self) -> None:
        """No SSM read, no RunTask (Req 11.18)."""
        assert self.resolver.calls == 0
        assert self.session.ssm.reads == []
        assert self.session.ecs.run_task_calls == []
        assert self.run_task_calls == []
        assert "ssm" not in self.session.requested
        assert "ecs" not in self.session.requested


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Fill every handler cache with an in-memory stub so ``_bootstrap`` builds nothing."""
    env = OrchestratorEnvConfig()
    h = _Harness(
        sqs=_StubSqs(),
        session=_StubBotoSession(),
        github=_StubGithub(),
        usage=_BusyUsageClient(QuotaBusy(retry_after_seconds=60, concurrency_limit=1)),
        resolver=_RecordingResolver(),
        clock=_Clock(_T0_MS),
        env=env,
    )

    def _record_run_task(call: RunTaskCall, *, ecs_client: object) -> RunTaskDispatchResult:
        del ecs_client
        h.run_task_calls.append(call)
        return RunTaskDispatchResult(task_arn="arn:aws:ecs:us-east-1:000000000000:task/x/y")

    monkeypatch.setattr(handler_module, "_ENV", env)
    monkeypatch.setattr(handler_module, "_RESOLVER", h.resolver)
    monkeypatch.setattr(handler_module, "_BOTO_SESSION", h.session)
    monkeypatch.setattr(handler_module, "_GITHUB_CLIENT", h.github)
    monkeypatch.setattr(handler_module, "_USAGE_CLIENT", h.usage)
    monkeypatch.setattr(handler_module, "_SQS_CLIENT", h.sqs)
    monkeypatch.setattr(handler_module, "_NOW_MS", h.clock)
    monkeypatch.setattr(handler_module, "submit_run_task", _record_run_task)
    return h


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's stream to a :class:`io.StringIO`."""
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-orchestrator")
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            original_streams.append((handler, handler.stream))
            handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in original_streams:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


def _log_records(log_stream: io.StringIO) -> list[dict[str, object]]:
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _messages(log_stream: io.StringIO, message: str) -> list[dict[str, object]]:
    return [r for r in _log_records(log_stream) if r.get("message") == message]


# ---------------------------------------------------------------------------
# Examples.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retry_after_seconds", [1, 60, 900])
def test_busy_within_the_limit_sends_one_delayed_copy_and_nothing_else(
    harness: _Harness,
    log_stream: io.StringIO,
    retry_after_seconds: int,
) -> None:
    """Req 11.18: one copy with ``DelaySeconds = retry_after_seconds``, SQS deletes the original."""
    harness.answer(retry_after_seconds=retry_after_seconds, concurrency_limit=1)
    body = make_sqs_job_message().model_dump_json()
    harness.clock.now_ms = _T0_MS + 120_000

    result = lambda_handler(_event(_original_record(body=body, first_ms=_T0_MS)), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert harness.sqs.sent == [
        _expected_copy(body=body, delay_seconds=retry_after_seconds, first_ms=_T0_MS)
    ]
    assert harness.sqs.visibility_changes == []
    harness.assert_no_dispatch()
    assert harness.github.check_runs == []
    assert harness.github.neutral_check_runs == []
    assert harness.session.dynamodb.puts == []
    assert harness.clock.calls == 1
    assert [r.run_id for r in harness.usage.quota_requests] == [CANONICAL_DELIVERY_ID]
    assert harness.usage.reports == []

    checked = _messages(log_stream, "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "busy"
    # Powertools omits None-valued keys, so no denial_reason on a busy outcome.
    assert "denial_reason" not in checked[0]
    requeued = _messages(log_stream, "busy_requeued")
    assert len(requeued) == 1
    assert requeued[0]["level"] == "INFO"
    assert requeued[0]["waited_seconds"] == 120
    assert requeued[0]["delay_seconds"] == retry_after_seconds
    assert _messages(log_stream, "busy_wait_exceeded") == []
    assert body not in log_stream.getvalue()


def test_busy_copies_cycle_without_reaching_the_dlq_then_time_out(
    harness: _Harness,
    log_stream: io.StringIO,
) -> None:
    """Req 11.19-11.21: copies arrive with receive count 1, so redrive never fires.

    With ``retry_after_seconds = 300`` and the default 1800-second limit the
    Job is re-queued at 0, 300, ..., 1800 seconds waited (exactly the limit
    still re-queues), seven cycles in all, more than ``maxReceiveCount``.
    The next cycle, at 2100 seconds, fails closed.
    """
    retry_after_seconds = 300
    harness.answer(retry_after_seconds=retry_after_seconds, concurrency_limit=1)
    body = make_sqs_job_message().model_dump_json()
    record = _original_record(body=body, first_ms=_T0_MS)
    harness.clock.now_ms = _T0_MS

    receives: dict[str, int] = {}
    results: list[dict[str, object]] = []
    requeue_cycles = 0
    while not harness.session.dynamodb.puts:
        message_id = record["messageId"]
        assert isinstance(message_id, str)
        receives[message_id] = receives.get(message_id, 0) + 1
        attributes = record["attributes"]
        assert isinstance(attributes, dict)
        assert attributes["ApproximateReceiveCount"] == "1"

        sent_before = len(harness.sqs.sent)
        results.append(lambda_handler(_event(record), _lambda_context()))
        if harness.session.dynamodb.puts:
            assert len(harness.sqs.sent) == sent_before
            break
        assert len(harness.sqs.sent) == sent_before + 1
        request = harness.sqs.sent[-1]
        assert request == _expected_copy(
            body=body, delay_seconds=retry_after_seconds, first_ms=_T0_MS
        )
        requeue_cycles += 1
        # SQS delivers the copy after its delay; the clock moves with it.
        harness.clock.now_ms += retry_after_seconds * 1000
        record = _delivered_copy(
            request, cycle=requeue_cycles, approximate_first_ms=harness.clock.now_ms
        )
        assert requeue_cycles < 50, "the Job never timed out"

    assert requeue_cycles == 7
    assert requeue_cycles > _MAX_RECEIVE_COUNT
    # Every invocation succeeded: nothing listed, nothing raised.
    assert results == [{"batchItemFailures": []}] * (requeue_cycles + 1)
    # Each message was received once and deleted, so none reaches the DLQ.
    assert all(count == 1 for count in receives.values())
    assert max(receives.values()) <= _MAX_RECEIVE_COUNT
    harness.assert_no_dispatch()

    waited = [r["waited_seconds"] for r in _messages(log_stream, "busy_requeued")]
    assert waited == [0, 300, 600, 900, 1200, 1500, 1800]
    exceeded = _messages(log_stream, "busy_wait_exceeded")
    assert len(exceeded) == 1
    assert exceeded[0]["waited_seconds"] == 2100
    assert len(harness.session.dynamodb.puts) == 1
    assert len(harness.github.check_runs) == 1
    assert harness.github.check_runs[0]["conclusion"] == "action_required"


@pytest.mark.parametrize("concurrency_limit", [1, 5, 10])
def test_busy_past_the_limit_fails_closed_with_the_busy_timeout_notice(
    harness: _Harness,
    log_stream: io.StringIO,
    concurrency_limit: int,
) -> None:
    """Req 11.20: no copy, one Fail_Closed_Result, SQS deletes the Job."""
    harness.answer(retry_after_seconds=60, concurrency_limit=concurrency_limit)
    body = make_sqs_job_message().model_dump_json()
    harness.clock.now_ms = _T0_MS + _DEFAULT_WAIT_LIMIT_SECONDS * 1000 + 1

    result = lambda_handler(_event(_original_record(body=body, first_ms=_T0_MS)), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert harness.sqs.attempts == []
    assert harness.sqs.visibility_changes == []
    harness.assert_no_dispatch()
    assert harness.github.neutral_check_runs == []

    assert len(harness.session.dynamodb.puts) == 1
    put = harness.session.dynamodb.puts[0]
    assert put["TableName"] == harness.env.trikon_verdicts_table
    item = put["Item"]
    assert isinstance(item, dict)
    assert item["decision"] == {"S": "require_human"}
    assert item["matched_rule"] == {"S": _BUSY_RULE}
    evidence = _evidence(item)
    assert evidence["error_class"] == "quota"
    assert evidence["error_code"] == "busy_timeout"
    assert evidence["delivery_id"] == CANONICAL_DELIVERY_ID

    assert len(harness.github.check_runs) == 1
    check_run = harness.github.check_runs[0]
    assert check_run["installation_id"] == CANONICAL_INSTALLATION_ID
    assert check_run["repo_full_name"] == CANONICAL_REPO_FULL_NAME
    assert check_run["head_sha"] == CANONICAL_HEAD_SHA
    assert check_run["title"] == _BUSY_TITLE
    assert check_run["conclusion"] == "action_required"
    assert check_run["details_url"] == _UPGRADE_URL
    summary = check_run["summary"]
    assert isinstance(summary, str)
    assert f"runs {_limit_text(concurrency_limit)} at a time" in summary
    assert "waited more than 30 minutes" in summary

    exceeded = _messages(log_stream, "busy_wait_exceeded")
    assert len(exceeded) == 1
    assert exceeded[0]["level"] == "INFO"
    assert exceeded[0]["waited_seconds"] == _DEFAULT_WAIT_LIMIT_SECONDS
    assert _messages(log_stream, "busy_requeued") == []


@pytest.mark.parametrize(
    "send_error", _SEND_ERRORS, ids=["ClientError", "EndpointConnectionError"]
)
def test_busy_send_failure_lists_the_record(
    harness: _Harness,
    log_stream: io.StringIO,
    send_error: Exception,
) -> None:
    """Req 11.18: an unsent copy keeps the original in the queue."""
    harness.sqs.send_error = send_error
    harness.answer(retry_after_seconds=90, concurrency_limit=1)
    body = make_sqs_job_message().model_dump_json()
    harness.clock.now_ms = _T0_MS + 60_000

    result = lambda_handler(
        _event(_original_record(body=body, first_ms=_T0_MS, message_id="sqs-msg-7")),
        _lambda_context(),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "sqs-msg-7"}]}
    assert len(harness.sqs.attempts) == 1
    assert harness.sqs.sent == []
    assert harness.sqs.visibility_changes == [
        {
            "QueueUrl": CANONICAL_VERIFY_JOBS_QUEUE_URL,
            "ReceiptHandle": "rh-sqs-msg-7",
            "VisibilityTimeout": 90,
        }
    ]
    harness.assert_no_dispatch()
    assert harness.session.dynamodb.puts == []
    assert harness.github.check_runs == []
    assert len(_messages(log_stream, "busy_requeue_failed")) == 1
    assert _messages(log_stream, "busy_requeued") == []


def test_busy_record_with_every_lambda_sqs_key_parses(
    harness: _Harness,
    log_stream: io.StringIO,
) -> None:
    """A busy copy as Lambda delivers it, with every record and attribute key."""
    carried_first_ms = _T0_MS - 400_000
    body = make_sqs_job_message().model_dump_json()
    record: dict[str, object] = {
        "messageId": "059f36b4-87a3-44ab-83d2-661975830a7d",
        "receiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a",
        "body": body,
        "attributes": {
            "ApproximateReceiveCount": "1",
            "SentTimestamp": str(_T0_MS),
            "SenderId": "AIDAIENQZJOLO23YVJ4VO",
            "ApproximateFirstReceiveTimestamp": str(_T0_MS + 1_000),
            "AWSTraceHeader": "Root=1-5759e988-bd862e3fe1be46a994272793;Sampled=1",
            "SequenceNumber": "18849496460467696128",
            "MessageGroupId": "1",
            "MessageDeduplicationId": "1",
            "DeadLetterQueueSourceArn": _EVENT_SOURCE_ARN + "-src",
        },
        "messageAttributes": {
            FIRST_RECEIVED_AT_ATTRIBUTE: _wire_attribute("Number", str(carried_first_ms)),
            "unrelated": {
                "dataType": "Binary",
                "binaryValue": "AAEC",
                "stringListValues": [],
                "binaryListValues": [],
            },
        },
        "md5OfBody": "e4e68fb7bd0e697a0ae8f1bb342846b3",
        "md5OfMessageAttributes": "00484c68...59e48f06",
        "eventSource": "aws:sqs",
        "eventSourceARN": _EVENT_SOURCE_ARN,
        "awsRegion": "us-east-1",
    }
    harness.clock.now_ms = _T0_MS + 2_000

    result = lambda_handler(_event(record), _lambda_context())

    assert result == {"batchItemFailures": []}
    # The carried attribute wins over the copy's own SQS first-receive time.
    assert harness.sqs.sent == [
        _expected_copy(body=body, delay_seconds=60, first_ms=carried_first_ms)
    ]
    harness.assert_no_dispatch()
    requeued = _messages(log_stream, "busy_requeued")
    assert len(requeued) == 1
    assert requeued[0]["waited_seconds"] == 402
    assert _messages(log_stream, "orchestrator_payload_rejected") == []
