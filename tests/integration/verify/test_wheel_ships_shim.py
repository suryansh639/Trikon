"""Integration test: prove the wheel ships ``_plugin_shim.py`` and the loader wires up.

Regression anchor for **Bug D** (v0.3.4): before the ``.gitignore``
negation ``!trikon/verify/_plugin_shim.py`` was added, hatchling's
VCS-aware file discovery silently dropped the shim from every published
wheel because the bare ``_*.py`` rule in ``.gitignore`` matched it.
:func:`trikon.verify.plugins._stage_shim` then hit ``FileNotFoundError``
under ``pip install trikon``, and any repo carrying ``.trikon/checks/*.py``
plugins failed with :class:`~trikon.verify.errors.PluginLoadError`.

Two test functions guard the fix:

* :func:`test_wheel_contains_and_resolves_plugin_shim` builds an
  ephemeral venv via :class:`venv.EnvBuilder`, installs the local wheel
  into it, and asserts
  ``importlib.resources.files("trikon.verify") / "_plugin_shim.py"``
  resolves to a readable file inside that venv (Requirement 2).
* :func:`test_load_and_run_plugins_end_to_end` drives
  :func:`trikon.verify.plugins.load_and_run_plugins` against a fake
  sandbox and a minimal repo containing ``.trikon/checks/no_op.py``,
  asserting the returned tuple carries one
  :class:`~trikon.evidence.report.PluginResult` with ``error is None``
  and ``findings == []`` (Requirement 3).

See ``.kiro/specs/plugin-shim-packaging/`` for the full spec (design.md
§5, §6; requirements.md §2, §3).
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

from trikon.evidence.report import EMPTY_IMPACT_SET, PluginResult
from trikon.verify.models import SandboxExecResult
from trikon.verify.plugins import load_and_run_plugins

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Wheel discovery
# ---------------------------------------------------------------------------
#
# ``__file__`` here is ``<repo>/tests/integration/verify/test_wheel_ships_shim.py``.
# ``.parents[3]`` walks up ``verify/`` -> ``integration/`` -> ``tests/`` ->
# repo root. The wheels landing pad is ``dist/`` under that root, matching
# the layout produced by ``uv build`` / ``hatch build``.
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_DIST_DIR: Path = _REPO_ROOT / "dist"

# ``importlib.resources.files("trikon.verify")`` transitively imports the
# ``trikon`` top-level package, whose ``__init__.py`` in turn imports
# :mod:`trikon.evidence.report` (which requires :mod:`pydantic`). We
# therefore install with the full dependency tree — ``--no-deps`` would
# leave the resolve one-liner raising ``ModuleNotFoundError`` for pydantic
# and mask the real question this test asks (is the shim archive entry
# shipped in the wheel and reachable via ``importlib.resources``?). This
# matches the recipe in ``design.md`` §6 verbatim.
_PIP_INSTALL_TIMEOUT_S: float = 300.0
_RESOLVE_TIMEOUT_S: float = 30.0


def _discover_newest_wheel() -> Path | None:
    """Return the newest ``dist/trikon-*.whl`` under the repo root, or ``None``.

    Newest is determined by modification time — the most recently produced
    wheel is what a developer running ``uv build`` immediately before the
    test suite would want to exercise.
    """
    if not _DIST_DIR.is_dir():
        return None
    candidates = list(_DIST_DIR.glob("trikon-*.whl"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _resolve_wheel_path(request: pytest.FixtureRequest) -> Path:
    """Pick the wheel to test against, or ``pytest.skip`` when none is available.

    Precedence:

    1. ``--wheel-path`` on the pytest CLI, if given and pointing at a
       real file.
    2. The newest ``dist/trikon-*.whl`` under the repo root.
    3. :func:`pytest.skip` with a clear message.

    Collection must always succeed even on a fresh checkout where
    ``dist/`` is empty — hence the ``skip`` rather than a hard failure.
    """
    raw_option: object = request.config.getoption("--wheel-path", default=None)
    if isinstance(raw_option, str) and raw_option:
        candidate = Path(raw_option).expanduser().resolve()
        if not candidate.is_file():
            pytest.skip(f"--wheel-path points to a missing file: {candidate}")
        return candidate

    discovered = _discover_newest_wheel()
    if discovered is None:
        pytest.skip("no wheel available at dist/trikon-*.whl; run 'uv build' first")
    return discovered


@pytest.fixture
def wheel_path(request: pytest.FixtureRequest) -> Path:
    """Locate the wheel to test — CLI override, then dist auto-discovery."""
    return _resolve_wheel_path(request)


# ---------------------------------------------------------------------------
# Cross-platform venv interpreter path
# ---------------------------------------------------------------------------


def _venv_python(venv_dir: Path) -> Path:
    """Return the interpreter path inside a venv, matching the host OS layout."""
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


# ---------------------------------------------------------------------------
# Requirement 2 — wheel installs and `importlib.resources` resolves the shim
# ---------------------------------------------------------------------------


def test_wheel_contains_and_resolves_plugin_shim(
    wheel_path: Path,
    tmp_path: Path,
) -> None:
    """Install the wheel into an ephemeral venv and resolve the shim resource.

    Concretely (design.md §6, Requirement 2):

    1. Build a fresh venv via :class:`venv.EnvBuilder` under ``tmp_path``.
    2. ``pip install <wheel>`` into that venv (full deps — the top-level
       ``trikon`` package init imports pydantic, so ``--no-deps`` would
       mask the packaging question with a stale-import failure).
    3. Run a Python one-liner that resolves
       ``files("trikon.verify") / "_plugin_shim.py"`` via
       :mod:`importlib.resources` and prints the first bytes of the shim
       to stdout. Non-empty stdout proves the archive entry exists and
       is readable — the exact failure mode that Bug D produced.
    """
    venv_dir = tmp_path / "venv"
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(str(venv_dir))

    venv_python = _venv_python(venv_dir)
    assert venv_python.is_file(), f"venv python not created at {venv_python}"

    install_result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(venv_python), "-m", "pip", "install", str(wheel_path)],
        check=True,
        text=True,
        capture_output=True,
        timeout=_PIP_INSTALL_TIMEOUT_S,
    )
    # ``check=True`` already turns non-zero exits into
    # ``CalledProcessError``. We keep the assertion for a clean
    # diagnostic in the (impossible) event that a future subprocess
    # change breaks that invariant.
    assert install_result.returncode == 0, install_result.stderr

    one_liner = (
        "from importlib.resources import files, as_file; "
        "p = files('trikon.verify') / '_plugin_shim.py'; "
        "assert p.is_file(), f'shim not found: {p}'; "
        "print(as_file(p).__enter__().read_bytes()[:100]"
        ".decode('utf-8', errors='replace'))"
    )
    # ``cwd=tmp_path`` prevents Python from auto-prepending pytest's
    # working directory (the Trikon repo root) to ``sys.path`` in the
    # subprocess. Without this, the local editable ``trikon/`` source
    # tree shadows the venv-installed wheel and the test measures the
    # wrong installation, defeating the whole point of the ephemeral
    # venv. ``tmp_path`` is guaranteed to have no ``trikon/`` directory.
    resolve_result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(venv_python), "-c", one_liner],
        check=True,
        text=True,
        capture_output=True,
        timeout=_RESOLVE_TIMEOUT_S,
        cwd=str(tmp_path),
    )
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert resolve_result.stdout.strip(), (
        f"expected a non-empty shim preview on stdout; stderr={resolve_result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Requirement 3 — `load_and_run_plugins` returns a clean PluginResult
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Structural stand-in for :class:`trikon.verify.sandbox.LocalDockerSandbox`.

    The plugin loader only ever calls :meth:`exec` on its sandbox
    argument (see the ``LocalDockerSandbox`` :class:`Protocol` in
    :mod:`trikon.verify.plugins`), so we implement that one method and
    nothing else. The signature mirrors the Protocol exactly — keyword
    arguments, defaults, and return type all match — so :mod:`mypy`
    accepts an instance as a valid ``LocalDockerSandbox``.

    Behavior:

    * The ``cat /workspace/tmp/plugin_output.json`` read gets a stubbed
      ``{"findings": []}`` payload, which is what the loader parses into
      :attr:`~trikon.evidence.report.PluginResult.findings`.
    * Every other exec (``mkdir``, ``python -c`` writes, ``python
      /workspace/tmp/_plugin_shim.py``) returns a zero exit code with
      empty stdout, matching the sandbox contract that non-error paths
      never raise (design.md §3.3).
    """

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str = "/workspace/repo",
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        # ``workdir``, ``timeout_seconds`` and ``env`` are part of the
        # Protocol surface but not observed by the fake — the test does
        # not need to distinguish call sites by those.
        _ = workdir
        _ = timeout_seconds
        _ = env
        if argv and argv[0] == "cat":
            return SandboxExecResult(
                exit_code=0,
                stdout='{"findings": []}',
                stderr="",
                duration_ms=0,
                timed_out=False,
            )
        return SandboxExecResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0,
            timed_out=False,
        )


