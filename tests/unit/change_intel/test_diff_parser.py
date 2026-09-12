"""Unit tests for :func:`trikon.change_intel.diff_parser.parse_diff`.

Covers Task 6.2 in ``.kiro/specs/change-intelligence/tasks.md``. The two
headline properties are:

* **Property 1 — Diff completeness (Validates: Requirements 1.1).**
  For any repo and commit pair, the union of hunk ``added_lines`` /
  ``removed_lines`` equals the added / removed line set that ``git diff
  --unified=0`` reports for that same range.
* **Property 2 — SHA-vs-diff-string equivalence (Validates: Requirements 1.2).**
  Feeding the same underlying diff to :func:`parse_diff` twice — once via the
  ``(base_sha, head_sha)`` path and once via the raw ``diff`` string — yields
  ``ChangeSet.files`` tuples that are equal element-for-element.

The remaining tests pin down the error contract, rename detection,
POSIX-sort determinism, binary-file handling, and the "``diff`` wins when
both arguments are supplied" precedence rule from ``design.md §2.1``.

All git operations run under :func:`subprocess.run` so this test file is
independent of the :mod:`gitpython` import used by production code — the
tests validate ``parse_diff`` behavior end-to-end without borrowing its
plumbing.

_Validates: Requirements 1.1, 1.2, 6.1._
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from trikon.change_intel import (
    ChangeSet,
    DiffInputError,
    DiffParseError,
    FileChange,
    RepoNotFoundError,
    parse_diff,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# subprocess-based git helpers
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git *args`` inside ``repo`` and return the completed process.

    ``check=True`` so misuses in the test setup fail loudly. Output is
    captured as text so a failed assertion can quote it directly.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_diff_text(repo: Path, base: str, head: str) -> str:
    """Return ``git diff --unified=0 --find-renames base head`` verbatim."""
    result = _run_git(repo, "diff", "--unified=0", "--find-renames", base, head)
    return result.stdout


def _commit_all(repo: Path, message: str) -> str:
    """Stage every change under ``repo`` and commit; return the resulting SHA."""
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Fresh git working tree in ``tmp_path`` with a single empty initial commit.

    The empty-init commit gives every test a base revision to diff against
    even when the scenario only introduces new files. The branch name is
    pinned to ``main`` so tests never depend on the local git default.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    _run_git(tmp_path, "config", "user.email", "test@example.com")
    _run_git(tmp_path, "config", "user.name", "Test")
    # Isolate from any global git config that might reject empty commits.
    _run_git(tmp_path, "config", "commit.gpgsign", "false")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Small helpers used by multiple tests
# ---------------------------------------------------------------------------


def _find_change(files: Iterable[FileChange], path: str) -> FileChange:
    """Return the sole :class:`FileChange` in ``files`` whose ``path`` matches.

    Raises :class:`AssertionError` on missing or duplicated entries so a
    failure message points at the offending scenario setup rather than a
    generic ``StopIteration`` from :func:`next`.
    """
    matches = [f for f in files if f.path == path]
    assert len(matches) == 1, f"expected exactly one FileChange for {path!r}, got {matches!r}"
    return matches[0]


def _files_without_sha(cs: ChangeSet) -> tuple[FileChange, ...]:
    """Return ``cs.files`` — Property 2 explicitly ignores ``base_sha`` / ``head_sha``.

    A thin wrapper so the "ignore SHA fields" comparison reads clearly at
    the call site.
    """
    return cs.files


# ---------------------------------------------------------------------------
# Property 1 — diff completeness (Requirements 1.1)
# ---------------------------------------------------------------------------


def test_property1_hunk_line_sets_match_git_diff_added_removed_lines(git_repo: Path) -> None:
    """Union of ``hunk.added_lines`` / ``removed_lines`` equals git's own added/removed sets.

    Set-up: commit a base file, then in a second commit modify it (adds and
    removes distinct lines), add a brand-new file, and delete an existing
    file. We then reconstruct the added/removed line index sets from the raw
    ``git diff --unified=0`` output line-by-line and compare against the
    :class:`ChangeSet` returned by :func:`parse_diff`.

    **Property 1 / Validates: Requirements 1.1.**
    """
    # ---- Base commit: two Python files, one to modify and one to delete.
    (git_repo / "keep.py").write_text(
        "a = 1\nb = 2\nc = 3\nd = 4\n",
        encoding="utf-8",
    )
    (git_repo / "gone.py").write_text("x = 0\n", encoding="utf-8")
    base_sha = _commit_all(git_repo, "base")

    # ---- Head commit: modify keep.py, add fresh.py, delete gone.py.
    (git_repo / "keep.py").write_text(
        # Line 1 unchanged, line 2 replaced, line 3 unchanged, line 4 replaced,
        # plus a new trailing line.
        "a = 1\nB = 22\nc = 3\nD = 44\ne = 5\n",
        encoding="utf-8",
    )
    (git_repo / "fresh.py").write_text("f = 6\ng = 7\n", encoding="utf-8")
    (git_repo / "gone.py").unlink()
    head_sha = _commit_all(git_repo, "head")

    # ---- Parse via the SHA path and index by file.
    cs = parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha)
    assert cs.base_sha == base_sha
    assert cs.head_sha == head_sha
    assert cs.repo_path == git_repo

    paths = {f.path for f in cs.files}
    assert paths == {"keep.py", "fresh.py", "gone.py"}

    # ---- Reconstruct git's own added/removed line index sets per file.
    raw = _git_diff_text(git_repo, base_sha, head_sha)
    git_added, git_removed = _line_index_sets_from_raw_diff(raw)

    # ---- Compare per file. Each FileChange must exactly cover git's set.
    for path in ("keep.py", "fresh.py", "gone.py"):
        fc = _find_change(cs.files, path)
        added_union: set[int] = set()
        removed_union: set[int] = set()
        for h in fc.hunks:
            added_union.update(h.added_lines)
            removed_union.update(h.removed_lines)
        assert added_union == git_added.get(path, set()), (
            f"added-line mismatch on {path}: parse_diff={added_union} git={git_added.get(path)}"
        )
        assert removed_union == git_removed.get(path, set()), (
            f"removed-line mismatch on {path}: parse_diff={removed_union} "
            f"git={git_removed.get(path)}"
        )


def _line_index_sets_from_raw_diff(
    raw_diff: str,
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Return ``(added_by_path, removed_by_path)`` reconstructed from unified-diff text.

    Reads ``--unified=0`` output as a tiny state machine: track the current
    file by preferring the ``b/<path>`` marker and falling back to
    ``a/<path>`` when the target is ``/dev/null`` (deleted file), then track
    pre/post cursors via ``@@ -a,b +c,d @@`` hunk headers. Every ``+`` /
    ``-`` line advances the respective cursor. This is intentionally the
    same accounting the production parser does, but written from scratch
    inside the test so a regression in the parser cannot mask itself here.
    """
    added: dict[str, set[int]] = {}
    removed: dict[str, set[int]] = {}

    current: str | None = None
    source_path: str | None = None
    old_cursor: int = 0
    new_cursor: int = 0

    for line in raw_diff.splitlines():
        if line.startswith("diff "):
            current = None
            source_path = None
            continue
        if line.startswith("--- "):
            token = line[4:]
            if token == "/dev/null":
                source_path = None
            else:
                source_path = token[2:] if token.startswith("a/") else token
            continue
        if line.startswith("+++ "):
            token = line[4:]
            if token == "/dev/null":
                # Deleted file: pull the path from the ``--- a/<path>`` side.
                current = source_path
            else:
                current = token[2:] if token.startswith("b/") else token
            continue
        if line.startswith("@@"):
            # @@ -old_start[,old_len] +new_start[,new_len] @@
            spec = line.split("@@")[1].strip()
            old_part, new_part = spec.split(" ")
            old_start = int(old_part.lstrip("-").split(",")[0])
            new_start = int(new_part.lstrip("+").split(",")[0])
            old_cursor = old_start
            new_cursor = new_start
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, set()).add(new_cursor)
            new_cursor += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed.setdefault(current, set()).add(old_cursor)
            old_cursor += 1
        # ``--unified=0`` emits no context lines, so we ignore anything else.

    return added, removed


