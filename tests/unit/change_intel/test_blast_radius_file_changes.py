"""Unit tests for :func:`trikon.change_intel.blast_radius._public_file_changes_sorted`.

Covers Task 6.1 item (e) in
``.kiro/specs/head-path-existence-filter/tasks.md``. Locks the
Wave-1 plumbing helper that translates
``ChangeSet.files: tuple[FileChange, ...]`` into
:class:`~trikon.evidence.report.FileChangeInfo` entries on the public
boundary:

* the output is sorted by ``path`` POSIX-lexicographic ascending so
  identical inputs produce byte-identical JSON downstream
  (Requirement 2.6);
* every ``change_kind == "renamed"`` input preserves its ``old_path``
  verbatim on the resulting :class:`FileChangeInfo`
  (Requirement 2.4);
* every non-rename input carries ``old_path is None`` on the
  resulting :class:`FileChangeInfo` regardless of what the input
  :class:`~trikon.change_intel.models.FileChange.old_path` says
  (Requirement 2.5 — the helper strips a spurious ``old_path`` from a
  non-rename entry so a malformed producer cannot leak an
  invariant-violating value onto the public boundary).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6.
"""

from __future__ import annotations

from trikon.change_intel.blast_radius import _public_file_changes_sorted
from trikon.change_intel.models import ChangeKind, FileChange
from trikon.evidence.report import FileChangeInfo


def _fc(
    path: str,
    change_kind: ChangeKind = "modified",
    *,
    old_path: str | None = None,
) -> FileChange:
    """Compact :class:`FileChange` factory for the translation tests.

    The helper under test consumes only ``path``, ``change_kind``, and
    ``old_path``, so ``hunks`` is fixed to the empty tuple — no
    downstream code looks at it on this raise site.
    """
    return FileChange(path=path, change_kind=change_kind, old_path=old_path, hunks=())


def test_public_file_changes_sorted_orders_by_path() -> None:
    """Output is sorted by ``path`` POSIX-lexicographic ascending.

    The input tuple is deliberately shuffled so a stable-but-unsorted
    pass-through implementation would fail the equality assertion. The
    expected order is ``a.py`` < ``b.py`` < ``c.py`` under
    POSIX-lexicographic ordering (Requirement 2.6).
    """
    files = (
        _fc("src/c.py", "modified"),
        _fc("src/a.py", "modified"),
        _fc("src/b.py", "modified"),
    )

    result = _public_file_changes_sorted(files)

    assert [info.path for info in result] == ["src/a.py", "src/b.py", "src/c.py"]


def test_public_file_changes_sorted_preserves_renamed_old_path() -> None:
    """A ``change_kind == "renamed"`` entry carries its ``old_path`` verbatim.

    The internal :class:`FileChange` records the pre-rename POSIX path
    on ``old_path`` when ``change_kind == "renamed"``; the helper must
    copy that value onto :class:`FileChangeInfo` without transformation
    (Requirement 2.4).
    """
    files = (_fc("src/new_name.py", "renamed", old_path="src/old_name.py"),)

    result = _public_file_changes_sorted(files)

    assert result == [
        FileChangeInfo(
            path="src/new_name.py",
            change_kind="renamed",
            old_path="src/old_name.py",
        ),
    ]


def test_public_file_changes_sorted_non_rename_carries_none_old_path() -> None:
    """Non-rename entries carry ``old_path is None`` on the public boundary.

    Even if a malformed :class:`FileChange` producer sets ``old_path``
    to a non-``None`` value on an ``added`` / ``modified`` / ``deleted``
    entry, the public :class:`FileChangeInfo` must surface
    ``old_path is None`` — the helper drops the stray value
    (Requirement 2.5).
    """
    files = (
        _fc("src/added.py", "added", old_path=None),
        _fc("src/modified.py", "modified", old_path=None),
        _fc("src/deleted.py", "deleted", old_path=None),
        # Malformed producer: a non-rename with a non-None old_path.
        # The helper must strip it.
        _fc("src/malformed.py", "modified", old_path="src/should_be_dropped.py"),
    )

    result = _public_file_changes_sorted(files)

    for info in result:
        assert info.old_path is None, (
            f"expected old_path=None on non-rename entry, got {info.old_path!r} on {info.path!r}"
        )


def test_public_file_changes_sorted_mixed_kinds_end_to_end() -> None:
    """Mixed-kind input produces the expected sorted, kind-preserving output.

    Combines every :data:`ChangeKind` value in one call and asserts the
    full projection: ``path`` verbatim, ``change_kind`` verbatim,
    ``old_path`` populated only on the rename entry, and the whole list
    sorted by ``path``.
    """
    files = (
        _fc("src/z_deleted.py", "deleted"),
        _fc("src/b_modified.py", "modified"),
        _fc("src/a_added.py", "added"),
        _fc("src/m_new.py", "renamed", old_path="src/m_old.py"),
    )

    result = _public_file_changes_sorted(files)

    assert result == [
        FileChangeInfo(path="src/a_added.py", change_kind="added", old_path=None),
        FileChangeInfo(path="src/b_modified.py", change_kind="modified", old_path=None),
        FileChangeInfo(
            path="src/m_new.py",
            change_kind="renamed",
            old_path="src/m_old.py",
        ),
        FileChangeInfo(path="src/z_deleted.py", change_kind="deleted", old_path=None),
    ]


def test_public_file_changes_sorted_empty_input_returns_empty_list() -> None:
    """An empty input tuple maps to an empty output list.

    Guards against a fencepost regression where the helper's sort key
    or comprehension could return ``None`` / raise on an empty iterable.
    """
    assert _public_file_changes_sorted(()) == []


def test_public_file_changes_sorted_is_deterministic_across_calls() -> None:
    """Calling the helper twice on the same input yields equal outputs.

    Locks the determinism half of Requirement 2.6 — identical inputs
    must produce identical outputs so downstream JSON serialization is
    byte-stable across repeated ``verify`` invocations.
    """
    files = (
        _fc("src/c.py", "modified"),
        _fc("src/a.py", "added"),
        _fc("src/b.py", "renamed", old_path="src/b_old.py"),
    )

    assert _public_file_changes_sorted(files) == _public_file_changes_sorted(files)
