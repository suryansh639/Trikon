"""Regression tests for change-kind classification in :func:`parse_diff`.

``added`` and ``deleted`` come only from explicit diff markers: a
``/dev/null`` source or target, or git's ``new file mode`` / ``deleted file
mode`` extended headers. The shape of the hunks never decides it.
``--unified=0``, which the SHA path always uses, writes a removal starting at
line 1 as ``@@ -1,N +0,0 @@`` and an insertion at the top of a file as
``@@ -0,0 +1,N @@``. unidiff's own ``is_removed_file`` / ``is_added_file``
read those headers as a deleted / added file, which used to turn such
modifications (and a rename whose edit starts at line 1) into the wrong kind.

Every git scenario runs twice:

* ``sha``: the SHA path, which shells out to ``git diff --unified=0``;
* ``diff_string``: the same range as a raw ``--unified=0`` diff string, so
  the diff-string path sees the same hunk shapes.

Plain (non-git) unified diffs and directly built unidiff objects cover the
markers on their own. The last test checks the consequence for
:func:`check_imports`: a leading-lines removal whose head no longer parses
now counts as a changed head file, so the report is incomplete (Req 4.10).

Git runs with identity, signing and line-ending settings passed per command,
so the tests never depend on (or touch) global git config. Files are written
as bytes with explicit ``\\n`` terminators.

_Validates: Requirements 3.3, 4.10._
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest
from unidiff.patch import PatchedFile, PatchInfo

from trikon.change_intel import ChangeSet, FileChange, parse_diff
from trikon.change_intel.diff_parser import _classify_change_kind
from trikon.change_intel.import_check_io import check_imports
from trikon.change_intel.models import ChangeKind
from trikon.evidence.report import ImportReport

_GIT_OPTIONS: Final = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "init.defaultBranch=main",
)
"""Per-command git settings, so global config can neither break nor leak in."""

_BOTH_PATHS: Final = pytest.mark.parametrize("use_sha", [True, False], ids=["sha", "diff_string"])

_HunkRange = tuple[int, int, int, int]
"""``(old_start, old_lines, new_start, new_lines)`` of one hunk."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run ``git *args`` in ``repo`` and return stdout; fail loudly on error."""
    result = subprocess.run(
        ["git", *_GIT_OPTIONS, *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def _write(repo: Path, rel: str, content: bytes) -> None:
    """Write ``content`` to ``repo / rel`` as raw bytes, creating parents."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _commit(repo: Path, message: str) -> str:
    """Stage everything in ``repo``, commit, and return the new HEAD SHA."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _change_set(repo: Path, base: str, head: str, *, use_sha: bool) -> ChangeSet:
    """Return the ``base..head`` ChangeSet via the SHA path or a ``--unified=0`` diff string."""
    if use_sha:
        change_set = parse_diff(repo, base_sha=base, head_sha=head)
    else:
        diff = _git(
            repo,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
            "--find-renames",
            base,
            head,
        )
        change_set = parse_diff(repo, diff=diff)
    assert (change_set.base_sha is not None) is use_sha
    return change_set


def _only_file(change_set: ChangeSet) -> FileChange:
    """Return the single FileChange of ``change_set``."""
    (file_change,) = change_set.files
    return file_change


def _hunk_ranges(file_change: FileChange) -> tuple[_HunkRange, ...]:
    """Return every hunk's header range, so a test can pin the shape it exercises."""
    return tuple(
        (hunk.old_start, hunk.old_lines, hunk.new_start, hunk.new_lines)
        for hunk in file_change.hunks
    )


def _lines(count: int, *, prefix: str = "v") -> bytes:
    """Return ``count`` distinct ``<prefix><n> = <n>`` lines with ``\\n`` terminators."""
    return "".join(f"{prefix}{n} = {n}\n" for n in range(1, count + 1)).encode("utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty git repository at ``tmp_path / "repo"``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


# ---------------------------------------------------------------------------
# Modifications whose hunks look like an added or deleted file
# ---------------------------------------------------------------------------


@_BOTH_PATHS
@pytest.mark.parametrize(
    ("before", "after", "expected_ranges"),
    [
        pytest.param(
            b"a = 1\nb = 2\nc = 3\n", b"c = 3\n", ((1, 2, 0, 0),), id="leading_lines_removed"
        ),
        pytest.param(b"c = 3\n", b"a = 1\nb = 2\nc = 3\n", ((0, 0, 1, 2),), id="top_insertion"),
        pytest.param(
            b"a = 1\nb = 2\n", b"x = 1\ny = 2\nz = 3\n", ((1, 2, 1, 3),), id="fully_replaced"
        ),
        pytest.param(b"a = 1\nb = 2\n", b"", ((1, 2, 0, 0),), id="emptied"),
        pytest.param(b"", b"a = 1\nb = 2\n", ((0, 0, 1, 2),), id="filled_from_empty"),
    ],
)
def test_modification_is_classified_modified(
    repo: Path,
    use_sha: bool,
    before: bytes,
    after: bytes,
    expected_ranges: tuple[_HunkRange, ...],
) -> None:
    """A file that exists at both ends is ``modified``, whatever its hunk headers say."""
    _write(repo, "pkg/m.py", before)
    base = _commit(repo, "base")
    _write(repo, "pkg/m.py", after)
    head = _commit(repo, "edit")

    file_change = _only_file(_change_set(repo, base, head, use_sha=use_sha))

    assert (file_change.path, file_change.change_kind, file_change.old_path) == (
        "pkg/m.py",
        "modified",
        None,
    )
    assert _hunk_ranges(file_change) == expected_ranges


# ---------------------------------------------------------------------------
# Real additions and deletions
# ---------------------------------------------------------------------------


@_BOTH_PATHS
@pytest.mark.parametrize(
    ("content", "expected_ranges"),
    [
        pytest.param(b"a = 1\nb = 2\n", ((0, 0, 1, 2),), id="with_content"),
        # An empty new file has no ---/+++ lines and no hunks; only git's
        # ``new file mode`` header marks it.
        pytest.param(b"", (), id="empty"),
    ],
)
def test_new_file_is_classified_added(
    repo: Path, use_sha: bool, content: bytes, expected_ranges: tuple[_HunkRange, ...]
) -> None:
    """A file absent at base is ``added``."""
    _write(repo, "README.md", b"v1\n")
    base = _commit(repo, "base")
    _write(repo, "pkg/new.py", content)
    head = _commit(repo, "add")

    file_change = _only_file(_change_set(repo, base, head, use_sha=use_sha))

    assert (file_change.path, file_change.change_kind, file_change.old_path) == (
        "pkg/new.py",
        "added",
        None,
    )
    assert _hunk_ranges(file_change) == expected_ranges


@_BOTH_PATHS
@pytest.mark.parametrize(
    ("content", "expected_ranges"),
    [
        pytest.param(b"a = 1\nb = 2\n", ((1, 2, 0, 0),), id="with_content"),
        # An empty deleted file has no ---/+++ lines and no hunks; only git's
        # ``deleted file mode`` header marks it.
        pytest.param(b"", (), id="empty"),
    ],
)
def test_removed_file_is_classified_deleted(
    repo: Path, use_sha: bool, content: bytes, expected_ranges: tuple[_HunkRange, ...]
) -> None:
    """A file absent at head is ``deleted``."""
    _write(repo, "README.md", b"v1\n")
    _write(repo, "pkg/gone.py", content)
    base = _commit(repo, "base")
    (repo / "pkg" / "gone.py").unlink()
    head = _commit(repo, "delete")

    file_change = _only_file(_change_set(repo, base, head, use_sha=use_sha))

    assert (file_change.path, file_change.change_kind, file_change.old_path) == (
        "pkg/gone.py",
        "deleted",
        None,
    )
    assert _hunk_ranges(file_change) == expected_ranges


# ---------------------------------------------------------------------------
# Renames
# ---------------------------------------------------------------------------


@_BOTH_PATHS
@pytest.mark.parametrize(
    ("after", "expected_ranges"),
    [
        pytest.param(_lines(10), (), id="pure"),
        # The edit removes line 1, so the only hunk is ``@@ -1 +0,0 @@``.
        pytest.param(_lines(10)[len(b"v1 = 1\n") :], ((1, 1, 0, 0),), id="leading_line_removed"),
        pytest.param(
            _lines(10).replace(b"v5 = 5\n", b"v5 = 50\n"), ((5, 1, 5, 1),), id="middle_line_edited"
        ),
    ],
)
def test_rename_is_classified_renamed(
    repo: Path, use_sha: bool, after: bytes, expected_ranges: tuple[_HunkRange, ...]
) -> None:
    """A rename, with or without edits, is ``renamed`` and keeps its old path."""
    _write(repo, "pkg/old_name.py", _lines(10))
    base = _commit(repo, "base")
    (repo / "pkg" / "old_name.py").unlink()
    _write(repo, "pkg/new_name.py", after)
    head = _commit(repo, "rename")

    file_change = _only_file(_change_set(repo, base, head, use_sha=use_sha))

    assert (file_change.path, file_change.change_kind, file_change.old_path) == (
        "pkg/new_name.py",
        "renamed",
        "pkg/old_name.py",
    )
    assert _hunk_ranges(file_change) == expected_ranges


# ---------------------------------------------------------------------------
# Plain unified diffs and the markers on their own
# ---------------------------------------------------------------------------

_STAMP: Final = "\t1970-01-01 00:00:00.000000000 +0000"
"""A ``diff -u`` style timestamp; unidiff strips it from the file name."""


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        pytest.param(
            f"--- /dev/null{_STAMP}\n+++ b/new.py{_STAMP}\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n",
            ("new.py", "added"),
            id="dev_null_source",
        ),
        pytest.param(
            f"--- a/old.py{_STAMP}\n+++ /dev/null{_STAMP}\n@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n",
            ("old.py", "deleted"),
            id="dev_null_target",
        ),
        pytest.param(
            "--- a/m.py\n+++ b/m.py\n@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n",
            ("m.py", "modified"),
            id="leading_lines_removed",
        ),
        pytest.param(
            "--- a/m.py\n+++ b/m.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n",
            ("m.py", "modified"),
            id="top_insertion",
        ),
    ],
)
def test_plain_unified_diff_uses_dev_null_markers(
    tmp_path: Path, diff: str, expected: tuple[str, ChangeKind]
) -> None:
    """A non-git diff is added / deleted only through a ``/dev/null`` side."""
    file_change = _only_file(parse_diff(tmp_path, diff=diff))

    assert (file_change.path, file_change.change_kind, file_change.old_path) == (*expected, None)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(("new file mode 100644\n",), "added", id="new_file_mode"),
        pytest.param(("deleted file mode 100644\n",), "deleted", id="deleted_file_mode"),
        pytest.param(("old mode 100644\n", "new mode 100755\n"), "modified", id="mode_change"),
    ],
)
def test_git_extended_header_alone_decides_added_or_deleted(
    headers: tuple[str, ...], expected: ChangeKind
) -> None:
    """The git header decides even when neither side names ``/dev/null``.

    unidiff's parser rewrites the source or target to ``/dev/null`` when it
    reads these headers. The file is built directly here, so that rewrite never
    happens and only the header is left to decide. A mode change (``new mode``)
    is not a new file.
    """
    patched_file = PatchedFile(
        patch_info=PatchInfo(["diff --git a/m.py b/m.py\n", *headers]),
        source="a/m.py",
        target="b/m.py",
    )

    assert _classify_change_kind(patched_file) == expected


