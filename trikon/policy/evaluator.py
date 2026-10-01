"""Apply a Policy to an ImpactSet + VerificationReport to produce a Verdict.

Evaluation model:
    Rules are iterated in declaration order.
    For each rule, all conditions in `when` must match (AND). An `any_of`
    condition holds a list of condition mappings and matches when at least one
    of them matches under the same AND semantics (OR inside a rule).
    The first rule whose `then` is a terminal decision (allow/block/require_human)
    determines the verdict. Non-terminal rules (`warn`) attach reasons and continue.

Every rule evaluation — matched or not — is recorded in `RuleResult` for transparency.

Every foreign exception raised by the dispatcher (unknown condition key,
malformed operator dict, wrong argument shape) is caught at this module
boundary and re-raised as :class:`RuleMatchError`. Nothing bare — no
``KeyError``, no ``TypeError``, no ``ValueError`` — escapes past
:func:`_rule_matches` / :func:`_conditions_match` (design.md §5.2-§5.5,
Requirement 7.1).

The keys the previous release understood resolve exactly as they did there,
with the same error messages (Requirement 6.8). The engine fail-safe work
added the test-strategy, import and Python_Change keys plus ``any_of``
(design §9, Requirements 6.1-6.7); see :func:`_conditions_match`.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import uuid4

from trikon.evidence.report import (
    Evidence,
    ImpactSet,
    RuleResult,
    Verdict,
    VerificationReport,
    is_python_change,
)
from trikon.policy.dsl import Policy, Rule
from trikon.policy.errors import RuleMatchError

TERMINAL = {"allow", "block", "require_human"}


# ---------------------------------------------------------------------------
# Operator table for the integer condition keys
# ---------------------------------------------------------------------------
#
# Three uniform ``(int, int) -> bool`` callables, one per accepted operator
# name. A dict is denser than an ``if``/``elif`` chain here because every
# value has the same signature (design.md §5.5). Shared by every integer key
# (``verification.static.new_errors``, ``verification.tests.executed``,
# ``verification.tests.total``, ``verification.imports.broken``) through
# :func:`_match_int`. The top-level condition dispatcher stays inline because
# its argument shapes are heterogeneous.
_INT_OPERATORS: dict[str, Callable[[int, int], bool]] = {
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
        # schema_version comes from the Pydantic default (currently 3).
    )


# ---------------------------------------------------------------------------
# Rule dispatcher — inline ``if``/``elif`` chain (design.md §5.2, design §9)
# ---------------------------------------------------------------------------


def _rule_matches(rule: Rule, change: ImpactSet, verification: VerificationReport) -> bool:
    """Return True iff every condition in ``rule.when`` holds against the evidence.

    Thin wrapper over :func:`_conditions_match`, which does the dispatch so
    that ``any_of`` can recurse into its sub-mappings with the same rule name
    in every error message.
    """
    return _conditions_match(rule.when, rule.name, change, verification)


def _conditions_match(
    when: Mapping[str, object],
    rule_name: str,
    change: ImpactSet,
    verification: VerificationReport,
) -> bool:
    """Return True iff every condition in ``when`` holds against the evidence.

    Dispatch table, previous-release keys (Requirements 1.1-1.5, 6.8):
        any_path_matches                     -> _match_any_path
        no_path_matches                      -> _match_no_path
        change.blast_radius.score            -> _match_blast_radius
        verification.tests.status            -> _match_tests_status
        verification.static.new_errors       -> _match_new_errors

    Keys added by the engine fail-safe work (design §9, Requirements 6.1-6.7):
        verification.tests.executed     {eq|gt|lt: int}  tests.passed + tests.failed
        verification.tests.total        {eq|gt|lt: int}  tests.total
        verification.tests.strategy     str              tests.strategy == value
        verification.tests.incomplete   bool             tests.incomplete == value
        verification.imports.broken     {eq|gt|lt: int}  len(imports.broken)
        verification.imports.incomplete bool             imports.incomplete == value
        change.python_change            bool             is_python_change(change) == value
        any_of                          non-empty list of non-empty mappings;
                                        true iff any mapping matches (recursive)

    ``verification.tests.executed`` is Executed_Test_Count, computed as
    ``passed + failed`` rather than read from the stored ``executed`` field,
    the same way the Safety_Floor computes it.

    Semantics (Requirements 1.6-1.7):
        - Empty ``when`` (``{}``) — unconditional match, return True.
        - Multiple keys — AND across all conditions (every one must match).
          Short-circuits on the first ``False``.
        - ``any_of`` — OR across its mappings. Short-circuits on the first
          ``True``, so later mappings are not evaluated, just as keys after a
          failing AND condition are not.
        - Unknown key — raise :class:`RuleMatchError`.
    """
    if not when:
        # Empty when — unconditional match (Requirement 1.7). Property 4 pins
        # this behavior universally.
        return True

    for key, value in when.items():
        # Previous-release keys. Behaviour and messages are unchanged
        # (Requirement 6.8).
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
        # Keys added by the engine fail-safe work (design §9).
        elif key == "verification.tests.executed":
            tests = verification.tests
            matched = _match_int(
                _expect_operator_dict(value, key), tests.passed + tests.failed, key
            )
        elif key == "verification.tests.total":
            matched = _match_int(_expect_operator_dict(value, key), verification.tests.total, key)
        elif key == "verification.tests.strategy":
            matched = verification.tests.strategy == _expect_str(value, key)
        elif key == "verification.tests.incomplete":
            matched = verification.tests.incomplete == _expect_bool(value, key)
        elif key == "verification.imports.broken":
            matched = _match_int(
                _expect_operator_dict(value, key), len(verification.imports.broken), key
            )
        elif key == "verification.imports.incomplete":
            matched = verification.imports.incomplete == _expect_bool(value, key)
        elif key == "change.python_change":
            matched = is_python_change(change) == _expect_bool(value, key)
        elif key == "any_of":
            # OR inside one rule (Requirement 6.10). ``any`` short-circuits on
            # the first matching mapping. The list's shape is checked up front,
            # so a malformed entry raises whatever the evidence is.
            matched = any(
                _conditions_match(conditions, rule_name, change, verification)
                for conditions in _expect_condition_list(value, key)
            )
        else:
            # Unknown condition key — policy authoring error (design.md §5.3,
            # Property 5). Surface as ``RuleMatchError`` so the SDK boundary
            # can fail-close to ``require_human``.
            raise RuleMatchError(f"unknown condition key: {key!r} in rule {rule_name!r}")

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
    thresholds all raise :class:`RuleMatchError` (design.md §5.5). The
    messages are built by :func:`_match_int` with this key, so they are the
    same strings the previous release raised.
    """
    count = sum(1 for f in verification.static.findings if bool(f.get("is_new")))
    return _match_int(op_dict, count, "verification.static.new_errors")


