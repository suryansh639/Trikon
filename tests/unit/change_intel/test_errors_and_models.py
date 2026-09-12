"""Unit tests for :mod:`trikon.change_intel.errors` and :mod:`trikon.change_intel.models`.

Covers Task 1.3 in ``.kiro/specs/change-intelligence/tasks.md``. Validates
Requirement 6.1 (error-hierarchy closure) at the whole-package level via an
AST scan, plus the frozen/slotted/hashable invariants of every internal data
model and the lazy-import contract of :func:`to_public_symbol_ref`.
"""

from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from trikon.change_intel import (
    AstParseError,
    BlastRadiusError,
    ChangeIntelError,
    ChangeSet,
    DepGraphError,
    DiffInputError,
    DiffParseError,
    FileChange,
    Hunk,
    RepoNotFoundError,
    SymbolDef,
    SymbolResolutionError,
)
from trikon.change_intel.models import SymbolRefInternal, to_public_symbol_ref

if TYPE_CHECKING:
    from trikon.evidence.report import SymbolRef as PublicSymbolRef


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ``tests/unit/change_intel/test_errors_and_models.py`` -> repo root (parents[3]).
CHANGE_INTEL_ROOT: Path = Path(__file__).resolve().parents[3] / "trikon" / "change_intel"

#: Every subclass of :class:`ChangeIntelError`. Kept as a hard-coded list so
#: adding a new subclass without updating this test is a deliberate act.
ERROR_SUBCLASSES: tuple[type[ChangeIntelError], ...] = (
    DiffInputError,
    RepoNotFoundError,
    DiffParseError,
    AstParseError,
    SymbolResolutionError,
    DepGraphError,
    BlastRadiusError,
)

#: Names that a ``raise`` site inside ``trikon/change_intel/**`` is allowed to
#: mention. The 8 ``ChangeIntelError`` names are the sanctioned vocabulary;
#: ``NotImplementedError`` covers stubs for tasks that have not landed yet
#: (see ``design.md §10 Definition of Done`` — must be zero on release);
#: ``AssertionError`` covers ``assert``-style unreachable guards.
_CHANGE_INTEL_ERROR_NAMES: frozenset[str] = frozenset(
    {"ChangeIntelError"} | {cls.__name__ for cls in ERROR_SUBCLASSES}
)
ALLOWED_STUB_NAMES: frozenset[str] = frozenset({"NotImplementedError", "AssertionError"})
ALLOWED_RAISE_NAMES: frozenset[str] = _CHANGE_INTEL_ERROR_NAMES | ALLOWED_STUB_NAMES


# ---------------------------------------------------------------------------
# Test-fixture builders — cheap constructors for the internal dataclasses.
# ---------------------------------------------------------------------------


def _hunk() -> Hunk:
    return Hunk(
        old_start=10,
        old_lines=2,
        new_start=10,
        new_lines=3,
        added_lines=(11,),
        removed_lines=(),
    )


def _file_change(path: str = "src/pkg/mod.py") -> FileChange:
    return FileChange(path=path, change_kind="modified", old_path=None, hunks=(_hunk(),))


def _change_set(files: tuple[FileChange, ...]) -> ChangeSet:
    return ChangeSet(
        repo_path=Path("/tmp/repo"),
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=files,
    )


def _symbol_def(qname: str = "pkg.mod.func") -> SymbolDef:
    return SymbolDef(
        qualified_name=qname,
        kind="function",
        file_path="src/pkg/mod.py",
        file_sha="c" * 64,
        start_line=1,
        end_line=10,
        start_byte=0,
        end_byte=200,
        is_public=True,
    )


def _symbol_ref_internal() -> SymbolRefInternal:
    return SymbolRefInternal(
        target_qualified_name="pkg.mod.func",
        target_file_path="src/pkg/mod.py",
        referring_file="src/pkg/other.py",
        referring_line=42,
        kind="call",
    )


# ---------------------------------------------------------------------------
# 1. Error hierarchy — importability, inheritance, and AstParseError kwargs.
# ---------------------------------------------------------------------------


def test_all_error_subclasses_importable_from_package() -> None:
    """Every error subclass is re-exported from :mod:`trikon.change_intel`.

    Consumers only ever import from the package root, so a missing re-export
    would break the SDK boundary contract without breaking the module itself.
    """
    package = importlib.import_module("trikon.change_intel")
    expected: tuple[type[ChangeIntelError], ...] = (ChangeIntelError, *ERROR_SUBCLASSES)
    for cls in expected:
        exported = getattr(package, cls.__name__, None)
        assert exported is cls, (
            f"{cls.__name__} is not re-exported from trikon.change_intel (got {exported!r})."
        )


