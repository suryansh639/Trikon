# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write, so it is
# silenced at the file level, matching :mod:`trikon_cloud.orchestrator.models`.
# mypy: disable-error-code="explicit-any"
"""Usage_API wire models and client results (sandbox-minute-plans §9).

Three groups:

* Requests: :class:`QuotaRequest` and :class:`UsageReport`. Frozen, strict,
  ``extra="forbid"``. They mirror Trikon_Web's ``src/usage/usageSchema.ts``
  field for field (Req 8.1, 9.1), including the Usage_Report cross-field rules
  (Req 9.2-9.4), so a report Trikon_Web would reject with ``invalid_payload``
  fails here at construction instead of on the wire.
* Responses: frozen, strict, ``extra="ignore"`` bodies for the 2xx outcomes the
  client acts on, as discriminated unions on ``outcome`` (and, for ``denied``,
  on ``reason``). They mirror ``src/usage/responses.ts``. Every other outcome
  (``conflict``, ``not_found``, ``unauthorized``, ``invalid_json``,
  ``invalid_payload``, ``payload_too_large``, ``unsupported_media_type``,
  ``rate_limited``, ``internal_error``) arrives with a non-2xx status and is
  classified by status alone, so it has no body model here.
* Results: the quota decisions :class:`QuotaAllowed`,
  :class:`QuotaAlreadySettled`, :class:`QuotaDenied`, :class:`QuotaBusy`, and
  :class:`QuotaUnavailable`; the usage results :class:`UsageDelivered` and
  :class:`UsageRejected`; and :class:`UsageReportUndeliveredError`.

:func:`https_base_url` is the base-URL rule both Lambda configs share
(Req 11.15).

No message raised from this module echoes a submitted value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from trikon_cloud.usage_client.metering import PER_RUN_CAP_MINUTES, billed_minutes

__all__ = [
    "INSTALLATION_ID_MAX",
    "PR_NUMBER_MAX",
    "QUOTA_BUSY_RETRY_MAX_SECONDS",
    "USAGE_REQUEST_VERSION",
    "DenialReason",
    "QuotaAllowed",
    "QuotaAllowedBody",
    "QuotaAlreadySettled",
    "QuotaAlreadySettledBody",
    "QuotaBusy",
    "QuotaBusyBody",
    "QuotaDecision",
    "QuotaDenied",
    "QuotaDeniedMinutesExhaustedBody",
    "QuotaDeniedNoPlanBody",
    "QuotaDeniedNotConnectedBody",
    "QuotaRequest",
    "QuotaResponseBody",
    "QuotaUnavailable",
    "UnavailableCause",
    "UsageDelivered",
    "UsageDuplicateBody",
    "UsageOutcome",
    "UsageRecordedBody",
    "UsageRejected",
    "UsageReport",
    "UsageReportUndeliveredError",
    "UsageResponseBody",
    "https_base_url",
    "parse_quota_decision",
    "parse_usage_outcome",
]

# ---------------------------------------------------------------------------
# Wire constants (mirroring src/usage/usageSchema.ts).
# ---------------------------------------------------------------------------

#: The only supported ``request_version`` (Req 8.1, 9.1).
USAGE_REQUEST_VERSION: Final = 1

#: GitHub App installation ids, 1 to 2^53 - 1 (``Number.MAX_SAFE_INTEGER``).
INSTALLATION_ID_MAX = 9_007_199_254_740_991

#: Largest pull request number Trikon_Web accepts.
PR_NUMBER_MAX = 2_147_483_647

#: Upper bound of a ``busy`` ``retry_after_seconds``: the SQS ``DelaySeconds``
#: maximum, so the value can be used as the re-queue delay unchanged (Req 11.18).
QUOTA_BUSY_RETRY_MAX_SECONDS = 900

# Patterns are anchored for pydantic's default Rust regex engine, where ``$``
# matches only at the very end (no trailing-newline match as in Python's
# ``re``). Character classes are spelled out as ASCII because ``\d`` is
# Unicode-aware in that engine but ASCII-only in JavaScript.
_RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_REPOSITORY_FULL_NAME_PATTERN = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
# Any 8-4-4-4-12 hex value, either case, with no version or variant check.
_UUID_PATTERN = (
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
# ISO 8601 UTC, optional fraction of up to 6 digits, ``Z`` only.
_USAGE_TIMESTAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z"
)

def _reject_bool(value: object) -> object:
    # ``Literal[1]`` matches by equality, so pydantic accepts ``True`` even in
    # strict mode. Trikon_Web's ``z.literal(1)`` does not.
    if isinstance(value, bool):
        raise ValueError("must be 1 (supported request versions: 1)")
    return value


RequestVersion = Annotated[Literal[1], BeforeValidator(_reject_bool)]
RunId = Annotated[StrictStr, StringConstraints(pattern=_RUN_ID_PATTERN)]
ReservationId = Annotated[StrictStr, StringConstraints(pattern=_UUID_PATTERN)]
BilledMinutes = Annotated[StrictInt, Field(ge=0, le=PER_RUN_CAP_MINUTES)]

_REQUEST_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)
_RESPONSE_CONFIG = ConfigDict(frozen=True, extra="ignore", strict=True)


# ---------------------------------------------------------------------------
# Timestamp helpers (mirroring parseIsoInstant in src/verdicts/validators.ts).
# ---------------------------------------------------------------------------


def _is_leap_year(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap_year(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar (year 0 included)."""
    y = year - 1 if month <= 2 else year
    era = y // 400
    yoe = y - era * 400
    mp = (month + 9) % 12
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def _usage_timestamp_ms(value: str) -> int | None:
    """Epoch milliseconds of a Usage_Report timestamp, or ``None`` if Trikon_Web would reject it.

    Like Trikon_Web, fraction digits beyond the third are dropped (truncated,
    not rounded), and out-of-range components and leap seconds are rejected.
    """
    match = _USAGE_TIMESTAMP_RE.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not 1 <= month <= 12 or not 1 <= day <= _days_in_month(year, month):
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    fraction = match.group(7)
    millis = 0 if fraction is None else int(fraction[:3].ljust(3, "0"))
    days = _days_from_civil(year, month, day)
    return ((days * 24 + hour) * 60 + minute) * 60_000 + second * 1_000 + millis


