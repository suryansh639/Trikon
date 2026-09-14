"""AST indexer — extract symbol tables from Python source.

This module is the read-only hot path of Change Intelligence. For every changed
file :func:`compute_impact` needs to know which functions, classes, methods, and
module-level assignments live at which byte ranges, so it can map diff hunks to
enclosing symbols. Getting a formatting-preserving concrete syntax tree from
``libcst`` is roughly an order of magnitude more expensive than parsing with
the standard library, and the resulting shape is only needed if we plan to
regenerate source — which Phase 1 does not.

:func:`fast_index_symbols` is the perf-critical entry point built on stdlib
``ast``. It emits :class:`~trikon.change_intel.models.SymbolDef` values with
byte-accurate ranges into the UTF-8 encoding of the source, using a precomputed
line-start table so we never have to re-encode source per node.

:func:`index_file` (Task 5.1) is the formatting-preserving-parser gate:
it computes ``file_sha``, infers the module path from ``__init__.py``
ancestors, runs the source through :func:`libcst.parse_module` (so only
libcst-clean files enter the cache), and then delegates the actual
symbol enumeration back to :func:`fast_index_symbols`. Its SQLite-cached
neighbor :func:`index_files` (Task 5.2) is still a stub.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import libcst

from trikon.change_intel.dep_graph import DepGraph
from trikon.change_intel.errors import AstParseError
from trikon.change_intel.models import SymbolDef, SymbolKind

__all__ = [
    "SymbolDef",
    "fast_index_symbols",
    "index_file",
    "index_files",
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fast_index_symbols(
    source: str,
    *,
    module_path: str = "",
    file_path: str = "",
    file_sha: str = "",
) -> list[SymbolDef]:
    """Extract symbol definitions from a Python source string.

    Captures four symbol kinds, matching :data:`SymbolKind`:

    * ``function`` — top-level ``def`` / ``async def``.
    * ``class`` — a class definition, at module level or nested inside another
      class. Class bodies are traversed for their own methods and inner
      classes; function bodies are not.
    * ``method`` — ``def`` / ``async def`` inside a class body at any class
      nesting depth.
    * ``assignment`` — module-level ``NAME = ...`` or ``NAME: T = ...`` where
      the left-hand side is a single :class:`ast.Name`. Multi-target
      assignments (``a = b = 1``), tuple unpacking (``x, y = ...``), and
      attribute assignments (``self.x = ...``) are excluded.

    Parameters
    ----------
    source:
        The full text of the Python module. Encoded to UTF-8 internally.
    module_path:
        Dotted package path used to build each symbol's ``qualified_name``
        (for example ``"payments.gateway"``). Empty string produces
        unqualified names; callers who don't care can leave it blank.
    file_path:
        POSIX-relative path recorded on every returned :class:`SymbolDef`.
        Also used as the filename in ``SyntaxError`` diagnostics.
    file_sha:
        Hex SHA-256 of the source bytes at index time. Threaded verbatim into
        every returned :class:`SymbolDef`; not recomputed here so callers stay
        in charge of the caching boundary.

    Returns
    -------
    list[SymbolDef]
        Symbols in source order. Byte offsets are half-open indices into the
        UTF-8 encoding of ``source``; line indices are 1-based inclusive.

    Raises
    ------
    AstParseError
        The source did not parse. The exception carries ``file_path`` and,
        when the underlying :class:`SyntaxError` provides one, ``line``.
    """
    try:
        tree = ast.parse(source, filename=file_path or "<string>")
    except SyntaxError as exc:
        raise AstParseError(
            str(exc),
            file_path=file_path,
            line=exc.lineno,
            cause=exc,
        ) from exc

    line_starts = _compute_line_starts(source.encode("utf-8"))

    symbols: list[SymbolDef] = []
    for node in tree.body:
        _visit_module_level(
            node,
            symbols,
            module_path=module_path,
            file_path=file_path,
            file_sha=file_sha,
            line_starts=line_starts,
        )
    return symbols


# ---------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------


def _visit_module_level(
    node: ast.stmt,
    symbols: list[SymbolDef],
    *,
    module_path: str,
    file_path: str,
    file_sha: str,
    line_starts: list[int],
) -> None:
    """Emit symbols for a single module-body statement.

    Top-level ``def`` / ``async def`` become ``function``; top-level ``class``
    becomes ``class`` and its body is walked with :func:`_visit_class_body`
    (which captures nested classes and their methods). Function bodies are
    intentionally not traversed — nested functions and lambdas are excluded
    per ``design.md §2.2``.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        symbols.append(
            _make_symbol(
                node,
                "function",
                [node.name],
                module_path=module_path,
                file_path=file_path,
                file_sha=file_sha,
                line_starts=line_starts,
            )
        )
        return

    if isinstance(node, ast.ClassDef):
        symbols.append(
            _make_symbol(
                node,
                "class",
                [node.name],
                module_path=module_path,
                file_path=file_path,
                file_sha=file_sha,
                line_starts=line_starts,
            )
        )
        _visit_class_body(
            node,
            [node.name],
            symbols,
            module_path=module_path,
            file_path=file_path,
            file_sha=file_sha,
            line_starts=line_starts,
        )
        return

    name = _module_level_assignment_target(node)
    if name is not None:
        symbols.append(
            _make_symbol(
                node,
                "assignment",
                [name],
                module_path=module_path,
                file_path=file_path,
                file_sha=file_sha,
                line_starts=line_starts,
            )
        )


