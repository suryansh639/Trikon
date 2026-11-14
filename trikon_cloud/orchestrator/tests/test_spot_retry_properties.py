# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin generates,
# not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Property tests for the Spot_Retry_Handler's pure decision logic (cost-cuts).

* Property 7 (task 14.2): the Retry_Job round trip from Runner_Overrides.
  The overrides :func:`build_run_task_call` writes for an Attempt 1 job,
  echoed back in a Spot_Interruption stop event, rebuild that job as
  Attempt 2 with the same Run_Id and Reservation_Id (Req 9.2, 9.4).
* Property 9 (task 14.3): the stopped-task decision table. For generated
  Task_Stopped_Events (sandbox or not, any stop code, runner exit code,
  Attempt and override fields valid, invalid or missing),
  :func:`decide_stopped_task` equals an independent reference
  implementation of the design's table and is deterministic (Req 9.6,
  11.2, 11.3, 11.4).

The row-by-row examples live in ``test_spot_retry_decisions.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Final, Literal

from hypothesis import event as hypothesis_event
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.orchestrator.ecs_dispatcher import build_run_task_call
from trikon_cloud.orchestrator.models import OrchestratorEnvConfig, VerifyJobMessage
from trikon_cloud.orchestrator.never_fail_open import FailClosedTarget
from trikon_cloud.orchestrator.spot_retry import (
    FailClosed,
    Ignore,
    Requeue,
    StoppedTaskDecision,
    Unroutable,
    decide_stopped_task,
    retry_sent_at,
)
from trikon_cloud.usage_reporter.models import EcsTaskStateChangeEvent
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdef"
_SHAS = st.text(alphabet=_HEX, min_size=40, max_size=40)
_NAME_PART = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1,
    max_size=39,
)
# GitHub ids are signed 64-bit; the override parser accepts up to 19 digits.
_GITHUB_IDS = st.integers(min_value=1, max_value=2**63 - 1)
_REPO_FULL_NAMES = st.one_of(
    st.builds(lambda owner, name: f"{owner}/{name}", _NAME_PART, _NAME_PART),
    st.text(min_size=1, max_size=80),
)
_EVENT_TYPES = st.one_of(
    st.sampled_from(
        ["pull_request.opened", "pull_request.synchronize", "pull_request.reopened"]
    ),
    st.text(min_size=1, max_size=40),
)
# The Run_Id: a GitHub delivery GUID in practice, any string on the wire.
_DELIVERY_IDS = st.one_of(st.uuids().map(str), st.text(min_size=1, max_size=64))
# ``sent_at`` is replaced on the Retry_Job, so any string will do.
_SENT_ATS = st.one_of(
    st.datetimes(timezones=st.just(UTC)).map(lambda dt: dt.isoformat(timespec="milliseconds")),
    st.text(max_size=40),
)


@st.composite
def _reservation_ids(draw: st.DrawFn) -> str:
    """UUID strings in lower, upper or per-character mixed case."""
    value = str(draw(st.uuids()))
    flips = draw(st.lists(st.booleans(), min_size=len(value), max_size=len(value)))
    return "".join(ch.upper() if flip else ch for ch, flip in zip(value, flips, strict=True))


@st.composite
def _attempt_one_jobs(draw: st.DrawFn) -> VerifyJobMessage:
    """A valid Attempt 1 Job_Message: no ``attempt`` or ``reservation_id`` on the wire."""
    return VerifyJobMessage(
        installation_id=draw(_GITHUB_IDS),
        repo_full_name=draw(_REPO_FULL_NAMES),
        pr_number=draw(_GITHUB_IDS),
        head_sha=draw(_SHAS),
        base_sha=draw(_SHAS),
        event_type=draw(_EVENT_TYPES),
        sent_at=draw(_SENT_ATS),
        delivery_id=draw(_DELIVERY_IDS),
    )


