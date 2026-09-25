# The Fargate task-metadata endpoint (``ECS_CONTAINER_METADATA_URI_V4``) and
# the Secrets Manager ``GetSecretValue`` shape are ``Any``-typed at the boto3
# / httpx boundary. Under this repo's ``disallow_any_explicit = true`` mypy
# config those annotations surface as ``explicit-any`` errors. The ``Any``
# uses in this module are scoped to those two upstream JSON shapes and to
# the ``log: Any`` param on the fail-closed helper (structlog's
# ``BoundLogger`` proxy is dynamic). Silence at the file level — every
# non-``Any`` boundary continues to be typed precisely.
# mypy: disable-error-code="explicit-any"
"""Docker ENTRYPOINT for the Trikon Cloud Fargate verify-runner (design.md §4).

The runner is a thin composition layer over the seven sibling modules
under :mod:`trikon_cloud.fargate_runner`. It orchestrates the 12-step
flow from design.md §4 exactly once per Fargate task launch:

  1. Load env config (:class:`RunnerEnvConfig`).
  2. Fetch App private key from Secrets Manager.
  3. Mint JIT installation token (cached for the task lifetime).
  4. Shallow-fetch ``head_sha`` + ``base_sha`` into ``/tmp/repo``.
  5. Invoke :func:`trikon.sdk.verify` with ``no_sandbox=True``.
  6. Read prior ``pr_state`` row from DynamoDB.
  7. Write verdict via conditional PutItem (``ConditionalCheckFailedException``
     is treated as an idempotency win — Property 1).
  8. Spill evidence to S3 when the gzipped blob exceeds ~350 KB.
  9. Render Markdown summary EXACTLY ONCE (Property 3), then post or
     PATCH the ``Trikon`` Check Run.
 10. Post or PATCH the byte-identical PR comment (best-effort).
 11. Upsert the ``pr_state`` row.
 12. Return 0.

Any exception raised between step 1 and step 11 triggers the
:func:`_fail_closed` handler — synthesizes a ``require_human``
verdict, best-effort persists + posts, returns 1
(``Never_Fail_Open_Contract`` — Property 2 / §12 path 14).

A :data:`signal.SIGALRM` handler installed at process start enforces
the 10-minute wall-time cap from Invariant 5 (§12 path 13).

Public surface:

* :func:`main` — the ``python -m trikon_cloud.fargate_runner.entrypoint``
  entry.

**Security invariants** (Invariant 6): the App private key PEM, the
signed JWT, the minted installation token, the Secrets Manager
``GetSecretValue`` response body, and the gzipped evidence blob bytes
are never logged at any level.

**SDK boundary** (Invariant 4): the runner imports from
:mod:`trikon.sdk` and :mod:`trikon.evidence.report` but never
monkeypatches, subclasses, or shadows any symbol therein. Every
runner-side computation on the returned :class:`~trikon.evidence.report.Verdict`
(``blast_radius_score`` integer bucket, ``matched_rule`` default) is
a local read.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from types import FrameType
from typing import Any, cast
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
import httpx
import structlog.contextvars
from pydantic import ValidationError

import trikon.sdk
from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    EMPTY_VERIFICATION,
    Evidence,
    Verdict,
)
from trikon_cloud.fargate_runner import (
    dynamodb_writer,
    git_ops,
    github_client,
    summary_builder,
    token_cache,
)
from trikon_cloud.fargate_runner import logger as runner_logger
from trikon_cloud.fargate_runner.models import (
    CheckRunCreatePayload,
    CheckRunOutput,
    CheckRunUpdatePayload,
    PrStateRow,
    RunnerEnvConfig,
    VerdictRow,
)

__all__ = ["main"]


# ---------------------------------------------------------------------------
# Module-level constants.
# ---------------------------------------------------------------------------

# Evidence blobs are gzipped and stored inline in DynamoDB when their
# compressed size stays under this threshold; anything larger spills to
# S3 (design.md §3.9 clarify answer 5). DynamoDB's per-item limit is
# 400 KB — this leaves ~50 KB headroom for the surrounding attributes
# and DynamoDB's per-attribute overhead.
_EVIDENCE_INLINE_LIMIT_BYTES = 350 * 1024

# Wall-time cost cap enforced via ``signal.alarm`` at process start
# (Invariant 5 — 10 minute Fargate cap). §12 path 13.
_TASK_WALL_TIME_CAP_SECONDS = 600

# Decision → GitHub Check Run ``conclusion`` mapping. ``require_human``
# maps to ``neutral`` because the customer-visible surface is
# "reviewer must decide" — not a build failure.
_CONCLUSION_BY_DECISION: dict[str, str] = {
    "allow": "success",
    "block": "failure",
    "require_human": "neutral",
}


# ---------------------------------------------------------------------------
# Entrypoint.
# ---------------------------------------------------------------------------


def main() -> int:
    """Docker ENTRYPOINT for the Trikon Cloud Fargate verify-runner.

    Executes the 12-step flow from design.md §4. Any exception between
    step 1 and step 11 is caught by the top-level handler and routed
    through :func:`_fail_closed` — synthetic ``require_human`` verdict,
    best-effort persist + post, exit 1 (Property 2).

    Returns:
        0 on success (including the ``ConditionalCheckFailedException``
        idempotency win from §12 path 7 and the two best-effort-degrade
        paths §12 paths 11 and 12).
        1 on any failure path (§12 paths 1-6, 8-10, 13, 14).
    """
    # Step 0: install the wall-time cap alarm. ``SIGALRM`` fires after
    # 10 minutes; :func:`_on_alarm` raises ``TimeoutError`` which the
    # top-level ``except`` catches and routes into ``_fail_closed``.
    # ``signal.SIGALRM`` / ``signal.alarm`` are POSIX-only; the runner
    # ships in a Linux container so the attribute-defined check is a
    # non-issue at runtime. mypy on a Windows dev host does not see
    # them — hence the local ``type: ignore``.
    signal.signal(signal.SIGALRM, _on_alarm)  # type: ignore[attr-defined]
    signal.alarm(_TASK_WALL_TIME_CAP_SECONDS)  # type: ignore[attr-defined]

    container_start_time = time.monotonic()
    # ``pr_ts`` is captured ONCE at container start (Requirement 5.5).
    # A retry of the same invocation (SQS re-drive, ECS task retry)
    # inside the same container reuses the same ``pr_ts`` so the
    # ``sk`` (``<pr_ts>#<audit_id>``) is stable across retries — the
    # DynamoDB conditional PutItem's ``ConditionalCheckFailedException``
    # is the load-bearing idempotency check for Property 1.
    pr_ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # Step 1: env config. On failure we route to ``_fail_closed_no_env``
    # (§12 path 1) — without the env we have no Secrets Manager ARN,
    # no installation ID, and no way to write a DynamoDB row or post to
    # GitHub, so the best we can do is emit a stderr diagnostic and
    # exit 1.
    try:
        env = RunnerEnvConfig()
    except ValidationError as exc:
        return _fail_closed_no_env(exc, pr_ts, container_start_time)

    # Configure structlog + bind the request-scoped contextvars so
    # every downstream log record carries the invocation identity.
    runner_logger.configure_logging(log_level=env.log_level)
    log = runner_logger.get_logger("trikon_cloud.fargate_runner.entrypoint")
    structlog.contextvars.bind_contextvars(
        delivery_id=env.delivery_id,
        installation_id=env.installation_id,
        repo_full_name=env.repo_full_name,
        pr_number=env.pr_number,
        head_sha=env.head_sha,
    )
    log.info("entrypoint start", step=1)

    # Top-level try / except wraps steps 2-11. Any exception routes
    # through :func:`_fail_closed` (Property 2 / §12 paths 2-14).
    try:
        # Step 2: fetch the App private key from Secrets Manager. The
        # PEM never enters any log record or exception message.
        log.info("fetching app private key", step=2)
        private_key_pem = _fetch_app_private_key(env.app_private_key_secret_arn)

        # Step 3: mint a JIT installation token, cached for the task
        # lifetime (design.md §3.9 clarify answer 3).
        log.info("minting installation token", step=3)
        gh_client = github_client.GithubClient()
        cache = token_cache.get_cache()
        installation_token = cache.get_or_mint(
            minter=lambda: gh_client.mint_installation_token(
                installation_id=env.installation_id,
                app_id=env.app_id,
                private_key_pem=private_key_pem,
            )
        )

        # Step 4: shallow-fetch the repo into ``/tmp/repo``.
        log.info("shallow-fetching repo", step=4)
        git_ops.shallow_fetch_and_checkout(
            repo_full_name=env.repo_full_name,
            head_sha=env.head_sha,
            base_sha=env.base_sha,
            installation_token=installation_token,
            working_dir=env.repo_working_dir,
        )

        # Step 5: invoke the SDK with ``no_sandbox=True`` — the Fargate
        # task IS the sandbox (design.md §3.9 clarify answer 1).
        log.info("invoking sdk.verify", step=5)
        verdict = trikon.sdk.verify(
            repo_path=env.repo_working_dir,
            base_sha=env.base_sha,
            head_sha=env.head_sha,
            no_sandbox=True,
            policy_path=_resolve_policy_path(env.repo_working_dir),
        )

        # Step 6: read the prior pr_state row (may be ``None`` on the
        # first invocation for this PR).
        log.info("reading pr_state", step=6, audit_id=str(verdict.audit_id))
        db = dynamodb_writer.DynamoDBWriter(
            verdicts_table_name=env.verdicts_table_name,
            pr_state_table_name=env.pr_state_table_name,
            evidence_bucket_name=env.evidence_bucket_name,
        )
        prior_pr_state = db.get_pr_state(
            installation_id=env.installation_id,
            repo_full_name=env.repo_full_name,
            pr_number=env.pr_number,
        )

        # Steps 7 + 8: write the verdict row. The verdict-row builder
        # owns the inline-vs-spill decision at the 350 KB guardrail.
        log.info("writing verdict to DynamoDB", step=7)
        duration_ms = int((time.monotonic() - container_start_time) * 1000)
        verdict_row = _build_verdict_row(
            verdict=verdict,
            env=env,
            pr_ts=pr_ts,
            duration_ms=duration_ms,
            db=db,
        )
        try:
            db.put_verdict(row=verdict_row)
        except dynamodb_writer.VerdictAlreadyCommittedError:
            # §12 path 7: idempotency win. A prior invocation for the
            # same ``(installation_id, sk)`` pair already committed
            # the verdict AND posted to GitHub — this run exits 0
            # without re-posting (Requirement 5.3, Property 1).
            log.info(
                "verdict already committed by prior invocation; skipping GitHub post",
                audit_id=str(verdict.audit_id),
            )
            return 0

        # Step 9a: render the Markdown summary EXACTLY ONCE
        # (Property 3 — ``Summary_Content_Parity``). The single
        # ``summary`` local is passed byte-identical to both the
        # Check Run output and the PR comment body below.
        log.info("rendering summary", step=9)
        audit_url = env.check_run_details_url_template.format(
            audit_id=verdict.audit_id
        )
        summary = summary_builder.render_summary(
            verdict=verdict,
            audit_url=audit_url,
            sdk_version=_get_sdk_version(),
            duration_ms=duration_ms,
        )
        check_run_title = summary.split("\n", 1)[0]

        # Step 9b: post or PATCH the Check Run. PATCH when the prior
        # invocation posted on the same head_sha (design.md §3.9
        # clarify answer 7).
        log.info("posting Check Run", step=9)
        conclusion = _CONCLUSION_BY_DECISION[verdict.decision]
        check_run_output = CheckRunOutput(title=check_run_title, summary=summary)
        check_run_id: int
        if (
            prior_pr_state is not None
            and prior_pr_state.last_check_run_id is not None
            and prior_pr_state.last_head_sha == env.head_sha
        ):
            patch_body = CheckRunUpdatePayload(
                status="completed",
                conclusion=conclusion,
                output=check_run_output,
                details_url=audit_url,
            )
            patched = gh_client.patch_check_run(
                installation_token=installation_token,
                repo_full_name=env.repo_full_name,
                check_run_id=prior_pr_state.last_check_run_id,
                body=patch_body,
            )
            check_run_id = patched.id
        else:
            create_body = CheckRunCreatePayload(
                name="Trikon",
                head_sha=env.head_sha,
                status="completed",
                conclusion=conclusion,
                output=check_run_output,
                details_url=audit_url,
            )
            created = gh_client.create_check_run(
                installation_token=installation_token,
                repo_full_name=env.repo_full_name,
                body=create_body,
            )
            check_run_id = created.id

        # Step 10: post or PATCH the PR comment. Best-effort per
        # Requirement 8.5 — a comment failure logs ERROR and continues
        # (the Check Run is the primary customer-visible surface;
        # §12 path 11).
        log.info("posting PR comment", step=10)
        comment_id: int | None
        try:
            if (
                prior_pr_state is not None
                and prior_pr_state.last_comment_id is not None
            ):
                patched_comment = gh_client.patch_pr_comment(
                    installation_token=installation_token,
                    repo_full_name=env.repo_full_name,
                    comment_id=prior_pr_state.last_comment_id,
                    body=summary,
                )
                comment_id = patched_comment.id
            else:
                created_comment = gh_client.create_pr_comment(
                    installation_token=installation_token,
                    repo_full_name=env.repo_full_name,
                    pr_number=env.pr_number,
                    body=summary,
                )
                comment_id = created_comment.id
        except github_client.GithubClientError as exc:
            log.error(
                "PR comment post failed; continuing with Check Run only",
                error_type=type(exc).__name__,
            )
            # Preserve the prior comment_id if any (best-effort
            # degrade — next invocation will retry the update).
            comment_id = (
                prior_pr_state.last_comment_id if prior_pr_state is not None else None
            )

        # Step 11: upsert the pr_state row so the next invocation can
        # PATCH-in-place. §12 path 12: a failure here does not undo
        # the committed verdict or the posted GitHub artifacts, so we
        # log ERROR and return 0.
        log.info("upserting pr_state", step=11)
        try:
            db.upsert_pr_state(
                row=PrStateRow(
                    pr_key=env.pr_state_key,
                    last_comment_id=comment_id,
                    last_check_run_id=check_run_id,
                    last_head_sha=env.head_sha,
                    last_updated_at=datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                )
            )
        except dynamodb_writer.VerdictWriteError as exc:
            log.error(
                "pr_state upsert failed; continuing (verdict already committed)",
                error_type=type(exc).__name__,
            )

        # Step 12: success.
        log.info("entrypoint success", step=12, exit_code=0)
        return 0

    except Exception as exc:  # top-level Never_Fail_Open catchall (Property 2)
        return _fail_closed(exc, env, pr_ts, container_start_time, log)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _on_alarm(signum: int, frame: FrameType | None) -> None:
    """SIGALRM handler — raise :class:`TimeoutError` to trip the top-level except.

    The raised exception is caught by :func:`main`'s outer
    ``try/except`` and routed through :func:`_fail_closed` (§12 path
    13). ``signum`` and ``frame`` are part of the signal-handler
    protocol and are not consulted.
    """
    del signum, frame
    raise TimeoutError("exceeded 10-minute cap")


def _fetch_app_private_key(secret_arn: str) -> bytes:
    """Fetch the GitHub App private key PEM from Secrets Manager.

    Returns the PEM as bytes. The returned value NEVER enters a log
    record or an exception message — Invariant 6. A ``ClientError``
    from ``GetSecretValue`` propagates to :func:`main`'s top-level
    ``except`` (§12 path 2).
    """
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret_str: str = cast(str, response["SecretString"])
    return secret_str.encode("utf-8")


def _resolve_policy_path(working_dir: Path) -> Path:
    """Return the ``.trikon/policy.yaml`` path under ``working_dir``.

    The SDK's :func:`trikon.sdk.verify` accepts either an existing
    policy path or a non-existent one — when the file is missing, the
    SDK's policy loader falls back to the packaged default per
    Requirement 1.3 and the SDK's own docstring. Passing the resolved
    path unconditionally keeps the caller's ``policy_path`` argument
    typed as :class:`~pathlib.Path` (the SDK signature is
    ``Path | str``, no ``None``).
    """
    return working_dir / ".trikon" / "policy.yaml"


def _get_sdk_version() -> str:
    """Return the installed :mod:`trikon` SDK version.

    Reads via :func:`importlib.metadata.version`. Falls back to
    ``"unknown"`` on :class:`~importlib.metadata.PackageNotFoundError`
    so the summary footer still renders on a broken install rather
    than raising into the Never_Fail_Open path for a purely cosmetic
    field.
    """
    try:
        return metadata.version("trikon")
    except metadata.PackageNotFoundError:
        return "unknown"


def _get_fargate_task_arn() -> str:
    """Return the ECS task ARN via the container metadata endpoint.

    Reads ``$ECS_CONTAINER_METADATA_URI_V4`` and issues a HTTP GET
    against the ``/task`` sub-endpoint. Returns ``"local"`` when the
    env var is absent (local dev / unit tests) or the HTTP call fails
    for any reason. This helper is deliberately non-raising — a
    metadata-endpoint hiccup is not a reason to trip the fail-closed
    path, since the ARN is a diagnostic-only field in the verdict row.
    """
    metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if metadata_uri is None:
        return "local"
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
            response = client.get(f"{metadata_uri}/task")
            if response.status_code == 200:
                data: dict[str, Any] = response.json()
                return cast(str, data.get("TaskARN", "local"))
    except httpx.HTTPError:
        pass
    return "local"


def _build_verdict_row(
    *,
    verdict: Verdict,
    env: RunnerEnvConfig,
    pr_ts: str,
    duration_ms: int,
    db: dynamodb_writer.DynamoDBWriter,
) -> VerdictRow:
    """Build a :class:`VerdictRow` from a :class:`Verdict`.

    Owns the inline-vs-S3-spill decision at the 350 KB compressed
    guardrail. Computes:

    * ``sk`` — the ``<pr_ts>#<audit_id>`` composite (design.md §5.2).
    * ``blast_radius_score`` — floor of
      :attr:`~trikon.evidence.report.ImpactSet.blast_radius_numeric`.
    * ``risk_bucket_sk`` — the GSI2 sort key
      (``<bucket>#<pr_ts>``, ``bucket`` = 4-digit zero-padded floored
      blast radius capped at 9999).
    * ``evidence_blob`` XOR ``evidence_s3_key`` — one is non-``None``
      per the :class:`VerdictRow` model_validator.
    """
    blast_int = int(verdict.evidence.change.blast_radius_numeric)
    risk_bucket_sk = f"{min(blast_int, 9999):04d}#{pr_ts}"

    evidence_json = verdict.evidence.model_dump_json()
    gzipped_evidence = db.compute_evidence_blob(evidence_json=evidence_json)

    evidence_blob: bytes | None
    evidence_s3_key: str | None
    if len(gzipped_evidence) <= _EVIDENCE_INLINE_LIMIT_BYTES:
        evidence_blob = gzipped_evidence
        evidence_s3_key = None
    else:
        evidence_blob = None
        evidence_s3_key = db.spill_evidence_to_s3(
            installation_id=env.installation_id,
            audit_id=verdict.audit_id,
            gzipped_bytes=gzipped_evidence,
        )

    return VerdictRow(
        installation_id=env.installation_id,
        sk=f"{pr_ts}#{verdict.audit_id}",
        repo_full_name=env.repo_full_name,
        pr_number=env.pr_number,
        head_sha=env.head_sha,
        base_sha=env.base_sha,
        decision=verdict.decision,
        matched_rule=verdict.matched_rule or "default",
        blast_radius_score=blast_int,
        new_errors=verdict.evidence.verification.static.new_errors,
        new_warnings=verdict.evidence.verification.static.new_warnings,
        preexisting_errors=verdict.evidence.verification.static.preexisting_errors,
        duration_ms=duration_ms,
        fargate_task_arn=_get_fargate_task_arn(),
        schema_version=verdict.schema_version,
        evidence_blob=evidence_blob,
        evidence_s3_key=evidence_s3_key,
        risk_bucket_sk=risk_bucket_sk,
    )


def _synthesize_fail_closed_verdict(
    *, exc: BaseException, env: RunnerEnvConfig
) -> Verdict:
    """Synthesize a ``require_human`` :class:`Verdict` for the fail-closed path.

    Mirrors the SDK's own ``_fail_closed_verdict`` shape: an empty
    :class:`~trikon.evidence.report.ImpactSet` bucketed ``HIGH`` (see
    :data:`~trikon.evidence.report.EMPTY_IMPACT_SET`) and a skipped
    :class:`~trikon.evidence.report.VerificationReport`
    (:data:`~trikon.evidence.report.EMPTY_VERIFICATION`). The
    ``reason`` names the exception class only (never the traceback or
    ``str(exc)``, which may carry PII — Requirement 10.7).
    """
    del env  # reserved for future context enrichment
    return Verdict(
        decision="require_human",
        reason=f"internal error: {type(exc).__name__}",
        matched_rule=None,
        evidence=Evidence(
            change=EMPTY_IMPACT_SET,
            verification=EMPTY_VERIFICATION,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
        warnings=[],
        schema_version=2,
    )


def _fail_closed(
    exc: BaseException,
    env: RunnerEnvConfig,
    pr_ts: str,
    container_start_time: float,
    log: Any,
) -> int:
    """Never_Fail_Open handler (Property 2 / §12 paths 2-14).

    Best-effort synthesizes a ``require_human`` verdict, writes it to
    DynamoDB, posts a ``require_human`` Check Run, and returns 1. Any
    failure inside the best-effort path is logged ERROR but does NOT
    propagate — the exit-1 return is invariant per §12. A last-line
    ``except`` catches a raise from inside the fail-closed handler
    itself so the runner always returns an int rather than crashing
    the container (which would leave ECS to synthesize a stopped
    reason with less context).
    """
    log.error(
        "entrypoint fail-closed",
        error_type=type(exc).__name__,
    )
    try:
        synth = _synthesize_fail_closed_verdict(exc=exc, env=env)
        duration_ms = int((time.monotonic() - container_start_time) * 1000)

        # Best-effort DynamoDB write. §12 states the verdict-row
        # persist may itself fail (e.g., throttling exhausted). We
        # log and continue — the invariant is exit 1, not persist
        # success.
        try:
            db = dynamodb_writer.DynamoDBWriter(
                verdicts_table_name=env.verdicts_table_name,
                pr_state_table_name=env.pr_state_table_name,
                evidence_bucket_name=env.evidence_bucket_name,
            )
            row = _build_verdict_row(
                verdict=synth,
                env=env,
                pr_ts=pr_ts,
                duration_ms=duration_ms,
                db=db,
            )
            db.put_verdict(row=row)
        except Exception as write_exc:  # best-effort persist (Property 2)
            log.error(
                "fail-closed DynamoDB write failed",
                error_type=type(write_exc).__name__,
            )

        # Best-effort Check Run post. The runner re-mints the
        # installation token here because the original mint may have
        # been the failing step — the token cache is safe to reuse
        # if it was populated on a prior step. The Check Run
        # ``name="Trikon"`` is fixed by ``Literal["Trikon"]`` on
        # :class:`CheckRunCreatePayload` (Invariant 7).
        try:
            private_key_pem = _fetch_app_private_key(env.app_private_key_secret_arn)
            gh_client = github_client.GithubClient()
            cache = token_cache.get_cache()
            installation_token = cache.get_or_mint(
                minter=lambda: gh_client.mint_installation_token(
                    installation_id=env.installation_id,
                    app_id=env.app_id,
                    private_key_pem=private_key_pem,
                )
            )
            audit_url = env.check_run_details_url_template.format(
                audit_id=synth.audit_id
            )
            summary = summary_builder.render_summary(
                verdict=synth,
                audit_url=audit_url,
                sdk_version=_get_sdk_version(),
                duration_ms=duration_ms,
            )
            title = summary.split("\n", 1)[0]
            gh_client.create_check_run(
                installation_token=installation_token,
                repo_full_name=env.repo_full_name,
                body=CheckRunCreatePayload(
                    name="Trikon",
                    head_sha=env.head_sha,
                    status="completed",
                    conclusion="neutral",
                    output=CheckRunOutput(title=title, summary=summary),
                    details_url=audit_url,
                ),
            )
        except Exception as post_exc:  # best-effort post (Property 2)
            log.error(
                "fail-closed Check Run post failed",
                error_type=type(post_exc).__name__,
            )
    except Exception as final_exc:  # absolute fallback (Property 2 invariant)
        # Something in the fail-closed handler itself raised (e.g., the
        # synthetic-verdict builder chokes on a corrupt env). Log and
        # exit 1 — no other action possible without risking an
        # infinite fail-closed recursion.
        log.error(
            "fail-closed handler raised",
            error_type=type(final_exc).__name__,
        )
    return 1


def _fail_closed_no_env(
    exc: BaseException, pr_ts: str, container_start_time: float
) -> int:
    """Fail-closed when :class:`RunnerEnvConfig` itself failed to load.

    §12 path 1: no env → no Secrets Manager ARN, no installation ID,
    no way to write a DynamoDB row or post to GitHub. Structlog is
    not configured yet (that happens after env loads), so we emit a
    single-line JSON diagnostic directly to stderr and exit 1.
    ``pr_ts`` and ``container_start_time`` are unused here but kept
    in the signature so the call site in :func:`main` has a single
    shape whether env-load succeeds or fails.
    """
    del pr_ts, container_start_time
    print(
        '{"level":"error","event":"entrypoint fail-closed (env config)",'
        f'"error_type":"{type(exc).__name__}"}}',
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
