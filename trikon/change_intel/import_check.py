"""Import_Checker core: find static imports that a change leaves broken.

This module is pure. It never touches git, Docker or the filesystem. Callers
hand :func:`check_trees` two path maps (base tree and head tree, each mapping a
repo-relative POSIX path to the file's raw bytes, ``None`` when unreadable) and
receive an :class:`ImportCheckResult`. Reading those trees is the job of the
I/O shell in :mod:`trikon.change_intel.import_check_io`.

Scope
-----
* **Static imports only.** ``import X`` and ``from M import n`` statements are
  checked at every nesting level: module body, function and class bodies,
  ``if TYPE_CHECKING:`` blocks, loops, ``with`` and ``match`` blocks. Dynamic
  imports (``importlib.import_module``, ``__import__``, string-based loaders)
  are expressions, not import statements, and are never reported.
* **Guarded imports are skipped.** An import inside a ``try`` body whose
  handlers include a bare ``except`` or name ``ImportError``,
  ``ModuleNotFoundError`` or ``Exception`` is an optional dependency by intent.
  Everything nested in that body inherits the guard.
* **Relative imports** are resolved against the file's package under the
  innermost Module_Root that contains the file, so ``src`` wins over the repo
  root for files under ``src/``.

Model (see the requirements glossary)
-------------------------------------
* **Module_Root**: the repo root, plus ``src`` when any path lives under it.
* **Module_Set**: per root, every ``.py`` file (``pkg/__init__.py`` names
  ``pkg``) and every ancestor directory. Names with a segment that is not an
  identifier (``.trikon``, ``my-dir``) are dropped because no static import
  can spell them. When ``pkg/__init__.py`` and ``pkg.py`` both exist, the
  package file is the one that defines ``pkg``.
* **Removed_Module**: in the base Module_Set and absent from the head one.
* **Removed_Name**: ``(M, n)`` where M is in both Module_Sets, M's base or head
  file is a changed path, and n is a Top_Level_Name of M at base but not at
  head. A tree where M has no file (a namespace directory) has no names. M is
  skipped when either file fails to parse, and when head M is an open
  namespace: a module-level ``__getattr__`` binding or a top-level star
  import means every name counts as present.

Failure handling
----------------
Per-file problems never raise. ``ast.parse`` receives the raw bytes, so PEP 263
coding cookies are honoured. A file that is unreadable or fails to parse is
recorded in ``unparsed_files``. ``incomplete`` is set when that file is the
base content of a changed path, or a head file in ``head_changed_paths``. An
unparsed head file is treated as binding every name, so it never produces a
false ``removed_module`` or ``removed_name`` report about itself.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from trikon.change_intel.models import Hunk

PathMap = Mapping[str, bytes | None]
"""Repo-relative POSIX path to raw file bytes; ``None`` means unreadable."""

BrokenKind = Literal["removed_module", "removed_name"]
"""Why an import is broken: its module is gone, or the name it imports is."""

ImportKind = Literal["import", "from"]
"""``import X`` versus ``from M import n``."""

_SRC_ROOT = "src"
_GUARD_EXCEPTIONS = frozenset({"ImportError", "ModuleNotFoundError", "Exception"})


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopLevelInfo:
    """Names bound in a module's top-level scope.

    ``open_namespace`` is true when the module binds ``__getattr__`` at top
    level (PEP 562) or holds a top-level ``from x import *``. Either makes the
    module's attribute set unknowable statically.
    """

    names: frozenset[str]
    open_namespace: bool


@dataclass(frozen=True, slots=True)
class ImportSite:
    """One static import statement found by :func:`iter_import_sites`.

    ``aliases`` holds the imported names as written, not their ``as`` names:
    the dotted module for ``import a.b``, the member names (or ``"*"``) for
    ``from M import ...``. ``module`` and ``level`` are only meaningful for
    ``kind == "from"``; plain imports carry ``None`` and ``0``.
    """

    lineno: int
    kind: ImportKind
    level: int
    module: str | None
    aliases: tuple[str, ...]
    guarded: bool


@dataclass(frozen=True, slots=True)
class BrokenImportRecord:
    """A head-tree import that refers to a Removed_Module or Removed_Name.

    ``module`` is the absolute imported module (relative imports resolved).
    ``name`` is the imported member for ``from`` imports (``"*"`` for a star
    import) and ``None`` for plain ``import X``.
    """

    path: str
    line: int
    module: str
    name: str | None
    kind: BrokenKind


@dataclass(frozen=True, slots=True)
class ImportCheckResult:
    """Outcome of :func:`check_trees`.

    ``broken`` is de-duplicated and sorted by ``(path, line, module, name or
    "", kind)``. ``unparsed_files`` is sorted and unique.
    """

    broken: tuple[BrokenImportRecord, ...]
    unparsed_files: tuple[str, ...]
    incomplete: bool
    removed_modules: frozenset[str]
    removed_names: frozenset[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class _TreeIndex:
    """Module layout of one tree: roots, Module_Set and module-to-file map."""

    roots: tuple[str, ...]
    modules: frozenset[str]
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _ScanContext:
    """Everything the head scan needs to classify one import site."""

    head: _TreeIndex
    head_infos: Mapping[str, TopLevelInfo]
    removed_modules: frozenset[str]
    removed_names: frozenset[tuple[str, str]]


_EMPTY_INFO = TopLevelInfo(names=frozenset(), open_namespace=False)
"""A namespace directory: no file, so no names."""

_UNKNOWN_INFO = TopLevelInfo(names=frozenset(), open_namespace=True)
"""An unparsed head file: assume every name is present (no false positives)."""


# ---------------------------------------------------------------------------
# Module_Set
# ---------------------------------------------------------------------------


def module_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the Module_Roots for a tree: ``("",)`` or ``("", "src")``.

    ``src`` is a root when any path lives under ``src/``. A ``src`` directory
    without Python files would contribute no modules either way.
    """
    prefix = _SRC_ROOT + "/"
    return ("", _SRC_ROOT) if any(path.startswith(prefix) for path in paths) else ("",)


