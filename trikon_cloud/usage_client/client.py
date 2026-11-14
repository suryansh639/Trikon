"""Signed Usage_API client for the Orchestrator and the Usage_Reporter (sandbox-minute-plans §9).

:class:`UsageApiClient` posts to Trikon_Web's two Usage_API routes,
``POST /api/v1/cloud/quota`` and ``POST /api/v1/cloud/usage`` (declared in
``trikon-web/app/src/usage/usage.wasp.ts``).

Every request body is produced once by :func:`~trikon_cloud.usage_client.signing.encode_body`
and posted with ``content=`` (never ``json=``), so the bytes on the wire are
the bytes that were signed. Each attempt re-signs with a fresh
Request_Timestamp, so a retry never carries a timestamp that may have left
Trikon_Web's Signature_Window (Req 11.2).

Retry policies (design §9), at most three attempts each:

* ``check_quota``: total budget ``budget_seconds`` (``TRIKON_QUOTA_TIME_BUDGET_SECONDS``,
  10 s by default), per-attempt timeout ``min(remaining, 4 s)``, backoff
  0.25 s then 0.5 s.
* ``report_usage``: total budget 30 s, per-attempt timeout
  ``min(remaining, 10 s)``, backoff 0.5 s then 1 s.

Backoff carries ±20 % jitter. Both retry network errors, timeouts, 429, and 5xx; every backoff sleep is
capped by the remaining budget. ``check_quota`` never raises: any other status
and any malformed 200 body become :class:`QuotaUnavailable` at once (Req 11.8,
11.18). ``report_usage`` returns :class:`UsageDelivered` for ``201 recorded``
or ``200 duplicate`` (Req 12.11), :class:`UsageRejected` for any other
non-retryable status or a 2xx with an unexpected body (Req 12.10), and raises
:class:`UsageReportUndeliveredError` once attempts or budget run out
(Req 12.7, 12.8, 12.12).

The clock, ``sleep``, and jitter are injected so tests run without real
waiting. The clock returns Unix wall-time seconds: it supplies both the
Request_Timestamp (which must be Unix seconds) and the budget timing, so a
test that advances one fake clock in ``sleep`` drives both.

This module logs nothing. Callers log the fixed event names and status codes.
No exception raised here carries the Usage_Secret, a Usage_Signature, a request
body, or a response body (Req 11.17, 12.15, 15.1).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final, Protocol

import httpx

from trikon_cloud.usage_client.models import (
    QuotaDecision,
    QuotaRequest,
    QuotaUnavailable,
    UnavailableCause,
    UsageDelivered,
    UsageRejected,
    UsageReport,
    UsageReportUndeliveredError,
    https_base_url,
    parse_quota_decision,
    parse_usage_outcome,
)
from trikon_cloud.usage_client.signing import encode_body, signed_headers

__all__ = [
    "QUOTA_PATH",
    "USAGE_PATH",
    "USAGE_REPORT_BUDGET_SECONDS",
    "UsageApiClient",
]

#: Quota_Endpoint path, relative to ``TRIKON_WEB_API_URL``.
QUOTA_PATH: Final = "/api/v1/cloud/quota"

#: Usage_Endpoint path, relative to ``TRIKON_WEB_API_URL``.
USAGE_PATH: Final = "/api/v1/cloud/usage"

#: Total time budget of one ``report_usage`` call (Req 12.7).
USAGE_REPORT_BUDGET_SECONDS: Final = 30.0

_MAX_ATTEMPTS: Final = 3

#: An attempt needs at least this much budget left to start. Below it no
#: request can complete, and with Unix-epoch clock values float rounding
#: alone can leave a sub-microsecond remainder after a capped sleep.
_MIN_ATTEMPT_SECONDS: Final = 0.001


@dataclass(frozen=True, slots=True)
class _RetryPolicy:
    """One row of the design §9 retry table."""

    per_attempt_timeout_seconds: float
    #: Base delay before attempt ``i + 2``; jitter is applied on top.
    backoff_seconds: tuple[float, ...]


_QUOTA_POLICY: Final = _RetryPolicy(per_attempt_timeout_seconds=4.0, backoff_seconds=(0.25, 0.5))
_USAGE_POLICY: Final = _RetryPolicy(per_attempt_timeout_seconds=10.0, backoff_seconds=(0.5, 1.0))


class _SecretSource(Protocol):
    """Structural type of :class:`~trikon_cloud.usage_client.secret.UsageSecretReader`."""

    def secret_bytes(self) -> bytes: ...


def _default_jitter(delay_seconds: float) -> float:
    """Return ``delay_seconds`` scaled by a factor drawn uniformly from [0.8, 1.2]."""
    return delay_seconds * random.uniform(0.8, 1.2)


@dataclass(frozen=True, slots=True)
class _Failure:
    """A retryable attempt outcome: a transport failure or a 429/5xx status."""

    cause: UnavailableCause  # "network", "timeout", or "http_status"
    status: int | None


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class UsageApiClient:
    """Signs and posts Quota_Requests and Usage_Reports to Trikon_Web.

    Args:
        base_url: ``TRIKON_WEB_API_URL``. Validated with
            :func:`~trikon_cloud.usage_client.models.https_base_url`
            (raises :class:`ValueError` if it is not an https base URL).
        secret_reader: Supplies the Usage_Secret bytes (read once, cached).
        http_client: The shared :class:`httpx.Client`. Each attempt passes its
            own timeout derived from the remaining budget.
        clock: Unix wall-time seconds. Defaults to :func:`time.time`.
        sleep: Blocks for the given seconds. Defaults to :func:`time.sleep`.
        jitter: Maps a base backoff delay to the delay actually slept.
            Defaults to ±20 % uniform jitter. The result is clamped to
            ``[0, remaining budget]``.
    """

    def __init__(
        self,
        base_url: str,
        secret_reader: _SecretSource,
        http_client: httpx.Client,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        *,
        jitter: Callable[[float], float] = _default_jitter,
    ) -> None:
        base = https_base_url(base_url)
        self._quota_url: str = base + QUOTA_PATH
        self._usage_url: str = base + USAGE_PATH
        self._secret_reader: _SecretSource = secret_reader
        self._http: httpx.Client = http_client
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], None] = sleep
        self._jitter: Callable[[float], float] = jitter

    def __repr__(self) -> str:
        return f"UsageApiClient(quota_url={self._quota_url!r}, usage_url={self._usage_url!r})"

    # ------------------------------------------------------------------
    # Quota_Endpoint
    # ------------------------------------------------------------------

    def check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision:
        """Ask Trikon_Web whether a run may start. Never raises (Req 11.8).

        Returns the decision of a ``200`` with a well-formed known body
        (a ``busy`` body's ``retry_after_seconds`` and ``concurrency_limit``
        are carried unchanged into :class:`QuotaBusy`, Req 11.18), and
        :class:`QuotaUnavailable` otherwise:

        * ``("malformed", 200)`` for a 200 whose body does not parse;
        * ``("http_status", status)`` at once for any status other than 200,
          429, and 5xx, and for a 429 or 5xx on the last attempt;
        * ``("network", None)`` or ``("timeout", None)`` when the last
          attempt failed in transport, or when no attempt could start
          (``("timeout", None)`` for a non-positive budget). A Usage_Secret
          that cannot be read is reported as ``("network", None)``.
        """
        try:
            return self._check_quota(request, budget_seconds)
        except Exception:
            # Defensive: the Orchestrator relies on this call never raising.
            # Whatever went wrong, no decision was obtained, so fail closed.
            return QuotaUnavailable(cause="network", status=None)

    def _check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision:
        body = encode_body(request)
        secret = self._secret_reader.secret_bytes()
        last: _Failure = _Failure(cause="timeout", status=None)
        for response in self._attempts(
            self._quota_url, body, secret, budget_seconds, _QUOTA_POLICY
        ):
            if isinstance(response, _Failure):
                last = response
                continue
            status = response.status_code
            if status == 200:
                decision = parse_quota_decision(response.content)
                if decision is None:
                    return QuotaUnavailable(cause="malformed", status=200)
                return decision
            if _is_retryable_status(status):
                last = _Failure(cause="http_status", status=status)
                continue
            return QuotaUnavailable(cause="http_status", status=status)
        return QuotaUnavailable(cause=last.cause, status=last.status)

    # ------------------------------------------------------------------
    # Usage_Endpoint
    # ------------------------------------------------------------------

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        """Deliver a Usage_Report (Req 12.5, 12.7, 12.10-12.12).

        Returns :class:`UsageDelivered` for ``201 recorded`` or
        ``200 duplicate``, and :class:`UsageRejected` (no retry) for any
        other status that is not 429 or 5xx, including a 2xx whose body or
        status pairing is unexpected.

        Raises:
            UsageReportUndeliveredError: three attempts or the 30-second
                budget ran out on network errors, timeouts, 429, or 5xx, or
                the Usage_Secret could not be read. The message names only the
                attempt count and the last cause and status.
        """
        body = encode_body(report)
        try:
            secret = self._secret_reader.secret_bytes()
        except Exception as exc:
            raise UsageReportUndeliveredError(
                "Usage_Report not sent: Usage_Secret unavailable"
            ) from exc
        last: _Failure | None = None
        attempts = 0
        for response in self._attempts(
            self._usage_url, body, secret, USAGE_REPORT_BUDGET_SECONDS, _USAGE_POLICY
        ):
            attempts += 1
            if isinstance(response, _Failure):
                last = response
                continue
            status = response.status_code
            if _is_retryable_status(status):
                last = _Failure(cause="http_status", status=status)
                continue
            outcome = parse_usage_outcome(response.content) if 200 <= status <= 299 else None
            if (status, outcome) == (201, "recorded"):
                return UsageDelivered(outcome="recorded")
            if (status, outcome) == (200, "duplicate"):
                return UsageDelivered(outcome="duplicate")
            return UsageRejected(status=status)
        if last is None:
            detail = "no attempt fit in the time budget"
        elif last.status is None:
            detail = f"last failure: {last.cause}"
        else:
            detail = f"last failure: {last.cause} {last.status}"
        raise UsageReportUndeliveredError(
            f"Usage_Report not delivered after {attempts} attempt(s); {detail}"
        )

    # ------------------------------------------------------------------
    # Shared attempt loop
    # ------------------------------------------------------------------

    def _attempts(
        self,
        url: str,
        body: bytes,
        secret: bytes,
        budget_seconds: float,
        policy: _RetryPolicy,
    ) -> Iterator[httpx.Response | _Failure]:
        """Yield one result per attempt, sleeping between attempts.

        The caller pulls the next attempt only after classifying the previous
        one as retryable, so a final result ends the loop without a further
        sleep or request. The remaining budget is measured on the injected
        clock before each attempt; the loop stops once it is used up or after
        :data:`_MAX_ATTEMPTS` attempts. Each attempt's timeout is
        ``min(remaining, policy.per_attempt_timeout_seconds)`` and each backoff
        sleep is capped by the remaining budget.
        """
        start = self._clock()

        def remaining() -> float:
            # Clamped so a wall clock stepping backwards cannot extend the budget.
            return min(budget_seconds, budget_seconds - (self._clock() - start))

        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                base = policy.backoff_seconds[attempt - 1]
                delay = min(max(self._jitter(base), 0.0), remaining())
                if delay > 0:
                    self._sleep(delay)
            left = remaining()
            if left < _MIN_ATTEMPT_SECONDS:
                return
            timeout = min(left, policy.per_attempt_timeout_seconds)
            yield self._post_once(url, body, secret, timeout)

    def _post_once(
        self, url: str, body: bytes, secret: bytes, timeout_seconds: float
    ) -> httpx.Response | _Failure:
        headers = signed_headers(secret, body, int(self._clock()))
        try:
            return self._http.post(
                url, content=body, headers=headers, timeout=httpx.Timeout(timeout_seconds)
            )
        except httpx.TimeoutException:
            return _Failure(cause="timeout", status=None)
        except httpx.HTTPError:
            return _Failure(cause="network", status=None)
