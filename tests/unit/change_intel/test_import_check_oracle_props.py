# Feature: trikon-engine-fail-safe, Property 1: Import_Checker matches an independent oracle
"""Property test: ``check_trees`` agrees with an oracle built from the tree model.

*For any* generated module tree and change, :func:`check_trees` over the
rendered base and head sources returns exactly the ``broken``,
``unparsed_files`` and ``incomplete`` values that an oracle computes from the
abstract model. ``removed_modules`` is compared exactly as well, and
``removed_names`` on the names the model tracks.

The model
---------
* A tree is a set of files placed on fixed **slots** (``p/__init__.py``,
  ``p.py``, ``p/q/m.py``, ``tests/helpers.py``, ``tool-scripts/run.py``, ...)
  in a flat or a ``src`` layout. Each slot carries its module name, its
  importable ancestor directories and its package parts as hand-written data,
  so the oracle never derives a module name from a path string.
* A file holds name definitions (``def``, ``class``, assignments, unpacking,
  ``import json as n`` ...) nested in a context (module level, ``if``,
  ``for``/``while``/``else``, ``with``, every ``try`` clause, or a decoy
  ``def``/``class`` body), import statements (plain or ``from``, absolute or
  relative at level 1 to 3, star imports, ``as`` names) placed in a context
  that is guarded or not and binds a top-level name or not, and dynamic-import
  decoys (``__import__``, ``importlib.import_module``, strings, comments).
  Some files carry a PEP 263 ``latin-1`` cookie and a non-UTF-8 byte.
* A change keeps, modifies (drops some definitions), deletes or renames each
  base file, swapping ``X.py`` and ``X/__init__.py`` when it can, and may add a
  file. Either side of a file may be corrupted (syntax error, null byte, or
  unreadable), and an unchanged file may be corrupted on both sides. Import
  statements are the same on both sides.
* Random imports seldom line up with what a change removes, so most files also
  get one *probe* import aimed at a real definition or submodule of another
  base file, and package files sometimes define a submodule's name.

Relative imports follow the design §1 rule literally: the target is
``package[: len(package) - (level - 1)] + module``, ``None`` when
``level - 1 > len(package)`` or a segment is not an identifier.

The renderer turns the model into bytes with plain string formatting and
records the line of every import. The oracle works from the model alone and
never imports :mod:`ast`. It applies the requirement and design rules: the
Module_Set and Removed_Module (Req 3.4); Removed_Name with the open-namespace,
namespace-directory and unparsed-file rules (Req 3.5 to 3.7); the scan of
every head file at every nesting level, skipping guarded sites and ignoring
dynamic imports (Req 4.1 to 4.9); and the unparsed and incomplete bookkeeping
(Req 3.7, 4.10).

**Validates: Requirements 3.1, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5,
4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 9.2**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from hypothesis import event, given, note, settings
from hypothesis import strategies as st

from trikon.change_intel.import_check import check_trees

Layout = Literal["flat", "src"]
Corruption = Literal["syntax", "null", "unreadable"]
ItemKind = Literal["name", "import", "decoy"]
Record = tuple[str, int, str, str | None, str]
"""``(path, line, module, name, kind)`` of one Broken_Import."""

_I: Final = "    "
_NOTES_PATH: Final = "docs/notes.txt"
_NOTES_TEXT: Final = b"import p.m\nfrom p import f\n"


# ---------------------------------------------------------------------------
# Slots: every place a generated file can live
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """A candidate file location, with its module facts written out by hand.

    ``rel`` is relative to the slot's root: the code root (``src/`` in the src
    layout) when ``code`` is true, else the repo root. ``own`` is the module
    the file defines under that root (``None`` when no static import can name
    it), ``dirs`` the importable ancestor directories, and ``package`` the raw
    directory parts that relative imports resolve against.
    """

    rel: str
    code: bool
    own: str | None
    dirs: tuple[str, ...]
    package: tuple[str, ...]
    is_package: bool


_SLOTS: Final[tuple[Slot, ...]] = (
    Slot("p/__init__.py", code=True, own="p", dirs=("p",), package=("p",), is_package=True),
    Slot("p.py", code=True, own="p", dirs=(), package=(), is_package=False),
    Slot("p/m.py", code=True, own="p.m", dirs=("p",), package=("p",), is_package=False),
    Slot("p/n.py", code=True, own="p.n", dirs=("p",), package=("p",), is_package=False),
    Slot(
        "p/n/__init__.py",
        code=True,
        own="p.n",
        dirs=("p", "p.n"),
        package=("p", "n"),
        is_package=True,
    ),
    Slot("p/q.py", code=True, own="p.q", dirs=("p",), package=("p",), is_package=False),
    Slot(
        "p/q/__init__.py",
        code=True,
        own="p.q",
        dirs=("p", "p.q"),
        package=("p", "q"),
        is_package=True,
    ),
    Slot(
        "p/q/m.py",
        code=True,
        own="p.q.m",
        dirs=("p", "p.q"),
        package=("p", "q"),
        is_package=False,
    ),
    Slot("r/m.py", code=True, own="r.m", dirs=("r",), package=("r",), is_package=False),
    Slot("m.py", code=True, own="m", dirs=(), package=(), is_package=False),
    Slot(
        "tests/helpers.py",
        code=False,
        own="tests.helpers",
        dirs=("tests",),
        package=("tests",),
        is_package=False,
    ),
    Slot(
        "tests/test_m.py",
        code=False,
        own="tests.test_m",
        dirs=("tests",),
        package=("tests",),
        is_package=False,
    ),
    Slot(
        "tool-scripts/run.py",
        code=False,
        own=None,
        dirs=(),
        package=("tool-scripts",),
        is_package=False,
    ),
)
_SLOT_BY_REL: Final = {slot.rel: slot for slot in _SLOTS}
_SWAPS: Final = {
    "p.py": "p/__init__.py",
    "p/__init__.py": "p.py",
    "p/n.py": "p/n/__init__.py",
    "p/n/__init__.py": "p/n.py",
    "p/q.py": "p/q/__init__.py",
    "p/q/__init__.py": "p/q.py",
}
"""Module file and package file for the same dotted name."""


def _path(layout: Layout, slot: Slot) -> str:
    """Repo-relative path of ``slot`` in ``layout``."""
    return f"src/{slot.rel}" if layout == "src" and slot.code else slot.rel


def _defines(layout: Layout, slot: Slot) -> frozenset[str]:
    """Module names the file on ``slot`` defines (src code files have two)."""
    if slot.own is None:
        return frozenset()
    if layout == "src" and slot.code:
        return frozenset({slot.own, f"src.{slot.own}"})
    return frozenset({slot.own})


def _contributes(layout: Layout, slot: Slot) -> frozenset[str]:
    """Every Module_Set entry the file on ``slot`` contributes."""
    names = set(_defines(layout, slot)) | set(slot.dirs)
    if layout == "src" and slot.code:
        names |= {"src", *(f"src.{directory}" for directory in slot.dirs)}
    return frozenset(names)


# ---------------------------------------------------------------------------
# Contexts: where a definition or an import sits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    """Lines wrapped around a statement, and what that placement means.

    ``guarded`` marks a Guarded_Import zone. ``binds`` is true when a name
    bound there is a Top_Level_Name (module scope via ``if``/``try``/``with``/
    ``for``/``while``); function, class and ``match`` bodies bind none.
    """

    before: tuple[str, ...]
    indent: str
    after: tuple[str, ...]
    guarded: bool
    binds: bool


def _guard(handler: str) -> Context:
    """A module-level ``try`` body whose single handler is ``handler``."""
    return Context(("try:",), _I, (handler, f"{_I}pass"), guarded=True, binds=True)


_CONTEXTS: Final[dict[str, Context]] = {
    "module": Context((), "", (), guarded=False, binds=True),
    "if": Context(("if TYPE_CHECKING:",), _I, (), guarded=False, binds=True),
    "else": Context(("if TYPE_CHECKING:", f"{_I}pass", "else:"), _I, (), guarded=False, binds=True),
    "for": Context(("for _i in ():",), _I, (), guarded=False, binds=True),
    "for_else": Context(("for _i in ():", f"{_I}pass", "else:"), _I, (), guarded=False, binds=True),
    "while": Context(("while False:",), _I, (), guarded=False, binds=True),
    "while_else": Context(
        ("while False:", f"{_I}pass", "else:"), _I, (), guarded=False, binds=True
    ),
    "with": Context(("with open(__file__):",), _I, (), guarded=False, binds=True),
    "if_for": Context(("if True:", f"{_I}for _i in ():"), _I * 2, (), guarded=False, binds=True),
    "try_value": Context(
        ("try:",), _I, ("except ValueError:", f"{_I}pass"), guarded=False, binds=True
    ),
    "try_finally": Context(("try:",), _I, ("finally:", f"{_I}pass"), guarded=False, binds=True),
    "handler": Context(
        ("try:", f"{_I}pass", "except ImportError:"), _I, (), guarded=False, binds=True
    ),
    "try_else": Context(
        ("try:", f"{_I}pass", "except ImportError:", f"{_I}pass", "else:"),
        _I,
        (),
        guarded=False,
        binds=True,
    ),
    "finally": Context(
        ("try:", f"{_I}pass", "except ImportError:", f"{_I}pass", "finally:"),
        _I,
        (),
        guarded=False,
        binds=True,
    ),
    "guard_import": _guard("except ImportError:"),
    "guard_mnf": _guard("except ModuleNotFoundError:"),
    "guard_exception": _guard("except Exception:"),
    "guard_bare": _guard("except:"),
    "guard_tuple": _guard("except (ValueError, ImportError):"),
    "guard_attr": _guard("except builtins.ModuleNotFoundError:"),
    "guard_star": _guard("except* ImportError:"),
    "guard_second": Context(
        ("try:",),
        _I,
        (
            "except ValueError:",
            f"{_I}pass",
            "except ImportError as exc:",
            f"{_I}raise SystemExit from exc",
        ),
        guarded=True,
        binds=True,
    ),
    "guard_with": Context(
        ("try:", f"{_I}with open(__file__):"),
        _I * 2,
        ("except ImportError:", f"{_I}pass"),
        guarded=True,
        binds=True,
    ),
    "guard_nested_try": Context(
        ("try:", f"{_I}try:"),
        _I * 2,
        (f"{_I}except ValueError:", f"{_I * 2}pass", "except ImportError:", f"{_I}pass"),
        guarded=True,
        binds=True,
    ),
    "guard_in_def": Context(
        ("def _guarded() -> None:", f"{_I}try:"),
        _I * 2,
        (f"{_I}except ImportError:", f"{_I * 2}pass"),
        guarded=True,
        binds=False,
    ),
    "def_in_guard": Context(
        ("try:", f"{_I}def _late() -> None:"),
        _I * 2,
        ("except ImportError:", f"{_I}pass"),
        guarded=True,
        binds=False,
    ),
    "def": Context(("def _helper() -> None:",), _I, (), guarded=False, binds=False),
    "async_def": Context(("async def _ahelper() -> None:",), _I, (), guarded=False, binds=False),
    "class": Context(("class _Holder:",), _I, (), guarded=False, binds=False),
    "method": Context(
        ("class _Owner:", f"{_I}def run(self) -> None:"), _I * 2, (), guarded=False, binds=False
    ),
    "match": Context(("match 0:", f"{_I}case _:"), _I * 2, (), guarded=False, binds=False),
}
# Imports in ``match`` blocks are scanned (Req 4.7) but always carry an
# untracked ``as`` name, so whether ``match`` counts as module scope for
# Top_Level_Names never matters here. Definitions never go in ``match``.
_NAME_CONTEXTS: Final = ("module",) * 6 + tuple(name for name in _CONTEXTS if name != "match")
_IMPORT_CONTEXTS: Final = ("module",) * 8 + ("if", "def", "class") + tuple(_CONTEXTS)


def _wrap(context: str, body: list[str]) -> list[str]:
    """Place ``body`` inside ``context``."""
    ctx = _CONTEXTS[context]
    return [*ctx.before, *(ctx.indent + line for line in body), *ctx.after]


# ---------------------------------------------------------------------------
# File model
# ---------------------------------------------------------------------------

# ``m``, ``n`` and ``q`` are also submodule names, so a package can shadow them.
_NAME_POOL: Final = ("f", "g", "K", "m", "n", "q", "f", "g", "m", "n", "__getattr__")
_NAME_FORMS: Final = (
    "def",
    "async_def",
    "class",
    "assign",
    "chain",
    "annassign",
    "augassign",
    "tuple",
    "list",
    "starred",
    "nested",
    "import_as",
    "from_as",
)
_FLAT_TARGETS: Final = (
    "p",
    "p.m",
    "p.n",
    "p.q",
    "p.q.m",
    "r",
    "r.m",
    "m",
    "tests",
    "tests.helpers",
    "json",
    # Never a module: only a removed dotted prefix can make these broken.
    "p.m.x",
    "p.zz",
    "r.m.x",
    "tests.helpers.x",
)
_SRC_TARGETS: Final = (
    *_FLAT_TARGETS,
    "src",
    "src.p",
    "src.p.m",
    "src.p.n",
    "src.p.q.m",
    "src.r",
    "src.m",
    "src.p.m.x",
)
_REL_MODULES: Final[tuple[str | None, ...]] = (
    None,
    "m",
    "n",
    "q",
    "q.m",
    "helpers",
    "p",
    "p.m",
    "r.m",
)
_LEVELS: Final = (0, 0, 1, 1, 2, 3)
_ALIAS_POOL: Final = ("f", "g", "K", "m", "n", "p", "q", "f", "g")
_ASNAMES: Final[tuple[str | None, ...]] = (None, None, None, "f", "z")
_MATCH_ASNAME: Final = "_mt"


@dataclass(frozen=True)
class NameDef:
    """One definition of ``name``; ``removed`` drops it from a modified head file."""

    name: str
    form: str
    context: str
    removed: bool


@dataclass(frozen=True)
class ImportStmt:
    """One static import statement and where it sits."""

    kind: Literal["import", "from"]
    level: int
    module: str | None
    aliases: tuple[tuple[str, str | None], ...]
    context: str


@dataclass(frozen=True)
class FileSpec:
    """Content of one file; ``order`` interleaves definitions, imports and decoys."""

    names: tuple[NameDef, ...]
    imports: tuple[ImportStmt, ...]
    decoys: tuple[str, ...]
    order: tuple[tuple[ItemKind, int], ...]
    latin1: bool


@dataclass(frozen=True)
class TreeFile:
    """A file as it appears in one tree."""

    slot: Slot
    spec: FileSpec
    corrupt: Corruption | None
    drop_removed: bool


@dataclass(frozen=True)
class Scenario:
    """Base and head trees (path to file) plus the change between them."""

    layout: Layout
    base: Mapping[str, TreeFile]
    head: Mapping[str, TreeFile]
    renames: tuple[tuple[str, str], ...]
    deleted: tuple[str, ...]
    added: tuple[str, ...]


def _visible(tree_file: TreeFile) -> list[NameDef]:
    """Definitions present in this tree's copy of the file."""
    return [d for d in tree_file.spec.names if not (tree_file.drop_removed and d.removed)]


