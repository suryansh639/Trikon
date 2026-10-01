"""Import_Checker I/O shell: read the base and head trees, then run the pure core.

:func:`check_imports` is the only entry point. It turns a :class:`ChangeSet`
plus the working tree at ``repo_path`` into the two path maps that
:func:`trikon.change_intel.import_check.check_trees` consumes, and converts the
result into the public :class:`~trikon.evidence.report.ImportReport`.

Steps
-----
1. **Early exit.** A change with no Python_File in any ``path`` or
   ``old_path`` returns ``ImportReport()`` without touching disk or git.
2. **Head tree.** ``os.walk(repo_path, followlinks=False)`` collects every
   ``.py`` file. VCS metadata (``.git``, ``.hg``, ``.svn``), virtual
   environments (``.venv``, ``venv`` and any directory holding
   ``pyvenv.cfg``) and ``__pycache__`` are pruned. Only regular files are
   read: a symlinked or special ``.py`` entry is listed with ``None`` content
   (never followed, never opened), as is a file whose read fails.
3. **Base tree.** The head map minus added paths and rename targets, with each
   changed file that existed at base (``modified`` and ``deleted`` at
   ``path``, ``renamed`` at ``old_path``) replaced by its base content:

   * with a caller-supplied base SHA, ``git show <sha>:<path>``; a non-zero
     exit means the content is unobtainable (``None``);
   * without one, :func:`~trikon.change_intel.import_check.reverse_hunks`
     undoes the diff on the head text. A deleted file reverses against an
     empty head; a change with no hunks (pure rename, mode change) keeps the
     head bytes.
4. **Check.** ``changed_paths`` is every ``path``/``old_path`` of the change;
   ``head_changed_paths`` is every non-deleted ``path``.

Error contract (Req 7.1, 7.2)
-----------------------------
Only :class:`~trikon.change_intel.errors.ImportCheckError` escapes, always
chained to its cause:

* ``git`` cannot be spawned (missing executable, other :class:`OSError`) or a
  ``git show`` exceeds ``git_timeout_seconds``;
* the head-tree walk raises :class:`OSError` (``repo_path`` missing, not a
  directory, or a directory under it that cannot be listed: a partial head
  tree would hide both modules and import sites);
* the base SHA is empty or starts with ``-``, so it would be read as a git
  option or as the index rather than a revision.

Per-file failures never raise (Req 3.7, 4.10). They become ``None`` content,
which the pure core records in ``unparsed_files`` and, for changed files, as
``incomplete``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path, PurePath

from trikon.change_intel.errors import ImportCheckError
from trikon.change_intel.import_check import ImportCheckResult, check_trees, reverse_hunks
from trikon.change_intel.models import ChangeSet, FileChange
from trikon.evidence.report import BrokenImport, ImportReport, is_python_path

_DEFAULT_GIT_TIMEOUT_SECONDS: float = 10.0
"""Ceiling for a single ``git show``; a stuck git must not stall the verdict."""

_PRUNED_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "__pycache__"}
)
"""Directory names that are never part of the Head_Tree, at any depth."""

_VENV_MARKER = "pyvenv.cfg"
"""A directory holding this file is a virtual environment and is pruned."""


def check_imports(
    change_set: ChangeSet,
    repo_path: Path,
    *,
    git_timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> ImportReport:
    """Return the :class:`ImportReport` for ``change_set`` against ``repo_path``.

    ``repo_path`` is the Head_Tree (the working tree the rest of the engine
    reads). The Base_Tree comes from ``change_set.base_sha`` via ``git show``
    when it is set, and from reversing the change's hunks otherwise (Req 3.2,
    3.3). See the module docstring for the walk rules.

    Raises
    ------
    ImportCheckError
        ``git`` could not be spawned or timed out, the head-tree walk raised
        :class:`OSError`, or ``base_sha`` is empty or starts with ``-``.
        Per-file read and parse failures are recorded on the report instead.
    """
    if not any(_touches_python(file_change) for file_change in change_set.files):
        return ImportReport()

    head = _read_head_tree(repo_path)
    base = _build_base_map(change_set, repo_path, head, git_timeout_seconds)
    changed_paths = frozenset(
        path
        for file_change in change_set.files
        for path in (file_change.path, file_change.old_path)
        if path is not None
    )
    head_changed_paths = frozenset(
        file_change.path for file_change in change_set.files if file_change.change_kind != "deleted"
    )
    result = check_trees(
        base,
        head,
        changed_paths=changed_paths,
        head_changed_paths=head_changed_paths,
    )
    return _to_report(result)


# ---------------------------------------------------------------------------
# Head tree
# ---------------------------------------------------------------------------


def _touches_python(file_change: FileChange) -> bool:
    """Return whether ``file_change`` has a Python_File ``path`` or ``old_path``."""
    if is_python_path(file_change.path):
        return True
    return file_change.old_path is not None and is_python_path(file_change.old_path)


def _is_pruned(parent: str, name: str) -> bool:
    """Return whether directory ``name`` under ``parent`` is outside the Head_Tree."""
    if name in _PRUNED_DIR_NAMES:
        return True
    return os.path.exists(os.path.join(parent, name, _VENV_MARKER))


def _read_regular_file(path: str) -> bytes | None:
    """Return the bytes of the regular file at ``path``; ``None`` otherwise.

    ``lstat`` keeps symlinks from being followed and keeps FIFOs and device
    nodes from being opened (either could block or never end).
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        # Deliberately recorded, not raised: an unreadable file is a per-file
        # failure that feeds ``unparsed_files`` / ``incomplete`` (Req 4.10).
        return None


