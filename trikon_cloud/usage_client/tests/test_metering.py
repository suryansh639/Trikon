"""Tests for the Metering_Rule (sandbox-minute-plans §10, Property 25).

The shared vectors below are pinned in both test suites: this file and
Trikon_Web's ``src/usage/metering.test.ts`` (Property 15). Keep them in sync.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.fargate_runner import entrypoint
from trikon_cloud.usage_client.metering import (
    MS_PER_MINUTE,
    PER_RUN_CAP_MINUTES,
    TASK_FAILED_TO_START,
    MeteredUsage,
    MeteringInputError,
    billed_minutes,
    format_utc_ms,
    meter_stopped_task,
    to_epoch_ms,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# (stop_ms - start_ms, billed minutes), design.md §10.
_SHARED_VECTORS: tuple[tuple[int, int], ...] = (
    (-5_000, 1),
    (0, 1),
    (1, 1),
    (60_000, 1),
    (60_001, 2),
    (599_999, 10),
    (600_000, 10),
    (3_600_000, 10),
)

# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

# Anchor instants between 2000-01-01 and 2100-01-01, in microseconds since the
# epoch, so sub-millisecond precision is exercised by to_epoch_ms's floor.
_ANCHOR_US = st.integers(min_value=946_684_800 * 10**6, max_value=4_102_444_800 * 10**6)

# Offsets from the anchor: arbitrary values from -15 to +120 minutes, plus
# values within a few microseconds of every whole-minute boundary up to the cap.
_BOUNDARY_US = st.builds(
    lambda minutes, jitter_us: minutes * MS_PER_MINUTE * 1_000 + jitter_us,
    st.integers(min_value=-2, max_value=PER_RUN_CAP_MINUTES + 2),
    st.integers(min_value=-2_000, max_value=2_000),
)
_DELTA_US = st.one_of(
    st.integers(min_value=-15 * 60 * 10**6, max_value=120 * 60 * 10**6),
    _BOUNDARY_US,
)

# Fixed offsets only: they need no tz database, which Windows may lack.
_TIMEZONES: st.SearchStrategy[tzinfo] = st.sampled_from(
    [
        UTC,
        timezone(timedelta(hours=5, minutes=30)),
        timezone(timedelta(hours=-8)),
        timezone(timedelta(hours=14)),
        timezone(timedelta(hours=-12)),
    ]
)

_STOP_CODES = st.one_of(
    st.none(),
    st.sampled_from(
        [
            TASK_FAILED_TO_START,
            "EssentialContainerExited",
            "UserInitiated",
            "ServiceSchedulerInitiated",
            "SpotInterruption",
            "TerminationNotice",
        ]
    ),
    st.text(max_size=24),
)


def _instant(us: int, tz: tzinfo) -> datetime:
    return (_EPOCH + timedelta(microseconds=us)).astimezone(tz)


@st.composite
def _instants_near(draw: st.DrawFn, anchor_us: int) -> datetime:
    return _instant(anchor_us + draw(_DELTA_US), draw(_TIMEZONES))


@st.composite
def _stopped_tasks(
    draw: st.DrawFn,
) -> tuple[datetime | None, datetime | None, datetime | None, datetime, str | None]:
    """(created_at, pull_started_at, stopped_at, event_time, stop_code) with a start."""
    anchor_us = draw(_ANCHOR_US)
    pull_started_at = draw(st.none() | _instants_near(anchor_us))
    # createdAt is optional only when pullStartedAt supplies the start.
    if pull_started_at is None or draw(st.booleans()):
        created_at: datetime | None = draw(_instants_near(anchor_us))
    else:
        created_at = None
    stopped_at = draw(st.none() | _instants_near(anchor_us))
    event_time = draw(_instants_near(anchor_us))
    stop_code = draw(_STOP_CODES)
    return created_at, pull_started_at, stopped_at, event_time, stop_code


def _parse_utc_ms(value: str) -> datetime:
    assert len(value) == 24 and value.endswith("Z"), value
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Property 25.
# ---------------------------------------------------------------------------


@given(task=_stopped_tasks())
@settings(max_examples=200, deadline=None)
def test_property_python_metering_rule(
    task: tuple[datetime | None, datetime | None, datetime | None, datetime, str | None],
) -> None:
    """Feature: sandbox-minute-plans, Property 25: The Python Metering_Rule.

    For any optional createdAt, optional pullStartedAt, optional stoppedAt,
    event time, and stop code where a start exists, meter_stopped_task uses
    pullStartedAt when present and createdAt otherwise as the start, stoppedAt
    when present and the event time otherwise as the stop, returns 0 billed
    minutes exactly for TaskFailedToStart and
    billed_minutes(start_ms, max(start_ms, stop_ms)) otherwise, returns
    stopped_at >= started_at, and re-metering its own formatted timestamps
    gives the same billed minutes.

    **Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.13, 16.12**
    """
    created_at, pull_started_at, stopped_at, event_time, stop_code = task

    usage = meter_stopped_task(
        created_at=created_at,
        pull_started_at=pull_started_at,
        stopped_at=stopped_at,
        event_time=event_time,
        stop_code=stop_code,
    )

    # Start and stop selection (Req 12.2, 12.4).
    start = pull_started_at if pull_started_at is not None else created_at
    assert start is not None
    stop = stopped_at if stopped_at is not None else event_time
    start_ms = to_epoch_ms(start)
    stop_ms = max(start_ms, to_epoch_ms(stop))

    # The report carries the exact instants metered (Req 12.5).
    assert usage.started_at == format_utc_ms(start_ms)
    assert usage.stopped_at == format_utc_ms(stop_ms)

    # Billed minutes (Req 12.2, 12.3).
    if stop_code == TASK_FAILED_TO_START:
        assert usage.billed_minutes == 0
    else:
        assert usage.billed_minutes == billed_minutes(start_ms, stop_ms)
        # Independent reference: ceil(duration / 60 s), clamped to [1, cap].
        reference = min(
            PER_RUN_CAP_MINUTES,
            max(1, math.ceil(Fraction(stop_ms - start_ms, MS_PER_MINUTE))),
        )
        assert usage.billed_minutes == reference
        assert 1 <= usage.billed_minutes <= PER_RUN_CAP_MINUTES

    # stopped_at >= started_at, as instants and as the wire strings.
    assert usage.started_at is not None
    assert usage.stopped_at is not None
    parsed_start = _parse_utc_ms(usage.started_at)
    parsed_stop = _parse_utc_ms(usage.stopped_at)
    assert parsed_stop >= parsed_start
    assert usage.stopped_at >= usage.started_at
    assert to_epoch_ms(parsed_start) == start_ms
    assert to_epoch_ms(parsed_stop) == stop_ms

    # Re-metering the formatted timestamps (what Trikon_Web checks) agrees.
    remetered = meter_stopped_task(
        created_at=None,
        pull_started_at=parsed_start,
        stopped_at=parsed_stop,
        event_time=parsed_stop,
        stop_code=stop_code,
    )
    assert remetered == usage
    if stop_code != TASK_FAILED_TO_START:
        assert billed_minutes(to_epoch_ms(parsed_start), to_epoch_ms(parsed_stop)) == (
            usage.billed_minutes
        )


# ---------------------------------------------------------------------------
# Shared vectors and examples.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("duration_ms", "expected"), _SHARED_VECTORS)
def test_billed_minutes_shared_vectors(duration_ms: int, expected: int) -> None:
    start_ms = 1_767_225_600_000  # 2026-01-01T00:00:00.000Z
    assert billed_minutes(start_ms, start_ms + duration_ms) == expected


@pytest.mark.parametrize(("duration_ms", "expected"), _SHARED_VECTORS)
def test_meter_stopped_task_shared_vectors(duration_ms: int, expected: int) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    usage = meter_stopped_task(
        created_at=None,
        pull_started_at=start,
        stopped_at=start + timedelta(milliseconds=duration_ms),
        event_time=start,
        stop_code="EssentialContainerExited",
    )
    clamped_stop = start + timedelta(milliseconds=max(0, duration_ms))
    assert usage == MeteredUsage(
        billed_minutes=expected,
        started_at="2026-01-01T00:00:00.000Z",
        stopped_at=format_utc_ms(to_epoch_ms(clamped_stop)),
    )


def test_per_run_cap_matches_runner_wall_time_cap() -> None:
    """**Validates: Requirements 12.14**"""
    assert PER_RUN_CAP_MINUTES * 60 == entrypoint._TASK_WALL_TIME_CAP_SECONDS
    assert entrypoint._TASK_WALL_TIME_CAP_SECONDS == 600


def test_missing_start_raises_metering_input_error() -> None:
    with pytest.raises(MeteringInputError):
        meter_stopped_task(
            created_at=None,
            pull_started_at=None,
            stopped_at=datetime(2026, 1, 1, tzinfo=UTC),
            event_time=datetime(2026, 1, 1, tzinfo=UTC),
            stop_code=None,
        )
