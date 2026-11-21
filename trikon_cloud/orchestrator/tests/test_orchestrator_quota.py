# Pydantic models and the powertools JSON records that flow through this file
# are typed ``Any`` upstream; under ``disallow_any_explicit = true`` each touch
# surfaces as ``explicit-any``. Same file-level silence as
# ``test_orchestrator_handler.py``.
# mypy: disable-error-code="explicit-any"
"""Quota-step examples for :mod:`trikon_cloud.orchestrator.handler` (task 24.9).

Feature: sandbox-minute-plans, Requirements 11.1, 11.3-11.8, 11.11-11.13.
Also the Retry_Job quota skip and Terminal branches (trikon-cloud-cost-cuts
Requirements 9.4, 9.5, 11.5); the full Spot dispatch examples live in
``test_spot_dispatch.py``.

Every external touchpoint is a local stub that appends to one shared
call-order list:

* ``"quota"`` — :meth:`_RecordingUsageClient.check_quota`.
* ``"ssm"`` — the stub SSM client's ``get_parameter`` (STEP 5).
* ``"run_task"`` — the patched ``handler.submit_run_task`` (STEP 7).
* ``"release"`` — :meth:`_RecordingUsageClient.report_usage` (the 0-minute
  release after a Terminal RunTask error).
* ``"fail_closed"`` — the patched ``handler.write_orchestrator_failure_verdict``.

No moto, no network: the boto3 session, GitHub client, and SQS client caches
are replaced with stubs, and the Never-Fail-Open writer is recorded instead of
run (its row and Check Run shape are covered in ``test_never_fail_open.py``).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import IO, Any, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trikon_cloud.orchestrator import handler as handler_module
from trikon_cloud.orchestrator.ecs_dispatcher import (
    RunTaskDispatchResult,
    RunTaskNoTaskError,
    TransientDispatchError,
)
from trikon_cloud.orchestrator.handler import lambda_handler
from trikon_cloud.orchestrator.models import RunTaskCall, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import FailClosedNotice
from trikon_cloud.orchestrator.quota_notices import spot_retry_dispatch_failed_notice
from trikon_cloud.usage_client.models import (
    QuotaAllowed,
    QuotaAlreadySettled,
    QuotaDecision,
    QuotaDenied,
    QuotaRequest,
    QuotaUnavailable,
    UnavailableCause,
    UsageDelivered,
    UsageRejected,
    UsageReport,
    UsageReportUndeliveredError,
)

from .conftest import (
    CANONICAL_DELIVERY_ID,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_WEB_APP_URL,
    make_sqs_job_message,
)

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_RESERVATION_ID: str = "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"
_TASK_ARN: str = (
    "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/abcdef0123456789"
)
# ``OrchestratorEnvConfig`` default for TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE.
_DETAILS_URL: str = f"https://api.trikon.unideploy.com/audits/{CANONICAL_DELIVERY_ID}"
# ``ecs_dispatcher._TRANSIENT_CODES`` / ``_TERMINAL_CODES`` members.
_TRANSIENT_CODE: str = "ThrottlingException"
_TERMINAL_CODE: str = "AccessDeniedException"
# Default TRIKON_QUOTA_TIME_BUDGET_SECONDS.
_DEFAULT_BUDGET_SECONDS: float = 10.0

_ReleaseBehaviour = UsageDelivered | UsageRejected | Exception


# ---------------------------------------------------------------------------
# Call-order recorder and stubs.
# ---------------------------------------------------------------------------


class _Recorder:
    """Shared call-order list plus the payload of each recorded call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.quota_requests: list[tuple[QuotaRequest, float]] = []
        self.reports: list[UsageReport] = []
        self.run_task_calls: list[RunTaskCall] = []
        self.fail_closed: list[dict[str, object]] = []


