"""Orchestrate one verification pass: state DB, sandbox, tests, static, plugins.

``run_verification`` is the sole public entry point of the Verification-Runner
subsystem. Given a repo path plus the ``ImpactSet`` produced by
``trikon.change_intel.compute_impact``, it opens the shared ``state.db``
connection, computes the impacted pytest node IDs via
:func:`~trikon.verify.test_selector.select_impacted_tests`, spins up a
sandbox (:class:`~trikon.verify.sandbox.LocalDockerSandbox` by default),
runs the test stage described below, then
:func:`~trikon.verify.static_checks.run_static_checks` and
:func:`~trikon.verify.plugins.load_and_run_plugins` sequentially inside the
same container (``design.md §2.4``), and assembles a
:class:`~trikon.evidence.report.VerificationReport`. Wall-clock timing for
both the sandbox lifetime and the whole call is captured through a
:func:`time.monotonic` bracket so the report shape from ``design.md §3.7``
can be populated on every success path.

Test stage
----------

:func:`_run_test_stage` runs four steps, in this order:

1. **Collection_Pass.** ``pytest --collect-only -q -p no:cacheprovider
   --override-ini=addopts= --json-report
   --json-report-file=/workspace/tmp/collect.json`` runs over the whole
   suite before any test runs (Requirement 1.1). The report is read back
   with ``cat``, like the execution report below, and parsed by
   :func:`~trikon.verify.collection.parse_collection_report`. ``collected``
   is the number of leaf items it found (Requirement 1.2). Every failed
   collector becomes a Collection_Error. It is attributable when its file or
   one of its traceback frames is a changed file, or when its file holds a
   Broken_Import from the ``imports`` report (Requirements 2.6, 2.7). A
   report that cannot be read or parsed raises
   :class:`~trikon.verify.errors.CollectionPassError` with the tail of the
   collection output. That is how a conftest import failure (pytest exits 4
   and writes no report) surfaces.
2. **Strategy.** :func:`~trikon.verify.strategy.choose_strategy` decides
   from four facts: whether the change touches a Python file, the selected
   node IDs, the coverage-map state, and whether the caller supplied the
   base SHA (a derived ``HEAD~1`` is a guess, so it does not count).

   * ``selected`` runs the selected node IDs with the previous release's
     argv: ``pytest --json-report --json-report-file=/workspace/tmp/pytest.json
     --override-ini=addopts= <node_ids…>``.
   * ``full_suite`` runs ``pytest -p no:cacheprovider --override-ini=addopts=
     --continue-on-collection-errors --json-report
     --json-report-file=/workspace/tmp/pytest.json`` with no positionals, so
     it covers the same test paths as the Collection_Pass. A Python change
     falls back to it when the selection is empty, or when the selection did
     not come from a Usable_Coverage_Map (Requirements 1.3, 1.4).
     ``--continue-on-collection-errors`` keeps one broken test file from
     zeroing the whole run.
   * ``none`` starts no test run. Only a change with no Python file and no
     selected tests gets it (Requirement 1.7).
3. **Execution.** The chosen run. Its report is read back with ``cat`` and
   parsed by :func:`_parse_pytest_json_report`.
4. **Assembly.** :func:`~trikon.verify.collection.assemble_test_report`
   builds the TestReport. Every count comes from the executed run, so a
   Python change that executed no test is ``skipped``, never ``passed``
   (Requirements 2.1-2.3).

The previous release synthesized an all-passed ``TestReport(total=0)``
whenever the selection was empty and skipped pytest. That was a fail-open
path, and it is gone. The selector's ``coverage_map_stale`` signal still
flows through to the TestReport on every path.

Deadline budget
---------------

The test stage gets half of ``deadline_seconds`` (at least 1 s), capped at
the time left in the deadline. :class:`~trikon.verify.collection.TestBudget`
gives the Collection_Pass at most 25% of it and the execution whatever is
left, both measured against one ``deadline_at`` (Requirement 1.8). A timeout
never raises:

* A Collection_Pass timeout skips execution (on the Docker backend the
  container has been killed, so no further exec could succeed) and marks the
  TestReport incomplete with ``collection_timeout`` (Requirement 2.8).
* An execution timeout, or no time left to start the execution, marks it
  incomplete with ``execution_timeout`` (Requirement 2.9). The status is
  ``failed`` only if a test had already failed, ``skipped`` otherwise
  (Requirement 2.10). The verbatim ``"sandbox exceeded 5-minute deadline"``
  entry is kept in ``failures`` for display continuity; it is not counted.

On the Docker backend a timeout kills the container, so the static stage
that follows raises :class:`~trikon.verify.errors.SandboxExecError` and the
SDK fails closed to ``require_human``. The incomplete TestReport is visible
on the host-local backend and in unit tests.

Never-fail-open discipline (Requirement 6.1, ``design.md §9``)
--------------------------------------------------------------

Every raise site inside :func:`run_verification` is already a
:class:`~trikon.verify.errors.VerificationRunnerError` subclass — the four
adapters, the sandbox module and the collection parser wrap their own
foreign exceptions at the raise site. This function therefore does **not**
catch and swallow: raises propagate unchanged and the SDK boundary (Task 12)
translates them into a ``require_human`` verdict backed by
``EMPTY_VERIFICATION`` (Requirement 6.2).

The two remaining raise families this module itself owns are the initial
``sqlite3`` handshake against ``state.db`` (pragma failures wrap as
:class:`~trikon.verify.errors.TestSelectionError` since the state DB is
primarily a coverage-map/baseline cache reader from this module's
perspective) and the optional ``git rev-parse`` fallback used to derive
``(base_sha, head_sha)`` when the caller does not pass them (wrapped as
:class:`~trikon.verify.errors.VerificationRunnerError`, the base class,
since no subclass semantically owns "git plumbing on the host").

Signature deviation from ``design.md §3.1``
-------------------------------------------

The design's signature closes over the ``(base_sha, head_sha)`` pair via the
``ChangeSet`` that produced the ``ImpactSet`` — but ``ImpactSet`` (in
``trikon.evidence.report``) does not carry those SHAs on the wire. Two
additional keyword-only parameters ``base_sha: str | None = None`` and
``head_sha: str | None = None`` are threaded here so:

* The SDK boundary (Task 12) — which has both SHAs from
  :func:`trikon.change_intel.diff_parser.parse_diff` — can pass them
  explicitly and skip the fallback.
* Direct callers on a git working tree (the ``trikon debug verify`` CLI,
  ad-hoc scripts) get sane defaults: ``HEAD~1`` and ``HEAD`` are derived
  via ``git rev-parse`` when both arguments are ``None``.

Whether ``base_sha`` was supplied is recorded before that derivation: only
a caller-supplied base SHA makes a coverage-map selection usable for the
strategy choice.

The keyword-only ``imports`` parameter carries the Import_Checker's
:class:`~trikon.evidence.report.ImportReport`. The test stage uses its
broken-import files to attribute collection errors, and the report (or an
empty one when ``None``) is embedded in the returned VerificationReport, so
every Verdict carries one (Requirement 4.13).

Validates: Requirements 1.1-1.8, 2.1-2.10, 4.13, 7.1, 7.2 of the engine
fail-safe spec, on top of Requirements 1.1, 3.1, 4.1, 6.1 of the
verification-runner spec.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from trikon.evidence.report import (
    ImpactSet,
    ImportReport,
    TestReport,
    TestResult,
    VerificationReport,
    is_python_change,
)
from trikon.verify.collection import (
    CollectionOutcome,
    TestBudget,
    assemble_test_report,
    classify_collection_errors,
    parse_collection_report,
)
from trikon.verify.db import ensure_verify_tables
from trikon.verify.errors import (
    CollectionPassError,
    TestSelectionError,
    VerificationRunnerError,
)
from trikon.verify.models import SelectedTests
from trikon.verify.plugins import load_and_run_plugins
from trikon.verify.sandbox import DEFAULT_SANDBOX_IMAGE, Sandbox, create_sandbox
from trikon.verify.state_migrations import maybe_migrate_verify_state
from trikon.verify.static_checks import run_static_checks
from trikon.verify.strategy import StrategyInputs, choose_strategy
from trikon.verify.test_selector import select_impacted_tests

if TYPE_CHECKING:
    # ``Policy`` rides the signature per ``design.md §3.1`` for forward
    # compatibility — Phase 3 will thread ``policy.network_allowlist`` into
    # :class:`LocalDockerSandbox` for Requirement 2.3. Task 9.1 does not
    # consume the value at runtime (the ``del policy`` early in the body
    # discards it), so importing under :data:`TYPE_CHECKING` keeps the
    # annotation strict-typed without pulling ``trikon.policy.dsl``'s
    # pydantic-``Any`` internals into this module's runtime graph.
    from trikon.policy.dsl import Policy

__all__ = ["run_verification"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#
# ``git rev-parse`` is a millisecond-scale operation on a healthy repo. A 10 s
# ceiling is orders of magnitude above the observed cost and only fires when
# the working tree is genuinely stuck (locked index, filesystem stall).
_GIT_REVPARSE_TIMEOUT_SECONDS: float = 10.0

# Per-stage share of ``deadline_seconds``. Task 9.1 split the budget evenly
# across three stages; Task 9.2 refines the split now that each stage has
# real budget requirements. The test stage is the heaviest of the three
# (Requirement 8's per-stage table from ``design.md §2.3`` allocates ≤ 5 s to
# pytest vs ≤ 3 s to each static tool and ≤ 500 ms per plugin), so it gets
# half the budget and static + plugins share the remainder equally. The test
# stage's half covers both the Collection_Pass and the test execution
# (:class:`~trikon.verify.collection.TestBudget` splits it).
_TEST_BUDGET_SHARE: float = 0.5
_STATIC_BUDGET_SHARE: float = 0.25
_PLUGIN_BUDGET_SHARE: float = 0.25

# Floor on the per-stage budget so a caller who passes an unreasonably
# tight ``deadline_seconds`` (e.g. 1.0 s for a smoke test) still hands the
# plugin loader a non-degenerate timeout. The floor is deliberately
# conservative: a well-formed plugin should complete in single-digit
# seconds and 1.0 s is the smallest number that keeps
# ``per_plugin_timeout_seconds`` sane on the plugin API.
_MIN_STAGE_BUDGET_SECONDS: float = 1.0

# Bounded read of the pytest JSON report file back out of the sandbox. The
# JSON payload is typically ≤ 200 KB on a small impacted-test subset;
# 10 s is a comfortable ceiling for a ``cat`` round-trip and stays
# orders of magnitude below any realistic sandbox deadline.
_PYTEST_REPORT_READ_TIMEOUT_SECONDS: float = 10.0

# Exact ``failure_summary`` string mandated verbatim by Requirement 2.2.
# The sandbox deadline is fixed at 5 minutes for the Team tier; the
# runtime ``deadline_seconds`` argument may narrow the budget but the
# synthesized ``TestResult`` still surfaces the contract wording so
# downstream consumers (CLI human formatter, audit log) render the
# canonical message.
_SANDBOX_TIMEOUT_MESSAGE: str = "sandbox exceeded 5-minute deadline"

# Path inside the sandbox for the ``pytest-json-report`` output file.
# The ``tmpfs`` mount from ``design.md §5.2`` backs ``/workspace/tmp``.
_PYTEST_REPORT_PATH: str = "/workspace/tmp/pytest.json"

# Path inside the sandbox for the Collection_Pass report, on the same tmpfs.
_COLLECT_REPORT_PATH: str = "/workspace/tmp/collect.json"

# Working directory for every pytest exec: the repo mount. The host-local
# backend rewrites it to the host repo path.
_REPO_WORKDIR: str = "/workspace/repo"

# The repo root as it appears in traceback frames inside the Docker sandbox.
# The host repo path is added at runtime for the host-local backend.
_DOCKER_REPO_PREFIX: str = "/workspace/repo/"

# How much of the Collection_Pass output a ``CollectionPassError`` carries
# when no report was written (e.g. a conftest import failure).
_COLLECTION_OUTPUT_TAIL_CHARS: int = 2048

# The Collection_Pass (design §7): the whole suite, collect only. ``-p
# no:cacheprovider`` keeps pytest from writing ``.pytest_cache`` into the
# read-only repo mount, and ``--override-ini=addopts=`` strips repo-side
# ``addopts`` so they cannot filter the report.
_COLLECTION_ARGV: tuple[str, ...] = (
    "pytest",
    "--collect-only",
    "-q",
    "-p",
    "no:cacheprovider",
    "--override-ini=addopts=",
    "--json-report",
    f"--json-report-file={_COLLECT_REPORT_PATH}",
)

# The Full_Suite_Run (design §7). No positionals, so pytest collects the same
# test paths as the Collection_Pass (config ``testpaths`` or the rootdir).
# ``--continue-on-collection-errors`` keeps one broken test file from
# stopping the whole run.
_FULL_SUITE_ARGV: tuple[str, ...] = (
    "pytest",
    "-p",
    "no:cacheprovider",
    "--override-ini=addopts=",
    "--continue-on-collection-errors",
    "--json-report",
    f"--json-report-file={_PYTEST_REPORT_PATH}",
)

# The Collection_Pass outcome recorded on a timeout. No report is read, so
# nothing was collected and no error is known.
_COLLECTION_TIMED_OUT: CollectionOutcome = CollectionOutcome(timed_out=True, collected=0, errors=())


def run_verification(
    repo_path: Path,
    impact: ImpactSet,
    *,
    policy: Policy | None = None,
    deadline_seconds: float = 300.0,
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
    state_db: Path | None = None,
    now: datetime | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_sandbox: bool = False,
    imports: ImportReport | None = None,
) -> VerificationReport:
    """Execute the impacted checks in isolation and return a structured report.

    Args:
        repo_path: Absolute path to the git repository being verified. The
            same path is bind-mounted read-only at ``/workspace/repo``
            inside the sandbox (``design.md §5.2``) and is where
            ``git rev-parse`` runs when the ``(base_sha, head_sha)`` pair
            is derived from the working tree.
        impact: The precomputed impact set from
            :func:`trikon.change_intel.compute_impact`. Fields read from
            it are ``changed_files`` (fed to :func:`run_static_checks`
            and :func:`load_and_run_plugins`), ``changed_symbols``
            (fed to :func:`select_impacted_tests`), and
            ``changed_files`` plus ``file_changes`` (``path`` and
            ``old_path``) for the Python_Change check and the
            collection-error attribution.
        policy: Optional policy carrying — in a future task — the
            ``network_allowlist`` for Requirement 2.3. Task 9.1 accepts
            the argument for signature stability but does not consume
            it; the sandbox always runs ``network_mode="none"`` here.
            Task 9.2 or later wires ``policy.network_allowlist`` into
            :class:`LocalDockerSandbox`.
        deadline_seconds: Wall-clock ceiling for the whole verdict.
            Defaults to 300 s (Requirement 2.2). Split across the three
            in-sandbox stages as ``test_budget = deadline_seconds *
            0.5``, ``static_budget = deadline_seconds * 0.25``,
            ``plugin_budget = deadline_seconds * 0.25`` — the test
            stage is the heaviest per ``design.md §2.3``'s per-stage
            timing table. Each per-stage budget is floored at
            :data:`_MIN_STAGE_BUDGET_SECONDS` so an unreasonably
            tight caller-supplied deadline still hands each stage a
            usable timeout. The test budget is also capped at the
            time left in the deadline when the sandbox is up, and is
            shared by the Collection_Pass (at most 25%) and the test
            execution (Requirement 1.8 of the engine fail-safe spec).
            On breach, the runner returns an incomplete ``TestReport``
            and never raises
            :class:`~trikon.verify.errors.SandboxTimeoutError` past
            this module boundary (Requirement 2.2 explicit,
            ``design.md §9.1``).
        sandbox_image: Docker image tag. Overridable for tests only;
            production callers always take the default of
            :data:`~trikon.verify.sandbox.DEFAULT_SANDBOX_IMAGE`
            (``suryansh639/trikon:0.5.0``, ``design.md §5.1``).
        state_db: SQLite state database path. Defaults to
            ``repo_path / ".trikon" / "state.db"`` — the same file
            Phase 1 uses. Its parent directory is created on demand so
            first-run callers do not need to precreate ``.trikon/``.
        now: Clock injection point for coverage-map staleness checks.
            Defaults to ``datetime.now(UTC)`` inside
            :func:`select_impacted_tests` when left as ``None``.
        base_sha: The base commit SHA that ``impact`` was computed
            against. When ``None``, this task derives it from
            ``git rev-parse HEAD~1`` inside ``repo_path``. See the
            module docstring "Signature deviation" section for why
            this is a keyword-only argument on top of the
            ``design.md §3.1`` signature. Only a supplied value counts
            as a base SHA for the test strategy: with ``None``, a
            non-empty selection for a Python change falls back to the
            full suite with reason ``no_base_sha``.
        head_sha: The head commit SHA the verdict is validating. When
            ``None``, derived from ``git rev-parse HEAD`` inside
            ``repo_path``. Same rationale as ``base_sha``.
        no_sandbox: When ``True``, verification runs on the host through
            :class:`~trikon.verify.local_sandbox.LocalSubprocessSandbox`
            with **no isolation whatsoever**. Intended exclusively for
            dev machines where Docker is unavailable and the repo
            being verified is already trusted. Defaults to ``False``
            (Docker-backed :class:`LocalDockerSandbox`) because the
            production-safe posture — sandboxed pytest / static /
            plugin dispatch on a hardened container — is the only
            appropriate mode for verification against untrusted code
            (AI-authored patches, incoming PRs, third-party
            contributions). The CLI displays a security-warning
            banner every time ``--no-sandbox`` is active; the SDK
            surfaces the parameter for parity but the safety
            responsibility to warn the user belongs to the CLI.
        imports: The Import_Checker's report for this change, from
            :func:`trikon.change_intel.import_check_io.check_imports`.
            Its ``broken`` files make a collection error in those
            files attributable to the change. ``None`` means no report
            was produced and is treated as an empty
            :class:`~trikon.evidence.report.ImportReport`.

    Returns:
        A fully populated :class:`VerificationReport` composed of:

        * The :class:`~trikon.evidence.report.TestReport` from the test
          stage (see the module docstring): the Collection_Pass, the
          ``selected`` / ``full_suite`` / ``none`` strategy, the chosen
          run and the assembled report. On a timeout it is marked
          incomplete instead of raising (Requirements 2.8-2.10). Every
          path propagates ``coverage_map_stale`` verbatim from the
          selector.
        * The :class:`~trikon.evidence.report.StaticReport` produced
          by :func:`run_static_checks`.
        * The list of :class:`~trikon.evidence.report.PluginResult`
          produced by :func:`load_and_run_plugins`.
        * ``sandbox_ms`` — wall-clock milliseconds spent inside the
          ``with LocalDockerSandbox(...)`` context, i.e. image pull +
          container create/start + every in-sandbox exec + container
          teardown.
        * ``total_ms`` — wall-clock milliseconds for the whole call,
          from ``state.db`` open through report assembly.

    Raises:
        VerificationRunnerError: Any internal failure. Concrete
            subclasses that can surface here are
            :class:`~trikon.verify.errors.TestSelectionError` (state.db
            open or coverage-map lookup),
            :class:`~trikon.verify.errors.SandboxUnavailableError`
            (Docker daemon unreachable — ``sandbox.__enter__``),
            :class:`~trikon.verify.errors.SandboxExecError` (any other
            Docker API failure — sandbox lifecycle or in-container
            exec),
            :class:`~trikon.verify.errors.CollectionPassError`
            (Collection_Pass report missing, undecodable or
            malformed),
            :class:`~trikon.verify.errors.StaticCheckError` (worktree
            materialization, host baseline tool, JSON parse), and
            :class:`~trikon.verify.errors.PluginLoadError` (plugin
            shim staging, missing output file). The bare
            :class:`~trikon.verify.errors.VerificationRunnerError`
            base class is used for the ``git rev-parse`` fallback
            because no subclass semantically owns "git plumbing on
            the host".
    """
    # ``policy`` is accepted for signature stability with
    # ``design.md §3.1``; its ``network_allowlist`` feeds
    # :class:`LocalDockerSandbox` in a follow-on task. Named receipt so
    # ruff F841 and mypy do not flag it as unused in Task 9.1.
    del policy

    total_start = time.monotonic()

    # -----------------------------------------------------------------------
    # Resolve inputs the callers left blank.
    # -----------------------------------------------------------------------
    resolved_state_db = state_db if state_db is not None else (repo_path / ".trikon" / "state.db")
    # Phase 1's ``compute_impact`` already creates ``<repo>/.trikon/`` on
    # first run, but callers of the Verification Runner in isolation may
    # not have gone through that codepath. Idempotent ``mkdir`` keeps us
    # safe.
    resolved_state_db.parent.mkdir(parents=True, exist_ok=True)

    # Recorded before the ``HEAD~1`` fallback: a derived base SHA is a
    # guess, so it never makes a coverage-map selection usable.
    base_sha_supplied = base_sha is not None
    resolved_base_sha, resolved_head_sha = _resolve_shas(repo_path, base_sha, head_sha)

    # Inputs to the test stage that depend only on the change.
    resolved_imports = imports if imports is not None else ImportReport()
    python_change = is_python_change(impact)
    changed_paths = _changed_paths(impact)
    broken_import_files = frozenset(record.path for record in resolved_imports.broken)

    # Per-stage budget split — the test stage is the heaviest per
    # ``design.md §2.3``, static and plugins share the remainder.
    # Each stage is floored so an unreasonably tight caller-supplied
    # ``deadline_seconds`` still hands the stage a usable timeout. The
    # test budget is built once the sandbox is up (see
    # :func:`_build_test_budget`).
    static_budget: float = max(deadline_seconds * _STATIC_BUDGET_SHARE, _MIN_STAGE_BUDGET_SECONDS)
    plugin_budget: float = max(deadline_seconds * _PLUGIN_BUDGET_SHARE, _MIN_STAGE_BUDGET_SECONDS)
    # ``static_budget`` is threaded to :func:`run_static_checks` in a
    # follow-on wave; the Task 7 signature does not yet accept a
    # per-stage deadline. Named receipt so ruff F841 does not flag
    # the local.
    del static_budget

    # -----------------------------------------------------------------------
    # Stage 0: state DB handshake and impacted-test selection.
    # -----------------------------------------------------------------------
    # ``select_impacted_tests`` needs the state DB connection but the
    # sandbox and static checks phases also read/write it. Opening once
    # and threading the connection through avoids a second WAL handshake
    # and keeps every write in the same transaction scope.
    conn = _open_state_db(resolved_state_db)
    try:
        selected = select_impacted_tests(
            conn,
            impact,
            repo_path=repo_path,
            base_sha=resolved_base_sha,
            now=now,
        )

        # -------------------------------------------------------------------
        # Stage 1-3: sandbox lifecycle wraps pytest, static checks, plugins.
        # -------------------------------------------------------------------
        # The three in-sandbox stages run sequentially inside the same
        # container per ``design.md §2.4``. The tests are delegated to
        # :func:`_run_test_stage`, which owns the Collection_Pass, the
        # strategy choice, the chosen pytest run, the report parse and
        # the timeout handling. Static checks and the plugin loader are
        # wired via their Task 7 / Task 8 signatures unchanged.
        sandbox: Sandbox = create_sandbox(no_sandbox=no_sandbox, image=sandbox_image)
        sandbox.mount_repo(repo_path)
        # Traceback frames name the repo as ``/workspace/repo`` inside
        # Docker and as the host path on the host-local backend.
        repo_prefixes = (_DOCKER_REPO_PREFIX, str(repo_path.resolve()))

        sandbox_start = time.monotonic()
        with sandbox:
            tests_report = _run_test_stage(
                sandbox,
                selected=selected,
                python_change=python_change,
                base_sha_supplied=base_sha_supplied,
                changed_paths=changed_paths,
                broken_import_files=broken_import_files,
                budget=_build_test_budget(deadline_seconds, total_start=total_start),
                repo_prefixes=repo_prefixes,
            )

            static_report = run_static_checks(
                sandbox,
                conn,
                impact,
                repo_path=repo_path,
                base_sha=resolved_base_sha,
                head_sha=resolved_head_sha,
            )

            plugin_results = load_and_run_plugins(
                sandbox,
                repo_path,
                impact,
                per_plugin_timeout_seconds=plugin_budget,
            )
        sandbox_ms = int((time.monotonic() - sandbox_start) * 1000)
    finally:
        conn.close()

    total_ms = int((time.monotonic() - total_start) * 1000)

    return VerificationReport(
        tests=tests_report,
        static=static_report,
        plugins=list(plugin_results),
        sandbox_ms=sandbox_ms,
        total_ms=total_ms,
        imports=resolved_imports,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _changed_paths(impact: ImpactSet) -> frozenset[str]:
    """Return every path the change touches, old and new.

    The union of ``changed_files`` and each ``file_changes`` entry's ``path``
    and ``old_path``. A collection error whose file, or one of whose traceback
    frames, is in this set is attributable to the change.
    """
    paths = set(impact.changed_files)
    for file_change in impact.file_changes:
        paths.add(file_change.path)
        if file_change.old_path is not None:
            paths.add(file_change.old_path)
    return frozenset(paths)


def _build_test_budget(
    deadline_seconds: float,
    *,
    total_start: float,
    clock: Callable[[], float] = time.monotonic,
) -> TestBudget:
    """Return the test stage's budget, starting now.

    Half of ``deadline_seconds`` (at least :data:`_MIN_STAGE_BUDGET_SECONDS`),
    capped at what is left of the deadline since ``total_start`` and never
    below 0 (design §7). The Collection_Pass and the execution both run
    against the returned ``deadline_at`` (Requirement 1.8).
    """
    remaining = deadline_seconds - (clock() - total_start)
    seconds = min(
        max(deadline_seconds * _TEST_BUDGET_SHARE, _MIN_STAGE_BUDGET_SECONDS),
        max(remaining, 0.0),
    )
    return TestBudget(deadline_at=clock() + seconds, total_seconds=seconds)


def _run_test_stage(
    sandbox: Sandbox,
    *,
    selected: SelectedTests,
    python_change: bool,
    base_sha_supplied: bool,
    changed_paths: frozenset[str],
    broken_import_files: frozenset[str],
    budget: TestBudget,
    repo_prefixes: tuple[str, ...],
    clock: Callable[[], float] = time.monotonic,
) -> TestReport:
    """Run the test stage and return its :class:`TestReport`.

    The steps from the module docstring, in order:

    1. The Collection_Pass, with ``budget.collection_timeout``. It always runs
       before any test (Requirement 1.1). After a collection timeout no
       execution is started.
    2. :func:`~trikon.verify.strategy.choose_strategy`.
    3. The ``full_suite`` or ``selected`` run with
       ``budget.execution_timeout``, or nothing for ``none``. When no time is
       left the run is not started, which counts as an execution timeout.
    4. :func:`~trikon.verify.collection.assemble_test_report`. After an
       execution timeout the verbatim ``"sandbox exceeded 5-minute
       deadline"`` entry is appended to ``failures`` for display continuity.
       It changes no count.

    Args:
        sandbox: An already-entered sandbox. The caller owns the
            context-manager lifecycle; this helper only issues ``exec``
            calls.
        selected: The Test_Selector output. ``node_ids`` and
            ``coverage_map_state`` feed the strategy, and
            ``coverage_map_stale`` is threaded through to the TestReport
            on every path.
        python_change: Whether the change touches a Python_File.
        base_sha_supplied: Whether the caller passed the base SHA.
        changed_paths: Every path the change touches, old and new.
        broken_import_files: The files that hold a Broken_Import.
        budget: The test stage's time budget.
        repo_prefixes: The absolute repo roots that traceback frames may
            name, stripped from collection-error frame paths.
        clock: Monotonic clock used to read the budget. Injectable for
            tests.

    Returns:
        The assembled TestReport. A timeout never raises.

    Raises:
        CollectionPassError: The Collection_Pass report could not be read
            back, does not decode, or has the wrong shape.
        SandboxExecError: Propagated from ``sandbox.exec`` on an
            infrastructure fault. Never on a pytest non-zero exit.
        VerificationRunnerError: Raised by
            :func:`_parse_pytest_json_report` on a malformed execution
            report.
    """
    collection = _run_collection_pass(
        sandbox,
        timeout_seconds=budget.collection_timeout(clock()),
        repo_prefixes=repo_prefixes,
    )

    decision = choose_strategy(
        StrategyInputs(
            python_change=python_change,
            selected=selected.node_ids,
            coverage_map_state=selected.coverage_map_state,
            base_sha_supplied=base_sha_supplied,
        )
    )

    execution: TestReport | None = None
    execution_timed_out = False
    timeout_ms = 0
    if decision.strategy != "none" and not collection.timed_out:
        execution_timeout = budget.execution_timeout(clock())
        if execution_timeout <= 0.0:
            # The budget is spent: a run started now could only time out.
            execution_timed_out = True
        else:
            argv = (
                _FULL_SUITE_ARGV
                if decision.strategy == "full_suite"
                else _selected_argv(decision.node_ids)
            )
            # No ``env`` here: the Docker backend's base exec env already
            # points ``TMPDIR`` at the writable tmpfs
            # (``sandbox._SANDBOX_EXEC_ENV``), and the host-local backend
            # keeps the host's own temp directory.
            result = sandbox.exec(argv, workdir=_REPO_WORKDIR, timeout_seconds=execution_timeout)
            if result.timed_out:
                # Neither backend leaves a partial report behind, so there
                # is nothing to parse.
                execution_timed_out = True
                timeout_ms = int(execution_timeout * 1000)
            else:
                execution = _read_pytest_report(
                    sandbox, coverage_map_stale=selected.coverage_map_stale
                )

    classified = classify_collection_errors(
        collection.errors,
        changed_paths=changed_paths,
        broken_import_files=broken_import_files,
    )
    tests = assemble_test_report(
        python_change=python_change,
        decision=decision,
        collection=collection,
        classified=classified,
        execution=execution,
        execution_timed_out=execution_timed_out,
        coverage_map_stale=selected.coverage_map_stale,
    )
    if not execution_timed_out:
        return tests

    timeout_entry = TestResult(
        node_id="<sandbox>",
        outcome="errored",
        duration_ms=timeout_ms,
        failure_summary=_SANDBOX_TIMEOUT_MESSAGE,
    )
    return tests.model_copy(update={"failures": [*tests.failures, timeout_entry]})


def _run_collection_pass(
    sandbox: Sandbox,
    *,
    timeout_seconds: float,
    repo_prefixes: tuple[str, ...],
) -> CollectionOutcome:
    """Run the Collection_Pass and return what it observed.

    A timeout of 0 means the budget is already spent: the pass is not
    started and is recorded as timed out, the same as a pass that ran past
    its timeout. Otherwise the report is read back with ``cat`` (the tmpfs
    is not visible on the host) and parsed.

    Raises:
        CollectionPassError: ``cat`` exits non-zero, i.e. pytest wrote no
            report (a conftest import failure exits 4 before the plugin
            writes one). The message carries the tail of the collection
            output. Also raised by
            :func:`~trikon.verify.collection.parse_collection_report` for a
            report that does not decode or has the wrong shape.
        SandboxExecError: Propagated from ``sandbox.exec``.
    """
    if timeout_seconds <= 0.0:
        return _COLLECTION_TIMED_OUT

    result = sandbox.exec(_COLLECTION_ARGV, workdir=_REPO_WORKDIR, timeout_seconds=timeout_seconds)
    if result.timed_out:
        return _COLLECTION_TIMED_OUT

    read_result = sandbox.exec(
        ("cat", _COLLECT_REPORT_PATH),
        timeout_seconds=_PYTEST_REPORT_READ_TIMEOUT_SECONDS,
    )
    if read_result.exit_code != 0:
        # The Docker backend merges stderr into stdout; the host-local
        # backend keeps them apart, so both are included.
        output_tail = (result.stdout + result.stderr)[-_COLLECTION_OUTPUT_TAIL_CHARS:]
        raise CollectionPassError(
            f"run_verification: collection report {_COLLECT_REPORT_PATH} could not be read "
            f"(pytest --collect-only exited {result.exit_code}, cat exited "
            f"{read_result.exit_code}); collection output tail:\n{output_tail}"
        )
    return parse_collection_report(read_result.stdout, repo_prefixes=repo_prefixes)


def _selected_argv(node_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return the pytest argv for a ``selected`` run.

    Unchanged from the previous release, so the argv pins in existing tests
    stay valid.
    """
    return (
        "pytest",
        "--json-report",
        f"--json-report-file={_PYTEST_REPORT_PATH}",
        # ``--override-ini=addopts=`` strips any repo-side ``addopts``
        # (coverage plugins, custom formatters) so the JSON output the
        # runner depends on is not filtered out by the target repo's
        # ``pyproject.toml``.
        "--override-ini=addopts=",
        *node_ids,
    )


