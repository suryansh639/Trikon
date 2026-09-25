"""Shallow-fetch + checkout subprocess wrappers (design.md §3.9 clarify 2).

Implements the memo §5.4 / Requirement 4.1 flow for materializing a
PR's head and merge-base commits into ``/tmp/repo`` before invoking
``trikon.sdk.verify(...)``:

1. ``git init working_dir``
2. ``git remote add origin https://x-access-token:<token>@github.com/<repo>.git``
3. ``git fetch --depth 50 origin <head_sha>``
4. ``git fetch --depth 50 origin <base_sha>`` — on failure (typically a
   force-push race that made the merge base unreachable at shallow
   depth 50), retry once via ``git fetch --unshallow origin <head_sha>``
   then re-attempt the base fetch (Requirement 4.5).
5. ``git checkout <head_sha>``

Public surface:

* :func:`shallow_fetch_and_checkout` — the four-step flow above.
* :class:`GitOpsError` — raised on any git subprocess non-zero exit
  after the retry path is exhausted.

**Security invariant** (Invariant 6): the auth URL embeds the
installation token in its authority portion. This module NEVER logs
the ``argv`` list at any level, NEVER echoes captured ``stderr`` (git
mirrors the URL back in error output), and NEVER embeds the URL in
raised exception messages. Only the step name and the numeric exit
code cross the observability boundary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from trikon_cloud.fargate_runner.logger import get_logger

# Ordering places the primary entrypoint before its exception — deliberate,
# not alphabetical (mirrors sibling ``dynamodb_writer`` / ``github_client``).
__all__ = ["shallow_fetch_and_checkout", "GitOpsError"]  # noqa: RUF022


_LOGGER = get_logger("trikon_cloud.fargate_runner.git_ops")


class GitOpsError(Exception):
    """Raised when a git subprocess exits non-zero.

    Carries the failing step name (``init`` / ``remote add`` /
    ``fetch head`` / ``fetch base`` / ``fetch head unshallow`` /
    ``fetch base retry`` / ``checkout``) and the exit code in its
    message. Never carries the auth URL or the installation token —
    the message is scrubbed before formatting.
    """


def shallow_fetch_and_checkout(
    *,
    repo_full_name: str,
    head_sha: str,
    base_sha: str,
    installation_token: str,
    working_dir: Path,
) -> None:
    """Shallow-fetch ``head_sha`` and ``base_sha`` into ``working_dir``.

    Implements the five-step flow from Requirement 4.1:

    1. ``git init working_dir``
    2. ``git remote add origin
       https://x-access-token:<token>@github.com/<repo>.git``
    3. ``git fetch --depth 50 origin <head_sha>``
    4. ``git fetch --depth 50 origin <base_sha>`` — on failure (e.g.,
       force-push race that made the merge base unreachable at shallow
       depth 50), retry once with ``git fetch --unshallow origin
       <head_sha>`` then re-attempt the base fetch (Requirement 4.5).
    5. ``git checkout <head_sha>``

    Every :func:`subprocess.run` call goes through the :func:`_run`
    helper, which passes ``check=False`` and inspects the return code
    manually so we can construct a scrubbed :class:`GitOpsError` on
    failure. The auth URL (which embeds the installation token) is
    never logged, never persisted, and never included in error
    messages.

    Args:
        repo_full_name: ``owner/repo`` — the GitHub repository path.
        head_sha: 40-char hex SHA of the PR head commit.
        base_sha: 40-char hex SHA of the merge base.
        installation_token: The JIT-minted installation token, embedded
            in the git URL only; never logged or persisted.
        working_dir: Local directory to initialise the clone into.
            Created via ``mkdir(parents=True, exist_ok=True)`` before
            ``git init``.

    Raises:
        GitOpsError: If any git subprocess exits non-zero (post-retry
            for the fetch-base failure path).
    """
    working_dir.mkdir(parents=True, exist_ok=True)
    auth_url = (
        f"https://x-access-token:{installation_token}@github.com/{repo_full_name}.git"
    )

    _run(["git", "init"], cwd=working_dir, step="init")
    _run(
        ["git", "remote", "add", "origin", auth_url],
        cwd=working_dir,
        step="remote add",
    )
    _run(
        ["git", "fetch", "--depth", "50", "origin", head_sha],
        cwd=working_dir,
        step="fetch head",
    )

    # Try the base fetch; on failure, retry once via --unshallow.
    try:
        _run(
            ["git", "fetch", "--depth", "50", "origin", base_sha],
            cwd=working_dir,
            step="fetch base",
        )
    except GitOpsError:
        # Force-push race or similar — the base_sha may not be reachable
        # from any current branch at depth 50. Unshallow the head, then
        # re-attempt the base fetch (Requirement 4.5).
        _run(
            ["git", "fetch", "--unshallow", "origin", head_sha],
            cwd=working_dir,
            step="fetch head unshallow",
        )
        _run(
            ["git", "fetch", "--depth", "50", "origin", base_sha],
            cwd=working_dir,
            step="fetch base retry",
        )

    _run(["git", "checkout", head_sha], cwd=working_dir, step="checkout")


def _run(argv: list[str], *, cwd: Path, step: str) -> None:
    """Run a git subprocess; raise :class:`GitOpsError` on non-zero exit.

    The ``argv`` list may embed the auth URL (specifically in the
    ``remote add`` step). This helper NEVER logs ``argv`` at any log
    level — the URL carries the installation token. Only the ``step``
    name and the numeric exit code are logged on failure.

    ``capture_output=True`` collects ``stderr`` for diagnostic
    reasoning if a future refactor decides to surface it — but this
    implementation deliberately drops the captured output on the
    floor. ``git``'s failure ``stderr`` frequently echoes the auth
    URL back to the caller; the logger's PII processor would redact
    the ``x-access-token:...@`` fragment via ``_ACCESS_TOKEN_RE``
    (see :mod:`~trikon_cloud.fargate_runner.logger`), but we err on
    the side of not logging ``stderr`` at all rather than trusting
    the scrubber on a hot secret path.
    """
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        _LOGGER.warning(
            "git subprocess failed",
            step=step,
            returncode=result.returncode,
        )
        # Do NOT include ``argv`` in the message — the ``remote add``
        # variant carries the installation token in the URL.
        raise GitOpsError(f"git {step} failed: exit {result.returncode}")
