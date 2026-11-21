# ruff: noqa: N815
# The EventBridge "ECS Task State Change" detail carries ``mixedCase`` field
# names (``taskArn``, ``lastStatus``, ``containerOverrides`` …). The Python
# attribute names match the wire form, as in
# :mod:`trikon_cloud.orchestrator.models`, so ``N815`` is silenced at the file
# level.
#
# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write, so it is
# silenced at the file level, matching :mod:`trikon_cloud.orchestrator.models`.
# mypy: disable-error-code="explicit-any"
"""Pydantic v2 models for the Usage_Reporter (sandbox-minute-plans §10).

* :class:`EcsTaskStateChangeEvent` and its parts: the subset of the
  EventBridge "ECS Task State Change" event the handler reads. AWS sends many
  more keys and adds new ones over time, so every model uses
  ``extra="ignore"``. Timestamps must carry a UTC offset (ECS sends ``Z``) and
  parse to aware datetimes. The Usage_Reporter and the Spot_Retry_Handler
  share these models and the :class:`EcsTaskDetail` helpers
  (:meth:`~EcsTaskDetail.is_stopped_sandbox_task`,
  :meth:`~EcsTaskDetail.runner_env`, :meth:`~EcsTaskDetail.runner_exit_code`).
* :class:`UsageReporterEnvConfig`: the Lambda environment contract, loaded once
  per cold start. A missing or malformed variable raises
  :class:`pydantic.ValidationError` before the first event is handled.
"""

from __future__ import annotations

from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trikon_cloud.usage_client.models import https_base_url

__all__ = [
    "EcsContainer",
    "EcsContainerOverride",
    "EcsEnvVar",
    "EcsOverrides",
    "EcsTaskDetail",
    "EcsTaskStateChangeEvent",
    "UsageReporterEnvConfig",
]

_EVENT_CONFIG = ConfigDict(frozen=True, extra="ignore")

#: ECS ``lastStatus`` of a task that has stopped.
_STOPPED: Final = "STOPPED"

#: Cluster the Orchestrator dispatches Sandbox_Tasks to (``RunTaskCall.cluster``).
_SANDBOX_CLUSTER_NAME: Final = "trikon-verify-cluster"

#: ECS task ``group``: ``RunTask`` without an explicit group uses ``family:<family>``.
_SANDBOX_TASK_GROUP: Final = "family:trikon-verify-runner"

#: The runner container: the Orchestrator's single override and the task's
#: essential container (``ContainerOverride.name``).
_RUNNER_CONTAINER: Final = "runner"


def _cluster_name(cluster_arn: str) -> str:
    """Return the cluster name from ``arn:aws:ecs:<region>:<account>:cluster/<name>``.

    A bare name (no ``/``) is returned unchanged.
    """
    return cluster_arn.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# EventBridge "ECS Task State Change" event.
# ---------------------------------------------------------------------------


class EcsEnvVar(BaseModel):
    """One ``environment`` entry of a container override."""

    model_config = _EVENT_CONFIG

    name: str
    value: str


class EcsContainer(BaseModel):
    """One ``containers`` entry: a container's state at the task state change.

    ``exitCode`` is absent until the container has exited (and for a
    container that never started).
    """

    model_config = _EVENT_CONFIG

    name: str
    exitCode: int | None = None


class EcsContainerOverride(BaseModel):
    """One ``overrides.containerOverrides`` entry.

    The Orchestrator dispatches a single override named ``runner`` whose
    environment carries ``TRIKON_DELIVERY_ID`` (the Run_Id) and
    ``TRIKON_RESERVATION_ID``.
    """

    model_config = _EVENT_CONFIG

    name: str
    environment: tuple[EcsEnvVar, ...] = ()

    def env_value(self, name: str) -> str | None:
        """Return the value of the first environment entry called ``name``, if any."""
        for entry in self.environment:
            if entry.name == name:
                return entry.value
        return None


class EcsOverrides(BaseModel):
    """The task's ``overrides`` object."""

    model_config = _EVENT_CONFIG

    containerOverrides: tuple[EcsContainerOverride, ...] = ()

    def container(self, name: str) -> EcsContainerOverride | None:
        """Return the first container override called ``name``, if any."""
        for override in self.containerOverrides:
            if override.name == name:
                return override
        return None


class EcsTaskDetail(BaseModel):
    """The ``detail`` object: the ECS task description at the state change."""

    model_config = _EVENT_CONFIG

    taskArn: str
    clusterArn: str
    taskDefinitionArn: str | None = None
    group: str | None = None
    lastStatus: str
    stopCode: str | None = None
    createdAt: AwareDatetime | None = None
    pullStartedAt: AwareDatetime | None = None
    stoppedAt: AwareDatetime | None = None
    overrides: EcsOverrides | None = None
    capacityProviderName: str | None = None
    containers: tuple[EcsContainer, ...] = ()

    def is_stopped_sandbox_task(self) -> bool:
        """True for a ``STOPPED`` ``trikon-verify-runner`` task on ``trikon-verify-cluster``."""
        return (
            self.lastStatus == _STOPPED
            and _cluster_name(self.clusterArn) == _SANDBOX_CLUSTER_NAME
            and self.group == _SANDBOX_TASK_GROUP
        )

    def runner_env(self, name: str) -> str | None:
        """Return env entry ``name`` of the ``runner`` container override, if present."""
        if self.overrides is None:
            return None
        runner = self.overrides.container(_RUNNER_CONTAINER)
        if runner is None:
            return None
        return runner.env_value(name)

    def runner_exit_code(self) -> int | None:
        """Return the ``exitCode`` of the first container called ``runner``, if any."""
        for container in self.containers:
            if container.name == _RUNNER_CONTAINER:
                return container.exitCode
        return None


class EcsTaskStateChangeEvent(BaseModel):
    """The EventBridge envelope. ``time`` is the event time (Req 12.4)."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    time: AwareDatetime
    source: str
    detail_type: str = Field(alias="detail-type")
    detail: EcsTaskDetail


# ---------------------------------------------------------------------------
# Lambda environment-variable contract.
# ---------------------------------------------------------------------------


class UsageReporterEnvConfig(BaseSettings):
    """Usage_Reporter Lambda environment variables (design §10, §11).

    ``extra="ignore"`` because pydantic-settings sees every process env var,
    not only these aliases.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # Usage_API base URL (Req 11.15). https only; trailing "/" stripped.
    trikon_web_api_url: str = Field(alias="TRIKON_WEB_API_URL")
    # SSM SecureString parameter name of the Usage_Secret ("/"-rooted).
    trikon_cloud_usage_secret_parameter_name: str = Field(
        alias="TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME", min_length=2, pattern=r"^/"
    )
    trikon_log_level: str = Field(alias="TRIKON_LOG_LEVEL", default="INFO")

    @field_validator("trikon_web_api_url")
    @classmethod
    def _https_base_url(cls, value: str) -> str:
        """Require an https base URL and strip any trailing ``/`` (Req 11.15)."""
        return https_base_url(value)
