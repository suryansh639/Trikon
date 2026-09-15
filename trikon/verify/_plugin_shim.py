"""In-sandbox shim that imports and executes a single repo-defined check plugin.

This module ships with the installed ``trikon`` package but is **not** imported
by the rest of the Trikon codebase. Instead ``load_and_run_plugins`` (see
``trikon/verify/plugins.py`` and design.md §8) bind-mounts this file into the
verification sandbox at ``/workspace/repo/.trikon/_plugin_shim.py`` and invokes
it as a standalone script:

    python /workspace/repo/.trikon/_plugin_shim.py

Wire contract (design.md §8, Requirements 4.1 / 4.2 / 4.3)
----------------------------------------------------------
Input  — ``/workspace/tmp/plugin_input.json`` (JSON object):

    {
        "repo_path":       "/workspace/repo",
        "plugin_rel_path": ".trikon/checks/no_direct_sql.py",
        "impact":          {"changed_files": ["src/api/payments.py", ...], ...}
    }

Output — ``/workspace/tmp/plugin_output.json`` (JSON object):

    on success:  {"findings": [{"path": str, "line": int,
                                 "rule_id": str, "message": str,
                                 "severity": str}, ...]}
    on failure:  {"error": "<repr of the exception or a shim-supplied string>"}

The shim **always exits with code 0** on every path. The host-side caller
distinguishes success from failure by reading the JSON payload, not the exit
code — this keeps ``LocalDockerSandbox.exec`` from ever raising for a plugin
fault (Requirement 4.2, "record the failure captured in the ``error`` field
and continue executing the remaining plugins").

Security invariants
-------------------
* **Standard-library-only imports.** The sandbox base image
  (``suryansh639/trikon:0.3.6``) may not have the ``trikon`` package installed
  and the shim must run against a bare ``python:3.11-slim`` layer. Everything
  the shim needs — ``importlib.util``, ``inspect``, ``json``, ``sys``,
  ``pathlib``, ``dataclasses``, ``collections.abc``, ``typing`` — is stdlib.

* **Never read shim source from the target repo.** The host-side loader
  bind-mounts this file from the installed ``trikon.verify`` package location
  (via ``importlib.resources.files("trikon.verify") / "_plugin_shim.py"``);
  the shim at ``.trikon/_plugin_shim.py`` inside the target repo is never
  trusted (design.md §8 note 1). This defends against a malicious repo
  overriding the loader.

* **Local ``_CheckContext`` reconstruction.** ``trikon.verify.plugins`` defines
  a ``CheckContext`` dataclass on the host, but the shim cannot import it
  (see previous invariant). Instead the shim reconstructs an equivalent
  dataclass locally whose shape matches the plugin surface documented in
  design.md §3.5 / §8 note 2: ``repo_path: Path``,
  ``changed_files: tuple[str, ...]``, ``read_bytes: Callable[[str], bytes]``.
  Plugin authors write against the trikon dataclass; the wire format across
  the sandbox boundary is JSON.

* **Finding wire format is ``dict[str, str | int]``.** The five fields
  (``path``, ``line``, ``rule_id``, ``message``, ``severity``) are the JSON
  wire projection of whatever Finding-shaped object the plugin returned.
  Using a dict rather than a Pydantic / dataclass model is a deliberate
  boundary decision: the shim runs on stdlib only, so it cannot construct
  ``trikon.verify.static_checks.Finding``; the host-side loader
  (``trikon/verify/plugins.py``, Task 8.2) re-hydrates the ``PluginResult``
  from this JSON.

Failure classification
----------------------
* ``inspect.iscoroutinefunction(check)`` is ``True``  → ``{"error":
  "async plugins not supported in Phase 2"}`` (Requirement 4.3).
* ``check`` attribute missing or non-callable                → ``{"error":
  "plugin missing callable 'check' function"}``.
* Any other exception raised by import, introspection, or the ``check``
  call itself                                                → ``{"error":
  "<repr(exc)>"}`` (Requirement 4.2).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

# ---------------------------------------------------------------------------
# Wire paths. Fixed to the sandbox tmpfs mount described in design.md §5.2.
#
# The two ``TRIKON_PLUGIN_*`` environment variables exist only so host-side
# tests can exercise the shim outside a container. Production callers
# (``load_and_run_plugins`` in ``trikon/verify/plugins.py``, Task 8.2) always
# rely on the defaults and never set these variables — the shim never
# broadcasts their existence to the plugin author.
# ---------------------------------------------------------------------------
_INPUT_PATH = Path(os.environ.get("TRIKON_PLUGIN_INPUT_PATH", "/workspace/tmp/plugin_input.json"))
_OUTPUT_PATH = Path(
    os.environ.get("TRIKON_PLUGIN_OUTPUT_PATH", "/workspace/tmp/plugin_output.json")
)


@dataclass(frozen=True)
class _CheckContext:
    """Sandbox-local reconstruction of ``trikon.verify.plugins.CheckContext``.

    Kept structurally compatible with the host-side dataclass so a plugin
    author's ``def check(ctx: CheckContext) -> list[Finding]`` runs unchanged;
    see design.md §8 note 2. Intentionally *not* a subclass or import of the
    host type — the shim runs stdlib-only.
    """

    repo_path: Path
    changed_files: tuple[str, ...]
    read_bytes: Callable[[str], bytes]


def _write_output(payload: dict[str, object]) -> None:
    """Serialize ``payload`` to ``_OUTPUT_PATH`` (creates parent dirs)."""
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps(payload), encoding="utf-8")


def _serialize_finding(finding: object) -> dict[str, str | int]:
    """Project a plugin-returned Finding-like object onto the wire schema.

    Duck-typed on purpose: the shim cannot ``isinstance``-check against
    ``trikon.verify.static_checks.Finding`` without importing ``trikon``.
    Accepts either ``path`` (the shim wire field) or ``file_path`` (the
    trikon Finding attribute) as the file path source, so plugin authors
    returning the trikon Finding get the right projection automatically.
    """
    path_val: object = getattr(finding, "path", None)
    if path_val is None:
        path_val = getattr(finding, "file_path", "")

    line_val: object = getattr(finding, "line", 0)
    try:
        line_int = int(cast("int | str", line_val))
    except (TypeError, ValueError):
        line_int = 0

    return {
        "path": str(path_val),
        "line": line_int,
        "rule_id": str(getattr(finding, "rule_id", "")),
        "message": str(getattr(finding, "message", "")),
        "severity": str(getattr(finding, "severity", "info")),
    }


def _make_read_bytes(repo_path: Path) -> Callable[[str], bytes]:
    """Build a closure over ``repo_path`` for the ``ctx.read_bytes`` field.

    Matches the shape declared in ``trikon.verify.plugins.CheckContext``:
    reads a repo-relative path and returns raw bytes. Under the sandbox
    security model the repo is bind-mounted read-only at ``/workspace/repo``
    so a plugin cannot mutate the host filesystem through this callable.
    """

    def read_bytes(rel_path: str) -> bytes:
        return (repo_path / rel_path).read_bytes()

    return read_bytes


def _load_input() -> dict[str, object] | None:
    """Read + parse ``_INPUT_PATH``. On any I/O or shape failure, emit an
    error payload and return ``None`` so the caller can bail cleanly.
    """
    try:
        raw = _INPUT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _write_output({"error": f"failed to read plugin input: {exc!r}"})
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _write_output({"error": f"failed to parse plugin input JSON: {exc!r}"})
        return None
    if not isinstance(parsed, dict):
        _write_output({"error": "plugin input must be a JSON object"})
        return None
    return cast("dict[str, object]", parsed)


def _load_plugin_module(plugin_path: Path) -> object | None:
    """Import the plugin file. Emits an error payload on failure and returns
    ``None``; on success returns the loaded module object.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            f"trikon_plugin_{plugin_path.stem}", plugin_path
        )
        if spec is None or spec.loader is None:
            _write_output({"error": f"could not load plugin spec for {plugin_path.name}"})
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        _write_output({"error": repr(exc)})
        return None


