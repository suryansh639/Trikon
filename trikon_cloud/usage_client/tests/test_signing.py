# The httpx compat shim forwards ``httpx.Request.__init__``'s untyped
# ``*args``/``**kwargs``, which needs ``Any`` under ``disallow_any_explicit``.
# The ``Any`` stays inside that test-only fixture.
# mypy: disable-error-code="explicit-any"
"""Property tests for Usage_Signature construction (sandbox-minute-plans §9, Property 26).

Feature: sandbox-minute-plans, Property 26: Signature construction.

For any secret bytes, timestamp, and body, :func:`sign` returns 64 lowercase
hex characters equal to an independent ``hmac.new(...).hexdigest()`` oracle,
and a request that respx records from :class:`UsageApiClient` carries exactly
the bytes :func:`encode_body` produced and the headers :func:`signed_headers`
computed for them. Each retry attempt re-signs with a fresh timestamp.

The pinned cross-language vector lives in ``test_cross_language_vectors.py``
(task 29.1), not here.

Library-compat shim and fixture choice:

* :func:`_httpx_respx_method_compat` is the same autouse shim the orchestrator
  and fargate_runner suites use: httpx 0.28 keeps a bytes ``method`` and
  respx 0.21's ``Method eq 'POST'`` matcher then never resolves. It is an
  autouse fixture on purpose. Hypothesis exempts autouse fixtures from the
  ``function_scoped_fixture`` health check, and the patch is stateless, so
  applying it once per test function (rather than once per example) is safe.
* respx is entered per example with ``with respx.mock(...)``, so every
  example starts with a fresh router and its own recorded calls.

No network, no AWS. The test secrets are deliberately not secret-shaped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.usage_client.client import QUOTA_PATH, USAGE_PATH, UsageApiClient
from trikon_cloud.usage_client.metering import billed_minutes, format_utc_ms
from trikon_cloud.usage_client.models import (
    INSTALLATION_ID_MAX,
    PR_NUMBER_MAX,
    QuotaAllowed,
    QuotaRequest,
    UsageDelivered,
    UsageReport,
)
from trikon_cloud.usage_client.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    encode_body,
    sign,
    signed_headers,
)

_BASE_URL = "https://trikon-web.test"
_QUOTA_URL = _BASE_URL + QUOTA_PATH
_USAGE_URL = _BASE_URL + USAGE_PATH

# ---------------------------------------------------------------------------
# Autouse compat fixture.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _httpx_respx_method_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28's bytes-vs-str method mismatch.

    Patches :meth:`httpx.Request.__init__` to decode a bytes method to str
    before delegating. Test-only; mirrors
    :mod:`trikon_cloud.orchestrator.tests.test_never_fail_open`.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)


@pytest.fixture(scope="module")
def http_client() -> Iterator[httpx.Client]:
    """One shared client: building its SSL context per example costs ~0.5 s on Windows.

    Module scope keeps Hypothesis's function-scoped-fixture health check quiet.
    The client holds no per-request state that matters here; respx is entered
    per example and intercepts its transport, so nothing reaches the network.
    """
    with httpx.Client() as client:
        yield client


# ---------------------------------------------------------------------------
# Oracle and test doubles.
# ---------------------------------------------------------------------------


def _oracle(secret: bytes, timestamp: str, body: bytes) -> str:
    """Independent HMAC-SHA256 of ``timestamp + "." + body`` (Req 11.2)."""
    return hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def _is_lower_hex_64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class _StubSecret:
    """Stands in for :class:`UsageSecretReader`."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def secret_bytes(self) -> bytes:
        return self._secret


