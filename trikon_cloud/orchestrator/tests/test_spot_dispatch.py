"""Spot dispatch tests for the Orchestrator (trikon-cloud-cost-cuts Req 8, 9, 11).

Current contents:

* Property 5: Job_Message attempt parsing (task 10.2). Hypothesis builds a
  valid :class:`SqsJobMessage`, adds an arbitrary ``attempt`` JSON value
  (absent, any int, float, bool, string or null) and an arbitrary
  ``reservation_id`` value, and checks that
  :meth:`VerifyJobMessage.model_validate_json` accepts the body exactly when
  the design says it should. It also checks that the Webhook_Receiver's wire
  JSON never carries either key (Req 8.7, 8.8).
* Retry_Job dispatch examples (task 12.2). Each example runs
  :func:`~trikon_cloud.orchestrator.handler.lambda_handler` end to end with
  a stub Usage_API client, a stub ECS client (``submit_run_task`` is real, so
  the exact ``ecs:RunTask`` kwargs are asserted) and a stub SSM client (the
  active revision and the App key). The Never-Fail-Open path is real too:
  ``write_orchestrator_failure_verdict`` writes to moto DynamoDB
  (``trikon_verdicts``) and reads the Interim_Check_Run id from moto
  ``trikon_pr_state``, and GitHub is respx. They cover the RunTask request
  for every Spot_Flag x Attempt combination, Attempt 3 and ``"2"`` on the
  malformed path, a Retry_Job's Terminal ``ClientError`` (release, then
  PATCH of the interim run with ``action_required``, or the POST fallback),
  ``tasks == []`` as Terminal, a transient error on a Retry_Job's last
  receive as Terminal, and the unchanged Attempt 1 Terminal result
  (Req 8.7, 8.9, 9.4, 9.5, 11.5, 11.8).
* Property 8: Retry_Jobs skip the quota and keep their ids (task 12.3).
  Hypothesis builds any valid Retry_Job (Attempt 2, a UUID Reservation_Id
  in any case, any Run_Id), any Spot_Flag value and any receive count
  below the max, and drives ``lambda_handler`` on in-memory stubs set per
  example through the module caches (no moto, no respx): a recording
  Usage_API client, a recording ECS client that returns one task (with the
  real ``submit_run_task``), an SSM stub and recorders for the
  fail-closed writer, GitHub and SQS. It checks zero Quota calls and one
  ``RunTask`` on FARGATE that carries the job's Run_Id and Reservation_Id
  byte for byte (Req 8.5, 9.4, 9.5).

No network and no real AWS: the Property 5 tests only parse JSON, the
dispatch examples use stubs, moto and respx (unmatched requests fail),
and the Property 8 test uses stubs only.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import string
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Final, cast
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
import respx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws
from pydantic import ValidationError

from trikon_cloud.fargate_runner.spot_contract import MAX_ATTEMPTS
from trikon_cloud.orchestrator import handler as handler_module
from trikon_cloud.orchestrator import never_fail_open as never_fail_open_module
from trikon_cloud.orchestrator.handler import lambda_handler
from trikon_cloud.orchestrator.models import VerifyJobMessage
from trikon_cloud.orchestrator.quota_notices import spot_retry_dispatch_failed_notice
from trikon_cloud.usage_client.models import (
    QuotaAllowed,
    QuotaDecision,
    QuotaRequest,
    UsageDelivered,
    UsageRejected,
    UsageReport,
)
from trikon_cloud.webhook_receiver.models import SqsJobMessage

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    make_sqs_job_message,
)

# ===========================================================================
# Property 5: Job_Message attempt parsing (Req 8.7, 8.8).
# ===========================================================================


class _Absent:
    """Sentinel: the key is left out of the JSON body."""

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT: Final = _Absent()

JsonScalar = int | float | bool | str | None

_SHA = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)

_SQS_JOB_MESSAGE = st.builds(
    SqsJobMessage,
    installation_id=st.integers(min_value=1, max_value=2**53),
    repo_full_name=st.text(max_size=60),
    pr_number=st.integers(min_value=1, max_value=2**31),
    head_sha=_SHA,
    base_sha=_SHA,
    event_type=st.text(max_size=30),
    sent_at=st.text(max_size=40),
    delivery_id=st.text(max_size=40),
)

_ATTEMPT: st.SearchStrategy[JsonScalar | _Absent] = st.one_of(
    st.just(_ABSENT),
    # The boundary values and their look-alikes, so every run hits them.
    st.sampled_from([1, 2, 0, 3, -1, "1", "2", 1.0, 2.0, True, False, None]),
    st.integers(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.text(max_size=10),
    st.none(),
)


def _uuid_strings() -> st.SearchStrategy[str]:
    """UUID strings in lower, upper and mixed case (no version check)."""
    return st.one_of(
        st.uuids().map(str),
        st.uuids().map(lambda u: str(u).upper()),
        st.text(alphabet=string.hexdigits, min_size=32, max_size=32).map(
            lambda h: f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        ),
    )


# JSON ``null`` is left out on purpose: the property speaks of
# ``reservation_id`` being absent, and an explicit ``null`` is the pydantic
# default for an Optional field. See the task report.
_RESERVATION_ID: st.SearchStrategy[JsonScalar | _Absent] = st.one_of(
    st.just(_ABSENT),
    _uuid_strings(),
    # Near-miss UUID strings.
    st.uuids().map(lambda u: u.hex),
    st.uuids().map(lambda u: f"{{{u}}}"),
    st.uuids().map(lambda u: f"urn:uuid:{u}"),
    st.uuids().map(lambda u: f"{u}\n"),
    st.uuids().map(lambda u: f" {u}"),
    st.uuids().map(lambda u: str(u)[:-1] + "g"),
    st.text(max_size=40),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
)


def _is_uuid_string(value: object) -> bool:
    """An 8-4-4-4-12 ASCII hex string, either case.

    Written without the model's regex so the test does not share its bug.
    """
    if not isinstance(value, str):
        return False
    groups = value.split("-")
    return [len(g) for g in groups] == [8, 4, 4, 4, 12] and all(
        c in string.hexdigits for g in groups for c in g
    )


def _is_json_int(value: object, expected: int) -> bool:
    """``value`` is the JSON integer ``expected`` (not a bool or a float)."""
    return type(value) is int and value == expected


def _should_accept(attempt: JsonScalar | _Absent, reservation_id: JsonScalar | _Absent) -> bool:
    """The acceptance rule of design Property 5."""
    first = (attempt is _ABSENT or _is_json_int(attempt, 1)) and reservation_id is _ABSENT
    retry = _is_json_int(attempt, MAX_ATTEMPTS) and _is_uuid_string(reservation_id)
    return first or retry


def _body_with(
    message: SqsJobMessage,
    attempt: JsonScalar | _Absent,
    reservation_id: JsonScalar | _Absent,
) -> str:
    """``message``'s wire JSON with the two optional keys added."""
    payload = json.loads(message.model_dump_json())
    assert isinstance(payload, dict)
    if not isinstance(attempt, _Absent):
        payload["attempt"] = attempt
    if not isinstance(reservation_id, _Absent):
        payload["reservation_id"] = reservation_id
    return json.dumps(payload)