class _RecordingUsageClient:
    """Usage_API stub returning a fixed decision and a fixed release result."""

    def __init__(
        self,
        recorder: _Recorder,
        decision: QuotaDecision,
        release: _ReleaseBehaviour,
    ) -> None:
        self._recorder = recorder
        self._decision = decision
        self._release = release

    def check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision:
        self._recorder.calls.append("quota")
        self._recorder.quota_requests.append((request, budget_seconds))
        return self._decision

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        self._recorder.calls.append("release")
        self._recorder.reports.append(report)
        if isinstance(self._release, Exception):
            raise self._release
        return self._release


class _RecordingSsmClient:
    """SSM stub: ``get_parameter`` records ``"ssm"`` and returns revision 17."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def get_parameter(self, *, Name: str) -> object:  # noqa: N803 - boto3 kwarg
        del Name
        self._recorder.calls.append("ssm")
        return {"Parameter": {"Value": "17"}}


class _RecordingPrStateClient:
    """DynamoDB stub: ``get_item`` on ``trikon_pr_state`` returns ``item``."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.item: dict[str, dict[str, str]] | None = None
        self.keys: list[tuple[str, dict[str, dict[str, str]]]] = []

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803 - boto3 kwarg
        Key: dict[str, dict[str, str]],  # noqa: N803 - boto3 kwarg
        ConsistentRead: bool,  # noqa: N803 - boto3 kwarg
    ) -> dict[str, object]:
        assert ConsistentRead is True
        self._recorder.calls.append("pr_state")
        self.keys.append((TableName, Key))
        return {} if self.item is None else {"Item": self.item}


class _StubBotoSession:
    """boto3 session stub: ``ssm``, ``ecs`` and the ``trikon_pr_state`` read."""

    def __init__(self, recorder: _Recorder) -> None:
        self._ssm = _RecordingSsmClient(recorder)
        self.dynamodb = _RecordingPrStateClient(recorder)

    def client(self, service_name: str) -> object:
        if service_name == "ssm":
            return self._ssm
        if service_name == "ecs":
            # Never used: ``submit_run_task`` is patched.
            return object()
        if service_name == "dynamodb":
            return self.dynamodb
        msg = f"unexpected boto3 client: {service_name}"
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def boto_session(recorder: _Recorder) -> _StubBotoSession:
    return _StubBotoSession(recorder)


@pytest.fixture(autouse=True)
def _reset_caches(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, boto_session: _StubBotoSession
) -> None:
    """Cold-start env and resolver; stub every client cache."""
    monkeypatch.setattr(handler_module, "_ENV", None)
    monkeypatch.setattr(handler_module, "_RESOLVER", None)
    monkeypatch.setattr(handler_module, "_BOTO_SESSION", boto_session)
    monkeypatch.setattr(handler_module, "_GITHUB_CLIENT", object())
    monkeypatch.setattr(handler_module, "_SQS_CLIENT", object())
    monkeypatch.setattr(handler_module, "_USAGE_CLIENT", None)