def _relative_to_root(path: str, root: str) -> str | None:
    """Return ``path`` relative to ``root``, or ``None`` when outside it."""
    if not root:
        return path
    prefix = root + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _module_segments(rel: str) -> list[str] | None:
    """Dotted segments naming the ``.py`` file at root-relative ``rel``.

    ``pkg/__init__.py`` names ``pkg``; a root-level ``__init__.py`` names
    nothing. Returns ``None`` when any segment is not an identifier.
    """
    parts = rel.split("/")
    stem = parts[-1].removesuffix(".py")
    segments = parts[:-1] if stem == "__init__" else [*parts[:-1], stem]
    if not segments or not all(segment.isidentifier() for segment in segments):
        return None
    return segments


def module_names_for_path(path: str, roots: tuple[str, ...]) -> tuple[str, ...]:
    """Return the module name ``path`` defines under each root containing it.

    ``src/orders/worker.py`` with roots ``("", "src")`` yields
    ``("src.orders.worker", "orders.worker")``, in root order. Non-``.py``
    paths and paths with a non-identifier segment yield nothing for that root.
    """
    if not path.endswith(".py"):
        return ()
    names: list[str] = []
    for root in roots:
        rel = _relative_to_root(path, root)
        if rel is None:
            continue
        segments = _module_segments(rel)
        if segments is not None:
            names.append(".".join(segments))
    return tuple(names)


def _package_names(rel: str) -> Iterator[str]:
    """Yield the dotted name of each importable ancestor directory of ``rel``.

    Stops at the first non-identifier directory: nothing below it is
    importable under this root.
    """
    directories = rel.split("/")[:-1]
    for depth, segment in enumerate(directories, start=1):
        if not segment.isidentifier():
            return
        yield ".".join(directories[:depth])


def build_module_set(paths: Iterable[str]) -> frozenset[str]:
    """Return the Module_Set of a tree given its file paths.

    Only ``.py`` paths count. Each contributes its own module name and the
    names of its ancestor directories, once per Module_Root containing it.
    """
    python_paths = [path for path in paths if path.endswith(".py")]
    roots = module_roots(python_paths)
    names: set[str] = set()
    for path in python_paths:
        names.update(module_names_for_path(path, roots))
        for root in roots:
            rel = _relative_to_root(path, root)
            if rel is not None:
                names.update(_package_names(rel))
    return frozenset(names)


def _file_rank(path: str) -> tuple[int, str]:
    """Sort key choosing which file defines a module: packages first, then path."""
    is_package = path == "__init__.py" or path.endswith("/__init__.py")
    return (0 if is_package else 1, path)


