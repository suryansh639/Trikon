"""Orchestrate one verification pass: state DB, sandbox, tests, static, plugins.

``run_verification`` is the sole public entry point of the Verification-Runner
subsystem. Given a repo path plus the ``ImpactSet`` produced by
``trikon.change_intel.compute_impact``, it opens the shared ``state.db``
connection, computes the impacted pytest node IDs via
:func:`~trikon.verify.test_selector.select_impacted_tests`, spins up a
:class:`~trikon.verify.sandbox.LocalDockerSandbox`, runs pytest against the
selected node IDs (``pytest --json-report``), then
:func:`~trikon.verify.static_checks.run_static_checks` and
:func:`~trikon.verify.plugins.load_and_run_plugins` sequentially inside the
same container (``design.md §2.4``), and assembles a
:class:`~trikon.evidence.report.VerificationReport`. Wall-clock timing for
both the sandbox lifetime and the whole call is captured through a
:func:`time.monotonic` bracket so the report shape from ``design.md §3.7``
can be populated on every success path.

Wave 3 scope (Task 9.2)
-----------------------

Task 9.2 lands the real pytest execution on top of Task 9.1's orchestrator
scaffold. The four in-sandbox stages the runner now drives are:

* **pytest** — ``pytest --json-report --json-report-file=/workspace/tmp/pytest.json
  --override-ini=addopts= <node_ids…>`` against the impacted-test subset.
  The JSON payload is read back via ``cat`` and parsed by
  :func:`_parse_pytest_json_report`.
* **static** — ruff + mypy diffed against the ``static_baseline`` cache.
* **plugins** — every ``.trikon/checks/*.py`` module in the repo.

The empty-selection short-circuit (no impacted tests) synthesizes an
``all-passed`` ``TestReport(total=0)`` and skips the pytest exec entirely —
pytest with an empty node-ID list would otherwise collect the full suite,
which contradicts Requirement 1.1's "exactly those test node IDs" guarantee.

The tests-slot ``status="skipped"`` synthesis from Task 9.1 is retired; the
selector's ``coverage_map_stale`` signal now flows through the real
``TestReport`` on every path (real pytest result, empty-selection short
circuit, or sandbox-timeout synthesis).

Never-fail-open discipline (Requirement 6.1, ``design.md §9``)
--------------------------------------------------------------

Every raise site inside :func:`run_verification` is already a
:class:`~trikon.verify.errors.VerificationRunnerError` subclass — the four
adapters and the sandbox module wrap their own foreign exceptions at the
raise site. This function therefore does **not** catch and swallow: raises
propagate unchanged and the SDK boundary (Task 12) translates them into a
``require_human`` verdict backed by ``EMPTY_VERIFICATION`` (Requirement 6.2).

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

Validates: Requirements 1.1, 3.1, 4.1, 6.1.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from trikon.evidence.report import (
    ImpactSet,
    TestReport,
    TestResult,
    VerificationReport,
)
from trikon.verify.db import ensure_verify_tables
from trikon.verify.errors import TestSelectionError, VerificationRunnerError
from trikon.verify.plugins import load_and_run_plugins
from trikon.verify.sandbox import Sandbox, create_sandbox
from trikon.verify.state_migrations import maybe_migrate_verify_state
from trikon.verify.static_checks import run_static_checks
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
# real budget requirements. Pytest is the heaviest of the three (Requirement
# 8's per-stage table from ``design.md §2.3`` allocates ≤ 5 s to pytest vs
# ≤ 3 s to each static tool and ≤ 500 ms per plugin), so pytest gets half
# the budget and static + plugins share the remainder equally.
_PYTEST_BUDGET_SHARE: float = 0.5
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


def run_verification(
    repo_path: Path,
    impact: ImpactSet,
    *,
    policy: Policy | None = None,
    deadline_seconds: float = 300.0,
    sandbox_image: str = "suryansh639/trikon:0.4.1",
    state_db: Path | None = None,
    now: datetime | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_sandbox: bool = False,
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
            and :func:`load_and_run_plugins`) and ``changed_symbols``
            (fed to :func:`select_impacted_tests`).
        policy: Optional policy carrying — in a future task — the
            ``network_allowlist`` for Requirement 2.3. Task 9.1 accepts
            the argument for signature stability but does not consume
            it; the sandbox always runs ``network_mode="none"`` here.
            Task 9.2 or later wires ``policy.network_allowlist`` into
            :class:`LocalDockerSandbox`.
        deadline_seconds: Wall-clock ceiling for the whole verdict.
            Defaults to 300 s (Requirement 2.2). Split across the three
            in-sandbox stages as ``pytest_budget = deadline_seconds *
            0.5``, ``static_budget = deadline_seconds * 0.25``,
            ``plugin_budget = deadline_seconds * 0.25`` — pytest is
            the heaviest stage per ``design.md §2.3``'s per-stage
            timing table. Each per-stage budget is floored at
            :data:`_MIN_STAGE_BUDGET_SECONDS` so an unreasonably
            tight caller-supplied deadline still hands each stage a
            usable timeout. On breach, the runner returns a
            synthesized failed ``TestReport`` and never raises
            :class:`~trikon.verify.errors.SandboxTimeoutError` past
            this module boundary (Requirement 2.2 explicit,
            ``design.md §9.1``).
        sandbox_image: Docker image tag. Overridable for tests only;
            production callers always take the default of
            ``suryansh639/trikon:0.4.1`` (``design.md §5.1``).
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
            ``design.md §3.1`` signature.
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

    Returns:
        A fully populated :class:`VerificationReport` composed of:

        * The :class:`~trikon.evidence.report.TestReport` from the
          in-sandbox ``pytest --json-report`` run (or the sandbox-
          timeout synthesis on a ``SandboxExecResult.timed_out``
          breach — Requirement 2.2). When the selector produced no
          node IDs, an all-passed ``TestReport(total=0)`` is
          synthesized and pytest is skipped so pytest does not
          collect the full suite (Requirement 1.1). Every path
          propagates ``coverage_map_stale`` verbatim from the
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

    resolved_base_sha, resolved_head_sha = _resolve_shas(repo_path, base_sha, head_sha)

    # Per-stage budget split — pytest is the heaviest stage per
    # ``design.md §2.3``, static and plugins share the remainder.
    # Each stage is floored so an unreasonably tight caller-supplied
    # ``deadline_seconds`` still hands the stage a usable timeout.
    pytest_budget: float = max(deadline_seconds * _PYTEST_BUDGET_SHARE, _MIN_STAGE_BUDGET_SECONDS)
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
        # container per ``design.md §2.4``. Pytest is delegated to
        # :func:`_run_pytest_stage` which owns the empty-selection
        # short circuit, the ``pytest --json-report`` exec, the JSON
        # parse via :func:`_parse_pytest_json_report`, and the
        # Requirement 2.2 sandbox-timeout synthesis. Static checks
        # and the plugin loader are wired via their Task 7 / Task 8
        # signatures unchanged.
        sandbox: Sandbox = create_sandbox(no_sandbox=no_sandbox, image=sandbox_image)
        sandbox.mount_repo(repo_path)

        sandbox_start = time.monotonic()
        with sandbox:
            tests_report = _run_pytest_stage(
                sandbox,
                selected_node_ids=list(selected.node_ids),
                pytest_budget=pytest_budget,
                coverage_map_stale=selected.coverage_map_stale,
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
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_pytest_stage(
    sandbox: Sandbox,
    *,
    selected_node_ids: list[str],
    pytest_budget: float,
    coverage_map_stale: bool,
) -> TestReport:
    """Run pytest on the impacted node IDs and return a :class:`TestReport`.

    Owns the three pytest-related failure paths so :func:`run_verification`
    stays a linear composition:

    * **Empty selection short-circuit** — an empty ``selected_node_ids``
      list synthesizes an all-passed ``TestReport(total=0)`` and skips
      the pytest exec entirely. pytest with no positional arguments
      would collect the full suite, which contradicts Requirement 1.1's
      "exactly those test node IDs" guarantee.
    * **Sandbox timeout synthesis** — when the pytest
      ``SandboxExecResult.timed_out`` flag is set, a failed
      ``TestReport`` is synthesized with a single
      ``TestResult(node_id="<sandbox>", outcome="errored")`` carrying
      the Requirement 2.2 verbatim ``failure_summary``. The function
      returns normally — a :class:`SandboxTimeoutError` is never
      raised past this module boundary (Requirement 2.2 explicit,
      ``design.md §9.1``).
    * **Report parse** — on a non-timeout pytest exit, the
      ``pytest-json-report`` JSON payload is read back via a bounded
      ``cat`` and parsed by :func:`_parse_pytest_json_report`.

    Args:
        sandbox: An already-entered :class:`LocalDockerSandbox`. The
            caller owns the context-manager lifecycle; this helper
            only issues ``exec`` calls.
        selected_node_ids: Deterministic-sorted pytest node IDs the
            :class:`~trikon.verify.models.SelectedTests` produced.
        pytest_budget: Wall-clock ceiling for the pytest exec. Passed
            verbatim to :meth:`LocalDockerSandbox.exec` and used to
            populate ``duration_ms`` on the synthesized timeout
            report.
        coverage_map_stale: The selector's staleness signal, threaded
            through to :attr:`TestReport.coverage_map_stale` on every
            return path (Requirement 1.3).

    Returns:
        A fully-populated :class:`TestReport`. Never raises
        :class:`~trikon.verify.errors.SandboxTimeoutError`; only
        infrastructure faults on the sandbox side or JSON-shape
        surprises propagate.

    Raises:
        SandboxExecError: Propagated from :meth:`LocalDockerSandbox.exec`
            on a Docker API failure (missing container, exec_create
            failed). Never on a pytest non-zero exit — that is a
            normal ``SandboxExecResult`` and flows through the JSON
            parse path.
        VerificationRunnerError: Raised by
            :func:`_parse_pytest_json_report` on a malformed report
            payload (JSON decode failure, missing ``tests`` key,
            wrong shape). The base class is used because no dedicated
            subclass semantically owns "pytest JSON parse".
    """
    if not selected_node_ids:
        # No impacted tests — synthesize an all-passed empty report so
        # pytest does not collect the full suite. Requirement 1.1's
        # "exactly those test node IDs" guarantee is preserved.
        return TestReport(
            status="passed",
            total=0,
            passed=0,
            failed=0,
            skipped=0,
            duration_ms=0,
            failures=[],
            coverage_map_stale=coverage_map_stale,
        )

    pytest_argv: tuple[str, ...] = (
        "pytest",
        "--json-report",
        f"--json-report-file={_PYTEST_REPORT_PATH}",
        # ``--override-ini=addopts=`` strips any repo-side ``addopts``
        # (coverage plugins, custom formatters) so the JSON output the
        # runner depends on is not filtered out by the target repo's
        # ``pyproject.toml``.
        "--override-ini=addopts=",
        *selected_node_ids,
    )

    pytest_result = sandbox.exec(
        pytest_argv,
        workdir="/workspace/repo",
        timeout_seconds=pytest_budget,
    )

    if pytest_result.timed_out:
        # Requirement 2.2 explicit: on sandbox deadline breach,
        # synthesize a failed report with the verbatim
        # ``failure_summary`` and return without raising.
        timeout_ms = int(pytest_budget * 1000)
        return TestReport(
            status="failed",
            total=len(selected_node_ids),
            passed=0,
            failed=1,
            skipped=0,
            duration_ms=timeout_ms,
            failures=[
                TestResult(
                    node_id="<sandbox>",
                    outcome="errored",
                    duration_ms=timeout_ms,
                    failure_summary=_SANDBOX_TIMEOUT_MESSAGE,
                )
            ],
            coverage_map_stale=coverage_map_stale,
        )

    # Non-timeout path — read the JSON report file back from the
    # tmpfs mount and parse it. ``cat`` is used (not a host-side
    # file read) because the tmpfs mount is not visible on the host.
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
