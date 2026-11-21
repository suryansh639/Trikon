"""Lambda handler for the Trikon Cloud Usage_Reporter (sandbox-minute-plans §10).

EventBridge invokes this Lambda asynchronously with one "ECS Task State
Change" event. For a Sandbox_Task that has stopped, the handler meters the
task with the shared Metering_Rule and sends one signed Usage_Report to
Trikon_Web's Usage_Endpoint:

1. Parse the event into :class:`EcsTaskStateChangeEvent`.
2. Ignore anything that is not a ``STOPPED`` task of the
   ``trikon-verify-runner`` family on ``trikon-verify-cluster``
   (INFO ``usage_event_ignored``).
2a. A Spot_Interruption (``stopCode == "SpotInterruption"``) logs INFO
   ``usage_report_skipped_spot_interruption`` and sends nothing; the retry
   reports the run (trikon-cloud-cost-cuts Req 12.1, 12.2).
3. Read the Run_Id (``TRIKON_DELIVERY_ID``) and the Reservation_Id
   (``TRIKON_RESERVATION_ID``) from the ``runner`` container override. A task
   without a Reservation_Id logs WARNING ``usage_report_skipped`` and sends
   nothing (Req 12.6).
4. Meter the task (Req 12.2-12.4) and report it (Req 12.5).
5. ``UsageDelivered`` logs INFO ``usage_report_delivered`` (Req 12.11).
   ``UsageRejected`` logs ERROR ``usage_report_rejected`` with the status and
   returns normally, so the event is not retried (Req 12.10).

Failures that a retry may fix propagate so the invocation fails and Lambda's
async retries, then the dead-letter destination, keep the event:
:class:`UsageReportUndeliveredError` (the client already retried within its
30-second budget, Req 12.7, 12.8), :class:`MeteringInputError` (no start
time), :class:`UsageEventError` (a Reservation_Id without a valid Run_Id),
and :class:`pydantic.ValidationError` for an event that does not parse.

Module-scope caches: :func:`_bootstrap` loads :class:`UsageReporterEnvConfig`
and builds one :class:`UsageApiClient` (with its boto3 SSM client,
:class:`UsageSecretReader`, and :class:`httpx.Client`) on the first
invocation, and reuses them for the container lifetime. Tests reset the caches
with :func:`_reset_bootstrap` or inject a stub client by assigning
:data:`_ENV` and :data:`_USAGE_CLIENT` directly.

Logging (Req 12.15): fixed event names with ``task_arn``, ``run_id``, and
``status`` only. Never the event, the Usage_Report body, a response body, the
Usage_Secret, or a Usage_Signature. Keys are passed per call rather than with
``append_keys`` so a warm container never carries one task's ids into the
next invocation's records.
"""

from __future__ import annotations

from typing import Final, Protocol

import boto3  # type: ignore[import-untyped]
import httpx
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import (
    RESERVATION_ID_ENV,
    RUN_ID_ENV,
    SPOT_INTERRUPTION_STOP_CODE,
)
from trikon_cloud.usage_client.client import UsageApiClient
from trikon_cloud.usage_client.metering import meter_stopped_task
from trikon_cloud.usage_client.models import (
    UsageDelivered,
    UsageRejected,
    UsageReport,
)
from trikon_cloud.usage_client.secret import UsageSecretReader
from trikon_cloud.usage_reporter.logger import get_logger
from trikon_cloud.usage_reporter.models import (
    EcsTaskStateChangeEvent,
    UsageReporterEnvConfig,
)

__all__ = ["UsageEventError", "lambda_handler"]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Timeout of the shared HTTP client. ``UsageApiClient`` passes its own
#: per-attempt timeout on every request; this only bounds a stray call.
_HTTP_TIMEOUT_SECONDS: Final = 10.0


class UsageEventError(ValueError):
    """A stopped Sandbox_Task carries a Reservation_Id but no valid Run_Id.

    The Orchestrator always sends both, so this is a malformed dispatch. The
    error propagates so the event is retried and then kept in the dead-letter
    destination. The message is fixed and names no id.
    """


class _UsageReportClient(Protocol):
    """Structural type of :meth:`UsageApiClient.report_usage`, so tests can inject a stub."""

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected: ...


# ---------------------------------------------------------------------------
# Cold-start module caches.
# ---------------------------------------------------------------------------

