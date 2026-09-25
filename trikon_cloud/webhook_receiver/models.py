# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write — silence it
# at the file level.
# mypy: disable-error-code="explicit-any"
"""Pydantic v2 models for the Trikon Cloud webhook receiver.

Three public models capturing the boundaries this Lambda touches:

* :class:`GithubWebhookPayload` — subset of the GitHub webhook body the
  receiver extracts (design.md §5.1). All nested models carry
  ``ConfigDict(extra="allow")`` so unknown GitHub fields do not reject
  validation — GitHub adds fields over time and the receiver must not
  fail on schema drift.
* :class:`SqsJobMessage` — the SQS body written to ``trikon-verify-jobs``
  (design.md §5.2). All fields required; no ``extra="allow"`` because the
  message shape is our schema, not GitHub's.
* :class:`ReceiverEnvConfig` — Lambda environment-variable contract
  (design.md §5.3). Loaded at cold start via
  :class:`pydantic_settings.BaseSettings`; a missing / malformed variable
  raises :class:`pydantic.ValidationError` at process start.

See ``.kiro/specs/trikon-cloud-webhook-receiver/design.md`` §5 for the
authoritative field definitions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

# Ordering mirrors design.md §5.1 → §5.2 → §5.3, not isort alphabetical.
__all__ = [  # noqa: RUF022
    "GithubWebhookPayload",
    "SqsJobMessage",
    "GithubInstallationPayload",
    "GithubInstallationRef",
    "GithubInstallationRepository",
    "ReceiverEnvConfig",
]


# ---------------------------------------------------------------------------
# §5.1 — GitHub webhook payload subset.
# ---------------------------------------------------------------------------


class InstallationRef(BaseModel):
    """``payload.installation`` — the GitHub App installation reference."""

    model_config = ConfigDict(extra="allow")

    id: int


class RepositoryRef(BaseModel):
    """``payload.repository`` — the target repository reference."""

    model_config = ConfigDict(extra="allow")

    full_name: str
    default_branch: str


class CommitRef(BaseModel):
    """A commit reference (used for ``pull_request.head`` and ``base``).

    ``sha`` is validated as a lowercase 40-char hex string — GitHub's
    invariant, echoed onto the SQS body via
    :class:`SqsJobMessage.head_sha` / ``base_sha``.
    """

    model_config = ConfigDict(extra="allow")

    sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")


class PullRequestRef(BaseModel):
    """``payload.pull_request`` — the target pull request."""

    model_config = ConfigDict(extra="allow")

    number: int = Field(ge=1)
    head: CommitRef
    base: CommitRef


class SenderRef(BaseModel):
    """``payload.sender`` — the GitHub user that triggered the event."""

    model_config = ConfigDict(extra="allow")

    login: str


class GithubWebhookPayload(BaseModel):
    """Top-level GitHub webhook body subset (design.md §5.1).

    ``extra="allow"`` on this model and every nested model lets GitHub
    add fields over time without breaking validation. The five fields
    below are the only ones the receiver reads.
    """

    model_config = ConfigDict(extra="allow")

    action: str
    installation: InstallationRef
    repository: RepositoryRef
    pull_request: PullRequestRef
    sender: SenderRef


# ---------------------------------------------------------------------------
# §5.2 — SQS job message body.
# ---------------------------------------------------------------------------


class SqsJobMessage(BaseModel):
    """SQS body written to ``trikon-verify-jobs`` (design.md §5.2).

    All fields required. No ``extra="allow"`` — the message shape is our
    internal contract with Spec 3 (the orchestrator), not GitHub's. Field
    declaration order is significant: ``model_dump_json()`` preserves
    declaration order and Requirement 5.3 fixes the on-the-wire order to
    match this class's declaration order verbatim.
    """

    installation_id: int = Field(ge=1)
    repo_full_name: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    base_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    event_type: str
    sent_at: str
    delivery_id: str


# ---------------------------------------------------------------------------
# Amendment §3.1 / §3.2 / §3.3 — GitHub installation-lifecycle payload subset.
# ---------------------------------------------------------------------------
#
# Three models covering ``X-GitHub-Event: installation`` and
# ``X-GitHub-Event: installation_repositories`` deliveries. All three carry
# ``frozen=True`` to match the immutability discipline of Spec 3's
# ``InstallationEventMessage`` (the SQS wire model these payloads are mapped
# to by ``handler._build_installation_message``). ``extra="allow"`` lets
# GitHub add fields over time without breaking validation.


class GithubInstallationRepository(BaseModel):
    """One repository reference inside an installation event payload.

    Corresponds to entries of ``payload.repositories`` (installation.created)
    and ``payload.repositories_added`` / ``payload.repositories_removed``
    (installation_repositories.added / .removed). Only ``full_name`` is
    consumed by the receiver; ``extra="allow"`` lets GitHub add fields
    over time without failing validation.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    full_name: str


