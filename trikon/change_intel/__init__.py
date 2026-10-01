"""Change Intelligence — convert a git diff into a structured impact set.

Pipeline:
    diff_parser.parse_diff()      → ChangeSet
    ast_indexer.index_files()     → per-file symbol table (cached in SQLite)
    symbol_resolver.resolve()     → cross-file references
    dep_graph.build()             → directed graph of "uses" edges
    blast_radius.compute_impact() → ImpactSet (impacted modules, symbols, tests, score)

The dep graph is incremental: files are re-indexed only when their SHA-256 changes.
This is the same design as the OSS ``impact-radius`` package.
"""

from trikon.change_intel.blast_radius import (
    BlastWeights,
    bucket,
    compute_impact,
    enclosing_symbols,
)
from trikon.change_intel.dep_graph import CURRENT_SCHEMA_VERSION, DepGraph
from trikon.change_intel.diff_parser import parse_diff
from trikon.change_intel.errors import (
    AstParseError,
    BlastRadiusError,
    ChangeIntelError,
    DepGraphError,
    DiffInputError,
    DiffParseError,
    ImportCheckError,
    RepoNotFoundError,
    SymbolResolutionError,
)
from trikon.change_intel.models import (
    ChangeKind,
    ChangeSet,
    FileChange,
    Hunk,
    RefKind,
    SymbolDef,
    SymbolKind,
    SymbolRefInternal,
    to_public_symbol_ref,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "AstParseError",
    "BlastRadiusError",
    "BlastWeights",
    "ChangeIntelError",
    "ChangeKind",
    "ChangeSet",
    "DepGraph",
    "DepGraphError",
    "DiffInputError",
    "DiffParseError",
    "FileChange",
    "Hunk",
    "ImportCheckError",
    "RefKind",
    "RepoNotFoundError",
    "SymbolDef",
    "SymbolKind",
    "SymbolRefInternal",
    "SymbolResolutionError",
    "bucket",
    "compute_impact",
    "enclosing_symbols",
    "parse_diff",
    "to_public_symbol_ref",
]
