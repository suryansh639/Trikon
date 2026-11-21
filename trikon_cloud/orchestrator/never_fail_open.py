# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# BaseModel subclasses we consume (:class:`VerdictRow`). Under the repo's
# ``disallow_any_explicit = true`` mypy config, class definitions surface
# as ``explicit-any`` errors from generated code. We do NOT define any
# BaseModel subclass in this module, so the plugin's synthesized ``Any``
# does not touch our public signatures — but ``botocore.exceptions.ClientError``
# and ``boto3`` are imported as untyped modules (``# type: ignore[import-untyped]``)
# and mypy would surface method-body ``Any`` from ``exc.response.get(...)``
# and ``boto3_session.client(...)``. We keep those explicit-any usages
# scoped to method bodies via :func:`typing.cast` boundaries, so no
# public signature carries ``Any``. No file-level suppression needed.
"""Never-Fail-Open path for the Trikon Cloud orchestrator (Spec 3).

Implements the four public symbols required by design.md §7:

* :func:`build_synthetic_verdict_row` — pure builder for a synthetic
  ``require_human`` :class:`~trikon_cloud.fargate_runner.models.VerdictRow`
  (design.md §3.5). Spec 2's :class:`VerdictRow` is reused verbatim; no
  new verdict model is defined.
* :func:`write_orchestrator_failure_verdict` — the six-step body from
  design.md §7.2: build the row, ``dynamodb:PutItem`` on
  ``trikon_verdicts`` with the natural-key ``ConditionExpression``,
  swallow ``ConditionalCheckFailedException`` as an idempotency win,
  then POST a neutral Check Run through
  :class:`OrchestratorGithubClient`. Check Run failures are logged at
  ERROR without re-raising — the DynamoDB write already satisfies
  Invariant 2 (design.md §4.5 partial-success semantics).
* :class:`OrchestratorGithubClient` — thin :class:`httpx.Client`
  wrapper (design.md §7.3). Mints an App JWT from the PEM held in an
  SSM ``SecureString`` parameter, exchanges it for an installation-scoped access
  token, caches both, and POSTs
  ``/repos/{repo_full_name}/check-runs`` with ``name="Trikon"``,
  ``status="completed"``, ``conclusion="neutral"``. Retries 5xx / 429
  three times with exponential backoff (0.5s, 1s, 2s) and a 30-second
  total budget; surfaces 4xx and budget exhaustion as
  :class:`OrchestratorGithubClientError`.
* :class:`OrchestratorGithubClientError` — raised on retry-budget
  exhaustion or non-retryable 4xx.
* :class:`FailClosedNotice` (sandbox-minute-plans) — optional copy and
  evidence for a quota Fail_Closed_Result. Passed to
  :func:`write_orchestrator_failure_verdict`, it overrides the row's
  ``matched_rule`` and the Check Run's title, summary, conclusion, and
  details URL via :meth:`OrchestratorGithubClient.create_check_run`.
  ``notice=None`` keeps the terminal behaviour byte for byte.

**Security invariants** (Invariant 6, Requirement 9.3):

* The App private key PEM, the signed App JWT, the installation
  access token, the raw SSM ``GetParameter`` response body, and the raw
  ``ecs.RunTask`` response body are never logged at any level.
* Failure-path exception messages carry status codes and error
  classes, never response bodies or credential bytes.

**PyJWT compatibility note** (established defect fix in Spec 2's
``github_client.py``): PyJWT 2.10 rejects a non-string ``iss`` claim
with ``TypeError`` at :func:`jwt.encode`. GitHub accepts either form
on the wire, so ``str(app_id)`` is used at JWT construction —
``iss=self._app_id`` (``int``) would break under PyJWT ≥ 2.10.

**Token caching semantics parallel Spec 2's**
``trikon_cloud.fargate_runner.token_cache``; the shape differs
because this client is multi-tenant per Lambda container while Spec
2's is single-tenant per Fargate task (design.md §7.4). Any future
refactor to share code would need to keep the multi-tenant key
structure here.
"""

from __future__ import annotations

import gzip
import json
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

import boto3  # type: ignore[import-untyped]
import httpx
import jwt
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trikon_cloud.fargate_runner.models import VerdictRow
from trikon_cloud.orchestrator.logger import get_logger
from trikon_cloud.orchestrator.models import OrchestratorEnvConfig
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# Ordering mirrors the design §7 walk (pure builder → six-step body →
# HTTP client → error). Not isort alphabetical.
__all__ = [  # noqa: RUF022
    "write_orchestrator_failure_verdict",
    "publish_fail_closed_result",
    "read_interim_check_run_id",
    "FailClosedNotice",
    "FailClosedTarget",
    "FailClosedPublication",
    "CheckRunConclusion",
    "build_synthetic_verdict_row",
    "OrchestratorGithubClient",
    "OrchestratorGithubClientError",
]


# ---------------------------------------------------------------------------
# Module-scope caches and retry constants.
# ---------------------------------------------------------------------------

# Multi-tenant installation-token cache: one entry per installation_id
# encountered within this Lambda container's lifetime (design.md §7.3,
# §7.4). Entries are `(token, expires_at)` where `expires_at` is a
# tz-aware UTC :class:`datetime`. A cached entry is reused only while
# it stays valid for at least a 5-minute safety margin.
_TOKEN_CACHE: dict[int, tuple[str, datetime]] = {}

# Single-slot App JWT cache. GitHub's App JWT max TTL is 10 minutes;
# we mint with `exp = now + 540` (9 minutes) and reuse while at least
# a 1-minute safety margin remains.
_APP_JWT_CACHE: tuple[str, datetime] | None = None

# Guards both caches — the orchestrator Lambda is single-threaded per
# invocation but Powertools may schedule concurrent async work in some
# extensions; the lock is a cheap belt-and-braces measure.
_CACHE_LOCK = threading.Lock()

