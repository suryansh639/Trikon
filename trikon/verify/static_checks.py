"""Run ruff and mypy against a change and diff findings against a baseline.

This module implements :func:`run_static_checks`, the head-side entry point
called by :func:`trikon.verify.runner.run_verification`. It executes each
:class:`~trikon.verify.models.StaticTool` inside a :class:`LocalDockerSandbox`,
parses the raw stdout into structured findings, and computes ``is_new`` for
every finding by set-subtraction on the ``(path, line, rule_id)`` triple.

Signature
---------

The public shape is fixed by ``design.md §3.4``::

    run_static_checks(sandbox, conn, impact, *, repo_path, base_sha, head_sha,
                      tools=DEFAULT_STATIC_TOOLS) -> StaticReport

Algorithm (5 steps, design.md §7)
---------------------------------

1. **Version capture** — for each tool, execute ``tool.version_command``
   (10 s timeout) and record ``stdout.strip()`` as the tool version. This
   value is the discriminator on the ``static_baseline`` cache key
   (Requirement 3.3), so a ``pyproject.toml`` bump of ``ruff==0.7.4`` →
   ``ruff==0.8.0`` invalidates every prior cache row on the next lookup.

2. **Baseline resolve** — for each tool, look up the ``static_baseline``
   row keyed by ``(base_sha, tool.name, tool_version)``. On a hit, decode
   the cached findings JSON and reduce to ``{(path, line, rule_id)}`` for
   the ``is_new`` set-subtraction. On a miss, materialize the base tree
   with ``git worktree add --detach`` on the host, invoke the tool
   against the worktree, persist the parsed findings back to
   ``static_baseline``, and remove the worktree.

3. **Head-side run** — for each tool, expand the ``{files}`` sentinel in
   ``argv_template`` with ``impact.changed_files`` and invoke it inside the
   sandbox. Ruff (``parse_json=True``) emits ``--output-format=json`` and
   flows through :func:`_parse_ruff_json`; mypy (``parse_json=False``)
   emits line-per-diagnostic text and flows through :func:`_parse_mypy_text`.

4. **is_new diff** — each parsed head finding is keyed by
   ``(path, line, rule_id)`` and checked for membership in ``base_keys``.
   Message text deliberately does not participate: ruff and mypy sometimes
   reformat messages between patch versions (quoting style, snippet width)
   without the underlying diagnostic changing, and Requirement 3.3 is the
   right place to invalidate on tool-version bumps — not per-finding on
   the message discriminator.

5. **Report assembly** — a single :class:`StaticReport` carrying
   ``tools_run`` in the order the tools were invoked, the three aggregate
   counters (``new_errors``, ``new_warnings``, ``preexisting_errors``),
   and the flat ``findings`` list in tool-then-emission order.

Baseline execution locus — Phase-2 pragmatic tradeoff
------------------------------------------------------

The design says the baseline tool runs "inside the sandbox" — the honest
form of that is: bind-mount the base worktree at a second path inside the
container and re-invoke the tool there. Task 5.1's
:class:`LocalDockerSandbox` only mounts the head-side repo (one bind
mount at ``/workspace/repo``); adding a second bind mount after the
container is already running is not supported by the ``docker-py`` API
without recreating the container. Phase 3 will land a multi-mount
sandbox and move the baseline execution back inside; until then, Task 7.2
runs the baseline tool on the host directly with
:func:`subprocess.run`, using the pinned dev-dependency versions of
``ruff`` / ``mypy`` from ``pyproject.toml``. This keeps the ``(base_sha,
tool, tool_version)`` cache-key discipline intact — Requirement 3.3 is
about the tool version, not the execution locus. See the
``# NOTE: baseline tool runs on host …`` comment on
:func:`_run_baseline_tool_on_host` for the raise-site anchor.

Error handling (design.md §9.1)
-------------------------------

Every raise site under this module is :class:`StaticCheckError`. The
sandbox itself wraps its own failures as
:class:`~trikon.verify.errors.SandboxExecError` /
:class:`~trikon.verify.errors.SandboxUnavailableError`, so those pass
through this module unchanged. Timeouts never raise —
:class:`~trikon.verify.models.SandboxExecResult` carries ``timed_out=True``
and this module treats such results as if the tool emitted empty output,
matching the never-fail-open contract of Requirement 6.

Task 7.2 adds three new raise families, all wrapped as
:class:`StaticCheckError`: :class:`subprocess.CalledProcessError` /
:class:`subprocess.TimeoutExpired` / :class:`FileNotFoundError` from the
host ``git worktree`` and baseline-tool invocations;
:class:`sqlite3.Error` from the ``static_baseline`` cache read or write;
and :class:`json.JSONDecodeError` from a corrupt cached findings payload
or from ruff's ``--output-format=json`` stdout on the BASE run.

Circular-import discipline
--------------------------

:class:`LocalDockerSandbox` is imported only under ``TYPE_CHECKING`` so this
module can be exercised in unit tests with a fake sandbox stand-in without
paying the Docker-client import tax and without creating a runtime cycle
back through :mod:`trikon.verify.sandbox`.

Type discipline
---------------

No ``dict[str, Any]`` appears on any surface. Parser helpers return
``list[dict[str, str | int]]`` — the union covers the scalar payload
shape (``path``, ``rule_id``, ``message``, ``severity`` as strings; ``line``
as int). :attr:`StaticReport.findings` is ``list[dict[str, object]]`` (from
:mod:`trikon.evidence.report`), so this module builds fresh dicts of that
wider shape when it stamps in ``is_new`` and ``tool``. The decoded
``json.loads`` payload is typed ``object`` and narrowed with
``isinstance`` checks so nothing of type ``Any`` escapes.

Validates: Requirements 3.1, 3.2, 3.3, 6.1.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from trikon.evidence.report import ImpactSet, StaticReport
from trikon.verify.errors import StaticCheckError
from trikon.verify.models import DEFAULT_STATIC_TOOLS, StaticTool

if TYPE_CHECKING:
    # The sandbox module lives in trikon.verify.sandbox; the import guard
    # prevents a circular dependency (sandbox.py depends on models.py, which
    # would otherwise re-enter this module through the __init__.py re-exports
    # if a test module imports the sandbox before static_checks). At
    # runtime the annotation is a forward reference resolved from the string
    # form, so the guard has zero cost past import time. Wave-2 widened the
    # ``sandbox`` parameter to the :data:`~trikon.verify.sandbox.Sandbox`
    # union so :func:`run_static_checks` accepts either the Docker-backed
    # backend or the ``--no-sandbox`` subprocess backend polymorphically.
    from trikon.verify.sandbox import Sandbox

__all__ = [
    "run_static_checks",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
#
# The 10 s ceiling on the version-capture ``exec`` matches design.md §7
# Step 1 exactly. It is deliberately short: a runaway ``ruff --version`` (or
# ``mypy --version``) is either a broken sandbox image or a broken host
# Docker daemon, and both cases should surface fast rather than eat the
# per-verdict deadline. Task 9.2 threads the caller's remaining budget into
# every tool exec; Task 7.1 does not enforce a head-run timeout here (the
# head-run ``exec`` uses the sandbox's per-call default of ``None``).

_TOOL_VERSION_TIMEOUT_SECONDS: float = 10.0

# The host-side baseline-tool subprocess ceiling. Long enough that ruff /
# mypy can crawl a few dozen changed files on a cold Python cache, short
# enough that a hung tool (Windows AV, corrupted mypy cache) surfaces as
# a ``StaticCheckError`` inside the same verdict window rather than
# stalling the outer deadline in :func:`trikon.verify.runner.run_verification`.
# The value is process-local; it does not participate in the cache key.
_HOST_TOOL_TIMEOUT_SECONDS: float = 60.0

# Bound on git worktree management. ``git worktree add`` on a healthy
# repository is a low-single-digit-seconds operation; ``git worktree
# remove --force`` is even cheaper. A 30 s ceiling is orders of magnitude
# above the observed cost and only fires if the working tree is genuinely
# stuck (locked index, filesystem stall).
_GIT_WORKTREE_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Mypy line format
# ---------------------------------------------------------------------------
#
# Design.md §3.4 pins the mypy invocation to ``--no-color-output
# --show-column-numbers``, so diagnostic lines look like::
#
#     src/api/payments.py:42:5: error: Message text  [rule-id]
#
# The column and the ``[rule-id]`` trailer are optional: some mypy diagnostic
# categories (``import``, ``syntax``) do not emit a column even with the flag
# present, and older mypy releases (< 1.7) elide the ``[rule-id]`` for a
# handful of internal error codes. Both are captured as optional groups so
# the regex never rejects a legitimate diagnostic line.
#
# Trailing summary lines like ``Found 1 error in 1 file (checked 1 source
# file)`` and blank lines have no ``line:col`` position and therefore fail
# to match; those cases are logged at DEBUG and skipped (design.md §7,
# task-7.1 acceptance criterion).

_MYPY_LINE_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+)(?::(?P<col>\d+))?:"
    r"\s*(?P<severity>\w+):\s*(?P<message>.*?)"
    r"(?:\s*\[(?P<rule_id>[^\]]+)\])?\s*$"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_static_checks(
    sandbox: Sandbox,
    conn: sqlite3.Connection,
    impact: ImpactSet,
    *,
    repo_path: Path,
    base_sha: str,
    head_sha: str,
    tools: Sequence[StaticTool] = DEFAULT_STATIC_TOOLS,
) -> StaticReport:
    """Run each tool on the head-side changed files and diff against baseline.

    Implements all five steps of ``design.md §7``. Step 2 (the baseline
    resolve) is delegated to :func:`_resolve_base_keys`, which owns the
    ``static_baseline`` cache read, the ``git worktree add --detach``
    materialization on miss, the host-side baseline-tool invocation, and
    the cache write.

    Args:
        sandbox: An already-entered :class:`LocalDockerSandbox`. The caller
            (``run_verification``) is responsible for the context-manager
            lifecycle; this function only issues ``exec`` calls.
        conn: SQLite connection to ``.trikon/state.db``. Read for the
            ``static_baseline`` cache lookup and written on a cache miss.
            The connection is expected to have the Phase-2 tables from
            :func:`trikon.verify.db.ensure_verify_tables` already present
            (Task 3.2 wires that guarantee into ``open_connection``).
        impact: The precomputed impact set. Only ``changed_files`` is read.
        repo_path: The absolute path to the git repository. Used by the
            baseline-resolve path to materialize a detached worktree at
            ``base_sha`` for the host-side tool run.
        base_sha: The base commit SHA. First component of the
            ``static_baseline`` cache key; also the commit the detached
            worktree is materialized at on a cache miss.
        head_sha: The head commit SHA. Recorded on the signature for
            symmetry with ``base_sha`` and for future audit metadata;
            unused in the current body.
        tools: The sequence of :class:`StaticTool` descriptors to invoke.
            Defaults to :data:`DEFAULT_STATIC_TOOLS` (ruff + mypy).

    Returns:
        A :class:`~trikon.evidence.report.StaticReport` with ``tools_run``
        in invocation order, aggregate counters, and the flat ``findings``
        list.

    Raises:
        StaticCheckError: On ruff JSON parse failure on the head run, on
            any ``sqlite3.Error`` from the ``static_baseline`` cache
            read or write, on ``git worktree add`` failure, on the
            host-side baseline-tool invocation failing to spawn, or on
            timeout of any host-side subprocess. Passes through any
            :class:`~trikon.verify.errors.SandboxExecError` /
            :class:`~trikon.verify.errors.SandboxUnavailableError` raised
            by the sandbox unchanged.
    """
    # ``head_sha`` is not read by this task's body — it rides the
    # signature for future audit metadata. Named receipt so mypy --strict
    # / ruff F841 do not flag it as unused.
    del head_sha

    # -----------------------------------------------------------------------
    # Step 1: capture tool versions
    # -----------------------------------------------------------------------
    tool_versions: dict[str, str] = {}
    for tool in tools:
        version_result = sandbox.exec(
            tool.version_command,
            timeout_seconds=_TOOL_VERSION_TIMEOUT_SECONDS,
        )
        # A timed-out version probe is treated as an empty version string
        # rather than a raise: the never-fail-open contract from
        # Requirement 6 says the runner surfaces evidence, not exceptions.
        # An empty version string participates in the cache key like any
        # other value — it will always miss the ``(base_sha, tool, "")``
        # slot and force a fresh baseline capture, which is the safe
        # side of the tradeoff.
        tool_versions[tool.name] = version_result.stdout.strip()

    # -----------------------------------------------------------------------
    # Steps 2-5: for each tool, resolve baseline, run at HEAD, diff, count.
    # -----------------------------------------------------------------------
    findings: list[dict[str, object]] = []
    new_errors: int = 0
    new_warnings: int = 0
    preexisting_errors: int = 0
    tools_run: list[str] = []

    for tool in tools:
        tools_run.append(tool.name)

        # ---------------------------------------------------------------
        # Step 2: resolve baseline keys for this tool.
        # ---------------------------------------------------------------
        base_keys: frozenset[tuple[str, int, str]] = _resolve_base_keys(
            conn=conn,
            tool=tool,
            tool_version=tool_versions[tool.name],
            base_sha=base_sha,
            repo_path=repo_path,
            changed_files=impact.changed_files,
        )

        # ---------------------------------------------------------------
        # Step 3: head-side tool run inside the sandbox.
        # ---------------------------------------------------------------
        argv = _expand_argv(tool.argv_template, impact.changed_files)
        # No timeout at Task 7.1/7.2 — Task 9.2 will thread the caller's
        # per-verdict deadline share into every stage. The sandbox itself
        # is bounded by the outer ``run_verification`` deadline.
        head_result = sandbox.exec(argv)

        parsed_head: list[dict[str, str | int]]
        if tool.parse_json:
            parsed_head = _parse_ruff_json(head_result.stdout)
        else:
            parsed_head = _parse_mypy_text(head_result.stdout)

        # ---------------------------------------------------------------
        # Step 4: is_new diff on the (path, line, rule_id) triple, and
        # Step 5: aggregate counter update.
        # ---------------------------------------------------------------
        for row in parsed_head:
            path_val = row["path"]
            line_val = row["line"]
            rule_id_val = row["rule_id"]
            message_val = row["message"]
            severity_val = row["severity"]

            # Coerce for the key. Parser output already respects the
            # scalar type for each field (path/rule_id/message/severity
            # as str, line as int), but ``dict[str, str | int]`` widens
            # the value slot so a narrow re-check here is what feeds the
            # typed key tuple used for set membership.
            path_str = path_val if isinstance(path_val, str) else str(path_val)
            line_int = line_val if isinstance(line_val, int) else 0
            rule_id_str = rule_id_val if isinstance(rule_id_val, str) else str(rule_id_val)
            severity_str = severity_val if isinstance(severity_val, str) else str(severity_val)

            key: tuple[str, int, str] = (path_str, line_int, rule_id_str)
            is_new: bool = key not in base_keys

            # Fresh dict[str, object] to accommodate the bool + str + int
            # union across all seven fields. dict invariance means we
            # cannot just widen ``row`` — a new dict is the mypy-strict
            # correct move.
            finding: dict[str, object] = {
                "path": path_str,
                "line": line_int,
                "rule_id": rule_id_str,
                "message": message_val,
                "severity": severity_str,
                "tool": tool.name,
                "is_new": is_new,
            }
            findings.append(finding)

            if is_new:
                if severity_str == "error":
                    new_errors += 1
                else:
                    new_warnings += 1
            elif severity_str == "error":
                preexisting_errors += 1
            # Non-error, pre-existing findings are intentionally not
            # counted: the three-counter shape from ``design.md §3.7``
            # only surfaces new noise and pre-existing errors.

    # tool_versions is captured for the cache key; log at DEBUG so
    # operators can inspect the pin behavior without changing the report
    # shape.
    _logger.debug("run_static_checks: tool_versions=%s", tool_versions)

    return StaticReport(
        tools_run=tools_run,
        new_errors=new_errors,
        new_warnings=new_warnings,
        preexisting_errors=preexisting_errors,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Baseline resolve — design.md §7 Step 2
# ---------------------------------------------------------------------------


def _resolve_base_keys(
    *,
    conn: sqlite3.Connection,
    tool: StaticTool,
    tool_version: str,
    base_sha: str,
    repo_path: Path,
    changed_files: Sequence[str],
) -> frozenset[tuple[str, int, str]]:
    """Return the ``(path, line, rule_id)`` triples for the baseline run.

    On a ``static_baseline`` cache hit, decode the cached findings JSON
    and reduce to the triple set — no sandbox / worktree work is done.
    On a miss, materialize the base tree at ``base_sha`` with
    ``git worktree add --detach`` on the host, invoke the tool against
    the worktree, JSON-encode the parsed findings back to
    ``static_baseline``, and return the reduced triple set. The worktree
    is always torn down in the ``finally`` block regardless of success.

    Cache key is the exact ``(base_sha, tool.name, tool_version)`` triple
    from Requirement 3.2 / 3.3. The ``UNIQUE(base_sha, tool, tool_version)``
    constraint from :func:`trikon.verify.db.ensure_verify_tables` is what
    makes a tool-version bump implicitly invalidate the cache row.

    Args:
        conn: SQLite connection to ``.trikon/state.db``.
        tool: The tool descriptor. ``tool.parse_json`` picks the parser
            for the host-side output on a cache miss.
        tool_version: The value captured from ``tool.version_command`` at
            the start of the verdict. Empty strings are permitted;
            they produce a distinguished cache slot.
        base_sha: The base commit SHA.
        repo_path: The absolute path to the git repository — the
            worktree is created relative to this path.
        changed_files: The head-side changed file list. The baseline run
            filters this against the base worktree so files added in the
            change (absent from the base tree) do not cause tool errors.

    Returns:
        A frozenset of ``(path, line, rule_id)`` triples. Empty when the
        baseline run produced no findings.

    Raises:
        StaticCheckError: On ``sqlite3.Error`` from the cache read or
            write, on JSON decode failure of the cached payload, on
            ``git worktree add`` failure, or on any host-side baseline
            subprocess failure. See :func:`_run_baseline_tool_on_host`
            and :func:`_add_worktree` for the individual raise sites.
    """
    # ---- Cache read --------------------------------------------------------
    try:
        cursor = conn.execute(
            "SELECT findings_json FROM static_baseline "
            "WHERE base_sha = ? AND tool = ? AND tool_version = ?",
            (base_sha, tool.name, tool_version),
        )
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise StaticCheckError(
            f"static_baseline read failed for tool={tool.name!r} base_sha={base_sha!r}: {exc}"
        ) from exc

    if row is not None:
        # Cache hit: decode and reduce.
        findings_json_val = row[0]
        cached_payload_str = findings_json_val if isinstance(findings_json_val, str) else ""
        try:
            cached_payload = json.loads(cached_payload_str)
        except json.JSONDecodeError as exc:
            raise StaticCheckError(
                f"static_baseline JSON decode failed for tool={tool.name!r} "
                f"base_sha={base_sha!r}: {exc}"
            ) from exc
        return _extract_finding_keys(cached_payload)

    # ---- Cache miss: materialize + run + persist --------------------------
    worktree_dir = _add_worktree(repo_path, base_sha)
    try:
        base_findings = _run_baseline_tool_on_host(
            tool=tool,
            worktree_dir=worktree_dir,
            changed_files=changed_files,
        )
    finally:
        _remove_worktree(worktree_dir)

    # Persist. The ``UNIQUE(base_sha, tool, tool_version)`` constraint
    # from Task 3.1 makes tool-version bumps invalidate the cache row
    # automatically; a plain ``INSERT`` is therefore the right verb here
    # rather than ``INSERT OR REPLACE`` — a duplicate write is a bug that
    # we would rather surface as ``IntegrityError`` than silently swallow.
    try:
        conn.execute(
            "INSERT INTO static_baseline "
            "(base_sha, tool, tool_version, findings_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                base_sha,
                tool.name,
                tool_version,
                json.dumps(base_findings),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise StaticCheckError(
            f"static_baseline write failed for tool={tool.name!r} base_sha={base_sha!r}: {exc}"
        ) from exc

    # Reduce the freshly-computed findings to the triple set for the
    # ``is_new`` diff. Passing the same shape through
    # :func:`_extract_finding_keys` keeps the hit and miss branches
    # symmetric.
    return _extract_finding_keys(base_findings)


def _extract_finding_keys(
    payload: object,
) -> frozenset[tuple[str, int, str]]:
    """Reduce a decoded findings payload to the ``(path, line, rule_id)`` set.

    Accepts either a freshly-parsed ``list[dict[str, str | int]]`` from
    one of the tool parsers or a ``json.loads`` result (typed
    ``object`` to keep the module clean under
    ``disallow_any_explicit=true``). Non-list payloads and non-dict
    entries are treated defensively: a corrupt cache row that decodes to
    ``None`` or to a JSON object returns an empty triple set rather than
    crashing the verdict, matching the never-fail-open contract of
    Requirement 6.

    ``message`` and ``severity`` are deliberately ignored — the diff
    discriminator is the triple only (``design.md §7`` Step 4).
    """
    if not isinstance(payload, list):
        # A malformed cache row that decodes to something other than a
        # list of dicts should not crash the verdict; the safe fallback
        # is "no baseline findings", i.e. everything is new.
        return frozenset()

    keys: list[tuple[str, int, str]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        raw_path = entry.get("path", "")
        raw_line = entry.get("line", 0)
        raw_rule = entry.get("rule_id", "")

        path_str = raw_path if isinstance(raw_path, str) else str(raw_path)
        # ``bool`` is a subclass of ``int``; excluding it here matches the
        # parser output type (``line`` is an ``int``, never a ``bool``).
        line_int = raw_line if isinstance(raw_line, int) and not isinstance(raw_line, bool) else 0
        rule_str = raw_rule if isinstance(raw_rule, str) else str(raw_rule)

        keys.append((path_str, line_int, rule_str))
    return frozenset(keys)


# ---------------------------------------------------------------------------
# Git worktree helpers
# ---------------------------------------------------------------------------


def _add_worktree(repo_path: Path, base_sha: str) -> Path:
    """Materialize ``base_sha`` in a detached worktree under a fresh tmpdir.

    Uses ``git worktree add --detach <tmp> <base_sha>`` on the host so
    the base-tree checkout does not pay the sandbox spin-up tax
    (``design.md §7``). The caller is responsible for pairing this with
    :func:`_remove_worktree` in a ``finally`` block.

    Args:
        repo_path: The absolute path to the git repository. Passed as
            ``cwd`` so ``git`` finds the parent repository even when the
            process's working directory is elsewhere.
        base_sha: The commit SHA to materialize. Any git revspec ``git``
            accepts on the command line would work, but the caller in
            :func:`_resolve_base_keys` always passes a full SHA.

    Returns:
        The absolute path to the new worktree directory. The directory
        is empty of git-worktree metadata by the caller's view — only
        the checked-out tree contents are of interest.

    Raises:
        StaticCheckError: On non-zero exit from ``git worktree add``, on
            timeout, or if ``git`` is not on ``PATH``.
    """
    tmp = Path(tempfile.mkdtemp(prefix="trikon-worktree-"))
    try:
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp), base_sha],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StaticCheckError(
            f"git worktree add timed out after {_GIT_WORKTREE_TIMEOUT_SECONDS}s "
            f"for base_sha={base_sha!r}"
        ) from exc
    except FileNotFoundError as exc:
        raise StaticCheckError(
            f"git worktree add failed: git binary not found on PATH ({exc})"
        ) from exc

    if result.returncode != 0:
        # Truncate stderr so a runaway git message doesn't blow up the
        # verdict payload; 500 chars is comfortably above any real git
        # error surface.
        raise StaticCheckError(
            f"git worktree add failed for base_sha={base_sha!r}: {result.stderr[:500]}"
        )
    return tmp


def _remove_worktree(worktree_dir: Path) -> None:
    """Best-effort teardown of a worktree created by :func:`_add_worktree`.

    Runs ``git worktree remove --force <dir>`` and swallows the outcome:
    this is a cleanup path invoked from a ``finally`` block, and the
    primary work has already been captured. A failure to remove the
    worktree is logged at WARNING (the git repository will show a
    stale worktree entry until ``git worktree prune`` is run) but does
    not mask the primary error path back in :func:`_resolve_base_keys`.

    Args:
        worktree_dir: The path returned by :func:`_add_worktree`.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            capture_output=True,
            text=True,
            timeout=_GIT_WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        _logger.warning(
            "git worktree remove failed for %s: %s (leaving on disk; run "
            "`git worktree prune` to clean up)",
            worktree_dir,
            exc,
        )
        return

    if result.returncode != 0:
        _logger.warning(
            "git worktree remove returned %d for %s: %s (leaving on disk; run "
            "`git worktree prune` to clean up)",
            result.returncode,
            worktree_dir,
            result.stderr[:500],
        )


