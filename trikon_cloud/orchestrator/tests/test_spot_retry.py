"""Moto-backed examples for the Spot_Retry_Handler Lambda.

Feature: trikon-cloud-cost-cuts, Requirements 9.3, 9.6, 9.9, 11.1-11.4, 11.6, 11.8.

``_bootstrap`` builds its real boto3 clients inside :func:`moto.mock_aws`, so
the claim store (``trikon-cloud-spot-retries``), the verdict write
(``trikon_verdicts``), the Interim_Check_Run lookup (``trikon_pr_state``),
the App-key read (SSM SecureString) and the Retry_Job send
(``trikon-verify-jobs``) all run against moto. GitHub is respx with
``assert_all_mocked=True``, so any request without a route fails the test.
The in-memory unit tests for the same handler live in
``test_spot_retry_handler.py``; this file shares no private helper with it.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
import respx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.orchestrator import never_fail_open as never_fail_open_module
from trikon_cloud.orchestrator import spot_retry_handler
from trikon_cloud.orchestrator.models import VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import OrchestratorGithubClientError
from trikon_cloud.orchestrator.spot_retry import SpotEventError
from trikon_cloud.orchestrator.spot_retry_handler import lambda_handler

from .conftest import (
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
)

_REGION: Final = "us-east-1"
_CLAIMS_TABLE: Final = "trikon-cloud-spot-retries"
_VERDICTS_TABLE: Final = "trikon_verdicts"
_PR_STATE_TABLE: Final = "trikon_pr_state"
_QUEUE_NAME: Final = "trikon-verify-jobs"
_TASK_ARN: Final = "arn:aws:ecs:us-east-1:123456789012:task/trikon-verify-cluster/5eed0001"
_CLUSTER_ARN: Final = "arn:aws:ecs:us-east-1:123456789012:cluster/trikon-verify-cluster"
_RESERVATION_ID: Final = "0b7c9d2e-4f1a-4c3b-8e5d-6a7b8c9d0e1f"
_NOW_MS: Final = 1_714_557_800_000
_INTERIM_CHECK_RUN_ID: Final = 31337
_OTHER_HEAD_SHA: Final = "0123456789abcdef0123456789abcdef01234567"
_PR_KEY: Final = f"{CANONICAL_INSTALLATION_ID}#{CANONICAL_REPO_FULL_NAME}#{CANONICAL_PR_NUMBER}"
_SPOT_MATCHED_RULE: Final = "spot interruption not retried"
_TOKEN_PATH: Final = f"/app/installations/{CANONICAL_INSTALLATION_ID}/access_tokens"
_POST_PATH: Final = f"/repos/{CANONICAL_REPO_FULL_NAME}/check-runs"
_PATCH_PATH: Final = f"{_POST_PATH}/{_INTERIM_CHECK_RUN_ID}"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Aws:
    """The moto Verify_Jobs_Queue URL; assertions build their own moto clients."""

    queue_url: str


@pytest.fixture
def _respx_bytes_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28: httpcore hands respx a bytes method.

    respx's ``Method`` matcher never matches ``b"POST"``, so decode it to
    ``str`` before :class:`httpx.Request` stores it. Test-only.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: str | bytes, *args: object, **kwargs: object) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Request, "__init__", _init)


def _create_resources(private_key_pem: str) -> str:
    """Create the three tables, the queue and the App-key parameter; return the queue URL."""
    ddb = boto3.client("dynamodb", region_name=_REGION)
    ddb.create_table(
        TableName=_CLAIMS_TABLE,
        KeySchema=[{"AttributeName": "task_arn", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "task_arn", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=_VERDICTS_TABLE,
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
    ddb.create_table(
        TableName=_PR_STATE_TABLE,
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("ssm", region_name=_REGION).put_parameter(
        Name=CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
        Value=private_key_pem,
        Type="SecureString",
    )
    queue_url = boto3.client("sqs", region_name=_REGION).create_queue(QueueName=_QUEUE_NAME)[
        "QueueUrl"
    ]
    assert isinstance(queue_url, str)
    return queue_url


@pytest.fixture
def aws(
    monkeypatch: pytest.MonkeyPatch,
    rsa_test_private_key_pem: bytes,
    _respx_bytes_method: None,
) -> Iterator[_Aws]:
    """Moto resources, a cold handler and empty GitHub token/JWT caches.

    Every handler cache starts ``None``, so the first invocation runs the
    real ``_bootstrap`` inside ``mock_aws``. The clock is fixed so claim
    staleness is deterministic. ``time.sleep`` is already a no-op (conftest),
    which keeps the GitHub retry loop instant.
    """
    for key, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", _REGION),
        ("AWS_REGION", _REGION),
    ):
        monkeypatch.setenv(key, value)
    for name in (
        "_ENV",
        "_BOTO_SESSION",
        "_CLAIMS",
        "_SQS_CLIENT",
        "_DDB_CLIENT",
        "_GITHUB_CLIENT",
    ):
        monkeypatch.setattr(spot_retry_handler, name, None)
    monkeypatch.setattr(spot_retry_handler, "_NOW_MS", lambda: _NOW_MS)
    monkeypatch.setattr(never_fail_open_module, "_APP_JWT_CACHE", None)
    never_fail_open_module._TOKEN_CACHE.clear()
    with mock_aws():
        queue_url = _create_resources(rsa_test_private_key_pem.decode("utf-8"))
        monkeypatch.setenv("TRIKON_VERIFY_JOBS_QUEUE_URL", queue_url)
        yield _Aws(queue_url=queue_url)
    never_fail_open_module._TOKEN_CACHE.clear()


@dataclass(frozen=True)
class _GitHub:
    router: respx.MockRouter
    token: respx.Route
    post: respx.Route
    patch: respx.Route


@pytest.fixture
def github() -> Iterator[_GitHub]:
    """respx on api.github.com: token mint, Check Run POST and Interim PATCH.

    ``assert_all_mocked=True`` fails any other request; routes need not all
    be called, so each test asserts the calls it expects.
    """
    with respx.mock(
        base_url="https://api.github.com",
        assert_all_mocked=True,
        assert_all_called=False,
    ) as router:
        token = router.post(_TOKEN_PATH).mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_test_token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        )
        post = router.post(_POST_PATH).mock(return_value=httpx.Response(201, json={"id": 4242}))
        patch = router.patch(_PATCH_PATH).mock(
            return_value=httpx.Response(200, json={"id": _INTERIM_CHECK_RUN_ID})
        )
        yield _GitHub(router=router, token=token, post=post, patch=patch)


# ---------------------------------------------------------------------------
# Event builder and readers.
# ---------------------------------------------------------------------------


def _overrides(**changes: str | None) -> dict[str, str]:
    """The Attempt 1 Spot Runner_Overrides; ``None`` drops a variable."""
    env: dict[str, str] = {
        "TRIKON_INSTALLATION_ID": str(CANONICAL_INSTALLATION_ID),
        "TRIKON_REPO_FULL_NAME": CANONICAL_REPO_FULL_NAME,
        "TRIKON_PR_NUMBER": str(CANONICAL_PR_NUMBER),
        "TRIKON_HEAD_SHA": CANONICAL_HEAD_SHA,
        "TRIKON_BASE_SHA": CANONICAL_BASE_SHA,
        "TRIKON_EVENT_TYPE": CANONICAL_EVENT_TYPE,
        "TRIKON_DELIVERY_ID": CANONICAL_DELIVERY_ID,
        "TRIKON_RESERVATION_ID": _RESERVATION_ID,
        "TRIKON_ATTEMPT": "1",
        "TRIKON_CAPACITY": "FARGATE_SPOT",
    }
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _stopped_event(
    *,
    overrides: dict[str, str] | None = None,
    stop_code: str = "SpotInterruption",
    exit_code: int | None = None,
) -> dict[str, object]:
    """An EventBridge "ECS Task State Change" event for a stopped Sandbox_Task."""
    environment = [
        {"name": name, "value": value}
        for name, value in (overrides if overrides is not None else _overrides()).items()
    ]
    container: dict[str, object] = {"name": "runner", "lastStatus": "STOPPED"}
    if exit_code is not None:
        container["exitCode"] = exit_code
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "time": "2024-05-01T10:03:20Z",
        "region": _REGION,
        "detail": {
            "taskArn": _TASK_ARN,
            "clusterArn": _CLUSTER_ARN,
            "group": "family:trikon-verify-runner",
            "lastStatus": "STOPPED",
            "stopCode": stop_code,
            "stoppedAt": "2024-05-01T10:03:19.500Z",
            "capacityProviderName": "FARGATE_SPOT",
            "containers": [container],
            "overrides": {"containerOverrides": [{"name": "runner", "environment": environment}]},
        },
    }


def _invoke(event: dict[str, object]) -> None:
    lambda_handler(event, LambdaContext())


def _queued_bodies(aws: _Aws) -> list[str]:
    """Drain the Verify_Jobs_Queue and return every message body."""
    bodies: list[str] = []
    while True:
        response = boto3.client("sqs", region_name=_REGION).receive_message(
            QueueUrl=aws.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
        )
        messages = response.get("Messages", [])
        if not messages:
            return bodies
        bodies.extend(str(message["Body"]) for message in messages)


def _claim_status(aws: _Aws) -> str | None:
    item = boto3.client("dynamodb", region_name=_REGION).get_item(
        TableName=_CLAIMS_TABLE, Key={"task_arn": {"S": _TASK_ARN}}, ConsistentRead=True
    ).get("Item")
    return None if item is None else str(item["status"]["S"])


def _claim_count(aws: _Aws) -> int:
    return len(boto3.client("dynamodb", region_name=_REGION).scan(TableName=_CLAIMS_TABLE)["Items"])


@dataclass(frozen=True)
class _VerdictRow:
    decision: str
    matched_rule: str
    error_code: object


def _verdicts(aws: _Aws) -> list[_VerdictRow]:
    rows: list[_VerdictRow] = []
    for item in boto3.client("dynamodb", region_name=_REGION).scan(TableName=_VERDICTS_TABLE)["Items"]:
        evidence = json.loads(gzip.decompress(item["evidence_blob"]["B"]))
        assert isinstance(evidence, dict)
        rows.append(
            _VerdictRow(
                decision=str(item["decision"]["S"]),
                matched_rule=str(item["matched_rule"]["S"]),
                error_code=evidence.get("error_code"),
            )
        )
    return rows


def _put_pr_state(aws: _Aws, *, head_sha: str) -> None:
    boto3.client("dynamodb", region_name=_REGION).put_item(
        TableName=_PR_STATE_TABLE,
        Item={
            "pr_key": {"S": _PR_KEY},
            "last_head_sha": {"S": head_sha},
            "last_check_run_id": {"N": str(_INTERIM_CHECK_RUN_ID)},
            "last_comment_id": {"NULL": True},
        },
    )


def _body(route: respx.Route) -> dict[str, object]:
    body = json.loads(route.calls.last.request.content)
    assert isinstance(body, dict)
    return body


def _assert_posted_action_required(gh: _GitHub) -> None:
    """Exactly one Check Run POST, ``completed`` / ``action_required``, and no PATCH."""
    assert gh.post.call_count == 1
    assert gh.patch.call_count == 0
    body = _body(gh.post)
    assert body["name"] == "Trikon"
    assert body["head_sha"] == CANONICAL_HEAD_SHA
    assert body["status"] == "completed"
    assert body["conclusion"] == "action_required"


def _assert_no_github(gh: _GitHub) -> None:
    assert gh.router.calls.call_count == 0


# ---------------------------------------------------------------------------
# Requeue and non-interruption (Req 9.3, 9.6, 9.9).
# ---------------------------------------------------------------------------


def test_spot_interruption_queues_exactly_one_retry_job(aws: _Aws, github: _GitHub) -> None:
    """A Spot_Interruption sends one Retry_Job; a second delivery sends none (Req 9.3, 9.9)."""
    event = _stopped_event()
    _invoke(event)
    _invoke(event)

    bodies = _queued_bodies(aws)
    assert len(bodies) == 1
    job = VerifyJobMessage.model_validate_json(bodies[0])
    assert job.attempt == 2
    assert job.reservation_id == _RESERVATION_ID
    assert job.delivery_id == CANONICAL_DELIVERY_ID
    assert job.installation_id == CANONICAL_INSTALLATION_ID
    assert job.head_sha == CANONICAL_HEAD_SHA
    assert _claim_status(aws) == "sent"
    assert _verdicts(aws) == []
    _assert_no_github(github)


def test_non_interruption_is_a_no_op(aws: _Aws, github: _GitHub) -> None:
    """A normal stop sends nothing, claims nothing and calls no GitHub API (Req 9.6, 9.9)."""
    _invoke(_stopped_event(stop_code="EssentialContainerExited", exit_code=0))

    assert _queued_bodies(aws) == []
    assert _claim_count(aws) == 0
    assert _verdicts(aws) == []
    _assert_no_github(github)


# ---------------------------------------------------------------------------
# Malformed overrides (Req 11.4, 9.9).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected_rows"),
    [
        pytest.param({"TRIKON_RESERVATION_ID": None}, 1, id="reservation-missing"),
        pytest.param({"TRIKON_RESERVATION_ID": "not-a-uuid"}, 1, id="reservation-invalid"),
        pytest.param({"TRIKON_ATTEMPT": None}, 1, id="attempt-missing"),
        pytest.param({"TRIKON_ATTEMPT": "01"}, 1, id="attempt-noncanonical"),
        pytest.param({"TRIKON_BASE_SHA": None}, 0, id="base-sha-missing-no-row"),
    ],
)
def test_malformed_overrides_with_known_identity_fail_closed(
    aws: _Aws, github: _GitHub, changes: dict[str, str | None], expected_rows: int
) -> None:
    """Known repo, head and installation: publish a Fail_Closed_Result, send nothing (Req 11.4)."""
    _invoke(_stopped_event(overrides=_overrides(**changes)))

    assert _queued_bodies(aws) == []
    _assert_posted_action_required(github)
    rows = _verdicts(aws)
    assert len(rows) == expected_rows
    for row in rows:
        assert row.decision == "require_human"
        assert row.matched_rule == _SPOT_MATCHED_RULE
        assert row.error_code == "retry_job_malformed"
    assert _claim_status(aws) == "failed_closed"


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"TRIKON_HEAD_SHA": None}, id="head-missing"),
        pytest.param({"TRIKON_HEAD_SHA": "not-a-sha"}, id="head-invalid"),
        pytest.param({"TRIKON_INSTALLATION_ID": None}, id="installation-missing"),
        pytest.param({"TRIKON_INSTALLATION_ID": "0"}, id="installation-zero"),
        pytest.param({"TRIKON_REPO_FULL_NAME": None}, id="repo-missing"),
    ],
)
def test_malformed_overrides_with_unknown_identity_raise(
    aws: _Aws, github: _GitHub, changes: dict[str, str | None]
) -> None:
    """No addressable head: raise so the event is retried and dead-lettered (Req 11.4)."""
    with pytest.raises(SpotEventError):
        _invoke(_stopped_event(overrides=_overrides(**changes)))

    assert _queued_bodies(aws) == []
    assert _claim_count(aws) == 0
    assert _verdicts(aws) == []
    _assert_no_github(github)


# ---------------------------------------------------------------------------
# Fail-closed paths (Req 11.1, 11.2, 11.3).
# ---------------------------------------------------------------------------


def test_attempt_two_interruption_fails_closed_with_no_message(
    aws: _Aws, github: _GitHub
) -> None:
    """Max_Attempts reached: Fail_Closed_Result and no Job_Message (Req 11.2)."""
    _invoke(
        _stopped_event(overrides=_overrides(TRIKON_ATTEMPT="2", TRIKON_CAPACITY="FARGATE"))
    )

    assert _queued_bodies(aws) == []
    _assert_posted_action_required(github)
    assert [(r.decision, r.error_code) for r in _verdicts(aws)] == [
        ("require_human", "retry_exhausted")
    ]
    assert _claim_status(aws) == "failed_closed"


def test_exit_75_without_spot_interruption_fails_closed(aws: _Aws, github: _GitHub) -> None:
    """Runner exited 75 (Interim posted) but no Spot_Interruption: fail closed (Req 11.3)."""
    _invoke(_stopped_event(stop_code="EssentialContainerExited", exit_code=75))

    assert _queued_bodies(aws) == []
    _assert_posted_action_required(github)
    assert [(r.decision, r.error_code) for r in _verdicts(aws)] == [
        ("require_human", "interim_without_retry")
    ]
    assert _claim_status(aws) == "failed_closed"


class _FailingSqs:
    """Stub for the one SQS call: ``SendMessage`` raises a ``ClientError``."""

    def __init__(self) -> None:
        self.calls = 0

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        del QueueUrl, MessageBody
        self.calls += 1
        raise ClientError(
            {"Error": {"Code": "KMS.KMSAccessDeniedException", "Message": "stubbed"}},
            "SendMessage",
        )


def test_send_message_client_error_fails_closed(
    aws: _Aws, github: _GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Retry_Job send publishes a Fail_Closed_Result (Req 11.1)."""
    failing = _FailingSqs()
    # _bootstrap keeps an injected cache, so only the SQS client is stubbed.
    monkeypatch.setattr(spot_retry_handler, "_SQS_CLIENT", failing)

    _invoke(_stopped_event())

    assert failing.calls == 1
    assert _queued_bodies(aws) == []
    _assert_posted_action_required(github)
    assert [(r.decision, r.error_code) for r in _verdicts(aws)] == [
        ("require_human", "retry_send_failed")
    ]
    assert _claim_status(aws) == "failed_closed"