# Aware instants with whole-minute UTC offsets, as EventBridge or ECS might
# render them; the Retry_Job ``sent_at`` is always normalised to UTC.
_OFFSETS = st.integers(min_value=-(23 * 60 + 59), max_value=23 * 60 + 59).map(
    lambda minutes: timezone(timedelta(minutes=minutes))
)
# Hypothesis takes naive bounds when ``timezones`` is given.
_INSTANTS = st.datetimes(
    min_value=datetime(2000, 1, 2),
    max_value=datetime(2099, 12, 30),
    timezones=_OFFSETS,
)
# Any runner exit except 75 (Runner_Exit_Interim), or none at all.
_NON_INTERIM_EXITS = st.one_of(
    st.none(), st.integers(min_value=-(2**31), max_value=2**31 - 1).filter(lambda c: c != 75)
)
_TASK_REVISIONS = st.integers(min_value=1, max_value=10**6)
_REGIONS = st.sampled_from(["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"])
_ACCOUNTS = st.text(alphabet="0123456789", min_size=12, max_size=12)


def _expected_sent_at(instant: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC, milliseconds floored (rebuilt here)."""
    utc = instant.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Property 7.
# ---------------------------------------------------------------------------


# Feature: trikon-cloud-cost-cuts, Property 7: Retry_Job round trip from Runner_Overrides
@given(
    message=_attempt_one_jobs(),
    reservation_id=_reservation_ids(),
    revision=_TASK_REVISIONS,
    region=_REGIONS,
    account=_ACCOUNTS,
    event_time=_INSTANTS,
    stopped_at=st.one_of(st.none(), _INSTANTS),
    exit_code=_NON_INTERIM_EXITS,
)
@settings(max_examples=200, deadline=None)
def test_property_retry_job_round_trip_from_runner_overrides(
    message: VerifyJobMessage,
    reservation_id: str,
    revision: int,
    region: str,
    account: str,
    event_time: datetime,
    stopped_at: datetime | None,
    exit_code: int | None,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 7: Retry_Job round trip from Runner_Overrides.

    The overrides the Orchestrator sends for an Attempt 1 Spot task, echoed
    back by ECS in a Spot_Interruption Task_Stopped_Event, rebuild the
    Job_Message as an Attempt 2 Retry_Job that keeps the Run_Id and the
    Reservation_Id byte for byte and survives a JSON round trip.

    **Validates: Requirements 9.2, 9.4**
    """
    assert message.attempt == 1
    assert message.reservation_id is None

    task_definition = f"trikon-verify-runner:{revision}"
    call = build_run_task_call(
        message,
        env=OrchestratorEnvConfig(),
        task_definition=task_definition,
        reservation_id=reservation_id,
        attempt=1,
        capacity="FARGATE_SPOT",
    )
    wire = call.model_dump(by_alias=True, mode="json", exclude_none=True)

    # ECS echoes the RunTask ``overrides`` in the stopped task's detail; the
    # task group is ``family:<family>`` of the task definition.
    container: dict[str, object] = {"name": "runner", "lastStatus": "STOPPED"}
    if exit_code is not None:
        container["exitCode"] = exit_code
    detail: dict[str, object] = {
        "taskArn": f"arn:aws:ecs:{region}:{account}:task/{wire['cluster']}/0123abcd",
        "clusterArn": f"arn:aws:ecs:{region}:{account}:cluster/{wire['cluster']}",
        "taskDefinitionArn": f"arn:aws:ecs:{region}:{account}:task-definition/{task_definition}",
        "group": f"family:{task_definition.split(':', 1)[0]}",
        "lastStatus": "STOPPED",
        "stopCode": "SpotInterruption",
        "capacityProviderName": "FARGATE_SPOT",
        "overrides": wire["overrides"],
        "containers": [container],
    }
    if stopped_at is not None:
        detail["stoppedAt"] = stopped_at.isoformat()
    raw = {
        "version": "0",
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": account,
        "time": event_time.isoformat(),
        "region": region,
        "detail": detail,
    }
    # Lambda hands the handler the EventBridge JSON, already parsed.
    event = EcsTaskStateChangeEvent.model_validate(json.loads(json.dumps(raw)))

    decision = decide_stopped_task(event)

    assert isinstance(decision, Requeue)
    job = decision.job
    sent_at = retry_sent_at(event)
    assert sent_at == _expected_sent_at(stopped_at if stopped_at is not None else event_time)
    assert job == message.model_copy(
        update={"attempt": 2, "reservation_id": reservation_id, "sent_at": sent_at}
    )
    # Run_Id and Reservation_Id byte-identical (Req 9.2, 9.4).
    assert job.delivery_id.encode("utf-8", "surrogatepass") == message.delivery_id.encode(
        "utf-8", "surrogatepass"
    )
    assert job.reservation_id == reservation_id

    # The Retry_Job survives the Verify_Jobs_Queue wire format.
    assert VerifyJobMessage.model_validate_json(job.model_dump_json()) == job


# ---------------------------------------------------------------------------
# Property 9.
# ---------------------------------------------------------------------------

# The reference implementation below restates the design's decision table
# with literal names and values. It uses no ``spot_retry`` or
# ``spot_contract`` helper, so it cannot share a parsing bug with the code
# under test.
_REF_SANDBOX_CLUSTER: Final = "trikon-verify-cluster"
_REF_SANDBOX_GROUP: Final = "family:trikon-verify-runner"
_REF_STOPPED: Final = "STOPPED"
_REF_SPOT_STOP_CODE: Final = "SpotInterruption"
_REF_INTERIM_EXIT: Final = 75
_REF_UUID: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_REF_DIGITS: Final = frozenset("0123456789")
_REF_HEX: Final = frozenset(_HEX)

_INSTALLATION_ID: Final = "TRIKON_INSTALLATION_ID"
_REPO_FULL_NAME: Final = "TRIKON_REPO_FULL_NAME"
_PR_NUMBER: Final = "TRIKON_PR_NUMBER"
_HEAD_SHA: Final = "TRIKON_HEAD_SHA"
_BASE_SHA: Final = "TRIKON_BASE_SHA"
_EVENT_TYPE: Final = "TRIKON_EVENT_TYPE"
_DELIVERY_ID: Final = "TRIKON_DELIVERY_ID"
_RESERVATION_ID: Final = "TRIKON_RESERVATION_ID"
_ATTEMPT: Final = "TRIKON_ATTEMPT"
_CAPACITY: Final = "TRIKON_CAPACITY"

_P9_REGION: Final = "us-east-1"
_P9_ACCOUNT: Final = "123456789012"

EnvPlacement = Literal["runner", "no_overrides", "other_container"]


@dataclass(frozen=True)
class _StopCase:
    """The raw inputs of one generated Task_Stopped_Event."""

    cluster_name: str
    group: str | None
    last_status: str
    stop_code: str | None
    runner_container: bool
    runner_exit: int | None
    sidecar_exit: int | Literal["absent"] | None
    env_placement: EnvPlacement
    env: tuple[tuple[str, str], ...]
    event_time: datetime
    stopped_at: datetime | None


# -- Strategies -------------------------------------------------------------

_CLUSTER_MISMATCHES = st.sampled_from(
    ["trikon-verify-cluster-staging", "TRIKON-VERIFY-CLUSTER", "default", "trikon-verify"]
)
_GROUP_MISMATCHES = st.sampled_from(
    [
        None,
        "family:trikon-verify-runner-canary",
        "family:other",
        "service:trikon-verify-runner",
        "FAMILY:trikon-verify-runner",
    ]
)
_STATUS_MISMATCHES = st.sampled_from(["RUNNING", "DEPROVISIONING", "PENDING", "stopped"])
# Branches are drawn about evenly, so repeating one weights it: half the
# stops are Spot_Interruptions and two in five runner exits are 75.
_STOP_CODES = st.one_of(
    st.just("SpotInterruption"),
    st.just("SpotInterruption"),
    st.sampled_from(
        [
            "EssentialContainerExited",
            "UserInitiated",
            "TaskFailedToStart",
            "ServiceSchedulerInitiated",
            "TerminationNotice",
            "spotinterruption",
            "SpotInterruption ",
        ]
    ),
    st.none(),
)
_EXIT_CODES = st.one_of(
    st.just(75),
    st.just(75),
    st.sampled_from([0, 1, 137, 143]),
    st.integers(min_value=-(2**31), max_value=2**31 - 1),
    st.none(),
)

# Invalid values are near misses of valid ones plus arbitrary text. The
# reference judges every raw value itself, so arbitrary text that happens
# to be valid is still classified correctly.
_INVALID_INTS = st.one_of(
    _GITHUB_IDS.map(lambda n: f"0{n}"),
    _GITHUB_IDS.map(lambda n: f"+{n}"),
    _GITHUB_IDS.map(lambda n: f"-{n}"),
    _GITHUB_IDS.map(lambda n: f" {n}"),
    _GITHUB_IDS.map(lambda n: f"{n}\n"),
    _GITHUB_IDS.map(lambda n: f"{n}.0"),
    st.integers(min_value=1000, max_value=10**9).map(lambda n: f"{n:_}"),
    st.sampled_from(["0", "", "abc", "0x1f", "\u0661\u0662", "\uff11\uff12"]),
    st.text(max_size=21),
)
_INVALID_SHAS = st.one_of(
    _SHAS.map(lambda sha: "A" + sha[1:]),
    _SHAS.map(lambda sha: sha[:-1] + "F"),
    _SHAS.map(lambda sha: sha[:39]),
    _SHAS.map(lambda sha: sha + "0"),
    _SHAS.map(lambda sha: sha + "\n"),
    _SHAS.map(lambda sha: "g" + sha[1:]),
    st.just(""),
    st.text(max_size=41),
)
_INVALID_RESERVATION_IDS = st.one_of(
    st.sampled_from(["", "not-a-uuid"]),
    st.uuids().map(lambda value: value.hex),
    st.uuids().map(lambda value: str(value)[:35]),
    st.uuids().map(lambda value: f"{value}\n"),
    st.uuids().map(lambda value: f" {value}"),
    st.uuids().map(lambda value: f"{{{value}}}"),
    st.text(max_size=40),
)
_INVALID_ATTEMPTS = st.one_of(
    st.sampled_from(["0", "3", "01", " 1", "1 ", "2\n", "one", ""]),
    st.text(max_size=3),
)

#: Each override: (valid values, invalid values or ``None`` when every
#: string is valid). Attempt "2" counts as valid input; the table routes it.
_FieldSpec = tuple[str, st.SearchStrategy[str], st.SearchStrategy[str] | None]
_OVERRIDE_FIELDS: Final[tuple[_FieldSpec, ...]] = (
    (_INSTALLATION_ID, _GITHUB_IDS.map(str), _INVALID_INTS),
    (_REPO_FULL_NAME, _REPO_FULL_NAMES, st.just("")),
    (_PR_NUMBER, _GITHUB_IDS.map(str), _INVALID_INTS),
    (_HEAD_SHA, _SHAS, _INVALID_SHAS),
    (_BASE_SHA, _SHAS, _INVALID_SHAS),
    (_EVENT_TYPE, _EVENT_TYPES, None),
    (_DELIVERY_ID, _DELIVERY_IDS, None),
    (_RESERVATION_ID, _reservation_ids(), _INVALID_RESERVATION_IDS),
    (_ATTEMPT, st.sampled_from(["1", "1", "2"]), _INVALID_ATTEMPTS),
    (_CAPACITY, st.sampled_from(["FARGATE_SPOT", "FARGATE"]), st.text(max_size=12)),
)
_FIELD_NAMES: Final = tuple(name for name, _, _ in _OVERRIDE_FIELDS)
# The identity names the pull request head; corrupting only the other
# fields keeps the event routable.
_IDENTITY_FIELDS: Final = (_INSTALLATION_ID, _REPO_FULL_NAME, _HEAD_SHA)
_NON_IDENTITY_FIELDS: Final = tuple(name for name in _FIELD_NAMES if name not in _IDENTITY_FIELDS)


@st.composite
def _override_envs(draw: st.DrawFn) -> tuple[tuple[str, str], ...]:
    """Runner_Overrides with zero to four fields invalid or missing, in any order.

    A corrupted field is invalid three times in four and missing otherwise
    (always missing when every string is valid for it).
    """
    corrupted = draw(
        st.one_of(
            st.just(frozenset[str]()),
            st.just(frozenset[str]()),
            st.sampled_from(_FIELD_NAMES).map(lambda name: frozenset({name})),
            st.sampled_from(_IDENTITY_FIELDS).map(lambda name: frozenset({name})),
            st.frozensets(st.sampled_from(_NON_IDENTITY_FIELDS), min_size=1, max_size=3),
            st.frozensets(st.sampled_from(_FIELD_NAMES), min_size=1, max_size=4),
        )
    )
    env: list[tuple[str, str]] = []
    for name, valid, invalid in _OVERRIDE_FIELDS:
        if name not in corrupted:
            env.append((name, draw(valid)))
        elif invalid is not None and draw(st.sampled_from([True, True, True, False])):
            env.append((name, draw(invalid)))
        # else: the field is missing.
    return tuple(draw(st.permutations(env)))


@st.composite
def _stop_cases(draw: st.DrawFn) -> _StopCase:
    """A Task_Stopped_Event's inputs; three in four are Sandbox_Tasks."""
    cluster_name: str = _REF_SANDBOX_CLUSTER
    group: str | None = _REF_SANDBOX_GROUP
    last_status: str = _REF_STOPPED
    if not draw(st.sampled_from([True, True, True, False])):
        mismatched = draw(
            st.frozensets(st.sampled_from(["cluster", "group", "status"]), min_size=1)
        )
        if "cluster" in mismatched:
            cluster_name = draw(_CLUSTER_MISMATCHES)
        if "group" in mismatched:
            group = draw(_GROUP_MISMATCHES)
        if "status" in mismatched:
            last_status = draw(_STATUS_MISMATCHES)
    sidecar: int | Literal["absent"] | None = draw(st.one_of(st.just("absent"), _EXIT_CODES))
    return _StopCase(
        cluster_name=cluster_name,
        group=group,
        last_status=last_status,
        stop_code=draw(_STOP_CODES),
        runner_container=draw(st.sampled_from([True, True, True, False])),
        runner_exit=draw(_EXIT_CODES),
        sidecar_exit=sidecar,
        env_placement=draw(
            st.one_of(
                st.just("runner"),
                st.just("runner"),
                st.sampled_from(["runner", "no_overrides", "other_container"]),
            )
        ),
        env=draw(_override_envs()),
        event_time=draw(_INSTANTS),
        stopped_at=draw(st.one_of(st.none(), _INSTANTS)),
    )


def _stop_event(case: _StopCase) -> EcsTaskStateChangeEvent:
    """Render ``case`` as the EventBridge JSON Lambda hands the handler."""
    containers: list[dict[str, object]] = []
    if case.sidecar_exit != "absent":
        # A non-runner container listed first; its exit code never counts.
        sidecar: dict[str, object] = {"name": "log-router", "lastStatus": case.last_status}
        if case.sidecar_exit is not None:
            sidecar["exitCode"] = case.sidecar_exit
        containers.append(sidecar)
    if case.runner_container:
        runner: dict[str, object] = {"name": "runner", "lastStatus": case.last_status}
        if case.runner_exit is not None:
            runner["exitCode"] = case.runner_exit
        containers.append(runner)
    prefix = f"arn:aws:ecs:{_P9_REGION}:{_P9_ACCOUNT}"
    detail: dict[str, object] = {
        "taskArn": f"{prefix}:task/{case.cluster_name}/0123abcd",
        "clusterArn": f"{prefix}:cluster/{case.cluster_name}",
        "taskDefinitionArn": f"{prefix}:task-definition/trikon-verify-runner:3",
        "lastStatus": case.last_status,
        "capacityProviderName": "FARGATE_SPOT",
        "containers": containers,
    }
    if case.group is not None:
        detail["group"] = case.group
    if case.stop_code is not None:
        detail["stopCode"] = case.stop_code
    if case.stopped_at is not None:
        detail["stoppedAt"] = case.stopped_at.isoformat()
    if case.env_placement != "no_overrides":
        name = "runner" if case.env_placement == "runner" else "log-router"
        environment = [{"name": key, "value": value} for key, value in case.env]
        detail["overrides"] = {"containerOverrides": [{"name": name, "environment": environment}]}
    raw = {
        "version": "0",
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": _P9_ACCOUNT,
        "time": case.event_time.isoformat(),
        "region": _P9_REGION,
        "detail": detail,
    }
    return EcsTaskStateChangeEvent.model_validate(json.loads(json.dumps(raw)))


# -- Reference implementation -----------------------------------------------


def _ref_positive_int(raw: str | None) -> int | None:
    """An id as the dispatcher writes it (``str(n)``, n >= 1, at most 19 digits)."""
    if raw is None or not 1 <= len(raw) <= 19 or not set(raw) <= _REF_DIGITS or raw[0] == "0":
        return None
    return int(raw)


def _ref_is_sha(raw: str) -> bool:
    """A 40-character lowercase hex commit SHA."""
    return len(raw) == 40 and set(raw) <= _REF_HEX


def _reference_decision(case: _StopCase) -> StoppedTaskDecision:
    """The design's stopped-task decision table, from the raw inputs of ``case``."""
    if (
        case.last_status != _REF_STOPPED
        or case.cluster_name != _REF_SANDBOX_CLUSTER
        or case.group != _REF_SANDBOX_GROUP
    ):
        return Ignore("not_sandbox_task")
    spot = case.stop_code == _REF_SPOT_STOP_CODE
    runner_exit = case.runner_exit if case.runner_container else None
    if not spot and runner_exit != _REF_INTERIM_EXIT:
        return Ignore("not_interrupted")  # Req 9.6

    # Only the ``runner`` container override carries Runner_Overrides.
    env = dict(case.env) if case.env_placement == "runner" else {}
    installation_id = _ref_positive_int(env.get(_INSTALLATION_ID))
    repo_full_name = env.get(_REPO_FULL_NAME)
    head_sha = env.get(_HEAD_SHA)
    if (
        installation_id is None
        or not repo_full_name
        or head_sha is None
        or not _ref_is_sha(head_sha)
    ):
        return Unroutable()  # Req 11.4: identity unknown
    pr_number = _ref_positive_int(env.get(_PR_NUMBER))
    target = FailClosedTarget(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        pr_number=pr_number,
    )

    base_sha = env.get(_BASE_SHA)
    event_type = env.get(_EVENT_TYPE)
    delivery_id = env.get(_DELIVERY_ID)
    row: SqsJobMessage | None = None
    if (
        pr_number is not None
        and base_sha is not None
        and _ref_is_sha(base_sha)
        and event_type is not None
        and delivery_id is not None
    ):
        stopped = case.stopped_at if case.stopped_at is not None else case.event_time
        row = SqsJobMessage(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            event_type=event_type,
            sent_at=_expected_sent_at(stopped),
            delivery_id=delivery_id,
        )

    if not spot:
        return FailClosed("interim_without_retry", target, row)  # Req 11.3
    attempt = env.get(_ATTEMPT)
    if attempt == "2":
        return FailClosed("retry_exhausted", target, row)  # Req 11.2
    reservation_id = env.get(_RESERVATION_ID)
    if (
        attempt != "1"
        or row is None
        or reservation_id is None
        or _REF_UUID.fullmatch(reservation_id) is None
    ):
        return FailClosed("retry_job_malformed", target, row)  # Req 11.4
    job = VerifyJobMessage(**row.model_dump(), attempt=2, reservation_id=reservation_id)
    return Requeue(job, target, row)  # Req 9.2


def _label(decision: StoppedTaskDecision) -> str:
    """A coverage label for Hypothesis statistics."""
    if isinstance(decision, Requeue):
        return "Requeue"
    return f"{type(decision).__name__}:{decision.reason}"


# Feature: trikon-cloud-cost-cuts, Property 9: Stopped-task decision table
@given(case=_stop_cases())
@settings(max_examples=500, deadline=None)
def test_property_stopped_task_decision_table(case: _StopCase) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 9: Stopped-task decision table.

    For any generated Task_Stopped_Event, varying the sandbox match
    (cluster, group, lastStatus), the stop code, the runner exit code, the
    ``TRIKON_ATTEMPT`` override and every other override field (valid,
    invalid or missing), :func:`decide_stopped_task` equals the reference
    implementation of the design's decision table, target and
    Job_Message included. Deciding the same event twice gives equal
    results.

    **Validates: Requirements 9.6, 11.2, 11.3, 11.4**
    """
    event = _stop_event(case)
    expected = _reference_decision(case)
    hypothesis_event(_label(expected))

    decision = decide_stopped_task(event)

    assert decision == expected
    assert decide_stopped_task(event) == decision
