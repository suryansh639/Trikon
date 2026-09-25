# ruff: noqa: N815
# The AWS wire shapes we validate here — ``ecs.RunTask`` responses and
# ``ssm.GetParameter`` responses — carry ``PascalCase`` / ``mixedCase``
# field names (``Parameter``, ``Value``, ``taskArn``). We keep the
# Python attribute names identical to the wire form so ``model_validate``
# can consume the raw boto3 response dict without an alias table, and so
# call sites read the wire field names verbatim per design §4.3.
# Ruff's ``N815`` (mixedCase variable in class scope) is silenced at the
# file level to match :mod:`trikon_cloud.orchestrator.models`.
#
# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every BaseModel subclass. Under the repo's ``disallow_any_explicit =
# true`` mypy config, each class definition surfaces as an
# ``explicit-any`` error. The error refers to code the plugin generates,
# not code we write — silence at the file level, matching
# :mod:`trikon_cloud.orchestrator.models` and
# :mod:`trikon_cloud.fargate_runner.dynamodb_writer`.
# mypy: disable-error-code="explicit-any"
"""ECS dispatcher for the Trikon Cloud orchestrator (Spec 3, §4).

Four concern groups mirror design.md's §4 layout:

* :func:`build_run_task_call` — pure, IO-free. Builds the exact
  ``ecs.RunTask`` request body prescribed by design §3.3 from a
  validated :class:`SqsJobMessage`, the loaded
  :class:`OrchestratorEnvConfig`, and the resolver-supplied
  ``family:revision`` string. The seven ``EnvOverride`` entries and the
  three ``EcsTag`` entries are emitted in fixed order — Requirements
  5.2 and 3.5.
* :func:`submit_run_task` — the single side-effect. One
  ``ecs.RunTask`` call per invocation (Requirement 3.1). ``ClientError``
  propagates unhandled so the handler can classify it.
* :func:`classify_client_error` — sorts a botocore ``ClientError`` into
  Transient or Terminal per the frozensets in design §4.2. Unknown
  codes classify as Transient (Requirement 6.4 fail-safe).
* :class:`TaskDefinitionResolver` — reads
  ``/trikon/verify-runner/active-revision`` from SSM Parameter Store
  once per container and returns ``trikon-verify-runner:<revision>``.
  Never emits ``:LATEST`` (Requirement 4.2). Instance-scope cache;
  container lifetime is the TTL (design §4.3).

The two ``*Protocol`` classes are structural types over the boto3
subsets we call — ``run_task`` and ``get_parameter``. ``boto3`` ships
untyped; the protocols let ``mypy --strict`` type-check the call sites
without a runtime dependency on ``boto3-stubs``.
"""

from __future__ import annotations

from typing import Literal, Protocol

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from trikon_cloud.orchestrator.models import (
    AwsvpcConfiguration,
    ContainerOverride,
    EcsTag,
    EnvOverride,
    NetworkConfiguration,
    OrchestratorEnvConfig,
    RunTaskCall,
    RunTaskOverrides,
)
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# Ordering mirrors the task's declared ``__all__`` list — pure builder
# first, then dispatch, then classifier, then resolver, then supporting
# types. Not isort alphabetical.
__all__ = [  # noqa: RUF022
    "build_run_task_call",
    "submit_run_task",
    "classify_client_error",
    "TaskDefinitionResolver",
    "RunTaskDispatchResult",
    "RunTaskResponse",
    "SsmGetParameterResponse",
    "EcsClientProtocol",
    "SsmClientProtocol",
    "TransientDispatchError",
    "TerminalDispatchError",
]


# ---------------------------------------------------------------------------
# Dispatch-outcome exceptions (design §4.1 STEP 7).
# ---------------------------------------------------------------------------


class TransientDispatchError(Exception):
    """Raised by the handler on a Transient ``ecs.RunTask`` failure.

    The handler wraps :func:`submit_run_task` in a try/except; if
    :func:`classify_client_error` returns ``"transient"`` it re-raises
    as ``TransientDispatchError`` so the Lambda runtime marks the batch
    as a failure and SQS returns the message for redrive
    (Requirement 6.1, design §4.1 STEP 7).
    """


class TerminalDispatchError(Exception):
    """Raised on a Terminal ``ecs.RunTask`` failure the caller opts to surface.

    The default handler path does NOT raise this — it invokes the
    Never-Fail-Open verdict writer inline and returns success so SQS
    deletes the message (Requirement 7.1). The exception is exported so
    higher-level callers (e.g. integration tests, alternate compositions)
    that prefer an exception-driven Terminal branch have a named type
    to catch on.
    """


# ---------------------------------------------------------------------------
# Structural protocols for the boto3 subsets we call.
# ---------------------------------------------------------------------------


