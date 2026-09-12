"""Public Python SDK entry point — Phase 1.

The CLI, MCP tool, GitHub App, and GitHub Actions integration are all thin
wrappers around :func:`verify`. This is the one code path.

Phase 1 scope
-------------

Phase 1 ships Change Intelligence only. The verification runner (Phase 2)
and policy engine (Phase 3) are not yet implemented, so :func:`verify` can
never legitimately emit ``allow`` or ``block`` — there is no verification
evidence to grade and no policy to consult. Every successful Phase-1 run
returns ``decision="require_human"`` with a fully-populated
:class:`~trikon.evidence.report.ImpactSet` under ``evidence.change`` and
the :data:`~trikon.evidence.report.EMPTY_VERIFICATION` sentinel elsewhere.

Never-fail-open
---------------

Any :class:`~trikon.change_intel.errors.ChangeIntelError` raised by the
change-intel pipeline is caught at the SDK boundary and translated into a
``require_human`` verdict backed by
:data:`~trikon.evidence.report.EMPTY_IMPACT_SET`, whose blast-radius bucket
is ``HIGH``. That invariant — "an unknown change is never mistaken for a
safe change" — is the closure test in
``tests/integration/change_intel/test_never_fail_open.py`` (task 10.2).

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
    print(verdict.decision)  # -> "require_human" in Phase 1
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trikon.change_intel.blast_radius import compute_impact
from trikon.change_intel.diff_parser import parse_diff
from trikon.change_intel.errors import ChangeIntelError
from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    EMPTY_VERIFICATION,
    Evidence,
    Verdict,
)

__all__ = ["verify"]


_PHASE_1_REASON: str = "Phase 1: change-intel only; no verification runner yet"


def verify(
    repo_path: Path | str,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
    *,
    policy_path: Path | str = ".trikon/policy.yaml",
    cache_db: Path | None = None,
) -> Verdict:
    """Run the Phase-1 Trikon pipeline (change-intelligence only) against a change set.

    The function is the single public entry point every wrapper — CLI,
    GitHub App, MCP tool, GitHub Action — funnels through. Phase 1
    executes only the change-intel half of the pipeline; the verification
    runner and policy engine land in Phases 2 and 3.

    Args:
        repo_path: Path to the git repository on disk. Accepts a
            :class:`pathlib.Path` or a ``str``.
        base_sha: Git SHA of the base commit. Passed straight to
            :func:`~trikon.change_intel.diff_parser.parse_diff`, which
            enforces the exactly-one-of-{SHAs, diff} rule.
        head_sha: Git SHA of the head commit. Same as ``base_sha``.
        diff: Unified-diff string. Mutually exclusive with SHA pair.
        policy_path: Reserved for Phase 3 (policy engine). Accepted here
            so callers written against the final signature keep working;
            unused in Phase 1.
        cache_db: Path to the SQLite state database. Forwarded to
            :func:`~trikon.change_intel.blast_radius.compute_impact`,
            which defaults to ``<repo>/.trikon/state.db`` when ``None``.

    Returns:
        A :class:`Verdict`. In Phase 1 the decision is always
        ``"require_human"``:

        * On success: ``evidence.change`` is the real
          :class:`~trikon.evidence.report.ImpactSet` produced by
          :func:`compute_impact` and the reason string is
          :data:`_PHASE_1_REASON`.
        * On any :class:`ChangeIntelError`: ``evidence.change`` is
          :data:`~trikon.evidence.report.EMPTY_IMPACT_SET` (bucket
          ``HIGH``) and the reason names the failing subclass. Never
          fail-open — see the module docstring.
    """
    del policy_path  # Phase 3 will consume this; kept in the signature to
    # avoid a churny rename when the policy engine lands.

    repo = Path(repo_path)

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
    except ChangeIntelError as exc:
        return _fail_closed_verdict(
            reason=f"change-intel error: {type(exc).__name__}: {exc}",
        )

    return Verdict(
        decision="require_human",
        reason=_PHASE_1_REASON,
        matched_rule=None,
        evidence=Evidence(
            change=impact,
            verification=EMPTY_VERIFICATION,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )


def _fail_closed_verdict(*, reason: str) -> Verdict:
    """Return the never-fail-open verdict backed by :data:`EMPTY_IMPACT_SET`.

    Called when the change-intel pipeline raises a
    :class:`ChangeIntelError`. The verdict is a well-formed
    :class:`Verdict` — every consumer that unpickles or JSON-parses it
    sees the same shape as a successful run — but the embedded impact set
    is the ``HIGH``-bucketed sentinel so any downstream check treats
    "unknown change" as maximally risky.

    Args:
        reason: Human-readable summary of the failure. Included verbatim
            in ``Verdict.reason`` so audit logs record the exception
            class and message that tripped the fail-closed path.

    Returns:
        A :class:`Verdict` with ``decision="require_human"``,
        ``matched_rule=None``, and the empty sentinels for change and
        verification evidence.
    """
    return Verdict(
        decision="require_human",
        reason=reason,
        matched_rule=None,
        evidence=Evidence(
            change=EMPTY_IMPACT_SET,
            verification=EMPTY_VERIFICATION,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )
