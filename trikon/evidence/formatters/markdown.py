"""Markdown formatter for the GitHub PR comment / check-run summary.

The output is designed to be scannable in 5 seconds by a reviewer:
    - Top line: verdict + one-sentence reason.
    - Second line: recommended focus (file paths).
    - Collapsible section: full impact set + test failures.
"""

from __future__ import annotations

from trikon.evidence.report import Verdict

ICON = {"allow": "✅", "block": "🛑", "require_human": "🔍"}


def format_markdown(verdict: Verdict) -> str:
    """Render a `Verdict` as a GitHub-flavored Markdown summary."""
    # TODO: implement. See docs/worked_example.md for the intended shape.
    icon = ICON.get(verdict.decision, "❔")
    return f"{icon} **Trikon: {verdict.decision.upper()}** — {verdict.reason}"
