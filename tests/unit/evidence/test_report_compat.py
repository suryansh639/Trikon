"""Backward-compatibility tests for the engine fail-safe model additions.

The engine fail-safe work added fields to :class:`TestReport`,
:class:`VerificationReport` and :class:`Verdict`, plus the new
:class:`ImportReport`. Every addition has a default, so:

- a constructor call written for the previous release (only the arguments
  that release knew about) still validates and picks up the new defaults
  (Requirement 7.6);
- the :data:`EMPTY_VERIFICATION` sentinel records ``strategy="none"``,
  because nothing ran;
- a Verdict JSON document written by the previous release
  (``tests/fixtures/verdicts/verdict_0_4_1.json``) parses without error,
  keeps ``schema_version == 2`` and every stored value, and fills every new
  field with its default (Requirements 7.4, 7.5).

The fixture is the ``bad_retry`` Sample_Repo_Suite Verdict produced by the
published previous release (spec task 1.2/1.3 baseline dump), so its tests,
failures, static report and policy trace are all populated.

**Validates: Requirements 7.4, 7.5, 7.6**
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

# ``TestReport`` and ``TestResult`` are reached through the module so pytest
# does not try to collect them as test classes from this module's namespace.
from trikon.evidence import report
from trikon.evidence.report import (
    EMPTY_VERIFICATION,
    Evidence,
    ImpactSet,
    ImportReport,
    RuleResult,
    StaticReport,
    Verdict,
    VerificationReport,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "verdicts" / "verdict_0_4_1.json"

# Field names each model had in the previous release, in declaration order.
_PREVIOUS_TEST_REPORT_FIELDS = (
    "status",
    "total",
    "passed",
    "failed",
    "skipped",
    "duration_ms",
    "failures",
    "coverage_map_stale",
)
_PREVIOUS_VERIFICATION_FIELDS = ("tests", "static", "plugins", "sandbox_ms", "total_ms")
_PREVIOUS_VERDICT_FIELDS = (
    "decision",
    "reason",
    "matched_rule",
    "evidence",
    "audit_id",
    "created_at",
    "warnings",
    "schema_version",
)

# Keys the engine fail-safe work added. None of them may appear in the
# previous-release fixture.
_NEW_TEST_REPORT_KEYS = frozenset(
    {
        "collected",
        "executed",
        "strategy",
        "strategy_reasons",
        "incomplete",
        "incomplete_reasons",
        "collection_errors",
    }
)
_NEW_VERIFICATION_KEYS = frozenset({"imports"})


def _previous_release_test_report() -> report.TestReport:
    """Build a TestReport with exactly the previous-release keyword arguments."""
    return report.TestReport(
        status="failed",
        total=3,
        passed=2,
        failed=1,
        skipped=0,
        duration_ms=12,
        failures=[
            report.TestResult(
                node_id="tests/test_retry.py::test_retries_until_success",
                outcome="failed",
                duration_ms=4,
                failure_summary="assert 1 == 2",
            )
        ],
        coverage_map_stale=True,
    )


def _previous_release_static_report() -> StaticReport:
    return StaticReport(
        tools_run=["ruff", "mypy"],
        new_errors=0,
        new_warnings=0,
        preexisting_errors=0,
        findings=[],
    )


def _previous_release_verification() -> VerificationReport:
    """Build a VerificationReport with exactly the previous-release keyword arguments."""
    return VerificationReport(
        tests=_previous_release_test_report(),
        static=_previous_release_static_report(),
        plugins=[],
        sandbox_ms=100,
        total_ms=120,
    )


def _assert_new_test_report_defaults(tests: report.TestReport) -> None:
    """Every TestReport field added by the fail-safe work holds its default."""
    assert tests.collected == 0
    assert tests.executed == 0
    assert tests.strategy == "selected"
    assert tests.strategy_reasons == []
    assert tests.incomplete is False
    assert tests.incomplete_reasons == []
    assert tests.collection_errors == []


def _assert_contains(expected: object, actual: object, where: str) -> None:
    """Assert ``actual`` holds every key and value of ``expected``, recursively.

    Dicts in ``actual`` may carry extra keys (the new fields); lists must
    match element by element; scalars must be equal.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), where
        for key, value in expected.items():
            assert key in actual, f"{where}.{key} missing"
            _assert_contains(value, actual[key], f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), where
        assert len(actual) == len(expected), where
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            _assert_contains(expected_item, actual_item, f"{where}[{index}]")
    else:
        assert actual == expected, where


def _load_fixture_object() -> dict[str, object]:
    raw: object = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


# ---------------------------------------------------------------------------
# Construction with previous-release arguments (Requirement 7.6)
# ---------------------------------------------------------------------------


