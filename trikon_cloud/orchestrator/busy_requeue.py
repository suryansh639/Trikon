"""Busy re-queue for the Trikon Cloud orchestrator (sandbox-minute-plans).

When the Quota_Endpoint answers ``busy`` (the Account's Concurrency_Limit is
reached), the orchestrator sends a delayed copy of the Job back to
``trikon-verify-jobs`` and lets SQS delete the original. Each copy is a new
message with receive count 1, so a waiting Job never reaches the DLQ under the
queue's ``maxReceiveCount = 3`` (Req 11.18, 11.19). The first receive time
travels with the copy in the ``trikon_first_received_at`` message attribute,
so the wait is measured from the first time the orchestrator saw the Job
(Req 11.21). See design.md, "Busy Jobs".

Three layers:

* the pure wait decision: :func:`first_received_at_ms`, :func:`decide_busy`,
  :class:`BusyRequeue` and :class:`BusyTimedOut`;
* the pure SQS copy request: :func:`build_requeue_request`;
* the thin sender: :func:`send_busy_copy`, one ``sqs:SendMessage`` with a
  best-effort ``ChangeMessageVisibility`` fallback.

Nothing in this module logs the message body (Req 11.17, design §7.4).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Protocol, TypedDict

from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)

from trikon_cloud.orchestrator.logger import get_logger
from trikon_cloud.orchestrator.models import SqsRecord

__all__ = [  # noqa: RUF022
    "FIRST_RECEIVED_AT_ATTRIBUTE",
    "MAX_SQS_DELAY_SECONDS",
    "BusyRequeue",
    "BusyTimedOut",
    "RequeueRequest",
    "first_received_at_ms",
    "decide_busy",
    "build_requeue_request",
    "send_busy_copy",
]

#: The ``Number`` message attribute carrying the Job's first receive time, in
#: Unix epoch milliseconds (Req 11.21).
FIRST_RECEIVED_AT_ATTRIBUTE = "trikon_first_received_at"

#: The SQS ``DelaySeconds`` maximum.
MAX_SQS_DELAY_SECONDS = 900

# A positive Unix-ms timestamp: no sign, no leading zero, at most 16 digits.
# ``[0-9]`` (not ``\d``) keeps it to ASCII digits; ``fullmatch`` rejects a
# trailing newline that ``$`` would accept.
_EPOCH_MS_PATTERN = re.compile(r"[1-9][0-9]{0,15}")

_NUMBER_DATA_TYPE = "Number"


@dataclass(frozen=True, slots=True)
class BusyRequeue:
    """Send a delayed copy of the Job (Req 11.18).

    ``delay_seconds`` is the copy's ``DelaySeconds``; ``first_received_at_ms``
    is carried unchanged into the copy's ``trikon_first_received_at``
    attribute (Req 11.21).
    """

    delay_seconds: int
    first_received_at_ms: int


@dataclass(frozen=True, slots=True)
class BusyTimedOut:
    """The Job waited longer than the Queue_Wait_Limit (Req 11.20).

    ``waited_seconds`` is computed locally from the first receive time, not
    taken from the Quota_Endpoint response.
    """

    concurrency_limit: int
    waited_seconds: int


def _epoch_ms(value: str | None) -> int | None:
    """Return ``value`` as an int when it is a well-formed Unix-ms string."""
    if value is None or _EPOCH_MS_PATTERN.fullmatch(value) is None:
        return None
    return int(value)


def first_received_at_ms(record: SqsRecord, *, now_ms: int) -> int:
    """The first time the orchestrator received this Job, carried across busy copies.

    1. ``messageAttributes["trikon_first_received_at"]`` with ``dataType``
       ``"Number"`` and a value matching ``^[1-9][0-9]{0,15}$`` (a copy
       written by the busy path);
    2. else ``attributes.ApproximateFirstReceiveTimestamp`` matching the same
       pattern (the original message from the webhook receiver);
    3. else ``now_ms``, logged WARNING ``first_received_at_fallback``.
    """
    attribute = record.messageAttributes.get(FIRST_RECEIVED_AT_ATTRIBUTE)
    if attribute is not None and attribute.dataType == _NUMBER_DATA_TYPE:
        carried = _epoch_ms(attribute.stringValue)
        if carried is not None:
            return carried
    approximate = _epoch_ms(record.attributes.ApproximateFirstReceiveTimestamp)
    if approximate is not None:
        return approximate
    get_logger().warning("first_received_at_fallback")
    return now_ms


def decide_busy(
    *,
    first_received_at_ms: int,
    now_ms: int,
    retry_after_seconds: int,
    concurrency_limit: int,
    wait_limit_seconds: int,
) -> BusyRequeue | BusyTimedOut:
    """Re-queue the Job, or time it out once it has waited too long.

    A first receive time in the future (clock skew) counts as 0 waited. A
    wait of exactly ``wait_limit_seconds`` still re-queues; one millisecond
    more times out ("longer than", Req 11.20). The delay is
    ``retry_after_seconds`` capped at the SQS maximum (Req 11.18).
    """
    waited_ms = max(0, now_ms - first_received_at_ms)
    if waited_ms > wait_limit_seconds * 1000:
        return BusyTimedOut(
            concurrency_limit=concurrency_limit,
            waited_seconds=waited_ms // 1000,
        )
    return BusyRequeue(
        delay_seconds=min(retry_after_seconds, MAX_SQS_DELAY_SECONDS),
        first_received_at_ms=first_received_at_ms,
    )


class _MessageAttributeValue(TypedDict):
    DataType: str
    StringValue: str


class RequeueRequest(TypedDict):
    """The ``sqs.send_message`` kwargs built by :func:`build_requeue_request`.

    A :class:`TypedDict` (a plain ``dict`` at runtime) so that
    :func:`send_busy_copy` can unpack it into the typed client protocol.
    """

    QueueUrl: str
    MessageBody: str
    DelaySeconds: int
    MessageAttributes: dict[str, _MessageAttributeValue]


def build_requeue_request(
    record: SqsRecord, *, queue_url: str, decision: BusyRequeue
) -> RequeueRequest:
    """The ``sqs.send_message`` kwargs for the Job's delayed copy.

    ``MessageBody`` is the original body byte for byte, so the copy is the
    same :class:`SqsJobMessage` with the same ``delivery_id`` and Run_Id. The
    only message attribute is the first receive time (Req 11.21).
    """
    return {
        "QueueUrl": queue_url,
        "MessageBody": record.body,
        "DelaySeconds": decision.delay_seconds,
        "MessageAttributes": {
            FIRST_RECEIVED_AT_ATTRIBUTE: {
                "DataType": _NUMBER_DATA_TYPE,
                "StringValue": str(decision.first_received_at_ms),
            }
        },
    }


class _SqsClientProtocol(Protocol):
    """Structural type for the two boto3 SQS calls the busy path makes.

    ``boto3`` ships without type stubs; this :class:`typing.Protocol` keeps
    ``mypy --strict`` clean without ``boto3-stubs``, matching the client
    protocols in :mod:`trikon_cloud.orchestrator.never_fail_open`.
    """

    def send_message(
        self,
        *,
        QueueUrl: str,  # noqa: N803 -- AWS boto3 API contract
        MessageBody: str,  # noqa: N803
        DelaySeconds: int,  # noqa: N803
        MessageAttributes: dict[str, _MessageAttributeValue],  # noqa: N803
    ) -> object: ...

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,  # noqa: N803 -- AWS boto3 API contract
        ReceiptHandle: str,  # noqa: N803
        VisibilityTimeout: int,  # noqa: N803
    ) -> object: ...


def send_busy_copy(
    sqs_client: _SqsClientProtocol,
    record: SqsRecord,
    *,
    queue_url: str,
    decision: BusyRequeue,
) -> bool:
    """Send the Job's delayed copy; return whether SQS accepted it.

    On success the caller returns no batch item failures, so SQS deletes the
    original. On a :class:`ClientError` or :class:`BotoCoreError` this logs
    WARNING ``busy_requeue_failed``, makes a best-effort
    ``ChangeMessageVisibility`` on the original with the same delay (its own
    errors are swallowed), and returns ``False``; the caller then lists the
    record as a batch item failure so the original stays in the queue.
    """
    request = build_requeue_request(record, queue_url=queue_url, decision=decision)
    try:
        sqs_client.send_message(**request)
    except (ClientError, BotoCoreError) as exc:
        get_logger().warning(
            "busy_requeue_failed",
            error_class=type(exc).__name__,
            delay_seconds=decision.delay_seconds,
        )
        # Best effort: hold the original back for the same delay so it is not
        # redelivered at once. Its own failure changes nothing for the caller.
        with contextlib.suppress(ClientError, BotoCoreError):
            sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=record.receiptHandle,
                VisibilityTimeout=decision.delay_seconds,
            )
        return False
    return True
