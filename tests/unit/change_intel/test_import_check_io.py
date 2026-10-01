"""Unit tests for the Import_Checker I/O shell, :func:`check_imports`.

Covers Task 7.2 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``. Each
scenario builds a real git repository under ``tmp_path``, turns the commit
range into a :class:`~trikon.change_intel.models.ChangeSet` with the real
:func:`~trikon.change_intel.parse_diff`, and runs :func:`check_imports`
against the working tree. Most scenarios run twice:

* ``base_sha``: the SHA path, so the base content comes from ``git show``
  (Req 3.2);
* ``hunk_reversal``: the same change as a raw diff string, so the base
  content is rebuilt by reversing the hunks on the head text (Req 3.3).

The remaining tests pin down the walk rules (pruned VCS and virtual
environment directories, symlinks, unreadable files), the per-file failures
that are recorded instead of raised, and the raise sites that must surface as
:class:`~trikon.change_intel.errors.ImportCheckError` chained to their cause
(Req 7.2). ``subprocess.run`` is monkeypatched only to simulate a missing git
or a git timeout, and to spy on the ``git show`` calls a real run makes.

Git runs with identity, signing and line-ending settings passed per command,
so the tests never depend on (or touch) global git config. Files are written
as bytes with explicit ``\\n`` terminators and every expected path is POSIX,
so the tests behave the same on Windows.

_Validates: Requirements 3.2, 3.3, 7.2, 10.6._
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import IO, Final, NoReturn

import pytest

from trikon.change_intel import (
    ChangeSet,
    FileChange,
    Hunk,
    ImportCheckError,
    import_check_io,
    parse_diff,
)
from trikon.change_intel.import_check_io import check_imports
from trikon.evidence.report import BrokenImport, ImportReport

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

_TIMEOUT: Final = 7.5
"""A non-default ``git_timeout_seconds``, so tests can see it reach ``git show``."""

_MODIFY_DIFF: Final = (
    "diff --git a/mod.py b/mod.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/mod.py\n"
    "+++ b/mod.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)
"""A one-line Python change for tests that need no git history."""

_SHA_MODES: Final = pytest.mark.parametrize(
    "use_sha", [True, False], ids=["base_sha", "hunk_reversal"]
)


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


def _py(*lines: str) -> bytes:
    """Return ``lines`` joined with explicit ``\\n`` terminators, UTF-8 encoded."""
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _commit(repo: Path, message: str) -> str:
    """Stage everything in ``repo``, commit, and return the new HEAD SHA."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _change_set(repo: Path, base: str, head: str, *, use_sha: bool) -> ChangeSet:
    """Return the ``base..head`` ChangeSet via the SHA path or the diff-string path."""
    if use_sha:
        change_set = parse_diff(repo, base_sha=base, head_sha=head)
    else:
        diff = _git(repo, "diff", "--no-color", "--no-ext-diff", "--find-renames", base, head)
        change_set = parse_diff(repo, diff=diff)
    assert (change_set.base_sha is not None) is use_sha
    return change_set


def _removed_module(path: str, module: str, name: str | None = None) -> BrokenImport:
    """Return a line-1 ``removed_module`` record."""
    return BrokenImport(path=path, line=1, module=module, name=name, kind="removed_module")


@dataclass(frozen=True)
class _GitCall:
    """One ``subprocess.run`` call made by :func:`check_imports`."""

    argv: tuple[str, ...]
    cwd: str
    timeout: float