@given(message=_SQS_JOB_MESSAGE, attempt=_ATTEMPT, reservation_id=_RESERVATION_ID)
@settings(max_examples=500, deadline=None)
def test_property_job_message_attempt_parsing(
    message: SqsJobMessage,
    attempt: JsonScalar | _Absent,
    reservation_id: JsonScalar | _Absent,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 5: Job_Message attempt parsing.

    Parsing succeeds if and only if the attempt is absent or the int 1 with no
    ``reservation_id``, or the int 2 with a UUID-string ``reservation_id``. On
    success the receiver's fields are unchanged and an absent attempt reads
    as 1.

    **Validates: Requirements 8.7, 8.8**
    """
    body = _body_with(message, attempt, reservation_id)

    if not _should_accept(attempt, reservation_id):
        with pytest.raises(ValidationError):
            VerifyJobMessage.model_validate_json(body)
        return

    parsed = VerifyJobMessage.model_validate_json(body)
    assert parsed.model_dump(exclude={"attempt", "reservation_id"}) == message.model_dump()
    if isinstance(attempt, _Absent):
        assert parsed.attempt == 1
    else:
        assert parsed.attempt == attempt
    if isinstance(reservation_id, _Absent):
        assert parsed.reservation_id is None
    else:
        assert parsed.reservation_id == reservation_id


@given(message=_SQS_JOB_MESSAGE)
@settings(max_examples=200, deadline=None)
def test_property_receiver_wire_json_has_no_attempt_keys(message: SqsJobMessage) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 5: Job_Message attempt parsing (receiver bytes).

    The JSON the Webhook_Receiver writes never carries ``attempt`` or
    ``reservation_id``, and the Orchestrator reads it as Attempt 1.

    **Validates: Requirements 8.7, 8.8**
    """
    wire = message.model_dump_json()
    payload = json.loads(wire)
    assert isinstance(payload, dict)
    assert "attempt" not in payload
    assert "reservation_id" not in payload

    parsed = VerifyJobMessage.model_validate_json(wire)
    assert parsed.attempt == 1
    assert parsed.reservation_id is None


# ===========================================================================
# Retry_Job dispatch examples (task 12.2; Req 8.7, 8.9, 9.4, 9.5, 11.5, 11.8).
#
# Unlike the recorder-based examples in ``test_orchestrator_quota.py``,
# nothing in the handler is patched here: ``submit_run_task`` serializes the
# real request into the stub ECS client, and the real
# ``write_orchestrator_failure_verdict`` writes to moto DynamoDB and calls
# GitHub through respx.
# ===========================================================================

_REGION: Final = "us-east-1"
_ACTIVE_REVISION_PARAMETER: Final = "/trikon/verify-runner/active-revision"
_TASK_ARN: Final = (
    "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/abcdef0123456789"
)
# The Quota_Endpoint's Reservation_Id (Attempt 1) and the Retry_Job's own
# (Attempt 2) differ, so each test shows which one reached RunTask.
_QUOTA_RESERVATION_ID: Final = "11111111-2222-4333-8444-555555555555"
_RETRY_RESERVATION_ID: Final = "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"
_INTERIM_CHECK_RUN_ID: Final = 777
_OTHER_HEAD_SHA: Final = "0" * 40
_INSTALLATION_TOKEN: Final = "ghs_test_installation_token"
# ``OrchestratorEnvConfig`` default for TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE.
_DETAILS_URL: Final = f"https://api.trikon.unideploy.com/audits/{CANONICAL_DELIVERY_ID}"
# ``ecs_dispatcher._TERMINAL_CODES`` / ``_TRANSIENT_CODES`` members.
_TERMINAL_CODE: Final = "AccessDeniedException"
_TRANSIENT_CODE: Final = "ThrottlingException"
_NO_TASK_REASON: Final = "Capacity is unavailable at this time."
_NO_TASK_RESPONSE: Final[dict[str, object]] = {
    "tasks": [],
    "failures": [
        {
            "arn": "arn:aws:ecs:us-east-1:000000000000:container-instance/x",
            "reason": _NO_TASK_REASON,
        }
    ],
}
_SPOT_TITLE: Final = "Trikon verification interrupted"
_SPOT_MATCHED_RULE: Final = "spot retry dispatch failed"
_PR_KEY: Final = f"{CANONICAL_INSTALLATION_ID}#{CANONICAL_REPO_FULL_NAME}#{CANONICAL_PR_NUMBER}"


class _StubUsageClient:
    """Usage_API stub: every Quota_Request is ``allowed``; releases are delivered."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.quota_requests: list[QuotaRequest] = []
        self.reports: list[UsageReport] = []

    def check_quota(self, request: QuotaRequest, budget_seconds: float) -> QuotaDecision:
        del budget_seconds
        self._calls.append("quota")
        self.quota_requests.append(request)
        return QuotaAllowed(reservation_id=_QUOTA_RESERVATION_ID)

    def report_usage(self, report: UsageReport) -> UsageDelivered | UsageRejected:
        self._calls.append("release")
        self.reports.append(report)
        return UsageDelivered(outcome="recorded")


class _StubEcsClient:
    """ECS stub: records the ``run_task`` kwargs, then answers or raises."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.requests: list[dict[str, object]] = []
        self.response: dict[str, object] = {"tasks": [{"taskArn": _TASK_ARN}], "failures": []}
        self.error_code: str | None = None

    def run_task(self, **kwargs: object) -> object:
        self._calls.append("run_task")
        self.requests.append(dict(kwargs))
        if self.error_code is not None:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "injected by test"}},
                "RunTask",
            )
        return self.response


