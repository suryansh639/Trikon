"""Regression tests: the in-sandbox pytest exec gets a writable ``TMPDIR``.

The Docker sandbox runs with a read-only rootfs and a read-only repo mount;
only the tmpfs mounts are writable. Before this fix the runner's pytest exec
carried no ``env`` and the image sets no ``TMPDIR``, so pytest's capture
``TemporaryFile`` raised ``FileNotFoundError: No usable temporary directory
found`` before collection. No JSON report was written, the ``cat`` read-back
failed to decode, and every verdict with a non-empty test selection
fail-closed to ``require_human``.

The fix is a base exec environment in :meth:`LocalDockerSandbox.exec`
(``_SANDBOX_EXEC_ENV``). These tests drive the real ``LocalDockerSandbox`` and
the real ``_run_test_stage`` against a fake Docker client that models the
read-only filesystem: pytest can only write a report (the Collection_Pass's
``collect.json`` or the run's ``pytest.json``) when ``TMPDIR`` names one of
the tmpfs mounts passed to ``containers.create``. With the fix reverted, the
Collection_Pass writes nothing and the stage fails closed with
``CollectionPassError``, before any test runs. A third test checks that the
host-local backend keeps the host's own ``TMPDIR`` on both pytest execs.

No Docker daemon is needed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import docker
import pytest

# Aliased so pytest does not try to collect the ``Test*`` classes.
from trikon.evidence.report import TestReport as _TestReport
from trikon.verify.collection import TestBudget as _Budget
from trikon.verify.local_sandbox import LocalSubprocessSandbox
from trikon.verify.models import SelectedTests
from trikon.verify.runner import _run_test_stage
from trikon.verify.sandbox import LocalDockerSandbox

_REPORT_PATH = "/workspace/tmp/pytest.json"
_COLLECT_PATH = "/workspace/tmp/collect.json"
_NODE_ID = "tests/test_a.py::test_ok"
_REPORT_JSON = json.dumps({"tests": [{"nodeid": _NODE_ID, "outcome": "passed", "duration": 0.01}]})
_COLLECT_JSON = json.dumps(
    {
        "collectors": [
            {
                "nodeid": "tests/test_a.py",
                "outcome": "passed",
                "result": [{"nodeid": _NODE_ID, "type": "Function"}],
            }
        ]
    }
)
# What each pytest exec writes, keyed by its ``--json-report-file`` path.
_REPORTS = {_COLLECT_PATH: _COLLECT_JSON, _REPORT_PATH: _REPORT_JSON}

# A coverage-map selection with a supplied base SHA, so the strategy is
# ``selected`` and the run uses the selected argv.
_SELECTED = SelectedTests(
    node_ids=(_NODE_ID,),
    coverage_map_stale=False,
    fallback_reasons=(),
    coverage_map_state="present",
)


def _run_stage(sandbox: LocalDockerSandbox | LocalSubprocessSandbox, repo: Path) -> _TestReport:
    """Run the real test stage with a fixed clock and a generous budget."""
    return _run_test_stage(
        sandbox,
        selected=_SELECTED,
        python_change=True,
        base_sha_supplied=True,
        changed_paths=frozenset({"src/a.py"}),
        broken_import_files=frozenset(),
        budget=_Budget(deadline_at=60.0, total_seconds=60.0),
        repo_prefixes=("/workspace/repo/", str(repo)),
        clock=lambda: 0.0,
    )


def _report_file(argv: tuple[str, ...]) -> str | None:
    """Return the ``--json-report-file`` path a pytest argv writes, if any."""
    for token in argv:
        if token.startswith("--json-report-file="):
            return token.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Fake Docker client
# ---------------------------------------------------------------------------


@dataclass
class _ExecCall:
    """One ``exec_create`` call as the fake daemon saw it."""

    argv: tuple[str, ...]
    environment: dict[str, str]
    exit_code: int = 0


@dataclass
class _FakeDockerState:
    """Shared state for the fake client: tmpfs mounts, files and exec calls."""

    tmpfs_mounts: set[str] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)
    calls: dict[str, _ExecCall] = field(default_factory=dict)

    def ordered_calls(self) -> list[_ExecCall]:
        return list(self.calls.values())


class _FakeContainer:
    id = "fake-container"

    def start(self) -> None:
        return None

    def kill(self, signal: str = "SIGKILL") -> None:
        del signal

    def remove(self, *, force: bool = False) -> None:
        del force


class _FakeContainers:
    def __init__(self, state: _FakeDockerState) -> None:
        self._state = state

    def create(self, **kwargs: object) -> _FakeContainer:
        tmpfs = kwargs.get("tmpfs")
        assert isinstance(tmpfs, dict)
        assert kwargs.get("read_only") is True
        self._state.tmpfs_mounts = {str(target) for target in tmpfs}
        return _FakeContainer()


class _FakeImages:
    def get(self, image: str) -> object:
        del image
        return object()


class _FakeApi:
    """Models the read-only container: only tmpfs paths are writable."""

    def __init__(self, state: _FakeDockerState) -> None:
        self._state = state

    def exec_create(
        self,
        container_id: str,
        *,
        cmd: list[str],
        user: str,
        workdir: str,
        environment: dict[str, str],
    ) -> dict[str, str]:
        del container_id, user, workdir
        exec_id = f"exec-{len(self._state.calls)}"
        self._state.calls[exec_id] = _ExecCall(argv=tuple(cmd), environment=dict(environment))
        return {"Id": exec_id}

    def exec_start(self, exec_id: str, *, detach: bool, stream: bool) -> bytes:
        del detach, stream
        call = self._state.calls[exec_id]
        if call.argv[0] == "pytest":
            # tempfile needs a writable directory; on this rootfs only the
            # tmpfs mounts qualify, and only TMPDIR can point pytest at one.
            if call.environment.get("TMPDIR") in self._state.tmpfs_mounts:
                report_file = _report_file(call.argv)
                assert report_file in _REPORTS, call.argv
                self._state.files[report_file] = _REPORTS[report_file]
                return b""
            call.exit_code = 1
            return b"FileNotFoundError: No usable temporary directory found\n"
        if call.argv[0] == "cat":
            content = self._state.files.get(call.argv[1])
            if content is None:
                call.exit_code = 1
                return f"cat: {call.argv[1]}: No such file or directory\n".encode()
            return content.encode()
        return b""

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        return {"ExitCode": self._state.calls[exec_id].exit_code}


class _FakeDockerClient:
    def __init__(self, state: _FakeDockerState) -> None:
        self.api = _FakeApi(state)
        self.containers = _FakeContainers(state)
        self.images = _FakeImages()

    def ping(self) -> bool:
        return True


@pytest.fixture()
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDockerState:
    """Route ``docker.from_env`` to the fake client and return its state."""
    state = _FakeDockerState()
    client = _FakeDockerClient(state)

    def fake_from_env() -> _FakeDockerClient:
        return client

    # Patch the module object: ``trikon.verify`` is shadowed by the
    # re-exported ``verify`` function, so a dotted-string target would not
    # resolve. ``sandbox.py`` calls ``docker.from_env()`` on this module.
    monkeypatch.setattr(docker, "from_env", fake_from_env)
    return state


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


def test_docker_pytest_exec_gets_tmpdir_on_writable_tmpfs(
    fake_docker: _FakeDockerState, tmp_path: Path
) -> None:
    sandbox = LocalDockerSandbox(image="trikon-test:unused")
    sandbox.mount_repo(tmp_path)

    with sandbox:
        report = _run_stage(sandbox, tmp_path)

    # The Collection_Pass runs first, then the selected run; both pytest
    # execs get the writable TMPDIR.
    pytest_calls = [c for c in fake_docker.ordered_calls() if c.argv[0] == "pytest"]
    assert [_report_file(c.argv) for c in pytest_calls] == [_COLLECT_PATH, _REPORT_PATH]
    assert "--collect-only" in pytest_calls[0].argv
    assert pytest_calls[1].argv[-1] == _NODE_ID
    for call in pytest_calls:
        assert call.environment["TMPDIR"] == "/workspace/tmp"
        assert call.environment["TMPDIR"] in fake_docker.tmpfs_mounts

    # Both reports reach the TestReport instead of fail-closing.
    assert report.status == "passed"
    assert (report.total, report.passed, report.failed) == (1, 1, 0)
    assert (report.collected, report.executed, report.strategy) == (1, 1, "selected")

    # Every exec inherits the base env; the dep install keeps its own pair.
    for call in fake_docker.ordered_calls():
        assert call.environment.get("TMPDIR") in fake_docker.tmpfs_mounts, call.argv
    pip_calls = [c for c in fake_docker.ordered_calls() if c.argv[0] == "pip"]
    assert len(pip_calls) == 1
    assert pip_calls[0].environment["PIP_CACHE_DIR"] == "/workspace/pip-cache"


def test_docker_exec_caller_env_overrides_base_env(
    fake_docker: _FakeDockerState, tmp_path: Path
) -> None:
    sandbox = LocalDockerSandbox(image="trikon-test:unused")
    sandbox.mount_repo(tmp_path)

    with sandbox:
        sandbox.exec(("true",), env=(("TMPDIR", "/workspace/pip-cache"), ("EXTRA", "1")))

    last = fake_docker.ordered_calls()[-1]
    assert last.argv == ("true",)
    assert last.environment == {"TMPDIR": "/workspace/pip-cache", "EXTRA": "1"}


# ---------------------------------------------------------------------------
# Host-local backend
# ---------------------------------------------------------------------------


def test_local_backend_keeps_host_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host_tmp = str(tmp_path / "host-tmp")
    monkeypatch.setenv("TMPDIR", host_tmp)
    seen_envs: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(args)
        seen_envs.append((argv, dict(env)))
        # ``cat <report>``; argv[0] may have been resolved to a full path.
        is_cat = len(argv) == 2 and argv[1] in _REPORTS
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout=_REPORTS[argv[1]] if is_cat else "",
            stderr="",
        )

    # Same module-object form as the fixture above, for the same reason.
    monkeypatch.setattr(subprocess, "run", fake_run)

    with LocalSubprocessSandbox() as sandbox:
        sandbox.mount_repo(tmp_path)
        report = _run_stage(sandbox, tmp_path)

    # Both pytest execs (Collection_Pass, then the selected run) keep the
    # host's TMPDIR.
    pytest_execs = [(argv, env) for argv, env in seen_envs if "--json-report" in argv]
    assert [_report_file(argv) for argv, _ in pytest_execs] == [_COLLECT_PATH, _REPORT_PATH]
    for _, env in pytest_execs:
        assert env["TMPDIR"] == host_tmp
    assert report.passed == 1
    assert report.collected == 1
