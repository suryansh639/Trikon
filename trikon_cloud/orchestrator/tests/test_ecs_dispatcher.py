# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin generates,
# not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Unit + property tests for :mod:`trikon_cloud.orchestrator.ecs_dispatcher`.

Covers the four concern groups declared in the sibling module (design.md
§3.3, §4.2, §4.3):

* :func:`build_run_task_call` — the pure, IO-free builder. Tests
  exercise the six shape invariants from design.md §10.4 Property 4:
  the seven ``EnvOverride`` entries appear in the fixed order
  (``TRIKON_INSTALLATION_ID`` first, ``TRIKON_DELIVERY_ID`` last), the
  two integer :class:`SqsJobMessage` fields (``installation_id``,
  ``pr_number``) are stringified via :func:`str`, ``taskRoleArn`` is
  ``arn:aws:iam::{account}:role/trikon-verify-task-role-{installation_id}``
  per Requirement 3.4, and the three tags are ordered
  ``installation_id``, ``repo``, ``pr`` per Requirement 3.5.
* :func:`submit_run_task` — the single side-effect. Tests use a
  hand-rolled :class:`EcsClientProtocol` implementation to capture
  the exact kwargs boto3 receives, assert ``run_task`` is invoked
  exactly once (Requirement 3.1), assert the returned
  :class:`RunTaskDispatchResult` carries ``response.tasks[0].taskArn``
  verbatim, and assert :class:`ClientError` propagates unhandled — the
  handler classifies, not the dispatcher.
* :func:`classify_client_error` — the Transient/Terminal sort. Tests
  parametrize over every code in ``_TERMINAL_CODES`` and every code
  in ``_TRANSIENT_CODES``, assert unknown codes classify as
  ``"transient"`` (Requirement 6.4 fail-safe default), and drive a
  hypothesis-backed totality check over 20 arbitrary error-code
  strings (Property 6).
* :class:`TaskDefinitionResolver` — the SSM Parameter Store reader
  with container-lifetime cache. Tests assert first-call SSM fetch
  returns ``trikon-verify-runner:<revision>``, second-call reuses the
  cache with zero additional SSM invocations (Property 5), values
  ``< 1`` raise :class:`ValueError`, and the emitted string never
  contains ``:LATEST`` (Requirement 4.2).
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.orchestrator.ecs_dispatcher import (
    RunTaskDispatchResult,
    TaskDefinitionResolver,
    build_run_task_call,
    classify_client_error,
    submit_run_task,
)
from trikon_cloud.orchestrator.models import (
    EnvOverride,
    OrchestratorEnvConfig,
    RunTaskCall,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    make_sqs_job_message,
)

# ---------------------------------------------------------------------------
# Hand-rolled Protocol implementations — spy on kwargs, return canned
# responses. Match the structural signatures declared on
# :class:`EcsClientProtocol` and :class:`SsmClientProtocol` in
# ``ecs_dispatcher.py`` exactly so mypy accepts them at the call sites.
# ---------------------------------------------------------------------------


