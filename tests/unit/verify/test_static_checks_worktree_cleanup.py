"""Regression test: the static-baseline worktree is removed from any cwd.

On a ``static_baseline`` cache miss, :func:`_resolve_base_keys` checks the
base SHA out with :func:`_add_worktree` and tears it down with
:func:`_remove_worktree` in a ``finally`` block. ``_add_worktree`` ran git
with ``cwd=repo_path``, but ``_remove_worktree`` ran ``git worktree remove``
in the process's working directory. Unless that happened to be the repository,
git looked at the wrong repository (or none), exited 128 ("is not a working
tree" / "not a git repository"), and every cache miss leaked a
``trikon-worktree-*`` directory plus a stale ``.git/worktrees`` entry. The
failure was only logged, so verdicts were unaffected.

The test builds a real git repository under ``tmp_path``, moves the process
cwd to a sibling directory that is not a repository, and runs the real
helpers. Git runs with identity, signing and line-ending settings passed per
command, so the test never depends on global git config. Paths from
``git worktree list`` are resolved and case-normalised before comparing, so
the test behaves the same on Windows.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

from trikon.verify import static_checks

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


def _git(repo: Path, *args: str) -> str:
    """Run ``git *args`` in ``repo`` and return stdout; fail loudly on error."""
    result = subprocess.run(
        ["git", *_GIT_OPTIONS, *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def _normalised(path: str | Path) -> str:
    """Return ``path`` resolved and case-normalised, for cross-platform equality."""
    return os.path.normcase(str(Path(path).resolve()))


def _listed_worktrees(repo: Path) -> list[str]:
    """Return the normalised path of every worktree ``git worktree list`` reports."""
    porcelain = _git(repo, "worktree", "list", "--porcelain")
    prefix = "worktree "
    return [
        _normalised(line[len(prefix) :])
        for line in porcelain.splitlines()
        if line.startswith(prefix)
    ]


def test_remove_worktree_runs_git_in_the_repo_not_the_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worktree added and removed from an unrelated cwd leaves nothing behind."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_bytes(b"x = 1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    worktree_dir = static_checks._add_worktree(repo, base_sha)
    try:
        assert (worktree_dir / "mod.py").is_file()
        assert _listed_worktrees(repo) == [_normalised(repo), _normalised(worktree_dir)]

        with caplog.at_level(logging.WARNING, logger=static_checks.__name__):
            static_checks._remove_worktree(repo, worktree_dir)

        assert not worktree_dir.exists()
        assert _listed_worktrees(repo) == [_normalised(repo)]
        assert [r for r in caplog.records if r.name == static_checks.__name__] == []
    finally:
        # Only does anything when an assertion above failed: do not leak the
        # mkdtemp directory into the system temp dir.
        shutil.rmtree(worktree_dir, ignore_errors=True)
