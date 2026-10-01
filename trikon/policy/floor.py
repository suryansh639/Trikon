"""Safety_Floor: a post-policy check that no policy can switch off.

The floor runs after :func:`trikon.policy.evaluator.evaluate_policy` and before
the audit write (Requirement 5.1). It only ever raises an ``allow`` decision;
every other decision passes through with its ``matched_rule`` and ``reason``
untouched (Requirement 5.7). The conditions, checked in this order:

1. ``allow`` and the ImportReport holds at least one Broken_Import
   -> ``block`` via :data:`FLOOR_BROKEN_IMPORTS` (Requirement 5.2). This check
   comes first, so it wins when an evidence gap holds too (Requirement 5.6).
2. ``allow``, a Python_Change, and any evidence gap
   -> ``require_human`` via :data:`FLOOR_INSUFFICIENT_EVIDENCE`
   (Requirements 5.3-5.5). The gaps are:

   - Executed_Test_Count is 0. It is computed as ``tests.passed +
     tests.failed`` rather than read from ``tests.executed``, so a report
     whose stored count disagrees with its outcomes cannot slip through;
   - the TestReport is marked incomplete;
   - the ImportReport is marked incomplete.

When the floor changes a decision, :func:`floor_verdict` sets
``matched_rule`` to the Floor_Rule_Id, rewrites ``reason`` to name the floor
condition and the original policy decision and rule (Requirement 5.8), and
appends a matching :class:`~trikon.evidence.report.RuleResult` to
``policy_results`` (Requirement 5.9).

Neither function takes a ``Policy``, so no policy setting can disable the
floor (Requirement 5.10). The module does no I/O and has no raise sites; it
runs inside the SDK's fail-closed ``try`` anyway, so any future
``TrikonError`` would still become ``require_human``.

Because a floored decision is never ``allow``, applying the floor twice gives
the same Verdict as applying it once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from trikon.evidence.report import (
    BrokenImport,
    Decision,
    Evidence,
    ImportReport,
    RuleResult,
    TestReport,
    Verdict,
)

FLOOR_BROKEN_IMPORTS: Final = "safety_floor.broken_imports"
FLOOR_INSUFFICIENT_EVIDENCE: Final = "safety_floor.insufficient_evidence"
FloorRuleId = Literal["safety_floor.broken_imports", "safety_floor.insufficient_evidence"]


@dataclass(frozen=True, slots=True)
class FloorResult:
    """Outcome of :func:`apply_safety_floor`.

    ``floor_rule`` is ``None`` when the floor left the decision unchanged; in
    that case ``condition`` is ``None`` too and ``decision`` is the input
    decision. Otherwise ``condition`` is the human-readable text that the
    Verdict ``reason`` and the floor's RuleResult carry.
    """

    decision: Decision
    floor_rule: FloorRuleId | None
    condition: str | None


def apply_safety_floor(
    decision: Decision,
    evidence: Evidence,
    is_python_change: bool,
) -> FloorResult:
    """Return the decision after the Safety_Floor, with the rule that fired.

    Only ``allow`` is ever changed. Broken imports are checked before the
    evidence gaps, so ``block`` wins over ``require_human``. The
    ``is_python_change`` flag is the caller's
    :func:`trikon.evidence.report.is_python_change` result for the change.
    """
    if decision != "allow":
        return FloorResult(decision=decision, floor_rule=None, condition=None)

    imports = evidence.verification.imports
    if imports.broken:
        return FloorResult(
            decision="block",
            floor_rule=FLOOR_BROKEN_IMPORTS,
            condition=_broken_imports_condition(imports.broken),
        )

    if is_python_change:
        gaps = _evidence_gaps(evidence.verification.tests, imports)
        if gaps:
            return FloorResult(
                decision="require_human",
                floor_rule=FLOOR_INSUFFICIENT_EVIDENCE,
                condition="Python change with " + "; ".join(gaps),
            )

    return FloorResult(decision=decision, floor_rule=None, condition=None)


def floor_verdict(verdict: Verdict, *, is_python_change: bool) -> Verdict:
    """Apply the Safety_Floor to a policy Verdict.

    Returns ``verdict`` itself when the floor does not fire. Otherwise returns
    a copy with the floor's ``decision``, ``matched_rule`` and ``reason``, and
    a new ``policy_results`` list ending in the floor's RuleResult. The input
    Verdict and its lists are never mutated. ``audit_id``, ``created_at``,
    ``warnings`` and ``schema_version`` carry over unchanged.
    """
    result = apply_safety_floor(verdict.decision, verdict.evidence, is_python_change)
    if result.floor_rule is None or result.condition is None:
        return verdict

    original_rule = verdict.matched_rule or "<no rule>"
    reason = (
        f"Safety floor {result.floor_rule}: {result.condition}. "
        f"Policy decided '{verdict.decision}' via rule '{original_rule}' "
        f"({verdict.reason})."
    )
    floor_result = RuleResult(
        rule_name=result.floor_rule,
        matched=True,
        would_emit=result.decision,
        reason=result.condition,
    )
    evidence = verdict.evidence.model_copy(
        update={"policy_results": [*verdict.evidence.policy_results, floor_result]},
    )
    return verdict.model_copy(
        update={
            "decision": result.decision,
            "matched_rule": result.floor_rule,
            "reason": reason,
            "evidence": evidence,
            # A fresh list, so the two Verdicts never share mutable state.
            "warnings": list(verdict.warnings),
        },
    )


# ---------------------------------------------------------------------------
# Condition text
# ---------------------------------------------------------------------------


def _broken_imports_condition(broken: list[BrokenImport]) -> str:
    """Describe the broken imports, naming the first one in report order.

    The Import_Checker sorts ``broken`` by path, then line (Requirement 4.11),
    so "first" is deterministic. A ``from M import n`` site renders as
    ``M.n``; an ``import M`` site as ``M``.
    """
    first = broken[0]
    target = first.module if first.name is None else f"{first.module}.{first.name}"
    return f"{len(broken)} broken import(s); first {first.path}:{first.line} -> {target}"


def _evidence_gaps(tests: TestReport, imports: ImportReport) -> list[str]:
    """Return one phrase per evidence gap, in a fixed order (empty if none)."""
    gaps: list[str] = []
    executed = tests.passed + tests.failed
    if executed == 0:
        gaps.append("0 executed tests")
    if tests.incomplete:
        detail = ", ".join(tests.incomplete_reasons) or "no reason recorded"
        gaps.append(f"incomplete test evidence ({detail})")
    if imports.incomplete:
        gaps.append(f"incomplete import analysis ({len(imports.unparsed_files)} unparsed file(s))")
    return gaps


__all__ = [
    "FLOOR_BROKEN_IMPORTS",
    "FLOOR_INSUFFICIENT_EVIDENCE",
    "FloorResult",
    "FloorRuleId",
    "apply_safety_floor",
    "floor_verdict",
]