def test_load_and_run_plugins_end_to_end(tmp_path: Path) -> None:
    """`load_and_run_plugins` returns a clean :class:`PluginResult` on the happy path.

    Wires up a minimal ``.trikon/checks/no_op.py`` plugin whose
    ``check(ctx)`` returns an empty list, then hands a
    :class:`_FakeSandbox` to
    :func:`trikon.verify.plugins.load_and_run_plugins`. The fake sandbox
    returns ``{"findings": []}`` for the shim's output-read call and
    ``exit_code=0`` for every other exec, so the loader's happy path is
    exercised end-to-end without spinning up Docker (Requirement 3.1 —
    ``load_and_run_plugins`` must not raise ``PluginLoadError`` on the
    shim staging path — and Requirement 3.3 —
    :attr:`PluginResult.error` is ``None`` and
    :attr:`PluginResult.findings` is empty).
    """
    fake_repo = tmp_path / "fake_repo"
    checks_dir = fake_repo / ".trikon" / "checks"
    checks_dir.mkdir(parents=True)
    (checks_dir / "no_op.py").write_text(
        "def check(ctx):\n    return []\n",
        encoding="utf-8",
    )

    sandbox = _FakeSandbox()
    results: tuple[PluginResult, ...] = load_and_run_plugins(
        sandbox,
        fake_repo,
        EMPTY_IMPACT_SET,
    )

    assert len(results) == 1, f"expected exactly one PluginResult, got {len(results)}"
    result = results[0]
    assert isinstance(result, PluginResult), f"expected PluginResult, got {type(result).__name__}"
    assert result.error is None, f"expected error=None, got {result.error!r}"
    assert result.findings == [], f"expected empty findings, got {result.findings!r}"
