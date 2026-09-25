# Hypothesis' ``@composite`` decorator returns strategies whose element type
# is inferred as ``Any`` under strict mypy (the ``@composite`` builder
# accepts a callable whose first parameter is a ``draw`` function returning
# ``Any``). Suppress ``explicit-any`` at file scope — this is a test module
# and every ``Any`` here is bounded to the hypothesis strategy surface.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.fargate_runner.summary_builder`.

Encodes **Property 3 (Summary_Content_Parity)** from design.md §11 plus
per-decision, per-line, and per-invariant rendering checks. The property
test is the load-bearing correctness proof: for every valid ``Verdict``,
:func:`render_summary` returns byte-identical output on repeated calls
with the same arguments — which is the precondition for the entrypoint
to safely bind one ``summary`` variable and pass it unmodified to both
the Check Run and the PR comment.
"""

from __future__ import annotations

import re
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings

from trikon.evidence.report import Verdict
from trikon_cloud.fargate_runner.summary_builder import render_summary

from .conftest import make_verdict

# The three terminal decisions the runner emits. ``Decision`` in the SDK
# also accepts ``"warn"``, but ``trikon.sdk.verify`` never emits a
# ``"warn"`` Verdict at its boundary (see the invariant note on
# :attr:`Verdict.decision`), so the summary renderer's decision→glyph
# map is keyed on exactly these three values.
_TERMINAL_DECISIONS: tuple[str, str, str] = ("allow", "block", "require_human")

# The ⚠️ warning-triangle emoji is a 2-codepoint sequence (U+26A0 base
# plus U+FE0F variation selector) that does not fit inside a Python
# regex character class, so the three glyphs are alternated with the
# ``|`` operator inside a group instead.
_FIRST_LINE_RE = re.compile(
    r"^\*\*Trikon Cloud\*\* verified this PR: (?:✅|🚫|⚠️) "
    r"\*\*(?:ALLOW|BLOCK|REQUIRE HUMAN REVIEW)\*\*$"
)


# ---------------------------------------------------------------------------
# Hypothesis strategy: build a well-formed Verdict from generator draws.
# ---------------------------------------------------------------------------


@st.composite
def _verdicts(draw: Any) -> Verdict:
    """Draw a well-formed :class:`Verdict` for Property 3 testing.

    Every field driven from a hypothesis strategy is bounded to values
    the runner might see in production — decisions restricted to the
    three terminal values, integer counts capped at 1000, blast-radius
    numeric bounded at 10 000, no NaN / infinity. The nested lists
    (``changed_files`` etc.) draw short text strings so the property
    test does not stall on 10 000-element diffs.
    """
    decision = draw(st.sampled_from(_TERMINAL_DECISIONS))
    matched_rule = draw(st.one_of(st.none(), st.text(min_size=1, max_size=40)))
    blast_radius_numeric = draw(
        st.floats(
            min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False
        )
    )
    blast_radius_score = draw(st.sampled_from(["LOW", "MEDIUM", "HIGH"]))
    new_errors = draw(st.integers(min_value=0, max_value=1000))
    new_warnings = draw(st.integers(min_value=0, max_value=1000))
    preexisting_errors = draw(st.integers(min_value=0, max_value=1000))
    changed_files = draw(st.lists(st.text(max_size=32), max_size=10))
    return make_verdict(
        decision=decision,
        matched_rule=matched_rule,
        blast_radius_numeric=blast_radius_numeric,
        blast_radius_score=blast_radius_score,
        new_errors=new_errors,
        new_warnings=new_warnings,
        preexisting_errors=preexisting_errors,
        changed_files=changed_files,
    )


# ---------------------------------------------------------------------------
# Property 3 — Summary_Content_Parity.
# ---------------------------------------------------------------------------


@given(verdict=_verdicts())
@settings(max_examples=100, deadline=None)
def test_property_summary_content_parity(verdict: Verdict) -> None:
    """Feature: trikon-cloud-fargate-runner, Property 3: Summary_Content_Parity.

    Validates: Requirements 7.1, 8.1, 8.3.

    For any well-formed ``Verdict``, :func:`render_summary` invoked
    twice with the same arguments returns byte-identical strings and
    the first line matches the check-run title pattern. That parity
    is the precondition for the entrypoint to bind one local
    ``summary`` variable and pass it unmodified to both the Check
    Run's ``output.summary`` and the PR comment's ``body``.
    """
    args: dict[str, Any] = {
        "verdict": verdict,
        "audit_url": "https://cloud.trikon.dev/audits/x",
        "sdk_version": "0.3.6",
        "duration_ms": 1234,
    }
    result1 = render_summary(**args)
    result2 = render_summary(**args)
    assert result1 == result2
    assert result1.encode() == result2.encode()

    first_line = result1.split("\n", 1)[0]
    assert _FIRST_LINE_RE.match(first_line), (
        f"first line does not match check-run title pattern: {first_line!r}"
    )


# ---------------------------------------------------------------------------
# Per-decision rendering tests.
# ---------------------------------------------------------------------------


def _render(**overrides: Any) -> str:
    """Convenience wrapper — build a verdict + render with defaults."""
    verdict = make_verdict(**overrides)
    return render_summary(
        verdict=verdict,
        audit_url="https://cloud.trikon.dev/audits/x",
        sdk_version="0.3.6",
        duration_ms=1234,
    )


def test_allow_decision_renders_check_emoji_and_ALLOW() -> None:  # noqa: N802
    """``allow`` maps to ✅ and the ALLOW label in the first line."""
    result = _render(decision="allow")
    assert "✅" in result
    assert "**ALLOW**" in result


def test_block_decision_renders_prohibited_emoji_and_BLOCK() -> None:  # noqa: N802
    """``block`` maps to 🚫 and the BLOCK label in the first line."""
    result = _render(decision="block")
    assert "🚫" in result
    assert "**BLOCK**" in result


def test_require_human_decision_renders_warning_emoji_and_REQUIRE_HUMAN_REVIEW() -> None:  # noqa: N802
    """``require_human`` maps to ⚠️ and the REQUIRE HUMAN REVIEW label."""
    result = _render(decision="require_human")
    assert "⚠️" in result
    assert "**REQUIRE HUMAN REVIEW**" in result


# ---------------------------------------------------------------------------
# Per-line rendering tests.
# ---------------------------------------------------------------------------


def test_reason_line_uses_default_when_matched_rule_is_none() -> None:
    """A verdict with no matched rule renders ``**Reason:** default``.

    Falls back to the literal ``"default"`` per design.md §7 — the SDK
    emits ``matched_rule=None`` when the policy engine takes its
    fall-through branch, and the user-facing summary surfaces that as
    the string ``"default"`` rather than an empty field.
    """
    result = _render(matched_rule=None)
    assert "**Reason:** default" in result


def test_reason_line_uses_matched_rule_when_present() -> None:
    """A matched rule name renders verbatim on the Reason line."""
    result = _render(matched_rule="new static errors")
    assert "**Reason:** new static errors" in result


def test_blast_radius_line_floors_numeric_to_int() -> None:
    """``blast_radius_numeric=42.7`` renders as ``42`` (integer floor).

    The blast-radius numeric field is a float in the SDK, but the
    user-facing summary always displays an integer — ``int()`` truncates
    toward zero for the non-negative range we accept.
    """
    result = _render(blast_radius_numeric=42.7)
    assert "**Blast radius:** 42 (" in result


def test_footer_contains_audit_url_and_sdk_version_and_duration() -> None:
    """The footer stitches together the three caller-supplied strings."""
    verdict = make_verdict()
    result = render_summary(
        verdict=verdict,
        audit_url="https://x",
        sdk_version="0.3.6",
        duration_ms=1234,
    )
    assert "_Verified by [Trikon](https://x) v0.3.6 in 1234ms_" in result


def test_first_line_extraction_gives_check_run_title() -> None:
    """The first line (before ``\\n``) is the Check Run's ``output.title``.

    The entrypoint extracts ``summary.split("\\n", 1)[0]`` — this test
    pins that the first line contains no embedded newline and matches
    the expected prefix, so the check-run title is well-formed on every
    decision path.
    """
    verdict = make_verdict(decision="block")
    summary = render_summary(
        verdict=verdict,
        audit_url="https://cloud.trikon.dev/audits/x",
        sdk_version="0.3.6",
        duration_ms=100,
    )
    first_line = summary.split("\n", 1)[0]
    assert first_line.startswith("**Trikon Cloud** verified this PR:")
    assert "\n" not in first_line


def test_never_contains_agentguard() -> None:
    """Invariant 7 assertion — the string ``AgentGuard`` never appears.

    Renders across every combination of decision and matched-rule
    (present / absent) and confirms neither the case-sensitive
    product name nor its lowercase variant leaks into the user copy.
    ``AgentGuard`` was Trikon's pre-rename product name; Invariant 7
    guards against a stray reference resurrecting in user-visible
    output.
    """
    for decision in _TERMINAL_DECISIONS:
        for matched_rule in (None, "some rule", "new static errors above threshold"):
            result = _render(decision=decision, matched_rule=matched_rule)
            assert "AgentGuard" not in result
            assert "agentguard" not in result.lower()
