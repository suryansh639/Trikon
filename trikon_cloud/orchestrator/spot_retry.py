"""Pure decision logic for the Spot_Retry_Handler (cost-cuts Req 9, 11).

No I/O, no clock, no logging. The Spot_Retry_Handler Lambda
(``spot_retry_handler.py``) parses each EventBridge "ECS Task State
Change" event into
:class:`~trikon_cloud.usage_reporter.models.EcsTaskStateChangeEvent` and
asks :func:`decide_stopped_task` what to do with it:

* :class:`Ignore` -- not a stopped Sandbox_Task, or a stop that is neither
  a Spot_Interruption nor a Runner exit after an Interim_Check_Run
  (Req 9.6).
* :class:`Requeue` -- a Spot_Interruption of an Attempt 1 task whose
  Runner_Overrides build a valid Retry_Job (Req 9.2).
* :class:`FailClosed` -- the pull request must get a Fail_Closed_Result
  (Req 11.2, 11.3, 11.4). The handler also builds this decision at
  runtime for ``retry_send_failed`` and ``retry_claim_stale``.
* :class:`Unroutable` -- the event needs action but the installation,
  repository or head SHA is unknown, so no Check Run can be posted. The
  handler raises :class:`SpotEventError` so the event is retried and kept
  in the dead-letter destination (Req 11.4).

Secret hygiene (Req 9.8): pydantic error text echoes input values, so a
:class:`pydantic.ValidationError` raised while parsing overrides is
swallowed here and never logged, re-raised or chained. Override values
are read only through the typed event model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import (
    ATTEMPT_ENV,
    MAX_ATTEMPTS,
    RESERVATION_ID_ENV,
    RUN_ID_ENV,
    RUNNER_EXIT_INTERIM,
    SPOT_INTERRUPTION_STOP_CODE,
)
from trikon_cloud.orchestrator.models import VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import FailClosedTarget
from trikon_cloud.usage_client.metering import format_utc_ms, to_epoch_ms
from trikon_cloud.usage_reporter.models import EcsTaskDetail, EcsTaskStateChangeEvent
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# Ordering walks the decision types, then the functions. Not isort alphabetical.
__all__ = [  # noqa: RUF022
    "SpotFailReason",
    "IgnoreReason",
    "Ignore",
    "Requeue",
    "FailClosed",
    "Unroutable",
    "StoppedTaskDecision",
    "SpotEventError",
    "decide_stopped_task",
    "build_retry_job",
    "retry_sent_at",
]

#: Why a Spot-related Fail_Closed_Result is published. The last two are
#: produced by the handler at runtime, never by :func:`decide_stopped_task`.
SpotFailReason = Literal[
    "retry_exhausted",  # Spot_Interruption on Attempt 2 (Req 11.2)
    "retry_job_malformed",  # overrides cannot build a Retry_Job (Req 11.4)
    "interim_without_retry",  # not a Spot_Interruption but runner exited 75 (Req 11.3)
    "retry_send_failed",  # SendMessage failed (Req 11.1), runtime only
    "retry_claim_stale",  # an earlier claimant died before confirming, runtime only
]

IgnoreReason = Literal["not_sandbox_task", "not_interrupted"]

# Runner_Overrides names set by ``ecs_dispatcher.build_run_task_call``. The
# Run_Id, Reservation_Id and Attempt names come from ``spot_contract``.
_INSTALLATION_ID_ENV: Final = "TRIKON_INSTALLATION_ID"
_REPO_FULL_NAME_ENV: Final = "TRIKON_REPO_FULL_NAME"
_PR_NUMBER_ENV: Final = "TRIKON_PR_NUMBER"
_HEAD_SHA_ENV: Final = "TRIKON_HEAD_SHA"
_BASE_SHA_ENV: Final = "TRIKON_BASE_SHA"
_EVENT_TYPE_ENV: Final = "TRIKON_EVENT_TYPE"

#: ``TRIKON_ATTEMPT`` values, as ``build_run_task_call`` writes them (``str(attempt)``).
_FIRST_ATTEMPT: Final = "1"
_LAST_ATTEMPT: Final = str(MAX_ATTEMPTS)

# Canonical positive decimal as ``str(int)`` writes it: ASCII digits only, no
# sign, whitespace, underscores or leading zero. 19 digits keeps the value in
# the signed 64-bit range of GitHub ids.
_POSITIVE_INT: Final = re.compile(r"[1-9][0-9]{0,18}")
_COMMIT_SHA: Final = re.compile(r"[0-9a-f]{40}")


# ---------------------------------------------------------------------------
# Decision types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ignore:
    """Nothing to do: no Job_Message and no Check Run (Req 9.6)."""

    reason: IgnoreReason


@dataclass(frozen=True)
class Requeue:
    """Send ``job`` (the Retry_Job) to the Verify_Jobs_Queue (Req 9.2, 9.3).

    ``target`` and ``row_message`` let the handler fail closed instead when
    the send fails or the claim is stale (Req 11.1).
    """

    job: VerifyJobMessage
    target: FailClosedTarget
    row_message: SqsJobMessage


@dataclass(frozen=True)
class FailClosed:
    """Publish a Fail_Closed_Result on ``target`` (Req 11.1-11.4).

    ``row_message`` is ``None`` when the overrides cannot build a
    :class:`SqsJobMessage`; the handler then posts the Check Run without a
    synthetic verdict row.
    """

    reason: SpotFailReason
    target: FailClosedTarget
    row_message: SqsJobMessage | None


@dataclass(frozen=True)
class Unroutable:
    """The event needs action, but no pull request head can be addressed (Req 11.4)."""

    reason: Literal["identity_unknown"] = "identity_unknown"


StoppedTaskDecision = Ignore | Requeue | FailClosed | Unroutable


class SpotEventError(Exception):
    """A Task_Stopped_Event the Spot_Retry_Handler cannot act on.

    Raised by the handler so Lambda retries the event and finally keeps it
    in the dead-letter destination (Req 11.4). Messages are fixed strings
    and never carry event or pydantic error text (Req 9.8).
    """


# ---------------------------------------------------------------------------
# Override parsing.
# ---------------------------------------------------------------------------


def _positive_int(raw: str | None) -> int | None:
    """Parse a canonical positive decimal, or return ``None``."""
    if raw is None or _POSITIVE_INT.fullmatch(raw) is None:
        return None
    return int(raw)


def _parse_target(detail: EcsTaskDetail) -> FailClosedTarget | None:
    """Return the pull request head named by the Runner_Overrides, if routable.

    Requires an installation id >= 1, a non-empty repository full name and
    a 40-character lowercase hex head SHA. ``pr_number`` is optional and is
    ``None`` when absent or invalid.
    """
    installation_id = _positive_int(detail.runner_env(_INSTALLATION_ID_ENV))
    repo_full_name = detail.runner_env(_REPO_FULL_NAME_ENV)
    head_sha = detail.runner_env(_HEAD_SHA_ENV)
    if (
        installation_id is None
        or not repo_full_name
        or head_sha is None
        or _COMMIT_SHA.fullmatch(head_sha) is None
    ):
        return None
    return FailClosedTarget(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        pr_number=_positive_int(detail.runner_env(_PR_NUMBER_ENV)),
    )


def _parse_row_message(detail: EcsTaskDetail, *, sent_at: str) -> SqsJobMessage | None:
    """Rebuild the Job_Message from the Runner_Overrides, or return ``None``.

    The overrides carry every :class:`SqsJobMessage` field except
    ``sent_at``, which the caller supplies (:func:`retry_sent_at`). Any
    missing or invalid field gives ``None``. A ``ValidationError`` is
    swallowed without being logged, because its text echoes input values.
    """
    installation_id = _positive_int(detail.runner_env(_INSTALLATION_ID_ENV))
    pr_number = _positive_int(detail.runner_env(_PR_NUMBER_ENV))
    repo_full_name = detail.runner_env(_REPO_FULL_NAME_ENV)
    head_sha = detail.runner_env(_HEAD_SHA_ENV)
    base_sha = detail.runner_env(_BASE_SHA_ENV)
    event_type = detail.runner_env(_EVENT_TYPE_ENV)
    delivery_id = detail.runner_env(RUN_ID_ENV)
    if (
        installation_id is None
        or pr_number is None
        or repo_full_name is None
        or head_sha is None
        or base_sha is None
        or event_type is None
        or delivery_id is None
    ):
        return None
    try:
        return SqsJobMessage(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            event_type=event_type,
            sent_at=sent_at,
            delivery_id=delivery_id,
        )
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Public pure functions.
# ---------------------------------------------------------------------------


def retry_sent_at(event: EcsTaskStateChangeEvent) -> str:
    """Return the Retry_Job ``sent_at``: the stop time in ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    ``detail.stoppedAt``, or the event time when it is absent. Deterministic
    per event, so a re-delivered event builds the same Retry_Job and the
    same synthetic-verdict ``sk``.
    """
    stopped = event.detail.stoppedAt
    return format_utc_ms(to_epoch_ms(stopped if stopped is not None else event.time))


def build_retry_job(
    row: SqsJobMessage, *, reservation_id: str | None
) -> VerifyJobMessage | None:
    """Return the Attempt 2 Retry_Job for ``row``, or ``None`` if it does not validate.

    The Retry_Job keeps every ``row`` field, including the Run_Id
    (``delivery_id``), and carries ``reservation_id`` (Req 9.2). A missing
    or non-UUID Reservation_Id gives ``None``; the validation error is not
    logged.
    """
    if reservation_id is None:
        return None
    try:
        return VerifyJobMessage.model_validate(
            {**row.model_dump(), "attempt": MAX_ATTEMPTS, "reservation_id": reservation_id}
        )
    except ValidationError:
        return None


def decide_stopped_task(event: EcsTaskStateChangeEvent) -> StoppedTaskDecision:
    """Decide what the Spot_Retry_Handler does with one Task_Stopped_Event.

    Deterministic: the same event always gives an equal decision.
    """
    detail = event.detail
    if not detail.is_stopped_sandbox_task():
        return Ignore("not_sandbox_task")
    spot = detail.stopCode == SPOT_INTERRUPTION_STOP_CODE
    interim_exit = detail.runner_exit_code() == RUNNER_EXIT_INTERIM
    if not spot and not interim_exit:
        return Ignore("not_interrupted")  # Req 9.6
    target = _parse_target(detail)
    if target is None:
        return Unroutable()  # Req 11.4: the handler raises
    row = _parse_row_message(detail, sent_at=retry_sent_at(event))
    if not spot:
        return FailClosed("interim_without_retry", target, row)  # Req 11.3
    attempt = detail.runner_env(ATTEMPT_ENV)
    if attempt == _LAST_ATTEMPT:
        return FailClosed("retry_exhausted", target, row)  # Req 11.2
    if attempt != _FIRST_ATTEMPT or row is None:
        return FailClosed("retry_job_malformed", target, row)  # Req 11.4
    job = build_retry_job(row, reservation_id=detail.runner_env(RESERVATION_ID_ENV))
    if job is None:
        return FailClosed("retry_job_malformed", target, row)  # Req 11.4
    return Requeue(job, target, row)  # Req 9.2
