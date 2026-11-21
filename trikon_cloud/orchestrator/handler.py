"""Lambda handler for the Trikon Cloud orchestrator (Spec 3).

Composition layer wiring the Wave-1 and Wave-2 leaves
(:mod:`trikon_cloud.orchestrator.models`,
:mod:`trikon_cloud.orchestrator.logger`,
:mod:`trikon_cloud.orchestrator.ecs_dispatcher`,
:mod:`trikon_cloud.orchestrator.never_fail_open`) into the eight-step
per-record flow prescribed by
``.kiro/specs/trikon-cloud-orchestrator/design.md`` §4.1.

The Lambda is triggered by the ``trikon-verify-jobs`` SQS queue with
``BatchSize=1`` (Requirement 1.1); each invocation processes exactly one
:class:`~trikon_cloud.webhook_receiver.models.SqsJobMessage` written by
Spec 1's webhook receiver. On success the handler dispatches one
``ecs.RunTask`` API call against ``trikon-verify-cluster`` and returns
``{"batchItemFailures": []}`` — SQS deletes the record. On Transient
``ClientError`` from ``ecs:RunTask`` the handler raises
:class:`~trikon_cloud.orchestrator.ecs_dispatcher.TransientDispatchError`
so the Lambda runtime surfaces the batch as a failure and SQS returns
the record to the source queue (Requirement 6.1). On Terminal
``ClientError`` the handler invokes the Never-Fail-Open path — a
synthetic ``require_human``
:class:`~trikon_cloud.fargate_runner.models.VerdictRow` plus a neutral
Check Run — and returns success so SQS deletes the record after the
verdict has been recorded (Invariant 2, Requirement 7.1).

Module-scope caches (design §4.1):

* :data:`_ENV` — :class:`OrchestratorEnvConfig` loaded once at cold
  start via pydantic-settings.
* :data:`_RESOLVER` — :class:`TaskDefinitionResolver` whose
  container-lifetime cache holds the ``trikon-verify-runner:<revision>``
  string after the first SSM read.
* :data:`_BOTO_SESSION` — one :class:`boto3.session.Session` shared
  across the SSM, ECS, and DynamoDB touchpoints. One SSM client from it
  serves both Secure_Parameter readers (App private key, Usage_Secret).
* :data:`_GITHUB_CLIENT` — one
  :class:`~trikon_cloud.orchestrator.never_fail_open.OrchestratorGithubClient`
  wrapping the App JWT / installation-token cache used by the
  Never-Fail-Open Check Run POST.

* :data:`_USAGE_CLIENT` — one
  :class:`~trikon_cloud.usage_client.client.UsageApiClient` (its own
  :class:`httpx.Client` and a
  :class:`~trikon_cloud.usage_client.secret.UsageSecretReader`) for the
  Quota_Endpoint check and the 0-minute release (sandbox-minute-plans).
* :data:`_SQS_CLIENT` — the SQS client that sends busy copies back to
  ``trikon-verify-jobs`` (sandbox-minute-plans Req 11.18).

:func:`_bootstrap` is idempotent: the first invocation pays the
init cost, every subsequent warm invocation is a no-op that returns
the cached six-tuple. Tests inject stubs by assigning the module
caches directly; :data:`_NOW_MS` is the injectable wall clock (Unix
milliseconds) used to measure a busy Job's wait.

**Quota step** (sandbox-minute-plans, design "Handler flow" STEP 4b):
after the job context is attached and before the task-definition
lookup, the handler asks the Quota_Endpoint whether the run may start.
``allowed`` dispatches with the Reservation_Id; ``already_settled``
returns success; ``denied`` and ``unavailable`` write a Fail_Closed_Result
and return success; ``busy`` re-queues a delayed copy or, past the
Queue_Wait_Limit, fails closed (:func:`handle_busy`). A Terminal
``RunTask`` error releases the Reservation with a 0-minute Usage_Report
before the existing Never-Fail-Open path.

**Retry_Jobs** (trikon-cloud-cost-cuts Requirements 9.4, 9.5, 11.5): an
Attempt 2 Job skips the Quota call (``quota_skipped_retry``) and
dispatches on FARGATE with its own Reservation_Id. ``RunTask`` returning
no task is Terminal (``aws_error_code="RunTaskNoTask"``) for every
attempt. A transient error on a Retry_Job's last receive
(``ApproximateReceiveCount >= TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT``) is
Terminal too. A Retry_Job's Terminal result uses
``action_required`` and completes the Interim_Check_Run recorded on
``trikon_pr_state``; Attempt 1 keeps the neutral result.

**Observability invariants** (Invariant 6, Requirement 9.3):

* The raw SQS record body is NEVER logged at any level. The
  payload-size gate emits a best-effort ``delivery_id`` extracted via
  :func:`_try_extract_delivery_id` — never the body itself.
* The ``ecs.RunTask`` response body is NEVER logged. The
  ``run_task_dispatched`` INFO record carries only
  ``dispatched_task_arn`` (Requirement 9.4) plus the request's
  ``attempt`` and ``capacity``; a no-task response contributes only
  ``failures[0].reason``, as ``run_task_failure_reason``.
* Credential material — App private key PEM, App JWT, installation
  token, SSM ``GetParameter`` response body, AWS access keys, the
  Usage_Secret, Usage_Signatures, and Usage_API request and response
  bodies — is NEVER logged. Enforced by call-site convention plus the ``LOGGING_DENYLIST``
  grep guard in ``test_logger.py``.

**Correlation invariant** (Requirement 1.4): ``delivery_id`` is the
single correlation key at this layer. The handler does NOT introduce a
separate ``audit_id`` — a downstream tier may add one, but the
orchestrator's job is to preserve the ``delivery_id`` byte-for-byte
from :class:`SqsJobMessage` through the ``TRIKON_DELIVERY_ID`` env
override on the dispatched task.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Protocol, cast

import boto3  # type: ignore[import-untyped]
import httpx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import (
    MAX_ATTEMPTS,
    Attempt,
    select_capacity,
)
from trikon_cloud.orchestrator.busy_requeue import (
    BusyTimedOut,
    _SqsClientProtocol,
    decide_busy,
    first_received_at_ms,
    send_busy_copy,
)
from trikon_cloud.orchestrator.ecs_dispatcher import (
    RunTaskNoTaskError,
    TaskDefinitionResolver,
    TransientDispatchError,
    build_run_task_call,
    classify_client_error,
    submit_run_task,
)
from trikon_cloud.orchestrator.logger import append_job_context, get_logger
from trikon_cloud.orchestrator.models import (
    OrchestratorEnvConfig,
    SqsEventEnvelope,
    SqsRecord,
    VerifyJobMessage,
)
from trikon_cloud.orchestrator.never_fail_open import (
    FailClosedNotice,
    OrchestratorGithubClient,
    read_interim_check_run_id,
    write_orchestrator_failure_verdict,
)
from trikon_cloud.orchestrator.quota_notices import (
    busy_timeout_notice,
    denial_notice,
    spot_retry_dispatch_failed_notice,
    usage_unavailable_notice,
)
from trikon_cloud.usage_client.client import UsageApiClient
from trikon_cloud.usage_client.models import (
    QuotaAllowed,
    QuotaAlreadySettled,
    QuotaBusy,
    QuotaDecision,
    QuotaDenied,
    QuotaRequest,
    QuotaUnavailable,
    UsageDelivered,
    UsageRejected,
    UsageReport,
    UsageReportUndeliveredError,
)
from trikon_cloud.usage_client.secret import UsageSecretReader
from trikon_cloud.webhook_receiver.models import SqsJobMessage

__all__ = ["handle_busy", "lambda_handler"]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# AWS SQS message body soft cap. Anything above this is either an SDK
# misuse or a producer bug; the payload-size gate rejects rather than
# retries because a redelivered oversize body would only re-fail
# (Requirement 2.1 — Terminal).
_PAYLOAD_SIZE_LIMIT_BYTES: int = 262_144  # 256 KiB

# Default timeout of the Usage_API :class:`httpx.Client`. Each attempt
# passes its own, smaller timeout derived from the remaining budget.
_USAGE_HTTP_TIMEOUT_SECONDS: float = 10.0

# ``aws_error_code`` of the Terminal path when ``ecs.RunTask`` returns no
# task (``tasks == []``, e.g. no Spot capacity; cost-cuts design D5).
_RUN_TASK_NO_TASK_CODE: str = "RunTaskNoTask"


class _UsageClient(Protocol):
    """Structural type of :class:`UsageApiClient`, so tests can inject a stub."""

    def check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision: ...

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected: ...


def _wall_clock_ms() -> int:
    """Unix wall time in whole milliseconds (the SQS timestamp unit)."""
    return time.time_ns() // 1_000_000


# ---------------------------------------------------------------------------
# Cold-start module caches (design §4.1).
# ---------------------------------------------------------------------------

_ENV: OrchestratorEnvConfig | None = None
_RESOLVER: TaskDefinitionResolver | None = None
_BOTO_SESSION: boto3.session.Session | None = None
_GITHUB_CLIENT: OrchestratorGithubClient | None = None
_USAGE_CLIENT: _UsageClient | None = None
_SQS_CLIENT: _SqsClientProtocol | None = None

#: Test seam: the wall clock (Unix ms) that measures a busy Job's wait.
_NOW_MS: Callable[[], int] = _wall_clock_ms


def _bootstrap() -> tuple[
    OrchestratorEnvConfig,
    TaskDefinitionResolver,
    boto3.session.Session,
    OrchestratorGithubClient,
    _UsageClient,
    _SqsClientProtocol,
]:
    """Lazily construct the six cold-start caches and return them.

    First invocation pays the full init cost: env-var parsing, boto3
    session construction, GitHub-client wiring (which itself carries a
    persistent :class:`httpx.Client`), the Usage_API client (its own
    :class:`httpx.Client` plus a :class:`UsageSecretReader`), and the SQS
    client for busy copies. Every subsequent invocation returns the
    cached six-tuple with no side effects. The Usage_Secret is read on the
    first Quota_Request, not here.

    Each :class:`httpx.Client` is owned by its client instance and
    therefore lives as long as that cache slot — the container lifetime.
    """
    global _ENV, _RESOLVER, _BOTO_SESSION, _GITHUB_CLIENT, _USAGE_CLIENT, _SQS_CLIENT
    if _ENV is None:
        _ENV = OrchestratorEnvConfig()
    if _RESOLVER is None:
        _RESOLVER = TaskDefinitionResolver(env=_ENV)
    if _BOTO_SESSION is None:
        _BOTO_SESSION = boto3.session.Session()
    if _GITHUB_CLIENT is None or _USAGE_CLIENT is None:
        # One SSM client serves both Secure_Parameter readers (the App
        # private key and the Usage_Secret). ``Session.client(...)`` is Any
        # (boto3 is untyped upstream); both readers take a structural
        # Protocol, so the Any-to-Protocol assignment is safe under
        # mypy --strict.
        ssm_client = _BOTO_SESSION.client("ssm")
        if _GITHUB_CLIENT is None:
            _GITHUB_CLIENT = OrchestratorGithubClient(
                app_id=_ENV.trikon_app_id,
                app_private_key_parameter_name=_ENV.trikon_app_private_key_parameter_name,
                ssm_client=ssm_client,
                http_client=httpx.Client(timeout=httpx.Timeout(10.0)),
            )
        if _USAGE_CLIENT is None:
            _USAGE_CLIENT = UsageApiClient(
                base_url=_ENV.trikon_web_api_url,
                secret_reader=UsageSecretReader(
                    parameter_name=_ENV.trikon_cloud_usage_secret_parameter_name,
                    ssm_client=ssm_client,
                ),
                http_client=httpx.Client(
                    timeout=httpx.Timeout(_USAGE_HTTP_TIMEOUT_SECONDS)
                ),
            )
    if _SQS_CLIENT is None:
        _SQS_CLIENT = cast(_SqsClientProtocol, _BOTO_SESSION.client("sqs"))
    return _ENV, _RESOLVER, _BOTO_SESSION, _GITHUB_CLIENT, _USAGE_CLIENT, _SQS_CLIENT


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _try_extract_delivery_id(body: str) -> str | None:
    """Best-effort ``delivery_id`` extraction for the payload-gate log.

    STEP 2 of the eight-step flow rejects oversized bodies BEFORE
    :class:`SqsJobMessage` validation runs, so ``delivery_id`` is not
    yet part of the log context. This helper does a defensive JSON
    decode purely to enrich the ``payload_exceeds_256kb`` ERROR record
    with a correlation id when one is extractable — a body that already
    breached the 256 KiB gate may not even parse as JSON, and this
    helper must never raise.

    Pure, no IO. Returns :data:`None` on any parse failure, on non-dict
    JSON roots, or on a missing / non-string ``delivery_id`` field.

    Parameters
    ----------
    body:
        The raw SQS record body — the same string that just failed the
        payload-size gate. Passed by value; never logged verbatim.

    Returns
    -------
    str | None
        The ``delivery_id`` if it decodes cleanly as a top-level string
        field, otherwise :data:`None`.
    """
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("delivery_id")
    if isinstance(value, str):
        return value
    return None


def _extract_aws_error_code(exc: ClientError) -> str:
    """Return the ``Error.Code`` string from a botocore :class:`ClientError`.

    :class:`ClientError.response` is untyped upstream (returns ``Any``);
    this helper narrows to :class:`str` via successive :func:`isinstance`
    checks so the caller's log records carry a stable ``aws_error_code``
    field. Returns ``"unknown"`` on any missing / non-string key rather
    than raising — a Lambda that crashes while trying to log a
    :class:`ClientError` is worse than one that logs an ``unknown``
    error code.
    """
    response_obj = exc.response
    if not isinstance(response_obj, dict):
        return "unknown"
    error_obj = response_obj.get("Error", {})
    if not isinstance(error_obj, dict):
        return "unknown"
    code_obj = error_obj.get("Code", "unknown")
    if not isinstance(code_obj, str):
        return "unknown"
    return code_obj


def _empty_batch_response() -> dict[str, object]:
    """Return the shared ``{"batchItemFailures": []}`` response.

    Every non-Transient exit from the handler returns this dict — SQS
    reads ``batchItemFailures`` and deletes each record whose
    ``messageId`` is NOT in the list. An empty list therefore means
    "delete every record in the batch" — which, at ``BatchSize=1``, is
    the single message just processed. Transient paths do NOT return
    from the handler at all; they raise
    :class:`TransientDispatchError` so the Lambda runtime marks the
    invocation as a failure and SQS re-drives the message
    (Requirement 6.1).
    """
    return {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# Quota step helpers (sandbox-minute-plans, design "Handler flow").
# ---------------------------------------------------------------------------


def _quota_outcome(decision: QuotaDecision) -> str:
    """The ``quota_outcome`` log value for ``decision`` (Req 11.17)."""
    if isinstance(decision, QuotaAllowed):
        return "allowed"
    if isinstance(decision, QuotaAlreadySettled):
        return "already_settled"
    if isinstance(decision, QuotaDenied):
        return "denied"
    if isinstance(decision, QuotaBusy):
        return "busy"
    return "unavailable"


def _check_quota(
    message: SqsJobMessage,
    *,
    usage_client: _UsageClient,
    env: OrchestratorEnvConfig,
) -> QuotaDecision:
    """STEP 4b: ask the Quota_Endpoint whether this run may start (Req 11.1).

    The Run_Id is the Job's ``delivery_id``. :class:`QuotaRequest` is
    stricter than :class:`SqsJobMessage` (Run_Id and repository name
    patterns, installation id range); a Job it rejects could only get a
    400 from Trikon_Web, so it fails closed as
    ``QuotaUnavailable("malformed")`` without a request.
    """
    try:
        request = QuotaRequest(
            installation_id=message.installation_id,
            run_id=message.delivery_id,
            repository_full_name=message.repo_full_name,
            pr_number=message.pr_number,
        )
    except ValidationError:
        return QuotaUnavailable(cause="malformed", status=None)
    return usage_client.check_quota(request, env.trikon_quota_time_budget_seconds)


def _write_fail_closed(
    notice: FailClosedNotice,
    *,
    message: SqsJobMessage,
    env: OrchestratorEnvConfig,
    boto_session: boto3.session.Session,
    github_client: OrchestratorGithubClient,
) -> dict[str, object]:
    """Write a quota Fail_Closed_Result and return success (Req 11.9-11.11).

    A non-conditional DynamoDB error propagates, so SQS redrives the Job
    (the existing Never-Fail-Open behaviour).
    """
    write_orchestrator_failure_verdict(
        sqs_message=message,
        error_class=notice.error_class,
        error_code=notice.error_code,
        boto3_session=boto_session,
        github_client=github_client,
        env=env,
        notice=notice,
    )
    return _empty_batch_response()


def _release_reservation(
    *,
    usage_client: _UsageClient,
    run_id: str,
    reservation_id: str,
) -> None:
    """Release the Reservation with a 0-minute Usage_Report (Req 11.12, 12.12).

    Never raises: raising would redeliver the Job, re-admit it under the
    same open Reservation, and fail RunTask again. An undelivered
    Reservation stops holding minutes after the Reservation_Timeout.
    """
    logger = get_logger()
    try:
        result = usage_client.report_usage(
            UsageReport(
                run_id=run_id,
                reservation_id=reservation_id,
                billed_minutes=0,
                started_at=None,
                stopped_at=None,
            )
        )
    except UsageReportUndeliveredError:
        logger.error("usage_release_failed", status=None)
        return
    except Exception:
        # Defensive: the release must never fail the terminal path.
        logger.error("usage_release_failed", status=None)
        return
    if isinstance(result, UsageDelivered):
        logger.info("usage_release_delivered", usage_outcome=result.outcome)
        return
    logger.error("usage_release_failed", status=result.status)


def handle_busy(
    record: SqsRecord,
    decision: QuotaBusy,
    env: OrchestratorEnvConfig,
    sqs_client: _SqsClientProtocol,
    now_ms: int,
    *,
    message: SqsJobMessage,
    boto_session: boto3.session.Session,
    github_client: OrchestratorGithubClient,
) -> dict[str, object]:
    """Handle a ``busy`` quota outcome (Req 11.18-11.21, design "Busy Jobs").

    Within the Queue_Wait_Limit, sends a delayed copy of the Job and
    returns no batch item failures, so SQS deletes the original; if the
    copy cannot be sent, lists the record so the original stays in the
    queue. Past the limit, writes the busy-timeout Fail_Closed_Result and
    returns success. Never calls ``ecs.RunTask``; on the re-queue path it
    posts no Check Run and writes no verdict row.
    """
    logger = get_logger()
    first = first_received_at_ms(record, now_ms=now_ms)
    outcome = decide_busy(
        first_received_at_ms=first,
        now_ms=now_ms,
        retry_after_seconds=decision.retry_after_seconds,
        concurrency_limit=decision.concurrency_limit,
        wait_limit_seconds=env.trikon_queue_wait_limit_seconds,
    )
    if isinstance(outcome, BusyTimedOut):
        logger.info("busy_wait_exceeded", waited_seconds=outcome.waited_seconds)
        notice = busy_timeout_notice(
            outcome.concurrency_limit,
            env.trikon_queue_wait_limit_seconds,
            env.trikon_web_app_url,
        )
        return _write_fail_closed(
            notice,
            message=message,
            env=env,
            boto_session=boto_session,
            github_client=github_client,
        )
    sent = send_busy_copy(
        sqs_client,
        record,
        queue_url=env.trikon_verify_jobs_queue_url,
        decision=outcome,
    )
    if not sent:
        # ``send_busy_copy`` already logged WARNING ``busy_requeue_failed``.
        return {"batchItemFailures": [{"itemIdentifier": record.messageId}]}
    logger.info(
        "busy_requeued",
        waited_seconds=max(0, now_ms - first) // 1000,
        delay_seconds=outcome.delay_seconds,
    )
    return _empty_batch_response()


# ---------------------------------------------------------------------------
# Terminal dispatch path (cost-cuts Req 11.5).
# ---------------------------------------------------------------------------


def _interim_check_run_id(
    message: VerifyJobMessage,
    *,
    env: OrchestratorEnvConfig,
    boto_session: boto3.session.Session,
) -> int | None:
    """The Interim_Check_Run id a Retry_Job's Runner left on ``trikon_pr_state``.

    ``None`` when the row is missing, on another head, or unreadable;
    the Fail_Closed_Result then creates a new Check Run.
    """
    return read_interim_check_run_id(
        boto_session.client("dynamodb"),
        table=env.trikon_pr_state_table,
        pr_key=f"{message.installation_id}#{message.repo_full_name}#{message.pr_number}",
        head_sha=message.head_sha,
    )


def _dispatch_terminal(
    *,
    message: VerifyJobMessage,
    aws_error_code: str,
    reservation_id: str,
    env: OrchestratorEnvConfig,
    boto_session: boto3.session.Session,
    github_client: OrchestratorGithubClient,
    usage_client: _UsageClient,
) -> dict[str, object]:
    """Release the Reservation, then fail closed; return success.

    Attempt 1 keeps the existing RunTask-terminal result (neutral Check
    Run, ``matched_rule="orchestrator terminal failure"``). A Retry_Job
    gets the Spot dispatch-failed notice (``action_required``) and
    completes the Interim_Check_Run when one is recorded for this head
    (cost-cuts Req 11.5, 11.7).
    """
    # Release first (sandbox-minute-plans Req 11.12, 12.12); never raises.
    _release_reservation(
        usage_client=usage_client,
        run_id=message.delivery_id,
        reservation_id=reservation_id,
    )
    if message.attempt != MAX_ATTEMPTS:
        write_orchestrator_failure_verdict(
            sqs_message=message,
            error_class="terminal",
            error_code=aws_error_code,
            boto3_session=boto_session,
            github_client=github_client,
            env=env,
        )
        return _empty_batch_response()
    details_url = env.trikon_check_run_details_url_template.format(
        delivery_id=message.delivery_id,
    )
    notice = spot_retry_dispatch_failed_notice(
        message.delivery_id, details_url, aws_error_code
    )
    write_orchestrator_failure_verdict(
        sqs_message=message,
        error_class=notice.error_class,
        error_code=notice.error_code,
        boto3_session=boto_session,
        github_client=github_client,
        env=env,
        notice=notice,
        complete_check_run_id=_interim_check_run_id(
            message, env=env, boto_session=boto_session
        ),
    )
    return _empty_batch_response()


# ---------------------------------------------------------------------------
# Lambda entry point — the eight-step flow (design §4.1).
# ---------------------------------------------------------------------------


def lambda_handler(
    event: dict[str, object],
    context: LambdaContext,
) -> dict[str, object]:
    """Dispatch one SQS-delivered :class:`SqsJobMessage` to ``ecs.RunTask``.

    Implements the eight-step per-record flow from design §4.1 verbatim.
    Every early return follows the batch-item-failure protocol: an empty
    ``batchItemFailures`` list tells SQS to delete the message, and a
    raised :class:`TransientDispatchError` tells the Lambda runtime to
    fail the batch so SQS redrives.

    Parameters
    ----------
    event:
        The raw AWS Lambda SQS event envelope. Validated into an
        :class:`SqsEventEnvelope` on entry; batch size 1 is asserted
        immediately after (Requirement 1.1).
    context:
        The AWS Lambda runtime context. Unused by the handler body —
        every correlation key comes from either the SQS record
        attributes (``sqs_message_id``, ``approximate_receive_count``)
        or the validated :class:`SqsJobMessage` (``installation_id``,
        ``repo_full_name``, ``pr_number``, ``delivery_id``,
        ``event_type``). Requirement 1.4 pins ``delivery_id`` as the
        single correlation key — the handler does NOT introduce a
        separate ``audit_id``.

    Returns
    -------
    dict[str, object]
        Always ``{"batchItemFailures": []}`` on any non-Transient exit.
        Transient failures never return — they raise
        :class:`TransientDispatchError`.
    """
    del context  # Unused — see docstring for correlation-key rationale.
    logger = get_logger()
    env, resolver, boto_session, github_client, usage_client, sqs_client = _bootstrap()

    envelope = SqsEventEnvelope.model_validate(event)
    # SQS event-source mapping is configured with ``BatchSize=1`` per
    # Requirement 1.1. The assertion is a defensive guard — production
    # never trips it, but a misconfigured event-source mapping would
    # otherwise silently drop the tail of a multi-record batch.
    assert len(envelope.Records) == 1, "SQS batch size must be 1 (Req 1.1)"
    record = envelope.Records[0]

    # STEP 1 — Attach SQS-level correlation keys BEFORE any parse so
    # payload-gate and validation failures still carry the message id
    # and receive count on their ERROR records (design §4.1 STEP 1,
    # Requirement 9.2).
    logger.append_keys(
        sqs_message_id=record.messageId,
        approximate_receive_count=int(record.attributes.ApproximateReceiveCount),
    )

    # STEP 2 — Payload-size gate. Bodies above 256 KiB are Terminal
    # (Requirement 2.1): a redelivered oversize body only re-fails, so
    # the correct action is to delete from the queue. The
    # :func:`_try_extract_delivery_id` best-effort parse enriches the
    # log record when possible without ever raising.
    body_bytes = record.body.encode("utf-8")
    if len(body_bytes) > _PAYLOAD_SIZE_LIMIT_BYTES:
        logger.error(
            "orchestrator_payload_rejected",
            reason="payload_exceeds_256kb",
            byte_count=len(body_bytes),
            delivery_id=_try_extract_delivery_id(record.body),
        )
        return _empty_batch_response()

    # STEP 3 — Parse :class:`VerifyJobMessage` (a :class:`SqsJobMessage`
    # plus the optional ``attempt`` / ``reservation_id``; receiver bodies
    # parse as Attempt 1 — cost-cuts Req 8.8). Malformed bodies, including
    # an invalid Attempt (cost-cuts Req 8.7), are Terminal
    # (Requirement 2.2): a redelivered malformed body only
    # re-fails. The error record carries the pydantic ``loc`` / ``type``
    # tuples so operators can diagnose the producer bug without the
    # raw body ever entering the log surface (Invariant 6).
    try:
        message = VerifyJobMessage.model_validate_json(record.body)
    except ValidationError as exc:
        logger.error(
            "orchestrator_payload_rejected",
            reason="malformed_sqs_body",
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        return _empty_batch_response()

    # STEP 4 — Attach the five natural-key context fields
    # (installation_id, repo_full_name, pr_number, delivery_id,
    # event_type) so every subsequent log record on this invocation is
    # navigable by any of them (Requirement 9.2).
    append_job_context(logger, message=message)

    # STEP 4b — Quota check (sandbox-minute-plans Req 11.1), before the
    # task-definition lookup and ``ecs.RunTask``. A Retry_Job (Attempt 2)
    # carries its Reservation_Id and makes no Quota call, so it leaves the
    # installation's usage unchanged (cost-cuts Req 9.4, 9.5).
    # The Spot_Retry_Handler is the only producer of Attempt 2, and
    # ``VerifyJobMessage`` guarantees it carries ``reservation_id``.
    # Attempt 1 checks the quota; only the outcome and Denial_Reason are
    # logged (Req 11.17), and ``check_quota`` never raises.
    reservation_id: str
    if message.attempt == MAX_ATTEMPTS and message.reservation_id is not None:
        logger.info("quota_skipped_retry", attempt=message.attempt)
        reservation_id = message.reservation_id
    else:
        decision = _check_quota(message, usage_client=usage_client, env=env)
        logger.info(
            "quota_checked",
            quota_outcome=_quota_outcome(decision),
            denial_reason=decision.reason if isinstance(decision, QuotaDenied) else None,
        )
        if isinstance(decision, QuotaAlreadySettled):
            # Req 11.7: nothing to run and nothing to post; SQS deletes the Job.
            logger.info("quota_already_settled")
            return _empty_batch_response()
        if isinstance(decision, QuotaDenied):
            # Req 11.4-11.6, 11.9-11.11.
            return _write_fail_closed(
                denial_notice(decision.reason, decision.period_end, env.trikon_web_app_url),
                message=message,
                env=env,
                boto_session=boto_session,
                github_client=github_client,
            )
        if isinstance(decision, QuotaBusy):
            # Req 11.18-11.21.
            return handle_busy(
                record,
                decision,
                env,
                sqs_client,
                _NOW_MS(),
                message=message,
                boto_session=boto_session,
                github_client=github_client,
            )
        if isinstance(decision, QuotaUnavailable):
            # Req 11.8-11.11.
            details_url = env.trikon_check_run_details_url_template.format(
                delivery_id=message.delivery_id,
            )
            return _write_fail_closed(
                usage_unavailable_notice(message.delivery_id, details_url, cause=decision.cause),
                message=message,
                env=env,
                boto_session=boto_session,
                github_client=github_client,
            )
        reservation_id = decision.reservation_id

    # STEP 5 — Resolve the active ``trikon-verify-runner`` revision
    # (Requirement 4.1). First warm invocation pays the SSM
    # ``GetParameter`` cost; subsequent invocations hit the resolver's
    # container-lifetime cache. Requirement 4.2 forbids ``:LATEST`` —
    # the resolver enforces an ``int >= 1`` value.
    ssm_client = boto_session.client("ssm")
    task_definition = resolver.resolve(ssm_client=ssm_client)

    # STEP 6 — Build the ``ecs.RunTask`` request body (pure, no IO),
    # carrying the ``allowed`` decision's Reservation_Id
    # (sandbox-minute-plans Req 11.3) or the Retry_Job's own
    # (cost-cuts Req 9.4). Attempt 1 runs on FARGATE_SPOT
    # while the Spot_Flag is on; everything else runs on FARGATE
    # (cost-cuts Req 8.3-8.6). ``VerifyJobMessage`` bounds ``attempt``
    # to 1..2, so the cast to :data:`Attempt` is exact.
    attempt = cast(Attempt, message.attempt)
    capacity = select_capacity(use_spot=env.trikon_use_fargate_spot, attempt=attempt)
    call = build_run_task_call(
        message,
        env=env,
        task_definition=task_definition,
        reservation_id=reservation_id,
        attempt=attempt,
        capacity=capacity,
    )

    # STEP 7 — Dispatch. Exactly one ``ecs.RunTask`` API call per
    # invocation (Requirement 3.1). ``ClientError`` is classified via
    # :func:`classify_client_error`; other ``BotoCoreError`` subclasses
    # (endpoint / read timeout, DNS failure) propagate unhandled — the
    # Lambda infra marks the batch as failed and SQS redrives, which is
    # the correct Transient-equivalent behavior (design §4.5).
    #
    # Every Terminal exit goes through :func:`_dispatch_terminal`:
    # release the Reservation, then the Never-Fail-Open result (Attempt 1:
    # the existing neutral result; Retry_Job: ``action_required``,
    # completing the Interim_Check_Run — cost-cuts Req 11.5, 11.7).
    def _terminal(aws_error_code: str) -> dict[str, object]:
        return _dispatch_terminal(
            message=message,
            aws_error_code=aws_error_code,
            reservation_id=reservation_id,
            env=env,
            boto_session=boto_session,
            github_client=github_client,
            usage_client=usage_client,
        )

    ecs_client = boto_session.client("ecs")
    try:
        result = submit_run_task(call, ecs_client=ecs_client)
    except RunTaskNoTaskError as exc:
        # ``tasks == []`` (e.g. no Spot capacity) is Terminal: a redrive
        # would only dead-letter the Job with no Check Run (cost-cuts D5).
        # Only ``failures[0].reason`` — an AWS-controlled string — is
        # logged, never the response body (Invariant 6).
        logger.error(
            "run_task_terminal_failure",
            aws_error_code=_RUN_TASK_NO_TASK_CODE,
            error_class="terminal",
            run_task_failure_reason=exc.reason,
        )
        return _terminal(_RUN_TASK_NO_TASK_CODE)
    except ClientError as exc:
        classification = classify_client_error(exc)
        aws_error_code = _extract_aws_error_code(exc)
        receive_count = int(record.attributes.ApproximateReceiveCount)
        last_retry_receive = (
            message.attempt == MAX_ATTEMPTS
            and receive_count >= env.trikon_verify_jobs_max_receive_count
        )
        if classification == "transient" and not last_retry_receive:
            # Transient: raise so the Lambda runtime marks the batch
            # as a failure and SQS returns the message to the queue
            # (Requirement 6.1). Retry counts and DLQ handling are
            # SQS-level concerns; the handler stays stateless. The
            # Reservation stays open; the redelivery is admitted under
            # the same id (sandbox-minute-plans Req 11.13).
            logger.warning(
                "run_task_transient_failure",
                aws_error_code=aws_error_code,
                error_class="transient",
            )
            raise TransientDispatchError from exc
        # Terminal: Never-Fail-Open. Write the synthetic
        # ``require_human`` verdict + post the Check Run, then return
        # success so SQS deletes the message (Requirement 7.1). A
        # transient error on a Retry_Job's last receive is Terminal too,
        # so the PR never dead-letters with only an Interim_Check_Run
        # (cost-cuts Req 11.5).
        if classification == "transient":
            logger.error(
                "run_task_terminal_failure",
                aws_error_code=aws_error_code,
                error_class="terminal",
                retry_receive_exhausted=True,
            )
        else:
            logger.error(
                "run_task_terminal_failure",
                aws_error_code=aws_error_code,
                error_class="terminal",
            )
        return _terminal(aws_error_code)

    # STEP 8 — Success. Log the dispatched task ARN (Requirement 9.4)
    # — the ONLY field extracted from the ``ecs.RunTask`` response, so
    # the response body itself never enters the log surface
    # (Invariant 6) — with the Attempt and capacity (cost-cuts Req 8.6).
    logger.info(
        "run_task_dispatched",
        dispatched_task_arn=result.task_arn,
        attempt=attempt,
        capacity=capacity,
    )
    return _empty_batch_response()
