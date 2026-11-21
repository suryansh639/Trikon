"""Property 32 and examples for :mod:`trikon_cloud.orchestrator.busy_requeue`.

Feature: sandbox-minute-plans, Property 32: Busy copies preserve the first
receive time (design.md, "Busy Jobs"; Req 11.18, 11.21, 16.15).

* The property builds arbitrary Lambda SQS records (any unicode body; a
  valid, malformed, or missing ``trikon_first_received_at`` attribute; a
  valid, malformed, or missing ``ApproximateFirstReceiveTimestamp``), runs
  any number of busy cycles, and feeds each copy back as the next record the
  way SQS would deliver it. The first receive time never changes, the body
  stays byte-identical, and each delay is ``min(retry_after_seconds, 900)``.
* The ``decide_busy`` properties pin the wait-limit boundary.
* The examples cover a future first receive time, the exact limit, the
  ``first_received_at_fallback`` warning, a real ``SendMessage`` through moto,
  and the best-effort ``ChangeMessageVisibility`` after a failed send.

No network and no real AWS: moto runs under ``mock_aws`` with fake
credentials, and the failure path uses an in-memory stub client.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import IO

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from trikon_cloud.orchestrator.busy_requeue import (
    FIRST_RECEIVED_AT_ATTRIBUTE,
    MAX_SQS_DELAY_SECONDS,
    BusyRequeue,
    BusyTimedOut,
    RequeueRequest,
    build_requeue_request,
    decide_busy,
    first_received_at_ms,
    send_busy_copy,
)
from trikon_cloud.orchestrator.models import SqsRecord
from trikon_cloud.webhook_receiver.models import SqsJobMessage

from .conftest import CANONICAL_VERIFY_JOBS_QUEUE_URL, make_sqs_job_message

_QUEUE_URL = CANONICAL_VERIFY_JOBS_QUEUE_URL
_EVENT_SOURCE_ARN = "arn:aws:sqs:us-east-1:000000000000:trikon-verify-jobs"
_MAX_EPOCH_MS = 10**16 - 1


# ---------------------------------------------------------------------------
# Independent oracle for the first-receive-time selection rule.
# ---------------------------------------------------------------------------


def _is_epoch_ms(value: str | None) -> bool:
    """A positive Unix-ms string: ASCII digits, no leading zero, 1-16 chars.

    Written without the module's regex so the test does not share its bug.
    ``isascii`` matters: ``str.isdigit`` accepts Arabic-Indic, full-width and
    superscript digits, and ``int`` parses some of them.
    """
    return (
        value is not None
        and 1 <= len(value) <= 16
        and value.isascii()
        and value.isdigit()
        and value[0] != "0"
    )


def _expected_first_received_at(raw: dict[str, object], now_ms: int) -> int:
    """The first receive time ``raw`` should yield, per design Property 32."""
    message_attributes = raw.get("messageAttributes", {})
    assert isinstance(message_attributes, dict)
    carried = message_attributes.get(FIRST_RECEIVED_AT_ATTRIBUTE)
    if isinstance(carried, dict) and carried.get("dataType") == "Number":
        value = carried.get("stringValue")
        if isinstance(value, str) and _is_epoch_ms(value):
            return int(value)
    attributes = raw["attributes"]
    assert isinstance(attributes, dict)
    approximate = attributes.get("ApproximateFirstReceiveTimestamp")
    if isinstance(approximate, str) and _is_epoch_ms(approximate):
        return int(approximate)
    return now_ms


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_VALID_EPOCH = st.integers(min_value=1, max_value=_MAX_EPOCH_MS).map(str)

_INVALID_EPOCH = st.one_of(
    st.sampled_from(
        [
            "",
            "0",
            "00",
            "-5",
            "+5",
            "1.5",
            "1e12",
            " 1700000000000",
            "1700000000000 ",
            "1700000000000\n",
            "\u0661\u0667\u0660\u0660",  # Arabic-Indic digits
            "\uff11\uff17\uff10\uff10",  # full-width digits
            "17\u00b3",  # superscript three: isdigit() but not ASCII
        ]
    ),
    # Leading zero.
    st.integers(min_value=1, max_value=10**15 - 1).map(lambda n: f"0{n}"),
    # Too many digits.
    st.integers(min_value=10**16, max_value=10**20).map(str),
    # Trailing newline (``$`` would accept it; ``fullmatch`` must not).
    st.integers(min_value=1, max_value=_MAX_EPOCH_MS).map(lambda n: f"{n}\n"),
    # Anything else; the oracle decides.
    st.text(max_size=20),
)

_EPOCH_STRING = st.one_of(_VALID_EPOCH, _INVALID_EPOCH)


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


_FIRST_RECEIVED_ATTRIBUTE = st.one_of(
    st.none(),
    # A valid carried value (a copy written by the busy path).
    _VALID_EPOCH.map(lambda v: _wire_attribute("Number", v)),
    # A Number attribute with a malformed value.
    _INVALID_EPOCH.map(lambda v: _wire_attribute("Number", v)),
    # Wrong data type, any value (or none).
    st.builds(
        _wire_attribute,
        st.sampled_from(["String", "Binary", "number", "Number.ms", ""]),
        st.one_of(st.none(), _EPOCH_STRING),
    ),
    # Number with no stringValue.
    st.just(_wire_attribute("Number", None)),
)

_JOB_BODIES = st.uuids().map(
    lambda u: make_sqs_job_message(delivery_id=str(u)).model_dump_json()
)
_BODIES = st.one_of(st.text(), _JOB_BODIES)


@st.composite
def _raw_records(draw: st.DrawFn) -> dict[str, object]:
    """A Lambda SQS record dict as the event source mapping delivers it."""
    attributes: dict[str, object] = {
        "ApproximateReceiveCount": draw(st.sampled_from(["1", "2", "3"])),
        "SentTimestamp": "1700000000000",
    }
    approximate = draw(st.one_of(st.none(), _EPOCH_STRING))
    if approximate is not None:
        attributes["ApproximateFirstReceiveTimestamp"] = approximate

    message_attributes: dict[str, object] = {}
    if draw(st.booleans()):
        message_attributes["unrelated"] = _wire_attribute("String", "x")
    carried = draw(_FIRST_RECEIVED_ATTRIBUTE)
    if carried is not None:
        message_attributes[FIRST_RECEIVED_AT_ATTRIBUTE] = carried

    return {
        "messageId": draw(st.uuids()).hex,
        "receiptHandle": "rh-" + draw(st.uuids()).hex,
        "body": draw(_BODIES),
        "attributes": attributes,
        "messageAttributes": message_attributes,
        "md5OfBody": "0" * 32,
        "eventSource": "aws:sqs",
        "eventSourceARN": _EVENT_SOURCE_ARN,
        "awsRegion": "us-east-1",
    }


def _delivered_copy(
    request: RequeueRequest, *, cycle: int, approximate_first_ms: int
) -> SqsRecord:
    """The Lambda record SQS would deliver for the copy ``request`` describes.

    The copy is a new message: new ids, receive count 1, and a fresh
    ``ApproximateFirstReceiveTimestamp``. ``MessageAttributes`` come back as
    ``messageAttributes`` with lower-camel ``dataType``/``stringValue``.
    """
    return SqsRecord.model_validate(
        {
            "messageId": f"copy-{cycle}",
            "receiptHandle": f"rh-copy-{cycle}",
            "body": request["MessageBody"],
            "attributes": {
                "ApproximateReceiveCount": "1",
                "ApproximateFirstReceiveTimestamp": str(approximate_first_ms),
                "SentTimestamp": str(approximate_first_ms),
            },
            "messageAttributes": {
                name: _wire_attribute(value["DataType"], value["StringValue"])
                for name, value in request["MessageAttributes"].items()
            },
            "md5OfBody": "0" * 32,
            "md5OfMessageAttributes": "0" * 32,
            "eventSource": "aws:sqs",
            "eventSourceARN": _EVENT_SOURCE_ARN,
            "awsRegion": "us-east-1",
        }
    )


def _job_message_or_none(body: str) -> SqsJobMessage | None:
    try:
        return SqsJobMessage.model_validate_json(body)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Property 32.
# ---------------------------------------------------------------------------


@given(
    raw=_raw_records(),
    start_ms=st.integers(min_value=1, max_value=10**13),
    wait_limit_seconds=st.integers(min_value=60, max_value=86_400),
    concurrency_limit=st.integers(min_value=1, max_value=10),
    cycles=st.lists(
        st.tuples(
            # How far past the first receive time this cycle runs (clamped to
            # the wait limit below, so every cycle re-queues). Negative values
            # model clock skew.
            st.integers(min_value=-10**7, max_value=10**9),
            # retry_after_seconds; above 900 checks the SQS cap.
            st.integers(min_value=1, max_value=5_000),
            # Extra ms before SQS first delivers the copy.
            st.integers(min_value=0, max_value=10**6),
        ),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=200, deadline=None)
def test_property_busy_copies_preserve_first_receive_time(
    raw: dict[str, object],
    start_ms: int,
    wait_limit_seconds: int,
    concurrency_limit: int,
    cycles: list[tuple[int, int, int]],
) -> None:
    """Feature: sandbox-minute-plans, Property 32: Busy copies preserve the first receive time.

    **Validates: Requirements 11.18, 11.21, 16.15**
    """
    original_body = raw["body"]
    assert isinstance(original_body, str)
    original_job = _job_message_or_none(original_body)

    record = SqsRecord.model_validate(raw)
    expected_first = _expected_first_received_at(raw, start_ms)
    assert first_received_at_ms(record, now_ms=start_ms) == expected_first

    # With no valid timestamp on the original, the fallback is the clock of
    # that delivery, so the original's cycle runs at ``start_ms``.
    original_falls_back = _expected_first_received_at(raw, start_ms + 1) != expected_first

    current = record
    for cycle, (offset_ms, retry_after_seconds, delivery_lag_ms) in enumerate(cycles):
        if cycle == 0 and original_falls_back:
            now_ms = start_ms
        else:
            now_ms = max(1, expected_first + min(offset_ms, wait_limit_seconds * 1000))
        first = first_received_at_ms(current, now_ms=now_ms)
        assert first == expected_first

        decision = decide_busy(
            first_received_at_ms=first,
            now_ms=now_ms,
            retry_after_seconds=retry_after_seconds,
            concurrency_limit=concurrency_limit,
            wait_limit_seconds=wait_limit_seconds,
        )
        expected_delay = min(retry_after_seconds, MAX_SQS_DELAY_SECONDS)
        assert decision == BusyRequeue(
            delay_seconds=expected_delay, first_received_at_ms=expected_first
        )

        request = build_requeue_request(current, queue_url=_QUEUE_URL, decision=decision)
        assert request == {
            "QueueUrl": _QUEUE_URL,
            "MessageBody": original_body,
            "DelaySeconds": expected_delay,
            "MessageAttributes": {
                FIRST_RECEIVED_AT_ATTRIBUTE: {
                    "DataType": "Number",
                    "StringValue": str(expected_first),
                }
            },
        }
        assert request["MessageBody"].encode("utf-8") == original_body.encode("utf-8")
        if original_job is not None:
            copied_job = SqsJobMessage.model_validate_json(request["MessageBody"])
            assert copied_job == original_job
            assert copied_job.delivery_id == original_job.delivery_id

        # SQS round trip: the copy comes back as a fresh message.
        current = _delivered_copy(
            request,
            cycle=cycle,
            approximate_first_ms=now_ms + expected_delay * 1000 + delivery_lag_ms,
        )
        assert current.attributes.ApproximateReceiveCount == "1"

    later_ms = expected_first + wait_limit_seconds * 1000
    assert first_received_at_ms(current, now_ms=later_ms) == expected_first
    assert current.body == original_body


@given(
    first_ms=st.integers(min_value=1, max_value=_MAX_EPOCH_MS),
    delta_ms=st.integers(min_value=-10**9, max_value=10**9),
    retry_after_seconds=st.integers(min_value=1, max_value=5_000),
    concurrency_limit=st.integers(min_value=1, max_value=100),
    wait_limit_seconds=st.integers(min_value=60, max_value=86_400),
)
@settings(max_examples=200, deadline=None)
def test_property_decide_busy_follows_the_wait_limit(
    first_ms: int,
    delta_ms: int,
    retry_after_seconds: int,
    concurrency_limit: int,
    wait_limit_seconds: int,
) -> None:
    """Feature: sandbox-minute-plans, Property 32: Busy copies preserve the first receive time (decide_busy).

    A future first receive time counts as 0 waited; more than the limit times
    out with ``waited_seconds`` computed locally; otherwise the delay is
    ``min(retry_after_seconds, 900)`` and the first receive time is carried.

    **Validates: Requirements 11.18, 11.21, 16.15**
    """
    decision = decide_busy(
        first_received_at_ms=first_ms,
        now_ms=first_ms + delta_ms,
        retry_after_seconds=retry_after_seconds,
        concurrency_limit=concurrency_limit,
        wait_limit_seconds=wait_limit_seconds,
    )
    waited_ms = max(0, delta_ms)
    if waited_ms > wait_limit_seconds * 1000:
        assert decision == BusyTimedOut(
            concurrency_limit=concurrency_limit, waited_seconds=waited_ms // 1000
        )
    else:
        assert decision == BusyRequeue(
            delay_seconds=min(retry_after_seconds, MAX_SQS_DELAY_SECONDS),
            first_received_at_ms=first_ms,
        )


@given(
    first_ms=st.integers(min_value=1, max_value=_MAX_EPOCH_MS),
    retry_after_seconds=st.integers(min_value=1, max_value=MAX_SQS_DELAY_SECONDS),
    concurrency_limit=st.integers(min_value=1, max_value=100),
    wait_limit_seconds=st.integers(min_value=60, max_value=86_400),
)
@settings(max_examples=200, deadline=None)
def test_property_decide_busy_limit_boundary(
    first_ms: int,
    retry_after_seconds: int,
    concurrency_limit: int,
    wait_limit_seconds: int,
) -> None:
    """Feature: sandbox-minute-plans, Property 32: Busy copies preserve the first receive time (limit boundary).

    Exactly the limit re-queues; one millisecond more times out.

    **Validates: Requirements 11.18, 11.21, 16.15**
    """
    limit_ms = wait_limit_seconds * 1000
    kwargs = {
        "first_received_at_ms": first_ms,
        "retry_after_seconds": retry_after_seconds,
        "concurrency_limit": concurrency_limit,
        "wait_limit_seconds": wait_limit_seconds,
    }
    assert decide_busy(now_ms=first_ms + limit_ms, **kwargs) == BusyRequeue(
        delay_seconds=retry_after_seconds, first_received_at_ms=first_ms
    )
    assert decide_busy(now_ms=first_ms + limit_ms + 1, **kwargs) == BusyTimedOut(
        concurrency_limit=concurrency_limit, waited_seconds=wait_limit_seconds
    )


# ---------------------------------------------------------------------------
# decide_busy examples.
# ---------------------------------------------------------------------------

_NOW_MS = 1_700_000_000_000


@pytest.mark.parametrize("ahead_ms", [1, 60_001, 86_400_000 * 365])
def test_future_first_receive_time_counts_as_zero_waited(ahead_ms: int) -> None:
    """Clock skew: a first receive time in the future never times out."""
    first = _NOW_MS + ahead_ms
    decision = decide_busy(
        first_received_at_ms=first,
        now_ms=_NOW_MS,
        retry_after_seconds=60,
        concurrency_limit=1,
        wait_limit_seconds=60,
    )
    assert decision == BusyRequeue(delay_seconds=60, first_received_at_ms=first)


def test_decide_busy_at_exactly_the_limit_requeues_and_one_ms_more_times_out() -> None:
    first = _NOW_MS
    at_limit = decide_busy(
        first_received_at_ms=first,
        now_ms=first + 1_800_000,
        retry_after_seconds=60,
        concurrency_limit=1,
        wait_limit_seconds=1800,
    )
    assert at_limit == BusyRequeue(delay_seconds=60, first_received_at_ms=first)

    past_limit = decide_busy(
        first_received_at_ms=first,
        now_ms=first + 1_800_001,
        retry_after_seconds=60,
        concurrency_limit=1,
        wait_limit_seconds=1800,
    )
    assert past_limit == BusyTimedOut(concurrency_limit=1, waited_seconds=1800)


# ---------------------------------------------------------------------------
# Log capture (same pattern as test_never_fail_open.py).
# ---------------------------------------------------------------------------


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
    """Parse the captured Powertools JSON lines into a list of dicts."""
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _record(
    *,
    body: str = "body",
    approximate: str | None = None,
    carried: str | None = None,
) -> SqsRecord:
    attributes: dict[str, object] = {"ApproximateReceiveCount": "1"}
    if approximate is not None:
        attributes["ApproximateFirstReceiveTimestamp"] = approximate
    message_attributes: dict[str, object] = {}
    if carried is not None:
        message_attributes[FIRST_RECEIVED_AT_ATTRIBUTE] = _wire_attribute(
            "Number", carried
        )
    return SqsRecord.model_validate(
        {
            "messageId": "m-1",
            "receiptHandle": "rh-original",
            "body": body,
            "attributes": attributes,
            "messageAttributes": message_attributes,
        }
    )


def test_first_received_at_falls_back_to_now_with_a_warning(
    log_stream: io.StringIO,
) -> None:
    record = _record(approximate="0123", carried="17\n")
    assert first_received_at_ms(record, now_ms=_NOW_MS) == _NOW_MS

    records = _log_records(log_stream)
    fallback = [r for r in records if r["message"] == "first_received_at_fallback"]
    assert len(fallback) == 1
    assert fallback[0]["level"] == "WARNING"


def test_first_received_at_does_not_warn_when_a_timestamp_is_valid(
    log_stream: io.StringIO,
) -> None:
    assert first_received_at_ms(_record(approximate="1700000000123"), now_ms=1) == (
        1_700_000_000_123
    )
    assert first_received_at_ms(
        _record(approximate="1700000000123", carried="1690000000000"), now_ms=1
    ) == 1_690_000_000_000
    assert _log_records(log_stream) == []


# ---------------------------------------------------------------------------
# moto SQS.
# ---------------------------------------------------------------------------


@pytest.fixture
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so ``mock_aws`` activates cleanly (no real AWS)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.mark.usefixtures("_aws_credentials")
@mock_aws()
def test_moto_sqs_accepts_the_copy_and_reads_it_back() -> None:
    sqs = boto3.client("sqs", region_name="us-east-1")
    job = make_sqs_job_message()
    record = _record(body=job.model_dump_json(), approximate="1700000000123")
    first = first_received_at_ms(record, now_ms=_NOW_MS)
    assert first == 1_700_000_000_123

    # The maximum delay and the Number attribute are accepted.
    delayed_url = sqs.create_queue(QueueName="trikon-verify-jobs-delayed")["QueueUrl"]
    assert send_busy_copy(
        sqs,
        record,
        queue_url=delayed_url,
        decision=BusyRequeue(
            delay_seconds=MAX_SQS_DELAY_SECONDS, first_received_at_ms=first
        ),
    )

    # A copy with no delay can be received at once and reads back intact.
    url = sqs.create_queue(QueueName="trikon-verify-jobs")["QueueUrl"]
    assert send_busy_copy(
        sqs,
        record,
        queue_url=url,
        decision=BusyRequeue(delay_seconds=0, first_received_at_ms=first),
    )
    received = sqs.receive_message(
        QueueUrl=url,
        AttributeNames=["All"],
        MessageAttributeNames=["All"],
        MaxNumberOfMessages=10,
        WaitTimeSeconds=0,
    )["Messages"]
    assert len(received) == 1
    message = received[0]
    assert message["Body"] == record.body
    assert message["MessageAttributes"] == {
        FIRST_RECEIVED_AT_ATTRIBUTE: {"DataType": "Number", "StringValue": str(first)}
    }

    # The copy as the Lambda event source mapping would deliver it.
    delivered = SqsRecord.model_validate(
        {
            "messageId": message["MessageId"],
            "receiptHandle": message["ReceiptHandle"],
            "body": message["Body"],
            "attributes": message["Attributes"],
            "messageAttributes": {
                name: _wire_attribute(value["DataType"], value["StringValue"])
                for name, value in message["MessageAttributes"].items()
            },
            "md5OfBody": message["MD5OfBody"],
            "eventSource": "aws:sqs",
            "eventSourceARN": _EVENT_SOURCE_ARN,
            "awsRegion": "us-east-1",
        }
    )
    assert first_received_at_ms(delivered, now_ms=_NOW_MS + 10**9) == first
    assert SqsJobMessage.model_validate_json(delivered.body).delivery_id == job.delivery_id


# ---------------------------------------------------------------------------
# send_busy_copy against a stub client.
# ---------------------------------------------------------------------------


class _StubSqs:
    """Records the two SQS calls; raises the configured errors."""

    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        visibility_error: Exception | None = None,
    ) -> None:
        self.send_error = send_error
        self.visibility_error = visibility_error
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
        self.sent.append(
            {
                "QueueUrl": QueueUrl,
                "MessageBody": MessageBody,
                "DelaySeconds": DelaySeconds,
                "MessageAttributes": MessageAttributes,
            }
        )
        if self.send_error is not None:
            raise self.send_error
        return {"MessageId": "copy-1"}

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
        if self.visibility_error is not None:
            raise self.visibility_error
        return {}


_SECRET_BODY = "BODY-SENTINEL-do-not-log"


def test_send_busy_copy_success_sends_one_copy_and_no_visibility_change() -> None:
    record = _record(body=_SECRET_BODY)
    decision = BusyRequeue(delay_seconds=60, first_received_at_ms=_NOW_MS)
    stub = _StubSqs()
    assert send_busy_copy(stub, record, queue_url=_QUEUE_URL, decision=decision) is True
    assert stub.sent == [
        dict(build_requeue_request(record, queue_url=_QUEUE_URL, decision=decision))
    ]
    assert stub.visibility_changes == []


@pytest.mark.parametrize(
    "send_error",
    [
        ClientError(
            {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue", "Message": "x"}},
            "SendMessage",
        ),
        EndpointConnectionError(endpoint_url="https://sqs.us-east-1.amazonaws.com/"),
    ],
    ids=["ClientError", "EndpointConnectionError"],
)
@pytest.mark.parametrize("visibility_fails", [False, True])
def test_send_busy_copy_failure_holds_back_the_original_and_returns_false(
    send_error: Exception,
    visibility_fails: bool,
    log_stream: io.StringIO,
) -> None:
    record = _record(body=_SECRET_BODY)
    decision = BusyRequeue(delay_seconds=120, first_received_at_ms=_NOW_MS)
    stub = _StubSqs(
        send_error=send_error,
        visibility_error=(
            ClientError(
                {"Error": {"Code": "ReceiptHandleIsInvalid", "Message": "x"}},
                "ChangeMessageVisibility",
            )
            if visibility_fails
            else None
        ),
    )

    assert send_busy_copy(stub, record, queue_url=_QUEUE_URL, decision=decision) is False

    assert len(stub.sent) == 1
    assert stub.visibility_changes == [
        {
            "QueueUrl": _QUEUE_URL,
            "ReceiptHandle": "rh-original",
            "VisibilityTimeout": 120,
        }
    ]
    records = _log_records(log_stream)
    failed = [r for r in records if r["message"] == "busy_requeue_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "WARNING"
    assert failed[0]["error_class"] == type(send_error).__name__
    assert failed[0]["delay_seconds"] == 120
    assert _SECRET_BODY not in log_stream.getvalue()
