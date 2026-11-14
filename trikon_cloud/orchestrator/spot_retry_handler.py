"""Lambda handler for the Trikon Cloud Spot_Retry_Handler (cost-cuts Req 9, 11).

The ``trikon-verify-runner-task-stopped`` EventBridge rule invokes this
Lambda asynchronously with one "ECS Task State Change" event. The handler
re-queues one on-demand Retry_Job for a Spot-interrupted Attempt 1
Sandbox_Task, and publishes a Fail_Closed_Result on every path that cannot
end in a real verdict:

1. Parse the event into :class:`EcsTaskStateChangeEvent`. An event that does
   not parse raises :class:`SpotEventError` with no pydantic context (Req 9.8).
2. :func:`decide_stopped_task` (pure) decides. :class:`Ignore` logs
   ``spot_event_ignored`` and returns (Req 9.6). :class:`Unroutable` logs
   ``spot_event_unroutable`` and raises :class:`SpotEventError` (Req 11.4).
3. Claim the task ARN in ``trikon-cloud-spot-retries`` and resolve the
   claim (D3). ``duplicate`` returns. ``in_flight`` raises
   :class:`SpotRetryInFlightError`, so Lambda retries the event later.
4. A :class:`Requeue` with a ``proceed`` claim sends the Retry_Job, the only
   ``SendMessage`` the handler ever makes (Req 9.3). A successful send marks
   the claim ``sent``. A failed send fails closed with ``retry_send_failed``
   (Req 11.1). A :class:`Requeue` with a stale claim never sends and fails
   closed with ``retry_claim_stale``.
5. Every :class:`FailClosed` publishes through
   :func:`publish_fail_closed_result`, completing the Interim_Check_Run when
   ``trikon_pr_state`` records one on the same head (Req 11.1-11.4, 11.7).
   A publish failure raises and leaves the claim ``claimed``, so the event
   is retried and finally dead-lettered (Req 11.6). A successful publish
   marks the claim ``failed_closed``.

:func:`_mark_best_effort` never raises: a raise after a successful send
would make a later delivery see a stale claim and fail closed while the
retry runs. It logs ``spot_retry_claim_update_failed`` instead.

Module-scope caches: :func:`_bootstrap` loads :class:`SpotRetryEnvConfig`,
one boto3 session with its SQS, DynamoDB and SSM clients, the
:class:`SpotRetryClaims` store and one :class:`OrchestratorGithubClient` on
the first invocation, and reuses them for the container lifetime. It is
idempotent. Tests reset the caches with :func:`_reset_bootstrap` or inject
stubs by assigning the module caches directly; :data:`_NOW_MS` is the
injectable wall clock (Unix milliseconds).

Logging (Req 9.8): fixed event names with ``task_arn``, ``run_id``,
``reason`` and ``aws_error_code`` only, passed per call (never
``append_keys``), so a warm container never carries one task's ids into the
next invocation. Never the event, its Runner_Overrides, the Retry_Job body,
pydantic error text, a GitHub response or any credential.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final, Literal, Protocol, cast

import boto3  # type: ignore[import-untyped]
import httpx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import RUN_ID_ENV
from trikon_cloud.orchestrator.models import SpotRetryEnvConfig, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import (
    FailClosedPublication,
    OrchestratorGithubClient,
    _DynamoDbClientProtocol,
    _DynamoDbGetItemProtocol,
    publish_fail_closed_result,
    read_interim_check_run_id,
)
from trikon_cloud.orchestrator.quota_notices import spot_fail_closed_notice
from trikon_cloud.orchestrator.spot_retry import (
    FailClosed,
    Ignore,
    Requeue,
    SpotEventError,
    Unroutable,
    decide_stopped_task,
)
from trikon_cloud.orchestrator.spot_retry_claims import SpotRetryClaims, resolve_claim
from trikon_cloud.orchestrator.spot_retry_logger import get_logger
from trikon_cloud.usage_reporter.models import EcsTaskStateChangeEvent

__all__ = ["SpotEventError", "SpotRetryInFlightError", "lambda_handler"]


# ---------------------------------------------------------------------------
# Constants and types.
# ---------------------------------------------------------------------------

#: Timeout of the GitHub :class:`httpx.Client`, as for the Orchestrator.
_GITHUB_HTTP_TIMEOUT_SECONDS: Final = 10.0

#: ``aws_error_code`` when a ``ClientError`` carries no readable code.
_UNKNOWN_ERROR_CODE: Final = "unknown"

#: Rendered into the details URL when the Run_Id is unknown.
_UNKNOWN_DELIVERY_ID: Final = "unknown"


class SpotRetryInFlightError(Exception):
    """A live invocation holds the claim for this task (Req 9.3).

    Raised so Lambda's async retries redeliver the event after the claimant
    has finished; the redelivery then sees ``duplicate`` or a stale claim.
    The message is fixed and names no id.
    """


class _SqsSendProtocol(Protocol):
    """Structural type for the one boto3 SQS call the handler makes."""

    def send_message(
        self,
        *,
        QueueUrl: str,  # noqa: N803 -- AWS boto3 API contract
        MessageBody: str,  # noqa: N803
    ) -> object: ...


class _SpotRetryDynamoDbProtocol(_DynamoDbClientProtocol, _DynamoDbGetItemProtocol, Protocol):
    """The verdict ``PutItem`` and the ``trikon_pr_state`` ``GetItem``."""


def _wall_clock_ms() -> int:
    """Unix wall time in whole milliseconds."""
    return time.time_ns() // 1_000_000


# ---------------------------------------------------------------------------
# Cold-start module caches.
# ---------------------------------------------------------------------------

_ENV: SpotRetryEnvConfig | None = None
_BOTO_SESSION: boto3.session.Session | None = None
_CLAIMS: SpotRetryClaims | None = None
_SQS_CLIENT: _SqsSendProtocol | None = None
_DDB_CLIENT: _SpotRetryDynamoDbProtocol | None = None
_GITHUB_CLIENT: OrchestratorGithubClient | None = None

#: Test seam: the wall clock (Unix ms) used for claim times.
_NOW_MS: Callable[[], int] = _wall_clock_ms


def _bootstrap() -> tuple[
    SpotRetryEnvConfig,
    SpotRetryClaims,
    _SqsSendProtocol,
    _SpotRetryDynamoDbProtocol,
    OrchestratorGithubClient,
    Callable[[], int],
]:
    """Build the missing cold-start caches and return them with the clock.

    The first invocation parses the environment (a missing or malformed
    variable raises :class:`pydantic.ValidationError`), builds one boto3
    session and its clients, the claim store and the GitHub client. Every
    later invocation returns the cached values with no side effects. The App
    private key is read on the first Check Run, not here.

    ``Session.client(...)`` is ``Any`` (boto3 is untyped upstream); each
    consumer takes a structural Protocol, so the assignments are safe under
    ``mypy --strict``.
    """
    global _ENV, _BOTO_SESSION, _CLAIMS, _SQS_CLIENT, _DDB_CLIENT, _GITHUB_CLIENT
    if _ENV is None:
        _ENV = SpotRetryEnvConfig()
    if _BOTO_SESSION is None:
        _BOTO_SESSION = boto3.session.Session()
    if _DDB_CLIENT is None or _CLAIMS is None:
        # One DynamoDB client serves the verdict write, the pr-state read and
        # the claim store.
        ddb_client = _BOTO_SESSION.client("dynamodb")
        if _DDB_CLIENT is None:
            _DDB_CLIENT = cast(_SpotRetryDynamoDbProtocol, ddb_client)
        if _CLAIMS is None:
            _CLAIMS = SpotRetryClaims(
                table_name=_ENV.trikon_spot_retries_table,
                ddb_client=ddb_client,
            )
    if _SQS_CLIENT is None:
        _SQS_CLIENT = cast(_SqsSendProtocol, _BOTO_SESSION.client("sqs"))
    if _GITHUB_CLIENT is None:
        _GITHUB_CLIENT = OrchestratorGithubClient(
            app_id=_ENV.trikon_app_id,
            app_private_key_parameter_name=_ENV.trikon_app_private_key_parameter_name,
            ssm_client=_BOTO_SESSION.client("ssm"),
            http_client=httpx.Client(timeout=httpx.Timeout(_GITHUB_HTTP_TIMEOUT_SECONDS)),
        )
    return _ENV, _CLAIMS, _SQS_CLIENT, _DDB_CLIENT, _GITHUB_CLIENT, _NOW_MS


def _reset_bootstrap() -> None:
    """Test seam: drop every cached value so the next call rebuilds them."""
    global _ENV, _BOTO_SESSION, _CLAIMS, _SQS_CLIENT, _DDB_CLIENT, _GITHUB_CLIENT
    _ENV = None
    _BOTO_SESSION = None
    _CLAIMS = None
    _SQS_CLIENT = None
    _DDB_CLIENT = None
    _GITHUB_CLIENT = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _aws_error_code(exc: BaseException) -> str:
    """Return a loggable error code: ``Error.Code`` of a ``ClientError``, else the type name.

    Never returns exception text, which may echo request values.
    """
    if not isinstance(exc, ClientError):
        return type(exc).__name__
    response = cast(object, exc.response)
    if not isinstance(response, dict):
        return _UNKNOWN_ERROR_CODE
    error = response.get("Error")
    if not isinstance(error, dict):
        return _UNKNOWN_ERROR_CODE
    code = error.get("Code")
    return code if isinstance(code, str) and code else _UNKNOWN_ERROR_CODE


def _parse_event(event: dict[str, object]) -> EcsTaskStateChangeEvent:
    """Validate the raw event, or raise :class:`SpotEventError` (Req 9.8).

    The error is raised outside the ``except`` block, so neither
    ``__cause__`` nor ``__context__`` carries the pydantic error, whose text
    echoes event values.
    """
    try:
        return EcsTaskStateChangeEvent.model_validate(event)
    except ValidationError:
        pass
    get_logger().error("spot_event_unparseable")
    raise SpotEventError("Task_Stopped_Event does not parse")


def _send_retry_job(
    sqs: _SqsSendProtocol,
    queue_url: str,
    job: VerifyJobMessage,
    *,
    task_arn: str,
    run_id: str | None,
) -> bool:
    """Send the Retry_Job once, with no delay and no attributes (Req 9.3).

    Returns ``False`` on ``ClientError`` or ``BotoCoreError`` and logs
    ``spot_retry_send_failed`` with the error code only.
    """
    try:
        sqs.send_message(QueueUrl=queue_url, MessageBody=job.model_dump_json())
    except (ClientError, BotoCoreError) as exc:
        get_logger().error(
            "spot_retry_send_failed",
            task_arn=task_arn,
            run_id=run_id,
            aws_error_code=_aws_error_code(exc),
        )
        return False
    return True


def _mark_best_effort(
    claims: SpotRetryClaims,
    *,
    task_arn: str,
    run_id: str | None,
    status: Literal["sent", "failed_closed"],
) -> None:
    """Record the claim's outcome; never raises (design "Handler flow").

    A failure logs WARNING ``spot_retry_claim_update_failed``. The claim then
    stays ``claimed`` and a late duplicate fails closed without sending.
    """
    try:
        claims.mark(task_arn=task_arn, status=status)
    except Exception as exc:
        # Defensive: raising here would turn a delivered outcome into a retry.
        get_logger().warning(
            "spot_retry_claim_update_failed",
            task_arn=task_arn,
            run_id=run_id,
            aws_error_code=_aws_error_code(exc),
        )


def _interim_check_run_id(
    decision: FailClosed,
    *,
    env: SpotRetryEnvConfig,
    ddb: _SpotRetryDynamoDbProtocol,
) -> int | None:
    """The Interim_Check_Run id recorded on ``trikon_pr_state`` for this head.

    ``None`` without a pull request number (no read happens) and whenever
    :func:`read_interim_check_run_id` finds nothing usable; a new Check Run
    is then created.
    """
    target = decision.target
    if target.pr_number is None:
        return None
    return read_interim_check_run_id(
        ddb,
        table=env.trikon_pr_state_table,
        pr_key=f"{target.installation_id}#{target.repo_full_name}#{target.pr_number}",
        head_sha=target.head_sha,
    )


def _publish_or_raise(
    decision: FailClosed,
    *,
    env: SpotRetryEnvConfig,
    ddb: _SpotRetryDynamoDbProtocol,
    github: OrchestratorGithubClient,
    task_arn: str,
    run_id: str | None,
) -> FailClosedPublication:
    """Publish the strict Fail_Closed_Result for ``decision`` (Req 11.1-11.4, 11.7).

    The Check Run concludes ``action_required``. Any DynamoDB or GitHub
    failure logs ERROR ``spot_fail_closed_publish_failed`` and propagates
    (Req 11.6).
    """
    details_url = env.trikon_check_run_details_url_template.format(
        delivery_id=run_id if run_id else _UNKNOWN_DELIVERY_ID,
    )
    notice = spot_fail_closed_notice(decision.reason, run_id, details_url)
    try:
        return publish_fail_closed_result(
            target=decision.target,
            row_message=decision.row_message,
            notice=notice,
            verdicts_table=env.trikon_verdicts_table,
            ddb_client=ddb,
            github_client=github,
            complete_check_run_id=_interim_check_run_id(decision, env=env, ddb=ddb),
        )
    except Exception as exc:
        get_logger().error(
            "spot_fail_closed_publish_failed",
            task_arn=task_arn,
            run_id=run_id,
            reason=decision.reason,
            aws_error_code=_aws_error_code(exc),
        )
        raise


# ---------------------------------------------------------------------------
# Lambda entry point.
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, object], context: LambdaContext) -> None:
    """Retry or fail closed one stopped Sandbox_Task (design "Handler flow").

    Returns ``None`` on every handled path (ignored, duplicate, re-queued,
    published). Raises :class:`SpotEventError`, :class:`SpotRetryInFlightError`,
    a claim-store error or a publish error so Lambda retries the event and
    finally keeps it in the dead-letter queue.
    """
    del context  # Unused: the task ARN and Run_Id are the correlation keys.
    log = get_logger()
    env, claims, sqs, ddb, github, now_ms = _bootstrap()

    parsed = _parse_event(event)
    decision = decide_stopped_task(parsed)
    task_arn = parsed.detail.taskArn
    run_id = parsed.detail.runner_env(RUN_ID_ENV)

    if isinstance(decision, Ignore):
        log.info("spot_event_ignored", task_arn=task_arn, run_id=run_id, reason=decision.reason)
        return
    if isinstance(decision, Unroutable):
        log.error("spot_event_unroutable", task_arn=task_arn, run_id=run_id)
        raise SpotEventError("interrupted task has no repository, head or installation")

    try:
        existing = claims.claim(task_arn=task_arn, run_id=run_id, now_ms=now_ms())
    except (ClientError, BotoCoreError) as exc:
        log.error(
            "spot_retry_claim_failed",
            task_arn=task_arn,
            run_id=run_id,
            aws_error_code=_aws_error_code(exc),
        )
        raise
    resolution = resolve_claim(existing, now_ms=now_ms())
    if resolution == "duplicate":
        log.info("spot_retry_duplicate", task_arn=task_arn, run_id=run_id)
        return
    if resolution == "in_flight":
        log.warning("spot_retry_in_flight", task_arn=task_arn, run_id=run_id)
        raise SpotRetryInFlightError("claim held by a concurrent invocation")

    fail_closed: FailClosed
    if isinstance(decision, Requeue):
        if resolution == "proceed":
            # Only the invocation that created the claim ever sends (Req 9.3).
            if _send_retry_job(
                sqs,
                env.trikon_verify_jobs_queue_url,
                decision.job,
                task_arn=task_arn,
                run_id=run_id,
            ):
                log.info("spot_retry_requeued", task_arn=task_arn, run_id=run_id)
                _mark_best_effort(claims, task_arn=task_arn, run_id=run_id, status="sent")
                return
            fail_closed = FailClosed("retry_send_failed", decision.target, decision.row_message)
        else:
            # proceed_fail_closed: the Retry_Job may already be queued; never resend.
            fail_closed = FailClosed("retry_claim_stale", decision.target, decision.row_message)
    else:
        fail_closed = decision

    # Req 11.6: raises on failure and leaves the claim "claimed".
    _publish_or_raise(
        fail_closed,
        env=env,
        ddb=ddb,
        github=github,
        task_arn=task_arn,
        run_id=run_id,
    )
    log.info(
        "spot_fail_closed_published",
        task_arn=task_arn,
        run_id=run_id,
        reason=fail_closed.reason,
    )
    _mark_best_effort(claims, task_arn=task_arn, run_id=run_id, status="failed_closed")
