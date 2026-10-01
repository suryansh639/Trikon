"""Parse a git diff (or a ``(base_sha, head_sha)`` range) into a structured :class:`ChangeSet`.

This is stage one of the Change Intelligence pipeline. Every downstream stage
(AST indexer, symbol resolver, dep graph, blast radius) consumes the
:class:`ChangeSet` produced here; if this module fails, the SDK boundary
translates the failure into a ``require_human`` verdict.

Two callable paths are supported by :func:`parse_diff`:

* **SHA path.** Callers pass ``base_sha`` and ``head_sha`` and Change
  Intelligence shells out to ``git diff --unified=0 --find-renames`` via
  :mod:`gitpython`. This is the human/CI workflow.
* **Diff-string path.** Callers pass a raw unified-diff blob. This is the
  agent workflow where the caller already holds the diff and does not need to
  hit the working tree. When both ``diff`` and SHAs are supplied, ``diff``
  wins (the agent knows what it changed).

Both paths converge on :mod:`unidiff` and produce a POSIX-path-sorted
:class:`ChangeSet`; identical inputs therefore produce byte-identical JSON,
which the callers of ``ImpactSet.model_dump_json`` depend on for reproducible
audits.

See ``design.md §2.1`` and ``requirements.md §1``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import git
import unidiff

from trikon.change_intel.errors import (
    DiffInputError,
    DiffParseError,
    RepoNotFoundError,
)
from trikon.change_intel.models import (
    ChangeKind,
    ChangeSet,
    FileChange,
    Hunk,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# The frozen dataclasses (Hunk, FileChange, ChangeSet, ChangeKind) live in
# ``trikon.change_intel.models`` as the authoritative source. They are
# re-exported here so existing imports (``from trikon.change_intel.diff_parser
# import ChangeSet``) keep working. Adding them to ``__all__`` also satisfies
# mypy ``--strict``'s ``implicit_reexport = False`` for downstream callers.
__all__ = [
    "ChangeKind",
    "ChangeSet",
    "FileChange",
    "Hunk",
    "parse_diff",
]


_DEFAULT_MAX_DIFF_BYTES: int = 50 * 1024 * 1024
"""50 MiB. Any diff larger than this is treated as an input-shape failure.

Rationale: the whole diff must fit in memory during a single :func:`parse_diff`
call, and ``unidiff`` allocates roughly ``2x`` the diff size while parsing.
100 MiB peak is the largest allocation we let this stage make.
"""

_DEV_NULL: str = "/dev/null"
"""The path a unified diff names for the missing side of an added or deleted file."""

_GIT_NEW_FILE_HEADER: str = "new file mode "
"""Prefix of git's extended header line that marks an added file."""

_GIT_DELETED_FILE_HEADER: str = "deleted file mode "
"""Prefix of git's extended header line that marks a deleted file."""


def parse_diff(
    repo_path: Path,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
    *,
    max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
) -> ChangeSet:
    """Return a :class:`ChangeSet` for ``repo_path`` at the given revision range or raw diff.

    Exactly one of ``(base_sha, head_sha)`` or ``diff`` must be supplied. When
    both are supplied, ``diff`` wins — the agent-driven path takes priority
    over the working tree, because the agent's diff is authoritative for what
    it just proposed. Missing both raises :class:`DiffInputError`.

    Parameters
    ----------
    repo_path:
        Path to the git working tree. Recorded on the :class:`ChangeSet` so
        downstream stages can resolve relative file paths. On the SHA path
        this must be a real git repository; on the diff-string path it can be
        any :class:`~pathlib.Path` because the working tree is not touched.
    base_sha, head_sha:
        Revision endpoints for the SHA path. Both required together — passing
        only one raises :class:`DiffInputError`.
    diff:
        Raw unified-diff string. Overrides the SHA path when supplied.
    max_diff_bytes:
        Size ceiling on the diff payload (UTF-8-encoded). Diffs larger than
        this raise :class:`DiffInputError` before any parsing runs.

    Returns
    -------
    ChangeSet
        POSIX-sorted, immutable, hashable. Every included file's hunks carry
        1-based line indices consistent with ``git diff --unified=0``.

    Raises
    ------
    DiffInputError
        Neither ``(base_sha, head_sha)`` nor ``diff`` was supplied; only one
        of ``base_sha`` / ``head_sha`` was supplied; or the diff exceeds
        ``max_diff_bytes``.
    RepoNotFoundError
        ``repo_path`` is not a git repository, or the SHAs cannot be resolved
        by the repository.
    DiffParseError
        The diff payload is malformed and :mod:`unidiff` cannot parse it.
    """
    diff_source, base_out, head_out = _select_diff_source(
        repo_path=repo_path,
        base_sha=base_sha,
        head_sha=head_sha,
        diff=diff,
        max_diff_bytes=max_diff_bytes,
    )

    patch_set = _parse_patch_set(diff_source)
    files = _build_file_changes(patch_set)

    return ChangeSet(
        repo_path=repo_path,
        base_sha=base_out,
        head_sha=head_out,
        files=files,
    )