class _StubSsmClient:
    """SSM stub serving the active revision (``17``) and the App key PEM."""

    def __init__(self, app_key_pem: str) -> None:
        self._values = {
            _ACTIVE_REVISION_PARAMETER: "17",
            CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME: app_key_pem,
        }
        self.reads: list[tuple[str, bool]] = []

    def get_parameter(
        self,
        *,
        Name: str,  # noqa: N803 - boto3 kwarg
        WithDecryption: bool = False,  # noqa: N803 - boto3 kwarg
    ) -> dict[str, object]:
        self.reads.append((Name, WithDecryption))
        return {"Parameter": {"Value": self._values[Name]}}


class _SpotSession:
    """boto3 session stand-in: stub SSM and ECS, moto DynamoDB."""

    def __init__(self, ssm: _StubSsmClient, ecs: _StubEcsClient) -> None:
        self._ssm = ssm
        self._ecs = ecs

    def client(self, service_name: str) -> object:
        if service_name == "ssm":
            return self._ssm
        if service_name == "ecs":
            return self._ecs
        if service_name == "dynamodb":
            return boto3.client("dynamodb", region_name=_REGION)
        msg = f"unexpected boto3 client: {service_name}"
        raise AssertionError(msg)


@dataclass(frozen=True)
class _Harness:
    calls: list[str]
    usage: _StubUsageClient
    ecs: _StubEcsClient
    ssm: _StubSsmClient


@pytest.fixture
def _httpx_respx_method_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28's bytes-vs-str method mismatch.

    Same test-only shim as ``test_orchestrator_handler.py``: httpcore hands
    respx a bytes method, which respx's ``Method`` matcher never matches.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: str | bytes, *args: object, **kwargs: object) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Request, "__init__", _init)


