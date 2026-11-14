"""Check Run copy for quota Fail_Closed_Results (sandbox-minute-plans).

Pure builders for the :class:`FailClosedNotice` values that
:func:`~trikon_cloud.orchestrator.never_fail_open.write_orchestrator_failure_verdict`
posts when the orchestrator does not run the hosted check (design.md,
"Check Run copy"; Requirements 11.4-11.6, 11.8-11.10, 11.20):

* :func:`denial_notice` — a ``denied`` quota outcome
  (``not_connected``, ``minutes_exhausted``, ``no_plan``).
* :func:`usage_unavailable_notice` — the Usage_API could not answer
  (network, timeout, non-2xx, malformed body).
* :func:`busy_timeout_notice` — a ``busy`` outcome after the Job waited
  longer than the Queue_Wait_Limit.
* :func:`format_reset_date` — the UTC reset date in the minutes-exhausted
  summary, e.g. ``"Jul 1, 2025"``.
* :func:`spot_fail_closed_notice` and
  :func:`spot_retry_dispatch_failed_notice` — Spot-related
  Fail_Closed_Results (trikon-cloud-cost-cuts Requirement 11). They use
  ``error_class="spot_interruption"`` and ``"terminal"`` respectively.

Every notice uses ``conclusion="action_required"`` (GitHub treats
``neutral`` as passing for required checks). Quota notices use
``error_class="quota"``.
``error_code`` is the denial reason, ``"busy_timeout"``, or the
unavailable cause. The copy below matches the design table byte for
byte; Invariant 7: the product name is "Trikon".

The module does no IO and reads no clock or environment; callers pass
``app_url`` (``TRIKON_WEB_APP_URL``) and the rendered details URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from trikon_cloud.orchestrator.never_fail_open import FailClosedNotice

__all__ = [  # noqa: RUF022
    "denial_notice",
    "usage_unavailable_notice",
    "busy_timeout_notice",
    "spot_fail_closed_notice",
    "spot_retry_dispatch_failed_notice",
    "format_reset_date",
    "DenialReason",
    "UnavailableCause",
    "SpotFailReason",
]

#: Denial reasons the Quota_Endpoint may return (``QuotaDenied.reason``).
DenialReason = Literal["not_connected", "minutes_exhausted", "no_plan"]

#: Causes of a ``QuotaUnavailable`` decision (``QuotaUnavailable.cause``).
UnavailableCause = Literal["network", "timeout", "http_status", "malformed"]

#: Why the Spot_Retry_Handler fails closed instead of re-queuing
#: (trikon-cloud-cost-cuts design, "Decision (pure)"; Requirement 11).
SpotFailReason = Literal[
    "retry_exhausted",
    "retry_job_malformed",
    "interim_without_retry",
    "retry_send_failed",
    "retry_claim_stale",
]

_ERROR_CLASS: str = "quota"
_CONCLUSION: Literal["action_required"] = "action_required"

# Connect_URL and Upgrade_URL paths on the Trikon_Web_App_URL.
_CONNECT_PATH: str = "/dashboard/installations"
_UPGRADE_PATH: str = "/account"

# Fixed English month abbreviations; independent of the process locale.
_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# ---------------------------------------------------------------------------
# Copy (design.md "Check Run copy" table).
# ---------------------------------------------------------------------------

_NOT_CONNECTED_RULE: str = "installation not connected"
_NOT_CONNECTED_TITLE: str = "Connect your Trikon account"
_NOT_CONNECTED_SUMMARY: str = (
    "This GitHub App installation is not connected to a Trikon account, "
    "so Trikon Cloud did not run the hosted check. Connect the installation "
    "to your Trikon account; new pushes to this pull request then run the check."
)

_EXHAUSTED_RULE: str = "sandbox minutes exhausted"
_EXHAUSTED_TITLE: str = "Monthly sandbox minutes used up"
_EXHAUSTED_SUMMARY_TEMPLATE: str = (
    "This Trikon account has used all of its sandbox minutes for the current "
    "period, so Trikon Cloud did not run the hosted check. Minutes reset on "
    "{reset_date} (UTC). Upgrading your plan raises the allowance immediately."
)

_NO_PLAN_RULE: str = "plan unavailable"
_NO_PLAN_TITLE: str = "Trikon plan unavailable"
_NO_PLAN_SUMMARY: str = (
    "Trikon could not determine the plan for this account, so Trikon Cloud "
    "did not run the hosted check. Check the plan on your Trikon account page."
)

_BUSY_RULE: str = "concurrency limit reached"
_BUSY_TITLE: str = "PR check couldn't start: too many checks in progress"
_BUSY_SUMMARY_TEMPLATE: str = (
    "This Trikon account's plan runs {limit_text} at a time, and this check "
    "waited more than {wait_text} for a free slot, so Trikon Cloud did not run "
    "the hosted check. New pushes to this pull request run the check again. "
    "Upgrading your plan raises the number of checks that run at once."
)
_BUSY_ERROR_CODE: str = "busy_timeout"

_UNAVAILABLE_RULE: str = "usage service unavailable"
_UNAVAILABLE_TITLE: str = "Trikon Cloud verification unavailable"
_UNAVAILABLE_SUMMARY_TEMPLATE: str = (
    "Trikon Cloud could not confirm the sandbox minutes for this pull request, "
    "so it did not run the hosted check. A human reviewer should review this "
    "change. Delivery id: {delivery_id}."
)

# Spot-related Fail_Closed_Results (trikon-cloud-cost-cuts design,
# "Fail-closed publishing"; Requirements 11.1-11.5, 11.7, D8).
_SPOT_FAIL_CLOSED_RULE: str = "spot interruption not retried"
_SPOT_DISPATCH_FAILED_RULE: str = "spot retry dispatch failed"
_SPOT_TITLE: str = "Trikon verification interrupted"
_SPOT_SUMMARY_TEMPLATE: str = (
    "The Trikon Cloud runner for this pull request was interrupted and could "
    "not be retried. A human reviewer must decide. Delivery id: {delivery_id}."
)
_SPOT_ERROR_CLASS: str = "spot_interruption"
_SPOT_DISPATCH_ERROR_CLASS: str = "terminal"
_UNKNOWN_DELIVERY_ID: str = "unknown"


def format_reset_date(period_end: datetime) -> str:
    """Render ``period_end`` as a UTC date such as ``"Jul 1, 2025"``.

    An aware datetime is converted to UTC first. A naive datetime is
    taken as already UTC, so the result never depends on the host's
    local timezone. The form matches the Account page's ``Intl``
    ``dateStyle: "medium"`` in English.
    """
    if period_end.tzinfo is None:
        utc = period_end.replace(tzinfo=UTC)
    else:
        utc = period_end.astimezone(UTC)
    return f"{_MONTHS[utc.month - 1]} {utc.day}, {utc.year}"


def denial_notice(
    reason: DenialReason,
    period_end: datetime | None,
    app_url: str,
) -> FailClosedNotice:
    """Build the notice for a ``denied`` quota outcome (Req 11.4-11.6).

    ``not_connected`` links to the Connect_URL; ``minutes_exhausted`` and
    ``no_plan`` link to the Upgrade_URL.

    Raises
    ------
    ValueError
        ``reason`` is ``minutes_exhausted`` and ``period_end`` is
        ``None`` (the Usage_API client only yields that reason with a
        parsed ``period_end``), or ``reason`` is not a known denial
        reason.
    """
    if reason == "not_connected":
        return FailClosedNotice(
            matched_rule=_NOT_CONNECTED_RULE,
            title=_NOT_CONNECTED_TITLE,
            summary=_NOT_CONNECTED_SUMMARY,
            details_url=app_url + _CONNECT_PATH,
            conclusion=_CONCLUSION,
            error_class=_ERROR_CLASS,
            error_code=reason,
        )
    if reason == "minutes_exhausted":
        if period_end is None:
            msg = "minutes_exhausted denial requires period_end"
            raise ValueError(msg)
        return FailClosedNotice(
            matched_rule=_EXHAUSTED_RULE,
            title=_EXHAUSTED_TITLE,
            summary=_EXHAUSTED_SUMMARY_TEMPLATE.format(reset_date=format_reset_date(period_end)),
            details_url=app_url + _UPGRADE_PATH,
            conclusion=_CONCLUSION,
            error_class=_ERROR_CLASS,
            error_code=reason,
        )
    if reason == "no_plan":
        return FailClosedNotice(
            matched_rule=_NO_PLAN_RULE,
            title=_NO_PLAN_TITLE,
            summary=_NO_PLAN_SUMMARY,
            details_url=app_url + _UPGRADE_PATH,
            conclusion=_CONCLUSION,
            error_class=_ERROR_CLASS,
            error_code=reason,
        )
    msg = f"unknown denial reason: {reason!r}"
    raise ValueError(msg)


def usage_unavailable_notice(
    delivery_id: str,
    details_url: str,
    *,
    cause: UnavailableCause,
) -> FailClosedNotice:
    """Build the notice for a Usage_API failure (Req 11.8).

    ``details_url`` is the rendered ``TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE``
    (the same URL the RunTask-terminal Check Run uses). ``cause`` is
    ``QuotaUnavailable.cause`` and becomes the evidence ``error_code``.
    """
    return FailClosedNotice(
        matched_rule=_UNAVAILABLE_RULE,
        title=_UNAVAILABLE_TITLE,
        summary=_UNAVAILABLE_SUMMARY_TEMPLATE.format(delivery_id=delivery_id),
        details_url=details_url,
        conclusion=_CONCLUSION,
        error_class=_ERROR_CLASS,
        error_code=cause,
    )


def _limit_text(concurrency_limit: int) -> str:
    """``"1 PR check"`` for 1, ``"{n} PR checks"`` above 1."""
    if concurrency_limit == 1:
        return "1 PR check"
    return f"{concurrency_limit} PR checks"


def _wait_text(wait_limit_seconds: int) -> str:
    """Whole minutes (``"30 minutes"``, ``"1 minute"``), else ``"{s} seconds"``."""
    minutes, remainder = divmod(wait_limit_seconds, 60)
    if remainder != 0:
        return f"{wait_limit_seconds} seconds"
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def busy_timeout_notice(
    concurrency_limit: int,
    wait_limit_seconds: int,
    app_url: str,
) -> FailClosedNotice:
    """Build the notice for a ``busy`` Job past the Queue_Wait_Limit (Req 11.20).

    ``concurrency_limit`` is ``QuotaBusy.concurrency_limit``;
    ``wait_limit_seconds`` is ``TRIKON_QUEUE_WAIT_LIMIT_SECONDS``.

    Raises
    ------
    ValueError
        ``concurrency_limit`` or ``wait_limit_seconds`` is below 1.
    """
    if concurrency_limit < 1:
        msg = f"concurrency_limit must be at least 1, got {concurrency_limit}"
        raise ValueError(msg)
    if wait_limit_seconds < 1:
        msg = f"wait_limit_seconds must be at least 1, got {wait_limit_seconds}"
        raise ValueError(msg)
    return FailClosedNotice(
        matched_rule=_BUSY_RULE,
        title=_BUSY_TITLE,
        summary=_BUSY_SUMMARY_TEMPLATE.format(
            limit_text=_limit_text(concurrency_limit),
            wait_text=_wait_text(wait_limit_seconds),
        ),
        details_url=app_url + _UPGRADE_PATH,
        conclusion=_CONCLUSION,
        error_class=_ERROR_CLASS,
        error_code=_BUSY_ERROR_CODE,
    )


def _spot_summary(delivery_id: str | None) -> str:
    """The fixed Spot summary; a missing or blank delivery id renders ``unknown``."""
    if delivery_id is None or not delivery_id.strip():
        rendered = _UNKNOWN_DELIVERY_ID
    else:
        rendered = delivery_id
    return _SPOT_SUMMARY_TEMPLATE.format(delivery_id=rendered)


def spot_fail_closed_notice(
    reason: SpotFailReason,
    delivery_id: str | None,
    details_url: str,
) -> FailClosedNotice:
    """Build the Spot_Retry_Handler's Fail_Closed_Result (Req 11.1-11.4).

    ``reason`` becomes the evidence ``error_code``; ``error_class`` is
    ``"spot_interruption"``. ``delivery_id`` is ``None`` when the
    interrupted task's overrides do not carry one, and the summary then
    says ``unknown``. The conclusion is ``action_required`` (D8).
    """
    return FailClosedNotice(
        matched_rule=_SPOT_FAIL_CLOSED_RULE,
        title=_SPOT_TITLE,
        summary=_spot_summary(delivery_id),
        details_url=details_url,
        conclusion=_CONCLUSION,
        error_class=_SPOT_ERROR_CLASS,
        error_code=reason,
    )


def spot_retry_dispatch_failed_notice(
    delivery_id: str | None,
    details_url: str,
    aws_error_code: str,
) -> FailClosedNotice:
    """Build the Orchestrator's Fail_Closed_Result for a failed Retry_Job dispatch (Req 11.5).

    ``aws_error_code`` is the RunTask ``ClientError`` code (or
    ``"RunTaskNoTask"``) and becomes the evidence ``error_code``;
    ``error_class`` is ``"terminal"``. The conclusion is
    ``action_required`` (D8).
    """
    return FailClosedNotice(
        matched_rule=_SPOT_DISPATCH_FAILED_RULE,
        title=_SPOT_TITLE,
        summary=_spot_summary(delivery_id),
        details_url=details_url,
        conclusion=_CONCLUSION,
        error_class=_SPOT_DISPATCH_ERROR_CLASS,
        error_code=aws_error_code,
    )
