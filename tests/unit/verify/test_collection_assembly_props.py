# Feature: trikon-engine-fail-safe, Property 7: TestReport assembly invariants
"""Property test: ``assemble_test_report`` keeps the Requirement 2 invariants.

*For any* strategy decision, collection outcome (timed out or not, with any
mix of attributable and non-attributable errors), execution report or
timeout, and ``python_change`` flag, the assembled TestReport satisfies:

- ``executed == passed + failed``, and ``incomplete == bool(incomplete_reasons)``,
  with the reasons in the canonical order;
- status is ``failed`` whenever an attributable error exists;
- otherwise, with strategy ``none`` and ``python_change`` false, status is
  ``passed`` and ``total == executed == 0``;
- otherwise, on a timeout, status is ``failed`` if ``failed > 0`` and
  ``skipped`` if not;
- otherwise, status is ``passed`` only if ``executed > 0 and failed == 0``,
  and ``skipped`` when ``executed == 0``;
- the counts equal those of the execution report.

The generators pair strategy ``none`` with ``execution=None`` and no
execution timeout, because a ``none`` decision never starts a pytest run.
Every other input is drawn independently, so the invariants are checked on a
superset of what the runner can produce. The runner's display-only
"sandbox exceeded 5-minute deadline" failure entry is added by the runner,
not by assembly, so it is not expected here.

The modules are imported whole where a ``Test*`` class lives
(``report.TestReport``, ``report.TestResult``) so pytest does not try to
collect them.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.10**
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import event, given, settings
from hypothesis import strategies as st

from trikon.evidence import report
from trikon.evidence.report import CollectionError, IncompleteReason, StrategyReason
from trikon.verify.collection import (
    CollectionOutcome,
    RawCollectionError,
    assemble_test_report,
)
from trikon.verify.strategy import StrategyDecision

# The canonical order of Incomplete_Reasons (design §5).
_CANONICAL_REASONS: tuple[IncompleteReason, ...] = (
    "collection_timeout",
    "execution_timeout",
    "collection_error",
)

# Every reason tuple ``choose_strategy`` can attach to a ``full_suite`` run.
_FULL_SUITE_REASONS: tuple[tuple[StrategyReason, ...], ...] = (
    ("empty_selection",),
    ("coverage_map_missing",),
    ("coverage_map_stale",),
    ("no_base_sha",),
    ("coverage_map_missing", "no_base_sha"),
    ("coverage_map_stale", "no_base_sha"),
)


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One generated call to :func:`assemble_test_report`."""

    python_change: bool
    decision: StrategyDecision
    collection: CollectionOutcome
    classified: tuple[CollectionError, ...]
    execution: report.TestReport | None
    execution_timed_out: bool
    coverage_map_stale: bool


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_TEST_FILE: st.SearchStrategy[str] = st.from_regex(
    r"\Atests/(?:[a-z][a-z0-9_]{0,6}/)?test_[a-z0-9_]{1,10}\.py\Z"
)

_NODE_ID: st.SearchStrategy[str] = st.builds(
    lambda path, name: f"{path}::test_{name}",
    _TEST_FILE,
    st.from_regex(r"\A[a-z0-9_]{1,10}\Z"),
)

_DECISION: st.SearchStrategy[StrategyDecision] = st.one_of(
    st.lists(_NODE_ID, min_size=1, max_size=4).map(
        lambda ids: StrategyDecision(strategy="selected", reasons=(), node_ids=tuple(ids))
    ),
    st.sampled_from(_FULL_SUITE_REASONS).map(
        lambda reasons: StrategyDecision(strategy="full_suite", reasons=reasons, node_ids=())
    ),
    st.just(StrategyDecision(strategy="none", reasons=(), node_ids=())),
)

# True one time in four. Used for the inputs that override the completed-run
# rules (attributable errors, timeouts, a missing execution report), so the
# completed-run branches still get a fair share of the 100 examples.
_SOMETIMES: st.SearchStrategy[bool] = st.sampled_from((False, False, False, True))

_CLASSIFIED_ERROR: st.SearchStrategy[CollectionError] = st.builds(
    CollectionError,
    path=st.one_of(_TEST_FILE, st.just("<session>")),
    message=st.text(min_size=1, max_size=40),
    attributable=_SOMETIMES,
)


@st.composite
def _execution_report(draw: st.DrawFn) -> report.TestReport:
    """A TestReport as ``_parse_pytest_json_report`` would return it."""
    passed = draw(st.integers(min_value=0, max_value=20))
    failed = draw(st.integers(min_value=0, max_value=5))
    skipped = draw(st.integers(min_value=0, max_value=5))
    # pytest-json-report's ``total`` can also count errored items.
    errored = draw(st.integers(min_value=0, max_value=3))
    failures = [
        report.TestResult(
            node_id=f"tests/test_gen.py::test_f{index}",
            outcome="failed",
            duration_ms=draw(st.integers(min_value=0, max_value=5_000)),
            failure_summary=draw(st.one_of(st.none(), st.text(max_size=20))),
        )
        for index in range(failed)
    ]
    return report.TestReport(
        status=draw(st.sampled_from(("passed", "failed", "skipped"))),
        total=passed + failed + skipped + errored,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_ms=draw(st.integers(min_value=0, max_value=10**6)),
        failures=failures,
    )