@pytest.fixture(autouse=True)
def _record_fail_closed(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """Replace the Never-Fail-Open writer with a recorder."""

    def _stub(**kwargs: object) -> None:
        recorder.calls.append("fail_closed")
        recorder.fail_closed.append(kwargs)

    monkeypatch.setattr(handler_module, "write_orchestrator_failure_verdict", _stub)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the powertools logger's stream to a :class:`io.StringIO`."""
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-orchestrator")
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            original_streams.append((handler, handler.stream))
            handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in original_streams:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _install_usage_client(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    decision: QuotaDecision,
    *,
    release: _ReleaseBehaviour | None = None,
) -> None:
    client = _RecordingUsageClient(
        recorder,
        decision,
        release if release is not None else UsageDelivered(outcome="recorded"),
    )
    monkeypatch.setattr(handler_module, "_USAGE_CLIENT", client)


def _stub_run_task(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    error_code: str | None = None,
    no_task_reason: str | None = None,
) -> None:
    """Patch ``handler.submit_run_task``.

    Raises ``ClientError`` if ``error_code``, else ``RunTaskNoTaskError``
    if ``no_task_reason``.
    """

    def _stub(call: RunTaskCall, *, ecs_client: object) -> RunTaskDispatchResult:
        del ecs_client
        recorder.calls.append("run_task")
        recorder.run_task_calls.append(call)
        if no_task_reason is not None:
            raise RunTaskNoTaskError(no_task_reason)
        if error_code is not None:
            raise ClientError(
                {"Error": {"Code": error_code, "Message": "injected by test"}},
                "RunTask",
            )
        return RunTaskDispatchResult(task_arn=_TASK_ARN)

    monkeypatch.setattr(handler_module, "submit_run_task", _stub)


def _lambda_context() -> LambdaContext:
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-orchestrator-test"
    return cast(LambdaContext, ctx)


def _event(**overrides: object) -> dict[str, object]:
    body = make_sqs_job_message(**overrides).model_dump_json()
    return _event_with_body(body)


def _event_with_body(body: str, *, receive_count: int = 1) -> dict[str, object]:
    return {
        "Records": [
            {
                "messageId": "sqs-msg-1",
                "receiptHandle": "handle-1",
                "body": body,
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
            }
        ]
    }


def _retry_event(*, receive_count: int = 1) -> dict[str, object]:
    """A Retry_Job (Attempt 2) carrying :data:`_RESERVATION_ID`."""
    message = VerifyJobMessage.model_validate(
        {
            **make_sqs_job_message().model_dump(),
            "attempt": 2,
            "reservation_id": _RESERVATION_ID,
        }
    )
    return _event_with_body(message.model_dump_json(), receive_count=receive_count)


def _log_records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _by_message(records: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("message") == message]


def _only_notice(recorder: _Recorder) -> FailClosedNotice:
    """The single Fail_Closed_Result's notice, checking the evidence kwargs."""
    assert len(recorder.fail_closed) == 1
    kwargs = recorder.fail_closed[0]
    notice = kwargs["notice"]
    assert isinstance(notice, FailClosedNotice)
    assert kwargs["error_class"] == notice.error_class == "quota"
    assert kwargs["error_code"] == notice.error_code
    assert notice.conclusion == "action_required"
    return notice


# ---------------------------------------------------------------------------
# allowed (Req 11.1, 11.3).
# ---------------------------------------------------------------------------


def test_allowed_checks_quota_before_ssm_and_dispatches_once_with_both_ids(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "ssm", "run_task"]
    # The Quota_Request carries the Job's natural keys, Run_Id = delivery_id.
    request, budget = recorder.quota_requests[0]
    assert request == QuotaRequest(
        installation_id=CANONICAL_INSTALLATION_ID,
        run_id=CANONICAL_DELIVERY_ID,
        repository_full_name=CANONICAL_REPO_FULL_NAME,
        pr_number=CANONICAL_PR_NUMBER,
    )
    assert budget == _DEFAULT_BUDGET_SECONDS
    # One RunTask, carrying the Run_Id and the Reservation_Id.
    assert len(recorder.run_task_calls) == 1
    environment = {
        entry.name: entry.value
        for entry in recorder.run_task_calls[0].overrides.containerOverrides[0].environment
    }
    assert environment["TRIKON_DELIVERY_ID"] == CANONICAL_DELIVERY_ID
    assert environment["TRIKON_RESERVATION_ID"] == _RESERVATION_ID
    assert recorder.reports == []
    assert recorder.fail_closed == []

    checked = _by_message(_log_records(log_stream), "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "allowed"
    assert checked[0].get("denial_reason") is None


def test_quota_budget_follows_env(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    monkeypatch.setenv("TRIKON_QUOTA_TIME_BUDGET_SECONDS", "4.5")
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder)

    lambda_handler(_event(), _lambda_context())

    assert recorder.quota_requests[0][1] == 4.5


# ---------------------------------------------------------------------------
# already_settled (Req 11.7).
# ---------------------------------------------------------------------------


def test_already_settled_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    _install_usage_client(
        monkeypatch, recorder, QuotaAlreadySettled(reservation_id=_RESERVATION_ID)
    )
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota"]
    records = _log_records(log_stream)
    checked = _by_message(records, "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "already_settled"
    assert len(_by_message(records, "quota_already_settled")) == 1


# ---------------------------------------------------------------------------
# denied (Req 11.4-11.6, 11.11).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "matched_rule", "title", "details_url", "summary_fragment"),
    [
        (
            QuotaDenied(reason="not_connected", period_end=None),
            "installation not connected",
            "Connect your Trikon account",
            f"{CANONICAL_WEB_APP_URL}/dashboard/installations",
            "not connected to a Trikon account",
        ),
        (
            QuotaDenied(reason="no_plan", period_end=None),
            "plan unavailable",
            "Trikon plan unavailable",
            f"{CANONICAL_WEB_APP_URL}/account",
            "could not determine the plan",
        ),
        (
            QuotaDenied(
                reason="minutes_exhausted",
                period_end=datetime(2025, 7, 1, tzinfo=UTC),
            ),
            "sandbox minutes exhausted",
            "Monthly sandbox minutes used up",
            f"{CANONICAL_WEB_APP_URL}/account",
            "Minutes reset on Jul 1, 2025 (UTC).",
        ),
    ],
    ids=["not_connected", "no_plan", "minutes_exhausted"],
)
def test_denied_writes_fail_closed_result_and_no_run_task(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
    decision: QuotaDenied,
    matched_rule: str,
    title: str,
    details_url: str,
    summary_fragment: str,
) -> None:
    _install_usage_client(monkeypatch, recorder, decision)
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "fail_closed"]
    notice = _only_notice(recorder)
    assert notice.matched_rule == matched_rule
    assert notice.title == title
    assert notice.details_url == details_url
    assert notice.error_code == decision.reason
    assert summary_fragment in notice.summary
    # The handler parses a VerifyJobMessage (cost-cuts D6); receiver bytes
    # are Attempt 1 with no reservation_id.
    assert recorder.fail_closed[0]["sqs_message"] == VerifyJobMessage.model_validate(
        make_sqs_job_message().model_dump()
    )

    checked = _by_message(_log_records(log_stream), "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "denied"
    assert checked[0]["denial_reason"] == decision.reason


# ---------------------------------------------------------------------------
# unavailable (Req 11.8, 11.11).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cause", "status"),
    [("network", None), ("timeout", None), ("http_status", 503), ("malformed", 200)],
)
def test_unavailable_writes_fail_closed_result_and_no_run_task(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
    cause: UnavailableCause,
    status: int | None,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaUnavailable(cause=cause, status=status))
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "fail_closed"]
    notice = _only_notice(recorder)
    assert notice.matched_rule == "usage service unavailable"
    assert notice.title == "Trikon Cloud verification unavailable"
    assert notice.details_url == _DETAILS_URL
    assert notice.error_code == cause
    assert f"Delivery id: {CANONICAL_DELIVERY_ID}." in notice.summary

    checked = _by_message(_log_records(log_stream), "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "unavailable"
    assert checked[0].get("denial_reason") is None


def test_malformed_quota_request_fails_closed_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    # ``SqsJobMessage`` accepts any delivery_id; the Run_Id pattern rejects ".".
    delivery_id = "not.a.valid.run.id"
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_event(delivery_id=delivery_id), _lambda_context())

    assert result == {"batchItemFailures": []}
    # No Quota_Request reached the client, and nothing was dispatched.
    assert recorder.calls == ["fail_closed"]
    notice = _only_notice(recorder)
    assert notice.matched_rule == "usage service unavailable"
    assert notice.error_code == "malformed"
    assert notice.details_url == (
        f"https://api.trikon.unideploy.com/audits/{delivery_id}"
    )

    checked = _by_message(_log_records(log_stream), "quota_checked")
    assert len(checked) == 1
    assert checked[0]["quota_outcome"] == "unavailable"