class EcsClientProtocol(Protocol):
    """Structural type for the subset of boto3 ECS client we call.

    :func:`submit_run_task` invokes ``ecs_client.run_task(**kwargs)``
    where ``kwargs`` is produced by
    ``RunTaskCall.model_dump(by_alias=True, mode="json")``. The keyword
    surface is therefore the entire ``ecs.RunTask`` API — enumerating
    each parameter here would duplicate the wire schema encoded on
    :class:`RunTaskCall`. The return value is validated by
    :class:`RunTaskResponse` at the call site, so this Protocol types
    it as :class:`object` (never :data:`typing.Any`).
    """

    def run_task(self, **kwargs: object) -> object:
        ...


class SsmClientProtocol(Protocol):
    """Structural type for the subset of boto3 SSM client we call.

    :meth:`TaskDefinitionResolver.resolve` invokes
    ``ssm_client.get_parameter(Name=<param>)`` once per cold start.
    ``Name`` is PascalCase because boto3's ``ssm.GetParameter`` wire
    kwarg is PascalCase — ``noqa: N803`` silences ruff's
    lowercase-argument rule at the single call site.
    """

    def get_parameter(self, *, Name: str) -> object:  # noqa: N803
        ...


# ---------------------------------------------------------------------------
# AWS response models — ``extra="allow"`` per the task contract.
# ---------------------------------------------------------------------------


class _RunTaskTask(BaseModel):
    """One entry of ``RunTaskResponse.tasks`` — carries ``taskArn``.

    Internal helper — not re-exported. ``extra="allow"`` because the
    boto3 response carries dozens of fields the dispatcher does not read
    (``lastStatus``, ``attachments``, ``containers`` …); we only need
    ``taskArn`` for the dispatch-result log line (Requirement 9.4).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    taskArn: str


class RunTaskResponse(BaseModel):
    """Validated view over the ``ecs.RunTask`` response body.

    ``extra="allow"`` so AWS response drift is non-breaking — new
    top-level fields (``failures``, ``ResponseMetadata`` …) neither
    reject validation nor force schema changes here. The single field
    the dispatcher reads is ``tasks[0].taskArn`` per design §4.1 STEP 8.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    tasks: tuple[_RunTaskTask, ...]


class _SsmParameter(BaseModel):
    """The ``Parameter`` sub-object of an ``ssm.GetParameter`` response.

    Internal helper — not re-exported. ``extra="allow"`` because the
    boto3 response carries ``ARN``, ``LastModifiedDate``, ``Version``,
    ``Type``, etc. The resolver reads only ``Value`` (design §4.3).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    Value: str


class SsmGetParameterResponse(BaseModel):
    """Validated view over the ``ssm.GetParameter`` response body.

    ``extra="allow"`` for AWS response-drift safety. The resolver reads
    ``Parameter.Value``, parses it as ``int``, and refuses values ``<1``
    per design §4.3.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    Parameter: _SsmParameter


# ---------------------------------------------------------------------------
# Dispatch result — the value :func:`submit_run_task` returns.
# ---------------------------------------------------------------------------


