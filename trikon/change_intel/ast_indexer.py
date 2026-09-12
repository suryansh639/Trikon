"""AST indexer — extracts a symbol table from Python files.

For each file, produces a list of `SymbolDef` entries with byte-offset ranges,
kinds (function / class / method / module-level assignment), and qualified names.

Uses `libcst` for the concrete syntax pass (round-trippable, formatting-preserving)
because we want the option to generate patches later. For raw indexing speed we
fall back to the stdlib `ast` module — see :func:`fast_index_symbols`.

Indexing results are cached in a SQLite database keyed by file SHA-256, so
unchanged files are never re-parsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolDef:
    """A single defined symbol within a Python module."""

    qualified_name: str  # "package.module.ClassName.method_name"
    kind: str  # "function" | "class" | "method" | "assignment"
    file_path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    is_public: bool  # heuristic: not underscore-prefixed


def index_file(file_path: Path) -> list[SymbolDef]:
    """Index a single Python file and return its top-level symbol definitions."""
    # TODO: implement with libcst.
    raise NotImplementedError


def index_files(file_paths: list[Path], cache_db: Path | None = None) -> dict[Path, list[SymbolDef]]:
    """Index many files, using the SQLite cache to skip unchanged ones.

    Returns a mapping from file path to its list of definitions.
    """
    # TODO: hash each file; look up in cache; index only cache misses; write back.
    raise NotImplementedError


def fast_index_symbols(source: str) -> list[SymbolDef]:
    """Fast, formatting-agnostic symbol pass using the stdlib `ast` module.

    Used when we do not need round-trippable output (i.e., every read path
    except patch generation).
    """
    # TODO: implement with ast.parse + NodeVisitor.
    raise NotImplementedError
