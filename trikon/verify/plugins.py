"""Discover, load, and execute repo-defined ``.trikon/checks/*.py`` plugins.

Public entry point: :func:`load_and_run_plugins`. Every ``.trikon/checks/*.py``
file that does not start with ``_`` is imported and invoked once per verdict.
Import happens **inside** the verification sandbox (never in the host process)
so a repo-supplied plugin cannot exfiltrate host state or reach network
resources — the sandbox contract from ``design.md §5`` still applies.

Plugin authors write the trikon-facing surface:

    # .trikon/checks/no_direct_sql.py
    from trikon.verify.plugins import CheckContext
    from trikon.verify.static_checks import Finding

    def check(ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for f in ctx.changed_files:
            if b"cursor.execute(" in ctx.read_bytes(f):
                findings.append(Finding(
                    tool="no_direct_sql", rule_id="NDS001",
                    severity="warning", file_path=str(f),
                    line=1, column=None,
                    message="Use the ORM, not raw SQL.",
                    is_new=True,
                ))
        return findings

Failure discipline (Requirements 4.1 / 4.2 / 4.3, design.md §8, §12):

* **Per-plugin failure** — import errors, missing / non-callable ``check``,
  ``async def check``, runtime exceptions, and per-plugin timeouts — are
  recorded on the returned :class:`PluginResult.error` field. Execution
  continues with the remaining plugins. Nothing raises past this module.
* **Infrastructure failure** — the ``.trikon/checks/`` directory is
  unreadable mid-glob, the shim cannot be staged into the sandbox, the
  input JSON cannot be written, or the output JSON is missing / malformed
  — raises :class:`PluginLoadError`. The SDK boundary translates that
  into a ``require_human`` verdict per Requirement 6.2.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING

from trikon.evidence.report import ImpactSet, PluginResult
from trikon.verify.errors import PluginLoadError

if TYPE_CHECKING:
    from typing import Protocol

    from trikon.verify.models import SandboxExecResult

    class LocalDockerSandbox(Protocol):
        """Structural type for the sandbox parameter (see design.md §3.3).

        The plugin loader talks to the sandbox only through ``exec``. Rather
        than importing the concrete class from ``trikon.verify.sandbox`` —
        which pulls the Docker client into the import graph and would force
        every ``import trikon.verify.plugins`` to eagerly connect to a
        Docker daemon — we describe the contract structurally.

        Any object that satisfies design.md §3.3 (including the concrete
        ``trikon.verify.sandbox.LocalDockerSandbox`` shipped by Task 5.1
        and any in-test double) satisfies this protocol.
        """

        def exec(
            self,
            argv: tuple[str, ...],
            *,
            workdir: str = "/workspace/repo",
            timeout_seconds: float | None = None,
            env: tuple[tuple[str, str], ...] = (),
        ) -> SandboxExecResult: ...


# ---------------------------------------------------------------------------
# Sandbox tmpfs layout (mirrors ``_plugin_shim.py`` defaults; design.md §5.2)
# ---------------------------------------------------------------------------
#
# The repo bind-mount at ``/workspace/repo`` is read-only, so the shim, the
# input JSON, and the output JSON all live under the writable tmpfs at
# ``/workspace/tmp`` (see ``design.md §5.2`` for the container spec).

_SANDBOX_TMP_DIR = "/workspace/tmp"
_SANDBOX_SHIM_PATH = f"{_SANDBOX_TMP_DIR}/_plugin_shim.py"
_SANDBOX_INPUT_PATH = f"{_SANDBOX_TMP_DIR}/plugin_input.json"
_SANDBOX_OUTPUT_PATH = f"{_SANDBOX_TMP_DIR}/plugin_output.json"

# Env vars the shim honors as an explicit override of its default paths.
# Setting them here (a) documents the shim's contract at the call site and
# (b) keeps ``_plugin_shim.py`` and this loader in lock-step when we later
# relocate the tmpfs mount point.
_SHIM_ENV: tuple[tuple[str, str], ...] = (
    ("TRIKON_PLUGIN_INPUT_PATH", _SANDBOX_INPUT_PATH),
    ("TRIKON_PLUGIN_OUTPUT_PATH", _SANDBOX_OUTPUT_PATH),
)

# Timeouts on the fixed-cost supporting exec calls (mkdir, staging, JSON I/O).
# Only the plugin exec itself uses the caller-supplied
# ``per_plugin_timeout_seconds``; the sandbox round-trips below are so cheap
# that a multi-second budget is a comfortable ceiling.
_MKDIR_TIMEOUT_S = 5.0
_STAGE_TIMEOUT_S = 10.0
_IO_TIMEOUT_S = 5.0


@dataclass
class CheckContext:
    """State passed to each custom check on the *host* / plugin-author side.

    The verification sandbox reconstructs an equivalent object locally (see
    :mod:`trikon.verify._plugin_shim`) from the JSON wire payload — plugin
    authors write against this dataclass, but the runtime instance that
    reaches ``check(ctx)`` is the shim's local reconstruction. Kept here as
    the canonical authoring surface for repo-defined plugins and referenced
    by ``design.md §8 note 2``.
    """

    repo_path: Path
    changed_files: list[Path]
    metadata: dict[str, str] = field(default_factory=dict)

    def read_bytes(self, file_path: Path) -> bytes:
        """Read a file relative to :attr:`repo_path`."""
        return (self.repo_path / file_path).read_bytes()


def load_and_run_plugins(
    sandbox: LocalDockerSandbox,
    repo_path: Path,
    impact: ImpactSet,
    *,
    per_plugin_timeout_seconds: float = 30.0,
) -> tuple[PluginResult, ...]:
    """Discover, import, and invoke every ``.trikon/checks/*.py`` plugin.

    Algorithm (design.md §8):

    1. Discover ``sorted((repo_path / ".trikon" / "checks").glob("*.py"))``,
       filtered to files that do not start with ``_``. A missing
       ``.trikon/checks/`` directory yields an empty tuple with no
       exception raised.
    2. Stage :mod:`trikon.verify._plugin_shim` into the sandbox tmpfs at
       ``/workspace/tmp/_plugin_shim.py``. The bind-mount at
       ``/workspace/repo`` is read-only (design.md §5.2), so the shim must
       live in the writable tmpfs; the source bytes are resolved via
       ``importlib.resources.files("trikon.verify") / "_plugin_shim.py"``
       (through ``as_file`` so a zipped-wheel install still works). We
       never read the shim source from the target repo — see
       ``design.md §8 note 1`` for the security rationale.
    3. For each plugin file, serialize a JSON input payload
       (``repo_path``, ``plugin_rel_path``, ``impact.changed_files``) into
       ``/workspace/tmp/plugin_input.json``, invoke
       ``python /workspace/tmp/_plugin_shim.py`` under
       ``per_plugin_timeout_seconds``, then read
       ``/workspace/tmp/plugin_output.json`` back via ``cat``.

    Per-plugin failures (import error, ``async def check``, runtime
    exception, timeout) are recorded on
    :attr:`PluginResult.error` and execution proceeds to the next plugin.
    Infrastructure failures (missing shim, unreadable output file,
    malformed output JSON) raise :class:`PluginLoadError`.
    """
    checks_dir = repo_path / ".trikon" / "checks"
    if not checks_dir.is_dir():
        return ()

    plugin_files = sorted(p for p in checks_dir.glob("*.py") if not p.name.startswith("_"))
    if not plugin_files:
        return ()

    _stage_shim(sandbox)

    results: list[PluginResult] = []
    for plugin_file in plugin_files:
        plugin_rel_path = plugin_file.relative_to(repo_path).as_posix()
        results.append(
            _run_one(
                sandbox,
                plugin_rel_path,
                impact,
                timeout_seconds=per_plugin_timeout_seconds,
            )
        )
    return tuple(results)


def _stage_shim(sandbox: LocalDockerSandbox) -> None:
    """Copy ``_plugin_shim.py`` into the sandbox tmpfs.

    Called once per :func:`load_and_run_plugins` invocation (before the
    per-plugin loop). The shim is a few kilobytes of stdlib-only Python, so
    a single ``python -c 'base64.b64decode(...)'`` write per verdict is
    negligible cost. Using ``base64`` avoids every shell-quoting corner
    case a naive ``cat > file`` heredoc would introduce.

    Raises:
        PluginLoadError: If ``mkdir -p`` on the tmpfs directory fails or
            if the ``python -c`` write inside the sandbox reports a
            non-zero exit code.
    """
    mkdir_result = sandbox.exec(
        ("mkdir", "-p", _SANDBOX_TMP_DIR),
        timeout_seconds=_MKDIR_TIMEOUT_S,
    )
    if mkdir_result.exit_code != 0:
        raise PluginLoadError(
            "failed to create sandbox tmpfs directory "
            f"{_SANDBOX_TMP_DIR!r}: {mkdir_result.stderr[:2000]}"
        )

    # ``importlib.resources.as_file`` gives us a real filesystem path even
    # when ``trikon`` is installed from a zipped wheel — the shim gets
    # extracted to a temp file for the duration of this context manager.
    shim_resource = files("trikon.verify") / "_plugin_shim.py"
    with as_file(shim_resource) as shim_path:
        shim_bytes = shim_path.read_bytes()

    b64_shim = base64.b64encode(shim_bytes).decode("ascii")
    write_result = sandbox.exec(
        (
            "python",
            "-c",
            (
                "import base64,pathlib;"
                f"pathlib.Path({_SANDBOX_SHIM_PATH!r})"
                f".write_bytes(base64.b64decode({b64_shim!r}))"
            ),
        ),
        timeout_seconds=_STAGE_TIMEOUT_S,
    )
    if write_result.exit_code != 0:
        raise PluginLoadError(
            f"failed to stage plugin shim into sandbox: {write_result.stderr[:2000]}"
        )


def _run_one(
    sandbox: LocalDockerSandbox,
    plugin_rel_path: str,
    impact: ImpactSet,
    *,
    timeout_seconds: float,
) -> PluginResult:
    """Execute a single plugin file inside the sandbox and hydrate its result.

    Wire contract (design.md §8):

    * Input: :attr:`_SANDBOX_INPUT_PATH` receives a JSON object with
      ``repo_path``, ``plugin_rel_path``, and ``impact.changed_files``.
      The shim reconstructs its local ``_CheckContext`` from that payload.
    * Output: :attr:`_SANDBOX_OUTPUT_PATH` receives either
      ``{"findings": [...]}`` on success or ``{"error": "..."}`` on any
      plugin-level failure. The shim always exits 0 (see
      ``_plugin_shim.py`` module docstring); a non-zero exit from ``cat``
      here means the file itself is missing, which is an infrastructure
      failure and raises :class:`PluginLoadError`.

    Timeouts: when the shim exec times out, the returned
    :class:`SandboxExecResult` carries ``timed_out=True`` and we synthesize
    a ``PluginResult`` whose ``error`` string names the concrete
    ``timeout_seconds`` budget the plugin blew through. Timeouts do **not**
    raise :class:`~trikon.verify.errors.SandboxTimeoutError` past this
    module boundary — per Requirement 2.2 (design.md §9) they surface as
    ``PluginResult.error`` strings only, so a runaway plugin degrades the
    verdict rather than aborting the whole verification run.

    Async rejection contract: when the plugin declares ``async def check``,
    the shim (:mod:`trikon.verify._plugin_shim`) writes
    ``{"error": "async plugins not supported in Phase 2"}`` into the
    output JSON and exits 0. The ``if "error" in parsed`` branch below
    propagates that string into :attr:`PluginResult.error` **verbatim** —
    do not reword it here or the async-rejection assertion in
    Requirement 4.3 stops matching the wire payload.
    """
    input_payload: dict[str, object] = {
        "repo_path": "/workspace/repo",
        "plugin_rel_path": plugin_rel_path,
        "impact": {"changed_files": list(impact.changed_files)},
    }
    input_json = json.dumps(input_payload)
    b64_input = base64.b64encode(input_json.encode("utf-8")).decode("ascii")

    write_input = sandbox.exec(
        (
            "python",
            "-c",
            (
                "import base64,pathlib;"
                f"pathlib.Path({_SANDBOX_INPUT_PATH!r})"
                f".write_bytes(base64.b64decode({b64_input!r}))"
            ),
        ),
        timeout_seconds=_IO_TIMEOUT_S,
    )
    if write_input.exit_code != 0:
        raise PluginLoadError(
            f"failed to write plugin input JSON for {plugin_rel_path}: {write_input.stderr[:2000]}"
        )

    exec_result = sandbox.exec(
        ("python", _SANDBOX_SHIM_PATH),
        timeout_seconds=timeout_seconds,
        env=_SHIM_ENV,
    )
    if exec_result.timed_out:
        # Timeouts do NOT raise SandboxTimeoutError past the module
        # boundary per Requirement 2.2 (design.md §9). They surface as
        # PluginResult.error strings only, so the remaining plugins in
        # this verdict still get their turn.
        return PluginResult(
            plugin=plugin_rel_path,
            findings=[],
            error=f"plugin exceeded {timeout_seconds}s timeout",
        )

    output = sandbox.exec(
        ("cat", _SANDBOX_OUTPUT_PATH),
        timeout_seconds=_IO_TIMEOUT_S,
    )
    if output.exit_code != 0:
        raise PluginLoadError(
            f"failed to read plugin output JSON for {plugin_rel_path}: {output.stderr[:2000]}"
        )

    try:
        parsed_raw = json.loads(output.stdout)
    except json.JSONDecodeError as exc:
        raise PluginLoadError(
            f"failed to parse plugin output JSON for {plugin_rel_path}: {exc!r}"
        ) from exc
    if not isinstance(parsed_raw, dict):
        raise PluginLoadError(f"plugin output must be a JSON object for {plugin_rel_path}")
    parsed: dict[str, object] = {str(k): v for k, v in parsed_raw.items()}

    if "error" in parsed:
        return PluginResult(
            plugin=plugin_rel_path,
            findings=[],
            error=str(parsed["error"]),
        )

    findings_raw = parsed.get("findings", [])
    if not isinstance(findings_raw, list):
        raise PluginLoadError(
            f"plugin output 'findings' must be a JSON array for {plugin_rel_path}"
        )

    findings: list[dict[str, object]] = []
    for entry in findings_raw:
        if isinstance(entry, dict):
            findings.append({str(k): v for k, v in entry.items()})
    return PluginResult(
        plugin=plugin_rel_path,
        findings=findings,
        error=None,
    )


__all__ = [
    "CheckContext",
    "load_and_run_plugins",
]
