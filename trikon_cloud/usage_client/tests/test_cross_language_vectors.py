"""Cross-language metering and signature vectors (sandbox-minute-plans §9-§10, task 29.1).

These are the only checks that the Python Usage_Client and Trikon_Web agree
on the wire. Trikon_Web's ``src/usage/crossLanguageVectors.test.ts``
(task 29.2) pins the same eight metering vectors through ``meteredMinutes``
and the same secret, timestamp, body, and hex through
``computeUsageSignature``. Change a value here only together with that file.

The test secret is deliberately not secret-shaped: plain ASCII words, no
provider prefix, and never used outside tests.

No network, no AWS.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from trikon_cloud.usage_client.metering import (
    PER_RUN_CAP_MINUTES,
    billed_minutes,
    meter_stopped_task,
)
from trikon_cloud.usage_client.models import QuotaRequest
from trikon_cloud.usage_client.signing import encode_body, sign

# ---------------------------------------------------------------------------
# Metering vectors (Req 12.13)
# ---------------------------------------------------------------------------

#: (duration in ms, billed minutes). Shared verbatim with Trikon_Web.
METERING_VECTORS: tuple[tuple[int, int], ...] = (
    (-5_000, 1),
    (0, 1),
    (1, 1),
    (60_000, 1),
    (60_001, 2),
    (599_999, 10),
    (600_000, 10),
    (3_600_000, 10),
)


def test_there_are_exactly_eight_metering_vectors() -> None:
    assert len(METERING_VECTORS) == 8


@pytest.mark.parametrize(("delta_ms", "expected"), METERING_VECTORS)
def test_metering_vector_with_meter_stopped_task_clamp(delta_ms: int, expected: int) -> None:
    # meter_stopped_task clamps a stop before the start to the start.
    assert billed_minutes(0, max(0, delta_ms)) == expected


@pytest.mark.parametrize(("delta_ms", "expected"), METERING_VECTORS)
def test_metering_vector_unclamped(delta_ms: int, expected: int) -> None:
    # billed_minutes clamps a negative duration to zero on its own as well.
    assert billed_minutes(0, delta_ms) == expected


def test_negative_vector_through_meter_stopped_task() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metered = meter_stopped_task(
        created_at=start,
        pull_started_at=None,
        stopped_at=start - timedelta(milliseconds=5_000),
        event_time=start,
        stop_code=None,
    )
    assert metered.billed_minutes == 1
    assert metered.started_at == metered.stopped_at == "2026-01-01T00:00:00.000Z"


def test_cap_matches_largest_vector() -> None:
    assert METERING_VECTORS[-1][1] == PER_RUN_CAP_MINUTES


# ---------------------------------------------------------------------------
# Signature vector (Req 11.2)
# ---------------------------------------------------------------------------

#: Test-only Usage_Secret (38 ASCII characters). Trikon_Web uses the same string.
VECTOR_SECRET = "trikon-cross-language-test-vector-0001"

#: Request_Timestamp: 2026-01-01T00:00:00Z in whole Unix seconds.
VECTOR_TIMESTAMP = 1767225600

#: The exact Quota_Request body bytes that are signed and sent.
VECTOR_BODY = (
    b'{"installation_id":42,"pr_number":7,"repository_full_name":"acme/widgets",'
    b'"request_version":1,"run_id":"delivery-0001"}'
)

#: Computed once with ``sign`` and pinned. Trikon_Web pins the same hex.
VECTOR_SIGNATURE = "d32cac227ef868f942a5c036f421b7ceea563fd5f196dcdafbb5fff341fb6330"


def test_vector_secret_is_long_ascii() -> None:
    assert VECTOR_SECRET.isascii()
    assert len(VECTOR_SECRET) >= 32


def test_vector_body_bytes() -> None:
    body = encode_body(
        QuotaRequest(
            installation_id=42,
            run_id="delivery-0001",
            repository_full_name="acme/widgets",
            pr_number=7,
        )
    )
    assert body == VECTOR_BODY


def test_vector_signature_is_pinned() -> None:
    signature = sign(VECTOR_SECRET.encode("utf-8"), VECTOR_TIMESTAMP, VECTOR_BODY)
    assert signature == VECTOR_SIGNATURE


def test_vector_signature_matches_independent_oracle() -> None:
    oracle = hmac.new(
        VECTOR_SECRET.encode("utf-8"),
        b"1767225600." + VECTOR_BODY,
        hashlib.sha256,
    ).hexdigest()
    assert oracle == VECTOR_SIGNATURE