# ---------------------------------------------------------------------------
# Property 2 — SHA vs diff-string equivalence (Requirements 1.2)
# ---------------------------------------------------------------------------


def test_property2_sha_and_diff_string_paths_return_equal_files(git_repo: Path) -> None:
    """The SHA path and diff-string path produce equal ``ChangeSet.files`` tuples.

    We compare element-for-element ignoring ``base_sha`` / ``head_sha`` (the
    diff-string path has both set to ``None`` by contract).

    **Property 2 / Validates: Requirements 1.2.**
    """
    # ---- Two-commit scenario: one modified, one added.
    (git_repo / "alpha.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    base_sha = _commit_all(git_repo, "base")

    (git_repo / "alpha.py").write_text("x = 1\ny = 42\nz = 3\n", encoding="utf-8")
    (git_repo / "beta.py").write_text("q = 9\n", encoding="utf-8")
    head_sha = _commit_all(git_repo, "head")

    diff_text = _git_diff_text(git_repo, base_sha, head_sha)

    cs_sha = parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha)
    cs_str = parse_diff(git_repo, diff=diff_text)

    # Files tuple must match exactly. Order is enforced by parse_diff itself
    # (POSIX-sorted), so equality is a direct comparison.
    assert _files_without_sha(cs_sha) == _files_without_sha(cs_str)

    # The diff-string path leaves the SHA fields as ``None``.
    assert cs_str.base_sha is None
    assert cs_str.head_sha is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_no_args_raises_diff_input_error(git_repo: Path) -> None:
    """Calling ``parse_diff`` with neither SHAs nor a diff raises :class:`DiffInputError`."""
    with pytest.raises(DiffInputError):
        parse_diff(git_repo)