def _read_pytest_report(sandbox: Sandbox, *, coverage_map_stale: bool) -> TestReport:
    """Read the execution report back from the sandbox and parse it.

    ``cat`` is used (not a host-side file read) because the tmpfs mount is
    not visible on the host. A missing report reaches
    :func:`_parse_pytest_json_report` as ``cat``'s error text and fails to
    decode, raising :class:`~trikon.verify.errors.VerificationRunnerError`.
    """
    read_result = sandbox.exec(
        ("cat", _PYTEST_REPORT_PATH),
        timeout_seconds=_PYTEST_REPORT_READ_TIMEOUT_SECONDS,
    )
    return _parse_pytest_json_report(read_result.stdout, coverage_map_stale=coverage_map_stale)


def _parse_pytest_json_report(stdout: str, *, coverage_map_stale: bool) -> TestReport:
    """Parse the ``pytest-json-report`` payload into a :class:`TestReport`.

    The plugin's on-disk shape is documented at
    https://github.com/numirias/pytest-json-report. Fields consumed here:

    * ``tests`` — list of per-test entries. Each entry has:

      * ``nodeid`` — pytest node ID string (always present).
      * ``outcome`` — ``"passed"``, ``"failed"``, ``"skipped"``, or
        ``"error"``. The plugin uses ``"error"`` where the Pydantic
        model uses ``"errored"``; this helper normalises the two.
      * ``duration`` — total test duration in seconds. May be absent
        on collection-only entries; defaults to ``0.0``.
      * ``call`` — optional block for a test that reached the call
        phase. When populated, ``call.crash.message`` and
        ``call.longrepr`` carry the failure summary.
      * ``longrepr`` — some plugin versions surface the failure
        summary at the top level for setup / collection errors.

    Aggregates the per-test outcomes into the report-level counts.
    The report ``status`` is ``"failed"`` if any test failed or
    errored, ``"passed"`` if at least one test ran and none failed
    (any mix of pass/skip), and ``"skipped"`` if every entry was
    skipped (or the ``tests`` list is empty).

    Args:
        stdout: The raw JSON string read out of ``/workspace/tmp/pytest.json``
            via the in-sandbox ``cat``.
        coverage_map_stale: Threaded through to
            :attr:`TestReport.coverage_map_stale` verbatim.

    Returns:
        A fully-populated :class:`TestReport`. The ``failures`` list
        contains one :class:`TestResult` per non-passed entry (failed,
        errored, or skipped) — passed entries are counted but do not
        clutter the ``failures`` list, matching the report model's
        semantic where ``failures`` is the actionable subset.

    Raises:
        VerificationRunnerError: On malformed JSON (decode failure,
            top-level shape not a mapping, ``tests`` key missing or
            not a list, per-test entry not a mapping). The base
            class is used because no dedicated subclass semantically
            owns "pytest JSON parse"; the SDK boundary catches the
            base class regardless (Requirement 6.1).
    """
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise VerificationRunnerError(
            f"run_verification: pytest JSON report decode failed: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise VerificationRunnerError(
            f"run_verification: pytest JSON report is not a mapping (got {type(payload).__name__})"
        )

    tests_field: object = payload.get("tests", [])
    if not isinstance(tests_field, list):
        raise VerificationRunnerError(
            "run_verification: pytest JSON report 'tests' field is not a list "
            f"(got {type(tests_field).__name__})"
        )

    results: list[TestResult] = []
    total = 0
    passed = 0
    failed = 0
    skipped = 0
    errored = 0
    duration_total_seconds = 0.0

    for entry in tests_field:
        if not isinstance(entry, dict):
            raise VerificationRunnerError(
                "run_verification: pytest JSON report entry is not a mapping "
                f"(got {type(entry).__name__})"
            )

        node_id = _coerce_str(entry.get("nodeid"), fallback="<unknown>")
        raw_outcome = _coerce_str(entry.get("outcome"), fallback="errored")
        outcome = _normalise_pytest_outcome(raw_outcome)
        duration_seconds = _coerce_float(entry.get("duration"), fallback=0.0)
        duration_ms = int(duration_seconds * 1000)
        duration_total_seconds += duration_seconds

        failure_summary: str | None = None
        if outcome in ("failed", "errored", "skipped"):
            failure_summary = _extract_failure_summary(entry)

        total += 1
        if outcome == "passed":
            passed += 1
        elif outcome == "failed":
            failed += 1
        elif outcome == "errored":
            errored += 1
        else:  # "skipped"
            skipped += 1

        if outcome != "passed":
            results.append(
                TestResult(
                    node_id=node_id,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    failure_summary=failure_summary,
                )
            )

    # Report-level status: ``failed`` if any test failed or errored,
    # ``passed`` if at least one test ran and none failed, ``skipped``
    # otherwise (empty selection or every entry skipped). ``failed``
    # and ``errored`` share the ``TestReport.failed`` counter — the
    # report model only carries a three-way split
    # (passed / failed / skipped) so errored entries roll up under
    # ``failed`` per ``design.md §3.7``. The individual
    # :class:`TestResult` entries preserve the ``errored`` outcome for
    # downstream consumers that care.
    status: Literal["passed", "failed", "skipped"]
    if failed > 0 or errored > 0:
        status = "failed"
    elif passed > 0:
        status = "passed"
    else:
        status = "skipped"

    return TestReport(
        status=status,
        total=total,
        passed=passed,
        failed=failed + errored,
        skipped=skipped,
        duration_ms=int(duration_total_seconds * 1000),
        failures=results,
        coverage_map_stale=coverage_map_stale,
    )


