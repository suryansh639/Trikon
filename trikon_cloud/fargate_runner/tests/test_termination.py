"""Tests for :mod:`trikon_cloud.fargate_runner.termination`.

Unit tests for the :class:`PublishGuard` transition table (design §6),
:func:`termination_action` and :func:`fail_closed_conclusion`, plus the
``main()`` SIGTERM matrix (Requirement 10.7). Properties 11 and 12
extend this module (tasks 17.4-17.5).
"""

from __future__ import annotations

import ast
import json
import signal
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
import respx
import structlog.contextvars
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws
from pydantic import ValidationError

import trikon.sdk
from trikon_cloud.fargate_runner import entrypoint, git_ops, termination
from trikon_cloud.fargate_runner.dynamodb_writer import DynamoDBWriter
from trikon_cloud.fargate_runner.github_client import GithubClient, GithubClientError
from trikon_cloud.fargate_runner.models import (
    INTERIM_CHECK_RUN_SUMMARY,
    INTERIM_CHECK_RUN_TITLE,
    CheckRunInterimPayload,
    CheckRunOutput,
)
from trikon_cloud.fargate_runner.spot_contract import (
    MAX_ATTEMPTS,
    RUNNER_EXIT_FAIL_CLOSED,
    RUNNER_EXIT_INTERIM,
    RUNNER_EXIT_SUCCESS,
    Capacity,
)
from trikon_cloud.fargate_runner.termination import (
    Phase,
    PublishGuard,
    TerminationRequested,
    fail_closed_conclusion,
    termination_action,
)

from .conftest import (
    APP_PRIVATE_KEY_PARAMETER_NAME,
    APP_PRIVATE_KEY_PEM,
    HEAD_SHA,
    INSTALLATION_ID,
    PR_NUMBER,
    REPO_FULL_NAME,
    make_check_run_response,
    make_comment_response,
    make_env_vars,
    make_installation_token_response,
    make_verdict,
)

_PUBLISHING = (Phase.PUBLISHING_FAIL_CLOSED, Phase.PUBLISHING_INTERIM)


def _guard(phase: Phase, *, requested: bool = False) -> PublishGuard:
    return PublishGuard(phase=phase, termination_requested=requested)


# ---------------------------------------------------------------------------
# Construction and the exception type.
# ---------------------------------------------------------------------------


def test_new_guard_is_running_without_a_request() -> None:
    guard = PublishGuard()
    assert guard.phase is Phase.RUNNING
    assert guard.termination_requested is False


def test_termination_requested_escapes_except_exception() -> None:
    """A BaseException, so ``except Exception`` blocks never swallow it."""
    assert not issubclass(TerminationRequested, Exception)
    with pytest.raises(TerminationRequested):
        try:
            raise TerminationRequested()
        except Exception:  # pragma: no cover - must not be reached
            pytest.fail("TerminationRequested was caught by except Exception")


# ---------------------------------------------------------------------------
# SIGTERM column: flag always; raise only while RUNNING.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "must_raise"),
    [
        (Phase.RUNNING, True),
        (Phase.POSTING_FINAL, False),
        (Phase.FINAL_POSTED, False),
        (Phase.PUBLISHING_FAIL_CLOSED, False),
        (Phase.PUBLISHING_INTERIM, False),
    ],
)
def test_on_sigterm_flags_and_raises_only_while_running(phase: Phase, must_raise: bool) -> None:
    guard = _guard(phase)
    assert guard.on_sigterm() is must_raise
    assert guard.termination_requested is True
    assert guard.phase is phase


# ---------------------------------------------------------------------------
# begin_final_post / final_posted columns.
# ---------------------------------------------------------------------------


def _phase(guard: PublishGuard) -> Phase:
    """Read the phase without mypy keeping an earlier ``is`` narrowing."""
    return guard.phase


def test_final_post_happy_path() -> None:
    guard = PublishGuard()
    guard.begin_final_post()
    assert _phase(guard) is Phase.POSTING_FINAL
    guard.final_posted()
    assert _phase(guard) is Phase.FINAL_POSTED


@pytest.mark.parametrize("phase", [p for p in Phase if p is not Phase.RUNNING])
def test_begin_final_post_outside_running_raises(phase: Phase) -> None:
    guard = _guard(phase)
    with pytest.raises(RuntimeError):
        guard.begin_final_post()
    assert guard.phase is phase


@pytest.mark.parametrize("phase", [p for p in Phase if p is not Phase.POSTING_FINAL])
def test_final_posted_outside_posting_final_raises(phase: Phase) -> None:
    guard = _guard(phase)
    with pytest.raises(RuntimeError):
        guard.final_posted()
    assert guard.phase is phase


