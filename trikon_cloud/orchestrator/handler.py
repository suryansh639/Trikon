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
  across the SSM, ECS, DynamoDB, and Secrets Manager touchpoints.
* :data:`_GITHUB_CLIENT` — one
  :class:`~trikon_cloud.orchestrator.never_fail_open.OrchestratorGithubClient`
  wrapping the App JWT / installation-token cache used by the
  Never-Fail-Open Check Run POST.

:func:`_bootstrap` is idempotent: the first invocation pays the
init cost, every subsequent warm invocation is a no-op that returns
the cached four-tuple.

**Observability invariants** (Invariant 6, Requirement 9.3):

* The raw SQS record body is NEVER logged at any level. The
  payload-size gate emits a best-effort ``delivery_id`` extracted via
  :func:`_try_extract_delivery_id` — never the body itself.
* The ``ecs.RunTask`` response body is NEVER logged. The
  ``run_task_dispatched`` INFO record carries only
  ``dispatched_task_arn`` (Requirement 9.4).
* Credential material — App private key PEM, App JWT, installation
  token, Secrets Manager response body, AWS access keys — is NEVER
  logged. Enforced by call-site convention plus the ``LOGGING_DENYLIST``
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

import boto3  # type: ignore[import-untyped]
import httpx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from trikon_cloud.orchestrator.ecs_dispatcher import (
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
)
from trikon_cloud.orchestrator.never_fail_open import (
    OrchestratorGithubClient,
    write_orchestrator_failure_verdict,
)
from trikon_cloud.webhook_receiver.models import SqsJobMessage

__all__ = ["lambda_handler"]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# AWS SQS message body soft cap. Anything above this is either an SDK
# misuse or a producer bug; the payload-size gate rejects rather than
# retries because a redelivered oversize body would only re-fail
# (Requirement 2.1 — Terminal).
_PAYLOAD_SIZE_LIMIT_BYTES: int = 262_144  # 256 KiB


# ---------------------------------------------------------------------------
# Cold-start module caches (design §4.1).
# ---------------------------------------------------------------------------

_ENV: OrchestratorEnvConfig | None = None
_RESOLVER: TaskDefinitionResolver | None = None
_BOTO_SESSION: boto3.session.Session | None = None
_GITHUB_CLIENT: OrchestratorGithubClient | None = None


def _bootstrap() -> tuple[
    OrchestratorEnvConfig,
    TaskDefinitionResolver,
    boto3.session.Session,
    OrchestratorGithubClient,
]:
    """Lazily construct the four cold-start caches and return them.

    First invocation pays the full init cost: env-var parsing, boto3
    session construction, GitHub-client wiring (which itself carries a
    persistent :class:`httpx.Client`). Every subsequent invocation
    returns the cached four-tuple with no side effects.

    The GitHub client's :class:`httpx.Client` is owned by the client
    instance and therefore lives as long as :data:`_GITHUB_CLIENT`
    itself — the container lifetime. Not exported as its own cache slot
    because the task-defined cache surface is exactly the four values
    listed above (design §4.1).
    """
    global _ENV, _RESOLVER, _BOTO_SESSION, _GITHUB_CLIENT
    if _ENV is None:
        _ENV = OrchestratorEnvConfig()
    if _RESOLVER is None:
        _RESOLVER = TaskDefinitionResolver(env=_ENV)
    if _BOTO_SESSION is None:
        _BOTO_SESSION = boto3.session.Session()
    if _GITHUB_CLIENT is None:
        # ``boto3.session.Session.client(...)`` is Any (boto3 is untyped
        # upstream); the target parameter type on
        # :class:`OrchestratorGithubClient` is a structural Protocol so
        # the Any-to-Protocol assignment is safe under mypy --strict.
        secrets_client = _BOTO_SESSION.client("secretsmanager")
        _GITHUB_CLIENT = OrchestratorGithubClient(
            app_id=_ENV.trikon_app_id,
            app_private_key_secret_arn=_ENV.trikon_app_private_key_secret_arn,
            secrets_client=secrets_client,
            http_client=httpx.Client(timeout=httpx.Timeout(10.0)),
        )
    return _ENV, _RESOLVER, _BOTO_SESSION, _GITHUB_CLIENT


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
    env, resolver, boto_session, github_client = _bootstrap()

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

    # STEP 3 — Parse :class:`SqsJobMessage`. Malformed bodies are
    # Terminal (Requirement 2.2): a redelivered malformed body only
    # re-fails. The error record carries the pydantic ``loc`` / ``type``
    # tuples so operators can diagnose the producer bug without the
    # raw body ever entering the log surface (Invariant 6).
    try:
        message = SqsJobMessage.model_validate_json(record.body)
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

    # STEP 5 — Resolve the active ``trikon-verify-runner`` revision
    # (Requirement 4.1). First warm invocation pays the SSM
    # ``GetParameter`` cost; subsequent invocations hit the resolver's
    # container-lifetime cache. Requirement 4.2 forbids ``:LATEST`` —
    # the resolver enforces an ``int >= 1`` value.
    ssm_client = boto_session.client("ssm")
    task_definition = resolver.resolve(ssm_client=ssm_client)

    # STEP 6 — Build the ``ecs.RunTask`` request body (pure, no IO).
    call = build_run_task_call(message, env=env, task_definition=task_definition)

    # STEP 7 — Dispatch. Exactly one ``ecs.RunTask`` API call per
    # invocation (Requirement 3.1). ``ClientError`` is classified via
    # :func:`classify_client_error`; other ``BotoCoreError`` subclasses
    # (endpoint / read timeout, DNS failure) propagate unhandled — the
    # Lambda infra marks the batch as failed and SQS redrives, which is
    # the correct Transient-equivalent behavior (design §4.5).
    ecs_client = boto_session.client("ecs")
    try:
        result = submit_run_task(call, ecs_client=ecs_client)
    except ClientError as exc:
        classification = classify_client_error(exc)
        aws_error_code = _extract_aws_error_code(exc)
        if classification == "transient":
            # Transient: raise so the Lambda runtime marks the batch
            # as a failure and SQS returns the message to the queue
            # (Requirement 6.1). Retry counts and DLQ handling are
            # SQS-level concerns; the handler stays stateless.
            logger.warning(
                "run_task_transient_failure",
                aws_error_code=aws_error_code,
                error_class="transient",
            )
            raise TransientDispatchError from exc
        # Terminal: Never-Fail-Open. Write the synthetic
        # ``require_human`` verdict + post a neutral Check Run, then
        # return success so SQS deletes the message (Requirement 7.1).
        logger.error(
            "run_task_terminal_failure",
            aws_error_code=aws_error_code,
            error_class="terminal",
        )
        write_orchestrator_failure_verdict(
            sqs_message=message,
            error_class="terminal",
            error_code=aws_error_code,
            boto3_session=boto_session,
            github_client=github_client,
            env=env,
        )
        return _empty_batch_response()

    # STEP 8 — Success. Log the dispatched task ARN (Requirement 9.4)
    # — the ONLY field extracted from the ``ecs.RunTask`` response, so
    # the response body itself never enters the log surface
    # (Invariant 6).
    logger.info(
        "run_task_dispatched",
        dispatched_task_arn=result.task_arn,
    )
    return _empty_batch_response()