def _visit_class_body(
    class_node: ast.ClassDef,
    path: list[str],
    symbols: list[SymbolDef],
    *,
    module_path: str,
    file_path: str,
    file_sha: str,
    line_starts: list[int],
) -> None:
    """Emit methods and nested-class symbols from a class body.

    ``path`` is the class-nesting prefix (``["Outer", "Inner"]``) that will
    be joined with the member name to build the ``qualified_name``. Methods
    are captured at any nesting depth of class-in-class; nested classes are
    themselves emitted as ``class`` symbols so downstream consumers can
    resolve the full dotted path.
    """
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append(
                _make_symbol(
                    node,
                    "method",
                    [*path, node.name],
                    module_path=module_path,
                    file_path=file_path,
                    file_sha=file_sha,
                    line_starts=line_starts,
                )
            )
        elif isinstance(node, ast.ClassDef):
            new_path = [*path, node.name]
            symbols.append(
                _make_symbol(
                    node,
                    "class",
                    new_path,
                    module_path=module_path,
                    file_path=file_path,
                    file_sha=file_sha,
                    line_starts=line_starts,
                )
            )
            _visit_class_body(
                node,
                new_path,
                symbols,
                module_path=module_path,
                file_path=file_path,
                file_sha=file_sha,
                line_starts=line_starts,
            )


