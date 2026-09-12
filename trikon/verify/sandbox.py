"""Isolation layer for verification.

Two backends:

    local_docker      (v0.1)   plain Docker container from a pinned base image.
    unideploy_warden  (v0.2)   shell out to Unideploy's `warden` binary for its
                               mTLS-bootstrapped sandbox.

Both backends expose the same `execute()` contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of a single sandbox command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class Sandbox:
    """Abstract sandbox interface."""

    def execute(
        self,
        command: list[str],
        repo_mount: Path,
        network: bool = False,
        env: dict[str, str] | None = None,
        timeout_s: int = 600,
    ) -> SandboxResult:
        raise NotImplementedError


class LocalDockerSandbox(Sandbox):
    """v0.1 backend. Uses `docker run` under the hood."""

    def __init__(self, image: str) -> None:
        self._image = image

    def execute(
        self,
        command: list[str],
        repo_mount: Path,
        network: bool = False,
        env: dict[str, str] | None = None,
        timeout_s: int = 600,
    ) -> SandboxResult:
        # TODO: subprocess.run(["docker", "run", ...]) with:
        #   - --rm
        #   - --read-only mount for the repo
        #   - --tmpfs /work-out for results
        #   - --network none unless network=True
        #   - --user <non-root uid>
        raise NotImplementedError


class UnideployWardenSandbox(Sandbox):
    """v0.2 backend. Shells out to Unideploy's `warden` binary.

    Reuses the mTLS-bootstrapped sandbox that Unideploy autopilot already runs.
    See demounideploy-main/libs/server/src/sandbox.rs for the underlying impl.
    """

    def __init__(self, image: str, warden_binary: Path) -> None:
        self._image = image
        self._warden_binary = warden_binary

    def execute(
        self,
        command: list[str],
        repo_mount: Path,
        network: bool = False,
        env: dict[str, str] | None = None,
        timeout_s: int = 600,
    ) -> SandboxResult:
        # TODO: subprocess.run([str(self._warden_binary), "run", "--image", self._image, ...])
        raise NotImplementedError