@pytest.mark.parametrize("subclass", ERROR_SUBCLASSES, ids=lambda c: c.__name__)
def test_error_subclass_inherits_from_change_intel_error(
    subclass: type[ChangeIntelError],
) -> None:
    """Each error class inherits from :class:`ChangeIntelError`.

    The SDK boundary catches ``ChangeIntelError`` exactly once
    (``design.md §5.1``); any subclass that skips the base class silently
    bypasses the never-fail-open guard.
    """
    assert issubclass(subclass, ChangeIntelError)
    assert subclass is not ChangeIntelError


def test_ast_parse_error_exposes_file_path_and_line() -> None:
    """``AstParseError`` accepts ``file_path``/``line``/``cause`` and exposes them."""
    err = AstParseError(
        "syntax error at token",
        file_path="src/pkg/broken.py",
        line=17,
    )
    assert str(err) == "syntax error at token"
    assert err.file_path == "src/pkg/broken.py"
    assert err.line == 17
    assert err.cause is None


def test_ast_parse_error_defaults_line_to_none() -> None:
    """``line`` is optional; the default is ``None`` (unknown location)."""
    err = AstParseError("bad file", file_path="src/pkg/broken.py")
    assert err.line is None
    assert err.cause is None


def test_ast_parse_error_chains_cause() -> None:
    """Passing a :class:`SyntaxError` as ``cause`` sets ``__cause__`` for tracebacks."""
    syntax = SyntaxError("invalid syntax")
    err = AstParseError(
        "parse failed",
        file_path="src/pkg/broken.py",
        line=3,
        cause=syntax,
    )
    assert err.cause is syntax
    assert err.__cause__ is syntax


# ---------------------------------------------------------------------------
# 2. Data-model invariants — frozen, slotted, hashable.
# ---------------------------------------------------------------------------


# Tuples of (instance, mutable_field_name) — one per internal dataclass.
_FROZEN_CASES: tuple[tuple[object, str], ...] = (
    (_hunk(), "old_start"),
    (_file_change(), "path"),
    (_change_set((_file_change(),)), "base_sha"),
    (_symbol_def(), "qualified_name"),
    (_symbol_ref_internal(), "referring_line"),
)


@pytest.mark.parametrize(
    ("instance", "field_name"),
    _FROZEN_CASES,
    ids=[type(inst).__name__ for inst, _ in _FROZEN_CASES],
)
def test_dataclass_is_frozen(instance: object, field_name: str) -> None:
    """Mutating any field on a Change-Intel dataclass raises :class:`FrozenInstanceError`.

    ``frozen=True`` is what lets downstream stages hash and share these values
    without defensive copies; a regression here would open the door to spooky
    action at a distance across the pipeline.
    """
    with pytest.raises(FrozenInstanceError):
        setattr(instance, field_name, "mutated")


@pytest.mark.parametrize(
    "instance",
    [inst for inst, _ in _FROZEN_CASES],
    ids=[type(inst).__name__ for inst, _ in _FROZEN_CASES],
)
def test_dataclass_is_slotted(instance: object) -> None:
    """``slots=True`` means no per-instance ``__dict__`` — cheaper allocations on the hot path."""
    assert not hasattr(instance, "__dict__")


@pytest.mark.parametrize(
    "instance",
    [inst for inst, _ in _FROZEN_CASES],
    ids=[type(inst).__name__ for inst, _ in _FROZEN_CASES],
)
def test_dataclass_is_hashable(instance: object) -> None:
    """Instances are hashable — usable as :class:`set` members and dict keys.

    :meth:`DepGraph.transitive_dependents` puts ``SymbolDef`` values into sets
    during BFS; if hashability is lost we get a ``TypeError`` deep inside the
    graph walk. Better to fail here.
    """
    hash(instance)
    assert instance in {instance}
    assert {instance: 1}[instance] == 1


# ---------------------------------------------------------------------------
# 3. ``ChangeSet.python_files`` filter.
# ---------------------------------------------------------------------------


def test_python_files_filters_to_py_and_pyi() -> None:
    """``.py`` and ``.pyi`` survive the filter; other extensions do not."""
    files = (
        _file_change("README.md"),
        _file_change("src/pkg/mod.py"),
        _file_change("stubs/pkg/mod.pyi"),
        _file_change("assets/logo.png"),
        _file_change("scripts/deploy.sh"),
    )
    py_files = _change_set(files).python_files
    assert tuple(f.path for f in py_files) == ("src/pkg/mod.py", "stubs/pkg/mod.pyi")


