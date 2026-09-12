"""Compute the blast radius of a change.

The impact set is the union of:
    - directly changed symbols (from the diff)
    - transitive dependents (from the dep graph, up to N hops)
    - tests that exercise any of the above (from the coverage map)

A scalar `blast_radius_score` is produced by weighted combination and bucketed
into LOW / MEDIUM / HIGH for policy consumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trikon.change_intel.ast_indexer import SymbolDef
from trikon.change_intel.diff_parser import ChangeSet
from trikon.evidence.report import ImpactSet


@dataclass(frozen=True)
class BlastWeights:
    """Weights that shape the numeric blast-radius score.

    Defaults are calibrated for a mid-size Python service; large monorepos may
    want lower weights on module counts to avoid HIGH-bucket saturation.
    """

    impacted_modules: float = 1.0
    impacted_public_apis: float = 3.0
    impacted_test_files: float = 0.5
    cross_package_hops: float = 2.0
    sensitive_path_touch: float = 5.0


def compute_impact(
    change_set: ChangeSet,
    repo_path: Path,
    weights: BlastWeights | None = None,
) -> ImpactSet:
    """Given a ChangeSet, compute the full ImpactSet.

    Steps:
        1. Map each changed hunk to the enclosing symbols (via ast_indexer).
        2. Query the dep_graph for transitive dependents.
        3. Look up covering tests in the coverage map (verify.test_selector).
        4. Compute the numeric score and bucket it.
    """
    raise NotImplementedError


def bucket(score: float) -> str:
    """Bucket a numeric score into LOW / MEDIUM / HIGH."""
    if score < 5.0:
        return "LOW"
    if score < 15.0:
        return "MEDIUM"
    return "HIGH"


def enclosing_symbols(
    file_path: Path,
    changed_lines: list[int],
    symbols: list[SymbolDef],
) -> list[SymbolDef]:
    """Return the symbols whose line ranges contain any of `changed_lines`."""
    # TODO: implement — this is straightforward once the symbol table has line ranges.
    raise NotImplementedError
