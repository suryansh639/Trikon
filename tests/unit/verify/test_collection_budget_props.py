# Feature: trikon-engine-fail-safe, Property 5: Deadline budget never exceeded
"""Property test: the test-stage budget split never runs past the deadline.

*For any* test budget B > 0, collection share s in (0, 1], start time t0 and
collection duration c <= ``collection_timeout(t0)``,
``c + execution_timeout(t0 + c) <= B`` holds, and both timeouts are >= 0.

The scenario follows the runner (design §7). The budget is built at
``created`` with ``deadline_at = created + B``, and the Collection_Pass starts
at ``t0 = created + delay`` for a delay >= 0. The delay goes up to 2 * B, so
some passes start after the deadline and exercise the clamp at 0. The runner
never starts execution when its timeout is 0, so the worst case for execution
is running for the whole timeout. The share is either the default (the 25%
the runner uses) or any float in (0, 1].

Every time is drawn on a 2**-16 second grid, and the magnitudes are bounded
so every sum and difference in the scenario is exact in a float64. That keeps
the final check a strict ``<= B``. With arbitrary floats, the only possible
overshoot is an ulp of rounding in ``t0 + c`` itself. That is clock
arithmetic, not the budget split overrunning.

``TestBudget`` is reached through its module so pytest does not try to
collect it.

**Validates: Requirements 1.8**
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from trikon.verify import collection

# Grid resolution: 2**-16 s, about 15 microseconds.
_TICKS_PER_SECOND: int = 2**16

# |created| up to 2**24 s (about 194 days of uptime on the monotonic clock).
_MAX_CREATED_TICKS: int = 2**40

# B up to 2**16 s (about 18 hours). The runner's real budget is a few minutes.
_MAX_BUDGET_TICKS: int = 2**32


def _seconds(ticks: int) -> float:
    """Return ``ticks`` grid steps as seconds (exact, since |ticks| < 2**53)."""
    return ticks / _TICKS_PER_SECOND


@dataclass(frozen=True, slots=True)
class _Scenario:
    """One test stage: a budget, the collection start and its duration."""

    budget: collection.TestBudget
    collection_start: float
    collection_seconds: float


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    """Draw a budget, a start time at or after it, and a collection duration."""
    created_ticks = draw(st.integers(-_MAX_CREATED_TICKS, _MAX_CREATED_TICKS))
    budget_ticks = draw(st.integers(1, _MAX_BUDGET_TICKS))
    delay_ticks = draw(st.integers(0, 2 * budget_ticks))
    share = draw(st.none() | st.floats(min_value=0.0, max_value=1.0, exclude_min=True))

    deadline_at = _seconds(created_ticks + budget_ticks)
    total_seconds = _seconds(budget_ticks)
    budget = (
        collection.TestBudget(deadline_at=deadline_at, total_seconds=total_seconds)
        if share is None
        else collection.TestBudget(
            deadline_at=deadline_at, total_seconds=total_seconds, collection_share=share
        )
    )

    start_ticks = created_ticks + delay_ticks
    # Any whole number of grid steps up to the collection timeout.
    max_collection_ticks = math.floor(
        budget.collection_timeout(_seconds(start_ticks)) * _TICKS_PER_SECOND
    )
    collection_ticks = draw(st.integers(0, max_collection_ticks))

    return _Scenario(
        budget=budget,
        collection_start=_seconds(start_ticks),
        collection_seconds=_seconds(collection_ticks),
    )


@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_deadline_budget_never_exceeded(scenario: _Scenario) -> None:
    """Feature: trikon-engine-fail-safe, Property 5: Deadline budget never exceeded."""
    budget = scenario.budget
    start = scenario.collection_start
    spent = scenario.collection_seconds

    collection_timeout = budget.collection_timeout(start)
    assert collection_timeout >= 0.0
    # The scenario's precondition: collection stayed within its timeout.
    assert spent <= collection_timeout

    # The runner reads the clock again once collection has finished.
    execution_timeout = budget.execution_timeout(start + spent)
    assert execution_timeout >= 0.0

    # Collection plus an execution that uses its whole timeout fits in B.
    assert spent + execution_timeout <= budget.total_seconds
