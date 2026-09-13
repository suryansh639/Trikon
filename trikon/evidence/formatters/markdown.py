"""Markdown formatter for the GitHub PR comment / check-run summary.

The output is designed to be scannable in 5 seconds by a reviewer. The layout
is the six-block shape frozen in ``design.md §12`` (Requirements 6.1-6.6):

    1. Header — decision icon + matched_rule + reason.
    2. Focus your review on — up to 5 file paths from ``changed_files``.
    3. Impact — file / module / API counts + blast-radius bucket + numeric.
    4. Verification — pytest pass/fail/skip counts + up to 5 failing tests.
    5. Warnings (conditional) — every warning verbatim in list order.
    6. Footer — audit id + schema version under a horizontal rule.

``format_markdown`` is a pure function of :class:`~trikon.evidence.report.Verdict`:
no I/O, no template engine, no runtime dependency outside the standard
library and the already-imported Pydantic model (Requirement 6.6). Every
edge case (``EMPTY_IMPACT_SET``, empty ``warnings``, empty ``failures``)
renders a well-formed string without exceptions so the fail-closed path
never crashes downstream reporters (design.md §12.3-§12.6, §13).
"""

from __future__ import annotations

from trikon.evidence.report import Verdict

# Icon per decision. ``"warn"`` is included for forward-compat (design.md
# §3.6); :func:`trikon.sdk.verify` never emits ``decision == "warn"`` at
# the SDK boundary, so in practice only the three terminal icons render.
ICON: dict[str, str] = {
    "allow": "✅",
    "block": "🛑",
    "require_human": "🔍",
    "warn": "⚠️",
}


def format_markdown(verdict: Verdict) -> str:
    """Render a :class:`Verdict` as a GitHub-flavored Markdown summary.

    Pure function: no I/O, no template engine, only stdlib primitives and
    the Pydantic model. Handles the fail-closed shape
    (``EMPTY_IMPACT_SET`` + ``EMPTY_VERIFICATION`` + empty ``warnings``)
    without raising — Requirement 7.2 depends on this never crashing.
    """
    blocks: list[str] = [
        _render_header(verdict),
        _render_focus(verdict),
        _render_impact(verdict),
        _render_verification(verdict),
    ]

    warnings_block = _render_warnings(verdict)
    if warnings_block is not None:
        blocks.append(warnings_block)

    blocks.append(_render_footer(verdict))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Block 1 — Header
# ---------------------------------------------------------------------------


def _render_header(verdict: Verdict) -> str:
    """Render the header block (icon + decision + matched rule + reason).

    ``matched_rule`` is backtick-wrapped when set and rendered as the
    literal ``` `<no rule matched>` ``` on the fall-through / fail-closed
    path (design.md §12.2). The reason line is prefixed with ``> `` so it
    renders as a Markdown blockquote.
    """
    icon = ICON.get(verdict.decision, "❔")
    if verdict.matched_rule is not None:
        matched_rule_display = f"`{verdict.matched_rule}`"
    else:
        matched_rule_display = "`<no rule matched>`"

    decision_upper = verdict.decision.upper()
    return f"{icon} **Trikon: {decision_upper}** — {matched_rule_display}\n> {verdict.reason}"


# ---------------------------------------------------------------------------
# Block 2 — Focus your review on
# ---------------------------------------------------------------------------


def _render_focus(verdict: Verdict) -> str:
    """Render the "Focus your review on" block.

    Up to 5 backtick-wrapped file paths from
    ``verdict.evidence.change.changed_files`` in list order — no sort, no
    dedup (design.md §12.3). On an empty list (fail-closed with
    ``EMPTY_IMPACT_SET``), a single italic ``- _no files_`` bullet keeps
    the section shape stable.
    """
    changed_files = verdict.evidence.change.changed_files
    lines: list[str] = ["## Focus your review on"]

    if not changed_files:
        lines.append("- _no files_")
    else:
        for path in changed_files[:5]:
            lines.append(f"- `{path}`")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Block 3 — Impact
# ---------------------------------------------------------------------------


def _render_impact(verdict: Verdict) -> str:
    """Render the "Impact" block.

    Four bullets sourced from ``verdict.evidence.change`` (design.md
    §12.4): changed-file count, impacted-module count, impacted-public-API
    count, and the blast-radius bucket paired with its numeric score at
    two-decimal precision.
    """
    change = verdict.evidence.change
    blast_line = f"{change.blast_radius_score} ({change.blast_radius_numeric:.2f})"
    return "\n".join(
        [
            "## Impact",
            f"- **Changed files:** {len(change.changed_files)}",
            f"- **Impacted modules:** {len(change.impacted_modules)}",
            f"- **Impacted public APIs:** {len(change.impacted_public_apis)}",
            f"- **Blast radius:** {blast_line}",
        ]
    )


# ---------------------------------------------------------------------------
# Block 4 — Verification
# ---------------------------------------------------------------------------


def _render_verification(verdict: Verdict) -> str:
    """Render the "Verification" block.

    Top line is ``"{passed} passed · {failed} failed · {skipped} skipped"``
    (design.md §12.5). When ``failures`` is non-empty, an indented sub-list
    of up to 5 failing entries follows. Each entry is
    ``` - `<node_id>` — <failure_summary> ```; when ``failure_summary`` is
    ``None`` the dash-separator and text are omitted. When ``failures`` is
    empty, the sub-list header is omitted so the top line stands alone.
    """
    tests = verdict.evidence.verification.tests
    lines: list[str] = [
        "## Verification",
        f"- **Tests:** {tests.passed} passed · {tests.failed} failed · {tests.skipped} skipped",
    ]

    if tests.failures:
        lines.append("- **First 5 failing tests:**")
        for failure in tests.failures[:5]:
            if failure.failure_summary is None:
                lines.append(f"  - `{failure.node_id}`")
            else:
                lines.append(f"  - `{failure.node_id}` — {failure.failure_summary}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Block 5 — Warnings (conditional)
# ---------------------------------------------------------------------------


def _render_warnings(verdict: Verdict) -> str | None:
    """Render the "Warnings" block, or ``None`` when there is nothing to show.

    The block is emitted iff ``verdict.warnings`` is non-empty
    (Requirement 6.5, design.md §12.6). Every warning appears verbatim in
    list order — no truncation, no deduplication.
    """
    if not verdict.warnings:
        return None

    lines: list[str] = ["## Warnings"]
    for warning in verdict.warnings:
        lines.append(f"- {warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Block 6 — Footer
# ---------------------------------------------------------------------------


def _render_footer(verdict: Verdict) -> str:
    """Render the footer — horizontal rule + audit id + schema version.

    ``audit_id`` is stringified from the UUID; ``schema_version`` is the
    integer stored on the Verdict (design.md §12.7). The footer is what
    lets a reviewer paste an audit ID back into a compliance query and
    pull the exact stored ``verdict_json``.
    """
    return f"---\n_Audit id_: `{verdict.audit_id}`  ·  _Schema version_: `{verdict.schema_version}`"