# ---------------------------------------------------------------------------
# Diff acquisition
# ---------------------------------------------------------------------------


def _select_diff_source(
    *,
    repo_path: Path,
    base_sha: str | None,
    head_sha: str | None,
    diff: str | None,
    max_diff_bytes: int,
) -> tuple[str, str | None, str | None]:
    """Resolve the caller's arguments into a concrete unified-diff string.

    Returns a triple ``(diff_text, base_sha_out, head_sha_out)`` where the two
    trailing SHAs are ``None`` on the diff-string path and echo the caller's
    inputs on the SHA path. Enforces the "exactly one of" contract and the
    size ceiling before touching :mod:`git`.
    """
    if diff is not None:
        _enforce_size_ceiling(diff, max_diff_bytes=max_diff_bytes)
        return diff, None, None

    if base_sha is None or head_sha is None:
        raise DiffInputError(
            "parse_diff requires exactly one of (base_sha, head_sha) or diff; "
            f"got base_sha={base_sha!r}, head_sha={head_sha!r}, diff=None."
        )

    diff_text = _git_diff(repo_path, base_sha, head_sha)
    _enforce_size_ceiling(diff_text, max_diff_bytes=max_diff_bytes)
    return diff_text, base_sha, head_sha


def _git_diff(repo_path: Path, base_sha: str, head_sha: str) -> str:
    """Run ``git diff --unified=0 --find-renames <base>..<head>`` against ``repo_path``.

    :mod:`gitpython`'s exception vocabulary is translated into
    :class:`RepoNotFoundError`; the caller never sees a
    :class:`git.exc.GitError`. ``--unified=0`` collapses context lines so the
    added / removed line sets we build in :func:`_line_index_sets` are exact.
    """
    try:
        repo = git.Repo(str(repo_path))
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
        raise RepoNotFoundError(f"Not a git repository: {repo_path}") from exc

    try:
        raw_diff = repo.git.diff(
            base_sha,
            head_sha,
            "--unified=0",
            "--find-renames",
        )
    except git.GitCommandError as exc:
        stderr_obj = exc.stderr
        detail = stderr_obj.strip() if isinstance(stderr_obj, str) and stderr_obj else str(exc)
        raise RepoNotFoundError(
            f"git diff failed for {repo_path} at {base_sha}..{head_sha}: {detail}"
        ) from exc

    # gitpython returns str already; normalize to a trailing newline so
    # unidiff's line-based tokenizer doesn't drop the last hunk on inputs
    # produced by unusual git configurations. We coerce with str() because
    # gitpython lacks type stubs and the return type is Any.
    diff_text: str = str(raw_diff)
    if diff_text and not diff_text.endswith("\n"):
        diff_text = diff_text + "\n"
    return diff_text


