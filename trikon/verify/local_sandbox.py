"""Host-local subprocess backend for the Verification Runner.

This module ships the ``--no-sandbox`` opt-in backend for the verification
runner. Unlike :class:`~trikon.verify.sandbox.LocalDockerSandbox` — which
executes every command inside a hardened, network-disabled container —
:class:`LocalSubprocessSandbox` runs commands **directly on the host**
through :func:`subprocess.run`, with **no isolation whatsoever**.

Why does this exist?
--------------------

Docker Desktop is not installed on every developer's laptop, and on some
Windows / macOS environments it is actively prohibited by IT policy. The
subprocess backend lets a developer working on a repo they already trust
run ``trikon verify --no-sandbox`` and get a real verdict without having
to stand up Docker first. Nothing about this backend is safe for hostile
input: the pytest / ruff / mypy invocations run as the current user with
full filesystem access, network access, and no memory / cpu / pids cap.

Rules of engagement
-------------------

* This backend **MUST NEVER** be the default. Verification against
  untrusted code (CI runs on incoming PRs, third-party plugin dispatch,
  anything touched by an AI coding agent whose output has not been
  reviewed) MUST route through :class:`LocalDockerSandbox`.
* The CLI surfaces this backend exclusively through the ``--no-sandbox``
  flag and displays a security-warning banner every time the flag is
  active. The SDK exposes the same knob via ``verify(no_sandbox=True)``
  for parity, but the safety responsibility to warn the user belongs to
  the CLI, not the SDK.
* The public interface is deliberately identical to
  :class:`LocalDockerSandbox` (context manager, ``mount_repo``,
  ``exec`` -> :class:`SandboxExecResult`), so the runner can hold either
  backend polymorphically through the ``Sandbox`` union alias.

See ``design.md §5.6`` for the Docker vs subprocess backend rationale.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType

from trikon.verify.errors import SandboxExecError
from trikon.verify.models import SandboxExecResult

__all__ = ["LocalSubprocessSandbox"]


# The Docker sandbox mounts the target repo at this virtual path and the
# runner passes the same string as ``workdir`` on every ``exec`` call. The
# subprocess backend rewrites that prefix to the real host path so the
# runner does not need to know which backend it is talking to. Any other
# ``workdir`` value is passed through verbatim.
_DOCKER_REPO_MOUNT_TARGET = "/workspace/repo"


class LocalSubprocessSandbox:
    """Host-local subprocess backend — the ``--no-sandbox`` opt-in.

    Provides the same context-manager + ``mount_repo`` + ``exec`` shape as
    :class:`LocalDockerSandbox` so the verification runner can hold either
    backend polymorphically. Every command runs on the host through
    :func:`subprocess.run` with no isolation. See the module docstring for
    the rules of engagement.

    Tools invoked via ``exec`` resolve against a PATH that is prefixed
    with the running Python's bin directory. This means ``mypy``,
    ``ruff``, ``pytest``, and other tools installed alongside the venv
    Python are discoverable without the caller having to activate the
    venv externally — a plain ``.venv\\Scripts\\python.exe -m trikon.cli
    verify --no-sandbox ...`` invocation Just Works.
    """

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        """Configure the subprocess backend.

        Args:
            timeout_seconds: Accepted for interface parity with
                :class:`LocalDockerSandbox`'s Docker-side timeout knob.
                Unused by this backend — per-exec deadlines are honored
                via the ``timeout_seconds`` kwarg on :meth:`exec`
                instead. Present so callers can pass the same kwargs to
                either backend without conditional plumbing.
        """
        # ``timeout_seconds`` is accepted for interface parity but has no
        # role in this backend; each ``exec`` call carries its own
        # per-invocation deadline. Named receipt so ruff F841 stays quiet
        # while keeping the signature aligned with the Docker sandbox.
        del timeout_seconds

        # Populated by ``mount_repo`` before the first ``exec`` call.
        self._repo_path: Path | None = None

        # Toggled by ``__enter__`` / ``__exit__`` so ``exec`` refuses to
        # run outside an active context-manager scope, mirroring the
        # Docker sandbox's discipline.
        self._active: bool = False

    # ------------------------------------------------------------------ #
    # Context-manager plumbing.
    # ------------------------------------------------------------------ #

    def __enter__(self) -> LocalSubprocessSandbox:
        """Mark the sandbox active and verify the host has Python on PATH.

        The subprocess backend has no daemon to talk to, so ``__enter__``
        is nearly free. The only pre-flight is a ``shutil.which("python")``
        (falling back to ``python3``) — if Python is not on ``PATH`` at
        all, downstream ``exec`` calls that shell out to pytest / mypy /
        ruff will fail with confusing FileNotFoundError, so we surface it
        cleanly here.
        """
        if shutil.which("python") is None and shutil.which("python3") is None:
            raise SandboxExecError(
                "LocalSubprocessSandbox.__enter__ could not find a Python "
                "interpreter on PATH; --no-sandbox requires python or "
                "python3 to be resolvable via the shell."
            )
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Clear the active flag; no other side effects."""
        # No daemon, no container, no cleanup — just flip the flag.
        del exc_type, exc, tb
        self._active = False

    # ------------------------------------------------------------------ #
    # Public API — mount + exec.
    # ------------------------------------------------------------------ #

    def mount_repo(self, repo_path: Path, *, read_only: bool = True) -> None:
        """Record ``repo_path`` as the working directory for future execs.

        The subprocess backend cannot enforce a read-only view of the
        repository (that would require an OS-level bind mount with the
        ``ro`` flag or a copy-on-write staging directory, neither of
        which is portable across Windows / macOS / Linux without root).
        The parity contract is preserved by rejecting
        ``read_only=False`` — Phase 2 has no reason to write into the
        repo and we prefer a loud failure over a silent capability
        downgrade.

        Args:
            repo_path: Absolute or relative path to the repository. The
                path is resolved (``Path.resolve``) and stored; every
                subsequent ``exec`` runs with this as its ``cwd``
                unless the caller passes an override.
            read_only: MUST be ``True`` — the subprocess backend has
                no isolation to enforce so read-only is a contract we
                honor by refusing anything else.
        """
        if not read_only:
            raise SandboxExecError(
                "LocalSubprocessSandbox.mount_repo requires read_only=True; "
                "the host-local backend cannot enforce a writable mount view "
                "without an OS-level bind mount."
            )
        self._repo_path = repo_path.resolve()

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str | None = None,
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        """Run ``argv`` on the host and return the outcome.

        Contract parity with :meth:`LocalDockerSandbox.exec`: a non-zero
        exit is returned on the :class:`SandboxExecResult` — never
        raised. Wall-clock timeouts are enforced via
        :func:`subprocess.run`'s ``timeout`` kwarg and materialize as a
        result with ``exit_code=124``, ``timed_out=True``, and
        ``stderr="local sandbox exceeded deadline"`` (mirroring the
        Docker backend's synthesized timeout shape, Requirement 2.2).
        Only infrastructure faults (argv[0] not on PATH, OSError on
        spawn) raise :class:`SandboxExecError`.

        The ``env`` argument is merged **on top of** the current
        process's environment (``os.environ``) so subprocess inherits
        PATH, HOME, TMP, and every other host-level variable it needs
        to find pytest / ruff / mypy. This differs from the Docker
        backend, which starts each container from an empty environment
        and layers ``env`` on top of the image defaults, but that
        difference is intentional: the subprocess backend is running
        on the developer's machine and expects to inherit the shell
        environment the ``trikon`` command was launched from.

        Args:
            argv: The command to run. ``argv[0]`` is resolved through
                the current PATH.
            workdir: Working directory for the subprocess. ``None``
                (the default) means use the repo path recorded by
                :meth:`mount_repo`. The Docker backend passes
                ``/workspace/repo`` as the workdir for every exec; that
                virtual path is transparently rewritten to the host
                repo path here so the runner can stay backend-agnostic.
            timeout_seconds: Wall-clock ceiling. ``None`` disables the
                timeout. On breach the returned result carries
                ``timed_out=True`` and this method never raises.
            env: Extra environment variables to layer on top of
                :data:`os.environ`. Passed as a tuple of
                ``(name, value)`` pairs so callers can hold the value
                inside a frozen dataclass.

        Returns:
            A :class:`SandboxExecResult` with the process's exit code,
            captured stdout / stderr (UTF-8 decoded), duration in
            milliseconds, and the ``timed_out`` flag.

        Raises:
            SandboxExecError: On :class:`FileNotFoundError` (argv[0]
                not on PATH), any other :class:`OSError` from
                :func:`subprocess.run`, or if the sandbox has not been
                entered / :meth:`mount_repo` has not been called yet.
        """
        if not self._active:
            raise SandboxExecError(
                "LocalSubprocessSandbox.exec called outside of an active context manager"
            )

        # Strip sandbox-only ``--cache-dir=`` flags before host-side
        # invocation — mirror of the strip in
        # ``trikon.verify.static_checks._run_baseline_tool_on_host``
        # (v0.3.3 Bug F fix, which pinned
        # ``--cache-dir=/workspace/tmp/.<tool>_cache`` in
        # ``DEFAULT_STATIC_TOOLS`` so ruff and mypy could write their
        # caches to the sandbox's writable tmpfs while the repo
        # bind-mount stayed read-only inside the container). The
        # ``/workspace/tmp`` prefix only exists inside the Docker
        # sandbox; on any host filesystem it does not exist, and on
        # Windows it is not even a valid path. Without this strip ruff
        # and mypy fail cache init on the missing host path, produce
        # empty output, and ``--no-sandbox`` static findings silently
        # collapse to ``[]`` while the Docker path returns real findings
        # on the same diff. Letting the host tool fall back to its
        # default cache location (adjacent to the worktree, or the
        # user's platform cache dir) is correct — the worktree is
        # short-lived and torn down immediately after the tool exits.
        # See ``DEFAULT_STATIC_TOOLS`` in :mod:`trikon.verify.models`
        # for the sandbox-side pin.
        argv = tuple(t for t in argv if not t.startswith("--cache-dir="))

        resolved_workdir = self._resolve_workdir(workdir)

        merged_env: dict[str, str] = os.environ.copy()

        # Prepend the running Python's bin directory to PATH so venv
        # tool discovery works out of the box. When the caller launches
        # ``.venv/Scripts/python.exe -m trikon.cli verify --no-sandbox``
        # (Windows) or ``.venv/bin/python -m trikon.cli ...`` (Unix), the
        # subprocess backend inherits the parent's PATH — but that PATH
        # may not include the venv's script directory, so ``mypy`` /
        # ``ruff`` / ``pytest`` (all installed into the venv) fail to
        # resolve with ``FileNotFoundError`` and the whole verify run
        # fail-closes on a cosmetic PATH gap. Prepending
        # ``dirname(sys.executable)`` ensures the venv-installed tools
        # win over any host-level shadows and get found even when the
        # user has not activated the venv externally.
        python_bin_dir = str(Path(sys.executable).parent)
        existing_path = merged_env.get("PATH")
        if existing_path:
            merged_env["PATH"] = python_bin_dir + os.pathsep + existing_path
        else:
            merged_env["PATH"] = python_bin_dir

        # Caller-supplied env pairs override every default above,
        # including our PATH prepend — this preserves the documented
        # contract that ``env`` layers on top last.
        for name, value in env:
            merged_env[name] = value

        # Resolve argv[0] against the augmented PATH before spawning.
        # On Windows, ``CreateProcess`` searches PATH but does NOT try
        # PATHEXT extensions (``.exe`` / ``.bat`` / ``.cmd``) when the
        # caller passes a bare tool name like ``mypy``; without this
        # step, the venv ``Scripts/mypy.exe`` binary we just made
        # discoverable via PATH would still raise
        # ``FileNotFoundError``. ``shutil.which`` honors PATHEXT and
        # accepts an explicit ``path`` argument, so we pass the merged
        # PATH here to keep the resolution consistent with what the
        # subprocess would see. If ``which`` cannot resolve the tool
        # (bad tool name, still missing from PATH) we pass argv[0]
        # through unchanged so :func:`subprocess.run` raises the same
        # FileNotFoundError the caller expects for
        # ``SandboxExecError: command not found on PATH``.
        resolved_argv: list[str] = list(argv)
        if resolved_argv:
            resolved_exe = shutil.which(resolved_argv[0], path=merged_env.get("PATH"))
            if resolved_exe is not None:
                resolved_argv[0] = resolved_exe

        started = time.monotonic()
        try:
            completed = subprocess.run(
                resolved_argv,
                cwd=str(resolved_workdir),
                env=merged_env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Requirement 2.2 explicit: a deadline breach never raises.
            # Return a synthesized timed-out result with the same shape
            # the Docker backend uses (exit_code=124, timed_out=True).
            stdout_bytes = exc.stdout if isinstance(exc.stdout, bytes | str) else None
            stdout_text: str
            if isinstance(stdout_bytes, bytes):
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            elif isinstance(stdout_bytes, str):
                stdout_text = stdout_bytes
            else:
                stdout_text = ""
            timeout_ms = int((timeout_seconds or 0.0) * 1000)
            return SandboxExecResult(
                exit_code=124,
                stdout=stdout_text,
                stderr="local sandbox exceeded deadline",
                duration_ms=timeout_ms,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise SandboxExecError(
                f"LocalSubprocessSandbox.exec: command not found on PATH: {argv[0]!r} ({exc})"
            ) from exc
        except OSError as exc:
            raise SandboxExecError(
                f"LocalSubprocessSandbox.exec: OS error running {argv!r}: {exc}"
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        return SandboxExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
            timed_out=False,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #

    def _resolve_workdir(self, workdir: str | None) -> Path:
        """Translate a Docker-style workdir into a host-side path.

        The runner passes ``/workspace/repo`` (the Docker sandbox's
        virtual mount target) as the ``workdir`` on every ``exec``
        call. The subprocess backend needs to rewrite that prefix to
        the real host path recorded by :meth:`mount_repo` so the
        runner can stay backend-agnostic. ``workdir=None`` is the
        implicit "use the repo path" default. Any other absolute path
        (host-side already) or relative fragment is passed through
        unchanged, resolved against the repo path.
        """
        if self._repo_path is None:
            raise SandboxExecError("LocalSubprocessSandbox.exec called before mount_repo")
        if workdir is None:
            return self._repo_path
        if workdir == _DOCKER_REPO_MOUNT_TARGET:
            return self._repo_path
        if workdir.startswith(_DOCKER_REPO_MOUNT_TARGET + "/"):
            suffix = workdir[len(_DOCKER_REPO_MOUNT_TARGET) + 1 :]
            return self._repo_path / suffix
        # Absolute host paths pass through; relative fragments resolve
        # against the repo path (the natural fallback for the runner's
        # rare non-``/workspace/repo`` workdir overrides).
        candidate = Path(workdir)
        if candidate.is_absolute():
            return candidate
        return self._repo_path / candidate