class _RecordingEcsClient:
    """Spy implementing :class:`EcsClientProtocol`.

    Captures every ``run_task`` invocation's kwargs on :attr:`calls`.
    Optionally raises the exception supplied via ``raise_exc`` — used
    to drive the :class:`ClientError` propagation test — otherwise
    returns the canned response dict (default: a single-task response
    boto3 shape).
    """

    def __init__(
        self,
        *,
        response: object | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._response: object = (
            response
            if response is not None
            else {
                "tasks": [
                    {
                        "taskArn": (
                            "arn:aws:ecs:us-east-1:000000000000:task/"
                            "trikon-verify-cluster/abcdef0123456789"
                        )
                    }
                ]
            }
        )
        self._raise_exc: BaseException | None = raise_exc

    def run_task(self, **kwargs: object) -> object:
        """Record kwargs then either raise or return the canned response."""
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class _RecordingSsmClient:
    """Spy implementing :class:`SsmClientProtocol`.

    Records every ``Name`` argument passed to ``get_parameter`` on
    :attr:`calls`. Returns a canned ``ssm.GetParameter`` response
    shape whose ``Parameter.Value`` is the string supplied at
    construction — the resolver parses this as :class:`int`.
    """

    def __init__(self, *, value: str) -> None:
        self.calls: list[str] = []
        self._value: str = value

    def get_parameter(self, *, Name: str) -> object:  # noqa: N803
        """Record the parameter name and return the canned response."""
        self.calls.append(Name)
        return {"Parameter": {"Value": self._value}}


def _client_error(code: str) -> ClientError:
    """Build a :class:`ClientError` carrying the given error code.

    Constructs the ``response`` dict with the ``Error.Code`` shape
    :func:`classify_client_error` reads through
    ``err.response.get("Error", {}).get("Code", "")``. ``operation_name``
    is fixed at ``"RunTask"`` for readability — the classifier does not
    inspect it.
    """
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "RunTask")


# ---------------------------------------------------------------------------
# §3.3 — Pure ``build_run_task_call`` builder (Property 4).
# ---------------------------------------------------------------------------


def test_build_run_task_call_produces_valid_run_task_call() -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    The builder returns a :class:`RunTaskCall` with every top-level
    field pinned per design.md §3.3: ``cluster``, ``launchType``, and
    ``count`` at their :data:`Literal` values, ``taskDefinition`` at
    the resolver-supplied ``family:revision`` string, and the network
    configuration populated from :class:`OrchestratorEnvConfig`.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()

    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )

    assert isinstance(call, RunTaskCall)
    assert call.cluster == "trikon-verify-cluster"
    assert call.launchType == "FARGATE"
    assert call.count == 1
    assert call.taskDefinition == "trikon-verify-runner:17"
    awsvpc = call.networkConfiguration.awsvpcConfiguration
    assert awsvpc.subnets == env.trikon_runner_subnet_ids
    assert awsvpc.securityGroups == env.trikon_runner_security_group_ids
    assert awsvpc.assignPublicIp == "DISABLED"


def test_build_run_task_call_env_overrides_in_fixed_order_with_correct_values() -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    Requirement 5.2 pins the seven ``EnvOverride`` entries at a fixed
    declaration order. ``TRIKON_INSTALLATION_ID`` is first,
    ``TRIKON_DELIVERY_ID`` is last, and the five entries between them
    appear in the exact sequence prescribed by design.md §3.3. Also
    Requirement 5.4: string fields are copied byte-for-byte from
    :class:`SqsJobMessage` — no normalization.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()

    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )
    container_overrides = call.overrides.containerOverrides
    assert len(container_overrides) == 1  # Requirement 5.1.
    assert container_overrides[0].name == "runner"

    environment: tuple[EnvOverride, ...] = container_overrides[0].environment
    assert len(environment) == 7  # Requirement 5.2 — exactly seven entries.

    expected: list[tuple[str, str]] = [
        ("TRIKON_INSTALLATION_ID", str(CANONICAL_INSTALLATION_ID)),
        ("TRIKON_REPO_FULL_NAME", CANONICAL_REPO_FULL_NAME),
        ("TRIKON_PR_NUMBER", str(CANONICAL_PR_NUMBER)),
        ("TRIKON_HEAD_SHA", CANONICAL_HEAD_SHA),
        ("TRIKON_BASE_SHA", CANONICAL_BASE_SHA),
        ("TRIKON_EVENT_TYPE", CANONICAL_EVENT_TYPE),
        ("TRIKON_DELIVERY_ID", CANONICAL_DELIVERY_ID),
    ]
    actual: list[tuple[str, str]] = [(entry.name, entry.value) for entry in environment]
    assert actual == expected

    # Sentinel checks pinning the two boundary entries — the doc is
    # explicit about ``TRIKON_INSTALLATION_ID`` first and
    # ``TRIKON_DELIVERY_ID`` last.
    assert environment[0].name == "TRIKON_INSTALLATION_ID"
    assert environment[-1].name == "TRIKON_DELIVERY_ID"