def test_only_base_sha_raises_diff_input_error(git_repo: Path) -> None:
    """Half of a SHA range is a shape violation and must fail closed."""
    with pytest.raises(DiffInputError):
        parse_diff(git_repo, base_sha="deadbeef")


def test_only_head_sha_raises_diff_input_error(git_repo: Path) -> None:
    """Same for the other half — symmetry, not a happy accident."""
    with pytest.raises(DiffInputError):
        parse_diff(git_repo, head_sha="deadbeef")


def test_max_diff_bytes_ceiling_enforced(git_repo: Path) -> None:
    """A diff larger than ``max_diff_bytes`` must raise :class:`DiffInputError`.

    We construct a ~100-byte diff string and set ``max_diff_bytes=10``; the
    exact contents don't matter because the size check runs before parsing.
    """
    big_diff = "diff --git a/foo b/foo\n" + ("x" * 100)
    with pytest.raises(DiffInputError):
        parse_diff(git_repo, diff=big_diff, max_diff_bytes=10)


def test_non_git_directory_raises_repo_not_found_error(tmp_path: Path) -> None:
    """A directory that has no ``.git`` entry cannot serve the SHA path."""
    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()
    with pytest.raises(RepoNotFoundError):
        parse_diff(non_repo, base_sha="deadbeef", head_sha="cafebabe")


def test_unknown_sha_raises_repo_not_found_error(git_repo: Path) -> None:
    """A well-formed but unresolvable SHA also raises :class:`RepoNotFoundError`.

    Rationale: ``design.md §2.1`` maps every ``git diff`` failure inside a
    real repository (missing SHA, ambiguous ref) onto :class:`RepoNotFoundError`
    so callers only need to catch one error type on the resolution path.
    """
    with pytest.raises(RepoNotFoundError):
        parse_diff(git_repo, base_sha="0" * 40, head_sha="1" * 40)


def test_malformed_diff_string_raises_diff_parse_error(git_repo: Path) -> None:
    """A diff with a valid header but a corrupted hunk body must raise :class:`DiffParseError`.

    ``unidiff`` is lenient with pure garbage input (see
    :func:`test_garbage_diff_string_returns_empty_change_set` below) but a
    real ``@@`` hunk header followed by content that lacks a ``+``/``-``/`` ``
    marker triggers ``UnidiffParseError`` — which the parser must translate
    into :class:`DiffParseError` at the module boundary.
    """
    broken = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "line-without-a-diff-marker\n"
    )
    with pytest.raises(DiffParseError):
        parse_diff(git_repo, diff=broken)


def test_garbage_diff_string_returns_empty_change_set(git_repo: Path) -> None:
    """Pure garbage input yields an empty ``ChangeSet.files`` — ``unidiff`` is lenient here.

    Documented nuance from the ``unidiff`` upstream: strings that never look
    like a diff header at all are silently discarded rather than rejected.
    ``parse_diff`` inherits that behavior. If the upstream tightens its parser
    later, this test will start failing and can be flipped over to a
    ``pytest.raises(DiffParseError)`` guard.
    """
    cs = parse_diff(git_repo, diff="not a valid diff\ngarbage\n")
    assert cs.files == ()
    assert cs.base_sha is None
    assert cs.head_sha is None


