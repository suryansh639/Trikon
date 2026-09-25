# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each class definition surfaces as an ``explicit-any`` error. The
# error refers to code the plugin generates, not code we write — silence it
# at the file level.
# mypy: disable-error-code="explicit-any"
"""Pydantic v2 models for the Trikon Cloud Fargate verify-runner.

Every model in this module maps 1:1 onto a subsection of design.md §5:

* :class:`RunnerEnvConfig` — memo §5.4 env-var contract (design §5.1).
  Loaded at process start; a missing / malformed variable raises
  :class:`pydantic.ValidationError`, caught by ``entrypoint.main()``'s
  top-level handler and routed to the Never_Fail_Open_Contract.
* :class:`VerdictRow` — the ``trikon_verdicts`` DynamoDB row shape
  (design §5.2). ``evidence_blob`` and ``evidence_s3_key`` are mutually
  exclusive — a Pydantic ``model_validator(mode="after")`` enforces
  exactly-one-non-``None``.
* :class:`PrStateRow` — the ``trikon_pr_state`` row shape (design §5.3).
* :class:`GithubInstallationTokenResponse` — response body from
  ``POST /app/installations/{id}/access_tokens`` (design §5.4).
* :class:`CheckRunOutput`, :class:`CheckRunCreatePayload`,
  :class:`CheckRunUpdatePayload` — Check Run request bodies (design §5.5).
  ``CheckRunCreatePayload.name`` is typed ``Literal["Trikon"]`` so
  Invariant 7 (product-name-in-user-copy) is enforced at the type level,
  not just at the value level.
* :class:`GithubCheckRunResponse`, :class:`GithubCommentResponse` —
  response shapes (design §5.6); the runner reads ``id`` only.

Every response model carries ``ConfigDict(extra="allow", frozen=True)``
so GitHub can add fields over time without breaking validation, while
still preventing the runner from mutating the parsed body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ordering mirrors design.md §5.1 → §5.6, not isort alphabetical.
__all__ = [  # noqa: RUF022
    "RunnerEnvConfig",
    "VerdictRow",
    "PrStateRow",
    "GithubInstallationTokenResponse",
    "GithubCheckRunResponse",
    "GithubCommentResponse",
    "CheckRunOutput",
    "CheckRunCreatePayload",
    "CheckRunUpdatePayload",
]


# ---------------------------------------------------------------------------
# §5.1 — Fargate task env-var contract.
# ---------------------------------------------------------------------------


class RunnerEnvConfig(BaseSettings):
    """ECS Fargate env-var contract (memo §5.4 / design §5.1).

    Loaded at process start by ``entrypoint.main()``. Validation failure
    raises :class:`pydantic.ValidationError` which the entrypoint's
    top-level handler routes to the Never_Fail_Open_Contract.

    The six memo-§5.4 core fields are populated by Spec 3 via the
    ``ecs.RunTask`` environment override. The config-derived fields
    (table names, bucket name, App identifiers, URL template, log
    level) are populated by the CDK stack's task-definition environment
    block.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # Memo §5.4 core (populated by Spec 3 via ecs.RunTask overrides).
    installation_id: int = Field(alias="TRIKON_INSTALLATION_ID", ge=1)
    repo_full_name: str = Field(alias="TRIKON_REPO_FULL_NAME", min_length=3)
    pr_number: int = Field(alias="TRIKON_PR_NUMBER", ge=1)
    head_sha: str = Field(
        alias="TRIKON_HEAD_SHA",
        min_length=40,
        max_length=40,
        pattern=r"^[0-9a-f]{40}$",
    )
    base_sha: str = Field(
        alias="TRIKON_BASE_SHA",
        min_length=40,
        max_length=40,
        pattern=r"^[0-9a-f]{40}$",
    )
    event_type: str = Field(alias="TRIKON_EVENT_TYPE")
    delivery_id: str = Field(alias="TRIKON_DELIVERY_ID")
    aws_region: str = Field(alias="AWS_REGION", default="us-east-1")

    # Config-derived (populated by the CDK stack's task-definition env).
    verdicts_table_name: str = Field(
        alias="TRIKON_VERDICTS_TABLE", default="trikon_verdicts"
    )
    pr_state_table_name: str = Field(
        alias="TRIKON_PR_STATE_TABLE", default="trikon_pr_state"
    )
    evidence_bucket_name: str = Field(
        alias="TRIKON_EVIDENCE_BUCKET", default="trikon-cloud-evidence"
    )
    app_private_key_secret_arn: str = Field(alias="TRIKON_APP_PRIVATE_KEY_SECRET_ARN")
    app_id: int = Field(alias="TRIKON_APP_ID", ge=1)
    check_run_details_url_template: str = Field(
        alias="TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE",
        default="https://cloud.trikon.dev/audits/{audit_id}",
    )
    log_level: str = Field(alias="TRIKON_LOG_LEVEL", default="INFO")

    @property
    def repo_working_dir(self) -> Path:
        """Filesystem path the runner clones into.

        Fixed at ``/tmp/repo`` because the Fargate task filesystem is
        ephemeral and ``/tmp`` is the writable scratch mount.
        """
        return Path("/tmp/repo")

    @property
    def pr_state_key(self) -> str:
        """Partition key for the ``trikon_pr_state`` row (memo §4.3)."""
        return f"{self.installation_id}#{self.repo_full_name}#{self.pr_number}"


# ---------------------------------------------------------------------------
# §5.2 — Verdict row (trikon_verdicts).
# ---------------------------------------------------------------------------


