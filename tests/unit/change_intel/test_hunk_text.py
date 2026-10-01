"""Unit tests for the :class:`~trikon.change_intel.models.Hunk` line-text fields.

Covers Task 2.5 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``.
``Hunk.source_lines`` / ``Hunk.target_lines`` carry the hunk's pre-image and
post-image text so the import checker can rebuild the base file without a
base SHA. The tests pin down two contracts:

* The new fields are invisible to ``==``, ``hash()`` and ``repr()``, so every
  existing equality assertion and content-keyed cache behaves as before.
* :func:`~trikon.change_intel.diff_parser.parse_diff` fills them from the
  diff in order, without line terminators, and leaves out unidiff's
  ``\\ No newline at end of file`` marker lines.

Every diff is an in-memory string built with explicit ``\\n`` escapes, so the
tests behave identically on Windows and POSIX.

_Validates: Requirements 3.3._
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trikon.change_intel import FileChange, Hunk, parse_diff

if TYPE_CHECKING:
    from pathlib import Path


_NO_NEWLINE_MARKER = "\\ No newline at end of file"
"""The marker line git emits after a final line that lacks a terminator."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _only_file(repo: Path, diff: str) -> FileChange:
    """Parse ``diff`` on the diff-string path and return its single FileChange."""
    change_set = parse_diff(repo, diff=diff)
    assert len(change_set.files) == 1, change_set.files
    return change_set.files[0]


def _all_text(hunk: Hunk) -> tuple[str, ...]:
    """Return every stored line of ``hunk`` from both sides."""
    return hunk.source_lines + hunk.target_lines


# ---------------------------------------------------------------------------
# Equality, hashing and repr are unchanged
# ---------------------------------------------------------------------------


def test_hunks_differing_only_in_text_are_equal_hash_equal_and_repr_equal() -> None:
    """Two Hunks with the same indices but different text are indistinguishable."""
    left = Hunk(
        old_start=3,
        old_lines=2,
        new_start=3,
        new_lines=2,
        added_lines=(4,),
        removed_lines=(4,),
        source_lines=("ctx", "old"),
        target_lines=("ctx", "new"),
    )
    right = Hunk(
        old_start=3,
        old_lines=2,
        new_start=3,
        new_lines=2,
        added_lines=(4,),
        removed_lines=(4,),
        source_lines=("something", "else"),
        target_lines=(),
    )

    assert left == right
    assert hash(left) == hash(right)
    assert len({left, right}) == 1
    assert repr(left) == repr(right)
    assert "source_lines" not in repr(left)
    assert "target_lines" not in repr(left)


def test_hunks_differing_in_indices_are_still_unequal() -> None:
    """Index fields still take part in equality; only the text fields are excluded."""
    base = Hunk(old_start=1, old_lines=1, new_start=1, new_lines=1, source_lines=("x",))
    moved = Hunk(old_start=2, old_lines=1, new_start=1, new_lines=1, source_lines=("x",))

    assert base != moved


def test_parsed_hunk_equals_index_only_hunk(tmp_path: Path) -> None:
    """A parsed Hunk (text filled) equals one built from indices alone.

    This is the compatibility guarantee for existing tests and caches that
    construct ``Hunk`` values without the new fields.
    """
    diff = (
        "diff --git a/m.py b/m.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1 +1 @@\n"
        "-v = 1\n"
        "+v = 2\n"
    )
    expected = FileChange(
        path="m.py",
        change_kind="modified",
        old_path=None,
        hunks=(
            Hunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=1,
                added_lines=(1,),
                removed_lines=(1,),
            ),
        ),
    )

    parsed = _only_file(tmp_path, diff)

    assert parsed.hunks[0].source_lines == ("v = 1",)
    assert parsed.hunks[0].target_lines == ("v = 2",)
    assert parsed == expected
    assert hash(parsed) == hash(expected)


# ---------------------------------------------------------------------------
# parse_diff fills the text fields
# ---------------------------------------------------------------------------


