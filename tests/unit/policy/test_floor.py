# Feature: trikon-engine-fail-safe, Property 8: Safety_Floor exact behaviour
"""Exact-behaviour tests for the Safety_Floor (``trikon.policy.floor``).

Property 8: *for any* Verdict and ``is_python_change`` flag:

- if the decision is ``allow`` and ``imports.broken`` is non-empty,
  ``floor_verdict`` returns ``block`` via ``safety_floor.broken_imports``;
- else, if the decision is ``allow``, the change is Python, and the evidence
  has ``passed + failed == 0``, ``tests.incomplete`` or ``imports.incomplete``,
  it returns ``require_human`` via ``safety_floor.insufficient_evidence``;
- otherwise it returns the Verdict unchanged.

Whenever the floor changes a decision it appends exactly one matching
RuleResult, the reason names the floor condition and the original decision
and rule, and ``audit_id``, ``created_at``, ``warnings`` and
``schema_version`` are preserved.

The example tests below pin one case per floor condition: broken imports
(with and without a Python change, and together with evidence gaps), each
insufficient-evidence gap, a non-Python change with gaps, a green Python
change, and the non-``allow`` decisions the floor must leave alone.

**Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9**
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ``TestReport`` is reached through the module so pytest does not try to
# collect it as a test class from this module's namespace.
from trikon.evidence import report
from trikon.evidence.report import (
    BrokenImport,
    Decision,
    Evidence,
    ImpactSet,
    ImportReport,
    IncompleteReason,
    RuleResult,
    StaticReport,
    Verdict,
    VerificationReport,
    is_python_change,
)
from trikon.policy.floor import (
    FLOOR_BROKEN_IMPORTS,
    FLOOR_INSUFFICIENT_EVIDENCE,
    FloorResult,
    FloorRuleId,
    apply_safety_floor,
    floor_verdict,
)

# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


def _expected_floor(verdict: Verdict, python_change: bool) -> tuple[Decision, FloorRuleId] | None:
    """Return the decision and rule Property 8 demands, or ``None`` for "unchanged"."""
    if verdict.decision != "allow":
        return None
    verification = verdict.evidence.verification
    if verification.imports.broken:
        return "block", FLOOR_BROKEN_IMPORTS
    executed = verification.tests.passed + verification.tests.failed
    if python_change and (
        executed == 0 or verification.tests.incomplete or verification.imports.incomplete
    ):
        return "require_human", FLOOR_INSUFFICIENT_EVIDENCE
    return None


def _expected_reason(rule_id: FloorRuleId, condition: str, original: Verdict) -> str:
    """The Verdict reason from design §8, built from the original Verdict."""
    original_rule = original.matched_rule or "<no rule>"
    return (
        f"Safety floor {rule_id}: {condition}. "
        f"Policy decided '{original.decision}' via rule '{original_rule}' "
        f"({original.reason})."
    )


def _broken_condition(broken: Sequence[BrokenImport]) -> str:
    """The broken-imports condition text, naming the first import in report order."""
    first = broken[0]
    target = first.module if first.name is None else f"{first.module}.{first.name}"
    return f"{len(broken)} broken import(s); first {first.path}:{first.line} -> {target}"


def _assert_condition_names_cause(condition: str, rule_id: FloorRuleId, original: Verdict) -> None:
    """Assert the condition text names exactly the floor condition(s) that held."""
    verification = original.evidence.verification
    if rule_id == FLOOR_BROKEN_IMPORTS:
        assert condition == _broken_condition(verification.imports.broken)
        return
    tests = verification.tests
    assert ("0 executed tests" in condition) == (tests.passed + tests.failed == 0)
    assert ("incomplete test evidence" in condition) == tests.incomplete
    assert ("incomplete import analysis" in condition) == verification.imports.incomplete


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# ``allow`` is drawn half the time, since it is the only decision the floor
# can change.
_DECISION: st.SearchStrategy[Decision] = st.sampled_from(
    ("allow", "allow", "allow", "block", "require_human", "warn"),
)

_TEXT: st.SearchStrategy[str] = st.text(max_size=20)
_PATH: st.SearchStrategy[str] = st.sampled_from(
    ("src/app.py", "src/pkg/__init__.py", "tests/test_app.py", "README.md", "docs/guide.md"),
)
_RULE_NAME: st.SearchStrategy[str] = st.sampled_from(
    (
        "broken static imports",
        "sensitive path requires human",
        "green, low-blast auto-allow",
        "custom rule",
    ),
)

# Each of ``passed`` / ``failed`` is 0 half the time, so Executed_Test_Count
# is 0 in about a quarter of the draws.
_TEST_COUNT: st.SearchStrategy[int] = st.just(0) | st.integers(min_value=1, max_value=1000)
_COUNT: st.SearchStrategy[int] = st.integers(min_value=0, max_value=1000)

_INCOMPLETE_REASONS: Final[tuple[IncompleteReason, ...]] = (
    "collection_timeout",
    "execution_timeout",
    "collection_error",
)

_IMPACT_SET: st.SearchStrategy[ImpactSet] = st.builds(
    ImpactSet,
    changed_files=st.lists(_PATH, max_size=3, unique=True),
    changed_symbols=st.builds(list),
    impacted_modules=st.builds(list),
    impacted_public_apis=st.builds(list),
    impacted_tests=st.builds(list),
    blast_radius_score=st.sampled_from(("LOW", "MEDIUM", "HIGH")),
    blast_radius_numeric=st.floats(min_value=0.0, max_value=100.0),
)

# ``executed`` is drawn independently of ``passed`` / ``failed``, so the
# stored field often disagrees with the outcomes the floor must count.
_TEST_REPORT: st.SearchStrategy[report.TestReport] = st.builds(
    report.TestReport,
    status=st.sampled_from(("passed", "failed", "skipped")),
    total=_COUNT,
    passed=_TEST_COUNT,
    failed=_TEST_COUNT,
    skipped=_COUNT,
    duration_ms=_COUNT,
    collected=_COUNT,
    executed=_COUNT,
    incomplete=st.booleans(),
    incomplete_reasons=st.lists(st.sampled_from(_INCOMPLETE_REASONS), max_size=3, unique=True),
)

_STATIC_REPORT: st.SearchStrategy[StaticReport] = st.builds(
    StaticReport,
    tools_run=st.lists(st.sampled_from(("ruff", "mypy")), max_size=2, unique=True),
    new_errors=_COUNT,
    new_warnings=_COUNT,
    preexisting_errors=_COUNT,
)

_BROKEN_IMPORT: st.SearchStrategy[BrokenImport] = st.builds(
    BrokenImport,
    path=_PATH,
    line=st.integers(min_value=1, max_value=10_000),
    module=st.sampled_from(("orders.worker", "pkg.util", "app")),
    name=st.none() | st.sampled_from(("run", "helper")),
    kind=st.sampled_from(("removed_module", "removed_name")),
)

# ``broken`` is empty half the time, so the insufficient-evidence branch is
# reached as often as the broken-imports branch.
_IMPORT_REPORT: st.SearchStrategy[ImportReport] = st.builds(
    ImportReport,
    broken=st.builds(list) | st.lists(_BROKEN_IMPORT, min_size=1, max_size=3),
    incomplete=st.booleans(),
    unparsed_files=st.lists(_PATH, max_size=2, unique=True),
)

_VERIFICATION_REPORT: st.SearchStrategy[VerificationReport] = st.builds(
    VerificationReport,
    tests=_TEST_REPORT,
    static=_STATIC_REPORT,
    sandbox_ms=_COUNT,
    total_ms=_COUNT,
    imports=_IMPORT_REPORT,
)

_RULE_RESULT: st.SearchStrategy[RuleResult] = st.builds(
    RuleResult,
    rule_name=_RULE_NAME,
    matched=st.booleans(),
    would_emit=st.none() | _DECISION,
    reason=st.none() | _TEXT,
)

_EVIDENCE: st.SearchStrategy[Evidence] = st.builds(
    Evidence,
    change=_IMPACT_SET,
    verification=_VERIFICATION_REPORT,
    policy_results=st.lists(_RULE_RESULT, max_size=3),
)

_VERDICT: st.SearchStrategy[Verdict] = st.builds(
    Verdict,
    decision=_DECISION,
    reason=_TEXT,
    matched_rule=st.none() | _RULE_NAME,
    evidence=_EVIDENCE,
    audit_id=st.uuids(),
    created_at=st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2100, 12, 31),
        timezones=st.just(UTC),
    ),
    warnings=st.lists(_TEXT, max_size=2),
    schema_version=st.sampled_from((2, 3)),
)

# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(verdict=_VERDICT, python_change=st.booleans())
def test_floor_exact_behaviour(verdict: Verdict, python_change: bool) -> None:
    """Feature: trikon-engine-fail-safe, Property 8: Safety_Floor exact behaviour.

    ``apply_safety_floor`` and ``floor_verdict`` agree with the oracle, and a
    floored Verdict carries the floor rule, the design §8 reason, exactly one
    appended RuleResult and the original identity fields.
    """
    original = copy.deepcopy(verdict)
    expected = _expected_floor(original, python_change)

    result = apply_safety_floor(original.decision, original.evidence, python_change)
    floored = floor_verdict(verdict, is_python_change=python_change)

    if expected is None:
        assert result == FloorResult(decision=original.decision, floor_rule=None, condition=None)
        assert floored == original
        return

    new_decision, rule_id = expected
    assert result.decision == new_decision
    assert result.floor_rule == rule_id
    condition = result.condition
    assert condition
    _assert_condition_names_cause(condition, rule_id, original)

    assert floored.decision == new_decision
    assert floored.matched_rule == rule_id
    assert floored.reason == _expected_reason(rule_id, condition, original)
    assert floored.evidence.policy_results == [
        *original.evidence.policy_results,
        RuleResult(rule_name=rule_id, matched=True, would_emit=new_decision, reason=condition),
    ]
    assert floored.evidence.change == original.evidence.change
    assert floored.evidence.verification == original.evidence.verification
    assert floored.audit_id == original.audit_id
    assert floored.created_at == original.created_at
    assert floored.warnings == original.warnings
    assert floored.schema_version == original.schema_version


# ---------------------------------------------------------------------------
# Examples: one per floor condition
# ---------------------------------------------------------------------------

_AUDIT_ID: Final = UUID("12345678-1234-5678-1234-567812345678")
_CREATED_AT: Final = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
_POLICY_RULE: Final = "green, low-blast auto-allow"
_POLICY_REASON: Final = "tests green and blast radius LOW"
_WARNINGS: Final = ("docs touched",)
_PRIOR_RESULT: Final = RuleResult(
    rule_name=_POLICY_RULE,
    matched=True,
    would_emit="allow",
    reason=_POLICY_REASON,
)


def _make_verdict(
    *,
    decision: Decision = "allow",
    matched_rule: str | None = _POLICY_RULE,
    changed_files: Sequence[str] = ("src/app.py",),
    passed: int = 3,
    failed: int = 0,
    executed: int | None = None,
    tests_incomplete: bool = False,
    incomplete_reasons: Sequence[IncompleteReason] = (),
    broken: Sequence[BrokenImport] = (),
    imports_incomplete: bool = False,
    unparsed_files: Sequence[str] = (),
) -> Verdict:
    """Build a policy Verdict; defaults describe a green, complete Python change."""
    if failed:
        status = "failed"
    elif passed:
        status = "passed"
    else:
        status = "skipped"
    tests = report.TestReport(
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        skipped=0,
        duration_ms=10,
        collected=passed + failed,
        executed=passed + failed if executed is None else executed,
        strategy="full_suite",
        incomplete=tests_incomplete,
        incomplete_reasons=list(incomplete_reasons),
    )
    verification = VerificationReport(
        tests=tests,
        static=StaticReport(
            tools_run=["ruff", "mypy"], new_errors=0, new_warnings=0, preexisting_errors=0
        ),
        sandbox_ms=5,
        total_ms=20,
        imports=ImportReport(
            broken=list(broken),
            incomplete=imports_incomplete,
            unparsed_files=list(unparsed_files),
        ),
    )
    change = ImpactSet(
        changed_files=list(changed_files),
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=1.0,
    )
    return Verdict(
        decision=decision,
        reason=_POLICY_REASON,
        matched_rule=matched_rule,
        evidence=Evidence(change=change, verification=verification, policy_results=[_PRIOR_RESULT]),
        audit_id=_AUDIT_ID,
        created_at=_CREATED_AT,
        warnings=list(_WARNINGS),
    )


def _floor(verdict: Verdict) -> Verdict:
    """Apply the floor with the flag the SDK would compute for this change."""
    return floor_verdict(verdict, is_python_change=is_python_change(verdict.evidence.change))


def _assert_floored(
    floored: Verdict,
    original: Verdict,
    *,
    decision: Decision,
    rule_id: FloorRuleId,
) -> str:
    """Assert the floor fired with ``decision`` / ``rule_id``; return the condition text."""
    assert floored.decision == decision
    assert floored.matched_rule == rule_id
    *kept, floor_result = floored.evidence.policy_results
    assert kept == original.evidence.policy_results
    assert floor_result.rule_name == rule_id
    assert floor_result.matched is True
    assert floor_result.would_emit == decision
    condition = floor_result.reason
    assert condition
    assert floored.reason == _expected_reason(rule_id, condition, original)
    assert floored.audit_id == original.audit_id
    assert floored.created_at == original.created_at
    assert floored.warnings == original.warnings
    assert floored.schema_version == original.schema_version
    return condition


_WORKER_IMPORT: Final = BrokenImport(
    path="tests/test_worker.py",
    line=6,
    module="orders.worker",
    kind="removed_module",
)
_HELPER_IMPORT: Final = BrokenImport(
    path="tests/test_worker.py",
    line=9,
    module="pkg.util",
    name="helper",
    kind="removed_name",
)


def test_green_python_change_stays_allow() -> None:
    verdict = _make_verdict()
    assert is_python_change(verdict.evidence.change)

    assert _floor(verdict) == verdict


@pytest.mark.parametrize(
    ("broken", "condition"),
    [
        (
            (_WORKER_IMPORT, _HELPER_IMPORT),
            "2 broken import(s); first tests/test_worker.py:6 -> orders.worker",
        ),
        ((_HELPER_IMPORT,), "1 broken import(s); first tests/test_worker.py:9 -> pkg.util.helper"),
    ],
)
def test_broken_imports_turn_allow_into_block(
    broken: tuple[BrokenImport, ...],
    condition: str,
) -> None:
    verdict = _make_verdict(broken=broken)

    floored = _floor(verdict)

    assert _assert_floored(floored, verdict, decision="block", rule_id=FLOOR_BROKEN_IMPORTS) == (
        condition
    )
    assert floored.reason == (
        f"Safety floor safety_floor.broken_imports: {condition}. "
        f"Policy decided 'allow' via rule '{_POLICY_RULE}' ({_POLICY_REASON})."
    )


def test_broken_imports_block_a_non_python_change() -> None:
    verdict = _make_verdict(changed_files=("README.md",), broken=(_WORKER_IMPORT,))
    assert not is_python_change(verdict.evidence.change)

    _assert_floored(_floor(verdict), verdict, decision="block", rule_id=FLOOR_BROKEN_IMPORTS)


def test_broken_imports_win_over_evidence_gaps() -> None:
    verdict = _make_verdict(
        passed=0,
        broken=(_WORKER_IMPORT,),
        tests_incomplete=True,
        incomplete_reasons=("collection_error",),
        imports_incomplete=True,
        unparsed_files=("src/bad.py",),
    )

    floored = _floor(verdict)

    _assert_floored(floored, verdict, decision="block", rule_id=FLOOR_BROKEN_IMPORTS)
    # Exactly one floor RuleResult, not one per condition.
    assert len(floored.evidence.policy_results) == 2


def test_zero_executed_tests_require_human() -> None:
    verdict = _make_verdict(passed=0, failed=0)

    condition = _assert_floored(
        _floor(verdict),
        verdict,
        decision="require_human",
        rule_id=FLOOR_INSUFFICIENT_EVIDENCE,
    )
    assert "0 executed tests" in condition


@pytest.mark.parametrize(
    ("passed", "failed", "stored_executed", "floored_decision"),
    [
        # The stored count claims tests ran, but no outcome was recorded.
        (0, 0, 5, "require_human"),
        # The stored count says 0, but outcomes show tests ran.
        (2, 0, 0, "allow"),
        # Failed tests were executed too.
        (0, 1, 0, "allow"),
    ],
)
def test_executed_count_comes_from_outcomes_not_stored_field(
    passed: int,
    failed: int,
    stored_executed: int,
    floored_decision: Decision,
) -> None:
    verdict = _make_verdict(passed=passed, failed=failed, executed=stored_executed)

    assert _floor(verdict).decision == floored_decision


@pytest.mark.parametrize("reason", ["collection_timeout", "execution_timeout", "collection_error"])
def test_incomplete_test_evidence_requires_human(reason: IncompleteReason) -> None:
    verdict = _make_verdict(tests_incomplete=True, incomplete_reasons=(reason,))

    condition = _assert_floored(
        _floor(verdict),
        verdict,
        decision="require_human",
        rule_id=FLOOR_INSUFFICIENT_EVIDENCE,
    )
    assert f"incomplete test evidence ({reason})" in condition
    assert "0 executed tests" not in condition


def test_incomplete_import_analysis_requires_human() -> None:
    verdict = _make_verdict(imports_incomplete=True, unparsed_files=("src/bad.py",))

    condition = _assert_floored(
        _floor(verdict),
        verdict,
        decision="require_human",
        rule_id=FLOOR_INSUFFICIENT_EVIDENCE,
    )
    assert "incomplete import analysis" in condition
    assert "incomplete test evidence" not in condition


def test_evidence_gaps_do_not_floor_a_non_python_change() -> None:
    verdict = _make_verdict(
        changed_files=("README.md", "docs/guide.md"),
        passed=0,
        tests_incomplete=True,
        incomplete_reasons=("execution_timeout",),
        imports_incomplete=True,
    )
    assert not is_python_change(verdict.evidence.change)

    assert _floor(verdict) == verdict


@pytest.mark.parametrize("decision", ["block", "require_human", "warn"])
def test_non_allow_decisions_are_kept(decision: Decision) -> None:
    verdict = _make_verdict(
        decision=decision,
        matched_rule="sensitive path requires human",
        passed=0,
        broken=(_WORKER_IMPORT,),
        tests_incomplete=True,
        imports_incomplete=True,
    )

    assert _floor(verdict) == verdict


def test_reason_names_no_rule_when_policy_matched_none() -> None:
    verdict = _make_verdict(matched_rule=None, passed=0)

    floored = _floor(verdict)

    _assert_floored(
        floored,
        verdict,
        decision="require_human",
        rule_id=FLOOR_INSUFFICIENT_EVIDENCE,
    )
    assert "Policy decided 'allow' via rule '<no rule>'" in floored.reason