_ENV: UsageReporterEnvConfig | None = None
_USAGE_CLIENT: _UsageReportClient | None = None


def _bootstrap() -> tuple[UsageReporterEnvConfig, _UsageReportClient]:
    """Return the cached env config and Usage_API client, building them on first use.

    The env config raises :class:`pydantic.ValidationError` on a missing or
    malformed variable, failing the first invocation. The Usage_Secret is read
    lazily by :class:`UsageSecretReader` on the first report, not here.
    """
    global _ENV, _USAGE_CLIENT
    if _ENV is None:
        _ENV = UsageReporterEnvConfig()
    if _USAGE_CLIENT is None:
        # boto3 is untyped upstream; UsageSecretReader takes a structural
        # Protocol, so the Any-to-Protocol assignment is safe under --strict.
        ssm_client = boto3.session.Session().client("ssm")
        _USAGE_CLIENT = UsageApiClient(
            base_url=_ENV.trikon_web_api_url,
            secret_reader=UsageSecretReader(
                parameter_name=_ENV.trikon_cloud_usage_secret_parameter_name,
                ssm_client=ssm_client,
            ),
            http_client=httpx.Client(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)),
        )
    return _ENV, _USAGE_CLIENT


def _reset_bootstrap() -> None:
    """Test seam: drop the cached env config and client so the next call rebuilds them."""
    global _ENV, _USAGE_CLIENT
    _ENV = None
    _USAGE_CLIENT = None


# ---------------------------------------------------------------------------
# Lambda entry point.
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, object], context: LambdaContext) -> None:
    """Report the metered minutes of one stopped Sandbox_Task (design §10).

    Returns ``None`` on every handled path (ignored, skipped, delivered,
    rejected). Raises to fail the invocation when a retry may succeed; see the
    module docstring.
    """
    del context  # Unused: the task ARN and Run_Id are the correlation keys.
    logger = get_logger()
    _env, client = _bootstrap()

    parsed = EcsTaskStateChangeEvent.model_validate(event)
    detail = parsed.detail
    task_arn = detail.taskArn

    if not detail.is_stopped_sandbox_task():
        logger.info("usage_event_ignored", task_arn=task_arn)
        return

    if detail.stopCode == SPOT_INTERRUPTION_STOP_CODE:
        # cost-cuts Req 12.1: the interrupted attempt is never billed. Its
        # retry carries the same Run_Id and Reservation_Id and is reported on
        # its own stop (Req 12.2).
        logger.info(
            "usage_report_skipped_spot_interruption",
            task_arn=task_arn,
            run_id=detail.runner_env(RUN_ID_ENV),
        )
        return

    reservation_id = detail.runner_env(RESERVATION_ID_ENV)
    if reservation_id is None:
        # Req 12.6: nothing to settle against, and a retry cannot add one.
        logger.warning("usage_report_skipped", task_arn=task_arn)
        return

    run_id = detail.runner_env(RUN_ID_ENV)
    if run_id is None:
        raise UsageEventError("stopped Sandbox_Task has a Reservation_Id but no Run_Id")

    # Req 12.2-12.4. MeteringInputError propagates (no start time).
    usage = meter_stopped_task(
        created_at=detail.createdAt,
        pull_started_at=detail.pullStartedAt,
        stopped_at=detail.stoppedAt,
        event_time=parsed.time,
        stop_code=detail.stopCode,
    )

    try:
        report = UsageReport(
            run_id=run_id,
            reservation_id=reservation_id,
            billed_minutes=usage.billed_minutes,
            started_at=usage.started_at,
            stopped_at=usage.stopped_at,
        )
    except ValidationError:
        # A malformed Run_Id or Reservation_Id. Re-raised without the pydantic
        # error, whose text would echo the override values into the log.
        raise UsageEventError(
            "stopped Sandbox_Task has a malformed Run_Id or Reservation_Id"
        ) from None

    # Req 12.5, 12.7. UsageReportUndeliveredError propagates (Req 12.8).
    result = client.report_usage(report)

    if isinstance(result, UsageDelivered):
        # Req 12.11: 201 recorded or 200 duplicate.
        logger.info("usage_report_delivered", task_arn=task_arn, run_id=run_id)
        return

    # Req 12.10: a non-retryable rejection. Logged, not raised, so the event
    # is not retried.
    logger.error(
        "usage_report_rejected",
        task_arn=task_arn,
        run_id=run_id,
        status=result.status,
    )
