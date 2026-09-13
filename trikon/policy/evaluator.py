"""Apply a Policy to an ImpactSet + VerificationReport to produce a Verdict.

Evaluation model:
    Rules are iterated in declaration order.
    For each rule, all conditions in `when` must match (AND).
    The first rule whose `then` is a terminal decision (allow/block/require_human)
    determines the verdict. Non-terminal rules (`warn`) attach reasons and continue.

Every rule evaluation — matched or not — is recorded in `RuleResult` for transparency.

Every foreign exception raised by the dispatcher (unknown condition key,
malformed operator dict, wrong argument shape) is caught at this module
boundary and re-raised as :class:`RuleMatchError`. Nothing bare — no
``KeyError``, no ``TypeError``, no ``ValueError`` — escapes past
:func:`_rule_matches` (design.md §5.2-§5.5, Requirement 7.1).
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import uuid4

from trikon.evidence.report import (
    Evidence,
    ImpactSet,
    RuleResult,
    Verdict,
    VerificationReport,
)
from trikon.policy.dsl import Policy, Rule
from trikon.policy.errors import RuleMatchError

TERMINAL = {"allow", "block", "require_human"}


# ---------------------------------------------------------------------------
# Operator table for ``verification.static.new_errors``
# ---------------------------------------------------------------------------
#
# Three uniform ``(int, int) -> bool`` callables, one per accepted operator
# name. A dict is denser than an ``if``/``elif`` chain here because every
# value has the same signature (design.md §5.5). The top-level condition
# dispatcher stays inline because its argument shapes are heterogeneous.
_NEW_ERRORS_OPERATORS: dict[str, Callable[[int, int], bool]] = {
    "eq": operator.eq,
    "gt": operator.gt,
    "lt": operator.lt,
}


def evaluate_policy(
    policy: Policy,
    change: ImpactSet,
    verification: VerificationReport,
) -> Verdict:
    """Apply the policy and return a Verdict."""
    rule_results: list[RuleResult] = []
    warnings: list[str] = []
    decision = None
    matched_rule_name: str | None = None
    reason = "No rule matched; defaulting to require_human."

    for rule in policy.rules:
        matched = _rule_matches(rule, change, verification)
        rule_results.append(
            RuleResult(
                rule_name=rule.name,
                matched=matched,
                # ``would_emit`` now reports the rule's ``then`` on both
                # terminal AND warn matches (Requirement 3.4).
                would_emit=rule.then if matched else None,
                reason=rule.reason,
            )
        )

        if not matched:
            continue

        if rule.then == "warn":
            # Non-terminal: accumulate the reason and fall through to the
            # next rule (Requirement 3.3, design.md §7.1).
            warnings.append(rule.reason or f"Rule '{rule.name}' warned.")
            continue

        # Terminal branch — first-match-terminal wins (Requirement 3.1).
        if rule.then in TERMINAL and decision is None:
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
        warnings=warnings,
        # schema_version=2 by Pydantic default (design.md §3.6).
    )


# ---------------------------------------------------------------------------
# Rule dispatcher — inline ``if``/``elif`` chain (design.md §5.2)
# ---------------------------------------------------------------------------


def _rule_matches(rule: Rule, change: ImpactSet, verification: VerificationReport) -> bool:
    """Return True iff every condition in ``rule.when`` holds against the evidence.

    Dispatch table (Requirements 1.1-1.5):
        any_path_matches                     -> _match_any_path
        no_path_matches                      -> _match_no_path
        change.blast_radius.score            -> _match_blast_radius
        verification.tests.status            -> _match_tests_status
        verification.static.new_errors       -> _match_new_errors

    Semantics (Requirements 1.6-1.7):
        - Empty ``when`` (``{}``) — unconditional match, return True.
        - Multiple keys — AND across all conditions (every one must match).
          Short-circuits on the first ``False``.
        - Unknown key — raise :class:`RuleMatchError`.
    """
    if not rule.when:
        # Empty when — unconditional match (Requirement 1.7). Property 4 pins
        # this behavior universally.
        return True

    for key, value in rule.when.items():
        if key == "any_path_matches":
            matched = _match_any_path(_expect_list_str(value, key), change)
        elif key == "no_path_matches":
            matched = _match_no_path(_expect_list_str(value, key), change)
        elif key == "change.blast_radius.score":
            matched = _match_blast_radius(_expect_str(value, key), change)
        elif key == "verification.tests.status":
            matched = _match_tests_status(_expect_str(value, key), verification)
        elif key == "verification.static.new_errors":
            matched = _match_new_errors(_expect_operator_dict(value, key), verification)
        else:
            # Unknown condition key — policy authoring error (design.md §5.3,
            # Property 5). Surface as ``RuleMatchError`` so the SDK boundary
            # can fail-close to ``require_human``.
            raise RuleMatchError(f"unknown condition key: {key!r} in rule {rule.name!r}")

        if not matched:
            # AND semantics with short-circuit (Requirement 1.6). One False
            # condition kills the rule; remaining keys are not evaluated.
            return False

    return True


# ---------------------------------------------------------------------------
# Condition matchers (design.md §5.4-§5.5)
# ---------------------------------------------------------------------------


def _match_any_path(patterns: list[str], change: ImpactSet) -> bool:
    """At least one pattern matches at least one changed file.

    Uses :class:`pathlib.PurePosixPath.match`, which honors ``**`` recursion
    on POSIX-style paths. Requirement 1.1 cites ``fnmatch.fnmatchcase``
    semantics as the policy-authoring shorthand; the ``PurePosixPath.match``
    upgrade is what actually makes ``auth/**`` match ``src/auth/token.py``
    (design.md §5.4).
    """
    for f in change.changed_files:
        p = PurePosixPath(f)
        for pat in patterns:
            if p.match(pat):
                return True
    return False


def _match_no_path(patterns: list[str], change: ImpactSet) -> bool:
    """Dual of :func:`_match_any_path` — no pattern matches any changed file."""
    return not _match_any_path(patterns, change)


def _match_blast_radius(expected: str, change: ImpactSet) -> bool:
    """Literal equality against ``change.blast_radius_score`` (Requirement 1.3)."""
    return change.blast_radius_score == expected


def _match_tests_status(expected: str, verification: VerificationReport) -> bool:
    """Literal equality against ``verification.tests.status`` (Requirement 1.4)."""
    return verification.tests.status == expected


def _match_new_errors(op_dict: dict[str, int], verification: VerificationReport) -> bool:
    """Count ``is_new`` findings and compare to a threshold under one operator.

    ``op_dict`` must be a single-key mapping of ``eq``/``gt``/``lt`` to an
    int threshold. Multi-key mappings, unknown operators, and non-int
    thresholds all raise :class:`RuleMatchError` (design.md §5.5).
    """
    if len(op_dict) != 1:
        raise RuleMatchError(
            f"verification.static.new_errors expects exactly one operator, got {sorted(op_dict)!r}"
        )
    ((op_name, threshold),) = op_dict.items()
    op = _NEW_ERRORS_OPERATORS.get(op_name)
    if op is None:
        raise RuleMatchError(
            f"verification.static.new_errors: unknown operator {op_name!r}; "
            f"expected one of {sorted(_NEW_ERRORS_OPERATORS)}"
        )
    if not isinstance(threshold, int):
        # Defensive; the shape guard already narrows to ``dict[str, int]``.
        # Kept explicit because design.md §5.5 enumerates it as a raise site.
        raise RuleMatchError(
            f"verification.static.new_errors[{op_name}]: expected int, "
            f"got {type(threshold).__name__}"
        )
    count = sum(1 for f in verification.static.findings if bool(f.get("is_new")))
    return op(count, threshold)


# ---------------------------------------------------------------------------
# Shape guards — Pydantic-style type checks for values pulled out of a
# ``dict[str, Any]`` YAML mapping (design.md §5.2). Each guard raises
# :class:`RuleMatchError` on any shape mismatch so the matcher functions
# have concrete signatures without ``Any`` leaking in.
# ---------------------------------------------------------------------------


def _expect_list_str(value: object, key: str) -> list[str]:
    """Narrow ``value`` to ``list[str]`` or raise :class:`RuleMatchError`."""
    if not isinstance(value, list):
        raise RuleMatchError(f"{key} expects a list of strings, got {type(value).__name__}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuleMatchError(
                f"{key} expects a list of strings, got a {type(item).__name__} entry"
            )
        result.append(item)
    return result


def _expect_str(value: object, key: str) -> str:
    """Narrow ``value`` to ``str`` or raise :class:`RuleMatchError`."""
    if not isinstance(value, str):
        raise RuleMatchError(f"{key} expects a string, got {type(value).__name__}")
    return value


def _expect_operator_dict(value: object, key: str) -> dict[str, int]:
    """Narrow ``value`` to ``dict[str, int]`` or raise :class:`RuleMatchError`.

    The multi-key / unknown-operator checks belong to
    :func:`_match_new_errors` (design.md §5.5); this guard only enforces
    the outer ``dict[str, int]`` shape.
    """
    if not isinstance(value, dict):
        raise RuleMatchError(
            f"{key} expects a mapping of operator to int, got {type(value).__name__}"
        )
    result: dict[str, int] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise RuleMatchError(
                f"{key} expects string operator keys, got a {type(k).__name__} key"
            )
        if not isinstance(v, int):
            raise RuleMatchError(f"{key}[{k!r}]: expected int threshold, got {type(v).__name__}")
        result[k] = v
    return result