# ---------------------------------------------------------------------------
# Errors that must raise (Req 11.6).
# ---------------------------------------------------------------------------


def test_github_5xx_raises_and_leaves_claim_claimed(aws: _Aws, github: _GitHub) -> None:
    """A Check Run POST that keeps returning 502 raises, so Lambda retries (Req 11.6)."""
    github.post.mock(return_value=httpx.Response(502))

    with pytest.raises(OrchestratorGithubClientError):
        _invoke(_stopped_event(overrides=_overrides(TRIKON_ATTEMPT="2")))

    assert github.post.call_count == 3  # the client's full retry budget
    assert _queued_bodies(aws) == []
    assert _claim_status(aws) == "claimed"


def test_claim_store_non_conditional_error_raises(aws: _Aws, github: _GitHub) -> None:
    """A claim ``PutItem`` error other than the condition check raises (Req 9.3, 11.6)."""
    boto3.client("dynamodb", region_name=_REGION).delete_table(TableName=_CLAIMS_TABLE)

    with pytest.raises(ClientError) as excinfo:
        _invoke(_stopped_event())

    assert excinfo.value.response["Error"]["Code"] == "ResourceNotFoundException"
    assert _queued_bodies(aws) == []
    _assert_no_github(github)


def test_verdict_write_non_conditional_error_raises(aws: _Aws, github: _GitHub) -> None:
    """A verdict ``PutItem`` error other than the condition check raises (Req 11.6)."""
    boto3.client("dynamodb", region_name=_REGION).delete_table(TableName=_VERDICTS_TABLE)

    with pytest.raises(ClientError) as excinfo:
        _invoke(_stopped_event(overrides=_overrides(TRIKON_ATTEMPT="2")))

    assert excinfo.value.response["Error"]["Code"] == "ResourceNotFoundException"
    assert _queued_bodies(aws) == []
    assert _claim_status(aws) == "claimed"
    _assert_no_github(github)


