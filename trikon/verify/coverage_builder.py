"""Build the ``coverage_map`` table by running the full test suite in a sandbox.

This module implements :func:`build_coverage_map`, the backend of the CLI
command ``trikon coverage build``. It runs the repository's full pytest suite
inside a :class:`LocalDockerSandbox` with :mod:`coverage.py` instrumentation,
enumerates the impacted symbols with :func:`trikon.change_intel.ast_indexer.index_file`,
and persists a ``symbol → set(test_ids)`` mapping to the ``coverage_map`` and
``tests_seen`` tables of ``.trikon/state.db`` (see ``design.md §3.6`` for the
signature and ``design.md §10`` for the algorithm).

Phase-2 symbol mapping — pragmatic all-to-all
---------------------------------------------

Symbol-accurate line-level coverage requires either coverage.py's per-test
contexts (``coverage run --context=…``) or a post-processing pass that maps
every executed line back to its enclosing AST node. Both are Phase-3 work.
The pragmatic Phase-2 mapping documented in ``tasks.md §10.1`` is:

* Run ``pytest --collect-only --quiet`` to enumerate every collected pytest
  node ID once. That's the ``test_nodes_seen`` universe.
* Run ``coverage run --source src -m pytest -q`` to produce ``.coverage``
  data, then ``coverage json -o /workspace/tmp/coverage.json`` to serialize
  it. The JSON's ``files`` key lists every source file that had at least one
  executed line — this is the file-level covered set.
* For each covered source file, feed the on-disk path through
  :func:`trikon.change_intel.ast_indexer.index_file` and take the
  :attr:`SymbolDef.qualified_name` of every emitted symbol as the coverage
  key. Associate ALL collected pytest node IDs with each symbol.

The mapping is intentionally crude: an all-to-all fan-out from every
touched-in-any-way source file to every collected test. It's still strictly
better than the filename-heuristic fallback that :func:`select_impacted_tests`
uses when the map is absent, because it eliminates the "no coverage-map row"
branch and prevents the wholesale staleness routing of Requirement 1.3.
Phase 3 refines this to per-symbol coverage via context tags.

Persistence and atomicity
-------------------------

Every ``coverage_map`` row is written with ``INSERT OR REPLACE`` on the
``UNIQUE(qualified_name, built_against_sha)`` constraint from
``design.md §4.1`` — rebuilds against the same head SHA overwrite in
place, rebuilds against a new SHA add fresh rows without evicting older
ones. ``tests_seen`` uses the same ``INSERT OR REPLACE`` verb on its
``test_node_id`` primary key.

Task 10.2 wraps the entire persistence block in an explicit
``BEGIN``/``COMMIT`` transaction with a ``ROLLBACK`` on any
:class:`sqlite3.Error` raised mid-write. The rollback discipline is the
mechanism by which Requirement 5.3 (previously-persisted rows are
byte-identical on failure) is enforced: no partial batch ever lands, the
INSERT OR REPLACE loops either apply as a single atomic unit or leave the
table at its pre-call state. The ``BEGIN`` is issued explicitly (rather
than relying on Python's default deferred-transaction auto-management)
so the semantics are the same whether the caller opened the connection
in autocommit mode (``isolation_level=None``, the pattern
``trikon.change_intel.dep_graph`` uses) or default deferred mode.

Failure discipline (Requirement 6.1)
------------------------------------

Every raise site *originating in this module* uses
:class:`~trikon.verify.errors.CoverageBuildError`: git-HEAD resolution
failures, malformed / truncated ``coverage.json``, ``sqlite3.Error`` from
the persistence step, and any invariant violation of the parsed JSON shape.
:class:`~trikon.verify.errors.SandboxExecError` and
:class:`~trikon.verify.errors.SandboxUnavailableError` raised by the sandbox
itself propagate through unchanged — they are already
:class:`~trikon.verify.errors.VerificationRunnerError` subclasses and
therefore satisfy Requirement 5.3's "surface the failure as a subclass of
VerificationRunnerError" contract at the SDK boundary.

Type discipline
---------------

No ``dict[str, Any]`` appears on any surface. The ``json.loads`` result of
``coverage.json`` is typed ``object`` and narrowed with ``isinstance``
checks at every access; the pytest-node parser returns a
``tuple[str, ...]``; the symbol collector returns a ``set[str]``.

Validates: Requirement 5.1.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from trikon.change_intel.ast_indexer import index_file
from trikon.change_intel.errors import ChangeIntelError
from trikon.verify.errors import CoverageBuildError
from trikon.verify.models import CoverageBuildReport, SandboxExecResult
from trikon.verify.sandbox import LocalDockerSandbox

__all__ = ["build_coverage_map"]


# ---------------------------------------------------------------------------
# Sandbox paths (mirrors design.md §5.2 tmpfs layout)
# ---------------------------------------------------------------------------
#
# The repo bind-mount at ``/workspace/repo`` is read-only, so the coverage
# data file lives under the writable tmpfs at ``/workspace/tmp``. These
# constants are duplicated (rather than imported) from ``sandbox.py`` to
# keep this module's public import surface minimal — pulling them from
# sandbox.py would drag the Docker client into the import graph even when
# a caller supplies a pre-entered sandbox.

_REPO_MOUNT_TARGET = "/workspace/repo"
_SANDBOX_TMP_DIR = "/workspace/tmp"
_COVERAGE_JSON_PATH = f"{_SANDBOX_TMP_DIR}/coverage.json"


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
#
# The per-command budgets below are deliberately generous. Task 10.1 hits
# them on the happy path only; a coverage build that takes longer than
# ``_COVERAGE_RUN_TIMEOUT_SECONDS`` is almost certainly a symptom of a
# stuck test rather than a large suite — Requirement 8.3 caps the whole
# ``trikon coverage build`` invocation at 30 s wall-clock on
# ``examples/sample_repo`` and any repo whose full suite exceeds a minute
# should be running the build asynchronously anyway.

_GIT_HEAD_TIMEOUT_SECONDS: float = 30.0

_MKDIR_TIMEOUT_SECONDS: float = 5.0
_COLLECT_TIMEOUT_SECONDS: float = 60.0
_COVERAGE_RUN_TIMEOUT_SECONDS: float = 600.0
_COVERAGE_JSON_TIMEOUT_SECONDS: float = 60.0
_JSON_READ_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Pytest node-ID pattern
# ---------------------------------------------------------------------------
#
# ``pytest --collect-only --quiet`` emits one node ID per line, followed by
# a trailing summary line like ``30 tests collected in 0.05s``. A node ID
# always starts with a POSIX-style path ending in ``.py`` and contains at
# least one ``::`` separator (``file.py::test_name`` at minimum). The regex
# below matches that shape at the start of the line so summary / blank /
# warning lines fall through cleanly. Parameterized tests
# (``file.py::test[case-1]``) match because the anchor is only on the
# ``.py::`` prefix, not the tail.

_PYTEST_NODE_LINE_RE = re.compile(r"^(?P<node>[^\s:]+\.py::\S+)\s*$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_coverage_map(
    repo_path: Path,
    conn: sqlite3.Connection,
    *,
    sandbox: LocalDockerSandbox | None = None,
    head_sha: str | None = None,
) -> CoverageBuildReport:
    """Run the full test suite instrumented and persist a coverage map.

    Implements the four-step sandbox flow from ``design.md §10``:

    1. ``pytest --collect-only --quiet`` enumerates every collected pytest
       node ID once. Lines matching the pytest node-ID pattern
       (``<path>.py::<name>``) are captured; summary and blank lines are
       dropped.
    2. ``coverage run --source src -m pytest -q --no-header
       --override-ini="addopts="`` runs the suite under coverage.py
       instrumentation. ``--override-ini="addopts="`` strips any repo-side
       ``addopts`` that would otherwise interfere with the driven run
       (repo-set marker filters, custom plugins) and matches the runner's
       treatment of pytest execution in ``design.md §9.2``.
    3. ``coverage json -o /workspace/tmp/coverage.json`` serializes the
       ``.coverage`` datafile as a JSON document rooted at
       ``/workspace/tmp`` — the sandbox's writable tmpfs.
    4. ``cat /workspace/tmp/coverage.json`` reads that document back out
       for host-side parsing.

    After the sandbox flow the module walks every file listed in the
    coverage JSON's ``files`` map, feeds it through
    :func:`trikon.change_intel.ast_indexer.index_file`, and associates
    every collected pytest node ID with every enumerated symbol — the
    Phase-2 all-to-all mapping documented in the module docstring.

    Args:
        repo_path: Absolute path to the git repository. Bind-mounted
            read-only at ``/workspace/repo`` inside the sandbox; also the
            working directory for the ``git rev-parse HEAD`` call when
            ``head_sha`` is not supplied.
        conn: Open :class:`sqlite3.Connection` to
            ``<repo>/.trikon/state.db``. Task 3.2 guarantees the Phase-2
            tables are present via the ``open_connection`` migration hook;
            this function does not attempt to create tables itself.
        sandbox: Optional pre-entered :class:`LocalDockerSandbox`. When
            ``None`` (the CLI's usage), the function constructs a fresh
            sandbox, calls :meth:`~LocalDockerSandbox.mount_repo` on it
            with ``repo_path``, and context-manages it inside this call.
            When supplied, the caller is responsible for the sandbox
            lifecycle — the sandbox must already be entered and mounted
            on the same ``repo_path``.
        head_sha: The git SHA to record on every ``coverage_map`` row and
            on the returned report's ``built_against_sha``. Defaults to
            ``git rev-parse HEAD`` executed against ``repo_path`` via
            :func:`subprocess.run`. Explicit values are passed through
            verbatim (no revspec resolution).

    Returns:
        A :class:`CoverageBuildReport` capturing the number of distinct
        symbols indexed, the number of distinct pytest nodes observed,
        the end-to-end wall-clock duration in milliseconds, the git SHA
        the build ran against, and the number of stale ``tests_seen``
        rows pruned (always ``0`` in Task 10.1 — pruning is Task 10.2).

    Raises:
        CoverageBuildError: On git-HEAD resolution failure, on failure of
            any of the sandbox commands' post-exec exit-code / timeout
            checks, on malformed ``coverage.json``, or on any
            :class:`sqlite3.Error` from the persistence step. The
            underlying cause is preserved on ``__cause__`` via
            ``raise ... from`` so the CLI surface can render it verbatim
            when it prints and exits 1 (``design.md §9.1``).
    """
    build_started = time.monotonic()
    resolved_head_sha = _resolve_head_sha(repo_path, head_sha)

    with _acquire_sandbox(sandbox, repo_path) as active_sandbox:
        collected_nodes = _collect_pytest_nodes(active_sandbox)
        _run_coverage_instrumented(active_sandbox)
        _serialize_coverage_json(active_sandbox)
        coverage_document = _read_coverage_json(active_sandbox)

    covered_files = _covered_source_files(coverage_document)
    symbol_qnames = _enumerate_symbols_for_files(repo_path, covered_files)

    _persist_coverage_map(
        conn=conn,
        symbol_qnames=symbol_qnames,
        collected_nodes=collected_nodes,
        head_sha=resolved_head_sha,
    )

    duration_ms = int((time.monotonic() - build_started) * 1000)
    return CoverageBuildReport(
        symbols_indexed=len(symbol_qnames),
        test_nodes_seen=len(collected_nodes),
        duration_ms=duration_ms,
        built_against_sha=resolved_head_sha,
        stale_rows_pruned=0,
    )


# ---------------------------------------------------------------------------
# HEAD-sha resolution
# ---------------------------------------------------------------------------


def _resolve_head_sha(repo_path: Path, head_sha: str | None) -> str:
    """Return the caller's ``head_sha`` or resolve ``git rev-parse HEAD``.

    Runs on the host, not inside the sandbox — the sandbox's repo mount
    is read-only and the pinned image has no ``git`` binary installed
    (design.md §5.1 pins only Python tooling). The resolved SHA is used
    as the ``built_against_sha`` cache-key component on every
    ``coverage_map`` row.

    Args:
        repo_path: The absolute path to the git repository. Passed as
            ``cwd`` so ``git`` finds the parent repository even when the
            interpreter's working directory is elsewhere.
        head_sha: If not ``None``, returned unchanged.

    Returns:
        The stripped stdout of ``git rev-parse HEAD``, or the pass-through
        ``head_sha`` argument.

    Raises:
        CoverageBuildError: On non-zero exit from ``git``, on timeout, or
            when ``git`` is not present on the caller's ``PATH``.
    """
    if head_sha is not None:
        return head_sha

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_HEAD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoverageBuildError(
            "git rev-parse HEAD timed out after "
            f"{_GIT_HEAD_TIMEOUT_SECONDS}s for repo_path={repo_path!s}"
        ) from exc
    except FileNotFoundError as exc:
        raise CoverageBuildError(
            f"git rev-parse HEAD failed: git binary not found on PATH ({exc})"
        ) from exc
    except OSError as exc:
        raise CoverageBuildError(
            f"git rev-parse HEAD failed for repo_path={repo_path!s}: {exc}"
        ) from exc

    if result.returncode != 0:
        # Truncate stderr so a runaway git message doesn't drown the CLI
        # summary; 500 chars is comfortably above any real git failure.
        raise CoverageBuildError(
            f"git rev-parse HEAD failed for repo_path={repo_path!s}: {result.stderr[:500]}"
        )

    resolved = result.stdout.strip()
    if not resolved:
        raise CoverageBuildError(
            f"git rev-parse HEAD returned an empty SHA for repo_path={repo_path!s}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Sandbox lifecycle helper
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _acquire_sandbox(
    supplied: LocalDockerSandbox | None,
    repo_path: Path,
) -> Iterator[LocalDockerSandbox]:
    """Yield an entered :class:`LocalDockerSandbox`, owning it when needed.

    When ``supplied`` is ``None`` the helper constructs a fresh sandbox,
    calls :meth:`~LocalDockerSandbox.mount_repo` on it with ``repo_path``
    (which must happen *before* ``__enter__`` per the sandbox docstring),
    and enters/exits the context manager as part of this call. When the
    caller supplies a sandbox, the helper assumes the caller has already
    entered it and mounted the repository on the same path — it just
    yields the object back and never manages its lifecycle.

    The split keeps the ``build_coverage_map`` body single-branched: the
    sandbox flow reads exactly the same regardless of who owns the
    sandbox.

    Args:
        supplied: The caller's sandbox, or ``None`` to construct one.
        repo_path: The repository root; used only when constructing a
            new sandbox (via :meth:`~LocalDockerSandbox.mount_repo`).

    Yields:
        An entered, repo-mounted :class:`LocalDockerSandbox` ready for
        :meth:`~LocalDockerSandbox.exec` calls.
    """
    if supplied is not None:
        yield supplied
        return
    owned = LocalDockerSandbox()
    owned.mount_repo(repo_path)
    with owned as active:
        yield active


# ---------------------------------------------------------------------------
# Sandbox flow — the four commands from design.md §10
# ---------------------------------------------------------------------------


def _collect_pytest_nodes(sandbox: LocalDockerSandbox) -> tuple[str, ...]:
    """Enumerate collected pytest node IDs via ``pytest --collect-only``.

    Runs ``pytest --collect-only --quiet`` from the mounted repository
    inside the sandbox and parses stdout for lines matching the pytest
    node-ID pattern. A pytest exit code of ``5`` (no tests collected) is
    treated as a legitimate empty-suite result rather than a failure —
    an empty ``coverage_map`` build is still a well-defined operation.
    Any other non-zero exit *and* an empty parse produces
    :class:`CoverageBuildError` so a collection error (broken conftest,
    ``ImportError`` on a test module) surfaces to the CLI.

    Args:
        sandbox: An already-entered sandbox with the repository mounted.

    Returns:
        A sorted, de-duplicated tuple of pytest node IDs.

    Raises:
        CoverageBuildError: On sandbox exec timeout or on a non-zero,
            non-``5`` exit code coupled with an empty parse.
    """
    result = sandbox.exec(
        ("pytest", "--collect-only", "--quiet"),
        workdir=_REPO_MOUNT_TARGET,
        timeout_seconds=_COLLECT_TIMEOUT_SECONDS,
    )
    _guard_not_timed_out(result, description="pytest --collect-only --quiet")

    node_ids = _parse_pytest_collect_output(result.stdout)
    if not node_ids and result.exit_code not in (0, 5):
        # Non-zero exit AND no node IDs parsed: something went wrong at
        # collection time. Exit code 5 is pytest's "no tests collected"
        # sentinel, which is a legitimate empty-suite outcome and not a
        # coverage-build failure.
        raise CoverageBuildError(
            "pytest --collect-only failed inside sandbox "
            f"(exit={result.exit_code}): {result.stdout[-2000:]}"
        )
    return node_ids


def _parse_pytest_collect_output(stdout: str) -> tuple[str, ...]:
    """Return a sorted tuple of node IDs parsed from ``--collect-only`` stdout.

    ``pytest --collect-only --quiet`` emits one node ID per line, a blank
    line, and a summary tail like ``30 tests collected in 0.05s``. Lines
    that fail the :data:`_PYTEST_NODE_LINE_RE` match are ignored so the
    parser is robust against future stdout additions.
    """
    seen: set[str] = set()
    for line in stdout.splitlines():
        match = _PYTEST_NODE_LINE_RE.match(line.strip())
        if match is not None:
            seen.add(match.group("node"))
    return tuple(sorted(seen))


def _run_coverage_instrumented(sandbox: LocalDockerSandbox) -> None:
    """Run the pytest suite under ``coverage.py`` instrumentation.

    Invokes ``coverage run --source src -m pytest -q --no-header
    --override-ini="addopts="``. The ``--override-ini`` clears any
    repo-side ``addopts`` (which could add ``-m``, ``-k``, or plugin
    flags that skew the coverage picture) exactly as
    :func:`run_verification` does for its pytest exec in
    ``design.md §9.2``.

    A pytest exit code of ``0`` (all pass) or ``1`` (some tests failed)
    is accepted: we still get valid coverage data in both cases. Any
    other exit code produces :class:`CoverageBuildError`.

    Args:
        sandbox: An already-entered sandbox with the repository mounted.

    Raises:
        CoverageBuildError: On sandbox exec timeout or on a coverage
            exit code outside ``{0, 1}``.
    """
    result = sandbox.exec(
        (
            "coverage",
            "run",
            "--source",
            "src",
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "--override-ini=addopts=",
        ),
        workdir=_REPO_MOUNT_TARGET,
        timeout_seconds=_COVERAGE_RUN_TIMEOUT_SECONDS,
    )
    _guard_not_timed_out(result, description="coverage run -m pytest")

    if result.exit_code not in (0, 1):
        raise CoverageBuildError(
            "coverage run -m pytest failed inside sandbox "
            f"(exit={result.exit_code}): {result.stdout[-2000:]}"
        )


def _serialize_coverage_json(sandbox: LocalDockerSandbox) -> None:
    """Produce ``/workspace/tmp/coverage.json`` from the ``.coverage`` datafile.

    The tmpfs target is ``/workspace/tmp`` (see ``design.md §5.2``);
    ``mkdir -p`` is issued first so the coverage command's ``-o`` argument
    lands in a directory that exists even on the very first sandbox use.

    Args:
        sandbox: An already-entered sandbox with the repository mounted.

    Raises:
        CoverageBuildError: On sandbox exec timeout or on a non-zero
            ``coverage json`` exit code.
    """
    mkdir_result = sandbox.exec(
        ("mkdir", "-p", _SANDBOX_TMP_DIR),
        timeout_seconds=_MKDIR_TIMEOUT_SECONDS,
    )
    _guard_not_timed_out(mkdir_result, description=f"mkdir -p {_SANDBOX_TMP_DIR}")
    if mkdir_result.exit_code != 0:
        raise CoverageBuildError(
            f"mkdir -p {_SANDBOX_TMP_DIR} failed inside sandbox "
            f"(exit={mkdir_result.exit_code}): {mkdir_result.stderr[:2000]}"
        )

    json_result = sandbox.exec(
        ("coverage", "json", "-o", _COVERAGE_JSON_PATH),
        workdir=_REPO_MOUNT_TARGET,
        timeout_seconds=_COVERAGE_JSON_TIMEOUT_SECONDS,
    )
    _guard_not_timed_out(json_result, description="coverage json")
    if json_result.exit_code != 0:
        raise CoverageBuildError(
            "coverage json failed inside sandbox "
            f"(exit={json_result.exit_code}): {json_result.stdout[-2000:]}"
        )


def _read_coverage_json(sandbox: LocalDockerSandbox) -> object:
    """Read ``/workspace/tmp/coverage.json`` back and decode it.

    Uses ``cat`` inside the sandbox because the tmpfs is not reachable
    from the host — the writable mount is per-container. The decoded
    payload is returned as ``object`` and the caller narrows it with
    ``isinstance`` checks; this keeps the module free of
    ``dict[str, Any]`` on any surface (pyproject sets
    ``disallow_any_explicit=true``).

    Args:
        sandbox: An already-entered sandbox with the repository mounted.

    Returns:
        The decoded JSON document (a ``dict`` on the happy path, but
        typed ``object`` for the caller's narrowing pass).

    Raises:
        CoverageBuildError: On sandbox exec timeout, on a non-zero
            ``cat`` exit code (file missing), or on
            :class:`json.JSONDecodeError` from the payload.
    """
    result = sandbox.exec(
        ("cat", _COVERAGE_JSON_PATH),
        timeout_seconds=_JSON_READ_TIMEOUT_SECONDS,
    )
    _guard_not_timed_out(result, description=f"cat {_COVERAGE_JSON_PATH}")
    if result.exit_code != 0:
        raise CoverageBuildError(
            f"cat {_COVERAGE_JSON_PATH} failed inside sandbox "
            f"(exit={result.exit_code}): {result.stderr[:2000]}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CoverageBuildError(f"coverage.json is not valid JSON: {exc}") from exc


def _guard_not_timed_out(
    result: SandboxExecResult,
    *,
    description: str,
) -> None:
    """Raise :class:`CoverageBuildError` when a sandbox exec timed out.

    A per-command sandbox timeout is a hard failure for coverage-build
    purposes: unlike the runner (which synthesizes a failed
    ``TestReport`` per Requirement 2.2), the coverage-build CLI has no
    "partial success" degradation mode — an incomplete build would
    poison every subsequent verdict's test selection. Requirement 5.3
    obliges us to surface the timeout to the caller.
    """
    if result.timed_out:
        raise CoverageBuildError(
            f"sandbox command {description!r} exceeded its deadline ({result.duration_ms} ms)"
        )


# ---------------------------------------------------------------------------
# Coverage-JSON parsing
# ---------------------------------------------------------------------------


def _covered_source_files(coverage_document: object) -> tuple[str, ...]:
    """Return the repo-relative paths of source files with any executed lines.

    coverage.py's JSON layout is documented at
    https://coverage.readthedocs.io/en/latest/cmd.html#json-reporting; the
    only key this function consults is ``files``, which maps a repo-relative
    path (matching the ``--source`` argument prefix, e.g. ``src/foo.py``)
    to a per-file report. A file is considered covered when its
    ``executed_lines`` array is non-empty; ``missing_lines`` is not
    consulted because a file with only misses is not evidence that any
    test touched it.

    The payload is typed ``object`` to keep the ``json.loads`` result
    Any-free at the module boundary. Every access is narrowed with
    ``isinstance``. A malformed root — ``coverage.json`` decoded to
    something other than a JSON object with a ``files`` dict — raises
    :class:`CoverageBuildError` because it indicates a broken coverage
    invocation upstream that must not be silently swallowed.
    """
    if not isinstance(coverage_document, dict):
        raise CoverageBuildError(
            f"coverage.json root is not a JSON object: got {type(coverage_document).__name__}"
        )

    files_section = coverage_document.get("files")
    if files_section is None:
        # Empty coverage build (no source files instrumented at all) is a
        # legitimate outcome — return an empty tuple rather than raise.
        return ()
    if not isinstance(files_section, dict):
        raise CoverageBuildError(
            f"coverage.json 'files' key is not a JSON object: got {type(files_section).__name__}"
        )

    covered: list[str] = []
    for raw_path, raw_entry in files_section.items():
        if not isinstance(raw_path, str) or not isinstance(raw_entry, dict):
            continue
        executed = raw_entry.get("executed_lines")
        if isinstance(executed, list) and len(executed) > 0:
            covered.append(raw_path)
    return tuple(sorted(set(covered)))


# ---------------------------------------------------------------------------
# Symbol enumeration via the Phase-1 AST indexer
# ---------------------------------------------------------------------------


def _enumerate_symbols_for_files(
    repo_path: Path,
    covered_files: Iterable[str],
) -> tuple[str, ...]:
    """Return sorted qualified names for every symbol in the covered files.

    Feeds each covered file through
    :func:`trikon.change_intel.ast_indexer.index_file` and collects the
    :attr:`SymbolDef.qualified_name` of every emitted symbol. Files that
    are missing from disk (a coverage.json reference to a since-deleted
    file), that are not readable, or that fail to parse under libcst's
    stricter grammar (raising
    :class:`trikon.change_intel.errors.AstParseError`) are skipped
    silently — one unindexable file must not prevent a coverage build
    from persisting the healthy portion of the map.

    Args:
        repo_path: The repository root. Every entry in ``covered_files``
            is resolved relative to this path before being handed to the
            indexer.
        covered_files: The repo-relative paths of source files that had
            at least one executed line in the coverage JSON.

    Returns:
        A sorted, de-duplicated tuple of ``qualified_name`` strings.
    """
    qnames: set[str] = set()
    for rel_path in covered_files:
        # ``coverage.json`` paths are POSIX-style even on Windows because
        # they come from inside the Linux sandbox. Path() normalizes the
        # separator on the host side so the subsequent existence check
        # works regardless of the host filesystem.
        on_disk = repo_path / rel_path
        if not on_disk.is_file():
            continue
        try:
            symbols = index_file(on_disk)
        except ChangeIntelError:
            # AstParseError and its siblings (DiffInputError,
            # RepoNotFoundError, ...) share the ChangeIntelError base.
            # Skip any file the indexer can't consume rather than
            # aborting the whole coverage build — the indexer's
            # invariants are stricter than pytest's own tolerance for
            # not-quite-valid Python (mostly f-string / walrus corners
            # older libcst pins reject).
            continue
        except OSError:
            # File was raced away between the ``is_file`` check and the
            # indexer's own read. Treat it the same as a missing file.
            continue
        for symbol in symbols:
            qnames.add(symbol.qualified_name)
    return tuple(sorted(qnames))


# ---------------------------------------------------------------------------
# Persistence — INSERT OR REPLACE on both tables
# ---------------------------------------------------------------------------


def _persist_coverage_map(
    *,
    conn: sqlite3.Connection,
    symbol_qnames: tuple[str, ...],
    collected_nodes: tuple[str, ...],
    head_sha: str,
) -> None:
    """Write the coverage map and ``tests_seen`` rows to state.db.

    Uses the Phase-2 all-to-all mapping described in the module
    docstring: every symbol receives the entire ``collected_nodes``
    tuple as its ``test_ids_json`` payload. Rows are upserted via
    ``INSERT OR REPLACE`` on the ``UNIQUE(qualified_name,
    built_against_sha)`` constraint from ``design.md §4.1`` so a rebuild
    against the same ``head_sha`` overwrites in place, and a rebuild
    against a new ``head_sha`` grows the table without evicting older
    rows.

    ``tests_seen`` rows are upserted the same way on the
    ``test_node_id`` primary key. ``last_outcome`` is set to
    ``"passed"`` because this pass does not parse per-test outcomes from
    the coverage run — the assumption is that ``coverage run`` succeeded
    end-to-end (exit ``0`` or ``1``) and the individual node IDs are
    "observed at least once". Per-node outcomes fold back in once the
    pytest JSON report is captured alongside the coverage data (Phase 3).

    Atomicity discipline (Task 10.2 / Requirement 5.3)
    --------------------------------------------------

    All writes execute inside a single transaction bracketed by
    ``conn.execute("BEGIN")`` and ``conn.commit()``. If any
    :class:`sqlite3.Error` is raised — from ``BEGIN`` itself, from any
    ``INSERT OR REPLACE``, or from the terminal ``COMMIT`` —
    ``conn.rollback()`` is called before the failure is re-raised as
    :class:`CoverageBuildError`. That guarantees the partial writes of a
    failed build never land: the ``coverage_map`` and ``tests_seen``
    rows are byte-identical before and after the failed call.

    The rollback itself is best-effort: if the ``ROLLBACK`` command
    fails (the connection is already broken, e.g. the underlying file
    was unlinked mid-write) the secondary failure is suppressed —
    :class:`CoverageBuildError` is raised from the *original* cause via
    ``raise ... from exc`` so the operator sees the root problem, not
    the follow-on rollback error. This mirrors the
    :func:`trikon.change_intel.dep_graph._safe_rollback` pattern.

    ``BEGIN`` is issued explicitly so the transaction start is
    unambiguous regardless of the caller's ``isolation_level`` setting.
    Python's :mod:`sqlite3` default (``isolation_level=""``) auto-starts
    a transaction on the first DML statement, but that auto-start races
    with the loop below and can leave the connection in an unexpected
    state on error paths; the explicit ``BEGIN`` sidesteps that.

    Args:
        conn: The state-db connection.
        symbol_qnames: Sorted, de-duplicated qualified names.
        collected_nodes: Sorted, de-duplicated pytest node IDs.
        head_sha: The SHA the build ran against; stamped on every
            ``coverage_map`` row's ``built_against_sha``.

    Raises:
        CoverageBuildError: On any :class:`sqlite3.Error` from
            ``BEGIN``, from the ``INSERT OR REPLACE`` statements, or
            from the terminal ``COMMIT``. The original error is
            preserved on ``__cause__`` and ``ROLLBACK`` has been
            attempted (best-effort) before the raise.
    """
    built_at_iso = datetime.now(UTC).isoformat()
    # Serialize the node-ID list *once* per build. Every ``coverage_map``
    # row gets the same JSON payload under the Phase-2 all-to-all
    # mapping, so pre-computing it saves ``len(symbol_qnames)`` calls
    # into ``json.dumps`` on a large repo.
    test_ids_json = json.dumps(list(collected_nodes))

    try:
        conn.execute("BEGIN")
        for qname in symbol_qnames:
            conn.execute(
                "INSERT OR REPLACE INTO coverage_map "
                "(qualified_name, test_ids_json, built_at, built_against_sha) "
                "VALUES (?, ?, ?, ?)",
                (qname, test_ids_json, built_at_iso, head_sha),
            )
        for node_id in collected_nodes:
            conn.execute(
                "INSERT OR REPLACE INTO tests_seen "
                "(test_node_id, last_seen, last_outcome) "
                "VALUES (?, ?, 'passed')",
                (node_id, built_at_iso),
            )
        conn.commit()
    except sqlite3.Error as exc:
        # Best-effort rollback: if the connection is already broken and
        # the rollback itself fails, suppress the secondary error and
        # re-raise the original as :class:`CoverageBuildError`. The
        # operator needs to see the *root* cause, not the follow-on
        # bookkeeping failure.
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        raise CoverageBuildError(
            f"coverage_map persistence failed: {type(exc).__name__}: {exc}"
        ) from exc