def test_build_run_task_call_integer_fields_stringified_via_str() -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    Requirement 5.3 pins the coercion of the two integer
    :class:`SqsJobMessage` fields (``installation_id``, ``pr_number``)
    to their :func:`str` representation on the ``EnvOverride`` values
    and on the ``tags`` values — ECS env-var and tag values are ``str``
    on the wire. Uses a non-canonical pair (``7``, ``123``) so the
    stringification is observable as ``"7"`` and ``"123"`` rather than
    coincidentally matching the canonical constants.
    """
    message = make_sqs_job_message(installation_id=7, pr_number=123)
    env = OrchestratorEnvConfig()

    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:1"
    )
    env_by_name: dict[str, str] = {
        entry.name: entry.value
        for entry in call.overrides.containerOverrides[0].environment
    }
    assert env_by_name["TRIKON_INSTALLATION_ID"] == "7"
    assert env_by_name["TRIKON_PR_NUMBER"] == "123"
    tags_by_key: dict[str, str] = {tag.key: tag.value for tag in call.tags}
    assert tags_by_key["installation_id"] == "7"
    assert tags_by_key["pr"] == "123"


def test_build_run_task_call_task_role_arn_matches_requirement_3_4_format() -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    Requirement 3.4 fixes the per-installation task role ARN at
    ``arn:aws:iam::{account_id}:role/trikon-verify-task-role-{installation_id}``
    — the Lifecycle_Handler provisions this role at
    ``installation.created`` and this dispatcher references it by
    exact-form ARN. The account id comes from ``env``, the
    installation id from the SQS message.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()

    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )

    expected = (
        f"arn:aws:iam::{CANONICAL_ACCOUNT_ID}"
        f":role/trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
    )
    assert call.overrides.taskRoleArn == expected


def test_build_run_task_call_tags_ordered_installation_repo_pr() -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    Requirement 3.5 fixes the ``tags`` array at exactly three entries
    in the order ``installation_id``, ``repo``, ``pr``. The
    fixed-length ``tuple[EcsTag, EcsTag, EcsTag]`` annotation on
    :class:`RunTaskCall` guarantees the length; this test guarantees
    the order and values.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()

    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )

    assert len(call.tags) == 3
    assert call.tags[0].key == "installation_id"
    assert call.tags[0].value == str(CANONICAL_INSTALLATION_ID)
    assert call.tags[1].key == "repo"
    assert call.tags[1].value == CANONICAL_REPO_FULL_NAME
    assert call.tags[2].key == "pr"
    assert call.tags[2].value == str(CANONICAL_PR_NUMBER)


# ---------------------------------------------------------------------------
# §4.1 STEP 7 — Dispatch via ``submit_run_task``.
# ---------------------------------------------------------------------------


def test_submit_run_task_invokes_run_task_exactly_once_with_dumped_kwargs() -> None:
    """``submit_run_task`` calls ``ecs.run_task`` exactly once (Requirement 3.1).

    The kwargs pass-through must match the
    :meth:`RunTaskCall.model_dump` output byte-for-byte —
    ``by_alias=True`` preserves the wire-form field names
    (``taskDefinition``, ``networkConfiguration``,
    ``awsvpcConfiguration``) and ``mode="json"`` degrades tuples to
    JSON lists so boto3 accepts the shape.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()
    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )
    ecs_client = _RecordingEcsClient()

    submit_run_task(call, ecs_client=ecs_client)

    assert len(ecs_client.calls) == 1
    dumped = call.model_dump(by_alias=True, mode="json")
    assert ecs_client.calls[0] == dumped
    # Sentinel checks on the top-level wire keys.
    assert ecs_client.calls[0]["cluster"] == "trikon-verify-cluster"
    assert ecs_client.calls[0]["taskDefinition"] == "trikon-verify-runner:17"
    assert ecs_client.calls[0]["launchType"] == "FARGATE"