# ---------------------------------------------------------------------------
# Interim_Check_Run completion (Req 11.3, 11.7).
# ---------------------------------------------------------------------------


def test_interim_run_on_same_head_is_patched(aws: _Aws, github: _GitHub) -> None:
    """``trikon_pr_state`` points at the Interim_Check_Run on this head: PATCH it."""
    _put_pr_state(aws, head_sha=CANONICAL_HEAD_SHA)

    _invoke(_stopped_event(stop_code="EssentialContainerExited", exit_code=75))

    assert github.patch.call_count == 1
    assert github.post.call_count == 0
    body = _body(github.patch)
    assert body["status"] == "completed"
    assert body["conclusion"] == "action_required"
    assert "head_sha" not in body
    assert _claim_status(aws) == "failed_closed"


@pytest.mark.parametrize(
    "pr_state_head",
    [pytest.param(_OTHER_HEAD_SHA, id="other-head"), pytest.param(None, id="no-row")],
)
def test_interim_run_elsewhere_or_absent_posts_new_run(
    aws: _Aws, github: _GitHub, pr_state_head: str | None
) -> None:
    """No Interim_Check_Run on this head: POST a new ``action_required`` Check Run."""
    if pr_state_head is not None:
        _put_pr_state(aws, head_sha=pr_state_head)

    _invoke(_stopped_event(stop_code="EssentialContainerExited", exit_code=75))

    _assert_posted_action_required(github)
    assert _claim_status(aws) == "failed_closed"
