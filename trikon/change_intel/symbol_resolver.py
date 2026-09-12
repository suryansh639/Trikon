"""Resolve cross-file symbol references.

Given a symbol def, find every call site and import that refers to it.
Uses `jedi` for its virtualenv-aware, stub-aware resolver.

Resolution is expensive on large repos, so results are cached by (file SHA,
symbol qualified name) in the same SQLite database as the AST indexer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trikon.change_intel.ast_indexer import SymbolDef


@dataclass(frozen=True)
class SymbolRef:
    """A reference (call site or import) to a defined symbol."""

    target: SymbolDef
    referring_file: str
    referring_line: int
    kind: str  # "call" | "import" | "attribute_access"


def find_references(
    symbol: SymbolDef,
    repo_path: Path,
    cache_db: Path | None = None,
) -> list[SymbolRef]:
    """Return every reference to `symbol` in the repository."""
    # TODO: implement with jedi.Project + jedi.Script.get_references().
    raise NotImplementedError