def test_submit_run_task_returns_dispatch_result_with_first_task_arn() -> None:
    """``submit_run_task`` returns ``RunTaskDispatchResult(task_arn=tasks[0].taskArn)``.

    Design §4.1 STEP 8 logs ``run_task_dispatched`` with the returned
    ``task_arn`` (Requirement 9.4). The dispatcher reads only the
    first task's ARN — ``count=1`` guarantees the boto3 response's
    ``tasks`` array carries exactly one entry.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()
    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )
    task_arn = "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/deadbeef"
    ecs_client = _RecordingEcsClient(response={"tasks": [{"taskArn": task_arn}]})

    result = submit_run_task(call, ecs_client=ecs_client)

    assert isinstance(result, RunTaskDispatchResult)
    assert result.task_arn == task_arn


def test_submit_run_task_propagates_client_error_unhandled() -> None:
    """A :class:`ClientError` from ``ecs.run_task`` propagates unhandled.

    Design §4.1 STEP 7 puts classification and Never-Fail-Open at the
    handler layer, not the dispatcher. The dispatcher's contract is
    "one call, one result-or-exception" — the handler wraps the call
    in try/except and drives Transient/Terminal branches from there.
    """
    message = make_sqs_job_message()
    env = OrchestratorEnvConfig()
    call = build_run_task_call(
        message, env=env, task_definition="trikon-verify-runner:17"
    )
    ecs_client = _RecordingEcsClient(
        raise_exc=_client_error("TaskDefinitionNotFound")
    )

    with pytest.raises(ClientError):
        submit_run_task(call, ecs_client=ecs_client)

    # The call still ran — the exception happens after run_task is invoked.
    assert len(ecs_client.calls) == 1


# ---------------------------------------------------------------------------
# §4.2 — Error classification (Property 6).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_code",
    [
        "InvalidParameterException",
        "AccessDeniedException",
        "ClusterNotFoundException",
        "TaskDefinitionNotFound",
        "NoSuchEntity",
    ],
)
def test_classify_client_error_terminal_codes_return_terminal(
    terminal_code: str,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification.

    Each entry in ``_TERMINAL_CODES`` classifies as ``"terminal"``.
    Design §4.2 defines Terminal codes as those where redrive would
    not change the outcome — the handler writes a
    ``require_human`` verdict and returns success.
    """
    assert classify_client_error(_client_error(terminal_code)) == "terminal"


