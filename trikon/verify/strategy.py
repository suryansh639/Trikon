"""Pure test-strategy decision for the Verification Runner.

The runner asks one question before it executes any tests: *which* tests may
it trust to stand in for the change? :func:`choose_strategy` answers it from
four facts and nothing else, so the answer is reproducible and the decision
table below is the whole contract (Requirement 1.3-1.7).

Decision table
--------------

``usable map`` means ``coverage_map_state == "present" and base_sha_supplied``.

=============  =========  ==========  ==========  ======================  ========
python_change  selected   usable map  strategy    reasons                 node_ids
=============  =========  ==========  ==========  ======================  ========
no             empty      any         none        ()                      ()
no             non-empty  any         selected    ()                      selected
yes            empty      any         full_suite  ("empty_selection",)    ()
yes            non-empty  yes         selected    ()                      selected
yes            non-empty  no          full_suite  fixed-order subset (*)  ()
=============  =========  ==========  ==========  ======================  ========

(*) The fallback reasons are emitted in this fixed order, each only when it
applies: ``coverage_map_missing`` (state is ``"missing"``),
``coverage_map_stale`` (state is ``"stale"``), ``no_base_sha`` (the caller did
not supply a base SHA). At least one always applies on this row, because the
map is unusable exactly when the state is not ``"present"`` or the base SHA is
absent.

Why the table looks like this:

* A Python change with no selected tests must never pass on zero tests, so it
  runs the whole suite (``empty_selection``).
* A Python change whose selection came from a missing or stale coverage map,
  or from a diff whose base the runner had to guess, is a heuristic guess.
  The runner runs the whole suite and records every reason the guess was
  untrustworthy.
* A change that touches no Python file cannot break a Python import or test
  through its own code, so it runs whatever was selected, or nothing at all
  (``none``). ``none`` is never produced for a Python change.

``node_ids`` holds the pytest positionals: the selected tuple, verbatim, for
``selected``; ``()`` for ``full_suite`` (pytest collects everything) and for
``none`` (no pytest test run starts).

This module imports only :mod:`trikon.evidence.report` and the standard
library. It has no I/O and no raise sites. :data:`CoverageMapState` lives here
rather than in :mod:`trikon.verify.models` so that ``models`` can import it
without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trikon.evidence.report import StrategyReason, TestStrategy

# Freshness of the coverage map that backed a test selection.
# ``present``: the map has rows, is recent enough and covered every symbol.
# ``missing``: the map has no rows at all.
# ``stale``: the map exists but is too old, or at least one symbol missed it.
CoverageMapState = Literal["present", "missing", "stale"]


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """The four facts :func:`choose_strategy` decides from.

    ``python_change`` is true when any changed path (old or new) is a Python
    file. ``selected`` holds the Test_Selector's node IDs in the order it
    produced them. ``coverage_map_state`` describes the map that backed that
    selection. ``base_sha_supplied`` is true only when the caller passed the
    base SHA, not when the runner derived it.
    """

    python_change: bool
    selected: tuple[str, ...]
    coverage_map_state: CoverageMapState
    base_sha_supplied: bool


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """The chosen strategy, why it was chosen, and the pytest positionals.

    ``reasons`` is empty unless ``strategy == "full_suite"``. ``node_ids`` is
    the selected tuple for ``selected`` and ``()`` for ``full_suite`` and
    ``none``.
    """

    strategy: TestStrategy
    reasons: tuple[StrategyReason, ...]
    node_ids: tuple[str, ...]


def _unusable_map_reasons(inputs: StrategyInputs) -> tuple[StrategyReason, ...]:
    """Return why the coverage map is unusable, in the fixed order.

    An empty result means the map is usable: the state is ``"present"`` and
    the caller supplied the base SHA.
    """
    reasons: list[StrategyReason] = []
    if inputs.coverage_map_state == "missing":
        reasons.append("coverage_map_missing")
    if inputs.coverage_map_state == "stale":
        reasons.append("coverage_map_stale")
    if not inputs.base_sha_supplied:
        reasons.append("no_base_sha")
    return tuple(reasons)


def choose_strategy(inputs: StrategyInputs) -> StrategyDecision:
    """Pick the test strategy for one change, following the module's table.

    Pure and total: every combination of inputs maps to exactly one row of
    the decision table in the module docstring.
    """
    if not inputs.python_change:
        if inputs.selected:
            return StrategyDecision(strategy="selected", reasons=(), node_ids=inputs.selected)
        return StrategyDecision(strategy="none", reasons=(), node_ids=())

    if not inputs.selected:
        return StrategyDecision(strategy="full_suite", reasons=("empty_selection",), node_ids=())

    reasons = _unusable_map_reasons(inputs)
    if reasons:
        return StrategyDecision(strategy="full_suite", reasons=reasons, node_ids=())
    return StrategyDecision(strategy="selected", reasons=(), node_ids=inputs.selected)


__all__ = [
    "CoverageMapState",
    "StrategyDecision",
    "StrategyInputs",
    "choose_strategy",
]