# ---------------------------------------------------------------------------
# RunTask errors after allowed (Req 11.12, 11.13).
# ---------------------------------------------------------------------------


def test_transient_run_task_error_raises_and_does_not_release(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder, error_code=_TRANSIENT_CODE)

    with pytest.raises(TransientDispatchError):
        lambda_handler(_event(), _lambda_context())

    assert recorder.calls == ["quota", "ssm", "run_task"]
    assert recorder.reports == []
    assert recorder.fail_closed == []


def test_terminal_run_task_error_releases_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder, error_code=_TERMINAL_CODE)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "ssm", "run_task", "release", "fail_closed"]
    assert recorder.reports == [
        UsageReport(
            run_id=CANONICAL_DELIVERY_ID,
            reservation_id=_RESERVATION_ID,
            billed_minutes=0,
            started_at=None,
            stopped_at=None,
        )
    ]
    # The existing RunTask-terminal row and Check Run (no notice).
    kwargs = recorder.fail_closed[0]
    assert kwargs["error_class"] == "terminal"
    assert kwargs["error_code"] == _TERMINAL_CODE
    assert kwargs.get("notice") is None

    records = _log_records(log_stream)
    delivered = _by_message(records, "usage_release_delivered")
    assert len(delivered) == 1
    assert delivered[0]["usage_outcome"] == "recorded"
    assert _by_message(records, "usage_release_failed") == []
    # The terminal log precedes the release log.
    messages = [r.get("message") for r in records]
    assert messages.index("run_task_terminal_failure") < messages.index(
        "usage_release_delivered"
    )


