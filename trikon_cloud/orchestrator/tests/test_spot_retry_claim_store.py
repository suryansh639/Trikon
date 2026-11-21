"""Examples for the Spot_Retry_Handler claim store (cost-cuts task 14.1, D3).

:class:`SpotRetryClaims` runs against moto DynamoDB; :func:`resolve_claim`
is pure. Property 10 (at most one Retry_Job per task) lives in
``test_spot_retry_claims.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, Literal

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.orchestrator.spot_retry_claims import (
    CLAIM_STALE_MS,
    CLAIM_TTL_SECONDS,
    ClaimRecord,
    ClaimResolution,
    SpotRetryClaims,
    resolve_claim,
)

_TABLE: Final = "trikon-cloud-spot-retries"
_TASK_ARN: Final = "arn:aws:ecs:us-east-1:123456789012:task/trikon-verify-cluster/0123abcd"
_RUN_ID: Final = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
_T0: Final = 1_714_557_750_123


@pytest.fixture
def ddb(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A moto DynamoDB client; the claims table is created by :func:`_create_table`."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


def _create_table() -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName=_TABLE,
        KeySchema=[{"AttributeName": "task_arn", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "task_arn", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _claims(ddb: object) -> SpotRetryClaims:
    return SpotRetryClaims(table_name=_TABLE, ddb_client=ddb)  # type: ignore[arg-type]


def _stored(task_arn: str = _TASK_ARN) -> dict[str, dict[str, str]]:
    response = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName=_TABLE, Key={"task_arn": {"S": task_arn}}, ConsistentRead=True
    )
    item: dict[str, dict[str, str]] = response["Item"]
    return item


# ---------------------------------------------------------------------------
# SpotRetryClaims.claim / mark (moto).
# ---------------------------------------------------------------------------


def test_first_claim_creates_the_record(ddb: object) -> None:
    _create_table()

    assert _claims(ddb).claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0) is None

    assert _stored() == {
        "task_arn": {"S": _TASK_ARN},
        "run_id": {"S": _RUN_ID},
        "status": {"S": "claimed"},
        "claimed_at_ms": {"N": str(_T0)},
        "expires_at": {"N": str(_T0 // 1_000 + CLAIM_TTL_SECONDS)},
    }


def test_claim_without_run_id_omits_the_attribute(ddb: object) -> None:
    _create_table()
    assert _claims(ddb).claim(task_arn=_TASK_ARN, run_id=None, now_ms=_T0) is None
    assert "run_id" not in _stored()


def test_second_claim_returns_the_existing_record_unchanged(ddb: object) -> None:
    _create_table()
    claims = _claims(ddb)
    claims.claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0)

    existing = claims.claim(task_arn=_TASK_ARN, run_id="other", now_ms=_T0 + 5_000)

    assert existing == ClaimRecord(status="claimed", claimed_at_ms=_T0)
    assert _stored()["run_id"] == {"S": _RUN_ID}
    assert _stored()["claimed_at_ms"] == {"N": str(_T0)}


@pytest.mark.parametrize("status", ["sent", "failed_closed"])
def test_mark_records_the_outcome_seen_by_later_claims(
    ddb: object, status: Literal["sent", "failed_closed"]
) -> None:
    _create_table()
    claims = _claims(ddb)
    claims.claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0)

    claims.mark(task_arn=_TASK_ARN, status=status)

    assert _stored()["status"] == {"S": status}
    assert claims.claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0 + 1) == ClaimRecord(
        status=status, claimed_at_ms=_T0
    )


def test_claims_are_per_task(ddb: object) -> None:
    _create_table()
    claims = _claims(ddb)
    assert claims.claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0) is None
    assert claims.claim(task_arn=_TASK_ARN + "x", run_id=_RUN_ID, now_ms=_T0) is None


def test_unreadable_existing_record_resolves_as_stale_claim(ddb: object) -> None:
    """A record the store cannot read fails closed, never sends."""
    _create_table()
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName=_TABLE,
        Item={"task_arn": {"S": _TASK_ARN}, "status": {"S": "bogus"}},
    )

    existing = _claims(ddb).claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0)

    assert existing == ClaimRecord(status="claimed", claimed_at_ms=0)
    assert resolve_claim(existing, now_ms=_T0) == "proceed_fail_closed"


def test_non_conditional_dynamodb_error_propagates(ddb: object) -> None:
    """No table: ``ResourceNotFoundException`` reaches the caller."""
    with pytest.raises(ClientError) as excinfo:
        _claims(ddb).claim(task_arn=_TASK_ARN, run_id=_RUN_ID, now_ms=_T0)
    assert excinfo.value.response["Error"]["Code"] == "ResourceNotFoundException"


def test_mark_error_propagates(ddb: object) -> None:
    with pytest.raises(ClientError):
        _claims(ddb).mark(task_arn=_TASK_ARN, status="sent")


# ---------------------------------------------------------------------------
# resolve_claim (pure).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "now_ms", "expected"),
    [
        (None, _T0, "proceed"),
        (ClaimRecord("sent", _T0), _T0 + 10 * CLAIM_STALE_MS, "duplicate"),
        (ClaimRecord("failed_closed", _T0), _T0, "duplicate"),
        (ClaimRecord("claimed", _T0), _T0, "in_flight"),
        (ClaimRecord("claimed", _T0), _T0 + CLAIM_STALE_MS - 1, "in_flight"),
        (ClaimRecord("claimed", _T0), _T0 + CLAIM_STALE_MS, "proceed_fail_closed"),
        (ClaimRecord("claimed", 0), _T0, "proceed_fail_closed"),
    ],
)
def test_resolve_claim_table(
    existing: ClaimRecord | None, now_ms: int, expected: ClaimResolution
) -> None:
    assert resolve_claim(existing, now_ms=now_ms) == expected


def test_claim_stale_window_is_lambda_timeout_plus_ten_seconds() -> None:
    assert CLAIM_STALE_MS == 100_000