def test_modification_diff_fills_source_and_target_lines_in_diff_order(tmp_path: Path) -> None:
    """Context+removed go to ``source_lines``; context+added go to ``target_lines``."""
    diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1,4 +1,5 @@\n"
        " a = 1\n"
        "-b = 2\n"
        "+b = 20\n"
        " c = 3\n"
        "-d = 4\n"
        "+d = 40\n"
        "+e = 5\n"
        "@@ -10,2 +11,3 @@ def tail():\n"
        "     j = 10\n"
        "+    k = 11\n"
        "\n"
        "@@ -20 +22,0 @@\n"
        "-z = 26\n"
    )

    file_change = _only_file(tmp_path, diff)
    assert file_change.change_kind == "modified"
    first, second, third = file_change.hunks

    assert first.source_lines == ("a = 1", "b = 2", "c = 3", "d = 4")
    assert first.target_lines == ("a = 1", "b = 20", "c = 3", "d = 40", "e = 5")
    # The index sets are unchanged by the new fields.
    assert first.added_lines == (2, 4, 5)
    assert first.removed_lines == (2, 4)

    # An empty context line ("\n" in the diff body) is stored as "".
    assert second.source_lines == ("    j = 10", "")
    assert second.target_lines == ("    j = 10", "    k = 11", "")

    # A pure-removal hunk (``--unified=0`` style) has an empty target side.
    assert third.source_lines == ("z = 26",)
    assert third.target_lines == ()

    for hunk in file_change.hunks:
        assert all("\n" not in text and "\r" not in text for text in _all_text(hunk))


def test_no_newline_marker_on_target_side_is_left_out(tmp_path: Path) -> None:
    """A marker after the final added line (end of hunk) does not reach either tuple."""
    diff = (
        "diff --git a/m.py b/m.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,2 +1,2 @@\n"
        " a = 1\n"
        "-b = 2\n"
        "+b = 3\n"
        f"{_NO_NEWLINE_MARKER}\n"
    )

    (hunk,) = _only_file(tmp_path, diff).hunks

    assert hunk.source_lines == ("a = 1", "b = 2")
    assert hunk.target_lines == ("a = 1", "b = 3")


def test_no_newline_markers_on_both_sides_are_left_out(tmp_path: Path) -> None:
    """Markers inside the hunk body and after it are both dropped.

    The first marker follows the removed line while unidiff is still reading
    the hunk body; the second follows the last added line after the hunk is
    complete. unidiff records them through different code paths, so this
    covers both.
    """
    diff = (
        "diff --git a/m.py b/m.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,2 +1,2 @@\n"
        " a = 1\n"
        "-b = 2\n"
        f"{_NO_NEWLINE_MARKER}\n"
        "+b = 3\n"
        f"{_NO_NEWLINE_MARKER}\n"
    )

    (hunk,) = _only_file(tmp_path, diff).hunks

    assert hunk.source_lines == ("a = 1", "b = 2")
    assert hunk.target_lines == ("a = 1", "b = 3")
    assert not any("No newline" in text for text in _all_text(hunk))
    assert hunk.added_lines == (2,)
    assert hunk.removed_lines == (2,)


def test_crlf_diff_lines_are_stored_without_carriage_return(tmp_path: Path) -> None:
    """Content lines of a CRLF file keep no ``\\r`` once stored."""
    diff = (
        "diff --git a/win.py b/win.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/win.py\n"
        "+++ b/win.py\n"
        "@@ -1,3 +1,3 @@\n"
        " a = 1\r\n"
        "-b = 2\r\n"
        "+b = 3\r\n"
        "\r\n"
    )

    (hunk,) = _only_file(tmp_path, diff).hunks

    assert hunk.source_lines == ("a = 1", "b = 2", "")
    assert hunk.target_lines == ("a = 1", "b = 3", "")


def test_added_file_hunk_has_empty_source_lines(tmp_path: Path) -> None:
    """A new file has no pre-image, so only ``target_lines`` is filled."""
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+x = 1\n"
        "+y = 2\n"
    )

    file_change = _only_file(tmp_path, diff)
    assert file_change.change_kind == "added"
    (hunk,) = file_change.hunks

    assert hunk.source_lines == ()
    assert hunk.target_lines == ("x = 1", "y = 2")


def test_deleted_file_hunk_has_empty_target_lines(tmp_path: Path) -> None:
    """A deleted file has no post-image, so only ``source_lines`` is filled."""
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
        f"{_NO_NEWLINE_MARKER}\n"
    )

    file_change = _only_file(tmp_path, diff)
    assert file_change.change_kind == "deleted"
    (hunk,) = file_change.hunks

    assert hunk.source_lines == ("x = 1", "y = 2")
    assert hunk.target_lines == ()