def _spy_on_git(monkeypatch: pytest.MonkeyPatch) -> list[_GitCall]:
    """Record every ``subprocess.run`` call and pass it through to the real one.

    The keyword-only signature mirrors the exact call ``_git_show`` makes, so
    a drift in that contract fails the test with a ``TypeError``. Install the
    spy only after all git setup is done.
    """
    calls: list[_GitCall] = []
    real_run = subprocess.run

    def spy(
        args: list[str],
        *,
        cwd: str,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(_GitCall(argv=tuple(args), cwd=cwd, timeout=timeout))
        return real_run(args, cwd=cwd, capture_output=capture_output, timeout=timeout, check=check)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def _forbid_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``subprocess.run`` call fail the test."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"git must not run here: {args!r}")

    monkeypatch.setattr(subprocess, "run", forbidden)


def _python_change_without_history(root: Path) -> ChangeSet:
    """Return a ChangeSet for ``_MODIFY_DIFF`` with ``mod.py`` at its head content."""
    _write(root, "mod.py", _py("x = 2"))
    return parse_diff(root, diff=_MODIFY_DIFF)


# ---------------------------------------------------------------------------
# Scenario builders (each returns ``(base_sha, head_sha)``)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty git repository at ``tmp_path / "repo"``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


_UTIL_DOCSTRING: Final = '"""Utilities."""'
"""Line 1 of ``pkg/util.py``; the edit below leaves it unchanged.

A removal starting at line 1 (a lone ``@@ -1,N +0,0 @@`` hunk under
``--unified=0``) is also a ``modified`` change, because ``parse_diff`` takes
added / deleted only from ``/dev/null`` and git's new / deleted file headers.
``test_diff_parser_change_kind.py`` covers that case, including its effect on
:func:`check_imports`.
"""


def _build_removed_name(repo: Path) -> tuple[str, str]:
    """``pkg/util.py`` loses ``helper``; the change also adds a file and edits a README."""
    _write(repo, "pkg/__init__.py", b"")
    _write(
        repo,
        "pkg/util.py",
        _py(
            _UTIL_DOCSTRING,
            "",
            "",
            "def helper():",
            "    return 1",
            "",
            "",
            "def other():",
            "    return 2",
        ),
    )
    _write(
        repo,
        "app.py",
        _py("from pkg.util import helper, other", "", "", "def run():", "    return helper()"),
    )
    _write(repo, "README.md", b"v1\n")
    base = _commit(repo, "base")
    _write(repo, "pkg/util.py", _py(_UTIL_DOCSTRING, "", "", "def other():", "    return 2"))
    _write(repo, "pkg/extra.py", _py("EXTRA = 1"))
    _write(repo, "README.md", b"v2\n")
    return base, _commit(repo, "remove helper")


def _removed_name_change(repo: Path, *, use_sha: bool) -> tuple[str, ChangeSet]:
    """Build :func:`_build_removed_name` and return ``(base_sha, change_set)``."""
    base, head = _build_removed_name(repo)
    change_set = _change_set(repo, base, head, use_sha=use_sha)
    kinds = {file_change.path: file_change.change_kind for file_change in change_set.files}
    assert kinds == {"README.md": "modified", "pkg/extra.py": "added", "pkg/util.py": "modified"}
    return base, change_set


def _build_deleted_module(repo: Path) -> tuple[str, str]:
    """``pkg/old.py`` is deleted while ``app.py`` still imports it."""
    _write(repo, "pkg/__init__.py", b"")
    _write(repo, "pkg/old.py", _py("def legacy():", "    return 1"))
    _write(
        repo, "app.py", _py("import pkg.old", "", "", "def run():", "    return pkg.old.legacy()")
    )
    base = _commit(repo, "base")
    (repo / "pkg" / "old.py").unlink()
    return base, _commit(repo, "delete pkg/old.py")


# ---------------------------------------------------------------------------
# Early exit
# ---------------------------------------------------------------------------


def test_non_python_change_returns_empty_report_without_disk_or_git(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change with no Python_File is ``ImportReport()``, even for a missing repo path."""
    _write(repo, "README.md", b"v1\n")
    _write(repo, "docs/guide.txt", b"one\n")
    _write(repo, "app.py", _py("import missing_module"))
    base = _commit(repo, "base")
    _write(repo, "README.md", b"v2\n")
    (repo / "docs" / "guide.txt").unlink()
    head = _commit(repo, "docs only")
    change_set = _change_set(repo, base, head, use_sha=True)
    assert {file_change.path for file_change in change_set.files} == {
        "README.md",
        "docs/guide.txt",
    }
    _forbid_git(monkeypatch)

    assert check_imports(change_set, repo) == ImportReport()
    # The early exit happens before the head walk, so a missing path is fine.
    assert check_imports(change_set, tmp_path / "does-not-exist") == ImportReport()


# ---------------------------------------------------------------------------
# Base content: git show (Req 3.2) and hunk reversal (Req 3.3)
# ---------------------------------------------------------------------------


@_SHA_MODES
def test_modified_file_base_content_yields_removed_name(
    repo: Path, monkeypatch: pytest.MonkeyPatch, use_sha: bool
) -> None:
    """The base of a modified file comes from ``git show`` or from reversing its hunks.

    Only the modified Python file is read at base: the added file has no base
    and the README is not a Python_File. The hunk-reversal path never runs git.
    """
    base, change_set = _removed_name_change(repo, use_sha=use_sha)
    calls = _spy_on_git(monkeypatch)

    report = check_imports(change_set, repo, git_timeout_seconds=_TIMEOUT)

    assert report == ImportReport(
        broken=[
            BrokenImport(
                path="app.py", line=1, module="pkg.util", name="helper", kind="removed_name"
            )
        ]
    )
    expected_calls = (
        [_GitCall(argv=("git", "show", f"{base}:pkg/util.py"), cwd=str(repo), timeout=_TIMEOUT)]
        if use_sha
        else []
    )
    assert calls == expected_calls


@_SHA_MODES
def test_deleted_module_base_content_is_obtained(repo: Path, use_sha: bool) -> None:
    """A deleted file's base comes from ``git show`` or from reversing against an empty head."""
    base, head = _build_deleted_module(repo)
    change_set = _change_set(repo, base, head, use_sha=use_sha)
    (deleted,) = change_set.files
    assert (deleted.path, deleted.change_kind) == ("pkg/old.py", "deleted")

    report = check_imports(change_set, repo)

    # ``incomplete`` stays False only if the deleted file's base content was
    # obtained and parsed.
    assert report == ImportReport(broken=[_removed_module("app.py", "pkg.old")])


@_SHA_MODES
def test_pure_rename_base_content_is_obtained(repo: Path, use_sha: bool) -> None:
    """A rename with no hunks keeps the head bytes as the base of ``old_path``."""
    _write(repo, "pkg/__init__.py", b"")
    _write(repo, "pkg/old_name.py", _py("VALUE = 1", "OTHER = 2"))
    _write(repo, "app.py", _py("from pkg.old_name import VALUE"))
    base = _commit(repo, "base")
    (repo / "pkg" / "old_name.py").rename(repo / "pkg" / "new_name.py")
    head = _commit(repo, "rename")
    change_set = _change_set(repo, base, head, use_sha=use_sha)
    assert change_set.files == (
        FileChange(
            path="pkg/new_name.py", change_kind="renamed", old_path="pkg/old_name.py", hunks=()
        ),
    )

    report = check_imports(change_set, repo)

    assert report == ImportReport(broken=[_removed_module("app.py", "pkg.old_name", "VALUE")])


def test_rename_out_of_python_still_counts_as_python_change(repo: Path) -> None:
    """A ``.py`` old path alone makes it a Python change; the base comes from ``git show``."""
    _write(repo, "legacy.py", _py("X = 1", "Y = 2"))
    _write(repo, "app.py", _py("import legacy"))
    base = _commit(repo, "base")
    (repo / "legacy.py").rename(repo / "legacy.txt")
    head = _commit(repo, "rename to text")
    change_set = _change_set(repo, base, head, use_sha=True)
    assert change_set.files == (
        FileChange(path="legacy.txt", change_kind="renamed", old_path="legacy.py", hunks=()),
    )

    report = check_imports(change_set, repo)

    assert report == ImportReport(broken=[_removed_module("app.py", "legacy")])


def test_non_utf8_bytes_outside_hunks_round_trip(repo: Path) -> None:
    """Hunk reversal keeps a latin-1 byte intact, so the coding cookie still applies.

    ``caf\\xe9`` is an identifier ending in e-acute under the cookie. A lossy
    decode would turn it into bytes that no longer form an identifier, and the
    base would land in ``unparsed_files``.
    """
    body = b"A = 1\nB = 2\nC = 3\nD = 4\n"
    cookie = b"# -*- coding: latin-1 -*-\ncaf\xe9 = 1\n"
    _write(repo, "pkg/__init__.py", b"")
    _write(repo, "pkg/strings.py", cookie + body + b"\n\ndef helper():\n    return A\n")
    _write(repo, "app.py", _py("from pkg.strings import helper"))
    base = _commit(repo, "base")
    _write(repo, "pkg/strings.py", cookie + body)
    head = _commit(repo, "remove helper")
    change_set = _change_set(repo, base, head, use_sha=False)

    report = check_imports(change_set, repo)

    assert report == ImportReport(
        broken=[
            BrokenImport(
                path="app.py", line=1, module="pkg.strings", name="helper", kind="removed_name"
            )
        ]
    )


# ---------------------------------------------------------------------------
# Unobtainable base content is recorded, not raised (Req 3.7, 7.2)
# ---------------------------------------------------------------------------


def test_git_show_failure_marks_base_unobtainable(repo: Path) -> None:
    """A non-zero ``git show`` exit gives ``None`` base content, not an exception."""
    _, change_set = _removed_name_change(repo, use_sha=True)
    change_set = replace(change_set, base_sha="0" * 40)

    report = check_imports(change_set, repo)

    assert report == ImportReport(incomplete=True, unparsed_files=["pkg/util.py"])


@pytest.mark.parametrize("drift", ["edited", "missing"])
def test_hunks_that_do_not_match_head_mark_base_unobtainable(repo: Path, drift: str) -> None:
    """Without a base SHA, a working tree that no longer matches the diff gives ``None``."""
    _, change_set = _removed_name_change(repo, use_sha=False)
    if drift == "edited":
        _write(repo, "pkg/util.py", _py("def other():", "    return 3"))
    else:
        (repo / "pkg" / "util.py").unlink()

    report = check_imports(change_set, repo)

    assert report.incomplete is True
    assert report.unparsed_files == ["pkg/util.py"]


def test_lone_surrogate_in_hunk_text_marks_base_unobtainable(tmp_path: Path) -> None:
    """Restored text that cannot be encoded back to bytes gives ``None`` base content.

    ``parse_diff`` cannot produce this hunk from a string (its size check
    encodes strictly), so the FileChange is built directly.
    """
    _write(tmp_path, "m.py", _py("x = 1"))
    hunk = Hunk(
        old_start=1,
        old_lines=1,
        new_start=1,
        new_lines=1,
        added_lines=(1,),
        removed_lines=(1,),
        source_lines=("x = '\ud800'",),
        target_lines=("x = 1",),
    )
    change_set = ChangeSet(
        repo_path=tmp_path,
        base_sha=None,
        head_sha=None,
        files=(FileChange(path="m.py", change_kind="modified", old_path=None, hunks=(hunk,)),),
    )

    report = check_imports(change_set, tmp_path)

    assert report == ImportReport(incomplete=True, unparsed_files=["m.py"])


# ---------------------------------------------------------------------------
# Head tree walk
# ---------------------------------------------------------------------------


def test_excluded_directories_are_not_part_of_head_tree(repo: Path) -> None:
    """VCS metadata, virtual environments and ``__pycache__`` are pruned at any depth.

    Every pruned directory holds an importer of the deleted module and an
    unparseable file; either would show up in the report if it were walked.
    The ``tools/`` copies are the control.
    """
    base, head = _build_deleted_module(repo)
    importer = _py("import pkg.old")
    unparseable = _py("def broken(:")
    pruned = (".git", ".hg", ".svn", ".venv", "venv", "pkg/__pycache__", "tools/.venv")
    for directory in pruned:
        _write(repo, f"{directory}/importer.py", importer)
        _write(repo, f"{directory}/broken.py", unparseable)
    _write(repo, "envs/py312/pyvenv.cfg", b"home = /usr/bin\n")
    _write(repo, "envs/py312/lib/importer.py", importer)
    _write(repo, "envs/py312/lib/broken.py", unparseable)
    _write(repo, "tools/importer.py", importer)
    _write(repo, "tools/broken.py", unparseable)
    change_set = _change_set(repo, base, head, use_sha=True)

    report = check_imports(change_set, repo)

    assert report == ImportReport(
        broken=[
            _removed_module("app.py", "pkg.old"),
            _removed_module("tools/importer.py", "pkg.old"),
        ],
        unparsed_files=["tools/broken.py"],
    )


def test_symlinked_python_file_is_listed_unparsed_and_not_followed(repo: Path) -> None:
    """A symlinked ``.py`` entry gets ``None`` content instead of its target's bytes."""
    base, head = _build_deleted_module(repo)
    try:
        os.symlink("app.py", repo / "linked.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are not permitted here: {exc}")
    change_set = _change_set(repo, base, head, use_sha=True)

    report = check_imports(change_set, repo)

    # Following the link would add a second broken import for linked.py.
    assert report == ImportReport(
        broken=[_removed_module("app.py", "pkg.old")],
        unparsed_files=["linked.py"],
    )


def test_unreadable_head_file_is_recorded_not_raised(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-file read error gives ``None``; a changed file then makes the report incomplete."""
    _, change_set = _removed_name_change(repo, use_sha=True)
    real_open = open

    def failing_open(file: str, mode: str = "r") -> IO[bytes]:
        if PurePath(file).as_posix().endswith("/pkg/util.py"):
            raise PermissionError(13, "Permission denied", file)
        handle: IO[bytes] = real_open(file, mode)
        return handle

    # Shadow the builtin inside the module under test only.
    monkeypatch.setattr(import_check_io, "open", failing_open, raising=False)

    report = check_imports(change_set, repo)

    assert report == ImportReport(incomplete=True, unparsed_files=["pkg/util.py"])


# ---------------------------------------------------------------------------
# Raise sites: only ImportCheckError escapes, chained to its cause (Req 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(2, "No such file or directory", "git"), "git not found on PATH"),
        (subprocess.TimeoutExpired(cmd=["git", "show"], timeout=_TIMEOUT), "timed out after 7.5s"),
        (PermissionError(13, "Permission denied", "git"), "OS error running git show"),
    ],
    ids=["git_missing", "git_timeout", "spawn_os_error"],
)
def test_git_spawn_failure_raises_chained_import_check_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError | subprocess.TimeoutExpired,
    message: str,
) -> None:
    """Git that cannot be spawned or times out raises, with the timeout passed through."""
    change_set = replace(_python_change_without_history(tmp_path), base_sha="abc1234")
    seen_timeouts: list[float] = []

    def failing_run(
        args: list[str],
        *,
        cwd: str,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> NoReturn:
        seen_timeouts.append(timeout)
        raise failure

    monkeypatch.setattr(subprocess, "run", failing_run)

    with pytest.raises(ImportCheckError, match=re.escape(message)) as excinfo:
        check_imports(change_set, tmp_path, git_timeout_seconds=_TIMEOUT)

    assert excinfo.value.__cause__ is failure
    assert seen_timeouts == [_TIMEOUT]


@pytest.mark.parametrize("base_sha", ["", "-", "--output=pwned.txt"])
def test_unsafe_base_sha_is_refused_before_git_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_sha: str
) -> None:
    """An empty SHA or one that git would read as an option is refused."""
    change_set = replace(_python_change_without_history(tmp_path), base_sha=base_sha)
    _forbid_git(monkeypatch)

    with pytest.raises(ImportCheckError, match="refusing base SHA"):
        check_imports(change_set, tmp_path)


@pytest.mark.parametrize("target", ["missing", "not_a_directory"])
def test_unwalkable_repo_path_raises_chained_import_check_error(
    tmp_path: Path, target: str
) -> None:
    """A head tree that cannot be walked raises instead of yielding a partial tree."""
    change_set = _python_change_without_history(tmp_path)
    repo_path = tmp_path / "repo"
    if target == "not_a_directory":
        repo_path.write_bytes(b"")

    with pytest.raises(ImportCheckError, match="cannot walk the head tree") as excinfo:
        check_imports(change_set, repo_path)

    assert isinstance(excinfo.value.__cause__, OSError)