def _module_level_assignment_target(node: ast.stmt) -> str | None:
    """Return the single :class:`ast.Name` target of an assignment, or ``None``.

    Filters out multi-target assignments (``a = b = 1``), tuple/list
    unpacking (``x, y = ...``), and attribute or subscript assignments
    (``self.x = ...``, ``d["k"] = ...``). Returns the identifier for both
    ``ast.Assign`` (single ``Name`` in ``targets``) and ``ast.AnnAssign``
    (``target`` is a ``Name``) statements; every other node returns
    ``None``.
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        return None

    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            return node.target.id
        return None

    return None


# ---------------------------------------------------------------------------
# Symbol construction
# ---------------------------------------------------------------------------


def _make_symbol(
    node: ast.stmt,
    kind: SymbolKind,
    name_path: list[str],
    *,
    module_path: str,
    file_path: str,
    file_sha: str,
    line_starts: list[int],
) -> SymbolDef:
    """Build a :class:`SymbolDef` from an AST node and its class-nesting path."""
    qualified_name = _qualified_name(module_path, name_path)
    start_line, end_line, start_byte, end_byte = _byte_range(node, line_starts)
    return SymbolDef(
        qualified_name=qualified_name,
        kind=kind,
        file_path=file_path,
        file_sha=file_sha,
        start_line=start_line,
        end_line=end_line,
        start_byte=start_byte,
        end_byte=end_byte,
        is_public=_is_qualified_name_public(qualified_name),
    )


def _qualified_name(module_path: str, name_path: list[str]) -> str:
    """Join a module path and a name path into a dotted qualified name."""
    if module_path:
        return ".".join([module_path, *name_path])
    return ".".join(name_path)


def _byte_range(
    node: ast.stmt,
    line_starts: list[int],
) -> tuple[int, int, int, int]:
    """Return ``(start_line, end_line, start_byte, end_byte)`` for a statement.

    Line indices are the values reported directly by ``ast``: 1-based and
    inclusive. Byte offsets are half-open into the UTF-8 encoding of the
    source and derived from the precomputed line-start table. ``col_offset``
    and ``end_col_offset`` are already byte offsets in CPython 3.8+, so they
    can be added to the line-start position without any decoding work.

    If ``end_lineno`` / ``end_col_offset`` are absent (should not happen on
    the statement types we visit under Python 3.11, but the ``ast`` type
    stubs allow ``None``), we fall back to the start position so the range
    is at least well-formed rather than raising.
    """
    start_line = node.lineno
    start_col = node.col_offset
    end_line = node.end_lineno if node.end_lineno is not None else start_line
    end_col = node.end_col_offset if node.end_col_offset is not None else start_col
    start_byte = line_starts[start_line - 1] + start_col
    end_byte = line_starts[end_line - 1] + end_col
    return start_line, end_line, start_byte, end_byte


def _compute_line_starts(source_bytes: bytes) -> list[int]:
    """Return the byte offset at which each source line begins.

    ``line_starts[i]`` is the byte index of the first byte of line ``i + 1``.
    The result always begins with ``0`` (line 1 starts at byte 0) and grows
    by one entry per line terminator. All three line terminators recognized
    by Python's tokenizer are handled: LF (``\\n``), CRLF (``\\r\\n``), and
    lone CR (``\\r``). CRLF is treated as a single terminator so subsequent
    ``col_offset`` values line up with the same encoding CPython sees after
    universal-newline normalization.
    """
    line_starts: list[int] = [0]
    length = len(source_bytes)
    index = 0
    while index < length:
        byte = source_bytes[index]
        if byte == 0x0D:  # CR
            if index + 1 < length and source_bytes[index + 1] == 0x0A:
                index += 2
            else:
                index += 1
            line_starts.append(index)
        elif byte == 0x0A:  # LF
            index += 1
            line_starts.append(index)
        else:
            index += 1
    return line_starts


# ---------------------------------------------------------------------------
# Visibility heuristic
# ---------------------------------------------------------------------------


def _is_qualified_name_public(qualified_name: str) -> bool:
    """Return whether every component of a dotted qualified name is public.

    A component is public when it does not begin with ``_``, or when it is a
    dunder identifier (starts and ends with ``__``, length ≥ 4) — which
    captures ``__init__``, ``__all__``, and the other special names that are
    part of a class's or module's public surface despite the underscores.

    Any single private component makes the whole name private:
    ``pkg._helper.foo`` is private even though ``pkg`` and ``foo`` on their
    own would be public.
    """
    return all(_component_is_public(part) for part in qualified_name.split("."))


def _component_is_public(component: str) -> bool:
    """Return whether one dot-separated component of a qualified name is public."""
    if not component.startswith("_"):
        return True
    return len(component) >= 4 and component.startswith("__") and component.endswith("__")


# ---------------------------------------------------------------------------
# Cached / formatting-preserving entry point — Task 5.1
# ---------------------------------------------------------------------------


def index_file(file_path: Path) -> list[SymbolDef]:
    """Index a single Python file, honoring the libcst parse gate.

    Reads ``file_path``'s bytes, computes the file's SHA-256, infers the
    dotted module path by walking up ``__init__.py`` anchors, and delegates
    the actual symbol enumeration to :func:`fast_index_symbols` after
    round-tripping the source through :func:`libcst.parse_module`. The
    libcst pass is a stricter parser than stdlib ``ast``: gating cache
    admissions on it keeps a formatting-preserving CST reachable for the
    eventual patch-generation path (design.md §2.2) without paying to walk
    it here.

    Parameters
    ----------
    file_path:
        Absolute or repo-relative path to a ``.py`` / ``.pyi`` file. Must
        exist and be readable — :meth:`Path.read_bytes` propagates the
        underlying :class:`OSError` unchanged. Filesystem failures are not
        parse failures, so they intentionally do not surface as
        :class:`AstParseError`.

    Returns
    -------
    list[SymbolDef]
        Same shape and source order as :func:`fast_index_symbols`. Each
        symbol carries ``file_path`` as the POSIX-normalized path, the
        file's SHA-256, and a qualified name prefixed with the inferred
        module path.

    Raises
    ------
    AstParseError
        The file is not valid Python (libcst's :class:`ParserSyntaxError`
        is the trigger). The exception carries ``file_path`` and, when
        libcst provides one, the offending line number.
    """
    file_bytes = file_path.read_bytes()
    source = file_bytes.decode("utf-8")
    file_sha = hashlib.sha256(file_bytes).hexdigest()
    module_path = _infer_module_path(file_path)

    try:
        libcst.parse_module(source)
    except libcst.ParserSyntaxError as exc:
        raise AstParseError(
            str(exc),
            file_path=str(file_path),
            line=getattr(exc, "raw_line", None),
            cause=None,
        ) from exc

    return fast_index_symbols(
        source,
        module_path=module_path,
        file_path=file_path.as_posix(),
        file_sha=file_sha,
    )


def _infer_module_path(file_path: Path) -> str:
    """Derive a dotted module path from a filesystem path.

    Walks upward from ``file_path``'s parent, accumulating directory names
    as long as each parent contains an ``__init__.py``. The first ancestor
    without one terminates the walk — that ancestor is the module's source
    root (typically ``src/`` in this project, but the algorithm makes no
    such assumption). The file's stem is always the last component.

    Examples
    --------
    * ``src/payments/gateway.py`` where ``src/payments/__init__.py`` exists
      but ``src/__init__.py`` does not → ``"payments.gateway"``.
    * ``foo/bar/baz.py`` with no ``__init__.py`` at any ancestor → ``"baz"``.
    * ``pkg/sub/mod.py`` with ``__init__.py`` at both ``pkg/`` and
      ``pkg/sub/`` → ``"pkg.sub.mod"``.

    The walk stops at the filesystem root, so a pathological input like
    ``/foo.py`` returns ``"foo"`` without touching ``/__init__.py``.
    """
    parts: list[str] = [file_path.stem]
    current = file_path.parent
    while current.parent != current:
        if (current / "__init__.py").exists():
            parts.insert(0, current.name)
            current = current.parent
        else:
            break
    return ".".join(parts)


# ---------------------------------------------------------------------------
# SQLite-cached bulk entry point — Task 5.2
# ---------------------------------------------------------------------------


def index_files(
    file_paths: list[Path],
    *,
    cache_db: Path,
) -> dict[Path, list[SymbolDef]]:
    """Index many Python files, hitting the SQLite cache for unchanged snapshots.

    For every path, in the order supplied, the routine:

    1. Reads the file's bytes and computes ``sha256``.
    2. Consults :meth:`DepGraph.file_needs_reindex`. On a cache hit, symbols
       are hydrated straight from the ``symbols`` table via
       :meth:`DepGraph.get_symbols` — no parser is invoked.
    3. On a cache miss, delegates to :func:`index_file` (which re-reads the
       file and runs the libcst gate) and stores the freshly-extracted
       symbols via :meth:`DepGraph.upsert_symbols`. ``upsert_symbols`` runs
       its own atomic three-step transaction so the cache never observes a
       partial update.

    A single :class:`~trikon.change_intel.dep_graph.DepGraph` instance is
    reused across the whole call: opening SQLite once per invocation, rather
    than once per file, keeps the warm-index target from ``design.md §7``
    (≤ 500 ms for a 5-file re-index) reachable.

    Parameters
    ----------
    file_paths:
        Paths to ``.py`` / ``.pyi`` sources. Duplicates are permitted —
        subsequent entries overwrite earlier ones in the returned dict, which
        matches Python's insertion-order semantics.
    cache_db:
        Location of the SQLite state store (typically ``<repo>/.trikon/state.db``).
        The database is created on first use with the DDL from ``design.md §4``.

    Returns
    -------
    dict[Path, list[SymbolDef]]
        Symbols keyed by the exact ``Path`` object the caller supplied. Iteration
        order matches ``file_paths`` — Python 3.7+ dicts preserve insertion order.

    Raises
    ------
    AstParseError
        Propagated verbatim from :func:`index_file` when a file fails to parse.
        Parse failures deliberately abort the whole batch; the caller decides
        whether to retry with a filtered path list.
    DepGraphError
        Propagated verbatim from :class:`DepGraph` when the SQLite backend
        rejects a query. Filesystem errors on :meth:`Path.read_bytes` propagate
        as :class:`OSError` unchanged — those are not Change Intelligence
        failures.
    """
    result: dict[Path, list[SymbolDef]] = {}
    with DepGraph(cache_db) as graph:
        for path in file_paths:
            file_bytes = path.read_bytes()
            file_sha = hashlib.sha256(file_bytes).hexdigest()

            if graph.file_needs_reindex(path, file_sha):
                symbols = index_file(path)
                graph.upsert_symbols(path, file_sha, symbols)
            else:
                symbols = graph.get_symbols(path, file_sha)

            result[path] = symbols
    return result