# Retry policy for the Check Run POST (task spec / design.md §7.3):
# three attempts, exponential backoff 0.5s / 1s / 2s with ±20% jitter,
# 30-second total budget. Retry on transient 5xx and 429.
_CHECK_RUN_RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_CHECK_RUN_RETRY_ATTEMPTS: int = 3
_CHECK_RUN_RETRY_BUDGET_SECONDS: float = 30.0
_CHECK_RUN_BACKOFFS_SECONDS: tuple[float, float, float] = (0.5, 1.0, 2.0)

# App JWT lifetime: mint 9 minutes ahead, reuse while ≥ 1 minute remains.
_APP_JWT_LIFETIME_SECONDS: int = 540
_APP_JWT_SAFETY_MARGIN = timedelta(minutes=1)

# Installation-token safety margin: reuse while ≥ 5 minutes remain
# (design.md §7.3). The GitHub-issued 1-hour TTL is respected via the
# cached `expires_at`.
_INSTALLATION_TOKEN_SAFETY_MARGIN = timedelta(minutes=5)

# Neutral-Check-Run copy templates. Held at module scope so any
# future audit / grep for the exact wire strings finds them in one
# place. Invariant 7: ``name`` is exactly ``"Trikon"``.
_CHECK_RUN_NAME: str = "Trikon"
_CHECK_RUN_TITLE: str = "Trikon Cloud verification unavailable"
_CHECK_RUN_SUMMARY_TEMPLATE: str = (
    "The Trikon Cloud verification runner could not be dispatched for this "
    "pull request. A human reviewer will follow up. Delivery id: {delivery_id}."
)

# ``matched_rule`` of the existing RunTask terminal path (Requirement 7.1).
_TERMINAL_MATCHED_RULE: str = "orchestrator terminal failure"

#: The two Check Run conclusions a Fail_Closed_Result may carry
#: (Requirement 11.10). Quota-related results use ``action_required``
#: because GitHub treats ``neutral`` as passing for required checks.
CheckRunConclusion = Literal["neutral", "action_required"]


@dataclass(frozen=True)
class FailClosedNotice:
    """Customer-facing copy and evidence for one Fail_Closed_Result.

    Passed to :func:`write_orchestrator_failure_verdict` to override
    the default RunTask-terminal row and Check Run (Requirements 11.9,
    11.10). Built by the pure ``quota_notices`` module.

    Attributes
    ----------
    matched_rule:
        The synthetic verdict row's ``matched_rule``; names the cause
        (e.g. ``"sandbox minutes exhausted"``).
    title:
        Check Run ``output.title``.
    summary:
        Check Run ``output.summary``. There is no separate text field.
    details_url:
        Check Run ``details_url`` (Connect_URL, Upgrade_URL, or the
        rendered details-URL template).
    conclusion:
        ``"neutral"`` or ``"action_required"``.
    error_class:
        ``error_class`` embedded in the row's ``evidence_blob``.
    error_code:
        ``error_code`` embedded in the row's ``evidence_blob``.
    """

    matched_rule: str
    title: str
    summary: str
    details_url: str
    conclusion: CheckRunConclusion
    error_class: str
    error_code: str


@dataclass(frozen=True)
class FailClosedTarget:
    """The pull request head a Spot-related Fail_Closed_Result lands on.

    Built by the Spot_Retry_Handler from the interrupted task's
    Runner_Overrides (cost-cuts design, "Decision (pure)"). ``pr_number``
    is ``None`` when the overrides do not carry it; then no
    ``trikon_pr_state`` read happens and a new Check Run is created.
    """

    installation_id: int
    repo_full_name: str
    head_sha: str
    pr_number: int | None


@dataclass(frozen=True)
class FailClosedPublication:
    """Outcome of :func:`publish_fail_closed_result`.

    ``row`` is ``"written"`` for a new synthetic verdict row,
    ``"existed"`` when the natural-key condition failed (re-delivery),
    and ``"skipped"`` when no row message was available. ``check_run``
    is ``"completed"`` when an existing Check Run was PATCHed and
    ``"created"`` when a new one was POSTed.
    """

    row: Literal["written", "existed", "skipped"]
    check_run: Literal["created", "completed"]


class OrchestratorGithubClientError(Exception):
    """Raised on Check Run retry-budget exhaustion or non-retryable 4xx.

    Caught by :func:`write_orchestrator_failure_verdict` and logged
    at ERROR without re-raising — the DynamoDB verdict write has
    already satisfied Invariant 2 by the time this exception can be
    raised, so the handler treats a Check Run failure as a
    partial-success degradation rather than an Invariant-2 violation
    (design.md §4.5 partial-success semantics).
    """


# ---------------------------------------------------------------------------
# Protocols — typed shims for the two untyped boto3 clients we touch.
# ---------------------------------------------------------------------------


class _SsmGetParameterProtocol(Protocol):
    """Structural type for the one boto3 SSM call we make.

    ``boto3`` ships without type stubs; this :class:`typing.Protocol`
    lets ``mypy --strict`` type-check the single ``get_parameter``
    call site in :meth:`OrchestratorGithubClient._get_app_private_key_pem`
    without a runtime dependency on ``boto3-stubs``. The client is
    expected to return a mapping whose ``Parameter.Value`` field is the
    decrypted PEM-encoded App private key.
    """

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803 -- AWS boto3 API contract
    ) -> Mapping[str, object]: ...