class VerdictRow(BaseModel):
    """One row of the ``trikon_verdicts`` DynamoDB table (design §5.2).

    ``evidence_blob`` (inline gzipped JSON) and ``evidence_s3_key``
    (S3 spill pointer) are mutually exclusive: DynamoDB rejects items
    over 400 KB and the runner spills to S3 above the ~350 KB
    guardrail. A :meth:`~pydantic.model_validator` enforces that
    exactly one of the two is non-``None``.

    ``risk_bucket_sk`` is the GSI2 sort key: ``"<bucket>#<pr_ts>"``
    where ``<bucket>`` is ``f"{min(int(blast_radius_numeric), 9999):04d}"``.
    """

    model_config = ConfigDict(frozen=True)

    installation_id: int = Field(ge=1)
    sk: str  # composite: "<pr_ts>#<audit_id>"
    repo_full_name: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40)
    base_sha: str = Field(min_length=40, max_length=40)
    decision: str  # "allow" | "block" | "require_human"
    matched_rule: str  # "default" when Verdict.matched_rule is None
    blast_radius_score: int = Field(ge=0)
    new_errors: int = Field(ge=0)
    new_warnings: int = Field(ge=0)
    preexisting_errors: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    fargate_task_arn: str
    schema_version: int = Field(ge=1)
    evidence_blob: bytes | None
    evidence_s3_key: str | None
    risk_bucket_sk: str  # GSI2 sort key: "<bucket>#<pr_ts>"

    @model_validator(mode="after")
    def _check_evidence_exclusivity(self) -> VerdictRow:
        """Enforce ``evidence_blob`` XOR ``evidence_s3_key``.

        Exactly one of the two must be non-``None``: an inline
        row carries the gzipped JSON directly in ``evidence_blob``;
        a spilled row carries an S3 pointer in ``evidence_s3_key``.
        The DynamoDB writer picks one branch based on the ~350 KB
        guardrail (design §3.9, clarify answer 5).
        """
        if (self.evidence_blob is None) == (self.evidence_s3_key is None):
            raise ValueError(
                "VerdictRow requires exactly one of evidence_blob or evidence_s3_key"
            )
        return self


# ---------------------------------------------------------------------------
# §5.3 — PR-state row (trikon_pr_state).
# ---------------------------------------------------------------------------


class PrStateRow(BaseModel):
    """One row of the ``trikon_pr_state`` DynamoDB table (design §5.3).

    Tracks the last-known GitHub artifact IDs for a given PR so
    subsequent runs update-in-place instead of creating duplicates.
    ``last_comment_id`` and ``last_check_run_id`` are nullable per
    memo §4.3 — on the first run for a PR both are ``None`` and the
    runner falls through to the create path.
    """

    model_config = ConfigDict(frozen=True)

    pr_key: str  # "<installation_id>#<repo_full_name>#<pr_number>"
    last_comment_id: int | None
    last_check_run_id: int | None
    last_head_sha: str
    last_updated_at: str  # ISO-8601 UTC with ms precision


# ---------------------------------------------------------------------------
# §5.4 — Installation-token response.
# ---------------------------------------------------------------------------


class GithubInstallationTokenResponse(BaseModel):
    """Response body from ``POST /app/installations/{id}/access_tokens``.

    ``extra="allow"`` lets GitHub add fields (permissions map,
    repository selection, etc.) without breaking validation. The
    runner only reads ``token``; ``expires_at`` is retained for
    future diagnostic use but not consulted by the current
    task-lifetime cache (see :mod:`trikon_cloud.fargate_runner.token_cache`).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    token: str
    expires_at: str


# ---------------------------------------------------------------------------
# §5.5 — Check Run request bodies.
# ---------------------------------------------------------------------------


class CheckRunOutput(BaseModel):
    """The ``output`` sub-object of a Check Run create / update body."""

    title: str
    summary: str
    text: str | None = None


class CheckRunCreatePayload(BaseModel):
    """Body of ``POST /repos/{owner}/{repo}/check-runs``.

    ``name`` is typed ``Literal["Trikon"]`` so Invariant 7
    (product-name-in-user-copy) is enforced at the type level. Any
    caller that tries to pass a different name fails mypy before the
    runtime check even runs.
    """

    name: Literal["Trikon"]
    head_sha: str
    status: str  # "completed"
    conclusion: str  # "success" | "failure" | "neutral"
    output: CheckRunOutput
    details_url: str


class CheckRunUpdatePayload(BaseModel):
    """Body of ``PATCH /repos/{owner}/{repo}/check-runs/{check_run_id}``.

    The update path omits ``name`` and ``head_sha`` — GitHub keys the
    Check Run on its ID and rejects attempts to change either field
    on update.
    """

    status: str
    conclusion: str
    output: CheckRunOutput
    details_url: str


# ---------------------------------------------------------------------------
# §5.6 — Check Run and PR-comment response shapes.
# ---------------------------------------------------------------------------


class GithubCheckRunResponse(BaseModel):
    """Response from Check Run create / update.

    The runner reads ``id`` only (used to populate
    :attr:`PrStateRow.last_check_run_id`). ``extra="allow"`` retains
    the rest of the body for observability without pinning the
    schema.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: int


class GithubCommentResponse(BaseModel):
    """Response from PR-comment create / update.

    The runner reads ``id`` only (used to populate
    :attr:`PrStateRow.last_comment_id`).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: int