def _run() -> None:
    """Main body — see module docstring for the input / output contract."""
    payload = _load_input()
    if payload is None:
        return

    repo_path_raw = payload.get("repo_path")
    plugin_rel_path_raw = payload.get("plugin_rel_path")
    impact_raw = payload.get("impact")

    if not isinstance(repo_path_raw, str) or not isinstance(plugin_rel_path_raw, str):
        _write_output({"error": "plugin input missing 'repo_path' or 'plugin_rel_path'"})
        return
    if not isinstance(impact_raw, dict):
        _write_output({"error": "plugin input 'impact' must be a JSON object"})
        return
    impact = cast("dict[str, object]", impact_raw)

    repo_path = Path(repo_path_raw)
    plugin_path = repo_path / plugin_rel_path_raw

    module = _load_plugin_module(plugin_path)
    if module is None:
        return

    check = getattr(module, "check", None)
    if check is None or not callable(check):
        _write_output({"error": "plugin missing callable 'check' function"})
        return
    if inspect.iscoroutinefunction(check):
        _write_output({"error": "async plugins not supported in Phase 2"})
        return

    changed_files_raw = impact.get("changed_files", ())
    if isinstance(changed_files_raw, (list, tuple)):
        changed_files = tuple(str(f) for f in cast("list[object]", changed_files_raw))
    else:
        changed_files = ()
    ctx = _CheckContext(
        repo_path=repo_path,
        changed_files=changed_files,
        read_bytes=_make_read_bytes(repo_path),
    )

    check_fn = cast("Callable[[_CheckContext], object]", check)
    try:
        raw_findings = check_fn(ctx)
    except Exception as exc:
        _write_output({"error": repr(exc)})
        return

    findings: list[dict[str, str | int]] = []
    if isinstance(raw_findings, (list, tuple)):
        for f in cast("list[object]", list(raw_findings)):
            findings.append(_serialize_finding(f))
    _write_output({"findings": cast("list[object]", findings)})


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:  # last-resort catch-all — shim MUST always exit 0.
        _write_output({"error": f"shim internal failure: {exc!r}"})
    sys.exit(0)