# ---------------------------------------------------------------------------
# Rename detection
# ---------------------------------------------------------------------------


def test_rename_detected_as_single_file_change_with_old_path(git_repo: Path) -> None:
    """A ``git mv`` between commits surfaces as one ``renamed`` :class:`FileChange`.

    ``old_path`` carries the pre-rename POSIX path, ``path`` carries the new
    one. Both must have the ``a/`` / ``b/`` prefixes stripped.
    """
    (git_repo / "foo.py").write_text(
        "def hello() -> str:\n    return 'hi'\n",
        encoding="utf-8",
    )
    base_sha = _commit_all(git_repo, "base")

    _run_git(git_repo, "mv", "foo.py", "bar.py")
    head_sha = _commit_all(git_repo, "rename foo -> bar")

    cs = parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha)
    assert len(cs.files) == 1

    fc = cs.files[0]
    assert fc.change_kind == "renamed"
    assert fc.old_path == "foo.py"
    assert fc.path == "bar.py"


# ---------------------------------------------------------------------------
# POSIX-sort determinism
# ---------------------------------------------------------------------------


def test_change_set_files_are_posix_sorted(git_repo: Path) -> None:
    """``ChangeSet.files`` is POSIX-lex sorted regardless of author order.

    Three files added in a deliberately non-alphabetical filesystem order
    must appear as ``[aaa.py, mmm.py, zzz.py]`` on the way out.
    """
    base_sha = _run_git(git_repo, "rev-parse", "HEAD").stdout.strip()

    # Create in reverse-lex order to make sure the sort actually runs.
    (git_repo / "zzz.py").write_text("z = 1\n", encoding="utf-8")
    (git_repo / "aaa.py").write_text("a = 1\n", encoding="utf-8")
    (git_repo / "mmm.py").write_text("m = 1\n", encoding="utf-8")
    head_sha = _commit_all(git_repo, "add three")

    cs = parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha)
    assert [f.path for f in cs.files] == ["aaa.py", "mmm.py", "zzz.py"]


# ---------------------------------------------------------------------------
# Binary file handling
# ---------------------------------------------------------------------------


def test_binary_file_included_with_no_hunks(git_repo: Path) -> None:
    """A binary blob still appears in ``ChangeSet.files`` — with zero hunks.

    ``design.md §Data Models`` calls this out explicitly: binary files count
    as changed files (so callers see them) but carry no line-level hunks
    (the AST pipeline filters them out via ``ChangeSet.python_files``). This
    test also confirms :func:`parse_diff` does not crash on non-UTF-8 bytes.
    """
    (git_repo / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe\xfd\x00")
    head_sha = _commit_all(git_repo, "add binary")

    base_sha = _run_git(git_repo, "rev-parse", "HEAD~1").stdout.strip()
    cs = parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha)

    fc = _find_change(cs.files, "data.bin")
    assert fc.change_kind == "added"
    assert fc.hunks == ()
    # A binary-only change should never end up in ``python_files``.
    assert cs.python_files == ()


# ---------------------------------------------------------------------------
# "Both args" precedence
# ---------------------------------------------------------------------------


def test_diff_string_wins_when_both_arguments_supplied(git_repo: Path) -> None:
    """Passing both ``diff`` and SHAs takes the diff-string path.

    Per ``design.md §2.1``: agent-supplied diffs are authoritative, so
    ``diff`` wins over ``(base_sha, head_sha)``. The observable proof is that
    the resulting ``ChangeSet`` has ``base_sha`` and ``head_sha`` set to
    ``None`` (the diff-string outcome) rather than echoing the caller's SHAs.
    """
    # Build a valid on-disk change so both paths could resolve if we let them.
    (git_repo / "one.py").write_text("v = 1\n", encoding="utf-8")
    base_sha = _commit_all(git_repo, "base")
    (git_repo / "one.py").write_text("v = 2\n", encoding="utf-8")
    head_sha = _commit_all(git_repo, "head")
    diff_text = _git_diff_text(git_repo, base_sha, head_sha)

    cs = parse_diff(
        git_repo,
        base_sha=base_sha,
        head_sha=head_sha,
        diff=diff_text,
    )

    # ``diff`` won: SHA fields on the returned ChangeSet are ``None``.
    assert cs.base_sha is None
    assert cs.head_sha is None
    # ...and the content still matches what the SHA path would have produced.
    assert cs.files == parse_diff(git_repo, base_sha=base_sha, head_sha=head_sha).files