def _normalise_pytest_outcome(
    raw: str,
) -> Literal["passed", "failed", "skipped", "errored"]:
    """Map a pytest ``outcome`` string to the :class:`TestResult` literal.

    The ``pytest-json-report`` plugin emits ``"error"`` for collection /
    setup failures; the Pydantic model uses ``"errored"``. Any unknown
    value is coerced to ``"errored"`` so a plugin version drift cannot
    stall the parse.
    """
    if raw == "passed":
        return "passed"
    if raw == "failed":
        return "failed"
    if raw == "skipped":
        return "skipped"
    # "error" (plugin) → "errored" (report model); anything else
    # (future plugin extensions, malformed data) falls into the same
    # bucket so the parse never raises on an unknown outcome.
    return "errored"


def _coerce_str(value: object, *, fallback: str) -> str:
    """Return ``value`` as a non-empty ``str`` or ``fallback``.

    Used defensively during pytest JSON parse — some plugin versions
    omit optional fields, and a report-level parse failure is much
    worse than a per-entry ``<unknown>`` node_id. The parse still
    raises :class:`VerificationRunnerError` when a mandatory *shape*
    invariant is violated (top-level not a mapping, ``tests`` not a
    list), just not on missing optional strings.
    """
    if isinstance(value, str) and value:
        return value
    return fallback


