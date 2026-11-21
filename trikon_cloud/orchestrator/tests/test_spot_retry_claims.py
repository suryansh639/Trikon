"""Property test for the Spot_Retry_Handler dedupe claim (cost-cuts D3).

Feature: trikon-cloud-cost-cuts, Property 10: At most one Retry_Job per
interrupted task. Validates Requirements 9.3 and 11.1.

Drives the real :func:`lambda_handler` through generated sequences of
deliveries of the same Task_Stopped_Events (one Spot-interrupted Attempt 1
task, interleaved with other stopped tasks) on a generated non-decreasing
clock that crosses :data:`CLAIM_STALE_MS`. Each delivery may inject a fault
at the claim ``PutItem`` (throttled, or a crash right after the write), at
``SendMessage`` (an error, or a crash right after the send), at the ``mark``
``UpdateItem`` and at the GitHub publish.

The doubles are local: an in-memory DynamoDB with conditional-put semantics
on ``task_arn`` (claims) and on the verdict natural key, a recording SQS
queue and a recording GitHub client. A crash is modelled as a non-AWS
exception that escapes the handler, as a Lambda timeout or process death
would end the invocation without running the remaining steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.orchestrator import spot_retry_handler
from trikon_cloud.orchestrator.models import SpotRetryEnvConfig, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import (
    CheckRunConclusion,
    OrchestratorGithubClient,
    OrchestratorGithubClientError,
)
from trikon_cloud.orchestrator.spot_retry_claims import (
    CLAIM_STALE_MS,
    ClaimRecord,
    ClaimResolution,
    SpotRetryClaims,
    resolve_claim,
)
from trikon_cloud.orchestrator.spot_retry_handler import (
    SpotRetryInFlightError,
    lambda_handler,
)
from trikon_cloud.orchestrator.tests.conftest import (
    CANONICAL_BASE_SHA,
    CANONICAL_EVENT_TYPE,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
)

_CLAIMS_TABLE: Final = "trikon-cloud-spot-retries"
_PR_STATE_TABLE: Final = "trikon_pr_state"
_VERDICTS_TABLE: Final = "trikon_verdicts"
_CLUSTER_ARN: Final = "arn:aws:ecs:us-east-1:000000000000:cluster/trikon-verify-cluster"
_RESERVATION_ID: Final = "3f1c2a9e-7b4d-4e8a-9c21-5d6e7f8a9b0c"
_START_MS: Final = 1_714_557_751_000

TaskKind = Literal["requeue", "exhausted", "interim", "ignored"]
ClaimFault = Literal["none", "throttle", "crash_after_write"]
SendFault = Literal["none", "error", "crash_after_send"]


# ---------------------------------------------------------------------------
# Doubles.
# ---------------------------------------------------------------------------


class _CrashError(Exception):
    """The invocation dies here (timeout or process death), not an AWS error."""


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "stubbed"}}, operation)


@dataclass
class _Faults:
    """Faults armed for the current delivery only."""

    claim: ClaimFault = "none"
    send: SendFault = "none"
    mark_fails: bool = False
    publish_fails: bool = False


@dataclass
class _World:
    """Shared state of the doubles; ``current`` is the delivery index."""

    current: int = -1
    faults: _Faults = field(default_factory=_Faults)
    claims: dict[str, dict[str, Mapping[str, object]]] = field(default_factory=dict)
    verdict_keys: set[tuple[str, str]] = field(default_factory=set)
    claim_puts: list[int] = field(default_factory=list)
    claims_created: set[int] = field(default_factory=set)
    sent: list[tuple[int, str]] = field(default_factory=list)
    check_runs: list[tuple[int, CheckRunConclusion]] = field(default_factory=list)


class _FakeDynamoDb:
    """Claims with ``attribute_not_exists(task_arn)``, verdicts on the natural key."""

    def __init__(self, world: _World) -> None:
        self._w = world

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Item: Mapping[str, Mapping[str, object]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object:
        w = self._w
        if TableName == _CLAIMS_TABLE:
            assert ConditionExpression == "attribute_not_exists(task_arn)"
            w.claim_puts.append(w.current)
            if w.faults.claim == "throttle":
                raise _client_error("ProvisionedThroughputExceededException", "PutItem")
            key = str(Item["task_arn"]["S"])
            if key in w.claims:
                raise _client_error("ConditionalCheckFailedException", "PutItem")
            w.claims[key] = dict(Item)
            w.claims_created.add(w.current)
            if w.faults.claim == "crash_after_write":
                raise _CrashError("died after the claim write")
            return {}
        assert TableName == _VERDICTS_TABLE
        natural_key = (repr(Item["installation_id"]), repr(Item["sk"]))
        if natural_key in w.verdict_keys:
            raise _client_error("ConditionalCheckFailedException", "PutItem")
        w.verdict_keys.add(natural_key)
        return {}

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        ConsistentRead: bool,  # noqa: N803
    ) -> dict[str, object]:
        assert ConsistentRead
        if TableName == _CLAIMS_TABLE:
            item = self._w.claims.get(Key["task_arn"]["S"])
            return {} if item is None else {"Item": dict(item)}
        assert TableName == _PR_STATE_TABLE
        return {}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeNames: Mapping[str, str],  # noqa: N803
        ExpressionAttributeValues: Mapping[str, Mapping[str, str]],  # noqa: N803
    ) -> object:
        assert TableName == _CLAIMS_TABLE
        if self._w.faults.mark_fails:
            raise _client_error("ProvisionedThroughputExceededException", "UpdateItem")
        self._w.claims[Key["task_arn"]["S"]]["status"] = dict(ExpressionAttributeValues[":s"])
        return {}


class _RecordingSqs:
    def __init__(self, world: _World) -> None:
        self._w = world

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        if self._w.faults.send == "error":
            raise _client_error("ServiceUnavailable", "SendMessage")
        self._w.sent.append((self._w.current, MessageBody))
        if self._w.faults.send == "crash_after_send":
            raise _CrashError("died after SendMessage")
        return {"MessageId": f"m-{len(self._w.sent)}"}


class _RecordingGithub(OrchestratorGithubClient):
    """Records published Check Runs per delivery; may fail every call."""

    def __init__(self, world: _World) -> None:
        self._w = world

    def _record(self, conclusion: CheckRunConclusion) -> None:
        if self._w.faults.publish_fails:
            raise OrchestratorGithubClientError("github request returned 502 after 4 attempts")
        self._w.check_runs.append((self._w.current, conclusion))

    def create_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        self._record(conclusion)

    def update_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        check_run_id: int,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        self._record(conclusion)


# ---------------------------------------------------------------------------
# Events.
# ---------------------------------------------------------------------------


def _task_arn(task: int) -> str:
    return f"arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/{task:08x}abcd"


def _delivery_id(task: int) -> str:
    return f"e6e7a4d0-2b3c-4c5d-b6a7-{task:012x}"


def _event(task: int, kind: TaskKind) -> dict[str, object]:
    env: dict[str, str] = {
        "TRIKON_INSTALLATION_ID": str(CANONICAL_INSTALLATION_ID),
        "TRIKON_REPO_FULL_NAME": CANONICAL_REPO_FULL_NAME,
        "TRIKON_PR_NUMBER": str(CANONICAL_PR_NUMBER),
        "TRIKON_HEAD_SHA": f"{task:x}" * 40,
        "TRIKON_BASE_SHA": CANONICAL_BASE_SHA,
        "TRIKON_EVENT_TYPE": CANONICAL_EVENT_TYPE,
        "TRIKON_DELIVERY_ID": _delivery_id(task),
        "TRIKON_RESERVATION_ID": _RESERVATION_ID,
        "TRIKON_ATTEMPT": "2" if kind == "exhausted" else "1",
        "TRIKON_CAPACITY": "FARGATE" if kind == "exhausted" else "FARGATE_SPOT",
    }
    spot = kind in ("requeue", "exhausted")
    return {
        "version": "0",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "time": "2024-05-01T10:02:31Z",
        "detail": {
            "taskArn": _task_arn(task),
            "clusterArn": _CLUSTER_ARN,
            "group": "family:trikon-verify-runner",
            "lastStatus": "STOPPED",
            "stopCode": "SpotInterruption" if spot else "EssentialContainerExited",
            "stoppedAt": "2024-05-01T10:02:30.123456Z",
            "capacityProviderName": "FARGATE" if kind == "exhausted" else "FARGATE_SPOT",
            "containers": [
                {
                    "name": "runner",
                    "lastStatus": "STOPPED",
                    "exitCode": 75 if kind == "interim" else 0,
                }
            ],
            "overrides": {
                "containerOverrides": [
                    {
                        "name": "runner",
                        "environment": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Delivery:
    task: int
    advance_ms: int
    claim: ClaimFault
    send: SendFault
    mark_fails: bool
    publish_fails: bool


# Small steps keep a claim live; the boundary and large steps make it stale.
_ADVANCE = st.one_of(
    st.integers(min_value=0, max_value=5_000),
    st.sampled_from([CLAIM_STALE_MS - 1, CLAIM_STALE_MS]),
    st.integers(min_value=CLAIM_STALE_MS, max_value=3 * CLAIM_STALE_MS),
)
_CLAIM_FAULT: st.SearchStrategy[ClaimFault] = st.sampled_from(
    ["none", "none", "throttle", "crash_after_write"]
)
_SEND_FAULT: st.SearchStrategy[SendFault] = st.sampled_from(
    ["none", "none", "error", "crash_after_send"]
)
_OTHER_KIND: st.SearchStrategy[TaskKind] = st.sampled_from(
    ["requeue", "exhausted", "interim", "ignored"]
)


@st.composite
def _scenarios(draw: st.DrawFn) -> tuple[list[TaskKind], list[_Delivery]]:
    """Task 0 is a Spot-interrupted Attempt 1 task with 1-6 deliveries."""
    kinds: list[TaskKind] = ["requeue", *draw(st.lists(_OTHER_KIND, max_size=2))]
    order = [0] * draw(st.integers(min_value=1, max_value=6))
    for task in range(1, len(kinds)):
        order += [task] * draw(st.integers(min_value=1, max_value=3))
    order = draw(st.permutations(order))
    deliveries = [
        _Delivery(
            task=task,
            advance_ms=draw(_ADVANCE),
            claim=draw(_CLAIM_FAULT),
            send=draw(_SEND_FAULT),
            mark_fails=draw(st.booleans()),
            publish_fails=draw(st.booleans()),
        )
        for task in order
    ]
    return kinds, deliveries


# ---------------------------------------------------------------------------
# Reference model.
# ---------------------------------------------------------------------------


def _expected_resolution(stored: Mapping[str, object] | None, now_ms: int) -> ClaimResolution:
    """Independent restatement of D3 over the raw stored item."""
    if stored is None:
        return "proceed"
    status = stored["status"]
    claimed_at = stored["claimed_at_ms"]
    assert isinstance(status, Mapping) and isinstance(claimed_at, Mapping)
    if status["S"] in ("sent", "failed_closed"):
        return "duplicate"
    if now_ms - int(str(claimed_at["N"])) < CLAIM_STALE_MS:
        return "in_flight"
    return "proceed_fail_closed"


def _task_of_body(body: str) -> str:
    return VerifyJobMessage.model_validate_json(body).delivery_id


# ---------------------------------------------------------------------------
# Property 10.
# ---------------------------------------------------------------------------


# Feature: trikon-cloud-cost-cuts, Property 10: At most one Retry_Job per interrupted task
@given(scenario=_scenarios())
@settings(max_examples=200, deadline=None)
def test_property_at_most_one_retry_job_per_interrupted_task(
    scenario: tuple[list[TaskKind], list[_Delivery]],
) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 10: At most one Retry_Job per interrupted task.

    **Validates: Requirements 9.3, 11.1**
    """
    kinds, deliveries = scenario
    world = _World()
    ddb = _FakeDynamoDb(world)
    clock = [_START_MS]
    last_raised: dict[int, bool] = {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spot_retry_handler, "_ENV", SpotRetryEnvConfig())
        mp.setattr(
            spot_retry_handler,
            "_CLAIMS",
            SpotRetryClaims(table_name=_CLAIMS_TABLE, ddb_client=ddb),
        )
        mp.setattr(spot_retry_handler, "_DDB_CLIENT", ddb)
        mp.setattr(spot_retry_handler, "_SQS_CLIENT", _RecordingSqs(world))
        mp.setattr(spot_retry_handler, "_GITHUB_CLIENT", _RecordingGithub(world))
        mp.setattr(spot_retry_handler, "_NOW_MS", lambda: clock[0])

        for index, delivery in enumerate(deliveries):
            clock[0] += delivery.advance_ms
            kind = kinds[delivery.task]
            stored = world.claims.get(_task_arn(delivery.task))
            expected = _expected_resolution(stored, clock[0])
            world.current = index
            world.faults = _Faults(
                claim=delivery.claim,
                send=delivery.send,
                mark_fails=delivery.mark_fails,
                publish_fails=delivery.publish_fails,
            )

            error: Exception | None = None
            try:
                lambda_handler(_event(delivery.task, kind), LambdaContext())
            except Exception as exc:  # classified below
                error = exc
            last_raised[delivery.task] = error is not None

            sent_now = [body for i, body in world.sent if i == index]
            published_now = [c for i, c in world.check_runs if i == index]
            claimed_now = index in world.claim_puts

            # Per-delivery outcome against the reference model.
            if kind == "ignored":
                assert not claimed_now and not sent_now and not published_now
                assert error is None
                continue
            assert claimed_now
            if delivery.claim == "throttle":
                assert isinstance(error, ClientError)
                assert not sent_now and not published_now
                continue
            if expected == "proceed" and delivery.claim == "crash_after_write":
                assert isinstance(error, _CrashError)
                assert not sent_now and not published_now
                continue
            if expected == "duplicate":
                assert error is None and not sent_now and not published_now
                continue
            if expected == "in_flight":
                assert isinstance(error, SpotRetryInFlightError)
                assert not sent_now and not published_now
                continue

            sends = kind == "requeue" and expected == "proceed" and delivery.send != "error"
            if sends:
                # Only the invocation that created the claim ever sends.
                assert index in world.claims_created
                assert len(sent_now) == 1 and not published_now
                if delivery.send == "crash_after_send":
                    assert isinstance(error, _CrashError)
                else:
                    assert error is None
                continue
            # Every other proceed / proceed_fail_closed path fails closed
            # instead of sending (failed send, stale claim, non-requeue decision).
            assert not sent_now
            if delivery.publish_fails:
                assert isinstance(error, OrchestratorGithubClientError)
                assert not published_now
            else:
                assert error is None
                assert published_now == ["action_required"]

    # Global invariants across the whole sequence.
    sends_per_task: dict[str, int] = {}
    for index, body in world.sent:
        assert index in world.claims_created
        task_id = _task_of_body(body)
        sends_per_task[task_id] = sends_per_task.get(task_id, 0) + 1
    assert all(count == 1 for count in sends_per_task.values()), sends_per_task
    for task, kind in enumerate(kinds):
        if kind != "requeue":
            assert _delivery_id(task) not in sends_per_task
    assert all(conclusion == "action_required" for _, conclusion in world.check_runs)

    task_of_delivery = {index: delivery.task for index, delivery in enumerate(deliveries)}
    for task, kind in enumerate(kinds):
        reached_claim = any(task_of_delivery[i] == task for i in world.claim_puts)
        if not reached_claim or _delivery_id(task) in sends_per_task:
            continue
        published = any(task_of_delivery[i] == task for i, _ in world.check_runs)
        assert published or last_raised[task], (task, kind)