# ---------------------------------------------------------------------------
# Host-side baseline tool invocation
# ---------------------------------------------------------------------------


def _run_baseline_tool_on_host(
    *,
    tool: StaticTool,
    worktree_dir: Path,
    changed_files: Sequence[str],
) -> list[dict[str, str | int]]:
    """Invoke ``tool`` on the host against ``worktree_dir`` and parse output.

    NOTE: baseline tool runs on host, not in sandbox, for Phase 2 (the
    sandbox only mounts the head-side repo). Phase 3 will add multi-mount
    sandbox support and move this call back inside the container. The
    tool-version cache key still discriminates on the pinned version
    reported by the sandbox, not the host — a host with a mismatched
    ``ruff --version`` produces a stale cache row that will be
    invalidated on the next ``pyproject.toml`` bump anyway. This is the
    documented Phase-2 tradeoff.

    ``changed_files`` is filtered to those that exist inside the base
    worktree: a file that was *added* in the change is absent from the
    base tree and would otherwise cause ruff / mypy to fail with
    "no such file". Filtering to the intersection is the correct
    behavior — the baseline can only report findings for files that
    existed at ``base_sha``, and every head-side finding on a
    newly-added file is by definition new (empty baseline for that file
    means every triple misses ``base_keys``).

    Args:
        tool: The tool descriptor. ``tool.argv_template``,
            ``tool.parse_json`` are consumed.
        worktree_dir: The detached worktree from :func:`_add_worktree`.
            Passed as ``cwd`` so the tool resolves ``changed_files``
            relative to the base tree.
        changed_files: The head-side changed file list. Filtered before
            invocation.

    Returns:
        The list of parsed findings from the tool output. Same shape as
        :func:`_parse_ruff_json` / :func:`_parse_mypy_text`. Empty when
        the tool found nothing or when no changed file exists in the
        base tree.

    Raises:
        StaticCheckError: On subprocess timeout, on missing tool binary,
            or on ruff JSON parse failure (mypy tolerates unparseable
            trailing lines at DEBUG log level).
    """
    existing = [f for f in changed_files if (worktree_dir / f).is_file()]
    if not existing:
        return []

    argv = _expand_argv(tool.argv_template, existing)
    # PATH augmentation for venv-installed tools. Same rationale as
    # LocalSubprocessSandbox.exec: when the user launches trikon from
    # `.venv/Scripts/python.exe -m trikon.cli verify --no-sandbox`, the
    # venv's script directory typically isn't on PATH, so mypy / ruff
    # installed into the venv fail to resolve. Prepending
    # ``dirname(sys.executable)`` to PATH surfaces them; ``shutil.which``
    # with the augmented PATH also handles Windows PATHEXT so a bare
    # ``mypy`` / ``ruff`` argv[0] resolves to ``mypy.exe`` / ``ruff.exe``.
    merged_env = os.environ.copy()
    python_bin_dir = str(Path(sys.executable).parent)
    existing_path = merged_env.get("PATH")
    if existing_path:
        merged_env["PATH"] = python_bin_dir + os.pathsep + existing_path
    else:
        merged_env["PATH"] = python_bin_dir
    resolved_argv: list[str] = list(argv)
    if resolved_argv:
        resolved_exe = shutil.which(resolved_argv[0], path=merged_env["PATH"])
        if resolved_exe is not None:
            resolved_argv[0] = resolved_exe
    try:
        result = subprocess.run(
            resolved_argv,
            cwd=str(worktree_dir),
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=_HOST_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StaticCheckError(
            f"baseline tool {tool.name!r} timed out after {_HOST_TOOL_TIMEOUT_SECONDS}s"
        ) from exc
    except FileNotFoundError as exc:
        raise StaticCheckError(f"baseline tool {tool.name!r} not found on PATH: {exc}") from exc

    # Both ruff and mypy exit non-zero when they find issues; that is
    # the normal path and does not indicate a spawn failure. The
    # parsers tolerate empty stdout (no findings).
    if tool.parse_json:
        return _parse_ruff_json(result.stdout)
    return _parse_mypy_text(result.stdout)


# ---------------------------------------------------------------------------
# Argv expansion
# ---------------------------------------------------------------------------


def _expand_argv(
    argv_template: tuple[str, ...],
    changed_files: Sequence[str],
) -> tuple[str, ...]:
    """Expand the ``{files}`` sentinel in ``argv_template`` with ``changed_files``.

    The ``StaticTool.argv_template`` field carries the invariant argv shape
    (tool name, flag list, ``{files}`` sentinel). At runtime, ``{files}`` is
    replaced with the head-side changed file list — one argv token per
    file, so a five-file change against ruff produces
    ``("ruff", "check", "--output-format=json", "a.py", "b.py", ...)``.

    If ``{files}`` is absent from the template (a plausible mode for a tool
    that reads the file list from an env var or stdin instead), this
    function is a no-op copy of the input.
    """
    expanded: list[str] = []
    for token in argv_template:
        if token == "{files}":
            expanded.extend(changed_files)
        else:
            expanded.append(token)
    return tuple(expanded)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_ruff_json(stdout: str) -> list[dict[str, str | int]]:
    """Parse ruff's ``--output-format=json`` output.

    Ruff emits a JSON array of objects with this canonical shape (see
    https://docs.astral.sh/ruff/settings/#output-format)::

        [
          {
            "code": "F401",
            "filename": "src/api/payments.py",
            "location": {"row": 1, "column": 1},
            "end_location": {"row": 1, "column": 20},
            "message": "'os' imported but unused",
            "url": "..."
          },
          ...
        ]

    Each entry is projected into the parser's canonical finding shape::

        {"path": <filename>, "line": <row>, "rule_id": <code>,
         "message": <message>, "severity": "error"}

    Ruff has no notion of "warning" severity — every ruff diagnostic is an
    "error" in ruff's grading model, so ``severity`` is a fixed literal.

    Args:
        stdout: The raw stdout captured from ``ruff check
            --output-format=json ...``. May be empty (no findings), a JSON
            array (findings present), or malformed (parse failure).

    Returns:
        A list of finding dicts in emission order. Empty list when ruff
        found no diagnostics.

    Raises:
        StaticCheckError: When the payload is non-empty and not a JSON
            array of objects (Requirement 6.1 / design.md §9.1).
    """
    stripped = stdout.strip()
    if not stripped:
        return []

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise StaticCheckError(f"ruff JSON parse failure: {exc}") from exc

    if not isinstance(payload, list):
        raise StaticCheckError(
            f"ruff JSON parse failure: expected top-level array, got {type(payload).__name__}"
        )

    findings: list[dict[str, str | int]] = []
    for raw_entry in payload:
        if not isinstance(raw_entry, dict):
            raise StaticCheckError(
                f"ruff JSON parse failure: expected object entry, got {type(raw_entry).__name__}"
            )

        filename_val = raw_entry.get("filename", "")
        filename = filename_val if isinstance(filename_val, str) else ""

        code_val = raw_entry.get("code")
        rule_id = code_val if isinstance(code_val, str) else ""

        message_val = raw_entry.get("message", "")
        message = message_val if isinstance(message_val, str) else ""

        line = 0
        location_val = raw_entry.get("location")
        if isinstance(location_val, dict):
            row_val = location_val.get("row", 0)
            if isinstance(row_val, int) and not isinstance(row_val, bool):
                line = row_val

        findings.append(
            {
                "path": filename,
                "line": line,
                "rule_id": rule_id,
                "message": message,
                "severity": "error",
            }
        )
    return findings


def _parse_mypy_text(stdout: str) -> list[dict[str, str | int]]:
    """Parse mypy's ``--no-color-output --show-column-numbers`` text output.

    Mypy emits one diagnostic per line in the canonical shape::

        src/api/payments.py:42:5: error: message text  [rule-id]

    The column and the ``[rule-id]`` trailer are optional (see the
    :data:`_MYPY_LINE_RE` docstring). Trailing summary lines such as::

        Found 1 error in 1 file (checked 1 source file)

    have no ``line:col`` position and are skipped at DEBUG log level per
    the Task 7.1 acceptance criterion.

    Args:
        stdout: The raw stdout captured from ``mypy --no-color-output
            --show-column-numbers ...``. May be empty (no diagnostics) or
            contain a mix of diagnostic lines and summary lines.

    Returns:
        A list of finding dicts in emission order — the same shape ruff's
        parser returns. ``rule_id`` is the empty string for diagnostics
        without a ``[rule-id]`` trailer.
    """
    findings: list[dict[str, str | int]] = []
    for raw_line in stdout.splitlines():
        line_text = raw_line.rstrip()
        if not line_text:
            continue

        match = _MYPY_LINE_RE.match(line_text)
        if match is None:
            _logger.debug("mypy parser: skipping unparseable line: %r", line_text)
            continue

        rule_id_group = match.group("rule_id") or ""
        findings.append(
            {
                "path": match.group("path"),
                "line": int(match.group("line")),
                "rule_id": rule_id_group,
                "message": match.group("message").strip(),
                "severity": match.group("severity"),
            }
        )
    return findings