# ---------------------------------------------------------------------------
# Renderer (string formatting only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    """File bytes (``None`` = unreadable) and the 1-based line of each import."""

    data: bytes | None
    import_lines: tuple[int, ...]


def _name_lines(name: str, form: str) -> list[str]:
    """Source lines that bind ``name`` at the current scope."""
    templates = {
        "def": [f"def {name}() -> None:", f"{_I}pass"],
        "async_def": [f"async def {name}() -> None:", f"{_I}pass"],
        "class": [f"class {name}:", f"{_I}pass"],
        "assign": [f"{name} = 1"],
        "chain": [f"_ = {name} = 1"],
        "annassign": [f"{name}: int = 1"],
        "augassign": [f"{name} += 1"],
        "tuple": [f"{name}, _ = 1, 2"],
        "list": [f"[_, {name}] = [1, 2]"],
        "starred": [f"*{name}, _ = 1, 2"],
        "nested": [f"(_, ({name}, _)) = (1, (2, 3))"],
        "import_as": [f"import json as {name}"],
        "from_as": [f"from json import dumps as {name}"],
    }
    return templates[form]


def _statement_text(stmt: ImportStmt) -> str:
    """Source text of one import statement."""
    names = ", ".join(
        name if asname is None else f"{name} as {asname}" for name, asname in stmt.aliases
    )
    if stmt.kind == "import":
        return f"import {names}"
    return f"from {'.' * stmt.level}{stmt.module or ''} import {names}"


