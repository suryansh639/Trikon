"""Internal data models for the Change Intelligence pipeline.

Every type in this module is a frozen, slotted dataclass. That choice is
deliberate:

* Frozen dataclasses are immutable, so a ``ChangeSet`` or ``SymbolDef`` cannot
  be mutated after leaving its producer. Bugs where a downstream stage rewrites
  an upstream stage's output cannot happen.
* Frozen dataclasses are hashable, which lets us put ``SymbolDef`` instances
  into ``set()`` / ``dict`` keys during graph traversal without a bespoke
  ``__hash__``.
* ``slots=True`` eliminates the per-instance ``__dict__``. On a full Django
  cold index that is tens of thousands of symbol instantiations; the memory
  and allocation savings are meaningful on the hot path.

These types are the *internal* boundary of Change Intelligence. The public
boundary — the shape that lands in a ``Verdict`` — is the Pydantic model set in
:mod:`trikon.evidence.report`. The two shapes intentionally do not overlap; the
public surface has stricter validation and JSON-schema generation costs we do
not want to pay millions of times per cold index. :func:`to_public_symbol_ref`
is the one sanctioned bridge between the two worlds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trikon.evidence.report import SymbolRef


# ---------------------------------------------------------------------------
# Literal aliases
# ---------------------------------------------------------------------------

ChangeKind = Literal["added", "modified", "deleted", "renamed"]
"""How a single file changed between ``base_sha`` and ``head_sha``."""

SymbolKind = Literal["function", "class", "method", "assignment"]
"""The four symbol shapes Change Intelligence tracks in v0.1.

Lambdas, nested functions, comprehensions and ``TypeAlias`` are intentionally
not represented (see ``design.md §2.2``).
"""

RefKind = Literal["call", "import", "attribute_access"]
"""How a reference site touches its target symbol."""


# ---------------------------------------------------------------------------
# Diff models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hunk:
    """One contiguous edit region within a single file's diff.

    Line indices are 1-based and follow the unified-diff convention:
    ``old_start`` / ``old_lines`` describe the pre-image range, ``new_start`` /
    ``new_lines`` describe the post-image range. ``added_lines`` and
    ``removed_lines`` are the explicit index sets rather than counts, because
    downstream stages need to intersect them with symbol byte ranges.
    """

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    added_lines: tuple[int, ...] = field(default_factory=tuple)
    removed_lines: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class FileChange:
    """The full change to a single path between ``base_sha`` and ``head_sha``.

    ``path`` is POSIX-normalized and relative to ``ChangeSet.repo_path``.
    ``old_path`` is set only when ``change_kind == "renamed"``; the hunks are
    always expressed against the new path.
    """

    path: str
    change_kind: ChangeKind
    old_path: str | None
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The structured output of :func:`trikon.change_intel.diff_parser.parse_diff`.

    A ``ChangeSet`` is the sole input to :func:`compute_impact`. It carries the
    repo root, the SHA range (both ``None`` when the change was supplied as a
    raw diff string), and a POSIX-sorted tuple of ``FileChange`` entries so
    identical inputs produce byte-identical JSON.
    """

    repo_path: Path
    base_sha: str | None
    head_sha: str | None
    files: tuple[FileChange, ...]

    @property
    def python_files(self) -> tuple[FileChange, ...]:
        """Subset of :attr:`files` restricted to Python sources.

        Filters to ``.py`` and ``.pyi`` and preserves the tuple's existing
        POSIX-sorted order so downstream traversal remains deterministic.
        """
        return tuple(f for f in self.files if f.path.endswith((".py", ".pyi")))


# ---------------------------------------------------------------------------
# Symbol models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """A single function, class, method, or module-level assignment.

    ``file_path`` is POSIX-relative to the repo root. ``file_sha`` is the
    hex SHA-256 of the file's bytes at index time; together with
    ``qualified_name`` it forms the unique key used by
    :class:`~trikon.change_intel.dep_graph.DepGraph`. Line indices are 1-based
    inclusive; byte offsets are half-open into the UTF-8-encoded file bytes,
    so the enclosing-symbol lookup can slice source directly.
    """

    qualified_name: str
    kind: SymbolKind
    file_path: str
    file_sha: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    is_public: bool


@dataclass(frozen=True, slots=True)
class SymbolRefInternal:
    """One resolved reference site produced by the symbol resolver.

    Distinct from :class:`trikon.evidence.report.SymbolRef` on purpose: the
    public model is a Pydantic validated shape aimed at JSON emission, while
    this internal model is the cheap value object shuttled between resolver,
    dep graph, and blast-radius orchestrator.
    """

    target_qualified_name: str
    target_file_path: str
    referring_file: str
    referring_line: int
    kind: RefKind


# ---------------------------------------------------------------------------
# Public-boundary bridge
# ---------------------------------------------------------------------------


def to_public_symbol_ref(sym: SymbolDef) -> SymbolRef:
    """Translate an internal :class:`SymbolDef` into the public Pydantic model.

    ``trikon.evidence.report`` is imported lazily so that importing this module
    (which happens on every ``trikon`` import path) does not pull in Pydantic
    validation machinery. Callers on the cold path pay that cost only when
    they actually cross the public boundary.
    """
    from trikon.evidence.report import SymbolRef as PublicSymbolRef

    return PublicSymbolRef(
        qualified_name=sym.qualified_name,
        file_path=sym.file_path,
        kind=sym.kind,
    )


__all__ = [
    "ChangeKind",
    "ChangeSet",
    "FileChange",
    "Hunk",
    "RefKind",
    "SymbolDef",
    "SymbolKind",
    "SymbolRefInternal",
    "to_public_symbol_ref",
]
