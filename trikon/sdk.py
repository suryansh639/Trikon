"""Public Python SDK entry point — Phase 3.

The CLI, MCP tool, GitHub App, and GitHub Actions integration are all thin
wrappers around :func:`verify`. This is the one code path.

Phase 3 scope
-------------

Phase 3 adds the Policy Engine on top of Phase 2's Verification Runner.
:func:`verify` runs this pipeline, in order:

1. :func:`~trikon.change_intel.diff_parser.parse_diff` builds the
   :class:`~trikon.change_intel.models.ChangeSet`;
2. :func:`~trikon.change_intel.blast_radius.compute_impact` builds the
   :class:`~trikon.evidence.report.ImpactSet`;
3. :func:`~trikon.change_intel.import_check_io.check_imports` builds the
   :class:`~trikon.evidence.report.ImportReport` (broken static imports of
   removed modules or names);
4. :func:`~trikon.verify.runner.run_verification` runs the Collection_Pass,
   the chosen tests, static checks and plugins. It receives the change
   set's ``base_sha``/``head_sha`` (``None`` on the diff-string path) and
   the ImportReport, which it embeds in the
   :class:`~trikon.evidence.report.VerificationReport`;
5. :func:`~trikon.policy.loader.load_policy` and
   :func:`~trikon.policy.evaluator.evaluate_policy` produce the policy
   :class:`Verdict`;
6. :func:`~trikon.policy.floor.floor_verdict` applies the Safety_Floor.
   It only ever turns ``allow`` into ``block`` (broken imports) or
   ``require_human`` (a Python change without complete test or import
   evidence). It takes no policy argument, so the default and custom
   policies are floored alike and no policy setting can disable it.

Every returned Verdict — happy path *or* fail-closed — is persisted to
``audit_log`` by :func:`~trikon.audit_log.writer.record_verdict` before
:func:`verify` returns, so no verdict escapes without an audit trail
(Requirement 7.3). The audit write comes after the floor, so the single
row holds the floored decision.

Never-fail-open
---------------

Any :class:`~trikon.exceptions.TrikonError` raised inside the pipeline
— :class:`~trikon.change_intel.errors.ChangeIntelError` (including
:class:`~trikon.change_intel.errors.ImportCheckError` from the
Import_Checker), :class:`~trikon.verify.errors.VerificationRunnerError`
(including :class:`~trikon.verify.errors.CollectionPassError` from the
Collection_Pass), :class:`~trikon.policy.errors.PolicyLoadError`, or
:class:`~trikon.policy.errors.RuleMatchError` — is caught at the SDK
boundary and translated into a ``require_human`` verdict. Stages that
finished before the failure keep their evidence; the rest fall back to
:data:`~trikon.evidence.report.EMPTY_IMPACT_SET` (blast-radius bucket
``HIGH``) and :data:`~trikon.evidence.report.EMPTY_VERIFICATION`. When
the ImportReport was computed but :func:`run_verification` did not
finish, the empty verification carries that ImportReport, so every
Verdict reports its import findings. That invariant — "an unknown change
is never mistaken for a safe change" — is Property 9 in
``design.md §17``.

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
from trikon.change_intel.import_check_io import check_imports
from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    EMPTY_VERIFICATION,
    Evidence,
    ImpactSet,
    ImportReport,
    Verdict,
    VerificationReport,
    is_python_change,
)
from trikon.exceptions import TrikonError
from trikon.policy.evaluator import evaluate_policy
from trikon.policy.floor import floor_verdict
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
    GitHub App, MCP tool, GitHub Action — funnels through. It executes
    the full pipeline: change intelligence produces the
    :class:`~trikon.evidence.report.ImpactSet`, the Import_Checker
    produces the :class:`~trikon.evidence.report.ImportReport`, the
    verification runner produces the
    :class:`~trikon.evidence.report.VerificationReport` (which embeds
    the ImportReport), the policy loader resolves the
    ``.trikon/policy.yaml`` (falling back to the packaged default when
    missing), :func:`~trikon.policy.evaluator.evaluate_policy` grades
    the evidence against the policy, and
    :func:`~trikon.policy.floor.floor_verdict` applies the Safety_Floor
    to produce the terminal :class:`Verdict` (Requirement 5.1).

    Args:
        repo_path: Path to the git repository on disk. Accepts a
            :class:`pathlib.Path` or a ``str``.
        base_sha: Git SHA of the base commit. Passed straight to
            :func:`~trikon.change_intel.diff_parser.parse_diff`, which
            enforces the exactly-one-of-{SHAs, diff} rule. The parsed
            change set's SHA is forwarded to the Import_Checker (which
            reads the Base_Tree with ``git show``) and to
            :func:`run_verification` (where only a supplied base SHA
            makes a coverage-map selection usable).
        head_sha: Git SHA of the head commit. Same as ``base_sha``.
        diff: Unified-diff string. Mutually exclusive with SHA pair.
            On this path both SHAs reach the later stages as ``None``:
            the Import_Checker rebuilds the Base_Tree by reversing the
            diff hunks, and :func:`run_verification` derives the SHAs
            from ``HEAD~1`` / ``HEAD``.
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
        A :class:`Verdict` with ``schema_version`` 3. Two shapes:

        * On success: the Verdict produced by
          :func:`~trikon.policy.evaluator.evaluate_policy` — real
          ``decision``, real ``matched_rule``, real ``reason``, and
          ``warnings`` accumulated from every matching warn rule in
          declaration order — after the Safety_Floor. When the floor
          turns an ``allow`` into ``block`` or ``require_human``,
          ``matched_rule`` is the floor rule id
          (``safety_floor.broken_imports`` or
          ``safety_floor.insufficient_evidence``), ``reason`` names the
          floor condition and the original policy decision and rule,
          and ``evidence.policy_results`` ends with the floor's
          :class:`~trikon.evidence.report.RuleResult`. Any other
          decision passes through unchanged.
        * On any :class:`~trikon.exceptions.TrikonError` raised inside
          the pipeline (except :class:`AuditLogError`, see below),
          including :class:`~trikon.change_intel.errors.ImportCheckError`
          and :class:`~trikon.verify.errors.CollectionPassError`:
          ``decision == "require_human"``, ``matched_rule`` is ``None``,
          ``policy_results`` is empty, and the reason string is
          prefixed with the failing subclass name (via
          ``type(exc).__name__``) so audit reviewers can tell the
          failures apart at a glance. ``evidence.change`` is the real
          ImpactSet if :func:`compute_impact` finished, else
          :data:`~trikon.evidence.report.EMPTY_IMPACT_SET` (bucket
          ``HIGH``). ``evidence.verification`` is the real report if
          :func:`run_verification` finished; otherwise it is
          :data:`~trikon.evidence.report.EMPTY_VERIFICATION`, carrying
          the ImportReport when :func:`check_imports` finished
          (Requirement 4.13). Never fail-open — see the module
          docstring.

        On both return paths, exactly one row is appended to the
        ``audit_log`` table on ``<repo>/.trikon/state.db`` (or the
        explicit ``cache_db``) before returning; the row holds the
        post-floor decision and its ``audit_id`` matches the returned
        Verdict's ``audit_id`` (Property 8, Requirements 5.11 and 7.3).

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
    imports: ImportReport | None = None
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
        # Raises only ImportCheckError (a ChangeIntelError), which fails
        # closed below with the real ImpactSet and no ImportReport.
        imports = check_imports(change_set, repo)
        verification = run_verification(
            repo,
            impact,
            state_db=cache_db,
            no_sandbox=no_sandbox,
            # The change set echoes the caller's SHAs (``None`` on the
            # diff-string path), so the runner can tell a supplied base
            # SHA from one it derives with ``git rev-parse HEAD~1``.
            base_sha=change_set.base_sha,
            head_sha=change_set.head_sha,
            imports=imports,
        )
        policy = load_policy(repo, Path(policy_path))
        verdict = evaluate_policy(policy, impact, verification)
        # Safety_Floor: after the policy, before the audit write
        # (Requirement 5.1). No policy argument, so no policy can switch
        # it off (Requirement 5.10). Pure, with no raise sites; it sits
        # inside the ``try`` so any future TrikonError still fails closed.
        verdict = floor_verdict(verdict, is_python_change=is_python_change(impact))
    except TrikonError as exc:
        verdict = _fail_closed_verdict(
            reason=f"{type(exc).__name__}: {exc}",
            change=impact,
            # ``run_verification`` embeds the ImportReport itself. When it
            # did not finish (e.g. CollectionPassError), keep the report
            # ``check_imports`` already produced (Requirement 4.13).
            verification=(
                verification if verification is not None else _empty_with_imports(imports)
            ),
        )

    # Audit write is OUTSIDE the try/except (Requirement 4.5, design.md §8.2).
    # A failure here re-raises AuditLogError to the caller — a verdict without
    # an audit trail is a hard failure, not a silent degrade. Both the happy
    # path (post-floor) and the fail-closed path bound `verdict` above, so
    # exactly one row, holding the final decision, is persisted per
    # sdk.verify(...) invocation (Property 8, Requirements 5.11 and 7.3).
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
    :class:`ChangeIntelError` (Phase 1, including
    :class:`~trikon.change_intel.errors.ImportCheckError`),
    :class:`~trikon.verify.errors.VerificationRunnerError` (Phase 2,
    including :class:`~trikon.verify.errors.CollectionPassError`),
    or :class:`~trikon.policy.errors.PolicyLoadError` /
    :class:`~trikon.policy.errors.RuleMatchError` (Phase 3). The verdict
    is a well-formed :class:`Verdict` — every consumer that unpickles or
    JSON-parses it sees the same shape as a successful run — but the
    embedded impact set is the ``HIGH``-bucketed sentinel so any
    downstream check treats "unknown change" as maximally risky.

    ``warnings`` defaults to ``[]`` and ``schema_version`` takes the
    current :class:`Verdict` default (3) via the Pydantic model defaults —
    no code edit needed here (``design.md §10.1``, §7.4).

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


def _empty_with_imports(imports: ImportReport | None) -> VerificationReport:
    """Return the empty verification sentinel, carrying ``imports`` when set.

    Used on the fail-closed path when :func:`check_imports` finished but
    :func:`run_verification` did not (it raised, e.g.
    :class:`~trikon.verify.errors.CollectionPassError` or
    :class:`~trikon.verify.errors.SandboxUnavailableError`). Every other
    field is :data:`EMPTY_VERIFICATION`'s, so the tests stay ``skipped``
    with strategy ``none``; only the import findings survive
    (Requirement 4.13). With ``imports`` ``None`` (the failure came at or
    before :func:`check_imports`), the sentinel itself is returned and
    its default empty ImportReport applies.

    :meth:`~pydantic.BaseModel.model_copy` returns a new object, so the
    shared :data:`EMPTY_VERIFICATION` is never mutated.
    """
    if imports is None:
        return EMPTY_VERIFICATION
    return EMPTY_VERIFICATION.model_copy(update={"imports": imports})