class _DynamoDbClientProtocol(Protocol):
    """Structural type for the ``dynamodb:PutItem`` boto3 call we make.

    Same rationale as :class:`_SsmGetParameterProtocol`: keeps
    ``mypy --strict`` clean without ``boto3-stubs``. Item shape is
    the ``{"S":…, "N":…, "B":…}`` attribute-value form built by
    :func:`_to_dynamodb_item`.
    """

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Item: dict[str, dict[str, str | int | bytes]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object: ...


class _DynamoDbGetItemProtocol(Protocol):
    """Structural type for the ``dynamodb:GetItem`` call on ``trikon_pr_state``.

    Used by :func:`read_interim_check_run_id`. Same rationale as
    :class:`_DynamoDbClientProtocol`.
    """

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Key: dict[str, dict[str, str]],  # noqa: N803
        ConsistentRead: bool,  # noqa: N803
    ) -> Mapping[str, object]: ...


# ---------------------------------------------------------------------------
# §3.5 — Pure synthetic-verdict builder.
# ---------------------------------------------------------------------------


def build_synthetic_verdict_row(
    *,
    sqs_message: SqsJobMessage,
    error_class: str,
    error_code: str,
    duration_ms: int = 0,
    matched_rule: str = _TERMINAL_MATCHED_RULE,
) -> VerdictRow:
    """Build the synthetic ``require_human`` verdict row (design.md §3.5).

    Pure — no IO, no clock reads, no environment access. Every field
    on the returned :class:`VerdictRow` is populated verbatim from the
    design.md §3.5 table:

    * ``sk`` is ``f"{sqs_message.sent_at}#{sqs_message.delivery_id}"``
      matching Spec 2's ``sk`` pattern.
    * ``decision`` is ``"require_human"`` and ``matched_rule`` defaults
      to ``"orchestrator terminal failure"`` (Requirement 7.1); a
      Fail_Closed_Result passes the cause instead (Requirement 11.9).
    * The four score counters (``blast_radius_score``, ``new_errors``,
      ``new_warnings``, ``preexisting_errors``) are all ``0`` — the
      runner never observed the diff.
    * ``fargate_task_arn`` is the documented placeholder
      ``"n/a-orchestrator-terminal"``.
    * ``schema_version`` is ``2`` (matches Spec 2 §5.5).
    * ``evidence_blob`` is the gzip-compressed canonical JSON of
      ``{"error_class", "error_code", "delivery_id"}`` (sorted keys,
      compact separators) — inline and always small so this path
      never spills to S3; ``evidence_s3_key`` is ``None``.
    * ``risk_bucket_sk`` is ``f"0000#{sqs_message.sent_at}"`` — bucket
      ``0000`` for ``require_human`` verdicts with no risk score.

    ``duration_ms`` is passed by the caller (design.md §3.5:
    "Wall-clock ms from handler entry to
    :func:`write_orchestrator_failure_verdict` invocation"). Defaults
    to ``0`` so unit-test fixtures need not thread a wall-clock value.

    Parameters
    ----------
    sqs_message:
        The validated inbound :class:`SqsJobMessage`; all identity
        fields on the row (``installation_id``, ``repo_full_name``,
        ``pr_number``, ``head_sha``, ``base_sha``, ``sk`` components,
        ``risk_bucket_sk``) copy from here byte-for-byte.
    error_class:
        Short classification token embedded in ``evidence_blob``.
        Typically ``"terminal"`` from
        :func:`~trikon_cloud.orchestrator.ecs_dispatcher.classify_client_error`.
    error_code:
        AWS ``ClientError`` code embedded in ``evidence_blob``
        (e.g. ``"AccessDeniedException"``).
    duration_ms:
        Wall-clock milliseconds from handler entry.
    matched_rule:
        The row's ``matched_rule``. Defaults to the RunTask-terminal
        rule; quota Fail_Closed_Results pass the cause
        (``FailClosedNotice.matched_rule``).
    """
    evidence_payload = json.dumps(
        {
            "error_class": error_class,
            "error_code": error_code,
            "delivery_id": sqs_message.delivery_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence_blob = gzip.compress(evidence_payload)
    return VerdictRow(
        installation_id=sqs_message.installation_id,
        sk=f"{sqs_message.sent_at}#{sqs_message.delivery_id}",
        repo_full_name=sqs_message.repo_full_name,
        pr_number=sqs_message.pr_number,
        head_sha=sqs_message.head_sha,
        base_sha=sqs_message.base_sha,
        decision="require_human",
        matched_rule=matched_rule,
        blast_radius_score=0,
        new_errors=0,
        new_warnings=0,
        preexisting_errors=0,
        duration_ms=duration_ms,
        fargate_task_arn="n/a-orchestrator-terminal",
        schema_version=2,
        evidence_blob=evidence_blob,
        evidence_s3_key=None,
        risk_bucket_sk=f"0000#{sqs_message.sent_at}",
    )


def _to_dynamodb_item(
    row: VerdictRow,
) -> dict[str, dict[str, str | int | bytes]]:
    """Coerce :class:`VerdictRow` to the boto3 attribute-value shape.

    Private — scoped to :func:`write_orchestrator_failure_verdict`'s
    body and not exported. Maps each :class:`VerdictRow` field to the
    ``{"S":…, "N":…, "B":…}`` shape ``dynamodb:PutItem`` expects. The
    synthetic verdict always populates ``evidence_blob`` and always
    leaves ``evidence_s3_key`` at :data:`None` (design.md §3.5), so
    this helper always emits a ``{"B": row.evidence_blob}`` entry and
    never an ``evidence_s3_key`` entry — the ``VerdictRow``
    ``model_validator(mode="after")`` XOR guarantee upstream keeps
    the shape byte-identical to Spec 2's writer for the inline branch
    (Requirement 7.4).
    """
    assert row.evidence_blob is not None, (
        "build_synthetic_verdict_row always populates evidence_blob; "
        "evidence_s3_key is always None on the orchestrator path"
    )
    item: dict[str, dict[str, str | int | bytes]] = {
        "installation_id": {"N": str(row.installation_id)},
        "sk": {"S": row.sk},
        "repo_full_name": {"S": row.repo_full_name},
        "pr_number": {"N": str(row.pr_number)},
        "head_sha": {"S": row.head_sha},
        "base_sha": {"S": row.base_sha},
        "decision": {"S": row.decision},
        "matched_rule": {"S": row.matched_rule},
        "blast_radius_score": {"N": str(row.blast_radius_score)},
        "new_errors": {"N": str(row.new_errors)},
        "new_warnings": {"N": str(row.new_warnings)},
        "preexisting_errors": {"N": str(row.preexisting_errors)},
        "duration_ms": {"N": str(row.duration_ms)},
        "fargate_task_arn": {"S": row.fargate_task_arn},
        "schema_version": {"N": str(row.schema_version)},
        "risk_bucket_sk": {"S": row.risk_bucket_sk},
        "evidence_blob": {"B": row.evidence_blob},
    }
    return item


# ---------------------------------------------------------------------------
# §7.3 — OrchestratorGithubClient.
# ---------------------------------------------------------------------------


class OrchestratorGithubClient:
    """Thin :class:`httpx.Client` wrapper for the Never-Fail-Open POST.

    Two responsibilities:

    1. **Token minting** — sign an App JWT from the PEM held in an SSM
       ``SecureString`` parameter, exchange it for an installation-scoped access token,
       and cache both. Multi-tenant per Lambda container: one entry
       in :data:`_TOKEN_CACHE` per ``installation_id`` seen so far.
    2. **Neutral Check Run POST** — ``POST /repos/{repo_full_name}/check-runs``
       with ``name="Trikon"``, ``status="completed"``,
       ``conclusion="neutral"``, and a fixed-copy ``output`` object.
       Retries 5xx / 429 with exponential backoff; surfaces 4xx and
       retry-budget exhaustion as :class:`OrchestratorGithubClientError`.

    ``http_client`` is a dependency-injection seam for tests
    (typically a :mod:`respx`-backed :class:`httpx.Client`).
    ``ssm_client`` is likewise injectable for tests
    (typically a :mod:`moto`-backed boto3 client). Both are required
    parameters; the caller (the Lambda cold-start bootstrap) owns
    their construction and lifecycle.

    **Security invariants** (Invariant 6, Requirement 9.3):

    * The App private key PEM stays on the stack — read from SSM
      Parameter Store, passed directly to :func:`jwt.encode`, never assigned
      to a module- or instance-level attribute.
    * The signed JWT is cached only inside :data:`_APP_JWT_CACHE`
      and never appears in a log record.
    * The installation token is cached only inside
      :data:`_TOKEN_CACHE` and never appears in a log record.
    * Exception messages carry status codes and error class names,
      never response bodies or credential bytes.
    """

    def __init__(
        self,
        *,
        app_id: int,
        app_private_key_parameter_name: str,
        ssm_client: _SsmGetParameterProtocol,
        http_client: httpx.Client,
    ) -> None:
        self._app_id: int = app_id
        self._app_private_key_parameter_name: str = app_private_key_parameter_name
        self._ssm_client: _SsmGetParameterProtocol = ssm_client
        self._http_client: httpx.Client = http_client

    def create_neutral_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        details_url: str,
    ) -> None:
        """POST a ``completed`` / ``neutral`` Check Run on ``head_sha``.

        Design.md §7.2 step 5 body:

        .. code-block:: json

           {
             "name": "Trikon",
             "head_sha": "<head_sha>",
             "status": "completed",
             "conclusion": "neutral",
             "output": {
               "title": "Trikon Cloud verification unavailable",
               "summary": "...",
               "text": null
             },
             "details_url": "<details_url>"
           }

        ``name`` is the literal ``"Trikon"`` — Invariant 7 pins the
        product name in all customer-visible copy. The ``summary``
        embeds the ``delivery_id`` for operator correlation (extracted
        from the ``details_url`` template parameter which the caller
        renders from :attr:`OrchestratorEnvConfig.trikon_check_run_details_url_template`).

        Raises:
            OrchestratorGithubClientError: On retry-budget exhaustion
                or non-retryable 4xx. Caught upstream by
                :func:`write_orchestrator_failure_verdict` and logged
                at ERROR without re-raising.
        """
        # Extract delivery_id from the rendered details_url so the
        # summary carries it — the caller renders details_url from
        # the URL template, which contains ``{delivery_id}`` verbatim.
        # We do NOT parse the URL — the summary just embeds a
        # human-readable pointer.
        summary = _CHECK_RUN_SUMMARY_TEMPLATE.format(
            delivery_id=_extract_delivery_id_hint(details_url)
        )
        self.create_check_run(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            title=_CHECK_RUN_TITLE,
            summary=summary,
            conclusion="neutral",
            details_url=details_url,
        )

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
        """POST a ``completed`` Check Run named ``"Trikon"`` on ``head_sha``.

        The general form behind :meth:`create_neutral_check_run`; the
        caller supplies the copy, the conclusion (``"neutral"`` or
        ``"action_required"``, Requirement 11.10), and the details URL.
        ``name`` is always ``"Trikon"`` and ``status`` always
        ``"completed"``; ``output.text`` is always ``null``. The body
        key order matches the historical neutral POST so the wire
        bytes are unchanged for the terminal path.

        Raises:
            OrchestratorGithubClientError: On retry-budget exhaustion
                or non-retryable 4xx.
        """
        token = self._get_installation_token(installation_id)
        body: dict[str, object] = {
            "name": _CHECK_RUN_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": title,
                "summary": summary,
                "text": None,
            },
            "details_url": details_url,
        }
        self._request_with_retry(
            method="POST",
            url=f"https://api.github.com/repos/{repo_full_name}/check-runs",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json_body=body,
        )

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
        """PATCH an existing Check Run to ``completed`` with ``conclusion``.

        Completes the Interim_Check_Run (``in_progress``) the runner
        posted before a Spot interruption (cost-cuts design D1). The
        body omits ``name`` and ``head_sha``, which GitHub keys on the
        Check Run id, and otherwise matches :meth:`create_check_run`:
        ``status``, ``conclusion``, ``output`` (``text`` null) and
        ``details_url``. Same token minting and retry policy.

        Raises:
            OrchestratorGithubClientError: On retry-budget exhaustion
                or non-retryable 4xx (e.g. 404 for an unknown id).
        """
        token = self._get_installation_token(installation_id)
        body: dict[str, object] = {
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": title,
                "summary": summary,
                "text": None,
            },
            "details_url": details_url,
        }
        self._request_with_retry(
            method="PATCH",
            url=(
                f"https://api.github.com/repos/{repo_full_name}"
                f"/check-runs/{check_run_id}"
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Private helpers — token minting + retry loop.
    # ------------------------------------------------------------------

    def _get_installation_token(self, installation_id: int) -> str:
        """Return a cached installation token or mint a fresh one.

        Cache hit condition: entry exists AND ``expires_at - now >=
        5 minutes`` (design.md §7.3). Cache miss mints an App JWT
        (also cached) then POSTs
        ``/app/installations/{installation_id}/access_tokens``.
        """
        now = datetime.now(UTC)
        with _CACHE_LOCK:
            cached = _TOKEN_CACHE.get(installation_id)
            if cached is not None:
                token_str, expires_at = cached
                if expires_at - now >= _INSTALLATION_TOKEN_SAFETY_MARGIN:
                    return token_str
        # Mint outside the lock — the network round-trip must not
        # serialize concurrent installations. The App JWT helper has
        # its own critical section for its single-slot cache.
        app_jwt = self._get_app_jwt()
        response = self._request_with_retry(
            method="POST",
            url=(
                "https://api.github.com/app/installations/"
                f"{installation_id}/access_tokens"
            ),
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json_body=None,
        )
        # Response body is small (token + expiry + permissions map).
        # We only read ``token`` and ``expires_at``; the rest of the
        # body is discarded and never logged (Invariant 6).
        parsed = response.json()
        token_value = str(parsed["token"])
        expires_at_str = str(parsed["expires_at"])
        # GitHub returns ISO-8601 with a ``Z`` suffix (UTC). Python's
        # :meth:`datetime.fromisoformat` accepts ``+00:00`` but not
        # ``Z`` on older 3.11 patches; normalize.
        expires_at_dt = datetime.fromisoformat(
            expires_at_str.replace("Z", "+00:00")
        )
        with _CACHE_LOCK:
            _TOKEN_CACHE[installation_id] = (token_value, expires_at_dt)
        return token_value

    def _get_app_jwt(self) -> str:
        """Return a cached App JWT or mint a fresh one.

        JWT lifetime is 9 minutes (``exp = now + 540``); a cached
        entry is reused while at least a 1-minute safety margin
        remains. ``iss`` is ``str(self._app_id)`` — PyJWT ≥ 2.10
        rejects a non-string ``iss`` with
        ``TypeError("Issuer (iss) must be a string.")`` at
        :func:`jwt.encode` (established defect fix in Spec 2's
        ``github_client.py``).

        The App private key PEM is read from SSM Parameter Store
        per-mint — the caching layer sits on the JWT, not on the PEM.
        """
        global _APP_JWT_CACHE
        now = datetime.now(UTC)
        with _CACHE_LOCK:
            if _APP_JWT_CACHE is not None:
                jwt_str, expires_at = _APP_JWT_CACHE
                if expires_at - now >= _APP_JWT_SAFETY_MARGIN:
                    return jwt_str
        private_key_pem = self._get_app_private_key_pem()
        issued_at = int(now.timestamp()) - 60  # clock-skew tolerance
        expiry = int(now.timestamp()) + _APP_JWT_LIFETIME_SECONDS
        payload: dict[str, str | int] = {
            "iss": str(self._app_id),
            "iat": issued_at,
            "exp": expiry,
        }
        signed_jwt: str = jwt.encode(payload, private_key_pem, algorithm="RS256")
        expiry_dt = datetime.fromtimestamp(expiry, tz=UTC)
        with _CACHE_LOCK:
            _APP_JWT_CACHE = (signed_jwt, expiry_dt)
        return signed_jwt

    def _get_app_private_key_pem(self) -> bytes:
        """Read the App private key PEM from its SSM ``SecureString`` parameter.

        The returned bytes are consumed immediately by
        :func:`jwt.encode` and never held anywhere else. The SSM
        response body is NOT logged (Invariant 6). A ``ClientError``
        (e.g. ``ParameterNotFound``) or a ``KeyError`` on a response
        without ``Parameter.Value`` propagates unchanged.
        """
        response = self._ssm_client.get_parameter(
            Name=self._app_private_key_parameter_name,
            WithDecryption=True,
        )
        parameter = cast(Mapping[str, object], response["Parameter"])
        return cast(str, parameter["Value"]).encode("utf-8")

    def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, object] | None,
    ) -> httpx.Response:
        """Execute an HTTP request with the design.md §7.3 retry policy.

        Three attempts. Retry on ``httpx.HTTPError`` (network error,
        timeout) and on responses whose status codes are in
        :data:`_CHECK_RUN_RETRY_STATUSES` (``429``, ``500``, ``502``,
        ``503``, ``504``). Backoff between attempts uses
        :data:`_CHECK_RUN_BACKOFFS_SECONDS` with ±20% jitter, capped
        by the remaining slice of the 30-second budget. On budget
        exhaustion or non-retryable 4xx, raises
        :class:`OrchestratorGithubClientError` — the exception
        message carries the last observed status code but never the
        response body (Invariant 6).
        """
        start_monotonic = time.monotonic()
        last_status: int | None = None
        for attempt_index in range(_CHECK_RUN_RETRY_ATTEMPTS):
            elapsed = time.monotonic() - start_monotonic
            if elapsed >= _CHECK_RUN_RETRY_BUDGET_SECONDS:
                raise OrchestratorGithubClientError(
                    f"github retry budget exhausted before attempt {attempt_index + 1}"
                    f" (last_status={last_status})"
                )
            try:
                response = self._http_client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                if attempt_index == _CHECK_RUN_RETRY_ATTEMPTS - 1:
                    raise OrchestratorGithubClientError(
                        f"github request network failure after "
                        f"{attempt_index + 1} attempts: {type(exc).__name__}"
                    ) from exc
                _sleep_with_jitter(attempt_index, start_monotonic)
                continue
            last_status = response.status_code
            if 200 <= last_status < 300:
                return response
            if last_status in _CHECK_RUN_RETRY_STATUSES:
                if attempt_index == _CHECK_RUN_RETRY_ATTEMPTS - 1:
                    raise OrchestratorGithubClientError(
                        f"github request returned {last_status} after "
                        f"{attempt_index + 1} attempts"
                    )
                _sleep_with_jitter(attempt_index, start_monotonic)
                continue
            # Non-retryable — 4xx (401 / 403 / 404 / 422 …) or an
            # unexpected 3xx. Surface immediately; the response body
            # is deliberately NOT embedded in the exception message
            # (Invariant 6).
            raise OrchestratorGithubClientError(
                f"github request returned non-retryable {last_status}"
            )
        # Loop exits normally only via ``return``; the trailing raise
        # keeps mypy convinced the function does not fall through.
        raise OrchestratorGithubClientError(
            f"github retry loop exited unexpectedly (last_status={last_status})"
        )