def test_previous_release_fields_are_kept_in_order() -> None:
    """Every previous-release field still exists, in the same position (Req 7.4)."""
    tests_now = tuple(report.TestReport.model_fields)
    tests_before = _PREVIOUS_TEST_REPORT_FIELDS
    verification_now = tuple(VerificationReport.model_fields)
    verification_before = _PREVIOUS_VERIFICATION_FIELDS

    assert tests_now[: len(tests_before)] == tests_before
    assert set(tests_now) - set(tests_before) == _NEW_TEST_REPORT_KEYS
    assert verification_now[: len(verification_before)] == verification_before
    assert set(verification_now) - set(verification_before) == _NEW_VERIFICATION_KEYS
    assert tuple(Verdict.model_fields) == _PREVIOUS_VERDICT_FIELDS


def test_test_report_with_previous_release_kwargs_gets_new_defaults() -> None:
    tests = _previous_release_test_report()

    assert tests.status == "failed"
    assert (tests.total, tests.passed, tests.failed, tests.skipped) == (3, 2, 1, 0)
    assert tests.coverage_map_stale is True
    _assert_new_test_report_defaults(tests)


def test_verification_report_with_previous_release_kwargs_gets_empty_imports() -> None:
    verification = _previous_release_verification()

    assert verification.imports == ImportReport()
    _assert_new_test_report_defaults(verification.tests)


def test_import_report_defaults_mean_nothing_broken() -> None:
    imports = ImportReport()

    assert imports.broken == []
    assert imports.incomplete is False
    assert imports.unparsed_files == []


def test_verdict_with_previous_release_kwargs_gets_current_schema_version() -> None:
    change = ImpactSet(
        changed_files=["src/payments/retry.py"],
        changed_symbols=[],
        impacted_modules=["payments"],
        impacted_public_apis=[],
        impacted_tests=["tests/test_retry.py"],
        blast_radius_score="MEDIUM",
        blast_radius_numeric=9.5,
    )
    verdict = Verdict(
        decision="block",
        reason="One or more impacted tests failed.",
        matched_rule="impacted tests failed",
        evidence=Evidence(
            change=change,
            verification=_previous_release_verification(),
            policy_results=[
                RuleResult(
                    rule_name="impacted tests failed",
                    matched=True,
                    would_emit="block",
                    reason="One or more impacted tests failed.",
                )
            ],
        ),
        audit_id=UUID("00000000-0000-4000-8000-000000000001"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        warnings=[],
    )

    # A freshly built Verdict carries the bumped version (Requirement 7.8).
    assert verdict.schema_version == 3
    assert verdict.evidence.verification.imports == ImportReport()
    _assert_new_test_report_defaults(verdict.evidence.verification.tests)


def test_empty_verification_sentinel_records_strategy_none() -> None:
    tests = EMPTY_VERIFICATION.tests

    assert tests.strategy == "none"
    assert tests.status == "skipped"
    assert (tests.total, tests.passed, tests.failed, tests.skipped) == (0, 0, 0, 0)
    assert tests.collected == 0
    assert tests.executed == 0
    assert tests.strategy_reasons == []
    assert tests.incomplete is False
    assert tests.incomplete_reasons == []
    assert tests.collection_errors == []
    assert EMPTY_VERIFICATION.imports == ImportReport()


# ---------------------------------------------------------------------------
# Previous-release Verdict JSON fixture (Requirements 7.4, 7.5)
# ---------------------------------------------------------------------------


def test_fixture_is_previous_release_shaped() -> None:
    """Guard the fixture itself: schema_version 2 and none of the new keys."""
    raw = _load_fixture_object()
    evidence = raw["evidence"]
    assert isinstance(evidence, dict)
    verification = evidence["verification"]
    assert isinstance(verification, dict)
    tests = verification["tests"]
    assert isinstance(tests, dict)

    assert raw["schema_version"] == 2
    assert _NEW_VERIFICATION_KEYS.isdisjoint(verification)
    assert _NEW_TEST_REPORT_KEYS.isdisjoint(tests)


def test_fixture_parses_with_schema_version_2_and_new_defaults() -> None:
    verdict = Verdict.model_validate_json(_FIXTURE.read_text(encoding="utf-8"))

    assert verdict.schema_version == 2
    assert verdict.decision == "require_human"
    assert verdict.matched_rule == "sensitive path requires human"
    assert verdict.warnings == []

    tests = verdict.evidence.verification.tests
    assert tests.status == "failed"
    assert (tests.total, tests.passed, tests.failed, tests.skipped) == (3, 2, 1, 0)
    assert [failure.node_id for failure in tests.failures] == [
        "tests/test_retry.py::test_retries_until_success"
    ]
    assert tests.coverage_map_stale is True
    _assert_new_test_report_defaults(tests)

    assert verdict.evidence.verification.imports == ImportReport()
    assert verdict.evidence.verification.static.tools_run == ["ruff", "mypy"]
    assert len(verdict.evidence.policy_results) == 6


def test_fixture_values_survive_parsing_unchanged() -> None:
    """Every stored key keeps its name and value after a parse and dump (Req 7.4).

    The dump may add the new keys; it must not drop, rename or retype any
    key the previous release wrote.
    """
    raw = _load_fixture_object()
    verdict = Verdict.model_validate(raw)

    dumped: object = verdict.model_dump(mode="json")

    _assert_contains(raw, dumped, "verdict")