@pytest.mark.parametrize(
    ("release", "logged_status"),
    [
        (UsageRejected(status=409), 409),
        (UsageReportUndeliveredError("not delivered"), None),
        (RuntimeError("unexpected"), None),
    ],
    ids=["rejected", "undelivered", "unexpected_error"],
)
def test_failed_release_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
    release: _ReleaseBehaviour,
    logged_status: int | None,
) -> None:
    _install_usage_client(
        monkeypatch,
        recorder,
        QuotaAllowed(reservation_id=_RESERVATION_ID),
        release=release,
    )
    _stub_run_task(monkeypatch, recorder, error_code=_TERMINAL_CODE)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "ssm", "run_task", "release", "fail_closed"]
    records = _log_records(log_stream)
    failed = _by_message(records, "usage_release_failed")
    assert len(failed) == 1
    assert failed[0].get("status") == logged_status
    assert failed[0]["level"] == "ERROR"
    assert _by_message(records, "usage_release_delivered") == []


# ---------------------------------------------------------------------------
# Retry_Jobs and no-task RunTask responses (cost-cuts Req 9.4, 9.5, 11.5).
# ---------------------------------------------------------------------------

_INTERIM_CHECK_RUN_ID: int = 777
_NO_TASK_REASON: str = "Capacity is unavailable at this time."


def _environment(call: RunTaskCall) -> dict[str, str]:
    return {
        entry.name: entry.value for entry in call.overrides.containerOverrides[0].environment
    }