class RunTaskDispatchResult(BaseModel):
    """Success result of :func:`submit_run_task`.

    Frozen. Carries only the fully qualified task ARN emitted on the
    ``run_task_dispatched`` log line (design §4.1 STEP 8, Requirement
    9.4). Additional fields would either duplicate information already
    in log context (``dispatched_task_arn``) or leak boto3 response
    payload into the handler — Invariant 6 forbids the latter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_arn: str


# ---------------------------------------------------------------------------
# §4.2 — Error classification.
# ---------------------------------------------------------------------------


_TRANSIENT_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalFailure",
    }
)


_TERMINAL_CODES: frozenset[str] = frozenset(
    {
        "InvalidParameterException",
        "AccessDeniedException",
        "ClusterNotFoundException",
        "TaskDefinitionNotFound",
        "NoSuchEntity",
    }
)


def classify_client_error(err: ClientError) -> Literal["transient", "terminal"]:
    """Sort a botocore ``ClientError`` into Transient vs Terminal (design §4.2).

    Reads ``err.response["Error"]["Code"]`` defensively via ``.get()``
    — malformed error responses (empty ``Error`` object, missing
    ``Code`` key) never raise :class:`KeyError` from this classifier.

    Classification rules per Requirement 6.4:

    * Code ∈ ``_TERMINAL_CODES`` → ``"terminal"``. The handler writes a
      synthetic ``require_human`` verdict and returns success.
    * Code ∈ ``_TRANSIENT_CODES`` → ``"transient"``. The handler raises
      :class:`TransientDispatchError` and the message returns to SQS.
    * Unknown code → ``"transient"`` (fail-safe default). Better to
      re-drive an operator-unrecognised error than to prematurely
      terminal-fail a job whose failure class we have not yet catalogued.
    """
    code = err.response.get("Error", {}).get("Code", "")
    if code in _TERMINAL_CODES:
        return "terminal"
    if code in _TRANSIENT_CODES:
        return "transient"
    # Fail-safe default (Requirement 6.4).
    return "transient"


# ---------------------------------------------------------------------------
# §3.3 — Pure ``RunTaskCall`` builder.
# ---------------------------------------------------------------------------


def build_run_task_call(
    message: SqsJobMessage,
    *,
    env: OrchestratorEnvConfig,
    task_definition: str,
) -> RunTaskCall:
    """Build the ``ecs.RunTask`` request body prescribed by design §3.3.

    Pure — no IO, no clock reads, no environment reads beyond the
    already-loaded ``env``. Every :class:`RunTaskCall` field is
    populated to the concrete-payload example in design §3.3:

    * ``cluster``, ``launchType``, ``count`` — pinned literals.
    * ``taskDefinition`` — the resolver-supplied
      ``trikon-verify-runner:<revision>`` (Requirement 4.1).
    * ``networkConfiguration.awsvpcConfiguration`` — subnets and
      security groups from :attr:`OrchestratorEnvConfig`,
      ``assignPublicIp="DISABLED"`` (Requirement 3.3).
    * ``overrides.taskRoleArn`` — the per-installation role ARN
      formatted per Requirement 3.4:
      ``arn:aws:iam::{account_id}:role/trikon-verify-task-role-{installation_id}``.
    * ``overrides.containerOverrides`` — exactly one entry
      (``name="runner"``, Requirement 5.1) whose ``environment`` is a
      seven-tuple in the fixed order pinned by Requirement 5.2:
      ``TRIKON_INSTALLATION_ID``, ``TRIKON_REPO_FULL_NAME``,
      ``TRIKON_PR_NUMBER``, ``TRIKON_HEAD_SHA``, ``TRIKON_BASE_SHA``,
      ``TRIKON_EVENT_TYPE``, ``TRIKON_DELIVERY_ID``. Integer fields
      serialize via :func:`str`; string fields are copied byte-for-byte
      from :class:`SqsJobMessage` — no normalization (Requirement 5.4).
    * ``tags`` — a 3-tuple in the fixed order ``installation_id``,
      ``repo``, ``pr`` (Requirement 3.5).

    Args:
        message: Validated inbound SQS job message. All seven of its
            fields are consumed.
        env: Cold-start-loaded orchestrator environment config.
            Supplies the AWS account id, subnet ids, and security-group
            ids.
        task_definition: Fully qualified ``family:revision`` string
            returned by :meth:`TaskDefinitionResolver.resolve`. Never
            contains ``:LATEST`` (Requirement 4.2).

    Returns:
        A frozen :class:`RunTaskCall` whose
        ``.model_dump(by_alias=True, mode="json")`` is the exact
        boto3 kwargs dict.
    """
    task_role_arn = (
        f"arn:aws:iam::{env.trikon_aws_account_id}"
        f":role/trikon-verify-task-role-{message.installation_id}"
    )
    container_environment: tuple[EnvOverride, ...] = (
        EnvOverride(name="TRIKON_INSTALLATION_ID", value=str(message.installation_id)),
        EnvOverride(name="TRIKON_REPO_FULL_NAME", value=message.repo_full_name),
        EnvOverride(name="TRIKON_PR_NUMBER", value=str(message.pr_number)),
        EnvOverride(name="TRIKON_HEAD_SHA", value=message.head_sha),
        EnvOverride(name="TRIKON_BASE_SHA", value=message.base_sha),
        EnvOverride(name="TRIKON_EVENT_TYPE", value=message.event_type),
        EnvOverride(name="TRIKON_DELIVERY_ID", value=message.delivery_id),
    )
    return RunTaskCall(
        cluster="trikon-verify-cluster",
        taskDefinition=task_definition,
        launchType="FARGATE",
        count=1,
        networkConfiguration=NetworkConfiguration(
            awsvpcConfiguration=AwsvpcConfiguration(
                subnets=env.trikon_runner_subnet_ids,
                securityGroups=env.trikon_runner_security_group_ids,
                assignPublicIp="DISABLED",
            ),
        ),
        overrides=RunTaskOverrides(
            taskRoleArn=task_role_arn,
            containerOverrides=(
                ContainerOverride(name="runner", environment=container_environment),
            ),
        ),
        tags=(
            EcsTag(key="installation_id", value=str(message.installation_id)),
            EcsTag(key="repo", value=message.repo_full_name),
            EcsTag(key="pr", value=str(message.pr_number)),
        ),
    )


# ---------------------------------------------------------------------------
# §4.1 STEP 7 — Dispatch.
# ---------------------------------------------------------------------------


def submit_run_task(
    call: RunTaskCall,
    *,
    ecs_client: EcsClientProtocol,
) -> RunTaskDispatchResult:
    """Invoke ``ecs.RunTask`` exactly once (Requirement 3.1).

    Serializes the :class:`RunTaskCall` via
    ``model_dump(by_alias=True, mode="json")`` so field names match the
    boto3 wire form (``taskDefinition``, ``networkConfiguration``,
    ``awsvpcConfiguration`` …) and tuples serialize to JSON arrays that
    boto3 accepts as lists. Validates the response through
    :class:`RunTaskResponse` — ``extra="allow"`` absorbs response drift
    — and returns the first task ARN.

    ``botocore.exceptions.ClientError`` from ``run_task`` propagates
    unhandled. The handler catches it, classifies via
    :func:`classify_client_error`, and drives the Transient/Terminal
    branches per design §4.1 STEP 7.

    Args:
        call: The prepared :class:`RunTaskCall` from
            :func:`build_run_task_call`.
        ecs_client: A structurally typed boto3 ECS client. Only
            :meth:`EcsClientProtocol.run_task` is invoked.

    Returns:
        :class:`RunTaskDispatchResult` carrying the dispatched task's
        ARN — logged on the ``run_task_dispatched`` INFO record
        (Requirement 9.4).
    """
    response = ecs_client.run_task(**call.model_dump(by_alias=True, mode="json"))
    parsed = RunTaskResponse.model_validate(response)
    return RunTaskDispatchResult(task_arn=parsed.tasks[0].taskArn)


# ---------------------------------------------------------------------------
# §4.3 — Task-definition revision resolver.
# ---------------------------------------------------------------------------


class TaskDefinitionResolver:
    """Resolves the active ``trikon-verify-runner`` revision at cold start.

    Reads ``/trikon/verify-runner/active-revision`` (or whichever param
    name :class:`OrchestratorEnvConfig` supplies) from SSM Parameter
    Store on first call, parses the value as an ``int``, and returns
    ``f"trikon-verify-runner:{revision}"``.

    **TTL is the container lifetime, with no explicit refresh** (design
    §4.3). The resolved value is cached on the instance in
    :attr:`_cache` so subsequent warm invocations skip the SSM call
    entirely — this both eliminates the per-invocation ~5 ms
    GetParameter latency and avoids requiring ``ssm:GetParameter`` on
    the hot path IAM grants beyond cold start. A Lambda container
    lives ~15 min idle at most on AWS, so an operator SSM update
    propagates within one natural rotation cycle.

    Requirement 4.2 forbids ``:LATEST`` as a task-definition suffix —
    ``:LATEST`` is neither an SSM value the resolver accepts nor a
    string it can emit, so the guard is that the value must parse as
    an integer ``>= 1``.
    """

    def __init__(self, *, env: OrchestratorEnvConfig) -> None:
        """Bind the SSM parameter name from the env config.

        No IO here — the SSM read happens lazily on first
        :meth:`resolve` call.
        """
        self._param_name: str = env.trikon_verify_runner_active_revision_ssm_param
        # Instance-scope cache — one per :class:`TaskDefinitionResolver`.
        # A single module-scope resolver is constructed by the handler at
        # cold start, giving container-lifetime cache semantics without
        # module-level mutable state.
        self._cache: str | None = None

    def resolve(self, *, ssm_client: SsmClientProtocol) -> str:
        """Return the cached ``family:revision`` string, reading SSM on miss.

        First call: ``ssm.GetParameter`` on the configured parameter
        name; parse ``Parameter.Value`` as ``int``; raise
        :class:`ValueError` if ``< 1``; format
        ``f"trikon-verify-runner:{revision}"``; store on
        :attr:`_cache`; return. Subsequent calls: return the cache.

        Args:
            ssm_client: A structurally typed boto3 SSM client. Only
                :meth:`SsmClientProtocol.get_parameter` is invoked.

        Returns:
            The fully qualified task-definition string
            ``trikon-verify-runner:<revision>``. Never contains
            ``:LATEST`` (Requirement 4.2).

        Raises:
            ValueError: If the SSM parameter value parses as an integer
                ``< 1``. The handler treats this as a cold-start crash
                (design §4.5) — the Lambda infra returns the message
                to the queue for a later retry against a corrected
                SSM value.
            botocore.exceptions.ClientError: Propagated from
                :meth:`get_parameter`. Terminal at handler cold start.
        """
        if self._cache is not None:
            return self._cache
        response = ssm_client.get_parameter(Name=self._param_name)
        parsed = SsmGetParameterResponse.model_validate(response)
        revision = int(parsed.Parameter.Value)
        if revision < 1:
            raise ValueError(f"invalid revision from SSM: {revision}")
        self._cache = f"trikon-verify-runner:{revision}"
        return self._cache