def _decoy_lines(target: str) -> list[str]:
    """Dynamic and string-based imports of ``target``; none is a static import."""
    return [
        f'__import__("importlib").import_module("{target}")',
        f'__import__("{target}")',
        f'_SPEC = "from {target} import f"',
        f"# import {target}",
    ]


def _render(tree_file: TreeFile) -> Rendered:
    """Render one file and record where each of its imports landed."""
    spec = tree_file.spec
    lines = ["# -*- coding: latin-1 -*-", "# caf\xe9"] if spec.latin1 else []
    lines.append("from typing import TYPE_CHECKING")
    import_lines: dict[int, int] = {}
    for kind, index in spec.order:
        if kind == "name":
            definition = spec.names[index]
            if tree_file.drop_removed and definition.removed:
                continue
            lines.extend(_wrap(definition.context, _name_lines(definition.name, definition.form)))
        elif kind == "import":
            stmt = spec.imports[index]
            import_lines[index] = len(lines) + len(_CONTEXTS[stmt.context].before) + 1
            lines.extend(_wrap(stmt.context, [_statement_text(stmt)]))
        else:
            lines.extend(_decoy_lines(spec.decoys[index]))
    data = ("\n".join(lines) + "\n").encode("latin-1" if spec.latin1 else "utf-8")
    ordered = tuple(import_lines[index] for index in range(len(spec.imports)))
    if tree_file.corrupt == "unreadable":
        return Rendered(None, ordered)
    if tree_file.corrupt == "syntax":
        data += b"def broken(:\n"
    elif tree_file.corrupt == "null":
        data += b"_nul = 1\x00\n"
    return Rendered(data, ordered)


