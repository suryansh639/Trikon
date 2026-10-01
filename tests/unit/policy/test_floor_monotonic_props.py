# Feature: trikon-engine-fail-safe, Property 9: Safety_Floor is monotonic
"""Property test: the Safety_Floor never lowers a decision.

*For any* Verdict and ``is_python_change`` flag,
``severity(floor_verdict(v).decision) >= severity(v.decision)`` under the
Decision_Severity ordering ``allow < require_human < block``. ``warn`` is
outside that ordering, so a ``warn`` input must come back unchanged.

Both entry points are checked:

- :func:`~trikon.policy.floor.apply_safety_floor` on a bare decision and
  Evidence;
- :func:`~trikon.policy.floor.floor_verdict` on a whole Verdict.

For a ``block``, ``require_human`` or ``warn`` input the floor keeps the
decision, ``matched_rule``, ``reason`` and ``policy_results``
(Requirement 5.7). For every input, ``floor_verdict`` keeps ``audit_id``,
``created_at``, ``warnings`` and ``schema_version``, and never mutates the
Verdict it was given.

The generators cover every input the floor reads:

- ``tests.passed`` and ``tests.failed``, each 0 half the time, so the 0/0
  case (Executed_Test_Count 0) is common. The stored ``tests.executed`` is
  drawn independently, so it can disagree with them;
- ``tests.incomplete``, with or without ``incomplete_reasons``;
- ``imports.broken`` empty or not, and ``imports.incomplete``;
- the ``is_python_change`` flag and all four decisions.

**Validates: Requirements 5.7, 9.4**
"""

from __future__ import annotations

import copy
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
from trikon.policy.floor import apply_safety_floor, floor_verdict

# Decision_Severity. ``warn`` is deliberately absent: it is not in the ordering.
_SEVERITY: Final[dict[Decision, int]] = {"allow": 0, "require_human": 1, "block": 2}

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

_VERDICT: st.SearchStrategy[Verdict] = st.builds(
    Verdict,
    decision=_DECISION,
    reason=_TEXT,
    matched_rule=st.none() | _RULE_NAME,
    evidence=_EVIDENCE,
    audit_id=st.uuids(),
    created_at=_CREATED_AT,
    warnings=st.lists(_TEXT, max_size=2),
    schema_version=st.sampled_from((2, 3)),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_not_lowered(before: Decision, after: Decision) -> None:
    """Assert ``after`` is at least as severe as ``before``; ``warn`` stays ``warn``."""
    if before == "warn":
        assert after == "warn"
        return
    assert after in _SEVERITY, f"floor turned {before!r} into {after!r}"
    assert _SEVERITY[after] >= _SEVERITY[before], f"floor lowered {before!r} to {after!r}"


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(decision=_DECISION, evidence=_EVIDENCE, is_python_change=st.booleans())
def test_apply_safety_floor_is_monotonic(
    decision: Decision,
    evidence: Evidence,
    is_python_change: bool,
) -> None:
    """Feature: trikon-engine-fail-safe, Property 9: Safety_Floor is monotonic.

    ``apply_safety_floor`` never returns a less severe decision than it was
    given, and leaves every non-``allow`` decision as it was.
    """
    result = apply_safety_floor(decision, evidence, is_python_change)

    _assert_not_lowered(decision, result.decision)
    if decision != "allow":
        assert result.decision == decision
        assert result.floor_rule is None
        assert result.condition is None


@settings(max_examples=100, deadline=None)
@given(verdict=_VERDICT, is_python_change=st.booleans())
def test_floor_verdict_is_monotonic(verdict: Verdict, is_python_change: bool) -> None:
    """Feature: trikon-engine-fail-safe, Property 9: Safety_Floor is monotonic.

    ``floor_verdict`` never returns a less severe decision than the Verdict
    carried. It keeps a non-``allow`` Verdict's decision, ``matched_rule``,
    ``reason`` and ``policy_results``, always keeps ``audit_id``,
    ``created_at``, ``warnings`` and ``schema_version``, and does not mutate
    its input.
    """
    original = copy.deepcopy(verdict)
    original_dump = verdict.model_dump()

    floored = floor_verdict(verdict, is_python_change=is_python_change)

    # The input Verdict is untouched, including its nested lists.
    assert verdict == original
    assert verdict.model_dump() == original_dump

    _assert_not_lowered(original.decision, floored.decision)

    if original.decision != "allow":
        assert floored.decision == original.decision
        assert floored.matched_rule == original.matched_rule
        assert floored.reason == original.reason
        assert floored.evidence.policy_results == original.evidence.policy_results

    assert floored.audit_id == original.audit_id
    assert floored.created_at == original.created_at
    assert floored.warnings == original.warnings
    assert floored.schema_version == original.schema_version
