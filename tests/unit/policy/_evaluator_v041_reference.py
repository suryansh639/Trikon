"""Frozen copy of the previous release's (v041) ``_rule_matches``.

Taken from ``git show 8f29d43:trikon/policy/evaluator.py``, the published
previous release. Only ``_rule_matches`` and the helpers it calls are kept:
``evaluate_policy`` and the Verdict-building imports are dropped. The function
bodies, error messages and dispatch order are copied as they were, so the
engine fail-safe evaluator can be checked against them (Property 12,
Requirement 6.8).

Do not edit the logic here to follow later changes to
``trikon/policy/evaluator.py``. This module is the oracle for the
previous-release DSL keys. The leading underscore keeps pytest from
collecting it.

Previous-release keys handled here:

- ``any_path_matches``
- ``no_path_matches``
- ``change.blast_radius.score``
- ``verification.tests.status``
- ``verification.static.new_errors``
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from pathlib import PurePosixPath

from trikon.evidence.report import ImpactSet, VerificationReport
from trikon.policy.dsl import Rule
from trikon.policy.errors import RuleMatchError

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