# ---------------------------------------------------------------------------
# Oracle (model only; no ast, no path parsing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _View:
    """Top_Level_Names of a module in one tree."""

    names: frozenset[str]
    open: bool


_NAMESPACE: Final = _View(frozenset(), open=False)


@dataclass(frozen=True)
class Expected:
    """What :func:`check_trees` must return for a scenario."""

    broken: frozenset[Record]
    unparsed: tuple[str, ...]
    incomplete: bool
    removed_modules: frozenset[str]
    removed_names: frozenset[tuple[str, str]]


def _file_view(tree_file: TreeFile) -> _View:
    """Names a parseable file binds in module scope, and whether it is open."""
    names = {d.name for d in _visible(tree_file) if _CONTEXTS[d.context].binds}
    star = False
    for stmt in tree_file.spec.imports:
        if not _CONTEXTS[stmt.context].binds:
            continue
        for name, asname in stmt.aliases:
            if stmt.kind == "import":
                names.add(asname or name.split(".")[0])
            elif name == "*":
                star = True
            else:
                names.add(asname or name)
    return _View(frozenset(names), open=star or "__getattr__" in names)


def _module_set(layout: Layout, tree: Mapping[str, TreeFile]) -> frozenset[str]:
    """Module_Set of a tree."""
    return frozenset().union(*(_contributes(layout, tf.slot) for tf in tree.values()))


