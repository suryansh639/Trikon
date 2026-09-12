"""Unit tests for the policy evaluator — the terminal-first rule engine.

These tests seed synthetic ImpactSet + VerificationReport values and assert
that evaluate_policy() picks the right rule and returns the right decision.
No IO, no git, no docker.
"""

from __future__ import annotations

import pytest

# Marker for tests we know are pending real evaluator condition impl.
pending_eval = pytest.mark.skip(reason="evaluator._rule_matches not implemented yet")


@pending_eval
def test_first_terminal_rule_wins() -> None:
    """When two rules would emit terminal decisions, the earlier one wins."""
    # TODO: build a Policy with two matching terminal rules and assert first wins.
    raise NotImplementedError


@pending_eval
def test_non_terminal_warn_does_not_decide() -> None:
    """A 'warn' rule attaches a reason but doesn't set the decision."""
    raise NotImplementedError


@pending_eval
def test_no_rule_matches_defaults_to_require_human() -> None:
    """The evaluator never silently allows on fall-through."""
    raise NotImplementedError


@pending_eval
def test_every_rule_leaves_a_trace() -> None:
    """All rules — matched or not — must appear in policy_results."""
    raise NotImplementedError
