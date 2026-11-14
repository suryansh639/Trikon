"""Tests for the quota Check Run copy (sandbox-minute-plans, Property 29).

The expected strings are copied from design.md ("Check Run copy" table),
not imported from ``quota_notices``, so drift between the module and the
design fails here. The Check Run body (``name="Trikon"``,
``status="completed"``) for a notice is covered in
``test_never_fail_open.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.orchestrator.never_fail_open import FailClosedNotice
from trikon_cloud.orchestrator.quota_notices import (
    DenialReason,
    SpotFailReason,
    UnavailableCause,
    busy_timeout_notice,
    denial_notice,
    format_reset_date,
    spot_fail_closed_notice,
    spot_retry_dispatch_failed_notice,
    usage_unavailable_notice,
)

# ---------------------------------------------------------------------------
# Design table copy (design.md, Section 9 "Check Run copy").
# ---------------------------------------------------------------------------

_NOT_CONNECTED_SUMMARY = (
    "This GitHub App installation is not connected to a Trikon account, so Trikon Cloud "
    "did not run the hosted check. Connect the installation to your Trikon account; new "
    "pushes to this pull request then run the check."
)
_EXHAUSTED_SUMMARY = (
    "This Trikon account has used all of its sandbox minutes for the current period, so "
    "Trikon Cloud did not run the hosted check. Minutes reset on {reset_date} (UTC). "
    "Upgrading your plan raises the allowance immediately."
)
_NO_PLAN_SUMMARY = (
    "Trikon could not determine the plan for this account, so Trikon Cloud did not run "
    "the hosted check. Check the plan on your Trikon account page."
)
_BUSY_SUMMARY = (
    "This Trikon account's plan runs {limit_text} at a time, and this check waited more "
    "than {wait_text} for a free slot, so Trikon Cloud did not run the hosted check. New "
    "pushes to this pull request run the check again. Upgrading your plan raises the "
    "number of checks that run at once."
)
_UNAVAILABLE_SUMMARY = (
    "Trikon Cloud could not confirm the sandbox minutes for this pull request, so it did "
    "not run the hosted check. A human reviewer should review this change. "
    "Delivery id: {delivery_id}."
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ---------------------------------------------------------------------------
# Independent oracles.
# ---------------------------------------------------------------------------


def _expected_reset_date(period_end: datetime) -> str:
    """UTC "Mon D, YYYY" via the offset arithmetic, not ``astimezone``."""
    offset = period_end.utcoffset()
    assert offset is not None
    utc = period_end.replace(tzinfo=None) - offset
    return f"{_MONTHS[utc.month - 1]} {utc.day}, {utc.year}"


def _expected_limit_text(limit: int) -> str:
    return "1 PR check" if limit == 1 else f"{limit} PR checks"


def _expected_wait_text(seconds: int) -> str:
    if seconds % 60 != 0:
        return f"{seconds} seconds"
    minutes = seconds // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _assert_quota_evidence(notice: FailClosedNotice, error_code: str) -> None:
    assert notice.conclusion == "action_required"
    assert notice.error_class == "quota"
    assert notice.error_code == error_code


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_LABEL = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)
_PATH_SEGMENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=10)


@st.composite
def _app_urls(draw: st.DrawFn) -> str:
    """https base URLs with an optional port and path, never a trailing slash."""
    host = ".".join(draw(st.lists(_LABEL, min_size=1, max_size=4)))
    port = draw(st.none() | st.integers(min_value=1, max_value=65_535))
    path = "".join("/" + seg for seg in draw(st.lists(_PATH_SEGMENT, max_size=2)))
    url = f"https://{host}{'' if port is None else f':{port}'}{path}"
    assert not url.endswith("/")
    return url


# Fixed offsets only (no tz database needed on Windows), whole minutes in
# the open interval (-24h, +24h) that ``datetime.timezone`` accepts.
_TIMEZONES = st.integers(min_value=-(24 * 60 - 1), max_value=24 * 60 - 1).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
_PERIOD_ENDS = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2200, 12, 31, 23, 59, 59),
    timezones=st.one_of(st.just(UTC), _TIMEZONES),
)
_DELIVERY_IDS = st.one_of(
    st.uuids().map(str),
    st.text(min_size=1, max_size=64),
)
_DETAILS_URLS = _app_urls().map(lambda base: base + "/verdicts?delivery=x")
_CONCURRENCY_LIMITS = st.one_of(
    st.sampled_from([1, 5, 10]),
    st.integers(min_value=1, max_value=100_000),
)
_WAIT_LIMITS = st.one_of(
    st.sampled_from([60, 61, 90, 119, 120, 1800, 86_340, 86_399, 86_400]),
    st.integers(min_value=1, max_value=1440).map(lambda minutes: minutes * 60),
    st.integers(min_value=60, max_value=86_400),
)

# ---------------------------------------------------------------------------
# Property 29.
# ---------------------------------------------------------------------------


@given(
    reason=st.sampled_from(["not_connected", "minutes_exhausted", "no_plan"]),
    period_end=_PERIOD_ENDS,
    app_url=_app_urls(),
    delivery_id=_DELIVERY_IDS,
    details_url=_DETAILS_URLS,
    cause=st.sampled_from(["network", "timeout", "http_status", "malformed"]),
    concurrency_limit=_CONCURRENCY_LIMITS,
    wait_limit_seconds=_WAIT_LIMITS,
)
@settings(max_examples=200, deadline=None)
def test_property_fail_closed_notices(
    reason: DenialReason,
    period_end: datetime,
    app_url: str,
    delivery_id: str,
    details_url: str,
    cause: UnavailableCause,
    concurrency_limit: int,
    wait_limit_seconds: int,
) -> None:
    """Feature: sandbox-minute-plans, Property 29: Fail-closed notices.

    For any denial reason, period_end, delivery id, concurrency_limit ≥ 1,
    Queue_Wait_Limit, and https app URL, the notice builder returns the
    Section 9 row for that cause: title and matched rule; details URL
    app_url + "/dashboard/installations" for not_connected and
    app_url + "/account" for minutes_exhausted, no_plan, and the busy
    timeout; a minutes_exhausted summary with format_reset_date(period_end)
    in UTC and the upgrade sentence; a busy-timeout notice whose summary
    names the limit and the wait; conclusion action_required.

    **Validates: Requirements 11.4, 11.5, 11.6, 11.9, 11.10, 11.20**
    """
    connect_url = app_url + "/dashboard/installations"
    upgrade_url = app_url + "/account"

    # format_reset_date: UTC "Mon D, YYYY" with fixed English months.
    reset_date = format_reset_date(period_end)
    assert reset_date == _expected_reset_date(period_end)

    # Denials (Req 11.4-11.6). period_end is passed for every reason; only
    # minutes_exhausted uses it.
    denial = denial_notice(reason, period_end, app_url)
    _assert_quota_evidence(denial, reason)
    if reason == "not_connected":
        assert denial == FailClosedNotice(
            matched_rule="installation not connected",
            title="Connect your Trikon account",
            summary=_NOT_CONNECTED_SUMMARY,
            details_url=connect_url,
            conclusion="action_required",
            error_class="quota",
            error_code="not_connected",
        )
    elif reason == "minutes_exhausted":
        assert denial == FailClosedNotice(
            matched_rule="sandbox minutes exhausted",
            title="Monthly sandbox minutes used up",
            summary=_EXHAUSTED_SUMMARY.format(reset_date=reset_date),
            details_url=upgrade_url,
            conclusion="action_required",
            error_class="quota",
            error_code="minutes_exhausted",
        )
        assert f"Minutes reset on {reset_date} (UTC)." in denial.summary
        assert "Upgrading your plan raises the allowance immediately." in denial.summary
    else:
        assert denial == FailClosedNotice(
            matched_rule="plan unavailable",
            title="Trikon plan unavailable",
            summary=_NO_PLAN_SUMMARY,
            details_url=upgrade_url,
            conclusion="action_required",
            error_class="quota",
            error_code="no_plan",
        )

    # Busy past the Queue_Wait_Limit (Req 11.20).
    busy = busy_timeout_notice(concurrency_limit, wait_limit_seconds, app_url)
    limit_text = _expected_limit_text(concurrency_limit)
    assert busy == FailClosedNotice(
        matched_rule="concurrency limit reached",
        title="PR check couldn't start: too many checks in progress",
        summary=_BUSY_SUMMARY.format(
            limit_text=limit_text,
            wait_text=_expected_wait_text(wait_limit_seconds),
        ),
        details_url=upgrade_url,
        conclusion="action_required",
        error_class="quota",
        error_code="busy_timeout",
    )
    assert f"runs {limit_text} at a time" in busy.summary

    # Usage_API unavailable: the details URL passes through unchanged.
    unavailable = usage_unavailable_notice(delivery_id, details_url, cause=cause)
    assert unavailable == FailClosedNotice(
        matched_rule="usage service unavailable",
        title="Trikon Cloud verification unavailable",
        summary=_UNAVAILABLE_SUMMARY.format(delivery_id=delivery_id),
        details_url=details_url,
        conclusion="action_required",
        error_class="quota",
        error_code=cause,
    )


# ---------------------------------------------------------------------------
# Examples.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wait_limit_seconds", "wait_text"),
    [(1800, "30 minutes"), (60, "1 minute"), (90, "90 seconds")],
)
def test_busy_timeout_wait_text_examples(wait_limit_seconds: int, wait_text: str) -> None:
    """Feature: sandbox-minute-plans, Requirement 11.20 (wait_text examples)."""
    notice = busy_timeout_notice(1, wait_limit_seconds, "https://trikon.example.com")
    assert notice.summary == _BUSY_SUMMARY.format(limit_text="1 PR check", wait_text=wait_text)
    assert notice.details_url == "https://trikon.example.com/account"


def test_busy_timeout_limit_text_plural_example() -> None:
    """Feature: sandbox-minute-plans, Requirement 11.20 (limit_text above 1)."""
    notice = busy_timeout_notice(5, 1800, "https://trikon.example.com")
    assert "runs 5 PR checks at a time" in notice.summary
    assert "waited more than 30 minutes for a free slot" in notice.summary


def test_format_reset_date_converts_to_utc() -> None:
    """Feature: sandbox-minute-plans, Requirement 11.5 (UTC reset date)."""
    # 2025-07-01T02:00+05:30 is 2025-06-30T20:30Z.
    ist = timezone(timedelta(hours=5, minutes=30))
    assert format_reset_date(datetime(2025, 7, 1, 2, 0, tzinfo=ist)) == "Jun 30, 2025"
    assert format_reset_date(datetime(2025, 7, 1, tzinfo=UTC)) == "Jul 1, 2025"


def test_minutes_exhausted_without_period_end_raises() -> None:
    """Feature: sandbox-minute-plans, Requirement 11.5 (period_end required)."""
    with pytest.raises(ValueError, match="period_end"):
        denial_notice("minutes_exhausted", None, "https://trikon.example.com")


# ---------------------------------------------------------------------------
# Spot-related notices (trikon-cloud-cost-cuts, Requirements 11.1-11.5, 11.7).
# ---------------------------------------------------------------------------

_SPOT_SUMMARY = (
    "The Trikon Cloud runner for this pull request was interrupted and could not be "
    "retried. A human reviewer must decide. Delivery id: {delivery_id}."
)
_SPOT_DETAILS_URL = "https://api.trikon.unideploy.com/audits/abc-123"


@pytest.mark.parametrize(
    "reason",
    [
        "retry_exhausted",
        "retry_job_malformed",
        "interim_without_retry",
        "retry_send_failed",
        "retry_claim_stale",
    ],
)
def test_spot_fail_closed_notice_copy_and_evidence(reason: SpotFailReason) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirements 11.1-11.4, 11.7."""
    notice = spot_fail_closed_notice(reason, "abc-123", _SPOT_DETAILS_URL)
    assert notice == FailClosedNotice(
        matched_rule="spot interruption not retried",
        title="Trikon verification interrupted",
        summary=_SPOT_SUMMARY.format(delivery_id="abc-123"),
        details_url=_SPOT_DETAILS_URL,
        conclusion="action_required",
        error_class="spot_interruption",
        error_code=reason,
    )