@st.composite
def _scenario(draw: st.DrawFn) -> _Scenario:
    decision = draw(_DECISION)
    classified = tuple(draw(st.lists(_CLASSIFIED_ERROR, max_size=3)))
    collection = CollectionOutcome(
        timed_out=draw(_SOMETIMES),
        collected=draw(st.integers(min_value=0, max_value=50)),
        errors=tuple(
            RawCollectionError(path=error.path, message=error.message, frame_paths=())
            for error in classified
        ),
    )
    if decision.strategy == "none":
        # A ``none`` decision starts no pytest run, so there is no execution
        # report and nothing to time out.
        execution: report.TestReport | None = None
        execution_timed_out = False
    else:
        execution = None if draw(_SOMETIMES) else draw(_execution_report())
        execution_timed_out = draw(_SOMETIMES)
    return _Scenario(
        python_change=draw(st.booleans()),
        decision=decision,
        collection=collection,
        classified=classified,
        execution=execution,
        execution_timed_out=execution_timed_out,
        coverage_map_stale=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(scenario=_scenario())
def test_assembled_test_report_invariants(scenario: _Scenario) -> None:
    """Feature: trikon-engine-fail-safe, Property 7: TestReport assembly invariants."""
    tests = assemble_test_report(
        python_change=scenario.python_change,
        decision=scenario.decision,
        collection=scenario.collection,
        classified=scenario.classified,
        execution=scenario.execution,
        execution_timed_out=scenario.execution_timed_out,
        coverage_map_stale=scenario.coverage_map_stale,
    )
    execution = scenario.execution
    attributable = [error for error in scenario.classified if error.attributable]
    timed_out = scenario.collection.timed_out or scenario.execution_timed_out

    # Counts come only from the execution report (Req 2.1); 0 without one.
    assert tests.total == (execution.total if execution else 0)
    assert tests.passed == (execution.passed if execution else 0)
    assert tests.failed == (execution.failed if execution else 0)
    assert tests.skipped == (execution.skipped if execution else 0)
    assert tests.duration_ms == (execution.duration_ms if execution else 0)
    assert tests.executed == tests.passed + tests.failed

    # Every TestReport records the strategy and collection fields (Req 2.5).
    assert tests.strategy == scenario.decision.strategy
    assert tests.strategy_reasons == list(scenario.decision.reasons)
    assert tests.collected == scenario.collection.collected
    assert tests.coverage_map_stale is scenario.coverage_map_stale
    assert tests.collection_errors == list(scenario.classified)

    # Attributable errors are appended to ``failures`` as errored entries,
    # after the execution's own failures (Req 2.6).
    assert tests.failures == [
        *(execution.failures if execution else []),
        *(
            report.TestResult(
                node_id=error.path,
                outcome="errored",
                duration_ms=0,
                failure_summary=error.message,
            )
            for error in attributable
        ),
    ]

    # Incomplete reasons: exactly the ones that apply, in canonical order
    # (Req 2.7, 2.10), and ``incomplete`` mirrors the list.
    applies: dict[IncompleteReason, bool] = {
        "collection_timeout": scenario.collection.timed_out,
        "execution_timeout": scenario.execution_timed_out,
        "collection_error": any(not error.attributable for error in scenario.classified),
    }
    assert tests.incomplete_reasons == [r for r in _CANONICAL_REASONS if applies[r]]
    assert tests.incomplete is bool(tests.incomplete_reasons)

    # Status, rule by rule in the documented order. ``event`` labels show the
    # branch mix under ``pytest --hypothesis-show-statistics``.
    if attributable:
        # Req 2.6.
        event("status rule: attributable error")
        assert tests.status == "failed"
    elif scenario.decision.strategy == "none" and not scenario.python_change:
        # Req 2.4.
        event("status rule: strategy none, non-Python change")
        assert tests.status == "passed"
        assert tests.total == 0
        assert tests.executed == 0
    elif timed_out:
        # Req 2.10.
        event(f"status rule: timeout, failed={tests.failed > 0}")
        assert tests.status == ("failed" if tests.failed > 0 else "skipped")
    else:
        # Req 2.2, 2.3: a completed run passes only with executed tests and
        # no failures, and is skipped when nothing executed.
        event(f"status rule: completed run, {tests.status}")
        if tests.failed > 0:
            assert tests.status == "failed"
        elif tests.executed > 0:
            assert tests.status == "passed"
        else:
            assert tests.status == "skipped"

    # A Python change never passes on zero executed tests or with a failure,
    # whichever rule decided the status (Req 2.2).
    if scenario.python_change and tests.status == "passed":
        assert tests.executed > 0
        assert tests.failed == 0
