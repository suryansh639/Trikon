# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write — silence it
# at the file level.
# mypy: disable-error-code="explicit-any"
"""Pydantic v2 models for the Trikon Cloud installation-lifecycle Lambda.

Three public models capturing the boundaries the Lifecycle_Handler touches:

* :class:`InstallationEventMessage` — the SQS body written to
  ``trikon-cloud-installation-events`` by Spec 1's webhook receiver
  (design.md §3.2). Internal wire contract: ``extra="forbid"`` and
  ``frozen=True`` so any shape drift from Spec 1 fails loudly at parse
  rather than silently no-op'ing (Requirement 11.1, 11.2).
* :class:`LifecycleTableRow` — the ``trikon-cloud-installations``
  DynamoDB row shape (design.md §2.2). Frozen so a parsed row cannot
  be mutated in place; the writer emits a fresh row on every update.
* :class:`LifecycleEnvConfig` — Lambda environment-variable contract
  (design.md §2.3). Loaded at cold start via
  :class:`pydantic_settings.BaseSettings`; a missing / malformed
  variable raises :class:`pydantic.ValidationError` at process start,
  before any SQS message is consumed.

See ``.kiro/specs/trikon-cloud-orchestrator/design.md`` §2.2, §2.3, and
§3.2 for the authoritative field definitions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ordering mirrors design.md §3.2 → §2.2 → §2.3, not isort alphabetical.
__all__ = ["InstallationEventMessage", "LifecycleTableRow", "LifecycleEnvConfig"]  # noqa: RUF022


# ---------------------------------------------------------------------------
# §3.2 — SQS body for trikon-cloud-installation-events.
# ---------------------------------------------------------------------------


class InstallationEventMessage(BaseModel):
    """SQS body written to ``trikon-cloud-installation-events`` (design.md §3.2).

    Written by Spec 1's webhook receiver (after its Requirement 12
    amendment). All fields required, no defaults. ``extra="forbid"``:
    unknown keys are a validation error, not a silent no-op — a shape
    drift from Spec 1 must fail loudly at parse (Requirement 11.1).

    ``repositories`` is ``tuple[str, ...]`` (not ``list[str]``) to keep
    the model hashable and frozen. Each entry is an ``owner/repo``
    string. On ``installation.created`` this is the initial repo set;
    on ``installation_repositories.added`` / ``.removed`` it is the
    delta the Lifecycle_Handler applies to the row's set attribute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: int = Field(ge=1)
    github_app_id: int = Field(ge=1)
    event_type: Literal[
        "installation.created",
        "installation.deleted",
        "installation_repositories.added",
        "installation_repositories.removed",
    ]
    repositories: tuple[str, ...]
    sent_at: str
    delivery_id: str


# ---------------------------------------------------------------------------
# §2.2 — trikon-cloud-installations row shape.
# ---------------------------------------------------------------------------


class LifecycleTableRow(BaseModel):
    """One row of the ``trikon-cloud-installations`` DynamoDB table (design.md §2.2).

    ``status`` is typed ``Literal["active", "disabled"]`` so the two-state
    lifecycle is enforced at the type level: no third state is
    representable. ``repositories`` is ``frozenset[str]`` so the row is
    hashable and the ``DynamoDB SS`` (string-set) attribute has an
    order-independent, duplicate-free Python representation (design.md
    §5.4 — DynamoDB set semantics are idempotent, and ``frozenset``
    mirrors that at the domain layer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    installation_id: int
    status: Literal["active", "disabled"]
    github_app_id: int
    created_at: str
    updated_at: str
    repositories: frozenset[str]


# ---------------------------------------------------------------------------
# §2.3 — Lifecycle_Handler environment-variable contract.
# ---------------------------------------------------------------------------


class LifecycleEnvConfig(BaseSettings):
    """Lambda environment variables (design.md §2.3).

    Loaded at cold start. Missing / malformed variables raise
    :class:`pydantic.ValidationError` before any SQS message is
    consumed — a misconfigured Lifecycle_Handler MUST NOT dequeue
    events it cannot process.

    ``TRIKON_AWS_ACCOUNT_ID`` and ``TRIKON_APP_PRIVATE_KEY_SECRET_ARN``
    are baked into the runtime IAM policy the Runtime_IAM_Renderer
    emits (see ``iam_template.py``); the other three carry defaults
    so local test construction only needs the two required values set.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    trikon_aws_account_id: str = Field(alias="TRIKON_AWS_ACCOUNT_ID")
    trikon_app_private_key_secret_arn: str = Field(
        alias="TRIKON_APP_PRIVATE_KEY_SECRET_ARN"
    )
    trikon_installations_table: str = Field(
        default="trikon-cloud-installations", alias="TRIKON_INSTALLATIONS_TABLE"
    )
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    trikon_log_level: str = Field(default="INFO", alias="TRIKON_LOG_LEVEL")
