"""Apply a Policy to an ImpactSet + VerificationReport to produce a Verdict.

Evaluation model:
    Rules are iterated in declaration order.
    For each rule, all conditions in `when` must match (AND).
    The first rule whose `then` is a terminal decision (allow/block/require_human)
    determines the verdict. Non-terminal rules (`warn`) attach reasons and continue.

Every rule evaluation — matched or not — is recorded in `RuleResult` for transparency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from trikon.evidence.report import (
    Evidence,
    ImpactSet,
    RuleResult,
    Verdict,
    VerificationReport,
)
from trikon.policy.dsl import Policy, Rule

TERMINAL = {"allow", "block", "require_human"}


def evaluate_policy(
    policy: Policy,
    change: ImpactSet,
    verification: VerificationReport,
) -> Verdict:
    """Apply the policy and return a Verdict."""
    rule_results: list[RuleResult] = []
    decision = None
    matched_rule_name: str | None = None
    reason = "No rule matched; defaulting to require_human."

    for rule in policy.rules:
        matched = _rule_matches(rule, change, verification)
        rule_results.append(
            RuleResult(
                rule_name=rule.name,
                matched=matched,
                would_emit=rule.then if matched else None,
                reason=rule.reason,
            )
        )
        if matched and rule.then in TERMINAL and decision is None:
            decision = rule.then
            matched_rule_name = rule.name
            reason = rule.reason or f"Matched rule '{rule.name}'."

    if decision is None:
        decision = "require_human"

    return Verdict(
        decision=decision,
        reason=reason,
        matched_rule=matched_rule_name,
        evidence=Evidence(change=change, verification=verification, policy_results=rule_results),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )


def _rule_matches(rule: Rule, change: ImpactSet, verification: VerificationReport) -> bool:
    """Return True if every condition in `rule.when` holds against the evidence.

    Condition keys (v0.1):
        any_path_matches: [glob, glob, ...]
        no_path_matches:  [glob, glob, ...]
        change.blast_radius.score: LOW | MEDIUM | HIGH
        verification.tests.status: passed | failed | skipped
        verification.static.new_errors: {eq: N} | {gt: N} | {lt: N}
    """
    # TODO: implement each condition kind. Keep this dispatch flat and testable.
    raise NotImplementedError
