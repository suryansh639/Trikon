# Feature: trikon-engine-fail-safe, Property 4: Strategy decision table
"""Property test: ``choose_strategy`` follows the Requirement 1 decision table.

*For any* combination of ``python_change``, an empty or non-empty selection,
coverage map state (``present``, ``missing``, ``stale``) and base-SHA
presence, :func:`trikon.verify.strategy.choose_strategy` returns the strategy,
ordered reasons and node ids that Requirement 1 prescribes.

The expected value is not a copy of the implementation. :func:`_oracle`
restates the acceptance criteria one by one (1.3 to 1.7) and builds the
fallback reasons by filtering the fixed reason order through a per-reason
"does it apply" table taken from the glossary:

- Usable_Coverage_Map: the map exists, is not stale, and the Change has a
  base SHA;
- ``coverage_map_missing`` applies when the map does not exist;
- ``coverage_map_stale`` applies when the map exists but is stale;
- ``no_base_sha`` applies when the caller supplied no base SHA.

Two tests share the oracle:

- a hypothesis property over generated inputs, including arbitrary
  node-id-like selections (duplicates allowed, so verbatim pass-through is
  checked);
- an exhaustive parametrized test over all 24 combinations of the boolean
  and enum inputs with a representative selection, so no table row depends
  on what hypothesis happens to draw.

Both also check the cross-cutting invariants: ``none`` is never chosen for a
Python change, reasons are empty unless the strategy is ``full_suite``, and
``full_suite`` always carries at least one reason.

**Validates: Requirements 1.3, 1.4, 1.5, 1.6, 1.7, 9.3**
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon.evidence.report import StrategyReason, TestStrategy
from trikon.verify.strategy import (
    CoverageMapState,
    StrategyDecision,
    StrategyInputs,
    choose_strategy,
)

# ---------------------------------------------------------------------------
# Independent restatement of Requirement 1.3-1.7
# ---------------------------------------------------------------------------

# The fixed order Requirement 1.4 reasons are recorded in.
_FALLBACK_REASON_ORDER: tuple[StrategyReason, ...] = (
    "coverage_map_missing",
    "coverage_map_stale",
    "no_base_sha",
)

_COVERAGE_MAP_STATES: tuple[CoverageMapState, ...] = ("present", "missing", "stale")


@dataclass(frozen=True, slots=True)
class _Expected:
    """What Requirement 1 says the runner must record for one input."""

    strategy: TestStrategy
    reasons: tuple[StrategyReason, ...]
    node_ids: tuple[str, ...]


def _applicable_fallback_reasons(
    coverage_map_state: CoverageMapState, base_sha_supplied: bool
) -> tuple[StrategyReason, ...]:
    """Every Strategy_Reason of Requirement 1.4 that applies, in fixed order."""
    map_exists = coverage_map_state != "missing"
    map_is_stale = coverage_map_state == "stale"
    applies: dict[StrategyReason, bool] = {
        "coverage_map_missing": not map_exists,
        "coverage_map_stale": map_exists and map_is_stale,
        "no_base_sha": not base_sha_supplied,
    }
    return tuple(reason for reason in _FALLBACK_REASON_ORDER if applies[reason])


def _oracle(inputs: StrategyInputs) -> _Expected:
    """Restate acceptance criteria 1.3 to 1.7 as an expected decision."""
    selection_empty = len(inputs.selected) == 0
    usable_map = (
        inputs.coverage_map_state != "missing"
        and inputs.coverage_map_state != "stale"
        and inputs.base_sha_supplied
    )

    if inputs.python_change:
        # 1.3: a Python change with nothing selected runs the whole suite.
        if selection_empty:
            return _Expected("full_suite", ("empty_selection",), ())
        # 1.5: a usable map makes the selection trustworthy.
        if usable_map:
            return _Expected("selected", (), inputs.selected)
        # 1.4: a selection made without a usable map falls back to the
        # whole suite, recording every applicable reason.
        return _Expected(
            "full_suite",
            _applicable_fallback_reasons(inputs.coverage_map_state, inputs.base_sha_supplied),
            (),
        )

    # 1.6: a non-Python change runs whatever was selected.
    if not selection_empty:
        return _Expected("selected", (), inputs.selected)
    # 1.7: a non-Python change with nothing selected starts no test run.
    return _Expected("none", (), ())


def _assert_matches_requirements(inputs: StrategyInputs, decision: StrategyDecision) -> None:
    """Check ``decision`` against the oracle and the cross-cutting invariants."""
    expected = _oracle(inputs)
    assert decision.strategy == expected.strategy, inputs
    assert decision.reasons == expected.reasons, inputs
    assert decision.node_ids == expected.node_ids, inputs

    # ``none`` would mean no pytest run, which a Python change never gets.
    if inputs.python_change:
        assert decision.strategy != "none", inputs
    # Strategy_Reason explains a full-suite run and nothing else.
    if decision.strategy == "full_suite":
        assert len(decision.reasons) >= 1, inputs
        assert decision.node_ids == (), inputs
    else:
        assert decision.reasons == (), inputs
    if decision.strategy == "selected":
        assert decision.node_ids == inputs.selected, inputs
    if decision.strategy == "none":
        assert decision.node_ids == (), inputs
    # Reasons are distinct.
    assert len(set(decision.reasons)) == len(decision.reasons), inputs


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# pytest node-id-like strings: a test file under ``tests/`` (optionally in a
# package directory), optionally followed by ``::test_<name>``.
_NODE_ID: st.SearchStrategy[str] = st.from_regex(
    r"\Atests/(?:[a-z][a-z0-9_]{0,8}/)?test_[a-z0-9_]{1,12}\.py(?:::test_[a-z0-9_]{1,12})?\Z"
)

_SELECTION: st.SearchStrategy[tuple[str, ...]] = st.one_of(
    st.just(()),
    st.lists(_NODE_ID, min_size=1, max_size=6).map(tuple),
)

_STATE: st.SearchStrategy[CoverageMapState] = st.sampled_from(_COVERAGE_MAP_STATES)


# ---------------------------------------------------------------------------
# Property 4: hypothesis over generated inputs
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    python_change=st.booleans(),
    selected=_SELECTION,
    coverage_map_state=_STATE,
    base_sha_supplied=st.booleans(),
)
def test_choose_strategy_follows_decision_table(
    python_change: bool,
    selected: tuple[str, ...],
    coverage_map_state: CoverageMapState,
    base_sha_supplied: bool,
) -> None:
    """Feature: trikon-engine-fail-safe, Property 4: Strategy decision table."""
    inputs = StrategyInputs(
        python_change=python_change,
        selected=selected,
        coverage_map_state=coverage_map_state,
        base_sha_supplied=base_sha_supplied,
    )
    _assert_matches_requirements(inputs, choose_strategy(inputs))


# ---------------------------------------------------------------------------
# Exhaustive table: all 24 boolean/enum combinations
# ---------------------------------------------------------------------------

_REPRESENTATIVE_SELECTION: tuple[str, ...] = (
    "tests/test_worker.py::test_runs",
    "tests/orders/test_retry.py",
)

_COMBINATIONS: list[tuple[bool, bool, CoverageMapState, bool]] = list(
    itertools.product((False, True), (False, True), _COVERAGE_MAP_STATES, (False, True))
)


@pytest.mark.parametrize(
    ("python_change", "selection_non_empty", "coverage_map_state", "base_sha_supplied"),
    _COMBINATIONS,
    ids=[f"py={p}-sel={s}-map={m}-base={b}".lower() for p, s, m, b in _COMBINATIONS],
)
def test_choose_strategy_exhaustive_table(
    python_change: bool,
    selection_non_empty: bool,
    coverage_map_state: CoverageMapState,
    base_sha_supplied: bool,
) -> None:
    """Every row of the decision table, with a representative selection."""
    selected = _REPRESENTATIVE_SELECTION if selection_non_empty else ()
    inputs = StrategyInputs(
        python_change=python_change,
        selected=selected,
        coverage_map_state=coverage_map_state,
        base_sha_supplied=base_sha_supplied,
    )
    _assert_matches_requirements(inputs, choose_strategy(inputs))


# Literal anchors for the fallback row, so the oracle's reason order is pinned
# by hand-written values and not only by the oracle itself.
@pytest.mark.parametrize(
    ("coverage_map_state", "base_sha_supplied", "expected_reasons"),
    [
        ("missing", True, ("coverage_map_missing",)),
        ("missing", False, ("coverage_map_missing", "no_base_sha")),
        ("stale", True, ("coverage_map_stale",)),
        ("stale", False, ("coverage_map_stale", "no_base_sha")),
        ("present", False, ("no_base_sha",)),
    ],
)
def test_fallback_reasons_literal_order(
    coverage_map_state: CoverageMapState,
    base_sha_supplied: bool,
    expected_reasons: tuple[StrategyReason, ...],
) -> None:
    """Python change, non-empty selection, unusable map: exact ordered reasons."""
    inputs = StrategyInputs(
        python_change=True,
        selected=_REPRESENTATIVE_SELECTION,
        coverage_map_state=coverage_map_state,
        base_sha_supplied=base_sha_supplied,
    )
    assert _oracle(inputs).reasons == expected_reasons
    assert choose_strategy(inputs) == StrategyDecision(
        strategy="full_suite", reasons=expected_reasons, node_ids=()
    )