def _coerce_float(value: object, *, fallback: float) -> float:
    """Return ``value`` as a ``float`` or ``fallback``.

    Handles the ``pytest-json-report`` case where ``duration`` is
    absent on collection-only entries or emitted as an ``int`` on
    a fast test. Any non-numeric value (``None``, string, missing)
    falls through to ``fallback``.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return fallback


def _extract_failure_summary(entry: dict[str, object]) -> str | None:
    """Best-effort extraction of a human-readable failure message.

    Checks (in order): ``call.crash.message``, ``call.longrepr``,
    top-level ``longrepr``. All three are optional on the pytest
    JSON payload — a skipped test typically has none of them and the
    return is ``None``.
    """
    call = entry.get("call")
    if isinstance(call, dict):
        crash = call.get("crash")
        if isinstance(crash, dict):
            message = crash.get("message")
            if isinstance(message, str) and message:
                return message
        longrepr = call.get("longrepr")
        if isinstance(longrepr, str) and longrepr:
            return longrepr

    top_longrepr = entry.get("longrepr")
    if isinstance(top_longrepr, str) and top_longrepr:
        return top_longrepr

    return None


def _open_state_db(state_db: Path) -> sqlite3.Connection:
    """Open ``state.db``, apply the Phase-1 pragmas, ensure verify tables.

    Mirrors the pragma order established by
    :meth:`trikon.change_intel.dep_graph.DepGraph._get_conn` so the two
    subsystems agree on the connection contract:

    * ``journal_mode=WAL`` — concurrent reader/writer safety.
    * ``synchronous=NORMAL`` — the WAL-safe fsync tradeoff.
    * ``foreign_keys=ON`` — Phase-1 FK constraints are enforced.
    * ``temp_store=MEMORY`` — sort/temp tables stay off disk.

    :func:`trikon.verify.db.ensure_verify_tables` is then invoked to
    bring the Phase-2 sibling tables (``coverage_map``, ``tests_seen``,
    ``static_baseline``) into existence on a fresh state DB. The
    function is additive-only (``design.md §4.2``) and idempotent by
    construction, so callers whose DB was created by Phase 1 see no
    behavior change. Finally,
    :func:`trikon.verify.state_migrations.maybe_migrate_verify_state`
    runs the Phase-2 row-level migration (v0.3.6 drop-and-stamp on any
    marker-absent state.db; fast-path no-op otherwise).

    Args:
        state_db: Absolute path to the SQLite state database.

    Returns:
        An open :class:`sqlite3.Connection`. The caller owns the
        lifecycle and MUST close it in a ``finally`` block.

    Raises:
        TestSelectionError: Wraps any :class:`sqlite3.Error` raised
            during :func:`sqlite3.connect` or pragma application. The
            state DB is primarily a coverage-map/baseline cache reader
            from this module's perspective, so
            :class:`TestSelectionError` is the appropriate subclass.
            The original exception is preserved on ``__cause__`` via
            ``raise ... from exc`` (Requirement 6.1,
            ``design.md §9.1``). :func:`ensure_verify_tables` itself
            already wraps its own failures as
            :class:`TestSelectionError`, so those propagate unchanged.
    """
    try:
        conn = sqlite3.connect(str(state_db))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
    except sqlite3.Error as exc:
        raise TestSelectionError(
            f"run_verification: failed to open state.db at {state_db}: {exc}"
        ) from exc

    try:
        ensure_verify_tables(conn)
    except VerificationRunnerError:
        # ``ensure_verify_tables`` already wraps its ``sqlite3.Error``
        # as :class:`TestSelectionError`; propagate unchanged but
        # ensure the partially-configured connection is released so a
        # retry gets a fresh handle.
        conn.close()
        raise

    # v0.3.6: Phase-2 row-level migration. Drops any static_baseline
    # rows persisted by a pre-v0.3.6 Trikon build (verify_schema_version
    # absent from schema_meta) and stamps the marker. Idempotent on a
    # marker-present DB — a subsequent invocation reads the marker and
    # takes the fast path. See spec
    # static-baseline-cache-poisoning-migration.
    try:
        maybe_migrate_verify_state(conn)
    except VerificationRunnerError:
        conn.close()
        raise

    return conn


def _resolve_shas(
    repo_path: Path,
    base_sha: str | None,
    head_sha: str | None,
) -> tuple[str, str]:
    """Return a ``(base_sha, head_sha)`` pair, deriving from git if unset.

    When both arguments are non-``None`` this is a straight-through
    pass. Otherwise a single ``git rev-parse`` shell-out per missing
    side fills in the blanks: ``HEAD~1`` for ``base_sha``, ``HEAD`` for
    ``head_sha``. The derivation always runs with ``cwd=repo_path`` so
    a working directory outside the repo does not fool git into
    resolving against some other checkout on the developer's machine.

    Args:
        repo_path: The absolute path to the git repository, used as
            ``cwd`` for the ``git rev-parse`` invocations.
        base_sha: The caller-supplied base SHA, or ``None`` to derive.
        head_sha: The caller-supplied head SHA, or ``None`` to derive.

    Returns:
        The resolved ``(base_sha, head_sha)`` tuple. Both entries are
        non-empty strings when the function returns normally.

    Raises:
        VerificationRunnerError: On any host-side ``git rev-parse``
            failure — the git binary is missing from ``PATH``, the
            invocation times out, the process exits non-zero (repo not
            a git repository, no ``HEAD``, no ``HEAD~1`` for a
            single-commit repo), or the output is empty. The base
            class is used deliberately here because no subclass
            semantically owns "git plumbing on the host"; the SDK
            boundary catches the base class regardless
            (Requirement 6.1).
    """
    if base_sha is not None and head_sha is not None:
        return base_sha, head_sha

    resolved_base = base_sha if base_sha is not None else _git_rev_parse(repo_path, "HEAD~1")
    resolved_head = head_sha if head_sha is not None else _git_rev_parse(repo_path, "HEAD")
    return resolved_base, resolved_head


def _git_rev_parse(repo_path: Path, revspec: str) -> str:
    """Return ``git rev-parse <revspec>`` output, wrapping every failure.

    Uses :func:`subprocess.run` on the host with a bounded timeout so a
    stuck git process cannot stall the outer verdict deadline. Every
    foreign exception (``FileNotFoundError`` from a missing ``git``
    binary, ``subprocess.TimeoutExpired``, ``OSError`` on a permission
    fault) is wrapped as :class:`VerificationRunnerError` so the
    module-boundary contract (Requirement 6.1) holds.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", revspec],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_REVPARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationRunnerError(
            "run_verification: git rev-parse timed out after "
            f"{_GIT_REVPARSE_TIMEOUT_SECONDS}s for {revspec!r} in {repo_path}"
        ) from exc
    except FileNotFoundError as exc:
        raise VerificationRunnerError(
            "run_verification: git binary not found on PATH — cannot derive "
            f"{revspec!r} for {repo_path}"
        ) from exc
    except OSError as exc:
        raise VerificationRunnerError(
            "run_verification: OS error running git rev-parse for "
            f"{revspec!r} in {repo_path}: {exc}"
        ) from exc

    if result.returncode != 0:
        # Truncate stderr defensively — a runaway git message would
        # otherwise leak into the SDK boundary's error surface.
        raise VerificationRunnerError(
            f"run_verification: git rev-parse {revspec!r} failed in {repo_path}: "
            f"{result.stderr.strip()[:500]}"
        )

    sha = result.stdout.strip()
    if not sha:
        raise VerificationRunnerError(
            f"run_verification: git rev-parse {revspec!r} produced empty output in {repo_path}"
        )
    return sha