# ---------------------------------------------------------------------------
# Requests.
# ---------------------------------------------------------------------------


class QuotaRequest(BaseModel):
    """Quota_Request body for ``POST /api/cloud/quota`` (Req 8.1, 11.1)."""

    model_config = _REQUEST_CONFIG

    request_version: RequestVersion = USAGE_REQUEST_VERSION
    installation_id: Annotated[StrictInt, Field(ge=1, le=INSTALLATION_ID_MAX)]
    run_id: RunId
    repository_full_name: Annotated[
        StrictStr, StringConstraints(pattern=_REPOSITORY_FULL_NAME_PATTERN)
    ]
    pr_number: Annotated[StrictInt, Field(ge=1, le=PR_NUMBER_MAX)]


class UsageReport(BaseModel):
    """Usage_Report body for ``POST /api/cloud/usage`` (Req 9.1-9.4).

    ``started_at`` and ``stopped_at`` are ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (as
    :func:`trikon_cloud.usage_client.metering.format_utc_ms` produces) or
    ``None``. Both keys are always sent; ``None`` serialises as ``null``.
    """

    model_config = _REQUEST_CONFIG

    request_version: RequestVersion = USAGE_REQUEST_VERSION
    run_id: RunId
    reservation_id: ReservationId
    billed_minutes: BilledMinutes
    started_at: StrictStr | None
    stopped_at: StrictStr | None

    @field_validator("started_at", "stopped_at")
    @classmethod
    def _valid_timestamp(cls, value: str | None) -> str | None:
        if value is not None and _usage_timestamp_ms(value) is None:
            raise ValueError("must be null or an ISO 8601 UTC date-time ending in Z")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> UsageReport:
        """Req 9.2-9.4, in Trikon_Web's order."""
        if self.billed_minutes > 0 and (self.started_at is None or self.stopped_at is None):
            raise ValueError("started_at and stopped_at are required when billed_minutes > 0")
        if self.started_at is None or self.stopped_at is None:
            return self
        # Both already passed _valid_timestamp, so neither parse returns None.
        started = _usage_timestamp_ms(self.started_at)
        stopped = _usage_timestamp_ms(self.stopped_at)
        if started is None or stopped is None:  # pragma: no cover - guarded above
            raise ValueError("timestamp must be an ISO 8601 UTC date-time ending in Z")
        if stopped < started:
            raise ValueError("stopped_at must not be earlier than started_at")
        if self.billed_minutes > 0 and self.billed_minutes != billed_minutes(started, stopped):
            raise ValueError("billed_minutes does not match the metered task time")
        return self


# ---------------------------------------------------------------------------
# Response bodies (mirroring src/usage/responses.ts).
# ---------------------------------------------------------------------------

DenialReason: TypeAlias = Literal["not_connected", "minutes_exhausted", "no_plan"]