def _index_tree(paths: Sequence[str]) -> _TreeIndex:
    """Build the roots, Module_Set and module-to-file map for one tree."""
    roots = module_roots(paths)
    files: dict[str, str] = {}
    for path in paths:
        for name in module_names_for_path(path, roots):
            current = files.get(name)
            if current is None or _file_rank(path) < _file_rank(current):
                files[name] = path
    return _TreeIndex(roots=roots, modules=build_module_set(paths), files=files)


# ---------------------------------------------------------------------------
# Top_Level_Name
# ---------------------------------------------------------------------------


def _target_names(target: ast.expr) -> Iterator[str]:
    """Yield the plain names an assignment target binds.

    Recurses through ``Tuple``, ``List`` and ``Starred``. Attribute and
    subscript targets bind no module-level name.
    """
    stack = [target]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Tuple | ast.List):
            stack.extend(node.elts)
        elif isinstance(node, ast.Starred):
            stack.append(node.value)


def _statement_bindings(node: ast.stmt) -> Iterator[str]:
    """Yield the names one top-level-scope statement binds."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield node.name
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _target_names(target)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        if isinstance(node.target, ast.Name):
            yield node.target.id
    elif isinstance(node, ast.Import):
        for alias in node.names:
            # ``import a.b`` binds ``a``; ``import a.b as c`` binds ``c``.
            yield alias.asname or alias.name.split(".", 1)[0]
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                yield alias.asname or alias.name


def _is_star_import(node: ast.stmt) -> bool:
    """Return whether ``node`` is ``from x import *``."""
    return isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)


def _top_level_blocks(node: ast.stmt) -> list[ast.stmt]:
    """Statements nested in ``node`` that still run in the module scope.

    Covers ``if``, ``try``/``try*`` (body, handlers, ``else``, ``finally``),
    ``with``, ``for`` and ``while``. Function and class bodies open a new
    scope and are never entered.
    """
    if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
        return [*node.body, *node.orelse]
    if isinstance(node, ast.With | ast.AsyncWith):
        return list(node.body)
    if isinstance(node, ast.Try | ast.TryStar):
        handler_bodies = [stmt for handler in node.handlers for stmt in handler.body]
        return [*node.body, *handler_bodies, *node.orelse, *node.finalbody]
    return []


def top_level_names(tree: ast.Module) -> TopLevelInfo:
    """Return the Top_Level_Names of a parsed module.

    Names come from function and class definitions, plain-name assignment
    targets (including unpacking), annotated and augmented assignments, and
    import bindings, anywhere in the module scope including nested
    ``if``/``try``/``with``/``for``/``while`` blocks.
    """
    names: set[str] = set()
    star = False
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        names.update(_statement_bindings(node))
        star = star or _is_star_import(node)
        stack.extend(_top_level_blocks(node))
    return TopLevelInfo(names=frozenset(names), open_namespace=star or "__getattr__" in names)


# ---------------------------------------------------------------------------
# Import sites
# ---------------------------------------------------------------------------


def _exception_name(node: ast.expr) -> str | None:
    """Return the bare class name of an ``except`` clause entry, if any."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _handler_guards(handler: ast.ExceptHandler) -> bool:
    """Return whether a handler makes its ``try`` body a Guarded_Import zone."""
    if handler.type is None:
        return True
    entries = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_exception_name(entry) in _GUARD_EXCEPTIONS for entry in entries)


def _body_statements(node: ast.stmt) -> list[ast.stmt]:
    """Every statement directly nested in a non-``try`` compound statement."""
    if isinstance(
        node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.With | ast.AsyncWith
    ):
        return list(node.body)
    if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
        return [*node.body, *node.orelse]
    if isinstance(node, ast.Match):
        return [stmt for case in node.cases for stmt in case.body]
    return []


def _nested_statements(node: ast.stmt, guarded: bool) -> list[tuple[ast.stmt, bool]]:
    """Statements nested in ``node``, each paired with its guard flag.

    A ``try`` body becomes guarded when any handler qualifies. Handlers,
    ``else`` and ``finally`` keep the enclosing flag.
    """
    if isinstance(node, ast.Try | ast.TryStar):
        body_guarded = guarded or any(_handler_guards(handler) for handler in node.handlers)
        nested = [(stmt, body_guarded) for stmt in node.body]
        nested += [(stmt, guarded) for handler in node.handlers for stmt in handler.body]
        nested += [(stmt, guarded) for stmt in (*node.orelse, *node.finalbody)]
        return nested
    return [(stmt, guarded) for stmt in _body_statements(node)]