# Feature: trikon-cloud-cost-cuts, Property 10: At most one Retry_Job per interrupted task
@given(
    existing=st.one_of(
        st.none(),
        st.builds(
            ClaimRecord,
            status=st.sampled_from(["claimed", "sent", "failed_closed"]),
            claimed_at_ms=st.integers(min_value=0, max_value=10**13),
        ),
    ),
    delta_ms=st.one_of(
        st.integers(min_value=-1_000, max_value=3 * CLAIM_STALE_MS),
        st.sampled_from([CLAIM_STALE_MS - 1, CLAIM_STALE_MS, CLAIM_STALE_MS + 1]),
    ),
)
@settings(max_examples=200, deadline=None)
def test_property_resolve_claim_follows_d3(existing: ClaimRecord | None, delta_ms: int) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 10: At most one Retry_Job per interrupted task (resolver).

    **Validates: Requirements 9.3, 11.1**
    """
    base = 0 if existing is None else existing.claimed_at_ms
    now_ms = base + delta_ms
    resolution = resolve_claim(existing, now_ms=now_ms)
    if existing is None:
        assert resolution == "proceed"
    elif existing.status in ("sent", "failed_closed"):
        assert resolution == "duplicate"
    elif delta_ms < CLAIM_STALE_MS:
        assert resolution == "in_flight"
    else:
        assert resolution == "proceed_fail_closed"
    assert resolve_claim(existing, now_ms=now_ms) == resolution