def _aware_utc(value: str) -> datetime:
    """Parse an ISO 8601 date-time with an offset and return it in UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be an ISO 8601 date-time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must include a UTC offset")
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        # e.g. "0001-01-01T00:00:00+01:00": the UTC instant is before year 1.
        # Pydantic only turns ValueError into a ValidationError, so re-raise
        # as one to keep the body "malformed" (Req 11.8).
        raise ValueError("must be a representable UTC date-time") from None


class QuotaAllowedBody(BaseModel):
    """``200 {"outcome":"allowed","reservation_id":...}`` (Req 8.3, 8.9).

    A new Reservation also carries ``reserved_minutes``, ``remaining_minutes``,
    and ``period_end``; the client needs only the Reservation_Id.
    """

    model_config = _RESPONSE_CONFIG

    outcome: Literal["allowed"]
    reservation_id: ReservationId


class QuotaAlreadySettledBody(BaseModel):
    """``200 {"outcome":"already_settled","reservation_id":...}`` (Req 8.4)."""

    model_config = _RESPONSE_CONFIG

    outcome: Literal["already_settled"]
    reservation_id: ReservationId


class QuotaDeniedNotConnectedBody(BaseModel):
    """``200 {"outcome":"denied","reason":"not_connected"}`` (Req 8.6)."""

    model_config = _RESPONSE_CONFIG

    outcome: Literal["denied"]
    reason: Literal["not_connected"]


class QuotaDeniedNoPlanBody(BaseModel):
    """``200 {"outcome":"denied","reason":"no_plan"}`` (Req 8.7)."""

    model_config = _RESPONSE_CONFIG

    outcome: Literal["denied"]
    reason: Literal["no_plan"]


class QuotaDeniedMinutesExhaustedBody(BaseModel):
    """``200 {"outcome":"denied","reason":"minutes_exhausted","remaining_minutes":0,"period_end":...}``.

    ``period_end`` must parse as an ISO 8601 date-time with an offset (Req 8.8).
    """

    model_config = _RESPONSE_CONFIG

    outcome: Literal["denied"]
    reason: Literal["minutes_exhausted"]
    period_end: StrictStr

    @field_validator("period_end")
    @classmethod
    def _parseable_period_end(cls, value: str) -> str:
        _aware_utc(value)
        return value

    @property
    def period_end_utc(self) -> datetime:
        """``period_end`` as an aware UTC datetime."""
        return _aware_utc(self.period_end)


class QuotaBusyBody(BaseModel):
    """``200 {"outcome":"busy","retry_after_seconds":N,"concurrency_limit":L}`` (Req 8.11, 8.12).

    ``retry_after_seconds`` is an integer from 1 to 900 (the SQS
    ``DelaySeconds`` range) and ``concurrency_limit`` an integer of at least 1,
    so both carry into :class:`QuotaBusy` unchanged (Req 11.18).
    """

    model_config = _RESPONSE_CONFIG

    outcome: Literal["busy"]
    retry_after_seconds: Annotated[StrictInt, Field(ge=1, le=QUOTA_BUSY_RETRY_MAX_SECONDS)]
    concurrency_limit: Annotated[StrictInt, Field(ge=1)]


_QuotaDeniedBody: TypeAlias = Annotated[
    QuotaDeniedNotConnectedBody | QuotaDeniedNoPlanBody | QuotaDeniedMinutesExhaustedBody,
    Field(discriminator="reason"),
]

#: A 200 Quota_Endpoint body, discriminated on ``outcome`` (and ``reason`` for ``denied``).
QuotaResponseBody: TypeAlias = Annotated[
    QuotaAllowedBody | QuotaAlreadySettledBody | _QuotaDeniedBody | QuotaBusyBody,
    Field(discriminator="outcome"),
]


class UsageRecordedBody(BaseModel):
    """``201 {"outcome":"recorded","reservation_id":...,"billed_minutes":N}`` (Req 9.6)."""

    model_config = _RESPONSE_CONFIG

    outcome: Literal["recorded"]
    reservation_id: ReservationId
    billed_minutes: BilledMinutes


class UsageDuplicateBody(BaseModel):
    """``200 {"outcome":"duplicate","reservation_id":...,"billed_minutes":N}`` (Req 9.7)."""

    model_config = _RESPONSE_CONFIG

    outcome: Literal["duplicate"]
    reservation_id: ReservationId
    billed_minutes: BilledMinutes


#: A 2xx Usage_Endpoint body, discriminated on ``outcome``.
UsageResponseBody: TypeAlias = Annotated[
    UsageRecordedBody | UsageDuplicateBody,
    Field(discriminator="outcome"),
]

UsageOutcome: TypeAlias = Literal["recorded", "duplicate"]

_QUOTA_BODY_ADAPTER: TypeAdapter[QuotaResponseBody] = TypeAdapter(QuotaResponseBody)
_USAGE_BODY_ADAPTER: TypeAdapter[UsageResponseBody] = TypeAdapter(UsageResponseBody)


# ---------------------------------------------------------------------------
# Client results.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotaAllowed:
    """The run may start under this Reservation (Req 11.3)."""

    reservation_id: str


@dataclass(frozen=True, slots=True)
class QuotaAlreadySettled:
    """The Run_Id's Reservation is already settled; nothing to run (Req 11.7)."""

    reservation_id: str