def _match_int(op_dict: dict[str, int], actual: int, key: str) -> bool:
    """Compare ``actual`` to the threshold in a single-operator ``op_dict``.

    ``op_dict`` must be a single-key mapping of ``eq``/``gt``/``lt`` to an
    int threshold; ``key`` names the condition in every error message.
    Multi-key mappings, unknown operators, and non-int thresholds all raise
    :class:`RuleMatchError` (design.md §5.5).
    """
    if len(op_dict) != 1:
        raise RuleMatchError(f"{key} expects exactly one operator, got {sorted(op_dict)!r}")
    ((op_name, threshold),) = op_dict.items()
    op = _INT_OPERATORS.get(op_name)
    if op is None:
        raise RuleMatchError(
            f"{key}: unknown operator {op_name!r}; expected one of {sorted(_INT_OPERATORS)}"
        )
    if not isinstance(threshold, int):
        # Defensive; the shape guard already narrows to ``dict[str, int]``.
        # Kept explicit because design.md §5.5 enumerates it as a raise site.
        raise RuleMatchError(f"{key}[{op_name}]: expected int, got {type(threshold).__name__}")
    return op(actual, threshold)


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


def _expect_bool(value: object, key: str) -> bool:
    """Narrow ``value`` to ``bool`` or raise :class:`RuleMatchError`.

    ``bool`` subclasses ``int``, not the other way round, so YAML ``1`` / ``0``
    fail the ``isinstance`` check and are rejected: only ``true`` / ``false``
    are accepted.
    """
    if not isinstance(value, bool):
        raise RuleMatchError(f"{key} expects a boolean, got {type(value).__name__}")
    return value


def _expect_operator_dict(value: object, key: str) -> dict[str, int]:
    """Narrow ``value`` to ``dict[str, int]`` or raise :class:`RuleMatchError`.

    The multi-key / unknown-operator checks belong to :func:`_match_int`
    (design.md §5.5); this guard only enforces the outer ``dict[str, int]``
    shape.
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


def _expect_condition_list(value: object, key: str) -> list[dict[str, object]]:
    """Narrow ``value`` to a non-empty list of non-empty condition mappings.

    Raises :class:`RuleMatchError` when ``value`` is not a list, the list is
    empty, an entry is not a mapping, an entry is empty, or an entry has a
    non-string key. An empty entry is rejected because an empty ``when``
    matches unconditionally, which would make the whole ``any_of`` vacuously
    true; an empty list is rejected because it could never match.
    """
    if not isinstance(value, list):
        raise RuleMatchError(
            f"{key} expects a non-empty list of condition mappings, got {type(value).__name__}"
        )
    if not value:
        raise RuleMatchError(
            f"{key} expects a non-empty list of condition mappings, got an empty list"
        )
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuleMatchError(
                f"{key}[{index}] expects a condition mapping, got {type(item).__name__}"
            )
        if not item:
            raise RuleMatchError(
                f"{key}[{index}] expects a non-empty condition mapping, got an empty mapping"
            )
        conditions: dict[str, object] = {}
        for cond_key, cond_value in item.items():
            if not isinstance(cond_key, str):
                raise RuleMatchError(
                    f"{key}[{index}] expects string condition keys, "
                    f"got a {type(cond_key).__name__} key"
                )
            conditions[cond_key] = cond_value
        result.append(conditions)
    return result