# ---------------------------------------------------------------------------
# Consequence for the Import_Checker (Req 4.10)
# ---------------------------------------------------------------------------


@_BOTH_PATHS
def test_check_imports_treats_leading_lines_removal_as_a_changed_head_file(
    repo: Path, use_sha: bool
) -> None:
    """Removing line 1 leaves an unparseable head, which makes the report incomplete.

    The base parses, so only the head file can mark the report incomplete,
    and it does so only if the change counts as modifying it (Req 4.10). On
    the SHA path, a file classified as deleted was left out of the changed
    head paths, so the report was not incomplete. On the diff-string path the
    base was rebuilt against an empty head instead of the real one.
    """
    _write(repo, "pkg/__init__.py", b"")
    _write(repo, "pkg/mod.py", b"if True:\n    VALUE = 1\n")
    _write(repo, "app.py", b"from pkg.mod import VALUE\n")
    base = _commit(repo, "base")
    _write(repo, "pkg/mod.py", b"    VALUE = 1\n")
    head = _commit(repo, "remove line 1")
    change_set = _change_set(repo, base, head, use_sha=use_sha)
    file_change = _only_file(change_set)
    assert (file_change.change_kind, _hunk_ranges(file_change)) == ("modified", ((1, 1, 0, 0),))

    report = check_imports(change_set, repo)

    assert report == ImportReport(incomplete=True, unparsed_files=["pkg/mod.py"])
