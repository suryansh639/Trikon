"""Property 30: usage report delivery and retry.

Drives :meth:`UsageApiClient.report_usage` through an :class:`httpx.MockTransport`
that plays a three-step script (transport errors, statuses, bodies) against a
fake clock that only moves when the client sleeps or a simulated request takes
time. Jitter is the identity unless a test says otherwise, so backoff sleeps
are exact.

Whether a 2xx body is a valid ``recorded`` or ``duplicate`` body is decided by
the generator, not by :func:`parse_usage_outcome`, so the per-attempt reference
model is independent of the parser.

**Validates: Requirements 12.7, 12.10, 12.11, 12.12**
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.usage_client.client import USAGE_PATH, UsageApiClient
from trikon_cloud.usage_client.metering import (
    PER_RUN_CAP_MINUTES,
    billed_minutes,
    format_utc_ms,
)
from trikon_cloud.usage_client.models import (
    UsageDelivered,
    UsageRejected,
    UsageReport,
    UsageReportUndeliveredError,
)
from trikon_cloud.usage_client.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    encode_body,
    signed_headers,
)

_BASE_URL: Final = "https://api.trikon.example"
_T0: Final = 1_750_000_000.0
_EPS: Final = 1e-6
_BUDGET: Final = 30.0
_PER_ATTEMPT_TIMEOUT: Final = 10.0
_MIN_ATTEMPT_SECONDS: Final = 0.001
_BACKOFF: Final = (0.5, 1.0)
_SECRET: Final = b"usage-test-value"
_KNOWN_OUTCOMES: Final = frozenset({"recorded", "duplicate"})
_UUID_RE: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_ALNUM: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

#: The outcome a 2xx body names when it is valid; ``None`` means malformed.
Expected: TypeAlias = Literal["recorded", "duplicate"] | None

#: Marks a body field that is left out.
_MISSING: Final = object()

_FIXED_REPORT: Final = UsageReport(
    run_id="run-fixed-0001",
    reservation_id="00000000-0000-4000-8000-000000000001",
    billed_minutes=0,
    started_at=None,
    stopped_at=None,
)


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = _T0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _StubSecret:
    def secret_bytes(self) -> bytes:
        return _SECRET


class _BrokenSecret:
    def secret_bytes(self) -> bytes:
        raise RuntimeError("secret store unavailable")


@dataclass(frozen=True, slots=True)
class _Raise:
    exc_type: type[httpx.TransportError]
    cause: Literal["network", "timeout"]


@dataclass(frozen=True, slots=True)
class _Respond:
    status: int
    body: bytes
    #: Only meaningful for a 2xx status.
    expected: Expected


_Step: TypeAlias = _Raise | _Respond


@dataclass(frozen=True, slots=True)
class _Call:
    now: float
    timeout: float
    method: str
    path: str
    content: bytes
    headers: dict[str, str]


class _Server:
    """Plays ``script`` one step per request and records each attempt."""

    def __init__(
        self,
        script: Sequence[_Step],
        clock: _FakeClock,
        latency_fractions: Sequence[float],
        latency_cap: float,
    ) -> None:
        self.script = script
        self.clock = clock
        self.latency_fractions = latency_fractions
        self.latency_cap = latency_cap
        self.calls: list[_Call] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        index = len(self.calls)
        timeout = float(request.extensions["timeout"]["read"])
        self.calls.append(
            _Call(
                now=self.clock.now,
                timeout=timeout,
                method=request.method,
                path=request.url.path,
                content=request.content,
                headers={
                    name: request.headers[name]
                    for name in ("Content-Type", TIMESTAMP_HEADER, SIGNATURE_HEADER)
                    if name in request.headers
                },
            )
        )
        if index >= len(self.script):
            raise RuntimeError("unscripted attempt")
        self.clock.now += self.latency_fractions[index] * min(timeout, self.latency_cap)
        step = self.script[index]
        if isinstance(step, _Raise):
            raise step.exc_type("simulated", request=request)
        return httpx.Response(step.status, content=step.body)


_Outcome: TypeAlias = UsageDelivered | UsageRejected | UsageReportUndeliveredError


def _run(
    report: UsageReport,
    script: Sequence[_Step],
    latency_fractions: Sequence[float] = (0.0, 0.0, 0.0),
    latency_cap: float = 0.0,
    secret: _StubSecret | _BrokenSecret | None = None,
    jitter: Callable[[float], float] = lambda delay: delay,
) -> tuple[_Outcome, _Server, _FakeClock]:
    clock = _FakeClock()
    server = _Server(script, clock, latency_fractions, latency_cap)
    result: _Outcome
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as http:
        client = UsageApiClient(
            _BASE_URL,
            secret or _StubSecret(),
            http,
            clock.time,
            clock.sleep,
            jitter=jitter,
        )
        try:
            result = client.report_usage(report)
        except UsageReportUndeliveredError as exc:
            result = exc
    return result, server, clock


# ---------------------------------------------------------------------------
# Reference model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Retry:
    """A retryable attempt: the cause and status the undelivered message should name."""

    cause: Literal["network", "timeout", "http_status"]
    status: int | None


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _classify(step: _Step) -> UsageDelivered | UsageRejected | _Retry:
    """Reference model of one attempt. ``_Retry`` means the call goes on."""
    if isinstance(step, _Raise):
        return _Retry(cause=step.cause, status=None)
    if _is_retryable(step.status):
        return _Retry(cause="http_status", status=step.status)
    if step.status == 201 and step.expected == "recorded":
        return UsageDelivered(outcome="recorded")
    if step.status == 200 and step.expected == "duplicate":
        return UsageDelivered(outcome="duplicate")
    return UsageRejected(status=step.status)


def _assert_undelivered(
    result: _Outcome, attempts: int, last: _Retry, report: UsageReport
) -> None:
    assert isinstance(result, UsageReportUndeliveredError)
    message = str(result)
    assert f"{attempts} attempt" in message
    assert last.cause in message
    if last.status is not None:
        assert str(last.status) in message
    # Nothing sensitive or identifying leaks into the message.
    body = encode_body(report)
    for leaked in (body.decode("ascii"), _SECRET.decode("ascii"), report.run_id, report.reservation_id):
        assert leaked not in message


def _assert_result(
    result: _Outcome,
    expected: UsageDelivered | UsageRejected | _Retry,
    attempts: int,
    report: UsageReport,
) -> None:
    if isinstance(expected, _Retry):
        _assert_undelivered(result, attempts, expected, report)
    else:
        assert type(result) is type(expected)
        assert result == expected


def _assert_calls(server: _Server, report: UsageReport) -> None:
    """Same bytes on every attempt, each re-signed for its own timestamp, timeout per the budget."""
    body = encode_body(report)
    for call in server.calls:
        assert call.method == "POST"
        assert call.path == USAGE_PATH
        assert call.content == body
        assert call.headers == signed_headers(_SECRET, body, int(call.now))
        remaining = _BUDGET - (call.now - _T0)
        assert 0 < call.timeout <= _PER_ATTEMPT_TIMEOUT
        assert call.timeout == pytest.approx(min(remaining, _PER_ATTEMPT_TIMEOUT), abs=_EPS)


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

# Run_Ids carry a prefix that never occurs in the client's messages, so the
# "no run_id in the message" check is meaningful even for one-character tails.
_RUN_IDS = st.text(alphabet=_ALNUM + "_-", min_size=1, max_size=120).map(lambda s: "run-" + s)
_RESERVATION_IDS = st.uuids().flatmap(lambda u: st.sampled_from([str(u), str(u).upper()]))


@st.composite
def _usage_reports(draw: st.DrawFn) -> UsageReport:
    run_id = draw(_RUN_IDS)
    reservation_id = draw(_RESERVATION_IDS)
    if draw(st.booleans()):
        # A task that never started: no timestamps, nothing billed (Req 12.12).
        return UsageReport(
            run_id=run_id,
            reservation_id=reservation_id,
            billed_minutes=0,
            started_at=None,
            stopped_at=None,
        )
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


# No lone surrogates: they cannot be encoded as UTF-8.
_TEXT = st.text(st.characters(codec="utf-8"), max_size=12)


def _json_containers(children: st.SearchStrategy[object]) -> st.SearchStrategy[object]:
    return st.one_of(
        st.lists(children, max_size=4), st.dictionaries(_TEXT, children, max_size=4)
    )


_JSON_SCALARS: st.SearchStrategy[object] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    _TEXT,
)
_JSON: st.SearchStrategy[object] = st.recursive(_JSON_SCALARS, _json_containers, max_leaves=8)

# Keys real responses carry next to the declared ones, plus arbitrary keys.
_NEIGHBOUR_KEYS = ("outcome", "reservation_id", "billed_minutes", "error", "reason")
_DECLARED: Final = frozenset({"outcome", "reservation_id", "billed_minutes"})


def _assemble(draw: st.DrawFn, fields: dict[str, object]) -> dict[str, object]:
    """``fields`` (dropping ``_MISSING`` ones) plus ignored extra keys, in random order."""
    keys = st.one_of(st.sampled_from(_NEIGHBOUR_KEYS), _TEXT).filter(lambda k: k not in _DECLARED)
    extras = draw(st.dictionaries(keys, _JSON, max_size=3))
    present = [(k, v) for k, v in fields.items() if v is not _MISSING]
    return dict(draw(st.permutations([*present, *extras.items()])))


_BAD_RESERVATION_IDS: st.SearchStrategy[object] = st.one_of(
    st.just(_MISSING),
    st.none(),
    st.integers(),
    st.uuids().map(lambda u: u.hex),
    _TEXT.filter(lambda s: _UUID_RE.fullmatch(s) is None),
)
_BAD_BILLED: st.SearchStrategy[object] = st.one_of(
    st.just(_MISSING),
    st.sampled_from([-1, PER_RUN_CAP_MINUTES + 1]),
    st.integers(max_value=-1),
    st.integers(min_value=PER_RUN_CAP_MINUTES + 1),
    st.floats(),  # includes integral floats such as 5.0
    st.booleans(),
    st.integers(min_value=0, max_value=PER_RUN_CAP_MINUTES).map(str),
    st.none(),
    st.lists(st.integers(), max_size=2),
)


@st.composite
def _outcome_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    """A ``recorded`` or ``duplicate`` body; valid only if both fields are valid."""
    outcome = draw(st.sampled_from(sorted(_KNOWN_OUTCOMES)))
    rid_valid = draw(st.booleans())
    billed_valid = draw(st.booleans())
    rid = draw(_RESERVATION_IDS) if rid_valid else draw(_BAD_RESERVATION_IDS)
    billed = (
        draw(st.integers(min_value=0, max_value=PER_RUN_CAP_MINUTES))
        if billed_valid
        else draw(_BAD_BILLED)
    )
    body = _assemble(draw, {"outcome": outcome, "reservation_id": rid, "billed_minutes": billed})
    expected: Expected = "recorded" if outcome == "recorded" else "duplicate"
    return body, expected if rid_valid and billed_valid else None


@st.composite
def _unknown_outcome_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    """Valid fields under a missing or unknown ``outcome``."""
    outcome = draw(
        st.one_of(
            st.just(_MISSING),
            st.sampled_from(["RECORDED", "Duplicate", "conflict", "not_found", "allowed"]),
            _TEXT.filter(lambda s: s not in _KNOWN_OUTCOMES),
            _JSON_SCALARS.filter(lambda v: not isinstance(v, str)),
        )
    )
    fields: dict[str, object] = {
        "outcome": outcome,
        "reservation_id": draw(_RESERVATION_IDS),
        "billed_minutes": draw(st.integers(min_value=0, max_value=PER_RUN_CAP_MINUTES)),
    }
    return _assemble(draw, fields), None


def _has_known_outcome(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    outcome = value.get("outcome")
    return isinstance(outcome, str) and outcome in _KNOWN_OUTCOMES


@st.composite
def _encoded(
    draw: st.DrawFn, bodies: st.SearchStrategy[tuple[dict[str, object], Expected]]
) -> tuple[bytes, Expected]:
    body, expected = draw(bodies)
    text = json.dumps(body, ensure_ascii=draw(st.booleans()))
    return text.encode("utf-8"), expected


_RANDOM_JSON_BYTES = st.builds(
    lambda value: json.dumps(value).encode("utf-8"), _JSON
).filter(lambda raw: not _has_known_outcome(raw))
_RANDOM_BYTES = st.one_of(
    st.binary(max_size=64), st.sampled_from([b"", b"{", b"null", b"[]", b"<html>oops</html>"])
).filter(lambda raw: not _has_known_outcome(raw))

#: A response body and the outcome it validly names, if any.
_BODIES: st.SearchStrategy[tuple[bytes, Expected]] = st.one_of(
    _encoded(_outcome_bodies()),
    _encoded(_unknown_outcome_bodies()),
    _RANDOM_JSON_BYTES.map(lambda raw: (raw, None)),
    _RANDOM_BYTES.map(lambda raw: (raw, None)),
)
_ANY_BODY_BYTES = _BODIES.map(lambda pair: pair[0])

_RAISES = st.sampled_from(
    [
        _Raise(httpx.ConnectError, "network"),
        _Raise(httpx.RemoteProtocolError, "network"),
        _Raise(httpx.ReadTimeout, "timeout"),
        _Raise(httpx.ConnectTimeout, "timeout"),
    ]
)
# 200 and 201 with every kind of body, so both right and wrong pairings occur.
_SUCCESS_STATUS = st.builds(
    lambda status, pair: _Respond(status, pair[0], pair[1]), st.sampled_from([200, 201]), _BODIES
)
_OTHER_2XX = st.builds(
    lambda status, pair: _Respond(status, pair[0], pair[1]),
    st.integers(min_value=202, max_value=299),
    _BODIES,
)
_RETRYABLE = st.builds(
    _Respond,
    st.one_of(st.just(429), st.integers(min_value=500, max_value=599)),
    _ANY_BODY_BYTES,
    st.none(),
)
_REJECTED = st.builds(
    _Respond,
    st.one_of(
        st.sampled_from([400, 401, 404, 409]),
        st.integers(min_value=300, max_value=499).filter(lambda s: s != 429),
    ),
    _ANY_BODY_BYTES,
    st.none(),
)
_STEP: st.SearchStrategy[_Step] = st.one_of(
    _RAISES, _RAISES, _RETRYABLE, _RETRYABLE, _SUCCESS_STATUS, _OTHER_2XX, _REJECTED
)
_SCRIPTS = st.lists(_STEP, min_size=3, max_size=3)
_FRACTIONS = st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=3, max_size=3)


# ---------------------------------------------------------------------------
# Property 30.
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(report=_usage_reports(), script=_SCRIPTS, fractions=_FRACTIONS)
def test_property_30_delivery_and_retry_with_ample_budget(
    report: UsageReport, script: list[_Step], fractions: list[float]
) -> None:
    """Feature: sandbox-minute-plans, Property 30 (ample budget: exact attempt counts).

    Requests take at most 1 s, so three attempts and both backoffs (4.5 s)
    always fit in 30 s: the call stops at the first delivered or rejected
    result, or raises after three retryable attempts.

    **Validates: Requirements 12.7, 12.10, 12.11, 12.12**
    """
    result, server, clock = _run(report, script, fractions, latency_cap=1.0)

    outcomes = [_classify(step) for step in script]
    attempts = next(
        (i + 1 for i, outcome in enumerate(outcomes) if not isinstance(outcome, _Retry)), 3
    )
    _assert_result(result, outcomes[attempts - 1], attempts, report)

    assert len(server.calls) == attempts
    assert clock.sleeps == list(_BACKOFF[: attempts - 1])
    _assert_calls(server, report)
    assert clock.now - _T0 <= _BUDGET + _EPS


@settings(max_examples=300, deadline=None)
@given(
    report=_usage_reports(),
    script=_SCRIPTS,
    fractions=_FRACTIONS,
    jitter_factor=st.one_of(st.just(1.0), st.floats(min_value=0.8, max_value=1.2)),
)
def test_property_30_delivery_and_retry_within_budget(
    report: UsageReport, script: list[_Step], fractions: list[float], jitter_factor: float
) -> None:
    """Feature: sandbox-minute-plans, Property 30 (slow requests: never overruns 30 s).

    Each request may take up to its whole per-attempt timeout and backoff
    carries up to ±20 % jitter. The call makes at most three attempts, never
    runs past 30 s on the injected clock, stops early only once the budget is
    used up, never attempts again after a delivered or rejected result, and
    reports by the last attempt it made.

    **Validates: Requirements 12.7, 12.10, 12.11, 12.12**
    """
    result, server, clock = _run(
        report,
        script,
        fractions,
        latency_cap=float("inf"),
        jitter=lambda delay: delay * jitter_factor,
    )

    made = len(server.calls)
    assert 1 <= made <= 3
    outcomes = [_classify(step) for step in script[:made]]
    # No attempt follows a delivered or rejected result.
    assert all(isinstance(outcome, _Retry) for outcome in outcomes[:-1])
    _assert_result(result, outcomes[-1], made, report)

    elapsed = clock.now - _T0
    assert elapsed <= _BUDGET + _EPS
    if made < 3 and isinstance(outcomes[-1], _Retry):
        assert elapsed >= _BUDGET - _MIN_ATTEMPT_SECONDS - _EPS
    _assert_calls(server, report)
    assert made - 1 <= len(clock.sleeps) <= made
    assert all(
        0 <= s <= b * jitter_factor + _EPS for s, b in zip(clock.sleeps, _BACKOFF, strict=False)
    )


# ---------------------------------------------------------------------------
# Examples.
# ---------------------------------------------------------------------------


def _body(outcome: str, **overrides: object) -> bytes:
    fields: dict[str, object] = {
        "outcome": outcome,
        "reservation_id": _FIXED_REPORT.reservation_id,
        "billed_minutes": 0,
        **overrides,
    }
    return json.dumps(fields).encode("utf-8")


_UNSCRIPTED: Final = _Respond(500, b"", None)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (201, _body("recorded"), UsageDelivered(outcome="recorded")),
        (200, _body("duplicate", billed_minutes=PER_RUN_CAP_MINUTES), UsageDelivered(outcome="duplicate")),
        (200, _body("recorded"), UsageRejected(status=200)),
        (201, _body("duplicate"), UsageRejected(status=201)),
        (201, _body("recorded", billed_minutes=PER_RUN_CAP_MINUTES + 1), UsageRejected(status=201)),
        (201, _body("recorded", reservation_id="not-a-uuid"), UsageRejected(status=201)),
        (201, b"not json", UsageRejected(status=201)),
        (202, _body("recorded"), UsageRejected(status=202)),
        (400, b'{"error":"invalid_payload"}', UsageRejected(status=400)),
        (401, b'{"error":"unauthorized"}', UsageRejected(status=401)),
        (404, b'{"error":"not_found"}', UsageRejected(status=404)),
        (409, b'{"error":"conflict"}', UsageRejected(status=409)),
    ],
)
def test_first_final_response_ends_the_call(
    status: int, body: bytes, expected: UsageDelivered | UsageRejected
) -> None:
    result, server, clock = _run(_FIXED_REPORT, [_Respond(status, body, None), _UNSCRIPTED])
    assert result == expected
    assert len(server.calls) == 1
    assert clock.sleeps == []


def test_three_retryable_failures_raise_with_backoff() -> None:
    script: list[_Step] = [
        _Raise(httpx.ConnectError, "network"),
        _Respond(429, b"", None),
        _Respond(503, b"", None),
    ]
    result, server, clock = _run(_FIXED_REPORT, script)
    _assert_undelivered(result, 3, _Retry(cause="http_status", status=503), _FIXED_REPORT)
    assert str(result) == "Usage_Report not delivered after 3 attempt(s); last failure: http_status 503"
    assert len(server.calls) == 3
    assert clock.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    ("first_latency_fraction", "attempts", "sleeps", "last_timeout"),
    [
        # 0.5 ms left after attempt 1: the capped sleep uses it up, no attempt 2.
        (2.99995, 1, [0.0005], 10.0),
        # 0.5005 s left: the 0.5 s backoff leaves 0.5 ms, below the 1 ms floor.
        (2.94995, 1, [0.5], 10.0),
        # 0.502 s left: the backoff leaves 2 ms, so attempt 2 runs with a 2 ms
        # timeout and takes half of it; the capped 1 ms sleep then ends the call.
        (2.9498, 2, [0.5, 0.001], 0.002),
    ],
)
def test_attempts_stop_once_less_than_a_millisecond_remains(
    first_latency_fraction: float, attempts: int, sleeps: list[float], last_timeout: float
) -> None:
    """A request can outlast its timeout (httpx times each operation separately)."""
    script = [_Respond(503, b"", None)] * 3
    result, server, clock = _run(
        _FIXED_REPORT, script, (first_latency_fraction, 0.5, 0.5), latency_cap=float("inf")
    )
    _assert_undelivered(result, attempts, _Retry(cause="http_status", status=503), _FIXED_REPORT)
    assert len(server.calls) == attempts
    assert server.calls[-1].timeout == pytest.approx(last_timeout, abs=_EPS)
    assert clock.sleeps == pytest.approx(sleeps, abs=_EPS)
    assert clock.now - _T0 <= _BUDGET + _EPS


def test_unreadable_secret_raises_chained_without_a_request() -> None:
    result, server, _ = _run(_FIXED_REPORT, [_UNSCRIPTED], secret=_BrokenSecret())
    assert isinstance(result, UsageReportUndeliveredError)
    assert isinstance(result.__cause__, RuntimeError)
    assert "secret store unavailable" not in str(result)
    assert server.calls == []