def _defining_path(layout: Layout, tree: Mapping[str, TreeFile], module: str) -> str | None:
    """The file that defines ``module``; a package file beats a module file."""
    candidates = sorted(path for path, tf in tree.items() if module in _defines(layout, tf.slot))
    packages = [path for path in candidates if tree[path].slot.is_package]
    preferred = packages or candidates
    return preferred[0] if preferred else None


def _view(tree: Mapping[str, TreeFile], path: str | None) -> _View | None:
    """Names of the module whose file is ``path``; ``None`` when it will not parse."""
    if path is None:
        return _NAMESPACE
    tree_file = tree[path]
    return None if tree_file.corrupt is not None else _file_view(tree_file)


def _resolve(slot: Slot, level: int, module: str | None) -> str | None:
    """Absolute target of an import in a file on ``slot`` (design §1 rule)."""
    if level == 0:
        return module
    package = slot.package
    keep = len(package) - (level - 1)
    if not package or keep < 0:
        return None
    parts = (*package[:keep], *(module.split(".") if module else ()))
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _under(name: str, removed: frozenset[str]) -> bool:
    """Whether ``name`` or a dotted prefix of it is a Removed_Module."""
    parts = name.split(".")
    return any(".".join(parts[:depth]) in removed for depth in range(1, len(parts) + 1))


def _removed_names(
    scenario: Scenario, common: frozenset[str], changed: frozenset[str]
) -> frozenset[tuple[str, str]]:
    """Removed_Name pairs (Req 3.5, 3.6, 3.7)."""
    removed: set[tuple[str, str]] = set()
    for module in common:
        base_path = _defining_path(scenario.layout, scenario.base, module)
        head_path = _defining_path(scenario.layout, scenario.head, module)
        if base_path not in changed and head_path not in changed:
            continue
        base_view = _view(scenario.base, base_path)
        head_view = _view(scenario.head, head_path)
        if base_view is None or head_view is None or head_view.open:
            continue
        removed.update((module, name) for name in base_view.names - head_view.names)
    return frozenset(removed)


def _scan(
    scenario: Scenario,
    head_lines: Mapping[str, tuple[int, ...]],
    head_set: frozenset[str],
    removed_modules: frozenset[str],
    removed_names: frozenset[tuple[str, str]],
) -> frozenset[Record]:
    """Broken_Imports in every parseable head file (Req 4.1 to 4.9)."""
    broken: set[Record] = set()
    for path, tree_file in scenario.head.items():
        if tree_file.corrupt is not None:
            continue
        for stmt, line in zip(tree_file.spec.imports, head_lines[path], strict=True):
            if _CONTEXTS[stmt.context].guarded:
                continue
            if stmt.kind == "import":
                broken.update(
                    (path, line, name, None, "removed_module")
                    for name, _ in stmt.aliases
                    if _under(name, removed_modules)
                )
                continue
            module = _resolve(tree_file.slot, stmt.level, stmt.module)
            if module is None:
                continue
            if _under(module, removed_modules):
                broken.update(
                    (path, line, module, name, "removed_module") for name, _ in stmt.aliases
                )
                continue
            view = _view(scenario.head, _defining_path(scenario.layout, scenario.head, module))
            for name, _ in stmt.aliases:
                if name == "*":
                    continue
                submodule = f"{module}.{name}"
                # An unparsed head file (view None) or an open namespace binds every name.
                present = view is None or view.open or name in view.names
                if submodule in removed_modules and not present:
                    broken.add((path, line, module, name, "removed_module"))
                elif (module, name) in removed_names and submodule not in head_set:
                    broken.add((path, line, module, name, "removed_name"))
    return frozenset(broken)