def test_retry_job_skips_quota_and_dispatches_on_demand_with_its_reservation(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id="unused"))
    _stub_run_task(monkeypatch, recorder)

    result = lambda_handler(_retry_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    # No Quota call, so the installation's usage is unchanged (Req 9.5).
    assert recorder.calls == ["ssm", "run_task"]
    assert recorder.quota_requests == []
    call = recorder.run_task_calls[0]
    environment = _environment(call)
    assert environment["TRIKON_DELIVERY_ID"] == CANONICAL_DELIVERY_ID
    assert environment["TRIKON_RESERVATION_ID"] == _RESERVATION_ID
    assert environment["TRIKON_ATTEMPT"] == "2"
    assert environment["TRIKON_CAPACITY"] == "FARGATE"
    assert call.capacityProviderStrategy is not None
    assert call.capacityProviderStrategy[0].capacityProvider == "FARGATE"

    records = _log_records(log_stream)
    assert _by_message(records, "quota_checked") == []
    skipped = _by_message(records, "quota_skipped_retry")
    assert len(skipped) == 1
    assert skipped[0]["attempt"] == 2
    dispatched = _by_message(records, "run_task_dispatched")
    assert len(dispatched) == 1
    assert dispatched[0]["dispatched_task_arn"] == _TASK_ARN
    assert dispatched[0]["attempt"] == 2
    assert dispatched[0]["capacity"] == "FARGATE"


def test_attempt_1_no_task_is_terminal_with_the_existing_neutral_result(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    log_stream: io.StringIO,
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder, no_task_reason=_NO_TASK_REASON)

    result = lambda_handler(_event(), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["quota", "ssm", "run_task", "release", "fail_closed"]
    assert [r.reservation_id for r in recorder.reports] == [_RESERVATION_ID]
    kwargs = recorder.fail_closed[0]
    assert kwargs["error_class"] == "terminal"
    assert kwargs["error_code"] == "RunTaskNoTask"
    assert kwargs.get("notice") is None
    assert kwargs.get("complete_check_run_id") is None

    records = _log_records(log_stream)
    terminal = _by_message(records, "run_task_terminal_failure")
    assert len(terminal) == 1
    assert terminal[0]["aws_error_code"] == "RunTaskNoTask"
    assert terminal[0]["run_task_failure_reason"] == _NO_TASK_REASON
    assert _by_message(records, "run_task_dispatched") == []


@pytest.mark.parametrize(
    ("error_code", "no_task_reason", "receive_count", "expected_code"),
    [
        (_TERMINAL_CODE, None, 1, _TERMINAL_CODE),
        (None, _NO_TASK_REASON, 1, "RunTaskNoTask"),
        # Default TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT is 3: the last receive.
        (_TRANSIENT_CODE, None, 3, _TRANSIENT_CODE),
    ],
    ids=["terminal_client_error", "no_task", "transient_on_last_receive"],
)
def test_retry_job_terminal_releases_then_completes_the_interim_run(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    boto_session: _StubBotoSession,
    error_code: str | None,
    no_task_reason: str | None,
    receive_count: int,
    expected_code: str,
) -> None:
    boto_session.dynamodb.item = {
        "last_head_sha": {"S": make_sqs_job_message().head_sha},
        "last_check_run_id": {"N": str(_INTERIM_CHECK_RUN_ID)},
    }
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id="unused"))
    _stub_run_task(
        monkeypatch, recorder, error_code=error_code, no_task_reason=no_task_reason
    )

    result = lambda_handler(_retry_event(receive_count=receive_count), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert recorder.calls == ["ssm", "run_task", "release", "pr_state", "fail_closed"]
    assert recorder.reports == [
        UsageReport(
            run_id=CANONICAL_DELIVERY_ID,
            reservation_id=_RESERVATION_ID,
            billed_minutes=0,
            started_at=None,
            stopped_at=None,
        )
    ]
    assert boto_session.dynamodb.keys == [
        (
            "trikon_pr_state",
            {
                "pr_key": {
                    "S": (
                        f"{CANONICAL_INSTALLATION_ID}#{CANONICAL_REPO_FULL_NAME}"
                        f"#{CANONICAL_PR_NUMBER}"
                    )
                }
            },
        )
    ]
    kwargs = recorder.fail_closed[0]
    expected_notice = spot_retry_dispatch_failed_notice(
        CANONICAL_DELIVERY_ID, _DETAILS_URL, expected_code
    )
    assert kwargs["notice"] == expected_notice
    assert expected_notice.conclusion == "action_required"
    assert kwargs["error_class"] == "terminal"
    assert kwargs["error_code"] == expected_code
    assert kwargs["complete_check_run_id"] == _INTERIM_CHECK_RUN_ID


@pytest.mark.parametrize(
    "event",
    [
        _retry_event(receive_count=2),
        _event_with_body(make_sqs_job_message().model_dump_json(), receive_count=5),
    ],
    ids=["retry_below_max_receive", "attempt_1_any_receive"],
)
def test_transient_error_redrives_unless_last_retry_receive(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    event: dict[str, object],
) -> None:
    _install_usage_client(monkeypatch, recorder, QuotaAllowed(reservation_id=_RESERVATION_ID))
    _stub_run_task(monkeypatch, recorder, error_code=_TRANSIENT_CODE)

    with pytest.raises(TransientDispatchError):
        lambda_handler(event, _lambda_context())

    assert recorder.calls[-2:] == ["ssm", "run_task"]
    assert recorder.reports == []
    assert recorder.fail_closed == []