@pytest.mark.parametrize(
    "transient_code",
    [
        "ThrottlingException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalFailure",
    ],
)
def test_classify_client_error_transient_codes_return_transient(
    transient_code: str,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification.

    Each entry in ``_TRANSIENT_CODES`` classifies as ``"transient"``.
    Design §4.2 defines Transient codes as those where redrive is
    likely to succeed — the handler raises
    :class:`TransientDispatchError` and the message returns to SQS
    for redrive.
    """
    assert classify_client_error(_client_error(transient_code)) == "transient"


@pytest.mark.parametrize(
    "unknown_code",
    ["UnknownFutureAwsError", "SomeNewCode", "", "InvalidClientTokenId"],
)
def test_classify_client_error_unknown_code_returns_transient_fail_safe(
    unknown_code: str,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification.

    Requirement 6.4 pins the fail-safe default at ``"transient"`` for
    codes not in either enumeration — better to redrive an unrecognised
    error than to prematurely terminal-fail a job whose failure class
    we have not yet catalogued. Includes an empty-string case exercising
    the ``.get("Code", "")`` defensive default in the classifier.
    """
    assert classify_client_error(_client_error(unknown_code)) == "transient"


@given(code=st.text(min_size=0, max_size=64))
@settings(max_examples=20, deadline=None)
def test_classify_client_error_totality_over_arbitrary_codes(code: str) -> None:
    """Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification.

    Totality: :func:`classify_client_error` returns one of
    ``{"transient", "terminal"}`` for every arbitrary error-code
    string — no :class:`KeyError`, no unhandled exception, no
    third return value. Hypothesis draws 20 arbitrary strings
    (including the empty string, unicode, and near-collisions with
    real AWS codes).
    """
    result = classify_client_error(_client_error(code))
    assert result in ("transient", "terminal")


# ---------------------------------------------------------------------------
# §4.3 — Task-definition revision resolver (Property 5).
# ---------------------------------------------------------------------------


def test_task_definition_resolver_first_call_fetches_from_ssm_and_pins_revision() -> None:
    """Feature: trikon-cloud-orchestrator, Property 5: task-definition pinned + SSM one-time-fetch cache.

    First call: :meth:`TaskDefinitionResolver.resolve` invokes
    :meth:`SsmClientProtocol.get_parameter` on the configured
    parameter name, parses ``Parameter.Value`` as :class:`int`,
    and returns ``f"trikon-verify-runner:{revision}"`` — the fully
    qualified ``family:revision`` string boto3's
    ``ecs.RunTask`` accepts (Requirement 4.1).
    """
    env = OrchestratorEnvConfig()
    resolver = TaskDefinitionResolver(env=env)
    ssm_client = _RecordingSsmClient(value="17")

    result = resolver.resolve(ssm_client=ssm_client)

    assert result == "trikon-verify-runner:17"
    assert ssm_client.calls == [env.trikon_verify_runner_active_revision_ssm_param]


def test_task_definition_resolver_second_call_returns_cached_value() -> None:
    """Feature: trikon-cloud-orchestrator, Property 5: task-definition pinned + SSM one-time-fetch cache.

    Second call: the resolver's instance-scope cache returns the
    prior result without invoking :meth:`get_parameter` again. Design
    §4.3 sets container lifetime as the TTL; a single cold-start SSM
    read amortizes across every warm invocation on the same Lambda
    container.
    """
    env = OrchestratorEnvConfig()
    resolver = TaskDefinitionResolver(env=env)
    ssm_client = _RecordingSsmClient(value="17")

    first = resolver.resolve(ssm_client=ssm_client)
    second = resolver.resolve(ssm_client=ssm_client)

    assert first == second == "trikon-verify-runner:17"
    # Exactly one SSM invocation across two resolver calls — Property 5.
    assert len(ssm_client.calls) == 1


@pytest.mark.parametrize("bad_revision", ["0", "-1", "-100"])
def test_task_definition_resolver_rejects_revision_below_one(
    bad_revision: str,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 5: task-definition pinned + SSM one-time-fetch cache.

    Values that parse as ``int`` but are ``< 1`` raise
    :class:`ValueError`. Design §4.3 treats these as cold-start
    misconfiguration — the handler propagates the failure so the
    Lambda infra returns the message to the queue for a later retry
    against a corrected SSM value. ECS task-definition revisions are
    always ``>= 1``.
    """
    env = OrchestratorEnvConfig()
    resolver = TaskDefinitionResolver(env=env)
    ssm_client = _RecordingSsmClient(value=bad_revision)

    with pytest.raises(ValueError, match="invalid revision"):
        resolver.resolve(ssm_client=ssm_client)


@pytest.mark.parametrize("revision", ["1", "17", "100", "999999"])
def test_task_definition_resolver_never_emits_latest_suffix(revision: str) -> None:
    """Feature: trikon-cloud-orchestrator, Property 5: task-definition pinned + SSM one-time-fetch cache.

    Requirement 4.2 forbids ``:LATEST`` as a task-definition suffix
    — the resolver's parse-as-int step makes emitting ``:LATEST``
    structurally impossible, and this test locks that guarantee in
    place across a range of well-formed integer revisions.
    """
    env = OrchestratorEnvConfig()
    resolver = TaskDefinitionResolver(env=env)
    ssm_client = _RecordingSsmClient(value=revision)

    result = resolver.resolve(ssm_client=ssm_client)

    assert ":LATEST" not in result
    assert result == f"trikon-verify-runner:{revision}"