def _oracle(
    scenario: Scenario,
    head_lines: Mapping[str, tuple[int, ...]],
    changed: frozenset[str],
    head_changed: frozenset[str],
) -> Expected:
    """Compute the expected result from the model alone."""
    base_set = _module_set(scenario.layout, scenario.base)
    head_set = _module_set(scenario.layout, scenario.head)
    removed_modules = base_set - head_set
    removed_names = _removed_names(scenario, base_set & head_set, changed)
    bad_base = {p for p, tf in scenario.base.items() if tf.corrupt is not None and p in changed}
    bad_head = {p for p, tf in scenario.head.items() if tf.corrupt is not None}
    return Expected(
        broken=_scan(scenario, head_lines, head_set, removed_modules, removed_names),
        unparsed=tuple(sorted(bad_base | bad_head)),
        incomplete=bool(bad_base) or not bad_head.isdisjoint(head_changed),
        removed_modules=removed_modules,
        removed_names=removed_names,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_LAYOUTS: Final[tuple[Layout, ...]] = ("flat", "src")
_CORRUPTIONS: Final[tuple[Corruption | None, ...]] = (
    None,
    None,
    None,
    None,
    None,
    None,
    "syntax",
    "null",
    "unreadable",
)
_FATES: Final = ("keep", "modify", "modify", "modify", "delete", "rename", "rename")
_PROBE_CONTEXTS: Final = ("module",) * 8 + _IMPORT_CONTEXTS
_FAMILIES: Final[tuple[tuple[str, ...], ...]] = (
    (),
    (),
    ("p/__init__.py", "p/m.py"),
    ("p/q/__init__.py", "p/q/m.py"),
    ("p.py", "p/n.py"),
)
"""Parent and child files a base tree may start from, so packages meet submodules."""

_name_defs: Final = st.builds(
    NameDef,
    name=st.sampled_from(_NAME_POOL),
    form=st.sampled_from(_NAME_FORMS),
    context=st.sampled_from(_NAME_CONTEXTS),
    removed=st.booleans(),
)


@st.composite
def _import_stmts(draw: st.DrawFn, layout: Layout, hot: tuple[str, ...]) -> ImportStmt:
    """One import statement; star imports only where they are legal.

    Absolute targets lean towards ``hot`` (modules of the base tree) so that
    deletions, renames and removed names are actually imported somewhere.
    """
    context = draw(st.sampled_from(_IMPORT_CONTEXTS))
    in_match = context == "match"
    pool = _SRC_TARGETS if layout == "src" else _FLAT_TARGETS
    targets = st.sampled_from(hot) | st.sampled_from(pool) if hot else st.sampled_from(pool)

    def asname() -> str | None:
        return _MATCH_ASNAME if in_match else draw(st.sampled_from(_ASNAMES))

    if draw(st.integers(0, 2)) == 0:
        modules = draw(st.lists(targets, min_size=1, max_size=2))
        return ImportStmt("import", 0, None, tuple((m, asname()) for m in modules), context)
    level = draw(st.sampled_from(_LEVELS))
    module = draw(targets) if level == 0 else draw(st.sampled_from(_REL_MODULES))
    if _CONTEXTS[context].binds and draw(st.integers(0, 7)) == 0:
        return ImportStmt("from", level, module, (("*", None),), context)
    names = draw(st.lists(st.sampled_from(_ALIAS_POOL), min_size=1, max_size=3))
    return ImportStmt("from", level, module, tuple((n, asname()) for n in names), context)


@st.composite
def _file_specs(
    draw: st.DrawFn, layout: Layout, hot: tuple[str, ...], shadows: tuple[str, ...] = ()
) -> FileSpec:
    """Content for one file: at most 4 definitions, 3 imports and 1 decoy.

    ``shadows`` are the leaf names of this module's submodules in the base
    tree; one of them may be defined too, so that the "``n`` is a Top_Level_Name
    of M" clause of Req 4.4 and the "``M.n`` not in Head_Module_Set" clause of
    Req 4.5 come up. :func:`_with_probe` may add a fourth import later.
    """
    drafted = draw(st.lists(_name_defs, max_size=3))
    if len(shadows) > 0 and draw(st.booleans()):
        drafted.append(replace(draw(_name_defs), name=draw(st.sampled_from(shadows))))
    else:
        drafted.extend(draw(st.lists(_name_defs, max_size=1)))
    names = tuple(drafted)
    imports = tuple(draw(st.lists(_import_stmts(layout, hot), max_size=3)))
    targets = _SRC_TARGETS if layout == "src" else _FLAT_TARGETS
    decoys = tuple(draw(st.lists(st.sampled_from(targets), max_size=1)))
    items: list[tuple[ItemKind, int]] = []
    items.extend(("name", index) for index in range(len(names)))
    items.extend(("import", index) for index in range(len(imports)))
    items.extend(("decoy", index) for index in range(len(decoys)))
    order = tuple(draw(st.permutations(items)))
    latin1 = draw(st.sampled_from((False, False, False, True)))
    return FileSpec(names=names, imports=imports, decoys=decoys, order=order, latin1=latin1)


def _relative_form(
    layout: Layout, importer: Slot, target: Slot, parts: tuple[str, ...]
) -> tuple[int, str | None] | None:
    """``(level, module)`` spelling ``parts`` relative to ``importer``, if any.

    Only offered when both files share a Module_Root and a package prefix.
    """
    if layout == "src" and importer.code != target.code:
        return None
    package = importer.package
    shared = 0
    while shared < min(len(package), len(parts)) and package[shared] == parts[shared]:
        shared += 1
    if shared == 0:
        return None
    return len(package) - shared + 1, ".".join(parts[shared:]) or None


def _shadows(slot: Slot, chosen: list[Slot]) -> tuple[str, ...]:
    """Leaf names of the base submodules directly under ``slot``'s module."""
    leaves = set()
    for other in chosen:
        parent, _, leaf = (other.own or "").rpartition(".")
        if slot.own and parent == slot.own:
            leaves.add(leaf)
    return tuple(sorted(leaves))


Target = tuple[Slot, FileSpec, str]
"""A base file a probe can aim at: slot, content and the fate the change gives it."""


def _leaning(
    targets: tuple[Target, ...], favoured: tuple[Target, ...]
) -> st.SearchStrategy[Target]:
    """Pick from ``targets``, half the time from the ``favoured`` subset."""
    everything = st.sampled_from(targets)
    return st.sampled_from(favoured) | everything if favoured else everything


@st.composite
def _probe_imports(
    draw: st.DrawFn, layout: Layout, importer: Slot, targets: tuple[Target, ...]
) -> ImportStmt:
    """A ``from`` import of a name or submodule that some base file defines.

    Independent draws rarely line up with what the change removes, so name
    probes lean towards modified files and their dropped definitions (Req 4.5)
    and submodule probes towards deleted or renamed files (Req 4.4).
    """
    context = draw(st.sampled_from(_PROBE_CONTEXTS))
    nested = tuple(target for target in targets if "." in (target[0].own or ""))
    if len(nested) > 0 and draw(st.booleans()):
        moved = tuple(target for target in nested if target[2] in {"delete", "rename"})
        slot, _, _ = draw(_leaning(nested, moved))
        parent, leaf = (slot.own or "").rsplit(".", 1)
        parts: tuple[str, ...] = tuple(parent.split("."))
        names: tuple[str, ...] = (leaf,)
    else:
        droppers = tuple(
            target
            for target in targets
            if target[2] == "modify" and any(d.removed for d in target[1].names)
        )
        slot, spec, _ = draw(_leaning(targets, droppers))
        parts = tuple((slot.own or "").split("."))
        dropped = {d.name for d in spec.names if d.removed}
        prefer_dropped = len(dropped) > 0 and draw(st.booleans())
        defined = dropped if prefer_dropped else {d.name for d in spec.names}
        pool = sorted(defined - {"__getattr__"}) or list(_ALIAS_POOL)
        names = tuple(draw(st.lists(st.sampled_from(pool), min_size=1, max_size=2)))
    relative = _relative_form(layout, importer, slot, parts)
    if relative is not None and draw(st.booleans()):
        level, module = relative
    else:
        absolute = ".".join(parts)
        spellings = (absolute, f"src.{absolute}") if layout == "src" and slot.code else (absolute,)
        level, module = 0, draw(st.sampled_from(spellings))
    asname = _MATCH_ASNAME if context == "match" else None
    return ImportStmt("from", level, module, tuple((name, asname) for name in names), context)


@st.composite
def _with_probe(
    draw: st.DrawFn,
    layout: Layout,
    importer: Slot,
    spec: FileSpec,
    targets: tuple[Target, ...],
) -> FileSpec:
    """``spec`` plus, three times in four, one probe import at a random position.

    A probe never aims at its own file: a module-scope ``from M import n``
    inside M binds ``n`` itself, so it could never see ``n`` removed.
    """
    others = tuple(target for target in targets if target[0] != importer)
    if not others or draw(st.integers(0, 3)) == 0:
        return spec
    probe = draw(_probe_imports(layout, importer, others))
    order = list(spec.order)
    order.insert(draw(st.integers(0, len(order))), ("import", len(spec.imports)))
    return replace(spec, imports=(*spec.imports, probe), order=tuple(order))


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    """A base tree of 2 to 6 files and a change applied to it."""
    layout = draw(st.sampled_from(_LAYOUTS))
    corruptions = _CORRUPTIONS if draw(st.integers(0, 2)) == 0 else (None,)

    def corruption() -> Corruption | None:
        return draw(st.sampled_from(corruptions))

    family = [_SLOT_BY_REL[rel] for rel in draw(st.sampled_from(_FAMILIES))]
    extra = draw(
        st.lists(st.sampled_from(_SLOTS), min_size=2 - len(family), max_size=6, unique=True)
    )
    chosen = list(dict.fromkeys([*family, *extra]))[:6]
    hot = tuple(sorted(frozenset().union(*(_contributes(layout, slot) for slot in chosen))))
    fates = [draw(st.sampled_from(_FATES)) for _ in chosen]
    drafts = [draw(_file_specs(layout, hot, _shadows(slot, chosen))) for slot in chosen]
    targets = tuple(
        (slot, spec, fate)
        for slot, spec, fate in zip(chosen, drafts, fates, strict=True)
        if slot.own is not None
    )
    specs = [
        draw(_with_probe(layout, slot, spec, targets))
        for slot, spec in zip(chosen, drafts, strict=True)
    ]
    used = {slot.rel for slot in chosen}
    base: dict[str, TreeFile] = {}
    head: dict[str, TreeFile] = {}
    renames: list[tuple[str, str]] = []
    deleted: list[str] = []
    for slot, spec, fate in zip(chosen, specs, fates, strict=True):
        path = _path(layout, slot)
        if fate == "keep":
            # Same corruption on both sides: an unchanged (possibly unparsed) file.
            both = corruption()
            base[path] = TreeFile(slot, spec, both, drop_removed=False)
            head[path] = TreeFile(slot, spec, both, drop_removed=False)
            continue
        base[path] = TreeFile(slot, spec, corruption(), drop_removed=False)
        if fate == "modify":
            head[path] = TreeFile(slot, spec, corruption(), drop_removed=True)
            continue
        free = sorted(rel for rel in _SLOT_BY_REL if rel not in used)
        swap = _SWAPS.get(slot.rel)
        if fate == "delete" or not free:
            deleted.append(path)
            continue
        if swap in free and draw(st.booleans()):
            target = _SLOT_BY_REL[swap]
        else:
            target = _SLOT_BY_REL[draw(st.sampled_from(free))]
        used.add(target.rel)
        new_path = _path(layout, target)
        head[new_path] = TreeFile(target, spec, corruption(), drop_removed=draw(st.booleans()))
        renames.append((path, new_path))
    added: list[str] = []
    free = sorted(rel for rel in _SLOT_BY_REL if rel not in used)
    if len(free) > 0 and draw(st.booleans()):
        slot = _SLOT_BY_REL[draw(st.sampled_from(free))]
        path = _path(layout, slot)
        spec = draw(_with_probe(layout, slot, draw(_file_specs(layout, hot)), targets))
        head[path] = TreeFile(slot, spec, corruption(), drop_removed=False)
        added.append(path)
    return Scenario(
        layout=layout,
        base=base,
        head=head,
        renames=tuple(renames),
        deleted=tuple(deleted),
        added=tuple(added),
    )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _tracked_names(scenario: Scenario) -> frozenset[str]:
    """Names the oracle models exactly: definitions and import bindings.

    Helper names from the scaffolding (``_helper``, ``_``, ``TYPE_CHECKING``
    ...) and the ``match`` as-name are left out of the Removed_Name comparison.
    """
    names: set[str] = set()
    for tree_file in (*scenario.base.values(), *scenario.head.values()):
        names.update(d.name for d in tree_file.spec.names)
        for stmt in tree_file.spec.imports:
            for name, asname in stmt.aliases:
                if stmt.kind == "import":
                    names.add(asname or name.split(".")[0])
                elif name != "*":
                    names.add(asname or name)
    return frozenset(names - {_MATCH_ASNAME})


def _changed_paths(
    scenario: Scenario,
    base_map: Mapping[str, bytes | None],
    head_map: Mapping[str, bytes | None],
) -> tuple[frozenset[str], frozenset[str]]:
    """``changed_paths`` and ``head_changed_paths`` as the I/O shell derives them."""
    changed = set(scenario.deleted) | set(scenario.added)
    head_changed = set(scenario.added)
    for old_path, new_path in scenario.renames:
        changed |= {old_path, new_path}
        head_changed.add(new_path)
    for path in scenario.base.keys() & scenario.head.keys():
        if base_map[path] != head_map[path]:
            changed.add(path)
            head_changed.add(path)
    return frozenset(changed), frozenset(head_changed)


def _note_tree(side: str, contents: Mapping[str, bytes | None]) -> None:
    """Print a tree's rendered sources when hypothesis reports a failure."""
    for path in sorted(contents):
        data = contents[path]
        text = "<unreadable>" if data is None else data.decode("latin-1")
        note(f"--- {side} {path}\n{text}")


@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_check_trees_matches_the_model_oracle(scenario: Scenario) -> None:
    """``check_trees`` reports exactly what the oracle derives from the model."""
    base_rendered = {path: _render(tf) for path, tf in scenario.base.items()}
    head_rendered = {path: _render(tf) for path, tf in scenario.head.items()}
    base_map: dict[str, bytes | None] = {path: r.data for path, r in base_rendered.items()}
    head_map: dict[str, bytes | None] = {path: r.data for path, r in head_rendered.items()}
    base_map[_NOTES_PATH] = _NOTES_TEXT
    head_map[_NOTES_PATH] = _NOTES_TEXT
    changed, head_changed = _changed_paths(scenario, base_map, head_map)
    _note_tree("base", base_map)
    _note_tree("head", head_map)
    note(f"changed_paths={sorted(changed)} head_changed_paths={sorted(head_changed)}")

    head_lines = {path: r.import_lines for path, r in head_rendered.items()}
    expected = _oracle(scenario, head_lines, changed, head_changed)
    result = check_trees(base_map, head_map, changed_paths=changed, head_changed_paths=head_changed)

    actual = {(r.path, r.line, r.module, r.name, r.kind) for r in result.broken}
    for kind in sorted({record[4] for record in expected.broken}):
        event(f"broken {kind}")
    event(f"incomplete={expected.incomplete}")

    assert result.removed_modules == expected.removed_modules
    tracked = _tracked_names(scenario)
    assert {pair for pair in result.removed_names if pair[1] in tracked} == expected.removed_names
    assert actual == expected.broken
    assert result.unparsed_files == expected.unparsed
    assert result.incomplete is expected.incomplete
    # Req 4.11: one record per finding, ordered by path then line.
    assert len(result.broken) == len(actual)
    positions = [(record.path, record.line) for record in result.broken]
    assert positions == sorted(positions)
    # Req 4.12: nothing removed means nothing broken.
    if not result.removed_modules and not result.removed_names:
        assert result.broken == ()