# ---------------------------------------------------------------------------
# claim_fail_closed column.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "requested", "granted"),
    [
        (Phase.RUNNING, False, True),
        (Phase.RUNNING, True, True),
        (Phase.POSTING_FINAL, False, True),
        (Phase.POSTING_FINAL, True, True),
        (Phase.FINAL_POSTED, False, True),
        (Phase.FINAL_POSTED, True, False),
        (Phase.PUBLISHING_FAIL_CLOSED, False, False),
        (Phase.PUBLISHING_FAIL_CLOSED, True, False),
        (Phase.PUBLISHING_INTERIM, False, False),
        (Phase.PUBLISHING_INTERIM, True, False),
    ],
)
def test_claim_fail_closed(phase: Phase, requested: bool, granted: bool) -> None:
    guard = _guard(phase, requested=requested)
    assert guard.claim_fail_closed() is granted
    assert guard.phase is (Phase.PUBLISHING_FAIL_CLOSED if granted else phase)
    assert guard.termination_requested is requested


# ---------------------------------------------------------------------------
# claim_interim column.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "requested", "granted"),
    [
        (Phase.RUNNING, False, False),
        (Phase.RUNNING, True, True),
        (Phase.POSTING_FINAL, False, False),
        (Phase.POSTING_FINAL, True, True),
        (Phase.FINAL_POSTED, False, False),
        (Phase.FINAL_POSTED, True, False),
        (Phase.PUBLISHING_FAIL_CLOSED, True, False),
        (Phase.PUBLISHING_INTERIM, True, False),
    ],
)
def test_claim_interim(phase: Phase, requested: bool, granted: bool) -> None:
    guard = _guard(phase, requested=requested)
    assert guard.claim_interim() is granted
    assert guard.phase is (Phase.PUBLISHING_INTERIM if granted else phase)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (PublishGuard.claim_fail_closed, PublishGuard.claim_interim),
        (PublishGuard.claim_interim, PublishGuard.claim_fail_closed),
        (PublishGuard.claim_fail_closed, PublishGuard.claim_fail_closed),
        (PublishGuard.claim_interim, PublishGuard.claim_interim),
    ],
)
def test_only_the_first_claim_wins(
    first: Callable[[PublishGuard], bool], second: Callable[[PublishGuard], bool]
) -> None:
    """Req 10.5: after a SIGTERM, at most one publication is claimed."""
    guard = PublishGuard()
    assert guard.on_sigterm() is True
    assert first(guard) is True
    assert second(guard) is False
    assert guard.phase in _PUBLISHING
    # A second SIGTERM during the publication is flagged and ignored.
    assert guard.on_sigterm() is False


def test_sigterm_during_final_post_is_deferred_then_interim_on_post_failure() -> None:
    """Req 10.4/10.5: a deferred SIGTERM still lets the termination path
    claim when the final post fails."""
    guard = PublishGuard()
    guard.begin_final_post()
    assert guard.on_sigterm() is False
    assert guard.claim_interim() is True


def test_sigterm_after_final_post_publishes_nothing() -> None:
    """Req 10.4: after FINAL_POSTED a requested termination never publishes."""
    guard = PublishGuard()
    guard.begin_final_post()
    guard.final_posted()
    assert guard.on_sigterm() is False
    assert guard.claim_interim() is False
    assert guard.claim_fail_closed() is False
    assert guard.phase is Phase.FINAL_POSTED


# ---------------------------------------------------------------------------
# termination_action and fail_closed_conclusion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "capacity", "expected"),
    [
        (1, "FARGATE_SPOT", "interim"),
        (1, "FARGATE", "fail_closed"),
        (2, "FARGATE", "fail_closed"),
        (2, "FARGATE_SPOT", "fail_closed"),
    ],
)
def test_termination_action(attempt: int, capacity: Capacity, expected: str) -> None:
    assert termination_action(attempt=attempt, capacity=capacity) == expected


@pytest.mark.parametrize(
    ("attempt", "terminated", "expected"),
    [
        (1, False, "neutral"),
        (1, True, "action_required"),
        (2, False, "action_required"),
        (2, True, "action_required"),
    ],
)
def test_fail_closed_conclusion(attempt: int, terminated: bool, expected: str) -> None:
    assert fail_closed_conclusion(attempt=attempt, terminated=terminated) == expected


# ---------------------------------------------------------------------------
# Packaging: the runner image copies only trikon_cloud/fargate_runner/.
# ---------------------------------------------------------------------------


def test_module_imports_only_stdlib_and_the_runner_package() -> None:
    source = Path(termination.__file__).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    allowed_roots = set(sys.stdlib_module_names) | {"__future__"}
    for module in modules:
        assert module.split(".")[0] in allowed_roots or module.startswith(
            "trikon_cloud.fargate_runner."
        ), module