def _enforce_size_ceiling(diff_text: str, *, max_diff_bytes: int) -> None:
    """Raise :class:`DiffInputError` if ``diff_text`` exceeds ``max_diff_bytes`` UTF-8 bytes.

    We measure encoded bytes rather than string length because non-ASCII diff
    content (mostly file paths) can inflate size by up to 4x compared to a
    character count. The ceiling is meant to bound memory, so bytes are the
    honest metric.
    """
    size = len(diff_text.encode("utf-8"))
    if size > max_diff_bytes:
        raise DiffInputError(f"Diff exceeds max_diff_bytes ({size} > {max_diff_bytes} bytes).")


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def _parse_patch_set(diff_text: str) -> unidiff.PatchSet:
    """Parse ``diff_text`` with :mod:`unidiff`, translating any failure to :class:`DiffParseError`.

    :mod:`unidiff` raises a variety of exceptions (``UnidiffParseError``,
    ``ValueError``, occasionally ``IndexError`` on truncated input). The
    module boundary contract in ``design.md §Error Handling`` forbids any of
    them from escaping, so we catch broadly here.
    """
    if diff_text == "":
        # Empty diff is a legal "no changes" input; unidiff produces an empty
        # PatchSet for it, but constructing one directly avoids a needless
        # tokenizer pass on the empty string.
        return unidiff.PatchSet([])

    try:
        return unidiff.PatchSet.from_string(diff_text)
    except Exception as exc:
        raise DiffParseError(f"Malformed unified diff: {exc}") from exc


def _build_file_changes(patch_set: unidiff.PatchSet) -> tuple[FileChange, ...]:
    """Convert every :class:`unidiff.PatchedFile` into a :class:`FileChange`.

    Returns a POSIX-lexicographically-sorted tuple so identical inputs produce
    byte-identical JSON downstream. Binary files come through with empty
    ``hunks`` — they are recorded in ``ChangeSet.files`` (they still count as
    changed files) but the AST indexer filters them out via
    :attr:`ChangeSet.python_files`.
    """
    changes: list[FileChange] = []
    for patched_file in patch_set:
        change_kind = _classify_change_kind(patched_file)
        old_path = _rename_source_path(patched_file) if change_kind == "renamed" else None
        hunks = tuple(_build_hunk(h) for h in patched_file)
        changes.append(
            FileChange(
                path=str(patched_file.path),
                change_kind=change_kind,
                old_path=old_path,
                hunks=hunks,
            )
        )
    changes.sort(key=lambda fc: fc.path)
    return tuple(changes)


def _classify_change_kind(patched_file: unidiff.PatchedFile) -> ChangeKind:
    """Map one :class:`unidiff.PatchedFile` to our :data:`ChangeKind` literal.

    Added and deleted are decided only from explicit markers (see
    :func:`_is_added_file` and :func:`_is_deleted_file`), never from hunk
    shape. unidiff's own ``is_added_file`` / ``is_removed_file`` also treat a
    lone ``@@ -0,0 +1,N @@`` / ``@@ -1,N +0,0 @@`` hunk as an added / deleted
    file, but ``--unified=0`` (which the SHA path always uses) produces exactly
    those headers for an insertion at the top of a file and for a removal
    starting at line 1. Both are modifications.

    Order matters: ``is_rename`` must be checked before falling back to
    ``"modified"`` because a rename-with-edits also changes lines, and we want
    the rename classification to win (downstream consumers care that the path
    moved, not just that lines changed).
    """
    if _is_added_file(patched_file):
        return "added"
    if _is_deleted_file(patched_file):
        return "deleted"
    if patched_file.is_rename:
        return "renamed"
    return "modified"


def _is_added_file(patched_file: unidiff.PatchedFile) -> bool:
    """Return whether the diff marks ``patched_file`` as added.

    The markers are a ``--- /dev/null`` source or git's ``new file mode``
    extended header. unidiff already rewrites the source to ``/dev/null``
    when its pattern matches that header; the header is also checked directly
    so the classification does not depend on that rewrite (the pattern does
    not match a ``\\r\\n``-terminated header, for one). The header is the only
    marker for an empty new file, whose git diff has no ``---`` / ``+++``
    lines and no hunks.
    """
    if str(patched_file.source_file) == _DEV_NULL:
        return True
    return _has_git_header(patched_file, _GIT_NEW_FILE_HEADER)