class GithubInstallationRef(BaseModel):
    """The ``payload.installation`` reference on installation events.

    Present on both ``installation`` and ``installation_repositories``
    event types. Both integers are consumed unchanged and echoed onto
    :class:`InstallationEventMessage`. ``ge=1`` mirrors the wire model's
    constraint so a zero or negative value fails at the ingress boundary
    (HTTP 400) rather than landing in Spec 3's DLQ.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: int = Field(ge=1)
    app_id: int = Field(ge=1)


class GithubInstallationPayload(BaseModel):
    """Top-level GitHub payload for installation lifecycle events.

    Covers both ``X-GitHub-Event: installation`` (actions ``created``,
    ``deleted``, plus other actions GitHub sends that we drop) and
    ``X-GitHub-Event: installation_repositories`` (actions ``added``,
    ``removed``, plus others we drop).

    Field selection semantics (implemented by
    :func:`trikon_cloud.webhook_receiver.handler._build_installation_message`):

    * ``installation.created`` → read ``repositories``.
    * ``installation.deleted`` → emit ``()`` regardless of payload
      contents (Requirement 2.6).
    * ``installation_repositories.added`` → read ``repositories_added``.
    * ``installation_repositories.removed`` → read ``repositories_removed``.

    The three list fields are declared **individually** rather than as a
    discriminated union so the parse succeeds on any well-formed payload
    — the (event_type, action) → list-field selection happens later in
    ``_build_installation_message``. ``tuple[..., ...] | None`` (rather
    than ``list[...] | None``) preserves hashability under
    ``frozen=True``; Pydantic v2 accepts JSON arrays and coerces to
    ``tuple`` without a custom validator.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    action: str
    installation: GithubInstallationRef
    repositories: tuple[GithubInstallationRepository, ...] | None = None
    repositories_added: tuple[GithubInstallationRepository, ...] | None = None
    repositories_removed: tuple[GithubInstallationRepository, ...] | None = None


# ---------------------------------------------------------------------------
# §5.3 — Lambda environment-variable contract.
# ---------------------------------------------------------------------------


class ReceiverEnvConfig(BaseSettings):
    """Lambda environment variables (design.md §5.3).

    The three ``TRIKON_*`` queue/secret variables are supplied by the CDK
    stack. ``AWS_REGION`` is supplied by the Lambda runtime. ``log_level``
    and ``aws_region`` carry defaults so a local ``ReceiverEnvConfig()``
    construction under test only needs the three secret/queue values set.
    A missing / empty ``TRIKON_INSTALLATION_EVENTS_QUEUE_URL`` at Lambda
    cold start raises :class:`pydantic.ValidationError`, mirroring the
    shipping behaviour for ``TRIKON_VERIFY_JOBS_QUEUE_URL``
    (Requirement 4.5).
    """

    webhook_secret_arn: str = Field(alias="TRIKON_WEBHOOK_SECRET_ARN")
    verify_jobs_queue_url: str = Field(alias="TRIKON_VERIFY_JOBS_QUEUE_URL")
    installation_events_queue_url: str = Field(
        alias="TRIKON_INSTALLATION_EVENTS_QUEUE_URL"
    )
    log_level: str = Field(default="INFO", alias="TRIKON_LOG_LEVEL")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
