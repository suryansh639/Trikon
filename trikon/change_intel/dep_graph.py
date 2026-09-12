"""Build and query the symbol dependency graph.

The graph is a directed graph:
    nodes = SymbolDef (functions, classes, methods, module-level assignments)
    edges = "uses"  (source symbol references target symbol)

Persisted in SQLite for incremental updates. The graph is not held entirely
in memory — traversals are query-driven.

Schema:
    symbols(id, qualified_name, kind, file_path, file_sha, start_line, end_line, is_public)
    edges(source_id, target_id, kind)
    file_index(path, sha, indexed_at)
"""

from __future__ import annotations

from pathlib import Path

from trikon.change_intel.ast_indexer import SymbolDef


class DepGraph:
    """SQLite-backed symbol dependency graph."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        # TODO: open connection, initialize schema if missing.

    def upsert_symbols(self, file_path: Path, symbols: list[SymbolDef]) -> None:
        """Insert or replace all symbols for a given file."""
        raise NotImplementedError

    def upsert_edges(self, source: SymbolDef, targets: list[SymbolDef]) -> None:
        """Record that `source` uses each of `targets`."""
        raise NotImplementedError

    def transitive_dependents(
        self,
        symbols: list[SymbolDef],
        max_hops: int = 5,
    ) -> list[SymbolDef]:
        """Return all symbols that transitively depend on any of `symbols`."""
        raise NotImplementedError

    def file_needs_reindex(self, file_path: Path, current_sha: str) -> bool:
        """True if the file has changed since the last index."""
        raise NotImplementedError