def _is_deleted_file(patched_file: unidiff.PatchedFile) -> bool:
    """Return whether the diff marks ``patched_file`` as deleted.

    The mirror of :func:`_is_added_file`: a ``+++ /dev/null`` target or git's
    ``deleted file mode`` extended header.
    """
    if str(patched_file.target_file) == _DEV_NULL:
        return True
    return _has_git_header(patched_file, _GIT_DELETED_FILE_HEADER)


def _has_git_header(patched_file: unidiff.PatchedFile, prefix: str) -> bool:
    """Return whether one of ``patched_file``'s extended header lines starts with ``prefix``.

    ``patch_info`` holds the lines unidiff read before the file's ``---``
    line; for git, that is the ``diff --git`` line and its extended headers.
    It is ``None`` when there were no such lines.
    """
    return any(str(line).startswith(prefix) for line in patched_file.patch_info or ())


def _rename_source_path(patched_file: unidiff.PatchedFile) -> str:
    """Return the pre-rename path stripped of the ``a/`` prefix.

    :mod:`unidiff` exposes ``patched_file.path`` which strips the ``b/``
    prefix off the *target* path, but there is no equivalent accessor for the
    source path on a rename, so we strip ``a/`` ourselves.
    """
    source: str = str(patched_file.source_file)
    if source.startswith("a/"):
        return source[2:]
    return source


def _build_hunk(unidiff_hunk: unidiff.Hunk) -> Hunk:
    """Convert one :class:`unidiff.Hunk` into our frozen :class:`Hunk`.

    The added / removed line index sets are computed once here rather than
    lazily so the resulting :class:`Hunk` is fully hashable and safe to store
    in caches keyed by content. The line-text fields are filled alongside;
    they do not take part in equality or hashing.
    """
    added_lines, removed_lines = _line_index_sets(unidiff_hunk)
    source_lines, target_lines = _line_texts(unidiff_hunk)
    return Hunk(
        old_start=int(unidiff_hunk.source_start),
        old_lines=int(unidiff_hunk.source_length),
        new_start=int(unidiff_hunk.target_start),
        new_lines=int(unidiff_hunk.target_length),
        added_lines=added_lines,
        removed_lines=removed_lines,
        source_lines=source_lines,
        target_lines=target_lines,
    )


def _line_texts(
    unidiff_hunk: Iterable[unidiff.patch.Line],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(source_lines, target_lines)`` as line text in diff order.

    ``source_lines`` is the pre-image side (context + removed lines);
    ``target_lines`` is the post-image side (context + added lines).

    Only context / added / removed lines are kept. unidiff's "No newline at
    end of file" marker lines (backslash line type) and the trailing blank
    lines it appends after a hunk (empty line type) match none of those
    predicates, so they never reach either tuple.
    """
    source: list[str] = []
    target: list[str] = []
    for line in unidiff_hunk:
        # unidiff's ``Line.value`` keeps the line terminator ("\n" or
        # "\r\n"). We store the text without it; ``reverse_hunks`` compares
        # after ``rstrip("\r\n")`` and splices whole lines.
        text = str(line.value).rstrip("\r\n")
        if line.is_context:
            source.append(text)
            target.append(text)
        elif line.is_removed:
            source.append(text)
        elif line.is_added:
            target.append(text)
    return tuple(source), tuple(target)


def _line_index_sets(
    unidiff_hunk: Iterable[unidiff.patch.Line],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return ``(added_line_numbers, removed_line_numbers)`` in ascending order.

    Added line numbers come from ``target_line_no`` (post-image indices);
    removed line numbers come from ``source_line_no`` (pre-image indices).
    :mod:`unidiff` occasionally emits a ``None`` line number on malformed
    context lines; we skip those defensively rather than surface a
    :class:`TypeError` at the module boundary.
    """
    added: list[int] = []
    removed: list[int] = []
    for line in unidiff_hunk:
        if line.is_added and line.target_line_no is not None:
            added.append(int(line.target_line_no))
        elif line.is_removed and line.source_line_no is not None:
            removed.append(int(line.source_line_no))
    added.sort()
    removed.sort()
    return tuple(added), tuple(removed)