def _sleep_with_jitter(attempt_index: int, start_monotonic: float) -> None:
    """Sleep for :data:`_CHECK_RUN_BACKOFFS_SECONDS` [i] scaled by ±20% jitter.

    The sleep duration is capped by the remaining slice of the
    30-second budget so the retry loop never sleeps past its cutoff.
    """
    base_seconds = _CHECK_RUN_BACKOFFS_SECONDS[attempt_index]
    jitter_factor = random.uniform(0.8, 1.2)
    elapsed = time.monotonic() - start_monotonic
    remaining_budget = max(0.0, _CHECK_RUN_RETRY_BUDGET_SECONDS - elapsed)
    duration = min(base_seconds * jitter_factor, remaining_budget)
    if duration > 0:
        time.sleep(duration)


def _extract_delivery_id_hint(details_url: str) -> str:
    """Extract the trailing path segment of ``details_url`` for the summary.

    The details-URL template is
    ``https://api.trikon.unideploy.com/audits/{delivery_id}``; the last
    path segment is the delivery_id. This helper is a best-effort
    parse so the Check Run summary can carry the delivery_id in
    human-readable form. Pure — no IO, no exceptions escape.
    """
    stripped = details_url.rstrip("/")
    last_slash = stripped.rfind("/")
    if last_slash == -1:
        return stripped
    return stripped[last_slash + 1 :]