class _FakeClock:
    """Unix wall-time seconds; :meth:`sleep` advances the clock by the delay."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _no_jitter(delay_seconds: float) -> float:
    return delay_seconds


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# Any key bytes, including empty and longer than the SHA-256 block size.
_SECRETS = st.binary(min_size=0, max_size=96)
# The UTF-8 text secrets Trikon_Web actually uses: printable ASCII, not secret-shaped.
_TEXT_SECRETS = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=64
).map(lambda s: ("usage-test-" + s).encode("utf-8"))
# Whole Unix seconds, up to the 15 digits Trikon_Web's timestamp regex allows.
_TIMESTAMPS = st.integers(min_value=0, max_value=10**15 - 1)
# Realistic wall-clock seconds for the client tests (2001 to 2096).
_WALL_SECONDS = st.integers(min_value=1_000_000_000, max_value=4_000_000_000)

_RUN_IDS = st.text(alphabet=_ALNUM + "_-", min_size=1, max_size=128)
_RESERVATION_IDS = st.uuids().flatmap(lambda u: st.sampled_from([str(u), str(u).upper()]))


@st.composite
def _quota_requests(draw: st.DrawFn) -> QuotaRequest:
    owner = draw(st.text(alphabet=_ALNUM + "-", min_size=1, max_size=39))
    repo = draw(st.text(alphabet=_ALNUM + "._-", min_size=1, max_size=100))
    return QuotaRequest(
        installation_id=draw(st.integers(min_value=1, max_value=INSTALLATION_ID_MAX)),
        run_id=draw(_RUN_IDS),
        repository_full_name=f"{owner}/{repo}",
        pr_number=draw(st.integers(min_value=1, max_value=PR_NUMBER_MAX)),
    )


@st.composite
def _usage_reports(draw: st.DrawFn) -> UsageReport:
    run_id = draw(_RUN_IDS)
    reservation_id = draw(_RESERVATION_IDS)
    if draw(st.booleans()):
        # A task that never started: no timestamps, nothing billed (Req 9.2).
        return UsageReport(
            run_id=run_id,
            reservation_id=reservation_id,
            billed_minutes=0,
            started_at=None,
            stopped_at=None,
        )
    # 2000-01-01 to 2100-01-01 in epoch ms; runs of up to two hours.
    start_ms = draw(st.integers(min_value=946_684_800_000, max_value=4_102_444_800_000))
    stop_ms = start_ms + draw(st.integers(min_value=0, max_value=2 * 3_600_000))
    billed = draw(st.sampled_from([0, billed_minutes(start_ms, stop_ms)]))
    return UsageReport(
        run_id=run_id,
        reservation_id=reservation_id,
        billed_minutes=billed,
        started_at=format_utc_ms(start_ms),
        stopped_at=format_utc_ms(stop_ms),
    )


_PAYLOADS: st.SearchStrategy[QuotaRequest | UsageReport] = st.one_of(
    _quota_requests(), _usage_reports()
)


# ---------------------------------------------------------------------------
# Helpers for the client properties.
# ---------------------------------------------------------------------------


def _success_response(payload: QuotaRequest | UsageReport) -> httpx.Response:
    if isinstance(payload, QuotaRequest):
        return httpx.Response(
            200,
            json={"outcome": "allowed", "reservation_id": "00000000-0000-4000-8000-000000000001"},
        )
    return httpx.Response(
        201,
        json={
            "outcome": "recorded",
            "reservation_id": payload.reservation_id,
            "billed_minutes": payload.billed_minutes,
        },
    )


def _send(client: UsageApiClient, payload: QuotaRequest | UsageReport) -> None:
    """Call the matching endpoint and assert it took the success path.

    ``check_quota`` swallows every exception, so asserting the decision is what
    proves the request was actually sent and answered.
    """
    if isinstance(payload, QuotaRequest):
        decision = client.check_quota(payload, budget_seconds=10.0)
        assert decision == QuotaAllowed(reservation_id="00000000-0000-4000-8000-000000000001")
    else:
        assert client.report_usage(payload) == UsageDelivered(outcome="recorded")


def _assert_signed(request: httpx.Request, secret: bytes, body: bytes, timestamp: int) -> None:
    """The recorded request carries ``body`` and the ``signed_headers`` for it."""
    assert request.method == "POST"
    assert request.content == body
    for name, value in signed_headers(secret, body, timestamp).items():
        assert request.headers[name] == value
    # And independently of signed_headers: the header pair verifies.
    ts_header = request.headers[TIMESTAMP_HEADER]
    assert ts_header == str(timestamp)
    assert request.headers[SIGNATURE_HEADER] == _oracle(secret, ts_header, request.content)


# ---------------------------------------------------------------------------
# Property 26: pure construction.
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    secret=st.one_of(_SECRETS, _TEXT_SECRETS), timestamp=_TIMESTAMPS, body=st.binary(max_size=512)
)
def test_sign_matches_independent_hmac(secret: bytes, timestamp: int, body: bytes) -> None:
    """Feature: sandbox-minute-plans, Property 26: Signature construction.

    **Validates: Requirements 11.2, 16.12**
    """
    signature = sign(secret, timestamp, body)
    assert _is_lower_hex_64(signature)
    assert signature == _oracle(secret, str(timestamp), body)

    headers = signed_headers(secret, body, timestamp)
    assert headers == {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: signature,
    }


@settings(max_examples=200)
@given(payload=_PAYLOADS)
def test_encode_body_is_canonical(payload: QuotaRequest | UsageReport) -> None:
    """Feature: sandbox-minute-plans, Property 26: Signature construction.

    **Validates: Requirements 11.2, 16.12**
    """
    body = encode_body(payload)
    # Pure ASCII, so the signed bytes and the wire bytes cannot differ by encoding.
    text = body.decode("ascii")
    # Round-trips to the model's JSON form and back to an equal model.
    assert json.loads(body) == payload.model_dump(mode="json")
    assert type(payload).model_validate_json(body) == payload
    # Keys are sorted.
    pairs: list[tuple[str, object]] = json.loads(body, object_pairs_hook=list)
    keys = [k for k, _ in pairs]
    assert keys == sorted(keys)
    # Compact: no generated field values contain whitespace, so any whitespace
    # would be a separator.
    assert not any(c.isspace() for c in text)
    assert '": ' not in text and ", " not in text
    # Deterministic: the same model always yields the same bytes.
    assert encode_body(payload) == body


# ---------------------------------------------------------------------------
# Property 26: what UsageApiClient puts on the wire.
# ---------------------------------------------------------------------------


@settings(max_examples=75, deadline=None)
@given(
    secret=st.one_of(_SECRETS, _TEXT_SECRETS),
    wall_seconds=_WALL_SECONDS,
    fraction=st.sampled_from([0.0, 0.25, 0.5, 0.999]),
    payload=_PAYLOADS,
)
def test_client_sends_encoded_body_with_signed_headers(
    http_client: httpx.Client,
    secret: bytes,
    wall_seconds: int,
    fraction: float,
    payload: QuotaRequest | UsageReport,
) -> None:
    """Feature: sandbox-minute-plans, Property 26: Signature construction.

    **Validates: Requirements 11.2, 16.12**
    """
    url = _QUOTA_URL if isinstance(payload, QuotaRequest) else _USAGE_URL
    clock = _FakeClock(wall_seconds + fraction)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(url).mock(return_value=_success_response(payload))
        client = UsageApiClient(
            _BASE_URL,
            _StubSecret(secret),
            http_client,
            clock=clock,
            sleep=clock.sleep,
            jitter=_no_jitter,
        )
        _send(client, payload)

    assert route.call_count == 1
    # The Request_Timestamp is int(clock()).
    _assert_signed(route.calls[0].request, secret, encode_body(payload), wall_seconds)


@settings(max_examples=50, deadline=None)
@given(
    secret=st.one_of(_SECRETS, _TEXT_SECRETS),
    wall_seconds=_WALL_SECONDS,
    payload=_PAYLOADS,
)
def test_each_retry_attempt_is_resigned_with_a_fresh_timestamp(
    http_client: httpx.Client, secret: bytes, wall_seconds: int, payload: QuotaRequest | UsageReport
) -> None:
    """Feature: sandbox-minute-plans, Property 26: Signature construction.

    A 503 is retried; the fake ``sleep`` advances the clock by the backoff
    (0.25 s for quota, 0.5 s for usage), which from ``.875`` crosses into the
    next whole second, so the second attempt must carry a new timestamp and a
    signature that verifies for it.

    **Validates: Requirements 11.2, 16.12**
    """
    url = _QUOTA_URL if isinstance(payload, QuotaRequest) else _USAGE_URL
    clock = _FakeClock(wall_seconds + 0.875)
    with respx.mock(assert_all_called=True) as router:
        route = router.post(url).mock(side_effect=[httpx.Response(503), _success_response(payload)])
        client = UsageApiClient(
            _BASE_URL,
            _StubSecret(secret),
            http_client,
            clock=clock,
            sleep=clock.sleep,
            jitter=_no_jitter,
        )
        _send(client, payload)

    assert route.call_count == 2
    body = encode_body(payload)
    first, second = (call.request for call in route.calls)
    _assert_signed(first, secret, body, wall_seconds)
    _assert_signed(second, secret, body, wall_seconds + 1)
    assert first.headers[TIMESTAMP_HEADER] != second.headers[TIMESTAMP_HEADER]
    assert first.headers[SIGNATURE_HEADER] != second.headers[SIGNATURE_HEADER]