def test_spot_retry_dispatch_failed_notice_copy_and_evidence() -> None:
    """Feature: trikon-cloud-cost-cuts, Requirements 11.5, 11.7."""
    notice = spot_retry_dispatch_failed_notice(
        "abc-123", _SPOT_DETAILS_URL, "AccessDeniedException"
    )
    assert notice == FailClosedNotice(
        matched_rule="spot retry dispatch failed",
        title="Trikon verification interrupted",
        summary=_SPOT_SUMMARY.format(delivery_id="abc-123"),
        details_url=_SPOT_DETAILS_URL,
        conclusion="action_required",
        error_class="terminal",
        error_code="AccessDeniedException",
    )


@pytest.mark.parametrize("delivery_id", [None, "", "   "])
def test_spot_notices_render_unknown_delivery_id(delivery_id: str | None) -> None:
    """Feature: trikon-cloud-cost-cuts, design "Fail-closed publishing"."""
    expected = _SPOT_SUMMARY.format(delivery_id="unknown")
    assert (
        spot_fail_closed_notice("retry_exhausted", delivery_id, _SPOT_DETAILS_URL).summary
        == expected
    )
    assert (
        spot_retry_dispatch_failed_notice(
            delivery_id, _SPOT_DETAILS_URL, "RunTaskNoTask"
        ).summary
        == expected
    )