def _import_site(node: ast.Import | ast.ImportFrom, guarded: bool) -> ImportSite:
    """Build the :class:`ImportSite` record for one import statement."""
    aliases = tuple(alias.name for alias in node.names)
    if isinstance(node, ast.Import):
        return ImportSite(
            lineno=node.lineno,
            kind="import",
            level=0,
            module=None,
            aliases=aliases,
            guarded=guarded,
        )
    return ImportSite(
        lineno=node.lineno,
        kind="from",
        level=node.level,
        module=node.module,
        aliases=aliases,
        guarded=guarded,
    )


def iter_import_sites(tree: ast.Module) -> Iterator[ImportSite]:
    """Yield every static import statement in ``tree``, in source order.

    Walks every statement at every depth (imports are statements, so no
    expression can hide one). Only ``ast.Import`` and ``ast.ImportFrom`` are
    yielded, which is why ``importlib`` and ``__import__`` calls never appear.
    The walk is iterative, so deeply nested code cannot exhaust the stack.
    """
    stack: list[tuple[ast.stmt, bool]] = [(stmt, False) for stmt in reversed(tree.body)]
    while stack:
        node, guarded = stack.pop()
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield _import_site(node, guarded)
        else:
            stack.extend(reversed(_nested_statements(node, guarded)))


# ---------------------------------------------------------------------------
# Relative imports
# ---------------------------------------------------------------------------


def _innermost_root(path: str, roots: tuple[str, ...]) -> str | None:
    """Return the longest root that contains ``path``."""
    containing = [root for root in roots if _relative_to_root(path, root) is not None]
    return max(containing, key=len) if containing else None