def test_python_files_preserves_input_order() -> None:
    """Filtering is stable: the surviving entries stay in producer order.

    Producers (``diff_parser``) hand us POSIX-sorted tuples; the filter must
    not resort or we lose byte-identical JSON output across runs (Goal G1 in
    ``design.md``).
    """
    files = (
        _file_change("z/late.py"),
        _file_change("a/early.md"),
        _file_change("m/middle.py"),
    )
    py_files = _change_set(files).python_files
    assert tuple(f.path for f in py_files) == ("z/late.py", "m/middle.py")


def test_python_files_empty_input_returns_empty_tuple() -> None:
    """No files in ⇒ empty tuple out — never ``None`` or a mutable list."""
    out = _change_set(()).python_files
    assert out == ()
    assert isinstance(out, tuple)


# ---------------------------------------------------------------------------
# 4. ``to_public_symbol_ref`` boundary bridge.
# ---------------------------------------------------------------------------


def test_to_public_symbol_ref_copies_fields_verbatim() -> None:
    """The bridge preserves ``qualified_name``, ``file_path``, and ``kind`` exactly."""
    sym = _symbol_def(qname="pkg.mod.Cls.method")
    public: PublicSymbolRef = to_public_symbol_ref(sym)
    assert public.qualified_name == "pkg.mod.Cls.method"
    assert public.file_path == "src/pkg/mod.py"
    assert public.kind == "function"


def test_to_public_symbol_ref_lazily_imports_evidence_report() -> None:
    """The Pydantic public model is imported on first call, not at module load.

    Module load of :mod:`trikon.change_intel.models` must stay cheap because
    every ``trikon`` import path pulls it in. Only the (rare) crossing of the
    public boundary pays the Pydantic import cost.
    """
    module_name = "trikon.evidence.report"
    # Force a fresh state: pop the module if some earlier test imported it.
    sys.modules.pop(module_name, None)
    assert module_name not in sys.modules

    to_public_symbol_ref(_symbol_def())

    assert module_name in sys.modules, (
        "to_public_symbol_ref must import trikon.evidence.report at call time"
    )


# ---------------------------------------------------------------------------
# 5. AST-scan invariant — every ``raise`` in the package is an allowed name.
# ---------------------------------------------------------------------------


def _iter_change_intel_files() -> list[Path]:
    """Return every ``.py`` file under :data:`CHANGE_INTEL_ROOT`, sorted for stability."""
    return sorted(p for p in CHANGE_INTEL_ROOT.rglob("*.py") if p.is_file())


def _raise_name(node: ast.Raise) -> str | None:
    """Return the raised type name for a :class:`ast.Raise` node, or ``None`` for a bare re-raise.

    Handles the three shapes commonly seen in this package:

    * ``raise`` (no ``exc``) — implicit re-raise inside an ``except`` block.
    * ``raise SomeError`` — ``exc`` is an :class:`ast.Name`.
    * ``raise SomeError(...)`` or ``raise SomeError(...) from cause`` — ``exc``
      is an :class:`ast.Call` whose ``func`` is an :class:`ast.Name` or
      :class:`ast.Attribute`.
    """
    exc = node.exc
    if exc is None:  # bare re-raise; always allowed.
        return None
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Call):
        func = exc.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    if isinstance(exc, ast.Attribute):
        return exc.attr
    return None


@pytest.mark.parametrize(
    "source_file",
    _iter_change_intel_files(),
    ids=lambda p: p.name,
)
def test_change_intel_module_raises_only_sanctioned_types(source_file: Path) -> None:
    """Every ``raise`` in ``trikon/change_intel/**`` is a :class:`ChangeIntelError` subclass.

    ``NotImplementedError`` is grandfathered in as long as some Task-5.1 /
    Task-5.2 / Task-7.1 / Task-8.x stubs remain — the DoD in ``design.md §10``
    is that these disappear before Phase 1 ships.

    Validates: Requirements 6.1.
    """
    source = source_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_file))

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        name = _raise_name(node)
        if name is None:
            # Bare `raise` inside an except block — implicit re-raise, allowed.
            continue
        if name not in ALLOWED_RAISE_NAMES:
            violations.append(
                f"{source_file.relative_to(CHANGE_INTEL_ROOT.parent.parent)}"
                f":{node.lineno}: raise {name}(...) — expected a ChangeIntelError subclass"
            )

    assert not violations, "Unsanctioned raise sites detected:\n  " + "\n  ".join(violations)