def _read_head_tree(repo_path: Path) -> dict[str, bytes | None]:
    """Map every Head_Tree ``.py`` path (repo-relative POSIX) to its bytes."""
    root = os.fspath(repo_path)

    def _raise_walk_error(exc: OSError) -> None:
        location = exc.filename if exc.filename is not None else root
        raise ImportCheckError(
            f"check_imports: cannot walk the head tree at {location}: {exc}"
        ) from exc

    contents: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise_walk_error, followlinks=False):
        dirnames[:] = [name for name in dirnames if not _is_pruned(dirpath, name)]
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == os.curdir else PurePath(rel_dir).as_posix() + "/"
        for name in filenames:
            if is_python_path(name):
                contents[prefix + name] = _read_regular_file(os.path.join(dirpath, name))
    return contents


# ---------------------------------------------------------------------------
# Base tree
# ---------------------------------------------------------------------------


def _base_path(file_change: FileChange) -> str | None:
    """Return the path ``file_change`` had at base, or ``None`` if it was added."""
    if file_change.change_kind == "renamed":
        return file_change.old_path
    if file_change.change_kind == "added":
        return None
    return file_change.path


def _build_base_map(
    change_set: ChangeSet,
    repo_path: Path,
    head: dict[str, bytes | None],
    git_timeout_seconds: float,
) -> dict[str, bytes | None]:
    """Return the Base_Tree path map for ``change_set``.

    Two passes, so a path that is both a rename target and another file's
    base path (a swap) ends up with its base content.
    """
    base_sha = change_set.base_sha
    if base_sha is not None and (not base_sha or base_sha.startswith("-")):
        raise ImportCheckError(
            f"check_imports: refusing base SHA {base_sha!r}; it is empty or starts with '-'"
        )

    base = dict(head)
    for file_change in change_set.files:
        if file_change.change_kind in ("added", "renamed"):
            base.pop(file_change.path, None)

    for file_change in change_set.files:
        path = _base_path(file_change)
        if path is None or not is_python_path(path):
            continue
        if base_sha is not None:
            base[path] = _git_show(repo_path, base_sha, path, git_timeout_seconds)
        else:
            base[path] = _reverse_file(file_change, head)
    return base


def _git_show(repo_path: Path, base_sha: str, path: str, timeout: float) -> bytes | None:
    """Return ``git show <base_sha>:<path>`` bytes; ``None`` on a non-zero exit.

    A non-zero exit (path absent at base, bad revision, not a repository) is
    a per-file failure. Failing to run git at all is not, and raises.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{base_sha}:{path}"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImportCheckError(
            f"check_imports: git show timed out after {timeout}s for "
            f"{base_sha}:{path} in {repo_path}"
        ) from exc
    except FileNotFoundError as exc:
        raise ImportCheckError(
            f"check_imports: cannot run git in {repo_path} (git not found on PATH?): {exc}"
        ) from exc
    except OSError as exc:
        raise ImportCheckError(
            f"check_imports: OS error running git show for {base_sha}:{path} in {repo_path}: {exc}"
        ) from exc
    if result.returncode != 0:
        return None
    return result.stdout


def _reverse_file(file_change: FileChange, head: dict[str, bytes | None]) -> bytes | None:
    """Rebuild one file's base bytes from its head bytes and the diff hunks.

    Head bytes are decoded with ``surrogateescape`` so bytes outside the
    hunks round-trip unchanged, which keeps PEP 263 coding cookies working.
    ``None`` means unobtainable: head content missing or unreadable, hunks
    that do not match the head, or restored text that cannot be encoded.
    """
    if file_change.change_kind == "deleted":
        head_text = ""
    else:
        raw = head.get(file_change.path)
        if raw is None:
            return None
        if not file_change.hunks:
            return raw
        head_text = raw.decode("utf-8", errors="surrogateescape")
    restored = reverse_hunks(head_text, file_change.hunks)
    if restored is None:
        return None
    try:
        return restored.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        # Deliberately recorded, not raised: diff text carrying a lone
        # surrogate cannot be written back as bytes, so the base content is
        # unobtainable (Req 3.7).
        return None


# ---------------------------------------------------------------------------
# Public report
# ---------------------------------------------------------------------------


def _to_report(result: ImportCheckResult) -> ImportReport:
    """Convert the pure core's result into the public :class:`ImportReport`."""
    return ImportReport(
        broken=[
            BrokenImport(
                path=record.path,
                line=record.line,
                module=record.module,
                name=record.name,
                kind=record.kind,
            )
            for record in result.broken
        ],
        incomplete=result.incomplete,
        unparsed_files=list(result.unparsed_files),
    )


__all__ = ["check_imports"]