def resolve_relative(
    path: str, level: int, module: str | None, roots: tuple[str, ...]
) -> str | None:
    """Resolve ``from <level dots><module> import ...`` in ``path`` to a dotted name.

    The package is the directory of ``path`` under its innermost root, which is
    right for both ``pkg/__init__.py`` and ``pkg/mod.py``. Returns ``None``
    when the file has no package, when ``level - 1`` exceeds the package
    depth, or when the result is empty or has a non-identifier segment.
    ``level < 1`` is an absolute import and returns ``module`` unchanged.
    """
    if level < 1:
        return module or None
    root = _innermost_root(path, roots)
    rel = None if root is None else _relative_to_root(path, root)
    if rel is None:
        return None
    package = rel.split("/")[:-1]
    if not package or level - 1 > len(package):
        return None
    parts = package[: len(package) - (level - 1)] + (module.split(".") if module else [])
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Base reconstruction
# ---------------------------------------------------------------------------


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping terminators, the way diff tools count lines.

    ``str.splitlines`` is avoided on purpose: it also splits on form feeds and
    other separators, which would misalign line numbers with the diff.
    """
    pieces = text.split("\n")
    tail = pieces.pop()
    lines = [piece + "\n" for piece in pieces]
    if tail:
        lines.append(tail)
    return lines


def _hunk_order(hunk: Hunk) -> tuple[int, int]:
    """Sort key for processing hunks from the bottom of the file up."""
    return (hunk.new_start, hunk.old_start)


def _hunk_matches(hunk: Hunk, lines: Sequence[str], start: int, end: int, limit: int) -> bool:
    """Return whether ``hunk`` describes ``lines[start:end]`` of the head file.

    ``limit`` is where the previously spliced (lower-down) hunk began, so
    overlapping or out-of-range hunks are rejected.
    """
    if start < 0 or end > limit:
        return False
    if len(hunk.source_lines) != hunk.old_lines or len(hunk.target_lines) != hunk.new_lines:
        return False
    return all(
        head.rstrip("\r\n") == target.rstrip("\r\n")
        for head, target in zip(lines[start:end], hunk.target_lines, strict=True)
    )


def reverse_hunks(head_text: str, hunks: Sequence[Hunk]) -> str | None:
    """Rebuild a file's base text by undoing ``hunks`` on its head text.

    Hunks are applied bottom-up (descending ``new_start``). Each head slice
    must equal the hunk's ``target_lines`` (compared without terminators)
    before it is replaced by ``source_lines``. Returns ``None`` when the diff
    does not match the head text or the hunk carries no line text, which
    counts as unobtainable base content (Req 3.7).

    Restored lines take the head's newline style. Unified diffs record a
    missing final newline only in ``\\ No newline`` markers, which ``Hunk``
    does not keep, so a restored last line follows the head file's ending.
    """
    lines = _split_lines(head_text)
    head_len = len(lines)
    newline = "\r\n" if "\r\n" in head_text else "\n"
    head_lacks_final_newline = head_len > 0 and not lines[-1].endswith("\n")
    limit = head_len
    for position, hunk in enumerate(sorted(hunks, key=_hunk_order, reverse=True)):
        start = hunk.new_start if hunk.new_lines == 0 else hunk.new_start - 1
        end = start + hunk.new_lines
        if not _hunk_matches(hunk, lines, start, end, limit):
            return None
        replacement = [text + newline for text in hunk.source_lines]
        if position == 0 and end == head_len and head_lacks_final_newline and replacement:
            replacement[-1] = hunk.source_lines[-1]
        lines[start:end] = replacement
        limit = start
    return "".join(lines)


# ---------------------------------------------------------------------------
# check_trees
# ---------------------------------------------------------------------------


def _parse(source: bytes | None, path: str) -> ast.Module | None:
    """Parse raw bytes, returning ``None`` for unreadable or unparseable files.

    ``ValueError`` covers null bytes on Python 3.11. ``RecursionError`` and
    ``MemoryError`` are how CPython's parser reports pathologically nested
    input; a hostile file must be recorded, never allowed to crash the check.
    """
    if source is None:
        return None
    try:
        return ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Deliberately recorded, not raised: per-file failures feed
        # ``unparsed_files`` / ``incomplete`` (Req 3.7, 4.10).
        return None


def _parse_paths(
    contents: PathMap, paths: Iterable[str]
) -> tuple[dict[str, ast.Module], frozenset[str]]:
    """Parse ``paths`` from ``contents``; return the trees and the failed paths."""
    trees: dict[str, ast.Module] = {}
    failed: set[str] = set()
    for path in paths:
        tree = _parse(contents.get(path), path)
        if tree is None:
            failed.add(path)
        else:
            trees[path] = tree
    return trees, frozenset(failed)


def _python_paths(contents: PathMap) -> list[str]:
    """Sorted Python_File paths of a tree."""
    return sorted(path for path in contents if path.endswith(".py"))


def _module_info(index: _TreeIndex, infos: Mapping[str, TopLevelInfo], module: str) -> TopLevelInfo:
    """Top-level names of ``module`` in a tree whose parsed files are ``infos``.

    No defining file (a namespace directory) gives no names. A defining file
    missing from ``infos`` failed to parse and is treated as binding every name.
    """
    path = index.files.get(module)
    if path is None:
        return _EMPTY_INFO
    return infos.get(path, _UNKNOWN_INFO)


def _base_info(
    base: PathMap, base_trees: Mapping[str, ast.Module], path: str | None
) -> TopLevelInfo | None:
    """Top-level names of a base-tree module file; ``None`` when it will not parse."""
    if path is None:
        return _EMPTY_INFO
    tree = base_trees.get(path)
    if tree is None:
        # Unchanged base file defining a module whose head file changed.
        tree = _parse(base.get(path), path)
    return None if tree is None else top_level_names(tree)


def _removed_names(
    base: PathMap,
    base_index: _TreeIndex,
    base_trees: Mapping[str, ast.Module],
    head_index: _TreeIndex,
    head_infos: Mapping[str, TopLevelInfo],
    changed_paths: frozenset[str],
) -> frozenset[tuple[str, str]]:
    """Compute the Removed_Name set (Req 3.5, 3.6)."""
    removed: set[tuple[str, str]] = set()
    for module in base_index.modules & head_index.modules:
        base_file = base_index.files.get(module)
        head_file = head_index.files.get(module)
        if base_file not in changed_paths and head_file not in changed_paths:
            continue
        head_info = _module_info(head_index, head_infos, module)
        if head_info.open_namespace:
            continue
        base_info = _base_info(base, base_trees, base_file)
        if base_info is not None:
            removed.update((module, name) for name in base_info.names - head_info.names)
    return frozenset(removed)


def _has_removed_prefix(name: str, removed_modules: frozenset[str]) -> bool:
    """Return whether ``name`` or one of its dotted prefixes is a Removed_Module."""
    parts = name.split(".")
    return any(".".join(parts[:depth]) in removed_modules for depth in range(1, len(parts) + 1))


def _check_plain_import(
    path: str, site: ImportSite, ctx: _ScanContext
) -> Iterator[BrokenImportRecord]:
    """Classify ``import X`` aliases (Req 4.2)."""
    for name in site.aliases:
        if _has_removed_prefix(name, ctx.removed_modules):
            yield BrokenImportRecord(
                path=path, line=site.lineno, module=name, name=None, kind="removed_module"
            )


def _check_from_import(
    path: str, site: ImportSite, ctx: _ScanContext
) -> Iterator[BrokenImportRecord]:
    """Classify ``from M import n`` aliases (Req 4.3 to 4.6)."""
    module = resolve_relative(path, site.level, site.module, ctx.head.roots)
    if module is None:
        return
    if _has_removed_prefix(module, ctx.removed_modules):
        for alias in site.aliases:
            yield BrokenImportRecord(
                path=path, line=site.lineno, module=module, name=alias, kind="removed_module"
            )
        return
    info = _module_info(ctx.head, ctx.head_infos, module)
    for alias in site.aliases:
        if alias == "*":
            continue
        submodule = f"{module}.{alias}"
        binds_alias = info.open_namespace or alias in info.names
        if submodule in ctx.removed_modules and not binds_alias:
            yield BrokenImportRecord(
                path=path, line=site.lineno, module=module, name=alias, kind="removed_module"
            )
        elif (module, alias) in ctx.removed_names and submodule not in ctx.head.modules:
            yield BrokenImportRecord(
                path=path, line=site.lineno, module=module, name=alias, kind="removed_name"
            )


def _scan_file(path: str, tree: ast.Module, ctx: _ScanContext) -> Iterator[BrokenImportRecord]:
    """Yield the Broken_Imports of one head file, skipping guarded sites."""
    for site in iter_import_sites(tree):
        if site.guarded:
            continue
        if site.kind == "import":
            yield from _check_plain_import(path, site, ctx)
        else:
            yield from _check_from_import(path, site, ctx)


def _record_key(record: BrokenImportRecord) -> tuple[str, int, str, str, str]:
    """Deterministic sort key for :class:`BrokenImportRecord`."""
    return (record.path, record.line, record.module, record.name or "", record.kind)


def check_trees(
    base: PathMap,
    head: PathMap,
    *,
    changed_paths: frozenset[str],
    head_changed_paths: frozenset[str],
) -> ImportCheckResult:
    """Find the Broken_Imports a change leaves in the head tree.

    ``base`` and ``head`` map repo-relative POSIX paths to raw bytes. Only
    ``.py`` keys are considered. ``changed_paths`` holds every path the change
    touches (including deleted files and rename sources); ``head_changed_paths``
    holds the added, modified and rename-target paths.

    Every head Python_File is parsed once (Req 4.1, 4.10). Base content is
    parsed for changed files only, plus any unchanged base file that defines a
    module whose head file changed.
    """
    base_index = _index_tree(_python_paths(base))
    head_paths = _python_paths(head)
    head_index = _index_tree(head_paths)
    removed_modules = base_index.modules - head_index.modules

    head_trees, head_failed = _parse_paths(head, head_paths)
    changed_base = [path for path in _python_paths(base) if path in changed_paths]
    base_trees, base_failed = _parse_paths(base, changed_base)
    incomplete = bool(base_failed) or not head_failed.isdisjoint(head_changed_paths)

    head_infos = {path: top_level_names(tree) for path, tree in head_trees.items()}
    removed_names = _removed_names(
        base, base_index, base_trees, head_index, head_infos, changed_paths
    )

    broken: set[BrokenImportRecord] = set()
    if removed_modules or removed_names:
        ctx = _ScanContext(
            head=head_index,
            head_infos=head_infos,
            removed_modules=removed_modules,
            removed_names=removed_names,
        )
        for path, tree in head_trees.items():
            broken.update(_scan_file(path, tree, ctx))

    return ImportCheckResult(
        broken=tuple(sorted(broken, key=_record_key)),
        unparsed_files=tuple(sorted(head_failed | base_failed)),
        incomplete=incomplete,
        removed_modules=removed_modules,
        removed_names=removed_names,
    )


__all__ = [
    "BrokenImportRecord",
    "BrokenKind",
    "ImportCheckResult",
    "ImportKind",
    "ImportSite",
    "PathMap",
    "TopLevelInfo",
    "build_module_set",
    "check_trees",
    "iter_import_sites",
    "module_names_for_path",
    "module_roots",
    "resolve_relative",
    "reverse_hunks",
    "top_level_names",
]