# ===========================================================================
# main() SIGTERM matrix (Requirement 10.1-10.7, design "Testing strategy").
#
# Every cell runs the real :func:`entrypoint.main` against moto (DynamoDB,
# S3, SSM) and respx (GitHub). A stubbed runner step calls the real
# ``entrypoint._on_sigterm(signal.SIGTERM, None)``. ``signal.signal`` and
# ``signal.alarm`` are neutralized as in ``test_entrypoint.py``, so no real
# handler or timer is installed and the cells also run on Windows.
#
# Configurations: Attempt 1 on Spot (interim, exit 75), Attempt 2 (fail
# closed on the Attempt 1 interim, exit 1) and Attempt 1 on-demand (fail
# closed, exit 1). Steps: 2 key fetch, 3 token mint, 4 git, 5 SDK verify,
# 6 pr_state read, 7 verdict write, 9b final post (success or failure),
# 10 comment, 11 pr_state upsert.
# ===========================================================================

_CHECK_RUN_ID = 111
_COMMENT_ID = 222
_PR_KEY = f"{INSTALLATION_ID}#{REPO_FULL_NAME}#{PR_NUMBER}"

_Target = tuple[object, str]


class _Callable(Protocol):
    """Any callable; avoids ``Callable[..., X]`` (explicit Any under mypy)."""

    def __call__(self, *args: object, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class _Config:
    attempt: str
    capacity: Capacity

    @property
    def is_interim(self) -> bool:
        """Attempt 1 on Spot posts the Interim_Check_Run (Req 10.1)."""
        return self.attempt == "1" and self.capacity == "FARGATE_SPOT"

    @property
    def interruption_exit(self) -> int:
        return RUNNER_EXIT_INTERIM if self.is_interim else RUNNER_EXIT_FAIL_CLOSED


_CONFIGS: dict[str, _Config] = {
    "attempt1-spot": _Config(attempt="1", capacity="FARGATE_SPOT"),
    "attempt2": _Config(attempt="2", capacity="FARGATE"),
    "attempt1-on-demand": _Config(attempt="1", capacity="FARGATE"),
}

_CHECK_RUN_WRITES: tuple[_Target, ...] = (
    (GithubClient, "create_check_run"),
    (GithubClient, "patch_check_run"),
)

# SIGTERM before the Final_Check_Run lands: one interruption result.
_INTERRUPTING_STEPS: dict[str, tuple[tuple[_Target, ...], bool]] = {
    "step2-key-fetch": (((entrypoint, "_fetch_app_private_key"),), False),
    "step3-token-mint": (((GithubClient, "mint_installation_token"),), False),
    "step4-git": (((git_ops, "shallow_fetch_and_checkout"),), False),
    "step5-sdk-verify": (((trikon.sdk, "verify"),), False),
    "step6-pr-state-read": (((DynamoDBWriter, "get_pr_state"),), False),
    "step7-verdict-write": (((DynamoDBWriter, "put_verdict"),), False),
    "step9b-final-post-fails": (_CHECK_RUN_WRITES, True),
}

# SIGTERM during a successful final post or after it: the final stays.
_FINAL_KEEPING_STEPS: dict[str, tuple[_Target, ...]] = {
    "step9b-final-post-succeeds": _CHECK_RUN_WRITES,
    "step10-comment": (
        (GithubClient, "create_pr_comment"),
        (GithubClient, "patch_pr_comment"),
    ),
    "step11-pr-state-upsert": ((DynamoDBWriter, "upsert_pr_state"),),
}


@dataclass
class _SigtermAt:
    """Fire the real SIGTERM handler from inside a stubbed runner step.

    Wraps the step's real callable. On the calls numbered in ``fire_on``
    (counted across every wrapped target) it calls
    ``entrypoint._on_sigterm``; with ``fail`` it then raises
    :class:`GithubClientError` to model a failed final post. Every other
    call delegates, so the interim and fail-closed paths reuse the step.
    """

    fire_on: frozenset[int] = frozenset({1})
    fail: bool = False
    calls: int = 0

    def _wrap(self, real: _Callable) -> _Callable:
        def _step(*args: object, **kwargs: object) -> object:
            self.calls += 1
            if self.calls in self.fire_on:
                entrypoint._on_sigterm(signal.SIGTERM, None)
                if self.fail:
                    raise GithubClientError("final Check Run post failed")
            return real(*args, **kwargs)

        return _step

    def install(self, monkeypatch: pytest.MonkeyPatch, targets: tuple[_Target, ...]) -> None:
        for owner, name in targets:
            monkeypatch.setattr(owner, name, self._wrap(getattr(owner, name)))


@pytest.fixture
def runner_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Moto AWS, a stubbed git and SDK, and the ``test_entrypoint`` patches."""
    monkeypatch.setattr("trikon_cloud.fargate_runner.token_cache._CACHE", None)
    monkeypatch.setattr(
        "trikon_cloud.fargate_runner.github_client._sleep_with_jitter",
        lambda *_a, **_kw: None,
    )

    # respx 0.21 + httpx 0.28: decode a bytes method so routes match.
    real_init = cast(_Callable, httpx.Request.__init__)

    def _init(self: httpx.Request, method: object, *args: object, **kwargs: object) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)

    # SIGALRM does not exist on Windows; never install real handlers or timers.
    if not hasattr(signal, "SIGALRM"):
        monkeypatch.setattr(signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(signal, "signal", lambda *_a, **_kw: None)
    monkeypatch.setattr(signal, "alarm", lambda *_a, **_kw: 0, raising=False)

    env = make_env_vars()
    env.update(
        AWS_ACCESS_KEY_ID="testing",
        AWS_SECRET_ACCESS_KEY="testing",
        AWS_SESSION_TOKEN="testing",
        AWS_DEFAULT_REGION="us-east-1",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    # Steps 4 and 5 baseline: no git subprocess, a fixed ``block`` verdict.
    verdict = make_verdict(decision="block")
    monkeypatch.setattr(git_ops, "shallow_fetch_and_checkout", lambda **_kw: None)
    monkeypatch.setattr(trikon.sdk, "verify", lambda **_kw: verdict)

    structlog.contextvars.clear_contextvars()
    with mock_aws():
        _provision_aws()
        yield
    structlog.contextvars.clear_contextvars()


def _provision_aws() -> None:
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
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
    dynamodb.create_table(
        TableName="trikon_pr_state",
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="trikon-cloud-evidence")
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name=APP_PRIVATE_KEY_PARAMETER_NAME,
        Value=APP_PRIVATE_KEY_PEM.decode("utf-8"),
        Type="SecureString",
    )


def _apply_config(monkeypatch: pytest.MonkeyPatch, config: _Config) -> None:
    """Set the Attempt and Capacity; Attempt 2 finds Attempt 1's interim."""
    monkeypatch.setenv("TRIKON_ATTEMPT", config.attempt)
    monkeypatch.setenv("TRIKON_CAPACITY", config.capacity)
    if config.attempt == "2":
        boto3.client("dynamodb", region_name="us-east-1").put_item(
            TableName="trikon_pr_state",
            Item={
                "pr_key": {"S": _PR_KEY},
                "last_head_sha": {"S": HEAD_SHA},
                "last_updated_at": {"S": "2026-01-01T00:00:00.000Z"},
                "last_check_run_id": {"N": str(_CHECK_RUN_ID)},
                "last_comment_id": {"NULL": True},
            },
        )


def _mock_github(router: respx.MockRouter) -> dict[str, respx.Route]:
    def _ok(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(200, json=body)

    repo = REPO_FULL_NAME
    return {
        "mint": router.post(f"/app/installations/{INSTALLATION_ID}/access_tokens").mock(
            return_value=_ok(make_installation_token_response())
        ),
        "create_check_run": router.post(f"/repos/{repo}/check-runs").mock(
            return_value=_ok(make_check_run_response(_CHECK_RUN_ID))
        ),
        "patch_check_run": router.patch(f"/repos/{repo}/check-runs/{_CHECK_RUN_ID}").mock(
            return_value=_ok(make_check_run_response(_CHECK_RUN_ID))
        ),
        "create_comment": router.post(f"/repos/{repo}/issues/{PR_NUMBER}/comments").mock(
            return_value=_ok(make_comment_response(_COMMENT_ID))
        ),
        "patch_comment": router.patch(f"/repos/{repo}/issues/comments/{_COMMENT_ID}").mock(
            return_value=_ok(make_comment_response(_COMMENT_ID))
        ),
    }


def _run_main() -> tuple[int, dict[str, respx.Route]]:
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        routes = _mock_github(router)
        exit_code = entrypoint.main()
    return exit_code, routes


def _check_run_writes(routes: dict[str, respx.Route]) -> list[tuple[str, dict[str, object]]]:
    """Every Check Run request that reached GitHub, as ``(method, body)``."""
    writes: list[tuple[str, dict[str, object]]] = []
    for name in ("create_check_run", "patch_check_run"):
        for call in routes[name].calls:
            writes.append((call.request.method, json.loads(call.request.content)))
    return writes


def _assert_one_interruption_result(
    config: _Config, exit_code: int, routes: dict[str, respx.Route]
) -> None:
    """Req 10.1-10.3, 10.5, 10.6: exactly one interim or fail-closed run."""
    writes = _check_run_writes(routes)
    assert len(writes) == 1, writes
    method, body = writes[0]
    assert exit_code == config.interruption_exit
    if config.is_interim:
        # Req 10.2: in_progress with no conclusion, on this head SHA.
        assert method == "POST"
        assert body["name"] == "Trikon"
        assert body["head_sha"] == HEAD_SHA
        assert body["status"] == "in_progress"
        assert "conclusion" not in body
        item = boto3.client("dynamodb", region_name="us-east-1").get_item(
            TableName="trikon_pr_state", Key={"pr_key": {"S": _PR_KEY}}
        )["Item"]
        assert item["last_check_run_id"]["N"] == str(_CHECK_RUN_ID)
        assert item["last_head_sha"]["S"] == HEAD_SHA
        assert entrypoint._GUARD.phase is Phase.PUBLISHING_INTERIM
    else:
        # Req 10.3, 11.7: action_required. Attempt 2 completes its interim.
        assert method == ("PATCH" if config.attempt == "2" else "POST")
        assert body["status"] == "completed"
        assert body["conclusion"] == "action_required"
        assert entrypoint._GUARD.phase is Phase.PUBLISHING_FAIL_CLOSED
    assert routes["create_comment"].call_count == 0
    assert routes["patch_comment"].call_count == 0
    assert entrypoint._GUARD.termination_requested is True


@pytest.mark.usefixtures("runner_harness")
@pytest.mark.parametrize("config_id", list(_CONFIGS))
@pytest.mark.parametrize("step", list(_INTERRUPTING_STEPS))
def test_sigterm_before_final_post_publishes_one_interruption_result(
    step: str, config_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 10.1, 10.2, 10.3, 10.5, 10.6, 10.7.

    Steps 2-7 raise out of the step; a SIGTERM during a final post that
    then fails is deferred and handled once the post raises.
    """
    config = _CONFIGS[config_id]
    _apply_config(monkeypatch, config)
    targets, fail = _INTERRUPTING_STEPS[step]
    hook = _SigtermAt(fail=fail)
    hook.install(monkeypatch, targets)

    exit_code, routes = _run_main()

    assert hook.calls >= 1, "the stubbed step never ran"
    _assert_one_interruption_result(config, exit_code, routes)


@pytest.mark.usefixtures("runner_harness")
@pytest.mark.parametrize("config_id", list(_CONFIGS))
@pytest.mark.parametrize("step", list(_FINAL_KEEPING_STEPS))
def test_sigterm_during_or_after_final_post_leaves_the_final_unchanged(
    step: str, config_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 10.4, 10.7: the Final_Check_Run is the only Check Run, exit 0."""
    config = _CONFIGS[config_id]
    _apply_config(monkeypatch, config)
    hook = _SigtermAt()
    hook.install(monkeypatch, _FINAL_KEEPING_STEPS[step])

    exit_code, routes = _run_main()

    assert hook.calls >= 1, "the stubbed step never ran"
    assert exit_code == RUNNER_EXIT_SUCCESS
    writes = _check_run_writes(routes)
    assert len(writes) == 1, writes
    method, body = writes[0]
    # Attempt 2 PATCHes the interim to its final; Attempt 1 creates it.
    assert method == ("PATCH" if config.attempt == "2" else "POST")
    assert body["status"] == "completed"
    assert body["conclusion"] == "failure"  # the stubbed ``block`` verdict
    assert routes["create_comment"].call_count == 1
    assert entrypoint._GUARD.phase is Phase.FINAL_POSTED
    assert entrypoint._GUARD.termination_requested is True


@pytest.mark.usefixtures("runner_harness")
@pytest.mark.parametrize("config_id", list(_CONFIGS))
@pytest.mark.parametrize("window", ["before-claim", "during-publication"])
def test_second_sigterm_adds_no_publication(
    window: str, config_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req 10.5: a repeat SIGTERM is ignored; one result is published.

    The first SIGTERM lands at step 5. The second lands either inside
    ``_handle_termination`` before the guard claims (via
    ``termination_action``) or during the interim or fail-closed post.
    """
    config = _CONFIGS[config_id]
    _apply_config(monkeypatch, config)
    first = _SigtermAt()
    first.install(monkeypatch, ((trikon.sdk, "verify"),))
    second = _SigtermAt()
    second_targets: tuple[_Target, ...] = (
        ((termination, "termination_action"),) if window == "before-claim" else _CHECK_RUN_WRITES
    )
    second.install(monkeypatch, second_targets)

    exit_code, routes = _run_main()

    assert first.calls == 1
    assert second.calls >= 1, "the second SIGTERM never fired"
    _assert_one_interruption_result(config, exit_code, routes)


# ===========================================================================
# Property 11: Runner publishes at most one interruption result.
#
# A pure reference model of ``entrypoint.main``'s control flow drives the
# real :class:`PublishGuard` and the real ``entrypoint._on_sigterm``. The
# model has no I/O: a "step" is a delivery point followed by an optional
# ordinary exception, and a "publication" is a granted claim. Delivery
# points are numbered in execution order, so a schedule of point numbers
# places one or more SIGTERMs anywhere: inside a step, during or after the
# final post, inside the ``except`` handler, before a claim and during a
# publication. The 17.3 matrix covers the same flow against the real
# ``main``; this explores the interleavings it cannot enumerate.
# ===========================================================================

_MAX_PRE_STEPS = 6  # steps 2-9a, before ``begin_final_post``
_MAX_POST_STEPS = 3  # steps 10-12, after ``final_posted``
_MAX_POINT = 24  # above the longest path's point count

_Kind = Literal["interim", "fail_closed"]


class _StepFailedError(Exception):
    """An ordinary exception from a runner step or the final post."""


@dataclass(frozen=True)
class _Schedule:
    attempt: int
    capacity: Capacity
    pre_steps: int
    pre_fail_at: int | None
    final_post_ok: bool
    post_steps: int
    post_fail_at: int | None
    sigterms: tuple[int, ...]  # delivery point of each SIGTERM; repeats allowed


@dataclass(frozen=True)
class _Delivery:
    point: int
    phase: Phase  # guard phase when the signal arrived
    asked_to_raise: bool  # ``PublishGuard.on_sigterm``'s answer
    raised: bool  # ``entrypoint._on_sigterm`` raised TerminationRequested


@dataclass(frozen=True)
class _Publication:
    kind: _Kind
    terminated: bool  # ``_fail_closed(terminated=...)``; False for interim
    requested_at_claim: bool


@dataclass
class _RecordingGuard(PublishGuard):
    """The real guard, recording each ``on_sigterm`` answer."""

    answers: list[tuple[Phase, bool]] | None = None

    def on_sigterm(self) -> bool:
        phase = self.phase
        answer = super().on_sigterm()
        if self.answers is not None:
            self.answers.append((phase, answer))
        return answer


class _MainModel:
    """Reference model of ``main`` / ``_run_steps`` / ``_handle_termination``
    / ``_post_interim`` / ``_fail_closed`` (design §6), minus all I/O."""

    def __init__(self, schedule: _Schedule) -> None:
        self.schedule = schedule
        self.answers: list[tuple[Phase, bool]] = []
        self.guard = _RecordingGuard(answers=self.answers)
        self.point = 0
        self.deliveries: list[_Delivery] = []
        self.publications: list[_Publication] = []
        self.final_posts = 0
        self.final_posted_at: int | None = None  # first point after FINAL_POSTED
        self.first_failure_at: int | None = None  # first point after the raise

    # -- primitives ----------------------------------------------------------

    def _point(self) -> None:
        """A place where the scheduled SIGTERMs for this point arrive."""
        here = self.point
        self.point += 1
        for _ in range(self.schedule.sigterms.count(here)):
            phase = self.guard.phase
            answered = len(self.answers)
            raised = False
            try:
                entrypoint._on_sigterm(signal.SIGTERM, None)
            except TerminationRequested:
                raised = True
            asked = self.answers[answered][1]
            self.deliveries.append(_Delivery(here, phase, asked, raised))
            if raised:
                raise TerminationRequested()

    def _fail(self) -> None:
        if self.first_failure_at is None:
            self.first_failure_at = self.point
        raise _StepFailedError()

    def _step(self, index: int, fail_at: int | None) -> None:
        self._point()
        if index == fail_at:
            self._fail()

    # -- the control flow ----------------------------------------------------

    def main(self) -> int:
        try:
            return self._run_steps()
        except TerminationRequested:
            return self._handle_termination()

    def _run_steps(self) -> int:
        s = self.schedule
        try:
            for i in range(s.pre_steps):
                self._step(i, s.pre_fail_at)
            self._point()  # step 9b, just before the guard transition
            self.guard.begin_final_post()
            self._point()  # during the Check Run create / PATCH
            if not s.final_post_ok:
                self._fail()
            self.final_posts += 1
            self._point()  # the post returned; ``final_posted`` not yet called
            self.guard.final_posted()
            self.final_posted_at = self.point
            for j in range(s.post_steps):
                self._step(j, s.post_fail_at)
            self._point()  # step 12, before ``return 0``
            return RUNNER_EXIT_SUCCESS
        except Exception:
            self._point()  # inside the catchall, before the flag check
            if self.guard.termination_requested:
                return self._handle_termination()
            return self._fail_closed(terminated=False)

    def _handle_termination(self) -> int:
        self._point()  # ``termination_action`` and the warning log
        action = termination_action(attempt=self.schedule.attempt, capacity=self.schedule.capacity)
        if action == "interim":
            return self._post_interim()
        return self._fail_closed(terminated=True)

    def _post_interim(self) -> int:
        self._point()  # before the claim
        requested = self.guard.termination_requested
        if not self.guard.claim_interim():
            if self.guard.phase is Phase.FINAL_POSTED:
                return RUNNER_EXIT_SUCCESS
            return RUNNER_EXIT_FAIL_CLOSED
        self.publications.append(_Publication("interim", False, requested))
        self._point()  # during the interim post and the pr_state write
        return RUNNER_EXIT_INTERIM

    def _fail_closed(self, *, terminated: bool) -> int:
        self._point()  # before the claim
        requested = self.guard.termination_requested
        if not self.guard.claim_fail_closed():
            if self.guard.phase is Phase.FINAL_POSTED:
                return RUNNER_EXIT_SUCCESS
            return RUNNER_EXIT_FAIL_CLOSED
        self.publications.append(_Publication("fail_closed", terminated, requested))
        self._point()  # during the fail-closed write and post
        return RUNNER_EXIT_FAIL_CLOSED


@st.composite
def _schedules(draw: st.DrawFn) -> _Schedule:
    """Attempt, Capacity, step outcomes and SIGTERM points (0-4, repeats allowed)."""
    pre_steps = draw(st.integers(min_value=0, max_value=_MAX_PRE_STEPS))
    post_steps = draw(st.integers(min_value=0, max_value=_MAX_POST_STEPS))
    pre_fail_at = (
        draw(st.none() | st.integers(min_value=0, max_value=pre_steps - 1)) if pre_steps else None
    )
    post_fail_at = (
        draw(st.none() | st.integers(min_value=0, max_value=post_steps - 1))
        if post_steps
        else None
    )
    return _Schedule(
        attempt=draw(st.sampled_from([1, 2])),
        capacity=draw(st.sampled_from(cast(list[Capacity], ["FARGATE", "FARGATE_SPOT"]))),
        pre_steps=pre_steps,
        pre_fail_at=pre_fail_at,
        final_post_ok=draw(st.booleans()),
        post_steps=post_steps,
        post_fail_at=post_fail_at,
        sigterms=tuple(
            draw(st.lists(st.integers(min_value=0, max_value=_MAX_POINT), max_size=4))
        ),
    )


_EXIT_BY_PUBLICATION: dict[_Kind | None, int] = {
    "interim": RUNNER_EXIT_INTERIM,
    "fail_closed": RUNNER_EXIT_FAIL_CLOSED,
    None: RUNNER_EXIT_SUCCESS,  # no interruption result: the final post completed
}


# Feature: trikon-cloud-cost-cuts, Property 11: Runner publishes at most one interruption result
@given(schedule=_schedules())
@settings(max_examples=500, deadline=None)
def test_property_runner_publishes_at_most_one_interruption_result(schedule: _Schedule) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 11: Runner publishes at most one interruption result.

    **Validates: Requirements 10.1, 10.3, 10.4, 10.5, 10.6**
    """
    model = _MainModel(schedule)
    saved_guard = entrypoint._GUARD
    entrypoint._GUARD = model.guard  # what ``main`` does; ``_on_sigterm`` reads it
    try:
        exit_code = model.main()
    finally:
        entrypoint._GUARD = saved_guard

    interim_task = schedule.attempt == 1 and schedule.capacity == "FARGATE_SPOT"
    publications = model.publications
    final_posted = model.final_posted_at is not None

    # Req 10.5: at most one of PUBLISHING_INTERIM and PUBLISHING_FAIL_CLOSED.
    assert len(publications) <= 1, publications
    publication = publications[0] if publications else None
    if publication is not None:
        assert model.guard.phase is (
            Phase.PUBLISHING_INTERIM
            if publication.kind == "interim"
            else Phase.PUBLISHING_FAIL_CLOSED
        )

    # The final post is never duplicated.
    assert model.final_posts <= 1

    # ``on_sigterm`` asks to raise only in RUNNING; the handler raises only
    # for the first SIGTERM (a repeat never escapes ``main``).
    for index, delivery in enumerate(model.deliveries):
        assert delivery.asked_to_raise is (delivery.phase is Phase.RUNNING), delivery
        assert delivery.raised is (delivery.asked_to_raise and index == 0), delivery

    # Req 10.4: after FINAL_POSTED a requested termination never publishes.
    # Only the unchanged non-SIGTERM post-final exception path may fail closed.
    if final_posted and publication is not None:
        assert publication.kind == "fail_closed"
        assert not publication.requested_at_claim
        assert not publication.terminated

    # Req 10.1: the interim needs Attempt 1 on Spot and a SIGTERM before FINAL_POSTED.
    if publication is not None and publication.kind == "interim":
        assert interim_task
        assert not final_posted
        assert publication.requested_at_claim
    # Req 10.3: the SIGTERM fail-closed result is only for Attempt 2 or on-demand.
    if publication is not None and publication.terminated:
        assert not interim_task

    # Req 10.1, 10.3: a SIGTERM while the run is healthy and before the
    # Final_Check_Run lands publishes exactly the configuration's result.
    first = model.deliveries[0] if model.deliveries else None
    healthy = first is not None and (
        model.first_failure_at is None or first.point < model.first_failure_at
    )
    if healthy and not final_posted:
        assert publication is not None
        assert publication.kind == ("interim" if interim_task else "fail_closed")
        assert publication.terminated is (publication.kind == "fail_closed")

    # Without any SIGTERM the existing behaviour is unchanged.
    if not model.deliveries:
        failed = model.first_failure_at is not None
        assert (publication is not None) is failed
        if publication is not None:
            assert publication.kind == "fail_closed"
            assert not publication.terminated

    # Req 10.6: the exit code identifies the outcome, and the three
    # constants are pairwise distinct.
    assert len({RUNNER_EXIT_INTERIM, RUNNER_EXIT_FAIL_CLOSED, RUNNER_EXIT_SUCCESS}) == 3
    if publication is None:
        assert final_posted  # nothing published only when the final post completed
    expected_exit = _EXIT_BY_PUBLICATION[publication.kind if publication else None]
    assert exit_code == expected_exit, (schedule, model.deliveries, publications)


# ===========================================================================
# Property 12: Spot-related Check Runs never pass (runner side).
#
# The runner's Spot-related Check Run bodies are the Interim_Check_Run
# (:class:`CheckRunInterimPayload`, sent by ``GithubClient.create_check_run``
# as ``model_dump_json()``) and the Fail_Closed_Result, whose conclusion is
# :func:`fail_closed_conclusion`. The Spot_Retry_Handler and Orchestrator
# notices are covered in ``orchestrator/tests/test_quota_notices.py``.
# ===========================================================================

# Conclusions GitHub counts as passing for a required check.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# Every Check Run conclusion GitHub accepts.
_CHECK_RUN_CONCLUSIONS = (
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "success",
    "skipped",
    "stale",
    "timed_out",
)
# Real 40-hex SHAs plus arbitrary text (no lone surrogates, which JSON
# serialization cannot encode and GitHub never sends).
_HEAD_SHAS = st.one_of(
    st.text(alphabet="0123456789abcdef", min_size=40, max_size=40),
    st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=64),
)


# Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass
@given(attempt=st.integers(min_value=1, max_value=MAX_ATTEMPTS), terminated=st.booleans())
@settings(max_examples=100, deadline=None)
def test_property_spot_fail_closed_conclusion_never_passes(attempt: int, terminated: bool) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass.

    A Runner Fail_Closed_Result after a SIGTERM or on the last Attempt is
    ``action_required``. Only the unchanged Attempt 1, non-SIGTERM path
    keeps ``neutral``.

    **Validates: Requirements 10.2, 11.7**
    """
    conclusion = fail_closed_conclusion(attempt=attempt, terminated=terminated)
    if terminated or attempt == MAX_ATTEMPTS:
        assert conclusion == "action_required"
        assert conclusion not in _PASSING_CONCLUSIONS
    else:
        assert attempt == 1
        assert not terminated
        assert conclusion == "neutral"


# Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass
@given(head_sha=_HEAD_SHAS, conclusion=st.sampled_from(_CHECK_RUN_CONCLUSIONS))
@settings(max_examples=200, deadline=None)
def test_property_interim_check_run_has_no_conclusion(head_sha: str, conclusion: str) -> None:
    """Feature: trikon-cloud-cost-cuts, Property 12: Spot-related Check Runs never pass.

    For any head SHA the serialized Interim_Check_Run is ``in_progress``
    with no ``conclusion`` key, and the model rejects a ``conclusion``
    or a ``completed`` status.

    **Validates: Requirements 10.2, 11.7**
    """
    payload = CheckRunInterimPayload(
        name="Trikon",
        head_sha=head_sha,
        status="in_progress",
        output=CheckRunOutput(title=INTERIM_CHECK_RUN_TITLE, summary=INTERIM_CHECK_RUN_SUMMARY),
    )
    body: dict[str, object] = json.loads(payload.model_dump_json())
    assert body["name"] == "Trikon"
    assert body["head_sha"] == head_sha
    assert body["status"] == "in_progress"
    assert "conclusion" not in body

    # The body round-trips, so each rejection below is due to the one change.
    assert CheckRunInterimPayload.model_validate(body) == payload
    with pytest.raises(ValidationError):
        CheckRunInterimPayload.model_validate({**body, "conclusion": conclusion})
    with pytest.raises(ValidationError):
        CheckRunInterimPayload.model_validate({**body, "status": "completed"})