def _create_tables() -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    client.create_table(
        TableName="trikon_verdicts",
        KeySchema=[
            {"AttributeName": "installation_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "installation_id", "AttributeType": "N"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName="trikon_pr_state",
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch,
    rsa_test_private_key_pem: bytes,
    _httpx_respx_method_compat: None,
) -> Iterator[_Harness]:
    """Cold-start the handler on stub Usage/ECS/SSM clients and moto DynamoDB.

    ``_GITHUB_CLIENT`` stays ``None`` so ``_bootstrap`` builds the real
    :class:`OrchestratorGithubClient` from the stub SSM client and a real
    :class:`httpx.Client`, which respx intercepts.
    """
    for key, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", _REGION),
    ):
        monkeypatch.setenv(key, value)
    never_fail_open_module._TOKEN_CACHE.clear()
    monkeypatch.setattr(never_fail_open_module, "_APP_JWT_CACHE", None)
    with mock_aws():
        _create_tables()
        calls: list[str] = []
        state = _Harness(
            calls=calls,
            usage=_StubUsageClient(calls),
            ecs=_StubEcsClient(calls),
            ssm=_StubSsmClient(rsa_test_private_key_pem.decode("utf-8")),
        )
        monkeypatch.setattr(handler_module, "_ENV", None)
        monkeypatch.setattr(handler_module, "_RESOLVER", None)
        monkeypatch.setattr(handler_module, "_BOTO_SESSION", _SpotSession(state.ssm, state.ecs))
        monkeypatch.setattr(handler_module, "_GITHUB_CLIENT", None)
        monkeypatch.setattr(handler_module, "_USAGE_CLIENT", state.usage)
        monkeypatch.setattr(handler_module, "_SQS_CLIENT", object())
        yield state
    never_fail_open_module._TOKEN_CACHE.clear()


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


def _lambda_context() -> LambdaContext:
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-orchestrator-test"
    return cast(LambdaContext, ctx)


def _event(body: str, *, receive_count: int = 1) -> dict[str, object]:
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


def _attempt_1_body() -> str:
    """The Webhook_Receiver's bytes: no ``attempt`` key, so Attempt 1 (Req 8.8)."""
    return make_sqs_job_message().model_dump_json()


def _retry_body() -> str:
    """A Retry_Job: Attempt 2 carrying :data:`_RETRY_RESERVATION_ID`."""
    return VerifyJobMessage.model_validate(
        {
            **make_sqs_job_message().model_dump(),
            "attempt": 2,
            "reservation_id": _RETRY_RESERVATION_ID,
        }
    ).model_dump_json()


def _receiver_body_plus(**extra: object) -> str:
    payload = json.loads(_attempt_1_body())
    assert isinstance(payload, dict)
    payload.update(extra)
    return json.dumps(payload)


def _expected_run_task_kwargs(
    *, capacity: str, attempt: int, reservation_id: str
) -> dict[str, object]:
    """The exact boto3 ``run_task`` kwargs (design D5, Req 8.3-8.6)."""
    environment = [
        ("TRIKON_INSTALLATION_ID", str(CANONICAL_INSTALLATION_ID)),
        ("TRIKON_REPO_FULL_NAME", CANONICAL_REPO_FULL_NAME),
        ("TRIKON_PR_NUMBER", str(CANONICAL_PR_NUMBER)),
        ("TRIKON_HEAD_SHA", CANONICAL_HEAD_SHA),
        ("TRIKON_BASE_SHA", CANONICAL_BASE_SHA),
        ("TRIKON_EVENT_TYPE", CANONICAL_EVENT_TYPE),
        ("TRIKON_DELIVERY_ID", CANONICAL_DELIVERY_ID),
        ("TRIKON_RESERVATION_ID", reservation_id),
        ("TRIKON_ATTEMPT", str(attempt)),
        ("TRIKON_CAPACITY", capacity),
    ]
    return {
        "cluster": "trikon-verify-cluster",
        "taskDefinition": "trikon-verify-runner:17",
        "capacityProviderStrategy": [{"capacityProvider": capacity, "weight": 1}],
        "count": 1,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": ["sg-verify"],
                "assignPublicIp": "ENABLED",
            }
        },
        "overrides": {
            "taskRoleArn": (
                f"arn:aws:iam::{CANONICAL_ACCOUNT_ID}"
                f":role/trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
            ),
            "containerOverrides": [
                {
                    "name": "runner",
                    "environment": [{"name": n, "value": v} for n, v in environment],
                }
            ],
        },
        "tags": [
            {"key": "installation_id", "value": str(CANONICAL_INSTALLATION_ID)},
            {"key": "repo", "value": CANONICAL_REPO_FULL_NAME},
            {"key": "pr", "value": str(CANONICAL_PR_NUMBER)},
        ],
    }


def _put_pr_state(*, head_sha: str) -> None:
    """Record the Interim_Check_Run the Runner left for this PR on ``head_sha``."""
    boto3.client("dynamodb", region_name=_REGION).put_item(
        TableName="trikon_pr_state",
        Item={
            "pr_key": {"S": _PR_KEY},
            "last_head_sha": {"S": head_sha},
            "last_check_run_id": {"N": str(_INTERIM_CHECK_RUN_ID)},
            "last_comment_id": {"NULL": True},
        },
    )


@dataclass(frozen=True)
class _Verdict:
    decision: str
    matched_rule: str
    evidence: dict[str, object]


def _verdicts() -> list[_Verdict]:
    """Every ``trikon_verdicts`` row, with its gzip evidence decoded."""
    items = boto3.client("dynamodb", region_name=_REGION).scan(
        TableName="trikon_verdicts"
    )["Items"]
    verdicts: list[_Verdict] = []
    for item in items:
        evidence = json.loads(gzip.decompress(item["evidence_blob"]["B"]))
        assert isinstance(evidence, dict)
        verdicts.append(
            _Verdict(
                decision=str(item["decision"]["S"]),
                matched_rule=str(item["matched_rule"]["S"]),
                evidence=evidence,
            )
        )
    return verdicts