# ---------------------------------------------------------------------------
# §7.2 — Six-step Never-Fail-Open body.
# ---------------------------------------------------------------------------


def write_orchestrator_failure_verdict(
    *,
    sqs_message: SqsJobMessage,
    error_class: str,
    error_code: str,
    boto3_session: boto3.session.Session,
    github_client: OrchestratorGithubClient,
    env: OrchestratorEnvConfig,
    duration_ms: int = 0,
    notice: FailClosedNotice | None = None,
    complete_check_run_id: int | None = None,
) -> None:
    """Synthetic ``require_human`` verdict + neutral Check Run (design.md §7.2).

    With ``notice=None`` this is the RunTask-terminal path, unchanged
    byte for byte: ``matched_rule="orchestrator terminal failure"``,
    the caller's ``error_class`` / ``error_code`` in ``evidence_blob``,
    and a neutral Check Run via
    :meth:`OrchestratorGithubClient.create_neutral_check_run`. With a
    :class:`FailClosedNotice`, the row takes ``notice.matched_rule``
    and the notice's ``error_class`` / ``error_code`` for its evidence,
    and the Check Run posts the notice's title, summary, conclusion,
    and details URL through
    :meth:`OrchestratorGithubClient.create_check_run` (Requirements
    11.9, 11.10). The DynamoDB condition and idempotency rule are the
    same on both paths.

    Six-step body — the two halves (DynamoDB write, Check Run POST)
    are wrapped independently. If the DDB write succeeds and the
    Check Run POST fails, the handler still returns success: the
    verdict row already satisfies Invariant 2 (customer-visible
    verdict exists) and the dashboard picks it up on the next poll
    (design.md §4.5 partial-success semantics). If the DDB write
    fails with anything other than ``ConditionalCheckFailedException``,
    the exception propagates so SQS returns the message to the queue
    for one more retry attempt before hitting the DLQ.

    Idempotency: the ``ConditionExpression`` is byte-identical to
    Spec 2's ``trikon_verdicts`` writer (Requirement 7.4). A
    redelivered terminal failure produces the same synthetic row on
    the first attempt and a ``ConditionalCheckFailedException`` on
    subsequent attempts — logged INFO and treated as success. A
    redelivery MAY produce a second neutral Check Run (GitHub's
    Check Run API is not deduplicating); Invariant 2 only requires
    *at least one* customer-visible verdict, so this is acceptable
    (design.md §7.1).

    Parameters
    ----------
    sqs_message:
        The validated inbound job message. All identity fields on the
        synthetic row and the Check Run come from here.
    error_class:
        Short classification token embedded in ``evidence_blob``.
    error_code:
        AWS ``ClientError`` code embedded in ``evidence_blob``.
    boto3_session:
        The cold-start-cached :class:`boto3.session.Session` from
        which this function derives the DynamoDB client. A session
        (rather than a pre-built client) is passed so the handler can
        share credentials / region across the DDB + SSM
        touchpoints (design.md §4.1 module-scope caches).
    github_client:
        The cold-start-cached :class:`OrchestratorGithubClient`.
    env:
        The cold-start-cached :class:`OrchestratorEnvConfig` — used
        for the verdicts-table name and the Check Run details-URL
        template.
    duration_ms:
        Wall-clock milliseconds from handler entry to this call.
    notice:
        Optional Fail_Closed_Result copy and evidence. ``None`` keeps
        the RunTask-terminal behaviour.
    complete_check_run_id:
        Optional id of an existing Check Run (the Interim_Check_Run of
        a Retry_Job) to PATCH to ``completed`` instead of POSTing a new
        one, falling back to the POST when the PATCH fails
        (:func:`_post_or_complete_check_run`; cost-cuts Req 11.5).
        Partial-success semantics are unchanged. ``None`` (the default)
        keeps the behaviour above byte for byte.
    """
    logger = get_logger()

    # Step 1 — Build the row (pure).
    if notice is None:
        row = build_synthetic_verdict_row(
            sqs_message=sqs_message,
            error_class=error_class,
            error_code=error_code,
            duration_ms=duration_ms,
        )
    else:
        row = build_synthetic_verdict_row(
            sqs_message=sqs_message,
            error_class=notice.error_class,
            error_code=notice.error_code,
            duration_ms=duration_ms,
            matched_rule=notice.matched_rule,
        )

    # Step 2 — DynamoDB PutItem with natural-key idempotency guard.
    # ``boto3_session.client("dynamodb")`` returns ``Any`` (boto3 is
    # untyped upstream). Cast to the local Protocol so the method
    # body stays type-checked without a runtime dependency on
    # ``boto3-stubs``.
    ddb_client: _DynamoDbClientProtocol = cast(
        _DynamoDbClientProtocol, boto3_session.client("dynamodb"),
    )
    item = _to_dynamodb_item(row)
    try:
        ddb_client.put_item(
            TableName=env.trikon_verdicts_table,
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(installation_id) "
                "AND attribute_not_exists(sk)"
            ),
        )
    except ClientError as exc:
        # Step 3 — Idempotency win: the row already exists from a
        # prior redelivered failure. Log INFO and return; the Check
        # Run POST does NOT retry either — the first delivery
        # already produced a customer-visible verdict pair.
        aws_error_code = _extract_client_error_code(exc)
        if aws_error_code == "ConditionalCheckFailedException":
            logger.info("verdict_already_exists", sk=row.sk)
            return
        # Any other ClientError propagates — SQS returns the message
        # for one more attempt before DLQ.
        raise

    # Step 4 — Log the successful DynamoDB write.
    logger.info("orchestrator_verdict_written", sk=row.sk)

    # Step 5 — POST the Check Run (neutral by default, or the
    # notice's copy). Failures are logged at ERROR without
    # re-raising (partial-success semantics).
    try:
        if complete_check_run_id is not None:
            # Spot Retry_Job path (cost-cuts Req 11.5): complete the
            # Interim_Check_Run, or create one when the PATCH fails.
            effective_notice = (
                notice
                if notice is not None
                else _terminal_notice(sqs_message=sqs_message, env=env)
            )
            _post_or_complete_check_run(
                github_client,
                FailClosedTarget(
                    installation_id=sqs_message.installation_id,
                    repo_full_name=sqs_message.repo_full_name,
                    head_sha=sqs_message.head_sha,
                    pr_number=sqs_message.pr_number,
                ),
                effective_notice,
                complete_check_run_id,
            )
        elif notice is None:
            details_url = env.trikon_check_run_details_url_template.format(
                delivery_id=sqs_message.delivery_id,
            )
            github_client.create_neutral_check_run(
                installation_id=sqs_message.installation_id,
                repo_full_name=sqs_message.repo_full_name,
                head_sha=sqs_message.head_sha,
                details_url=details_url,
            )
        else:
            github_client.create_check_run(
                installation_id=sqs_message.installation_id,
                repo_full_name=sqs_message.repo_full_name,
                head_sha=sqs_message.head_sha,
                title=notice.title,
                summary=notice.summary,
                conclusion=notice.conclusion,
                details_url=notice.details_url,
            )
    except OrchestratorGithubClientError as exc:
        # Body / credential bytes are NOT in ``exc``'s message by
        # construction (see :meth:`_request_with_retry`); logging
        # the type name and message is Invariant-6 safe.
        logger.error(
            "check_run_post_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    # Step 6 — Log resolution regardless of Check Run outcome. The
    # DDB row is the source of truth; this log record marks the
    # Never-Fail-Open path as complete for the audit trail.
    logger.info("orchestrator_failure_resolved")


# ---------------------------------------------------------------------------
# Spot-related Fail_Closed_Results (trikon-cloud-cost-cuts, Requirement 11).
# ---------------------------------------------------------------------------

# Same natural-key guard as Spec 2's writer and
# :func:`write_orchestrator_failure_verdict` (Requirement 7.4).
_VERDICT_CONDITION_EXPRESSION: str = (
    "attribute_not_exists(installation_id) AND attribute_not_exists(sk)"
)


def _terminal_notice(
    *, sqs_message: SqsJobMessage, env: OrchestratorEnvConfig
) -> FailClosedNotice:
    """The RunTask-terminal copy as a notice, for the PATCH path only.

    Used when :func:`write_orchestrator_failure_verdict` gets a
    ``complete_check_run_id`` but no notice. The copy, conclusion and
    details URL equal :meth:`OrchestratorGithubClient.create_neutral_check_run`.
    """
    details_url = env.trikon_check_run_details_url_template.format(
        delivery_id=sqs_message.delivery_id,
    )
    return FailClosedNotice(
        matched_rule=_TERMINAL_MATCHED_RULE,
        title=_CHECK_RUN_TITLE,
        summary=_CHECK_RUN_SUMMARY_TEMPLATE.format(
            delivery_id=_extract_delivery_id_hint(details_url)
        ),
        details_url=details_url,
        conclusion="neutral",
        error_class="terminal",
        error_code="",
    )


def _post_or_complete_check_run(
    github_client: OrchestratorGithubClient,
    target: FailClosedTarget,
    notice: FailClosedNotice,
    complete_check_run_id: int | None,
) -> Literal["created", "completed"]:
    """Complete an existing Check Run, else create one.

    With ``complete_check_run_id``, PATCH it to ``completed`` /
    ``notice.conclusion`` and return ``"completed"``. When that PATCH
    raises :class:`OrchestratorGithubClientError` (deleted run, 404,
    retry budget), or when no id is given, POST a new Check Run on
    ``target.head_sha`` and return ``"created"``. A POST failure
    propagates. Does not log.
    """
    if complete_check_run_id is not None:
        try:
            github_client.update_check_run(
                installation_id=target.installation_id,
                repo_full_name=target.repo_full_name,
                check_run_id=complete_check_run_id,
                title=notice.title,
                summary=notice.summary,
                conclusion=notice.conclusion,
                details_url=notice.details_url,
            )
        except OrchestratorGithubClientError:
            pass
        else:
            return "completed"
    github_client.create_check_run(
        installation_id=target.installation_id,
        repo_full_name=target.repo_full_name,
        head_sha=target.head_sha,
        title=notice.title,
        summary=notice.summary,
        conclusion=notice.conclusion,
        details_url=notice.details_url,
    )
    return "created"


def publish_fail_closed_result(
    *,
    target: FailClosedTarget,
    row_message: SqsJobMessage | None,
    notice: FailClosedNotice,
    verdicts_table: str,
    ddb_client: _DynamoDbClientProtocol,
    github_client: OrchestratorGithubClient,
    complete_check_run_id: int | None,
) -> FailClosedPublication:
    """Strict Fail_Closed_Result for the Spot_Retry_Handler (Req 11.1-11.4, 11.6).

    1. If ``row_message`` is not ``None``, build the synthetic
       ``require_human`` row with ``notice.matched_rule`` and the
       notice's evidence, and PutItem it with the natural-key
       condition. ``ConditionalCheckFailedException`` gives
       ``row="existed"`` (idempotent re-delivery); any other
       ``ClientError`` propagates. Without a row message, ``row`` is
       ``"skipped"``.
    2. Always post the Check Run, also after ``"existed"``, so a
       retried event whose first Check Run POST failed still
       publishes (:func:`_post_or_complete_check_run`). Unlike the
       partial-success :func:`write_orchestrator_failure_verdict`, an
       :class:`OrchestratorGithubClientError` propagates, so the
       Lambda event is retried and finally dead-lettered (Req 11.6).

    Does not log; the caller logs fixed event names.
    """
    row_outcome: Literal["written", "existed", "skipped"] = "skipped"
    if row_message is not None:
        row = build_synthetic_verdict_row(
            sqs_message=row_message,
            error_class=notice.error_class,
            error_code=notice.error_code,
            matched_rule=notice.matched_rule,
        )
        try:
            ddb_client.put_item(
                TableName=verdicts_table,
                Item=_to_dynamodb_item(row),
                ConditionExpression=_VERDICT_CONDITION_EXPRESSION,
            )
        except ClientError as exc:
            if _extract_client_error_code(exc) != "ConditionalCheckFailedException":
                raise
            row_outcome = "existed"
        else:
            row_outcome = "written"
    check_run = _post_or_complete_check_run(
        github_client, target, notice, complete_check_run_id
    )
    return FailClosedPublication(row=row_outcome, check_run=check_run)


def read_interim_check_run_id(
    ddb_client: _DynamoDbGetItemProtocol,
    *,
    table: str,
    pr_key: str,
    head_sha: str,
) -> int | None:
    """Return the Interim_Check_Run id recorded on ``trikon_pr_state``.

    ``GetItem(ConsistentRead=True)`` on ``pr_key``
    (``"{installation_id}#{repo_full_name}#{pr_number}"``). Returns
    ``last_check_run_id`` only when the row's ``last_head_sha`` equals
    ``head_sha``. A missing row, a ``NULL`` or malformed id, a
    different head, or any ``ClientError`` returns ``None``, and the
    caller then creates a new Check Run.
    """
    try:
        response = ddb_client.get_item(
            TableName=table,
            Key={"pr_key": {"S": pr_key}},
            ConsistentRead=True,
        )
    except ClientError:
        return None
    item = response.get("Item")
    if not isinstance(item, Mapping):
        return None
    head_attr = item.get("last_head_sha")
    if not isinstance(head_attr, Mapping) or head_attr.get("S") != head_sha:
        return None
    id_attr = item.get("last_check_run_id")
    if not isinstance(id_attr, Mapping):
        return None
    raw_id = id_attr.get("N")
    if not isinstance(raw_id, str):
        return None
    try:
        check_run_id = int(raw_id)
    except ValueError:
        return None
    return check_run_id if check_run_id >= 1 else None


def _extract_client_error_code(exc: ClientError) -> str:
    """Return the ``Error.Code`` string from a :class:`ClientError`.

    ``botocore.exceptions.ClientError.response`` is untyped upstream
    (returns ``Any``); this helper narrows it to a safe ``str`` for
    the caller. Returns an empty string on any missing key so the
    caller's equality check falls through to the re-raise branch.
    """
    response_obj = exc.response
    if not isinstance(response_obj, dict):
        return ""
    error_obj = response_obj.get("Error", {})
    if not isinstance(error_obj, dict):
        return ""
    code_obj = error_obj.get("Code", "")
    if not isinstance(code_obj, str):
        return ""
    return code_obj
