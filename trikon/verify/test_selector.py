"""Select the subset of pytest tests that exercise the impacted symbols.

Strategy (in priority order):
    1. Coverage-map lookup: `symbol → set(test_ids)` built during a prior full
       test run. Cached in ``.trikon/coverage.db``.
    2. Explicit inclusion: any test files that were themselves modified.
    3. Same-module heuristic: tests living in the same package as changed files
       (safety net for symbols with zero recorded coverage).

The coverage map ages. On green-verdict runs (where a full suite is available),
we opportunistically rebuild it. If the map is >7 days behind HEAD, we warn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trikon.change_intel.ast_indexer import SymbolDef


@dataclass(frozen=True)
class TestSelection:
    """The tests we intend to run for a given change."""

    test_ids: tuple[str, ...]      # pytest node IDs
    selected_via_coverage: int
    selected_via_filename: int
    selected_via_same_module: int
    coverage_map_stale: bool


def select_tests(
    impacted_symbols: list[SymbolDef],
    changed_test_files: list[Path],
    repo_path: Path,
    coverage_db: Path | None = None,
) -> TestSelection:
    """Return the pytest node IDs to execute for this change."""
    # TODO: implement — see docstring for the three strategies.
    raise NotImplementedError