def _recording(
    calls: list[str], label: str, status: int, payload: dict[str, object]
) -> Callable[[httpx.Request], httpx.Response]:
    def _side_effect(request: httpx.Request) -> httpx.Response:
        del request
        calls.append(label)
        return httpx.Response(status, json=payload)

    return _side_effect


def _token_route(router: respx.MockRouter, calls: list[str]) -> respx.Route:
    return router.post(f"/app/installations/{CANONICAL_INSTALLATION_ID}/access_tokens").mock(
        side_effect=_recording(
            calls,
            "github_token",
            201,
            {"token": _INSTALLATION_TOKEN, "expires_at": "2099-01-01T00:00:00Z"},
        )
    )


def _patch_route(router: respx.MockRouter, calls: list[str], *, status: int) -> respx.Route:
    return router.patch(
        f"/repos/{CANONICAL_REPO_FULL_NAME}/check-runs/{_INTERIM_CHECK_RUN_ID}"
    ).mock(side_effect=_recording(calls, "github_patch", status, {"id": _INTERIM_CHECK_RUN_ID}))


def _post_route(router: respx.MockRouter, calls: list[str]) -> respx.Route:
    return router.post(f"/repos/{CANONICAL_REPO_FULL_NAME}/check-runs").mock(
        side_effect=_recording(calls, "github_post", 201, {"id": 4242})
    )


def _json_body(route: respx.Route) -> dict[str, object]:
    body = json.loads(route.calls.last.request.content)
    assert isinstance(body, dict)
    return body


def _github_router() -> respx.MockRouter:
    """A respx router on api.github.com where an unmatched request fails."""
    return respx.mock(base_url="https://api.github.com", assert_all_mocked=True)


def _release(reservation_id: str) -> UsageReport:
    """The 0-minute Usage_Report that releases ``reservation_id``."""
    return UsageReport(
        run_id=CANONICAL_DELIVERY_ID,
        reservation_id=reservation_id,
        billed_minutes=0,
        started_at=None,
        stopped_at=None,
    )


def _spot_check_run_output(aws_error_code: str) -> dict[str, object]:
    notice = spot_retry_dispatch_failed_notice(CANONICAL_DELIVERY_ID, _DETAILS_URL, aws_error_code)
    assert notice.title == _SPOT_TITLE
    assert notice.conclusion == "action_required"
    return {"title": notice.title, "summary": notice.summary, "text": None}


def _log_records(log_stream: io.StringIO) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in log_stream.getvalue().splitlines():
        if line.strip():
            record = json.loads(line)
            assert isinstance(record, dict)
            records.append(record)
    return records


def _assert_retry_fail_closed_row(aws_error_code: str) -> None:
    assert _verdicts() == [
        _Verdict(
            decision="require_human",
            matched_rule=_SPOT_MATCHED_RULE,
            evidence={
                "error_class": "terminal",
                "error_code": aws_error_code,
                "delivery_id": CANONICAL_DELIVERY_ID,
            },
        )
    ]


# ---------------------------------------------------------------------------
# RunTask request for each Spot_Flag x Attempt (Req 8.3-8.6, 8.9, 9.4, 9.5).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spot_flag", "attempt", "capacity"),
    [
        ("true", 1, "FARGATE_SPOT"),
        ("true", 2, "FARGATE"),
        ("false", 1, "FARGATE"),
        ("false", 2, "FARGATE"),
    ],
    ids=["spot_on-attempt_1", "spot_on-attempt_2", "spot_off-attempt_1", "spot_off-attempt_2"],
)
def test_run_task_request_for_each_spot_flag_and_attempt(
    monkeypatch: pytest.MonkeyPatch,
    harness: _Harness,
    spot_flag: str,
    attempt: int,
    capacity: str,
) -> None:
    monkeypatch.setenv("TRIKON_USE_FARGATE_SPOT", spot_flag)
    body = _attempt_1_body() if attempt == 1 else _retry_body()
    reservation_id = _QUOTA_RESERVATION_ID if attempt == 1 else _RETRY_RESERVATION_ID

    with _github_router() as router:
        result = lambda_handler(_event(body), _lambda_context())

    assert result == {"batchItemFailures": []}
    # One RunTask with the exact request: a one-entry strategy, no launchType.
    assert harness.ecs.requests == [
        _expected_run_task_kwargs(
            capacity=capacity, attempt=attempt, reservation_id=reservation_id
        )
    ]
    assert "launchType" not in harness.ecs.requests[0]
    # Attempt 1 asks the Quota_Endpoint; a Retry_Job does not (Req 9.5).
    assert harness.calls == (["quota", "run_task"] if attempt == 1 else ["run_task"])
    assert harness.ssm.reads == [(_ACTIVE_REVISION_PARAMETER, False)]
    assert harness.usage.reports == []
    assert _verdicts() == []
    assert router.calls.call_count == 0


