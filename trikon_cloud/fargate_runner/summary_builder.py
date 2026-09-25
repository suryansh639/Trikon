"""Pure verdict → Markdown renderer (design.md §7).

Property 3 (``Summary_Content_Parity``) requires this function to be
invoked EXACTLY ONCE per verdict and its return value passed
byte-identical to both the Check Run's ``output.summary`` and the PR
comment's ``body`` field. The function is deterministic in the
verdict shape — no wall-clock reads, no random UUIDs, no IO — so a
second call with the same arguments produces the same bytes, and a
single call reused across both surfaces is the load-bearing pattern
the property depends on.

Public surface:

* :func:`render_summary` — pure function producing the Markdown
  template from design.md §7. The FIRST LINE of the return value is
  the Check Run's ``output.title`` (extracted via
  ``summary.split("\\n", 1)[0]``); the FULL string is both the Check
  Run's ``output.summary`` and the PR comment's ``body``.

Nothing else is exported. The two emoji / decision-label maps at
module scope are ``_``-prefixed so the type-level surface stays
minimal.
"""

from __future__ import annotations

from typing import Final

from trikon.evidence.report import Verdict

__all__ = ["render_summary"]


# ---------------------------------------------------------------------------
# Decision → glyph and decision → uppercase label maps (design.md §7).
# ---------------------------------------------------------------------------
#
# Keyed on the terminal ``Verdict.decision`` values only. The SDK's
# ``Decision`` literal also permits ``"warn"``, but that value is a
# rule-outcome flavor never emitted at the verdict boundary
# (``trikon/evidence/report.py`` invariant note on ``Verdict.decision``).
# A stray ``"warn"`` would trigger a ``KeyError`` at render time — the
# caller's ``except Exception`` in ``entrypoint.main`` routes such a
# failure into the Never_Fail_Open path.

_EMOJI_BY_DECISION: Final[dict[str, str]] = {
    "allow": "✅",
    "block": "🚫",
    "require_human": "⚠️",
}

_DECISION_UPPER_BY_DECISION: Final[dict[str, str]] = {
    "allow": "ALLOW",
    "block": "BLOCK",
    "require_human": "REQUIRE HUMAN REVIEW",
}


def render_summary(
    *,
    verdict: Verdict,
    audit_url: str,
    sdk_version: str,
    duration_ms: int,
) -> str:
    """Render a Markdown summary for both the Check Run and PR comment.

    Property 3 (``Summary_Content_Parity``) requires this function to
    be invoked EXACTLY ONCE per verdict and its return value passed
    byte-identical to both the Check Run's ``output.summary`` and the
    PR comment's ``body`` field. The function is pure: no IO, no
    wall-clock reads, no random UUIDs — every value comes from the
    arguments.

    Args:
        verdict: The ``Verdict`` from ``trikon.sdk.verify(...)``. This
            function reads ``verdict.decision``,
            ``verdict.matched_rule``, and
            ``verdict.evidence.change.*`` /
            ``verdict.evidence.verification.static.*``.
        audit_url: The ``details_url`` for the Check Run (typically the
            dashboard link keyed on ``verdict.audit_id``). Embedded in
            the footer link.
        sdk_version: The Trikon SDK version string (via
            ``importlib.metadata.version("trikon")``). Embedded in the
            footer.
        duration_ms: Wall time of the runner task in milliseconds.
            Embedded in the footer.

    Returns:
        The full Markdown summary. The FIRST LINE is the Check Run's
        ``output.title`` (extracted via
        ``summary.split("\\n", 1)[0]``); the FULL string is both the
        Check Run's ``output.summary`` and the PR comment's ``body``
        (byte-identical).
    """
    emoji = _EMOJI_BY_DECISION[verdict.decision]
    decision_upper = _DECISION_UPPER_BY_DECISION[verdict.decision]
    reason = verdict.matched_rule or "default"

    change = verdict.evidence.change
    static = verdict.evidence.verification.static

    blast_radius_int = int(change.blast_radius_numeric)
    blast_radius_label = change.blast_radius_score  # "HIGH" / "MEDIUM" / "LOW"

    header = f"**Trikon Cloud** verified this PR: {emoji} **{decision_upper}**"
    reason_line = f"**Reason:** {reason}"
    blast_line = f"**Blast radius:** {blast_radius_int} ({blast_radius_label})"
    static_line = (
        f"**Static findings:** {static.new_errors} new errors, "
        f"{static.new_warnings} new warnings, "
        f"{static.preexisting_errors} preexisting errors"
    )

    evidence_table = (
        "<details>\n"
        "<summary>Show evidence</summary>\n\n"
        "| Change kind | Count |\n"
        "|-------------|-------|\n"
        f"| Changed files | {len(change.changed_files)} |\n"
        f"| Changed symbols | {len(change.changed_symbols)} |\n"
        f"| Impacted modules | {len(change.impacted_modules)} |\n"
        f"| Impacted public APIs | {len(change.impacted_public_apis)} |\n"
        f"| Impacted tests | {len(change.impacted_tests)} |\n\n"
        "</details>"
    )

    footer = (
        f"_Verified by [Trikon]({audit_url}) v{sdk_version} in {duration_ms}ms_"
    )

    return "\n\n".join(
        [header, reason_line, blast_line, static_line, evidence_table, footer]
    )
