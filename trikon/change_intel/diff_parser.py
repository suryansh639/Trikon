"""Parse a git diff (or base/head SHAs) into a structured ChangeSet.

Uses gitpython to resolve SHAs against the local repository. Falls back to
parsing a supplied unified-diff string when SHAs are unavailable (e.g., when
called by an agent that only has the raw diff).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Hunk:
    """A single hunk within a file diff."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    added_lines: tuple[int, ...] = field(default_factory=tuple)
    removed_lines: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FileChange:
    """Change to a single file."""

    path: str
    change_kind: str  # "added" | "modified" | "deleted" | "renamed"
    old_path: str | None
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True)
class ChangeSet:
    """The full set of file-level changes between base and head."""

    repo_path: Path
    base_sha: str | None
    head_sha: str | None
    files: tuple[FileChange, ...]

    @property
    def python_files(self) -> tuple[FileChange, ...]:
        return tuple(f for f in self.files if f.path.endswith(".py"))


def parse_diff(
    repo_path: Path,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
) -> ChangeSet:
    """Return a `ChangeSet` for the given repo and revision range or diff string.

    Exactly one of {(base_sha, head_sha), diff} must be provided.
    """
    # TODO: implement. Use gitpython when SHAs are provided; use `unidiff` for raw strings.
    raise NotImplementedError