# ---------------------------------------------------------------------------
# Invalid Attempt values take the malformed-message path (Req 8.7).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [3, "2"],
    ids=["attempt_3", "attempt_string_2"],
)
def test_invalid_attempt_takes_the_malformed_message_path(
    harness: _Harness,
    log_stream: io.StringIO,
    attempt: object,
) -> None:
    body = _receiver_body_plus(attempt=attempt, reservation_id=_RETRY_RESERVATION_ID)

    with _github_router() as router:
        result = lambda_handler(_event(body), _lambda_context())

    assert result == {"batchItemFailures": []}
    # Nothing ran: no Quota call, no SSM read, no RunTask, no Check Run, no row.
    assert harness.calls == []
    assert harness.ssm.reads == []
    assert _verdicts() == []
    assert router.calls.call_count == 0
    rejected = [
        r for r in _log_records(log_stream) if r.get("message") == "orchestrator_payload_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "malformed_sqs_body"
    errors = rejected[0]["errors"]
    assert isinstance(errors, list)
    assert [e["loc"] for e in errors] == [["attempt"]]
    # The raw body never reaches the log (Invariant 6).
    assert CANONICAL_HEAD_SHA not in log_stream.getvalue()


# ---------------------------------------------------------------------------
# Retry_Job Terminal results (Req 11.5, 11.7, 11.8).
# ---------------------------------------------------------------------------


def test_retry_job_terminal_client_error_releases_then_completes_the_interim_run(
    harness: _Harness,
) -> None:
    _put_pr_state(head_sha=CANONICAL_HEAD_SHA)
    harness.ecs.error_code = _TERMINAL_CODE

    with _github_router() as router:
        _token_route(router, harness.calls)
        patch = _patch_route(router, harness.calls, status=200)
        result = lambda_handler(_event(_retry_body()), _lambda_context())

    assert result == {"batchItemFailures": []}
    # Release first, then the PATCH; no Quota call and no new Check Run.
    assert harness.calls == ["run_task", "release", "github_token", "github_patch"]
    assert harness.usage.quota_requests == []
    assert harness.usage.reports == [_release(_RETRY_RESERVATION_ID)]
    assert patch.call_count == 1
    assert patch.calls.last.request.headers["Authorization"] == f"Bearer {_INSTALLATION_TOKEN}"
    assert _json_body(patch) == {
        "status": "completed",
        "conclusion": "action_required",
        "output": _spot_check_run_output(_TERMINAL_CODE),
        "details_url": _DETAILS_URL,
    }
    _assert_retry_fail_closed_row(_TERMINAL_CODE)
    # The App key was read with decryption to mint the installation token.
    assert (CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME, True) in harness.ssm.reads


@pytest.mark.parametrize(
    ("interim_head_sha", "patch_status", "expected_github"),
    [
        (CANONICAL_HEAD_SHA, 404, ["github_token", "github_patch", "github_post"]),
        (_OTHER_HEAD_SHA, None, ["github_token", "github_post"]),
        (None, None, ["github_token", "github_post"]),
    ],
    ids=["patch_rejected", "interim_on_other_head", "no_pr_state_row"],
)
def test_retry_job_terminal_posts_a_new_check_run_when_no_interim_can_be_completed(
    harness: _Harness,
    interim_head_sha: str | None,
    patch_status: int | None,
    expected_github: list[str],
) -> None:
    if interim_head_sha is not None:
        _put_pr_state(head_sha=interim_head_sha)
    harness.ecs.error_code = _TERMINAL_CODE

    with _github_router() as router:
        _token_route(router, harness.calls)
        if patch_status is not None:
            _patch_route(router, harness.calls, status=patch_status)
        post = _post_route(router, harness.calls)
        result = lambda_handler(_event(_retry_body()), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert harness.calls == ["run_task", "release", *expected_github]
    assert harness.usage.reports == [_release(_RETRY_RESERVATION_ID)]
    assert _json_body(post) == {
        "name": "Trikon",
        "head_sha": CANONICAL_HEAD_SHA,
        "status": "completed",
        "conclusion": "action_required",
        "output": _spot_check_run_output(_TERMINAL_CODE),
        "details_url": _DETAILS_URL,
    }
    _assert_retry_fail_closed_row(_TERMINAL_CODE)


def test_retry_job_run_task_with_no_task_is_terminal(
    harness: _Harness,
    log_stream: io.StringIO,
) -> None:
    _put_pr_state(head_sha=CANONICAL_HEAD_SHA)
    harness.ecs.response = _NO_TASK_RESPONSE

    with _github_router() as router:
        _token_route(router, harness.calls)
        patch = _patch_route(router, harness.calls, status=200)
        result = lambda_handler(_event(_retry_body()), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert harness.calls == ["run_task", "release", "github_token", "github_patch"]
    assert harness.usage.reports == [_release(_RETRY_RESERVATION_ID)]
    assert _json_body(patch)["conclusion"] == "action_required"
    assert _json_body(patch)["output"] == _spot_check_run_output("RunTaskNoTask")
    _assert_retry_fail_closed_row("RunTaskNoTask")
    terminal = [
        r for r in _log_records(log_stream) if r.get("message") == "run_task_terminal_failure"
    ]
    assert len(terminal) == 1
    assert terminal[0]["aws_error_code"] == "RunTaskNoTask"
    assert terminal[0]["run_task_failure_reason"] == _NO_TASK_REASON


def test_transient_error_on_a_retry_jobs_last_receive_is_terminal(
    harness: _Harness,
    log_stream: io.StringIO,
) -> None:
    _put_pr_state(head_sha=CANONICAL_HEAD_SHA)
    harness.ecs.error_code = _TRANSIENT_CODE

    with _github_router() as router:
        _token_route(router, harness.calls)
        patch = _patch_route(router, harness.calls, status=200)
        # Default TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT is 3: the last receive.
        result = lambda_handler(_event(_retry_body(), receive_count=3), _lambda_context())

    assert result == {"batchItemFailures": []}
    assert harness.calls == ["run_task", "release", "github_token", "github_patch"]
    assert harness.usage.reports == [_release(_RETRY_RESERVATION_ID)]
    assert _json_body(patch)["conclusion"] == "action_required"
    assert _json_body(patch)["output"] == _spot_check_run_output(_TRANSIENT_CODE)
    _assert_retry_fail_closed_row(_TRANSIENT_CODE)
    terminal = [
        r for r in _log_records(log_stream) if r.get("message") == "run_task_terminal_failure"
    ]
    assert len(terminal) == 1
    assert terminal[0]["aws_error_code"] == _TRANSIENT_CODE
    assert terminal[0]["retry_receive_exhausted"] is True


# ---------------------------------------------------------------------------
# Attempt 1 Terminal results are unchanged (Req 11.5 scope; task 12.1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_code", "response", "expected_code"),
    [
        (_TERMINAL_CODE, None, _TERMINAL_CODE),
        (None, _NO_TASK_RESPONSE, "RunTaskNoTask"),
    ],
    ids=["terminal_client_error", "no_task"],
)
def test_attempt_1_terminal_result_is_unchanged(
    harness: _Harness,
    error_code: str | None,
    response: dict[str, object] | None,
    expected_code: str,
) -> None:
    # An Interim_Check_Run on this head must not be touched by Attempt 1.
    _put_pr_state(head_sha=CANONICAL_HEAD_SHA)
    harness.ecs.error_code = error_code
    if response is not None:
        harness.ecs.response = response

    with _github_router() as router:
        _token_route(router, harness.calls)
        post = _post_route(router, harness.calls)
        result = lambda_handler(_event(_attempt_1_body()), _lambda_context())

    assert result == {"batchItemFailures": []}
    # Quota, RunTask (on Spot: the flag defaults on), release of the
    # Quota_Endpoint's Reservation, then one new neutral Check Run; no PATCH.
    assert harness.calls == ["quota", "run_task", "release", "github_token", "github_post"]
    assert harness.ecs.requests[0]["capacityProviderStrategy"] == [
        {"capacityProvider": "FARGATE_SPOT", "weight": 1}
    ]
    assert harness.usage.reports == [_release(_QUOTA_RESERVATION_ID)]
    assert _json_body(post) == {
        "name": "Trikon",
        "head_sha": CANONICAL_HEAD_SHA,
        "status": "completed",
        "conclusion": "neutral",
        "output": {
            "title": "Trikon Cloud verification unavailable",
            "summary": (
                "The Trikon Cloud verification runner could not be dispatched for this "
                f"pull request. A human reviewer will follow up. Delivery id: "
                f"{CANONICAL_DELIVERY_ID}."
            ),
            "text": None,
        },
        "details_url": _DETAILS_URL,
    }
    assert _verdicts() == [
        _Verdict(
            decision="require_human",
            matched_rule="orchestrator terminal failure",
            evidence={
                "error_class": "terminal",
                "error_code": expected_code,
                "delivery_id": CANONICAL_DELIVERY_ID,
            },
        )
    ]


# ===========================================================================
# Property 8: Retry_Jobs skip the quota and keep their ids (Req 8.5, 9.4, 9.5).
#
# Every example cold-starts the handler on in-memory stubs assigned to the
# module caches inside its own ``pytest.MonkeyPatch.context()``: no moto and
# no respx, so 100 examples stay fast. ``submit_run_task`` is real, so the
# stub ECS client sees the exact boto3 ``run_task`` kwargs.
# ===========================================================================

# Any Run_Id: the Usage_API pattern, a UUID, or any text ``SqsJobMessage``
# accepts (``st.text`` draws no lone surrogates, so the body is valid JSON).
_RUN_ID: st.SearchStrategy[str] = st.one_of(
    st.from_regex(r"\A[A-Za-z0-9_-]{1,128}\Z"),
    st.uuids().map(str),
    st.text(max_size=128),
)


@st.composite
def _retry_jobs(draw: st.DrawFn) -> VerifyJobMessage:
    """Any valid Retry_Job: Attempt 2 with a UUID Reservation_Id in any case."""
    message = draw(_SQS_JOB_MESSAGE)
    return VerifyJobMessage.model_validate(
        {
            **message.model_dump(),
            "delivery_id": draw(_RUN_ID),
            "attempt": MAX_ATTEMPTS,
            "reservation_id": draw(_uuid_strings()),
        }
    )


@st.composite
def _receive_counts(draw: st.DrawFn) -> tuple[int, int]:
    """``(max_receive_count, receive_count)`` with the count below the max."""
    max_receive_count = draw(st.integers(min_value=2, max_value=1000))
    receive_count = draw(st.integers(min_value=1, max_value=max_receive_count - 1))
    return max_receive_count, receive_count


class _UntouchedClient:
    """Records and refuses any use: the success path must not touch it."""

    def __init__(self, name: str, touched: list[str]) -> None:
        self._name = name
        self._touched = touched

    def __getattr__(self, attribute: str) -> object:
        self._touched.append(f"{self._name}.{attribute}")
        msg = f"unexpected use of the {self._name} client: {attribute}"
        raise AssertionError(msg)


class _PropertySession:
    """boto3 session stand-in serving only the stub SSM and ECS clients."""

    def __init__(self, ssm: _StubSsmClient, ecs: _StubEcsClient) -> None:
        self._ssm = ssm
        self._ecs = ecs
        self.services: list[str] = []

    def client(self, service_name: str) -> object:
        self.services.append(service_name)
        if service_name == "ssm":
            return self._ssm
        if service_name == "ecs":
            return self._ecs
        msg = f"unexpected boto3 client: {service_name}"
        raise AssertionError(msg)


def _runner_environment(request: dict[str, object]) -> dict[str, str]:
    """The ``runner`` container's environment overrides as a name-to-value dict."""
    overrides = request["overrides"]
    assert isinstance(overrides, dict)
    containers = overrides["containerOverrides"]
    assert isinstance(containers, list)
    assert len(containers) == 1
    container = containers[0]
    assert isinstance(container, dict)
    assert container["name"] == "runner"
    entries = container["environment"]
    assert isinstance(entries, list)
    environment: dict[str, str] = {}
    for entry in entries:
        assert isinstance(entry, dict)
        name, value = entry["name"], entry["value"]
        assert isinstance(name, str)
        assert isinstance(value, str)
        assert name not in environment
        environment[name] = value
    return environment


@given(job=_retry_jobs(), spot_flag=st.booleans(), receive=_receive_counts())
@settings(max_examples=100, deadline=None)
def test_property_retry_jobs_skip_the_quota_and_keep_their_ids(
    job: VerifyJobMessage,
    spot_flag: bool,
    receive: tuple[int, int],
) -> None:
    # Feature: trikon-cloud-cost-cuts, Property 8: Retry_Jobs skip the quota and keep their ids
    """Feature: trikon-cloud-cost-cuts, Property 8: Retry_Jobs skip the quota and keep their ids.

    For any valid Retry_Job, Spot_Flag value and receive count below the
    max, the handler makes zero Quota calls and exactly one ``RunTask`` on
    FARGATE (no ``launchType``) whose ``TRIKON_DELIVERY_ID`` and
    ``TRIKON_RESERVATION_ID`` are the job's, byte for byte, with
    ``TRIKON_ATTEMPT == "2"``. Nothing is released, nothing fails closed,
    and GitHub and SQS are untouched.

    **Validates: Requirements 8.5, 9.4, 9.5**
    """
    max_receive_count, receive_count = receive
    assert job.reservation_id is not None
    calls: list[str] = []
    touched: list[str] = []
    fail_closed: list[dict[str, object]] = []
    usage = _StubUsageClient(calls)
    ecs = _StubEcsClient(calls)
    session = _PropertySession(_StubSsmClient("unused-app-key"), ecs)

    def _record_fail_closed(**kwargs: object) -> None:
        calls.append("fail_closed")
        fail_closed.append(dict(kwargs))

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TRIKON_USE_FARGATE_SPOT", "true" if spot_flag else "false")
        mp.setenv("TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT", str(max_receive_count))
        # Cold start: ``_bootstrap`` re-reads the env and builds a resolver.
        mp.setattr(handler_module, "_ENV", None)
        mp.setattr(handler_module, "_RESOLVER", None)
        mp.setattr(handler_module, "_BOTO_SESSION", session)
        mp.setattr(handler_module, "_GITHUB_CLIENT", _UntouchedClient("github", touched))
        mp.setattr(handler_module, "_USAGE_CLIENT", usage)
        mp.setattr(handler_module, "_SQS_CLIENT", _UntouchedClient("sqs", touched))
        mp.setattr(handler_module, "write_orchestrator_failure_verdict", _record_fail_closed)
        result = lambda_handler(
            _event(job.model_dump_json(), receive_count=receive_count),
            _lambda_context(),
        )

    assert result == {"batchItemFailures": []}
    # No Quota call, no release, no fail-closed result: only the RunTask.
    assert calls == ["run_task"]
    assert usage.quota_requests == []
    assert usage.reports == []
    assert fail_closed == []
    assert touched == []
    assert session.services == ["ssm", "ecs"]

    assert len(ecs.requests) == 1
    request = ecs.requests[0]
    assert "launchType" not in request
    assert request["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE", "weight": 1}]
    environment = _runner_environment(request)
    assert environment["TRIKON_DELIVERY_ID"].encode("utf-8") == job.delivery_id.encode("utf-8")
    assert environment["TRIKON_RESERVATION_ID"].encode("utf-8") == job.reservation_id.encode(
        "utf-8"
    )
    assert environment["TRIKON_ATTEMPT"] == "2"
    assert environment["TRIKON_CAPACITY"] == "FARGATE"
