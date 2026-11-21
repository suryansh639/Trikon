"""Dedupe claim store for the Spot_Retry_Handler (cost-cuts D3, Req 9.3, 11.1).

Table ``trikon-cloud-spot-retries`` (partition key ``task_arn``,
PAY_PER_REQUEST, TTL attribute ``expires_at``) holds one claim per
interrupted Sandbox_Task:

=================  ====  ==============================================
Attribute          Type  Meaning
=================  ====  ==============================================
``task_arn``       S     Interrupted task ARN (key)
``run_id``         S     Run_Id, for operators (omitted when unknown)
``status``         S     ``claimed``, ``sent`` or ``failed_closed``
``claimed_at_ms``  N     Claim time (Unix ms)
``expires_at``     N     Unix seconds = claim time + 7 days (TTL)
=================  ====  ==============================================

Only the invocation whose conditional ``PutItem`` creates the claim may send
a Retry_Job. Every later delivery reads the existing record and
:func:`resolve_claim` turns it into one of ``duplicate``, ``in_flight`` or
``proceed_fail_closed``, so at most one Retry_Job is ever sent per task.

No method logs; the handler logs fixed event names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

# Ordering walks the types, the pure resolver, then the store. Not isort alphabetical.
__all__ = [  # noqa: RUF022
    "ClaimStatus",
    "ClaimRecord",
    "ClaimResolution",
    "CLAIM_STALE_MS",
    "CLAIM_TTL_SECONDS",
    "resolve_claim",
    "SpotRetryClaims",
]

ClaimStatus = Literal["claimed", "sent", "failed_closed"]

ClaimResolution = Literal["proceed", "proceed_fail_closed", "duplicate", "in_flight"]

#: A ``claimed`` record younger than this is held by a live invocation:
#: the Lambda timeout (90 s) plus 10 s.
CLAIM_STALE_MS: Final = 100_000

#: Claim lifetime before DynamoDB TTL may delete it: 7 days.
CLAIM_TTL_SECONDS: Final = 7 * 86_400

_CLAIM_CONDITION: Final = "attribute_not_exists(task_arn)"
_CONDITIONAL_CHECK_FAILED: Final = "ConditionalCheckFailedException"
_STATUSES: Final[frozenset[str]] = frozenset({"claimed", "sent", "failed_closed"})


@dataclass(frozen=True)
class ClaimRecord:
    """The part of an existing claim that :func:`resolve_claim` reads."""

    status: ClaimStatus
    claimed_at_ms: int


#: The record an unreadable stored claim resolves as: a stale ``claimed``
#: claim, so the handler fails closed and never sends.
_UNREADABLE_CLAIM: Final = ClaimRecord(status="claimed", claimed_at_ms=0)


def resolve_claim(existing: ClaimRecord | None, *, now_ms: int) -> ClaimResolution:
    """Turn the outcome of :meth:`SpotRetryClaims.claim` into the handler's next step.

    * ``proceed`` -- this invocation created the claim; it may send.
    * ``duplicate`` -- an earlier invocation already sent or failed closed.
    * ``in_flight`` -- a live invocation holds the claim; retry later.
    * ``proceed_fail_closed`` -- the claimant died before confirming; fail
      closed without sending, because the Retry_Job may already be queued.
    """
    if existing is None:
        return "proceed"
    if existing.status in ("sent", "failed_closed"):
        return "duplicate"
    if now_ms - existing.claimed_at_ms < CLAIM_STALE_MS:
        return "in_flight"
    return "proceed_fail_closed"


class _ClaimsDynamoDbProtocol(Protocol):
    """Structural type for the three boto3 DynamoDB calls the store makes."""

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Item: dict[str, dict[str, str]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object: ...

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Key: dict[str, dict[str, str]],  # noqa: N803
        ConsistentRead: bool,  # noqa: N803
    ) -> Mapping[str, object]: ...

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Key: dict[str, dict[str, str]],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeNames: dict[str, str],  # noqa: N803
        ExpressionAttributeValues: dict[str, dict[str, str]],  # noqa: N803
    ) -> object: ...


def _client_error_code(exc: ClientError) -> str:
    """Return ``Error.Code`` of a botocore ``ClientError``, or ``""``."""
    response = cast(object, exc.response)
    if not isinstance(response, Mapping):
        return ""
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _parse_record(item: object) -> ClaimRecord:
    """Read ``status`` and ``claimed_at_ms`` from a GetItem ``Item``.

    A missing item or an unreadable attribute resolves as
    :data:`_UNREADABLE_CLAIM` (stale ``claimed``), which fails closed.
    """
    if not isinstance(item, Mapping):
        return _UNREADABLE_CLAIM
    status_attr = item.get("status")
    claimed_attr = item.get("claimed_at_ms")
    if not isinstance(status_attr, Mapping) or not isinstance(claimed_attr, Mapping):
        return _UNREADABLE_CLAIM
    status = status_attr.get("S")
    raw_claimed = claimed_attr.get("N")
    if not isinstance(status, str) or status not in _STATUSES:
        return _UNREADABLE_CLAIM
    if not isinstance(raw_claimed, str):
        return _UNREADABLE_CLAIM
    try:
        claimed_at_ms = int(raw_claimed)
    except ValueError:
        return _UNREADABLE_CLAIM
    return ClaimRecord(status=cast(ClaimStatus, status), claimed_at_ms=claimed_at_ms)


class SpotRetryClaims:
    """Conditional claim store on ``trikon-cloud-spot-retries``."""

    def __init__(self, *, table_name: str, ddb_client: _ClaimsDynamoDbProtocol) -> None:
        self._table_name = table_name
        self._ddb = ddb_client

    def claim(self, *, task_arn: str, run_id: str | None, now_ms: int) -> ClaimRecord | None:
        """Create the claim for ``task_arn``, or return the claim that already exists.

        ``PutItem`` with ``ConditionExpression="attribute_not_exists(task_arn)"``.
        Returns ``None`` when this call created the claim. On
        ``ConditionalCheckFailedException`` returns the existing record,
        read with ``GetItem(ConsistentRead=True)``; an unreadable or
        vanished record is returned as a stale ``claimed`` claim. Any other
        ``ClientError`` propagates.
        """
        item: dict[str, dict[str, str]] = {
            "task_arn": {"S": task_arn},
            "status": {"S": "claimed"},
            "claimed_at_ms": {"N": str(now_ms)},
            "expires_at": {"N": str(now_ms // 1_000 + CLAIM_TTL_SECONDS)},
        }
        if run_id is not None:
            item["run_id"] = {"S": run_id}
        try:
            self._ddb.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=_CLAIM_CONDITION,
            )
        except ClientError as exc:
            if _client_error_code(exc) != _CONDITIONAL_CHECK_FAILED:
                raise
        else:
            return None
        response = self._ddb.get_item(
            TableName=self._table_name,
            Key={"task_arn": {"S": task_arn}},
            ConsistentRead=True,
        )
        return _parse_record(response.get("Item"))

    def mark(self, *, task_arn: str, status: Literal["sent", "failed_closed"]) -> None:
        """Record the claim's outcome with an unconditional ``UpdateItem SET #s = :s``.

        Errors propagate; the handler's best-effort wrapper decides what to
        do with them.
        """
        self._ddb.update_item(
            TableName=self._table_name,
            Key={"task_arn": {"S": task_arn}},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": status}},
        )
