"""Docker-backed isolation layer for the Verification Runner.

Phase 2 ships a single sandbox backend: :class:`LocalDockerSandbox`, driven
by the local Docker daemon through the ``docker-py`` client. The class is
used as a context manager by :func:`trikon.verify.runner.run_verification`
to spin up a pinned container from ``trikon/sandbox:0.1.0``, run pytest,
ruff, mypy, and repo-defined plugins inside it, and tear the container down
cleanly on the way out.

Every call site in this module catches ``docker.errors.*`` (and every other
foreign exception the Docker client can raise) and re-raises the appropriate
:class:`VerificationRunnerError` subclass from :mod:`trikon.verify.errors`.
That closure is what lets ``trikon.sdk.verify`` translate any Docker-layer
failure into a ``require_human`` verdict without leaking a
``docker.errors.APIError`` past the SDK boundary (Requirement 6.1).

The container spec passed to ``client.containers.create`` matches the table
in ``design.md §5.2`` exactly: read-only bind mount of the repo at
``/workspace/repo``, two writable tmpfs mounts, ``network_mode="none"``,
dropped capabilities, ``no-new-privileges``, a read-only root filesystem,
and hard mem/CPU/pids ceilings. Timeout enforcement is wired (Task 5.2);
the network-allowlist branch is accepted at the API surface but falls back
to ``network_mode="none"`` in Phase 2 (Task 5.3, Option D) — full iptables
egress control lands in Phase 3. See the :class:`LocalDockerSandbox`
docstring for the Phase-2 vs Phase-3 tension.

See ``design.md §3.3`` for the public API contract; ``design.md §5.2`` for
the container spec; ``design.md §5.4`` for the startup sequence; and
``design.md §9.1`` for the raise-site table.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import time
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import docker
import docker.errors
import docker.types

from trikon.verify.errors import SandboxExecError, SandboxUnavailableError
from trikon.verify.models import SandboxExecResult

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from docker import DockerClient
    from docker.models.containers import Container

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — pulled out of the class body so both the container spec and the
# startup sequence agree on the exact paths and uid/gid pairs.
# ---------------------------------------------------------------------------

_SANDBOX_UID_GID = "10001:10001"
_REPO_MOUNT_TARGET = "/workspace/repo"
_TMPFS_TMP = "/workspace/tmp"
_TMPFS_PIP_CACHE = "/workspace/pip-cache"
_TMPFS_SPEC: dict[str, str] = {
    _TMPFS_TMP: "size=512m,uid=10001,gid=10001",
    _TMPFS_PIP_CACHE: "size=256m,uid=10001,gid=10001",
}
_CONTAINER_LABELS: dict[str, str] = {
    "trikon.role": "verify-sandbox",
    "trikon.version": "0.1.0",
}
_CPU_PERIOD = 100_000
# Startup guardrails — kept in module scope so `__enter__` and `mount_repo`
# share a single source of truth. Timeouts are conservative; the design's
# cold-path budget in §2.3 allows up to 25s across image pull + dep install.
_MOUNT_PROBE_TIMEOUT_SECONDS = 5.0
_DEP_INSTALL_TIMEOUT_SECONDS = 60.0


def _resolve_docker_socket_path() -> str:
    """Return the socket path :func:`docker.from_env` will try to open.

    ``docker.from_env`` picks the daemon endpoint from the ``DOCKER_HOST``
    environment variable if set, otherwise falls back to a per-OS default.
    Requirement 6.3 obliges :class:`SandboxUnavailableError` to name the
    exact path that failed to connect, so this helper reproduces the same
    resolution locally and hands the answer to the error constructor.
    """
    env_host = os.environ.get("DOCKER_HOST")
    if env_host:
        return env_host
    if os.name == "nt":
        # Docker Desktop on Windows exposes a named pipe by default.
        return "npipe:////./pipe/docker_engine"
    return "unix:///var/run/docker.sock"


class LocalDockerSandbox:
    """Docker-backed sandbox for Verification Runner (design.md §3.3).

    Use as a context manager::

        with LocalDockerSandbox() as sb:
            sb.mount_repo(repo_path)
            result = sb.exec(("pytest", "-q"), timeout_seconds=60.0)

    ``__enter__`` verifies the Docker daemon is reachable, pulls the image
    if it is missing from the local cache, creates the container with the
    hardened spec from ``design.md §5.2``, and starts it. ``__exit__``
    kills and removes the container (idempotent — repeated exits from a
    single ``with`` scope are a no-op past the first).

    Every method raises a :class:`VerificationRunnerError` subclass on
    failure. ``docker.errors.*`` never escapes this class — every call site
    wraps them into :class:`SandboxUnavailableError` (daemon reachability)
    or :class:`SandboxExecError` (everything else). Non-zero exit codes
    from ``exec`` are returned inside :class:`SandboxExecResult`; they never
    raise (design.md §3.3 explicit). Wall-clock timeouts are enforced by
    ``exec`` and also never raise: a breach produces a
    :class:`SandboxExecResult` with ``timed_out=True`` and ``exit_code=124``
    (Requirement 2.2, design.md §5.5). :class:`SandboxTimeoutError` exists
    in the error hierarchy for completeness only and is never raised past
    this module boundary (design.md §9.1).

    **Phase-2 limitation — network allowlist.** A non-empty
    ``network_allowlist`` is accepted at construction time but is *not*
    enforced in Phase 2. ``design.md §5.3`` calls for a dedicated bridge
    network plus an in-container ``iptables`` init sequence that pins
    egress to the resolved IP set. That init sequence requires
    ``CAP_NET_ADMIN`` inside the container, which conflicts with the
    ``cap_drop=["ALL"]`` and ``no-new-privileges`` hardening invariants
    this class holds unconditionally (Requirement 2.1, ``design.md §5.2``).
    Weakening either invariant for the allowlist branch is a
    defense-in-depth regression we are not willing to ship in Phase 2;
    the alternative — attaching to a bare bridge network *without* the
    iptables pin — is strictly worse than ``network_mode="none"`` because
    it opens outbound egress to every reachable destination rather than
    the requested subset. So Phase 2 fails safe: when a non-empty
    allowlist is supplied, the constructor logs a WARNING once and the
    sandbox falls back to ``network_mode="none"``. Full iptables egress
    control lands in Phase 3 once the surrounding CI + integration-test
    infrastructure for allowlist enforcement is wired.
    """

    def __init__(
        self,
        *,
        image: str = "trikon/sandbox:0.1.0",
        network_allowlist: tuple[str, ...] | None = None,
        mem_limit: str = "2g",
        cpu_quota: int = 200_000,
        pids_limit: int = 512,
    ) -> None:
        """Configure the sandbox spec; nothing talks to Docker yet.

        The Docker daemon is not contacted until :meth:`__enter__`. The
        constructor's only side effect is a one-shot WARNING log emitted
        when ``network_allowlist`` is non-empty (see the class docstring
        for why Phase 2 falls back to ``network_mode="none"``).

        Args:
            image: Pinned sandbox image tag. Defaults to
                ``trikon/sandbox:0.1.0``, the tag built by
                ``scripts/build_sandbox_image.sh``.
            network_allowlist: Egress allowlist requested by policy. In
                Phase 2 this argument is accepted for API stability with
                the eventual Phase-3 signature but is **not enforced**;
                a non-empty value triggers a WARNING log and the sandbox
                falls back to ``network_mode="none"``. See the class
                docstring for the rationale.
            mem_limit: Container memory ceiling (Docker size string).
            cpu_quota: CPU quota microseconds per ``cpu_period`` window
                (``design.md §5.2``); ``200_000`` at the default 100 ms
                period pins the container to two logical CPUs.
            pids_limit: PID cap for the container namespace.
        """
        self._image = image
        self._network_allowlist = network_allowlist
        self._mem_limit = mem_limit
        self._cpu_quota = cpu_quota
        self._pids_limit = pids_limit

        # Populated by ``mount_repo`` before ``__enter__`` finishes building
        # the container; a ``mount_repo`` call after entry with a different
        # path raises. See the ``mount_repo`` docstring.
        self._repo_path: Path | None = None

        # Populated by ``__enter__``; cleared by ``__exit__``.
        self._client: DockerClient | None = None
        self._container: Container | None = None

        # TODO(Phase 3): implement iptables egress control per
        # ``design.md §5.3``. Phase 3 will populate these fields in
        # ``__enter__`` when ``network_allowlist`` is non-empty (create a
        # dedicated bridge network, resolve hostnames via
        # ``socket.getaddrinfo``, and apply ``iptables -A OUTPUT`` rules
        # inside the container's netns) and tear them down in
        # ``__exit__``. Left as ``None`` in Phase 2 so the teardown path
        # below is a no-op on every code path.
        self._network_id: str | None = None
        self._network_name: str | None = None

        if network_allowlist:
            # Fail-safe fallback (Option D from Task 5.3 design tension).
            # Emit exactly once at construction so callers see the
            # semantic gap on the first log line rather than after a
            # verdict silently talked to the wider internet — or, worse,
            # silently failed to reach an allowlisted host.
            logger.warning(
                "network allowlist enforcement is stubbed in Phase 2; "
                "falling back to network_mode='none' for safety. "
                "Full iptables egress control lands in Phase 3. "
                "Requested allowlist: %s",
                network_allowlist,
            )

    # ------------------------------------------------------------------ #
    # Context-manager plumbing (design.md §3.3, §5.4).
    # ------------------------------------------------------------------ #

    def __enter__(self) -> LocalDockerSandbox:
        """Bring the sandbox online: daemon health, image pull, container start.

        The sequence exactly follows ``design.md §5.4`` — verify the daemon
        is reachable, ensure the image is present, create + start the
        container from the hardened spec, then run the mount-probe and pip
        dep-install steps against the running container.
        """
        socket_path = _resolve_docker_socket_path()
        try:
            self._client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise SandboxUnavailableError(
                f"Docker daemon unreachable at {socket_path}: {exc}. "
                f"Trikon uses Docker for sandboxed verification by default. "
                f"To fix this, either:\n"
                f"  - Start Docker Desktop (Windows/macOS) or the Docker daemon (Linux), or\n"
                f"  - Re-run with --no-sandbox to use the local-subprocess backend "
                f"(host-level, no isolation; only appropriate for trusted local repos)."
            ) from exc

        # A bare-metal ``docker.from_env`` succeeds even when the daemon is
        # down — the client only opens the socket on the first API call. A
        # cheap ``ping`` here surfaces "no daemon" as ``SandboxUnavailable``
        # instead of leaking a lower-level ``requests.ConnectionError`` out
        # of ``images.get`` a couple of lines below.
        try:
            self._client.ping()
        except docker.errors.DockerException as exc:
            raise SandboxUnavailableError(
                f"Docker daemon unreachable at {socket_path}: {exc}. "
                f"Trikon uses Docker for sandboxed verification by default. "
                f"To fix this, either:\n"
                f"  - Start Docker Desktop (Windows/macOS) or the Docker daemon (Linux), or\n"
                f"  - Re-run with --no-sandbox to use the local-subprocess backend "
                f"(host-level, no isolation; only appropriate for trusted local repos)."
            ) from exc

        self._ensure_image()
        self._create_and_start_container()
        # Post-start sanity + dep install per design.md §5.4. These use the
        # public ``exec`` path so a hostile container spec that silently
        # discarded the bind mount fails loudly here, not deep inside the
        # first pytest run.
        if self._repo_path is not None:
            self._verify_mount_and_install_deps()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Tear down the container regardless of how the ``with`` block exited.

        Failure to kill or remove is swallowed into a ``SandboxExecError``
        so a botched teardown does not mask the original exception (if any)
        from the ``with`` block itself; the runner treats the botched
        teardown as an infrastructure fault and lets the verdict fail
        closed.

        The Phase-3 allowlist branch also removes the dedicated bridge
        network after the container is gone; that path is a no-op in
        Phase 2 because ``_network_id`` is never populated (see the
        class docstring).
        """
        container = self._container
        client = self._client
        network_id = self._network_id
        network_name = self._network_name
        self._container = None
        self._network_id = None
        self._network_name = None
        if container is None:
            self._client = None
            return
        try:
            # Container may already be dead (exit-race). Swallow that
            # narrow case and proceed to remove; every other Docker error
            # is caught by the outer except below.
            with contextlib.suppress(docker.errors.APIError):
                container.kill()
            container.remove(force=True)
        except docker.errors.DockerException as exc:
            self._client = None
            raise SandboxExecError(f"failed to tear down verify sandbox container: {exc}") from exc
        finally:
            # TODO(Phase 3): once the allowlist branch actually creates a
            # bridge network in ``__enter__``, this block removes it
            # best-effort after the container is torn down. In Phase 2
            # ``network_id`` is always ``None`` so the whole block is
            # skipped. Network removal must never raise past
            # ``__exit__`` — the container is already gone and a
            # leftover empty bridge is a nuisance, not a correctness
            # failure.
            if network_id is not None and client is not None:
                try:
                    network = client.networks.get(network_id)
                    network.remove()
                except docker.errors.DockerException as network_exc:
                    logger.warning(
                        "failed to remove verify sandbox network %s (%s): %s",
                        network_name,
                        network_id,
                        network_exc,
                    )
            self._client = None

    # ------------------------------------------------------------------ #
    # Public API — mount + exec (design.md §3.3).
    # ------------------------------------------------------------------ #

    def mount_repo(self, repo_path: Path, *, read_only: bool = True) -> None:
        """Record the repository to bind-mount at ``/workspace/repo``.

        Called *before* ``__enter__`` — the mount is baked into the
        container spec via ``client.containers.create`` and cannot be added
        after the container is running. A second call inside the same
        context-manager scope with a different path raises
        :class:`SandboxExecError` (idempotent within a scope only).

        ``read_only`` is present on the signature for forward compatibility
        with Phase 3 mutations of the mounted tree; the Phase-2 sandbox
        always mounts read-only, per Requirement 2.1.
        """
        # Reject read-write mounts up front — Phase 2 has no reason to
        # write into the repo, and the container spec below always sets
        # ``read_only=True`` on the Mount. Passing ``read_only=False`` here
        # is a caller bug worth surfacing loudly.
        if not read_only:
            raise SandboxExecError(
                "LocalDockerSandbox.mount_repo requires read_only=True in Phase 2"
            )
        resolved = repo_path.resolve()
        if self._repo_path is not None and self._repo_path != resolved:
            raise SandboxExecError(
                f"repo already mounted at {self._repo_path}; refusing to remount at {resolved}"
            )
        self._repo_path = resolved

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str = _REPO_MOUNT_TARGET,
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        """Run ``argv`` inside the container and return the outcome.

        Contract from ``design.md §3.3``: a non-zero exit is returned on
        the :class:`SandboxExecResult` — it never raises. Only
        infrastructure faults (Docker API failure, missing container)
        raise, and they raise :class:`SandboxExecError`.

        Wall-clock timeouts are enforced via a ``time.monotonic`` bracket
        around ``exec_start`` (design.md §5.5): when the deadline lapses
        before the exec returns, the container is killed with ``SIGKILL``
        and this method returns
        ``SandboxExecResult(exit_code=124, timed_out=True, ...)`` — it
        never raises past the module boundary (Requirement 2.2 explicit).
        ``timeout_seconds=None`` disables the bracket entirely; callers
        that want the runner-wide budget must pass a concrete float.

        Because ``docker-py``'s ``exec_start(stream=False)`` blocks in the
        current thread until the exec finishes, the deadline is enforced
        by running ``exec_start`` on a single-worker
        :class:`~concurrent.futures.ThreadPoolExecutor` and waiting on the
        future with the requested timeout. On breach we kill the
        container so the background ``exec_start`` unblocks quickly and
        the pool can drain on the way out; the pool is shut down without
        waiting so a hung Docker daemon cannot stall the caller past the
        deadline.
        """
        container = self._container
        client = self._client
        if container is None or client is None:
            raise SandboxExecError(
                "LocalDockerSandbox.exec called outside of an active context manager"
            )

        try:
            create_response = client.api.exec_create(
                container.id,
                cmd=list(argv),
                user=_SANDBOX_UID_GID,
                workdir=workdir,
                environment=dict(env),
            )
        except docker.errors.DockerException as exc:
            raise SandboxExecError(f"exec_create failed for {argv!r}: {exc}") from exc

        exec_id = create_response["Id"]
        started = time.monotonic()

        if timeout_seconds is None:
            # No deadline — take the direct synchronous path so we do not
            # pay for a thread hand-off on the hot warm-verdict path.
            try:
                output = client.api.exec_start(exec_id, detach=False, stream=False)
            except docker.errors.DockerException as exc:
                raise SandboxExecError(f"exec_start failed for {argv!r}: {exc}") from exc
        else:
            # ``exec_start(stream=False)`` blocks until the exec finishes.
            # Wrap it in a single-worker thread pool so we can bound the
            # wait with ``future.result(timeout=...)``.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(
                    client.api.exec_start,
                    exec_id,
                    detach=False,
                    stream=False,
                )
                try:
                    output = future.result(timeout=timeout_seconds)
                except concurrent.futures.TimeoutError:
                    # Deadline breach: SIGKILL the container to unblock the
                    # background ``exec_start`` and return a synthesized
                    # timed-out result. Requirement 2.2 forbids raising here.
                    with contextlib.suppress(docker.errors.DockerException):
                        container.kill(signal="SIGKILL")
                    return SandboxExecResult(
                        exit_code=124,
                        stdout="",
                        stderr="sandbox exceeded deadline",
                        duration_ms=int(timeout_seconds * 1000),
                        timed_out=True,
                    )
                except docker.errors.DockerException as exc:
                    raise SandboxExecError(f"exec_start failed for {argv!r}: {exc}") from exc
            finally:
                # Do not block the caller on the background thread. On the
                # timeout path we already killed the container which
                # unblocks ``exec_start``; on the success path the future
                # has already resolved.
                pool.shutdown(wait=False)

        try:
            inspect = client.api.exec_inspect(exec_id)
        except docker.errors.DockerException as exc:
            raise SandboxExecError(f"exec_inspect failed for {argv!r}: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _decode_exec_output(output)
        exit_code_raw = inspect.get("ExitCode")
        exit_code = int(exit_code_raw) if exit_code_raw is not None else 0
        return SandboxExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            duration_ms=duration_ms,
            timed_out=False,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers — image pull, container create, startup sequence.
    # ------------------------------------------------------------------ #

    def _ensure_image(self) -> None:
        """Pull ``self._image`` if it is not already resident locally."""
        assert self._client is not None  # narrowed by __enter__
        try:
            self._client.images.get(self._image)
            return
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.DockerException as exc:
            raise SandboxExecError(
                f"failed to check local image cache for {self._image!r}: {exc}"
            ) from exc

        try:
            self._client.images.pull(self._image)
        except docker.errors.DockerException as exc:
            raise SandboxExecError(f"failed to pull sandbox image {self._image!r}: {exc}") from exc

    def _create_and_start_container(self) -> None:
        """Create + start the container from the hardened spec in design.md §5.2."""
        assert self._client is not None  # narrowed by __enter__
        mounts: list[docker.types.Mount] = []
        if self._repo_path is not None:
            mounts.append(
                docker.types.Mount(
                    target=_REPO_MOUNT_TARGET,
                    source=str(self._repo_path),
                    type="bind",
                    read_only=True,
                )
            )

        # ``network_mode="none"`` unconditionally in Phase 2. A non-empty
        # ``network_allowlist`` is honored at the API surface but not
        # enforced: the constructor already logged a WARNING and stashed
        # the requested allowlist on ``self._network_allowlist`` for
        # observability. The Phase-3 bridge-network branch will assign
        # ``self._network_name`` in ``__enter__`` and swap this value to
        # ``self._network_name`` at that point. See the class docstring
        # for the Phase-2/Phase-3 tension.
        network_mode = "none"

        try:
            container = self._client.containers.create(
                image=self._image,
                command=["sleep", "infinity"],
                user=_SANDBOX_UID_GID,
                working_dir=_REPO_MOUNT_TARGET,
                mounts=mounts,
                tmpfs=dict(_TMPFS_SPEC),
                network_mode=network_mode,
                mem_limit=self._mem_limit,
                pids_limit=self._pids_limit,
                cpu_period=_CPU_PERIOD,
                cpu_quota=self._cpu_quota,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                read_only=True,
                labels=dict(_CONTAINER_LABELS),
                detach=True,
                auto_remove=False,
            )
        except docker.errors.DockerException as exc:
            raise SandboxExecError(f"failed to create verify sandbox container: {exc}") from exc

        self._container = container
        try:
            container.start()
        except docker.errors.DockerException as exc:
            # Best-effort cleanup so a failed start does not leak a
            # never-run container into the local Docker state.
            with contextlib.suppress(docker.errors.DockerException):
                container.remove(force=True)
            self._container = None
            raise SandboxExecError(f"failed to start verify sandbox container: {exc}") from exc

    def _verify_mount_and_install_deps(self) -> None:
        """Startup sequence from design.md §5.4.

        Runs after the container is up:

        1. Probe that the read-only bind mount actually landed by testing
           for the presence of ``/workspace/repo/.git`` — an upstream
           permission or path failure would silently produce an empty
           mount, and we want that to surface here rather than as a
           mysterious pytest collection failure downstream.
        2. Install the repository's dev dependencies via ``pip install
           --no-deps -e .[dev]`` into the tmpfs-backed pip cache.

        Both steps use the same :meth:`exec` codepath that downstream
        callers use, so error handling and the container-lifecycle
        invariants are exercised on the happy path of every verdict.
        """
        probe = self.exec(
            ("test", "-d", "/workspace/repo/.git"),
            timeout_seconds=_MOUNT_PROBE_TIMEOUT_SECONDS,
        )
        if probe.exit_code != 0:
            raise SandboxExecError(
                "verify sandbox mount probe failed: /workspace/repo/.git is "
                f"not a directory inside the container (exit={probe.exit_code})"
            )

        result = self.exec(
            ("pip", "install", "--no-deps", "-e", ".[dev]"),
            workdir=_REPO_MOUNT_TARGET,
            env=(("PIP_CACHE_DIR", _TMPFS_PIP_CACHE),),
            timeout_seconds=_DEP_INSTALL_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            # Truncate the captured output the same way the design snippet
            # in §5.4 does — a long pip failure log would drown the
            # verdict-level error message otherwise.
            raise SandboxExecError(
                f"dep install failed inside verify sandbox: {result.stdout[:2000]}"
            )


def _decode_exec_output(output: object) -> str:
    """Coerce ``exec_start`` output into a UTF-8 string.

    ``docker-py``'s ``exec_start`` returns either ``bytes`` (default,
    ``stream=False``), a generator of ``bytes`` (``stream=True``), or
    ``None`` when the exec produced no output at all. We only ever call it
    with ``stream=False`` above, so the interesting branches are bytes and
    ``None``; the tuple branch handles the ``demux=True`` shape defensively
    in case a future caller flips that flag.
    """
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    if isinstance(output, tuple) and len(output) == 2:
        # ``demux=True`` returns (stdout, stderr); we do not enable it yet,
        # but decoding defensively costs nothing.
        stdout_bytes = output[0] if isinstance(output[0], bytes) else b""
        return stdout_bytes.decode("utf-8", errors="replace")
    # Any other shape (e.g. a stream generator) is not something we can
    # collapse into a string synchronously; represent it as an empty
    # payload rather than leaking a repr that could confuse downstream
    # parsers. The exit-code + duration on the result are still authoritative.
    return ""


# ---------------------------------------------------------------------------
# Sandbox factory + polymorphic type alias.
# ---------------------------------------------------------------------------
#
# LocalSubprocessSandbox implements the same protocol (context manager +
# ``mount_repo`` + ``exec`` -> :class:`SandboxExecResult`), so callers that
# hold either backend polymorphically annotate the variable with the
# ``Sandbox`` union alias below. The subprocess module is imported at the
# bottom of this module to avoid a circular import — ``local_sandbox.py``
# does not import ``sandbox.py`` back.
from trikon.verify.local_sandbox import LocalSubprocessSandbox  # noqa: E402

Sandbox = LocalDockerSandbox | LocalSubprocessSandbox


def create_sandbox(
    *,
    no_sandbox: bool = False,
    image: str = "trikon/sandbox:0.1.0",
    network_allowlist: tuple[str, ...] | None = None,
    mem_limit: str = "2g",
    cpu_quota: int = 200_000,
    pids_limit: int = 512,
) -> Sandbox:
    """Instantiate the appropriate sandbox backend.

    Defaults to :class:`LocalDockerSandbox` (production-safe isolation).
    When ``no_sandbox=True``, returns :class:`LocalSubprocessSandbox` —
    a host-level subprocess runner with NO isolation, intended for dev
    machines where Docker is unavailable. The CLI surfaces this via
    ``--no-sandbox`` and displays a security warning banner whenever it
    is active.

    The Docker-only construction arguments (``image``,
    ``network_allowlist``, ``mem_limit``, ``cpu_quota``, ``pids_limit``)
    are ignored when ``no_sandbox=True``; keeping them on the factory
    signature lets the runner call this factory with the same keyword
    set regardless of backend.
    """
    if no_sandbox:
        return LocalSubprocessSandbox()
    return LocalDockerSandbox(
        image=image,
        network_allowlist=network_allowlist,
        mem_limit=mem_limit,
        cpu_quota=cpu_quota,
        pids_limit=pids_limit,
    )


__all__ = [
    "LocalDockerSandbox",
    "LocalSubprocessSandbox",
    "Sandbox",
    "create_sandbox",
]