@dataclass(frozen=True, slots=True)
class QuotaDenied:
    """The run may not start. ``period_end`` is set only for ``minutes_exhausted``."""

    reason: DenialReason
    period_end: datetime | None


@dataclass(frozen=True, slots=True)
class QuotaBusy:
    """The Concurrency_Limit is reached; re-queue after ``retry_after_seconds`` (Req 11.18)."""

    retry_after_seconds: int
    concurrency_limit: int


UnavailableCause: TypeAlias = Literal["network", "timeout", "http_status", "malformed"]


@dataclass(frozen=True, slots=True)
class QuotaUnavailable:
    """No usable quota decision (Req 11.8). ``status`` is the last HTTP status, if any."""

    cause: UnavailableCause
    status: int | None


QuotaDecision: TypeAlias = (
    QuotaAllowed | QuotaAlreadySettled | QuotaDenied | QuotaBusy | QuotaUnavailable
)


@dataclass(frozen=True, slots=True)
class UsageDelivered:
    """Trikon_Web settled the Reservation (``201 recorded`` or ``200 duplicate``, Req 12.11)."""

    outcome: UsageOutcome


@dataclass(frozen=True, slots=True)
class UsageRejected:
    """Trikon_Web rejected the report with a non-retryable status, or a 2xx with an
    unexpected body. Not retried (Req 12.10)."""

    status: int


class UsageReportUndeliveredError(RuntimeError):
    """The report was not delivered within the attempt limit or the time budget.

    The Usage_Reporter lets this propagate so Lambda's async retries and the
    dead-letter destination keep the event (Req 12.7, 12.8).
    """


# ---------------------------------------------------------------------------
# Body parsing.
# ---------------------------------------------------------------------------


def parse_quota_decision(
    raw: bytes | str,
) -> QuotaAllowed | QuotaAlreadySettled | QuotaDenied | QuotaBusy | None:
    """Map a 200 Quota_Endpoint body to its decision, or ``None`` if it is malformed.

    ``None`` covers invalid JSON, a non-object root, an unknown ``outcome`` or
    ``reason``, and any field that fails validation; the client turns it into
    ``QuotaUnavailable(cause="malformed")`` (Req 11.8).
    """
    try:
        body = _QUOTA_BODY_ADAPTER.validate_json(raw)
    except ValidationError:
        return None
    if isinstance(body, QuotaAllowedBody):
        return QuotaAllowed(reservation_id=body.reservation_id)
    if isinstance(body, QuotaAlreadySettledBody):
        return QuotaAlreadySettled(reservation_id=body.reservation_id)
    if isinstance(body, QuotaBusyBody):
        return QuotaBusy(
            retry_after_seconds=body.retry_after_seconds,
            concurrency_limit=body.concurrency_limit,
        )
    if isinstance(body, QuotaDeniedMinutesExhaustedBody):
        return QuotaDenied(reason="minutes_exhausted", period_end=body.period_end_utc)
    return QuotaDenied(reason=body.reason, period_end=None)


def parse_usage_outcome(raw: bytes | str) -> UsageOutcome | None:
    """Return ``"recorded"`` or ``"duplicate"`` for a valid 2xx Usage_Endpoint body, else ``None``.

    The caller still checks the status pairing (201 for ``recorded``, 200 for
    ``duplicate``).
    """
    try:
        body = _USAGE_BODY_ADAPTER.validate_json(raw)
    except ValidationError:
        return None
    return body.outcome


# ---------------------------------------------------------------------------
# Shared config rule.
# ---------------------------------------------------------------------------


def https_base_url(value: str) -> str:
    """Validate an https base URL and return it without trailing ``/`` (Req 11.15).

    The scheme must be ``https``, the host non-empty, and there must be no
    query or fragment (not even an empty ``?`` or ``#``). Whitespace and
    control characters are rejected rather than silently stripped by
    :func:`urllib.parse.urlsplit`. Raises :class:`ValueError`, which pydantic
    field validators turn into a ``ValidationError``.
    """
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("must not contain whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("must use the https scheme")
    if not parts.hostname:
        raise ValueError("must include a host")
    if "?" in value or "#" in value:
        raise ValueError("must not include a query or fragment")
    try:
        _ = parts.port
    except ValueError:
        raise ValueError("must have a valid port") from None
    return value.rstrip("/")