# ---------------------------------------------------------------------------
# Property 12: Spot-related Check Runs never pass (notice side).
#
# The runner side (Interim_Check_Run and fail_closed_conclusion) is in
# ``fargate_runner/tests/test_termination.py``.
# ---------------------------------------------------------------------------

_SPOT_FAIL_REASONS: tuple[SpotFailReason, ...] = get_args(SpotFailReason)
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_SPOT_DELIVERY_IDS = st.one_of(
    st.none(),
    st.sampled_from(["", "   ", "\t\n"]),
    _DELIVERY_IDS,
)
_AWS_ERROR_CODES = st.one_of(
    st.sampled_from(
        ["AccessDeniedException", "ThrottlingException", "ClientException", "RunTaskNoTask"]
    ),
    st.text(min_size=1, max_size=64),
)


# Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass
@given(
    reason=st.sampled_from(_SPOT_FAIL_REASONS),
    delivery_id=_SPOT_DELIVERY_IDS,
    details_url=_DETAILS_URLS,
    aws_error_code=_AWS_ERROR_CODES,
)
@settings(max_examples=200, deadline=None)
def test_property_spot_notices_never_pass(
    reason: SpotFailReason,
    delivery_id: str | None,
    details_url: str,
    aws_error_code: str,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass.

    For any Spot fail reason, delivery id (missing or blank renders
    ``unknown``), details URL and AWS error code, both Spot notices are
    ``action_required`` with the fixed Spot summary.

    **Validates: Requirements 10.2, 11.7**
    """
    assert set(_SPOT_FAIL_REASONS) == {
        "retry_exhausted",
        "retry_job_malformed",
        "interim_without_retry",
        "retry_send_failed",
        "retry_claim_stale",
    }
    blank = delivery_id is None or delivery_id.strip() == ""
    summary = _SPOT_SUMMARY.format(delivery_id="unknown" if blank else delivery_id)

    fail_closed = spot_fail_closed_notice(reason, delivery_id, details_url)
    dispatch_failed = spot_retry_dispatch_failed_notice(delivery_id, details_url, aws_error_code)

    for notice in (fail_closed, dispatch_failed):
        assert notice.conclusion == "action_required"
        assert notice.conclusion not in _PASSING_CONCLUSIONS
        assert notice.title == "Trikon verification interrupted"
        assert notice.summary == summary
        assert notice.details_url == details_url
    assert fail_closed.matched_rule == "spot interruption not retried"
    assert fail_closed.error_class == "spot_interruption"
    assert fail_closed.error_code == reason
    assert dispatch_failed.matched_rule == "spot retry dispatch failed"
    assert dispatch_failed.error_class == "terminal"
    assert dispatch_failed.error_code == aws_error_code
