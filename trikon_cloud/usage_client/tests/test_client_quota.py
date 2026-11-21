"""Property 28: quota responses classify as allowed, settled, denied, busy, or unavailable.

Drives :meth:`UsageApiClient.check_quota` through an :class:`httpx.MockTransport`
that plays a three-step script (transport errors, statuses, bodies) against a
fake clock that only moves when the client sleeps or a simulated request takes
time. Jitter is the identity, so backoff sleeps are exact.

The expected decision of each 200 body is built by the generator, not by
:func:`parse_quota_decision`, so the oracle is independent of the parser.

**Validates: Requirements 11.8, 11.18**
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Final, Literal, TypeAlias

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.usage_client.client import QUOTA_PATH, UsageApiClient
from trikon_cloud.usage_client.models import (
    QuotaAllowed,
    QuotaAlreadySettled,
    QuotaBusy,
    QuotaDecision,
    QuotaDenied,
    QuotaRequest,
    QuotaUnavailable,
    UnavailableCause,
)

_BASE_URL: Final = "https://api.trikon.example"
_T0: Final = 1_750_000_000.0
_EPS: Final = 1e-6
_PER_ATTEMPT_TIMEOUT: Final = 4.0
_MIN_ATTEMPT_SECONDS: Final = 0.001
_BACKOFF: Final = (0.25, 0.5)
_REQUEST: Final = QuotaRequest(
    installation_id=42, run_id="run-1", repository_full_name="octo/repo", pr_number=7
)
_KNOWN_OUTCOMES: Final = frozenset({"allowed", "already_settled", "denied", "busy"})
_UUID_RE: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

#: The decision a 200 body should produce; ``None`` means malformed.
Expected: TypeAlias = QuotaAllowed | QuotaAlreadySettled | QuotaDenied | QuotaBusy | None

_PLAIN_REASONS: Final[tuple[Literal["not_connected", "no_plan"], ...]] = (
    "not_connected",
    "no_plan",
)

#: Marks a body field that is left out.
_MISSING: Final = object()


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
        return b"usage-test-value"


class _BrokenSecret:
    def secret_bytes(self) -> bytes:
        raise RuntimeError("secret store unavailable")


@dataclass(frozen=True, slots=True)
class _Raise:
    exc_type: type[httpx.TransportError]
    cause: UnavailableCause


@dataclass(frozen=True, slots=True)
class _Respond:
    status: int
    body: bytes
    #: Only meaningful for status 200.
    expected: Expected


_Step: TypeAlias = _Raise | _Respond


@dataclass(frozen=True, slots=True)
class _Call:
    now: float
    timeout: float
    method: str
    path: str


class _Server:
    """Plays ``script`` one step per request and records each attempt.

    Nothing here asserts: ``check_quota`` swallows every exception, so checks
    run on the recorded calls after the call returns.
    """

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
        self.calls.append(_Call(self.clock.now, timeout, request.method, request.url.path))
        if index >= len(self.script):
            raise RuntimeError("unscripted attempt")
        self.clock.now += self.latency_fractions[index] * min(timeout, self.latency_cap)
        step = self.script[index]
        if isinstance(step, _Raise):
            raise step.exc_type("simulated", request=request)
        return httpx.Response(step.status, content=step.body)


def _run(
    script: Sequence[_Step],
    budget: float,
    latency_fractions: Sequence[float] = (0.0, 0.0, 0.0),
    latency_cap: float = 0.0,
    secret: _StubSecret | _BrokenSecret | None = None,
) -> tuple[QuotaDecision, _Server, _FakeClock]:
    clock = _FakeClock()
    server = _Server(script, clock, latency_fractions, latency_cap)
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as http:
        client = UsageApiClient(
            _BASE_URL,
            secret or _StubSecret(),
            http,
            clock.time,
            clock.sleep,
            jitter=lambda delay: delay,
        )
        result = client.check_quota(_REQUEST, budget)
    return result, server, clock


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _classify(step: _Step) -> tuple[QuotaDecision, bool]:
    """Reference model of one attempt: the decision it implies and whether it ends the call."""
    if isinstance(step, _Raise):
        return QuotaUnavailable(cause=step.cause, status=None), False
    if step.status == 200:
        decision = step.expected or QuotaUnavailable(cause="malformed", status=200)
        return decision, True
    unavailable = QuotaUnavailable(cause="http_status", status=step.status)
    return unavailable, not _is_retryable(step.status)


def _assert_decision(result: QuotaDecision, expected: QuotaDecision) -> None:
    assert type(result) is type(expected)
    assert result == expected
    if isinstance(result, QuotaBusy):
        # Carried unchanged: real ints, not bools or floats that compare equal.
        assert type(result.retry_after_seconds) is int
        assert type(result.concurrency_limit) is int
    if isinstance(result, QuotaDenied):
        if result.reason == "minutes_exhausted":
            assert result.period_end is not None
            assert result.period_end.utcoffset() == timedelta(0)
        else:
            assert result.period_end is None


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

# No lone surrogates: they cannot be encoded as UTF-8 and would turn a valid
# body malformed for reasons unrelated to the field under test.
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
_NEIGHBOUR_KEYS = (
    "outcome",
    "reservation_id",
    "reason",
    "period_end",
    "retry_after_seconds",
    "concurrency_limit",
    "reserved_minutes",
    "remaining_minutes",
)

_RESERVATION_IDS = st.builds(
    lambda value, upper: str(value).upper() if upper else str(value), st.uuids(), st.booleans()
)


def _assemble(
    draw: st.DrawFn, fields: dict[str, object], declared: frozenset[str]
) -> dict[str, object]:
    """``fields`` (dropping ``_MISSING`` ones) plus ignored extra keys, in random order."""
    keys = st.one_of(st.sampled_from(_NEIGHBOUR_KEYS), _TEXT).filter(lambda k: k not in declared)
    extras = draw(st.dictionaries(keys, _JSON, max_size=3))
    present = [(k, v) for k, v in fields.items() if v is not _MISSING]
    return dict(draw(st.permutations([*present, *extras.items()])))


@st.composite
def _reservation_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    outcome = draw(st.sampled_from(["allowed", "already_settled"]))
    valid = draw(st.booleans())
    if valid:
        rid: object = draw(_RESERVATION_IDS)
    else:
        rid = draw(
            st.one_of(
                st.just(_MISSING),
                st.none(),
                st.integers(),
                st.uuids().map(lambda u: u.hex),
                _TEXT.filter(lambda s: _UUID_RE.fullmatch(s) is None),
            )
        )
    body = _assemble(
        draw, {"outcome": outcome, "reservation_id": rid}, frozenset({"outcome", "reservation_id"})
    )
    if not valid:
        return body, None
    assert isinstance(rid, str)
    if outcome == "allowed":
        return body, QuotaAllowed(reservation_id=rid)
    return body, QuotaAlreadySettled(reservation_id=rid)


@st.composite
def _plain_denied_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    """``not_connected`` and ``no_plan``; an extra ``period_end`` is ignored."""
    reason = draw(st.sampled_from(_PLAIN_REASONS))
    body = _assemble(
        draw, {"outcome": "denied", "reason": reason}, frozenset({"outcome", "reason"})
    )
    return body, QuotaDenied(reason=reason, period_end=None)


@st.composite
def _exhausted_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    naive = draw(st.datetimes(min_value=datetime(1970, 1, 1), max_value=datetime(2200, 1, 1)))
    offset = timedelta(minutes=draw(st.integers(min_value=-1439, max_value=1439)))
    timespec = draw(st.sampled_from(["seconds", "milliseconds", "microseconds"]))
    if timespec == "seconds":
        naive = naive.replace(microsecond=0)
    elif timespec == "milliseconds":
        naive = naive.replace(microsecond=naive.microsecond // 1000 * 1000)
    aware = naive.replace(tzinfo=timezone(offset))
    text = aware.isoformat(timespec=timespec)
    if offset == timedelta(0) and draw(st.booleans()):
        text = text.removesuffix("+00:00") + "Z"
    body = _assemble(
        draw,
        {"outcome": "denied", "reason": "minutes_exhausted", "period_end": text},
        frozenset({"outcome", "reason", "period_end"}),
    )
    return body, QuotaDenied(reason="minutes_exhausted", period_end=aware.astimezone(UTC))


def _parses_as_aware(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


@st.composite
def _bad_exhausted_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    period_end = draw(
        st.one_of(
            st.sampled_from(
                [
                    _MISSING,
                    None,
                    "",
                    "2026-03-01T00:00:00",  # no offset
                    "2026-02-30T00:00:00Z",  # no such day
                    "0001-01-01T00:00:00+01:00",  # UTC instant before year 1
                    "9999-12-31T23:59:59-01:00",  # UTC instant after year 9999
                ]
            ),
            st.integers(),
            _TEXT.filter(lambda s: not _parses_as_aware(s)),
        )
    )
    body = _assemble(
        draw,
        {"outcome": "denied", "reason": "minutes_exhausted", "period_end": period_end},
        frozenset({"outcome", "reason", "period_end"}),
    )
    return body, None


_NON_INT_VALUES: st.SearchStrategy[object] = st.one_of(
    st.floats(),  # includes integral floats such as 5.0
    st.booleans(),
    st.integers(min_value=1, max_value=900).map(str),
    st.none(),
    st.lists(st.integers(), max_size=2),
)


@st.composite
def _busy_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    """In-range values give QuotaBusy unchanged; anything else is malformed."""
    retry_valid = draw(st.booleans())
    limit_valid = draw(st.booleans())
    retry: object
    limit: object
    if retry_valid:
        retry = draw(st.integers(min_value=1, max_value=900))
    else:
        retry = draw(
            st.one_of(
                st.just(_MISSING),
                st.sampled_from([0, 901, -1]),
                st.integers(max_value=0),
                st.integers(min_value=901),
                _NON_INT_VALUES,
            )
        )
    if limit_valid:
        limit = draw(st.integers(min_value=1, max_value=2**53))
    else:
        limit = draw(
            st.one_of(
                st.just(_MISSING), st.sampled_from([0, -1]), st.integers(max_value=0), _NON_INT_VALUES
            )
        )
    body = _assemble(
        draw,
        {"outcome": "busy", "retry_after_seconds": retry, "concurrency_limit": limit},
        frozenset({"outcome", "retry_after_seconds", "concurrency_limit"}),
    )
    if retry_valid and limit_valid:
        assert isinstance(retry, int)
        assert isinstance(limit, int)
        return body, QuotaBusy(retry_after_seconds=retry, concurrency_limit=limit)
    return body, None


@st.composite
def _unknown_bodies(draw: st.DrawFn) -> tuple[dict[str, object], Expected]:
    """An unknown ``outcome``, or ``denied`` with a missing or unknown ``reason``."""
    if draw(st.booleans()):
        outcome = draw(
            st.one_of(
                st.just(_MISSING),
                st.sampled_from(["ALLOWED", "Busy", "rate_limited", "conflict"]),
                _TEXT.filter(lambda s: s not in _KNOWN_OUTCOMES),
                _JSON_SCALARS.filter(lambda v: not isinstance(v, str)),
            )
        )
        fields: dict[str, object] = {"outcome": outcome}
        declared = frozenset({"outcome"})
    else:
        reason = draw(
            st.one_of(
                st.just(_MISSING),
                st.sampled_from(["suspended", "NO_PLAN", "minutes-exhausted"]),
                _TEXT.filter(lambda s: s not in {"not_connected", "no_plan", "minutes_exhausted"}),
                _JSON_SCALARS.filter(lambda v: not isinstance(v, str)),
            )
        )
        fields = {"outcome": "denied", "reason": reason}
        declared = frozenset({"outcome", "reason"})
    return _assemble(draw, fields, declared), None


def _has_known_outcome(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("outcome") in _KNOWN_OUTCOMES


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
    st.binary(max_size=64), st.sampled_from([b"", b"{", b"null", b"[]", b"<html>busy</html>"])
).filter(lambda raw: not _has_known_outcome(raw))

#: A 200 body and the decision it should produce.
_BODIES: st.SearchStrategy[tuple[bytes, Expected]] = st.one_of(
    _encoded(_reservation_bodies()),
    _encoded(_plain_denied_bodies()),
    _encoded(_exhausted_bodies()),
    _encoded(_bad_exhausted_bodies()),
    _encoded(_busy_bodies()),
    _encoded(_unknown_bodies()),
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
_OK = _BODIES.map(lambda pair: _Respond(200, pair[0], pair[1]))
_RETRYABLE = st.builds(
    _Respond,
    st.one_of(st.just(429), st.integers(min_value=500, max_value=599)),
    _ANY_BODY_BYTES,
    st.none(),
)
_FINAL_STATUS = st.builds(
    _Respond,
    st.integers(min_value=201, max_value=499).filter(lambda s: s != 429),
    _ANY_BODY_BYTES,
    st.none(),
)
_STEP: st.SearchStrategy[_Step] = st.one_of(_RAISES, _OK, _RETRYABLE, _FINAL_STATUS)
_SCRIPTS = st.lists(_STEP, min_size=3, max_size=3)
_FRACTIONS = st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=3, max_size=3)


# ---------------------------------------------------------------------------
# Property 28.
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    script=_SCRIPTS,
    budget=st.floats(min_value=5.0, max_value=20.0),
    fractions=_FRACTIONS,
)
def test_property_28_classification_with_ample_budget(
    script: list[_Step], budget: float, fractions: list[float]
) -> None:
    """Feature: sandbox-minute-plans, Property 28 (ample budget: exact attempt counts).

    Requests take at most 1 s, so three attempts and both backoffs (3.75 s)
    always fit: the call stops at the first non-retryable result or after
    three attempts.

    **Validates: Requirements 11.8, 11.18**
    """
    result, server, clock = _run(script, budget, fractions, latency_cap=1.0)

    outcomes = [_classify(step) for step in script]
    attempts = next((i + 1 for i, (_, final) in enumerate(outcomes) if final), 3)
    _assert_decision(result, outcomes[attempts - 1][0])

    assert len(server.calls) == attempts
    assert clock.sleeps == list(_BACKOFF[: attempts - 1])
    for call in server.calls:
        assert call.method == "POST"
        assert call.path == QUOTA_PATH
        remaining = budget - (call.now - _T0)
        assert call.timeout == pytest.approx(min(remaining, _PER_ATTEMPT_TIMEOUT), abs=_EPS)
    assert clock.now - _T0 <= budget + _EPS


@settings(max_examples=300, deadline=None)
@given(
    script=_SCRIPTS,
    budget=st.one_of(
        st.floats(min_value=-1.0, max_value=3.0),
        st.sampled_from([0.0, 0.0005, 0.001, 0.25, 0.2505]),
    ),
    fractions=_FRACTIONS,
)
def test_property_28_classification_within_tight_budget(
    script: list[_Step], budget: float, fractions: list[float]
) -> None:
    """Feature: sandbox-minute-plans, Property 28 (tight budget: never overruns).

    Each request may take up to its whole per-attempt timeout. The call makes
    at most three attempts, never runs past the budget on the injected clock,
    stops early only once the budget is used up, and classifies by the last
    attempt it made (``("timeout", None)`` when none fit).

    **Validates: Requirements 11.8, 11.18**
    """
    result, server, clock = _run(script, budget, fractions, latency_cap=float("inf"))

    made = len(server.calls)
    assert made <= 3
    outcomes = [_classify(step) for step in script[:made]]
    # No attempt follows a non-retryable result.
    assert not any(final for _, final in outcomes[:-1])
    expected = outcomes[-1][0] if outcomes else QuotaUnavailable(cause="timeout", status=None)
    _assert_decision(result, expected)

    elapsed = clock.now - _T0
    assert elapsed <= max(budget, 0.0) + _EPS
    if made < 3 and not (outcomes and outcomes[-1][1]):
        assert elapsed >= budget - _MIN_ATTEMPT_SECONDS - _EPS
    for call in server.calls:
        remaining = budget - (call.now - _T0)
        assert 0 < call.timeout <= _PER_ATTEMPT_TIMEOUT
        assert call.timeout == pytest.approx(min(remaining, _PER_ATTEMPT_TIMEOUT), abs=_EPS)
    assert all(0 <= s <= b for s, b in zip(clock.sleeps, _BACKOFF, strict=False))


# ---------------------------------------------------------------------------
# Examples.
# ---------------------------------------------------------------------------


def _single_200(body: dict[str, object] | bytes) -> tuple[QuotaDecision, _Server, _FakeClock]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return _run([_Respond(200, raw, None)] * 3, budget=10.0)


@pytest.mark.parametrize(
    "period_end",
    ["2026-03-01T00:00:00.000Z", "2026-03-01T05:30:00+05:30", "2026-02-28T19:00:00-05:00"],
)
def test_minutes_exhausted_period_end_is_aware_utc(period_end: str) -> None:
    result, server, _ = _single_200(
        {
            "outcome": "denied",
            "reason": "minutes_exhausted",
            "remaining_minutes": 0,
            "period_end": period_end,
        }
    )
    assert result == QuotaDenied(
        reason="minutes_exhausted", period_end=datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert isinstance(result, QuotaDenied)
    assert result.period_end is not None
    assert result.period_end.tzinfo is UTC
    assert len(server.calls) == 1


@pytest.mark.parametrize("reason", ["not_connected", "no_plan"])
def test_other_denials_have_no_period_end(reason: Literal["not_connected", "no_plan"]) -> None:
    result, _, _ = _single_200(
        {"outcome": "denied", "reason": reason, "period_end": "2026-03-01T00:00:00Z"}
    )
    assert result == QuotaDenied(reason=reason, period_end=None)


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "throttled"},
        {"outcome": "denied", "reason": "suspended"},
        {"outcome": "denied"},
        {"reason": "no_plan"},
        {"outcome": "denied", "reason": "minutes_exhausted"},
        {"outcome": "denied", "reason": "minutes_exhausted", "period_end": "2026-03-01T00:00:00"},
        {"outcome": "denied", "reason": "minutes_exhausted", "period_end": "0001-01-01T00:00:00+01:00"},
        {"outcome": "allowed", "reservation_id": "not-a-uuid"},
        b"not json",
        b"[]",
    ],
)
def test_unknown_or_invalid_bodies_are_malformed(body: dict[str, object] | bytes) -> None:
    result, server, _ = _single_200(body)
    assert result == QuotaUnavailable(cause="malformed", status=200)
    assert len(server.calls) == 1


@pytest.mark.parametrize(("retry", "limit"), [(1, 1), (900, 1), (30, 5), (1, 2**53)])
def test_busy_in_range_is_carried_unchanged(retry: int, limit: int) -> None:
    result, _, _ = _single_200(
        {"outcome": "busy", "retry_after_seconds": retry, "concurrency_limit": limit}
    )
    _assert_decision(result, QuotaBusy(retry_after_seconds=retry, concurrency_limit=limit))


@pytest.mark.parametrize(
    "fields",
    [
        {"retry_after_seconds": 0, "concurrency_limit": 1},
        {"retry_after_seconds": 901, "concurrency_limit": 1},
        {"retry_after_seconds": -5, "concurrency_limit": 1},
        {"retry_after_seconds": 5.0, "concurrency_limit": 1},
        {"retry_after_seconds": 5.5, "concurrency_limit": 1},
        {"retry_after_seconds": True, "concurrency_limit": 1},
        {"retry_after_seconds": "5", "concurrency_limit": 1},
        {"retry_after_seconds": 5, "concurrency_limit": 0},
        {"retry_after_seconds": 5, "concurrency_limit": -1},
        {"retry_after_seconds": 5, "concurrency_limit": 2.0},
        {"retry_after_seconds": 5, "concurrency_limit": True},
        {"retry_after_seconds": 5, "concurrency_limit": "2"},
        {"retry_after_seconds": 5},
        {"concurrency_limit": 1},
        {},
    ],
)
def test_busy_out_of_range_or_missing_is_malformed(fields: dict[str, object]) -> None:
    result, _, _ = _single_200({"outcome": "busy", **fields})
    assert result == QuotaUnavailable(cause="malformed", status=200)


def test_unreadable_secret_fails_closed_without_a_request() -> None:
    result, server, _ = _run([_Respond(200, b"{}", None)] * 3, 10.0, secret=_BrokenSecret())
    assert result == QuotaUnavailable(cause="network", status=None)
    assert server.calls == []
