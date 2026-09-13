"""Public Python SDK entry point — Phase 3.

The CLI, MCP tool, GitHub App, and GitHub Actions integration are all thin
wrappers around :func:`verify`. This is the one code path.

Phase 3 scope
-------------

Phase 3 adds the Policy Engine on top of Phase 2's Verification Runner.
:func:`verify` now runs the full change-intel → run_verification →
load_policy → evaluate_policy pipeline and returns the :class:`Verdict`
produced by :func:`~trikon.policy.evaluator.evaluate_policy` — the
decision is no longer hardcoded. Every returned Verdict — happy path
*or* fail-closed — is persisted to ``audit_log`` by
:func:`~trikon.audit_log.writer.record_verdict` before :func:`verify`
returns, so no verdict escapes without an audit trail (Requirement 7.3).

Never-fail-open
---------------

Any :class:`~trikon.exceptions.TrikonError` raised inside the pipeline
— :class:`~trikon.change_intel.errors.ChangeIntelError`,
:class:`~trikon.verify.errors.VerificationRunnerError`,
:class:`~trikon.policy.errors.PolicyLoadError`, or
:class:`~trikon.policy.errors.RuleMatchError` — is caught at the SDK
boundary and translated into a ``require_human`` verdict backed by
:data:`~trikon.evidence.report.EMPTY_IMPACT_SET` (blast-radius bucket
``HIGH``) and :data:`~trikon.evidence.report.EMPTY_VERIFICATION`. That
invariant — "an unknown change is never mistaken for a safe change" —
is Property 9 in ``design.md §17``.

:class:`~trikon.policy.errors.AuditLogError` is the one exception: the
audit write runs *outside* the fail-closed ``try``/``except`` block,
and a failure there re-raises to the caller rather than fail-closing
(Requirement 4.5, ``design.md §8.2``). A verdict without an audit
trail is a hard failure, not a silent degrade.

Usage
-----

.. code-block:: python

    from pathlib import Path
    from trikon import verify

    verdict = verify(
        repo_path=Path("/path/to/repo"),
        base_sha="abc123",
        head_sha="def456",
    )
    print(verdict.decision)  # -> real policy decision from evaluate_policy
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trikon.audit_log import ensure_audit_tables, record_verdict
from trikon.change_intel.blast_radius import compute_impact
from trikon.change_intel.diff_parser import parse_diff
from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    EMPTY_VERIFICATION,
    Evidence,
    ImpactSet,
    Verdict,
    VerificationReport,
)
from trikon.exceptions import TrikonError
from trikon.policy.evaluator import evaluate_policy
from trikon.policy.loader import load_policy
from trikon.verify.runner import run_verification

__all__ = ["verify"]


def verify(
    repo_path: Path | str,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
    *,
    policy_path: Path | str = ".trikon/policy.yaml",
    cache_db: Path | None = None,
    no_sandbox: bool = False,
) -> Verdict:
    """Run the Phase-3 Trikon pipeline against a change set.

    The function is the single public entry point every wrapper — CLI,
    GitHub App, MCP tool, GitHub Action — funnels through. Phase 3
    executes the full pipeline: change intelligence produces the
    :class:`~trikon.evidence.report.ImpactSet`, the verification runner
    produces the :class:`~trikon.evidence.report.VerificationReport`,
    the policy loader resolves the ``.trikon/policy.yaml`` (falling back
    to the packaged default when missing), and
    :func:`~trikon.policy.evaluator.evaluate_policy` grades the two
    evidence artifacts against the policy to produce the terminal
    :class:`Verdict`.

    Args:
        repo_path: Path to the git repository on disk. Accepts a
            :class:`pathlib.Path` or a ``str``.
        base_sha: Git SHA of the base commit. Passed straight to
            :func:`~trikon.change_intel.diff_parser.parse_diff`, which
            enforces the exactly-one-of-{SHAs, diff} rule.
        head_sha: Git SHA of the head commit. Same as ``base_sha``.
        diff: Unified-diff string. Mutually exclusive with SHA pair.
        policy_path: Path to the policy YAML. Resolved against
            ``repo_path`` when relative; absolute paths pass through
            unchanged. Missing file falls back to the packaged
            :func:`~trikon.policy.loader.default_policy` (Requirement 2.2).
        cache_db: Path to the SQLite state database. Forwarded to both
            :func:`~trikon.change_intel.blast_radius.compute_impact` and
            :func:`~trikon.verify.runner.run_verification`, and used as
            the target of the audit-log write below; each defaults to
            ``<repo>/.trikon/state.db`` when ``None`` (the same file
            Phase 1 uses — see ``design.md §4``).
        no_sandbox: When ``True``, verification runs on the host through
            :class:`~trikon.verify.local_sandbox.LocalSubprocessSandbox`
            instead of the Docker-backed
            :class:`~trikon.verify.sandbox.LocalDockerSandbox`. Threaded
            straight through to :func:`run_verification`. Defaults to
            ``False`` because the sandboxed backend is the only safe
            posture for verification against untrusted code. The SDK
            surfaces the parameter for parity with the CLI; the
            responsibility to display a security-warning banner when
            ``no_sandbox=True`` belongs to the CLI, not the SDK, so a
            programmatic caller integrating Trikon into a broader tool
            is expected to surface its own warning to end users.

    Returns:
        A :class:`Verdict`. Two shapes:

        * On success: the Verdict produced by
          :func:`~trikon.policy.evaluator.evaluate_policy` — real
          ``decision``, real ``matched_rule``, real ``reason``, and
          ``warnings`` accumulated from every matching warn rule in
          declaration order.
        * On any :class:`~trikon.exceptions.TrikonError` raised inside
          the pipeline (except :class:`AuditLogError`, see below):
          ``decision == "require_human"``, ``evidence.change`` is
          :data:`~trikon.evidence.report.EMPTY_IMPACT_SET` (bucket
          ``HIGH``), ``evidence.verification`` is
          :data:`~trikon.evidence.report.EMPTY_VERIFICATION`, and the
          reason string is prefixed with the failing subclass name (via
          ``type(exc).__name__``) so audit reviewers can distinguish
          ``PolicyLoadError`` / ``RuleMatchError`` /
          ``ChangeIntelError`` / ``VerificationRunnerError`` at a
          glance. Never fail-open — see the module docstring.

        On both return paths, exactly one row is appended to the
        ``audit_log`` table on ``<repo>/.trikon/state.db`` (or the
        explicit ``cache_db``) before returning; the row's ``audit_id``
        matches the returned Verdict's ``audit_id`` (Property 8,
        Requirement 7.3).

    Raises:
        AuditLogError: If the audit-log write fails (disk full,
            corrupted WAL, DDL failure). Propagated *outside* the
            fail-closed ``try``/``except`` — a verdict without an
            audit trail is a hard failure, not a silent degrade
            (Requirement 4.5, ``design.md §8.2``).
    """
    repo = Path(repo_path)

    # Track partial pipeline progress so the fail-closed path can hand real
    # evidence (whatever the pipeline managed to compute before the failure)
    # to ``_fail_closed_verdict`` instead of throwing it away. The safety
    # invariant is unchanged — ``decision`` remains ``require_human`` on any
    # ``TrikonError`` — but the embedded evidence now reflects what was
    # actually observed rather than the empty sentinels.
    impact: ImpactSet | None = None
    verification: VerificationReport | None = None

    try:
        change_set = parse_diff(
            repo,
            base_sha=base_sha,
            head_sha=head_sha,
            diff=diff,
        )
        impact = compute_impact(
            change_set,
            repo,
            cache_db=cache_db,
        )
        verification = run_verification(
            repo,
            impact,
            state_db=cache_db,
            no_sandbox=no_sandbox,
        )
        policy = load_policy(repo, Path(policy_path))
        verdict = evaluate_policy(policy, impact, verification)
    except TrikonError as exc:
        verdict = _fail_closed_verdict(
            reason=f"{type(exc).__name__}: {exc}",
            change=impact,
            verification=verification,
        )

    # Audit write is OUTSIDE the try/except (Requirement 4.5, design.md §8.2).
    # A failure here re-raises AuditLogError to the caller — a verdict without
    # an audit trail is a hard failure, not a silent degrade. Both the happy
    # path and the fail-closed path bound `verdict` above, so exactly one row
    # is persisted per sdk.verify(...) invocation (Property 8, Requirement 7.3).
    state_db = cache_db if cache_db is not None else repo / ".trikon" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_db))
    try:
        ensure_audit_tables(conn)
        record_verdict(conn, verdict)
    finally:
        conn.close()

    return verdict


def _fail_closed_verdict(
    *,
    reason: str,
    change: ImpactSet | None = None,
    verification: VerificationReport | None = None,
) -> Verdict:
    """Return the never-fail-open verdict, optionally reflecting partial progress.

    Called when the pipeline raises any :class:`TrikonError` subclass —
    :class:`ChangeIntelError` (Phase 1),
    :class:`~trikon.verify.errors.VerificationRunnerError` (Phase 2),
    or :class:`~trikon.policy.errors.PolicyLoadError` /
    :class:`~trikon.policy.errors.RuleMatchError` (Phase 3). The verdict
    is a well-formed :class:`Verdict` — every consumer that unpickles or
    JSON-parses it sees the same shape as a successful run — but the
    embedded impact set is the ``HIGH``-bucketed sentinel so any
    downstream check treats "unknown change" as maximally risky.

    ``warnings`` defaults to ``[]`` and ``schema_version`` defaults to
    ``2`` via the Pydantic model defaults added in Phase 3 Task 2.2 — no
    code edit needed here (``design.md §10.1``, §7.4).

    The ``change`` and ``verification`` overrides let callers embed
    whatever evidence the pipeline *did* manage to produce before the
    failure (e.g., a real :class:`ImpactSet` computed by
    :func:`compute_impact` before :func:`run_verification` raised
    because Docker was unavailable). ``decision="require_human"`` is
    unchanged — the safety invariant is preserved, only the evidence
    tells the truth about what was computed. When either override is
    ``None``, the corresponding empty sentinel
    (:data:`EMPTY_IMPACT_SET` / :data:`EMPTY_VERIFICATION`) is used, so
    callers that pass nothing keep the historical behavior.

    Args:
        reason: Human-readable summary of the failure. Included verbatim
            in ``Verdict.reason`` so audit logs record the exception
            class and message that tripped the fail-closed path.
        change: Optional :class:`ImpactSet` produced by an earlier
            pipeline stage. When ``None``, :data:`EMPTY_IMPACT_SET`
            (bucket ``HIGH``) is embedded instead.
        verification: Optional :class:`VerificationReport` produced by
            :func:`run_verification`. When ``None``,
            :data:`EMPTY_VERIFICATION` is embedded instead.

    Returns:
        A :class:`Verdict` with ``decision="require_human"``,
        ``matched_rule=None``, and either the caller-provided evidence
        or the empty sentinels for change and verification.
    """
    return Verdict(
        decision="require_human",
        reason=reason,
        matched_rule=None,
        evidence=Evidence(
            change=change if change is not None else EMPTY_IMPACT_SET,
            verification=verification if verification is not None else EMPTY_VERIFICATION,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )
