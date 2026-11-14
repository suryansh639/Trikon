"""The Metering_Rule for stopped Sandbox_Tasks (sandbox-minute-plans §10).

Pure: no I/O, no clock. Shared by the Usage_Reporter and its tests (Req 12.13).

It mirrors ``meteredMinutes`` in Trikon_Web's ``src/usage/metering.ts``
exactly. Both sides compute on integer milliseconds, and the Usage_Reporter
sends the same millisecond values it metered, so Trikon_Web's check of a
genuine Usage_Report always agrees::

    billed_minutes(start, stop) = min(10, max(1, ceil(max(0, stop - start) / 60_000)))

A stop earlier than the start counts as zero duration, every run bills at
least one minute, and no run bills more than the Per_Run_Cap.

:data:`PER_RUN_CAP_MINUTES` is the Per_Run_Cap in minutes. It equals the
runner's 600-second wall-time cap (``_TASK_WALL_TIME_CAP_SECONDS`` in
``trikon_cloud/fargate_runner/entrypoint.py``), which stays unchanged
(Req 12.14).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "MS_PER_MINUTE",
    "PER_RUN_CAP_MINUTES",
    "TASK_FAILED_TO_START",
    "MeteredUsage",
    "MeteringInputError",
    "billed_minutes",
    "format_utc_ms",
    "meter_stopped_task",
    "to_epoch_ms",
]

#: Per_Run_Cap in minutes: 600 seconds of runner wall time (Req 12.14).
PER_RUN_CAP_MINUTES = 10

#: Milliseconds in one billed minute.
MS_PER_MINUTE = 60_000

#: ECS stop code of a task that never started; such a task bills 0 minutes (Req 12.3).
TASK_FAILED_TO_START = "TaskFailedToStart"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class MeteringInputError(ValueError):
    """The stopped-task data cannot be metered (no start time, or a naive datetime).

    The Usage_Reporter lets this propagate, so the invocation fails and the
    event goes through Lambda's async retries to the dead-letter destination.
    """


@dataclass(frozen=True)
class MeteredUsage:
    """The metered result for one stopped Sandbox_Task.

    ``started_at`` and ``stopped_at`` are the exact instants the Metering_Rule
    used, formatted as ``YYYY-MM-DDTHH:MM:SS.mmmZ``. They are ``None`` only for
    the Orchestrator's 0-minute release report, which this module never builds.
    """

    billed_minutes: int
    started_at: str | None
    stopped_at: str | None


def to_epoch_ms(value: datetime) -> int:
    """Return ``value`` as whole Unix epoch milliseconds, rounded down.

    Integer arithmetic only, so no float rounding creeps in. Raises
    :class:`MeteringInputError` for a naive datetime, whose instant is unknown.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise MeteringInputError("timestamp must be timezone-aware")
    delta = value - _EPOCH
    # timedelta normalises to days, 0 <= seconds < 86400, 0 <= microseconds < 10**6,
    # so floor division of microseconds floors the whole value, negatives included.
    return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000


def format_utc_ms(ms: int) -> str:
    """Format epoch milliseconds as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC.

    The output matches JavaScript's ``Date.prototype.toISOString`` for years
    0001 to 9999.
    """
    instant = _EPOCH + timedelta(milliseconds=ms)
    return (
        f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}"
        f"T{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}"
        f".{instant.microsecond // 1_000:03d}Z"
    )


def billed_minutes(start_ms: int, stop_ms: int) -> int:
    """Billed minutes for a task that ran from ``start_ms`` to ``stop_ms``.

    The duration is clamped at zero, rounded up to whole minutes, and clamped
    to ``[1, PER_RUN_CAP_MINUTES]``. The result is non-decreasing in ``stop_ms``.
    """
    duration = max(0, stop_ms - start_ms)
    return min(PER_RUN_CAP_MINUTES, max(1, -(-duration // MS_PER_MINUTE)))


def meter_stopped_task(
    *,
    created_at: datetime | None,
    pull_started_at: datetime | None,
    stopped_at: datetime | None,
    event_time: datetime,
    stop_code: str | None,
) -> MeteredUsage:
    """Apply the Metering_Rule to one stopped Sandbox_Task (Req 12.2-12.4).

    * Start: ``pullStartedAt``, or ``createdAt`` when it is absent (Req 12.2).
    * Stop: ``stoppedAt``, or the event time when it is absent (Req 12.4). A
      stop before the start is clamped to the start, so Trikon_Web never sees
      a report whose stop precedes its start.
    * A task stopped with ``TaskFailedToStart`` bills 0 minutes (Req 12.3).

    Raises:
        MeteringInputError: Neither ``pullStartedAt`` nor ``createdAt`` is
            present, or a datetime is naive.
    """
    start = pull_started_at if pull_started_at is not None else created_at
    stop = stopped_at if stopped_at is not None else event_time
    if start is None:
        raise MeteringInputError("stopped task has neither pullStartedAt nor createdAt")
    start_ms = to_epoch_ms(start)
    stop_ms = max(start_ms, to_epoch_ms(stop))
    billed = 0 if stop_code == TASK_FAILED_TO_START else billed_minutes(start_ms, stop_ms)
    return MeteredUsage(
        billed_minutes=billed,
        started_at=format_utc_ms(start_ms),
        stopped_at=format_utc_ms(stop_ms),
    )
