# Feature: trikon-engine-fail-safe, Property 10: Safety_Floor is idempotent
"""Property test: applying the Safety_Floor twice equals applying it once.

*For any* Verdict and ``is_python_change`` flag,
``floor_verdict(floor_verdict(v)) == floor_verdict(v)``. In particular the
second pass leaves the decision unchanged (Requirement 9.5) and does not
append a second floor RuleResult to ``policy_results``.

Two generators drive the property:

- every decision (``allow``, ``block``, ``require_human``, ``warn``), so the
  pass-through path is covered;
- ``allow`` only, so most examples take the path where the floor fires and
  the second pass sees a floored Verdict.

The evidence generators match the monotonicity test: ``tests.passed`` and
``tests.failed`` are each 0 half the time, ``tests.executed`` is drawn
independently of them, and ``tests.incomplete``, ``imports.broken`` and
``imports.incomplete`` all vary.

**Validates: Requirements 9.5**
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

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
)
from trikon.policy.floor import FLOOR_BROKEN_IMPORTS, FLOOR_INSUFFICIENT_EVIDENCE, floor_verdict

_FLOOR_RULE_IDS: Final[frozenset[str]] = frozenset(
    (FLOOR_BROKEN_IMPORTS, FLOOR_INSUFFICIENT_EVIDENCE),
)

# ---------------------------------------------------------------------------
# Scalar strategies
# ---------------------------------------------------------------------------

_DECISIONS: Final[tuple[Decision, ...]] = ("allow", "block", "require_human", "warn")
_DECISION: st.SearchStrategy[Decision] = st.sampled_from(_DECISIONS)

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

# Each of ``passed`` / ``failed`` is 0 half the time, so 0/0 comes up in
# about a quarter of the draws.
_TEST_COUNT: st.SearchStrategy[int] = st.just(0) | st.integers(min_value=1, max_value=1000)
_COUNT: st.SearchStrategy[int] = st.integers(min_value=0, max_value=1000)

_INCOMPLETE_REASONS: Final[tuple[IncompleteReason, ...]] = (
    "collection_timeout",
    "execution_timeout",
    "collection_error",
)

_CREATED_AT: st.SearchStrategy[datetime] = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 12, 31),
    timezones=st.just(UTC),
)

# ---------------------------------------------------------------------------
# Model strategies
# ---------------------------------------------------------------------------

# The floor never reads the ImpactSet (the Python_Change flag is passed in
# separately), so a small valid shape is enough.
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

_IMPORT_REPORT: st.SearchStrategy[ImportReport] = st.builds(
    ImportReport,
    broken=st.lists(_BROKEN_IMPORT, max_size=3),
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


def _verdicts(decision: st.SearchStrategy[Decision]) -> st.SearchStrategy[Verdict]:
    """Build Verdicts whose decision is drawn from ``decision``."""
    return st.builds(
        Verdict,
        decision=decision,
        reason=_TEXT,
        matched_rule=st.none() | _RULE_NAME,
        evidence=_EVIDENCE,
        audit_id=st.uuids(),
        created_at=_CREATED_AT,
        warnings=st.lists(_TEXT, max_size=2),
        schema_version=st.sampled_from((2, 3)),
    )


_ANY_VERDICT: st.SearchStrategy[Verdict] = _verdicts(_DECISION)
_ALLOW_VERDICT: st.SearchStrategy[Verdict] = _verdicts(st.just("allow"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _floor_rule_count(verdict: Verdict) -> int:
    """Count the floor RuleResults in ``verdict.evidence.policy_results``."""
    return sum(1 for r in verdict.evidence.policy_results if r.rule_name in _FLOOR_RULE_IDS)


def _assert_idempotent(verdict: Verdict, is_python_change: bool) -> None:
    """Assert that a second floor pass returns a Verdict equal to the first."""
    once = floor_verdict(verdict, is_python_change=is_python_change)
    once_dump = once.model_dump()

    twice = floor_verdict(once, is_python_change=is_python_change)

    assert twice == once
    assert twice.model_dump() == once_dump
    # Requirement 9.5 spelled out: the decision does not move on the second pass.
    assert twice.decision == once.decision
    # The second pass adds no further floor RuleResult.
    assert _floor_rule_count(twice) == _floor_rule_count(once)
    # The second pass does not mutate the first pass's output either.
    assert once.model_dump() == once_dump


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(verdict=_ANY_VERDICT, is_python_change=st.booleans())
def test_floor_verdict_is_idempotent(verdict: Verdict, is_python_change: bool) -> None:
    """Feature: trikon-engine-fail-safe, Property 10: Safety_Floor is idempotent.

    For any decision, flooring the floored Verdict again changes nothing.
    """
    _assert_idempotent(verdict, is_python_change)


@settings(max_examples=100, deadline=None)
@given(verdict=_ALLOW_VERDICT, is_python_change=st.booleans())
def test_floor_verdict_is_idempotent_on_allow(verdict: Verdict, is_python_change: bool) -> None:
    """Feature: trikon-engine-fail-safe, Property 10: Safety_Floor is idempotent.

    Starting from ``allow``, where the floor can fire, a second pass leaves
    the floored (or unfloored) Verdict as the first pass returned it.
    """
    _assert_idempotent(verdict, is_python_change)
