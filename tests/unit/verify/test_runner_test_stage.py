"""Runner tests for the test stage (Requirement 9.6 of the engine fail-safe spec).

These drive the real :func:`trikon.verify.runner._run_test_stage` against a
fake sandbox and a fake clock. No Docker daemon is needed.

The fake sandbox answers each exec from a script keyed by the exact argv and
fails the test on any argv it was not given. The scripts are keyed by the
pinned argv literals below, not by the runner's own constants, so an argv
drift fails every stage test as well as the pin test. Report read-backs are
scripted as ``cat <path>`` execs, the way the runner reads them on both
backends.

The fake clock moves only when a scripted exec says how long it took, so each
timeout the runner hands the sandbox is an exact number. Every test uses a
100 s budget ending at t=100, so the Collection_Pass share is 25 s.

Cases (design §7):

* the empty-selection fallback to a Full_Suite_Run, including a run that
  executes nothing, which is ``skipped`` and never ``passed``;
* the fallback when the selection did not come from a Usable_Coverage_Map
  (the filename heuristic with no map, a stale map, a derived base SHA);
* an Attributable_Collection_Error through each attribution route, and a
  Collection_Error that is not attributable;
* a collection timeout, with no execution exec, and an execution timeout,
  each also when the budget is already spent before the step starts;
* the Collection_Pass running before any execution, and an unreadable
  collection report failing closed before any execution;
* strategy ``none`` issuing no execution exec;
* the argv pins: the Collection_Pass and Full_Suite_Run argv from the design,
  and the selected argv unchanged from the previous release.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

import pytest

from trikon.evidence.report import CollectionError

# Aliased so pytest does not try to collect the ``Test*`` classes.
from trikon.evidence.report import TestReport as _TestReport
from trikon.evidence.report import TestResult as _TestResult
from trikon.verify.collection import TestBudget as _Budget
from trikon.verify.errors import CollectionPassError
from trikon.verify.models import SandboxExecResult, SelectedTests
from trikon.verify.runner import (
    _COLLECT_REPORT_PATH,
    _COLLECTION_ARGV,
    _FULL_SUITE_ARGV,
    _PYTEST_REPORT_PATH,
    _run_test_stage,
    _selected_argv,
)
from trikon.verify.sandbox import Sandbox
from trikon.verify.strategy import CoverageMapState

# ---------------------------------------------------------------------------
# Pinned argv (design §7)
# ---------------------------------------------------------------------------

_REPO_WORKDIR = "/workspace/repo"
_REPO_PREFIXES = ("/workspace/repo/",)
_COLLECT_JSON_PATH = "/workspace/tmp/collect.json"
_PYTEST_JSON_PATH = "/workspace/tmp/pytest.json"

_PINNED_COLLECTION_ARGV: tuple[str, ...] = (
    "pytest",
    "--collect-only",
    "-q",
    "-p",
    "no:cacheprovider",
    "--override-ini=addopts=",
    "--json-report",
    "--json-report-file=/workspace/tmp/collect.json",
)
_PINNED_FULL_SUITE_ARGV: tuple[str, ...] = (
    "pytest",
    "-p",
    "no:cacheprovider",
    "--override-ini=addopts=",
    "--continue-on-collection-errors",
    "--json-report",
    "--json-report-file=/workspace/tmp/pytest.json",
)
_COLLECT_CAT: tuple[str, ...] = ("cat", _COLLECT_JSON_PATH)
_PYTEST_CAT: tuple[str, ...] = ("cat", _PYTEST_JSON_PATH)


def _pinned_selected_argv(*node_ids: str) -> tuple[str, ...]:
    """Return the previous release's argv for a ``selected`` run."""
    return (
        "pytest",
        "--json-report",
        "--json-report-file=/workspace/tmp/pytest.json",
        "--override-ini=addopts=",
        *node_ids,
    )


# ---------------------------------------------------------------------------
# Fake clock and sandbox
# ---------------------------------------------------------------------------


@dataclass
class _Clock:
    """A monotonic clock that moves only when a scripted exec advances it."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class _Step:
    """One scripted exec: the result it returns and the seconds it takes."""

    result: SandboxExecResult
    seconds: float = 0.0


@dataclass(frozen=True)
class _Call:
    """One exec as the fake sandbox saw it. ``workdir`` is ``None`` when not passed."""

    argv: tuple[str, ...]
    workdir: str | None
    timeout_seconds: float | None


@dataclass
class _FakeSandbox:
    """Structural stand-in for :data:`trikon.verify.sandbox.Sandbox`.

    The test stage only issues ``exec`` calls on an already-entered sandbox,
    so that is the only method. Each exec is answered from ``script`` by its
    exact argv and advances ``clock`` by the step's ``seconds``. An argv that
    is not in the script fails the test.
    """

    clock: _Clock
    script: dict[tuple[str, ...], _Step]
    calls: list[_Call] = field(default_factory=list)

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str | None = None,
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        del env
        self.calls.append(_Call(argv=argv, workdir=workdir, timeout_seconds=timeout_seconds))
        step = self.script.get(argv)
        if step is None:
            pytest.fail(f"unscripted sandbox exec: {argv!r}")
        self.clock.now += step.seconds
        return step.result

    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]


def _done(stdout: str = "", *, exit_code: int = 0) -> SandboxExecResult:
    """Return an exec that finished within its timeout."""
    return SandboxExecResult(
        exit_code=exit_code, stdout=stdout, stderr="", duration_ms=0, timed_out=False
    )


def _timed_out() -> SandboxExecResult:
    """Return an exec that ran past its timeout (the Docker backend's shape)."""
    return SandboxExecResult(exit_code=124, stdout="", stderr="", duration_ms=0, timed_out=True)


# ---------------------------------------------------------------------------
# Report payloads
# ---------------------------------------------------------------------------

_WORKER_A = "tests/test_worker.py::test_runs_job"
_WORKER_B = "tests/test_worker.py::test_skips_empty_queue"
_RETRY = "tests/test_retry.py::test_retries_until_success"
_ALL_TESTS = (_WORKER_A, _WORKER_B, _RETRY)
_ASSERTION_MESSAGE = "AssertionError: assert [3.02, 3.02] == [0.02, 0.02]"

# A test module that imports a module the change deleted.
_DELETED_MODULE_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_orders.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "/usr/local/lib/python3.11/importlib/__init__.py:126: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_orders.py:1: in <module>\n"
    "    from orders.worker import run\n"
    "E   ModuleNotFoundError: No module named 'orders.worker'"
)
_DELETED_MODULE_MESSAGE = "ModuleNotFoundError: No module named 'orders.worker'"

# A test module whose import chain fails inside a changed source file. The
# frame is absolute, so attribution depends on the repo prefix being stripped.
_CHANGED_SOURCE_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_orders.py'.\n"
    "Traceback:\n"
    "tests/test_orders.py:1: in <module>\n"
    "    from orders.api import handler\n"
    "/workspace/repo/src/orders/api.py:3: in <module>\n"
    "    from orders.helpers import retry_delay\n"
    "E   ImportError: cannot import name 'retry_delay' from 'orders.helpers'"
)
_CHANGED_SOURCE_MESSAGE = "ImportError: cannot import name 'retry_delay' from 'orders.helpers'"

# A test module that fails for a reason the change did not cause.
_MISSING_DEPENDENCY_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_legacy.py'.\n"
    "Traceback:\n"
    "tests/test_legacy.py:1: in <module>\n"
    "    import yaml\n"
    "E   ModuleNotFoundError: No module named 'yaml'"
)
_MISSING_DEPENDENCY_MESSAGE = "ModuleNotFoundError: No module named 'yaml'"


def _collector(
    nodeid: str, children: tuple[str, ...] = (), *, longrepr: str | None = None
) -> dict[str, object]:
    """Return one ``collectors`` entry; a ``longrepr`` makes it a failed one."""
    entry: dict[str, object] = {
        "nodeid": nodeid,
        "outcome": "passed" if longrepr is None else "failed",
        "result": [{"nodeid": child} for child in children],
    }
    if longrepr is not None:
        entry["longrepr"] = longrepr
    return entry


def _collect_json(*broken: tuple[str, str]) -> str:
    """Return a ``collect.json`` for the three-test suite.

    ``broken`` adds (test file, longrepr) pairs: test modules that failed to
    import. They add no leaf items, so ``collected`` stays 3.
    """
    broken_paths = tuple(path for path, _ in broken)
    collectors = [
        _collector("", ("tests",)),
        _collector("tests", ("tests/test_worker.py", "tests/test_retry.py", *broken_paths)),
        _collector("tests/test_worker.py", (_WORKER_A, _WORKER_B)),
        _collector("tests/test_retry.py", (_RETRY,)),
        *(_collector(path, longrepr=longrepr) for path, longrepr in broken),
    ]
    return json.dumps({"exitcode": 2 if broken else 0, "collectors": collectors})


def _pytest_json(*, passed: tuple[str, ...] = (), failed: tuple[str, ...] = ()) -> str:
    """Return a ``pytest.json`` whose ``tests`` hold the given outcomes."""
    tests: list[dict[str, object]] = [
        {"nodeid": nodeid, "outcome": "passed", "duration": 0.25} for nodeid in passed
    ]
    tests.extend(
        {
            "nodeid": nodeid,
            "outcome": "failed",
            "duration": 0.25,
            "call": {"crash": {"message": _ASSERTION_MESSAGE}},
        }
        for nodeid in failed
    )
    return json.dumps({"tests": tests})


def _collection_steps(collect_json: str, *, seconds: float = 0.0) -> dict[tuple[str, ...], _Step]:
    """Script a Collection_Pass that finishes and writes ``collect_json``."""
    return {
        _PINNED_COLLECTION_ARGV: _Step(_done(), seconds=seconds),
        _COLLECT_CAT: _Step(_done(collect_json)),
    }


def _execution_steps(argv: tuple[str, ...], pytest_json: str) -> dict[tuple[str, ...], _Step]:
    """Script an execution with ``argv`` that finishes and writes ``pytest_json``."""
    return {argv: _Step(_done()), _PYTEST_CAT: _Step(_done(pytest_json))}


# ---------------------------------------------------------------------------
# Driving the stage
# ---------------------------------------------------------------------------

_BUDGET = _Budget(deadline_at=100.0, total_seconds=100.0)
_CHANGED_SOURCE = frozenset({"src/orders/api.py"})
_NO_BROKEN_IMPORTS: frozenset[str] = frozenset()
_SANDBOX_TIMEOUT_ENTRY_SUMMARY = "sandbox exceeded 5-minute deadline"


def _selection(*node_ids: str, state: CoverageMapState = "present") -> SelectedTests:
    """Return a Test_Selector output backed by a map in ``state``."""
    return SelectedTests(
        node_ids=node_ids,
        coverage_map_stale=state != "present",
        fallback_reasons=(),
        coverage_map_state=state,
    )


def _run(
    sandbox: _FakeSandbox,
    *,
    selected: SelectedTests,
    python_change: bool = True,
    base_sha_supplied: bool = True,
    changed_paths: frozenset[str] = _CHANGED_SOURCE,
    broken_import_files: frozenset[str] = _NO_BROKEN_IMPORTS,
) -> _TestReport:
    """Run the real test stage on the fake sandbox and its clock."""
    return _run_test_stage(
        cast(Sandbox, sandbox),
        selected=selected,
        python_change=python_change,
        base_sha_supplied=base_sha_supplied,
        changed_paths=changed_paths,
        broken_import_files=broken_import_files,
        budget=_BUDGET,
        repo_prefixes=_REPO_PREFIXES,
        clock=sandbox.clock,
    )


# ---------------------------------------------------------------------------
# Argv pins
# ---------------------------------------------------------------------------


def test_argv_pins() -> None:
    assert _COLLECT_REPORT_PATH == _COLLECT_JSON_PATH
    assert _PYTEST_REPORT_PATH == _PYTEST_JSON_PATH
    assert _COLLECTION_ARGV == _PINNED_COLLECTION_ARGV
    assert "--collect-only" in _COLLECTION_ARGV
    assert _FULL_SUITE_ARGV == _PINNED_FULL_SUITE_ARGV
    assert "--continue-on-collection-errors" in _FULL_SUITE_ARGV
    assert _selected_argv((_WORKER_A, _RETRY)) == _pinned_selected_argv(_WORKER_A, _RETRY)


# ---------------------------------------------------------------------------
# Strategy fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pytest_json", "executed", "status"),
    [
        pytest.param(_pytest_json(passed=_ALL_TESTS), 3, "passed", id="suite-ran"),
        pytest.param(_pytest_json(), 0, "skipped", id="suite-ran-nothing"),
    ],
)
def test_empty_selection_falls_back_to_the_full_suite(
    pytest_json: str, executed: int, status: str
) -> None:
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(_collect_json()),
            **_execution_steps(_PINNED_FULL_SUITE_ARGV, pytest_json),
        },
    )

    report = _run(sandbox, selected=_selection(state="missing"))

    assert sandbox.argvs() == [
        _PINNED_COLLECTION_ARGV,
        _COLLECT_CAT,
        _PINNED_FULL_SUITE_ARGV,
        _PYTEST_CAT,
    ]
    assert report.strategy == "full_suite"
    assert report.strategy_reasons == ["empty_selection"]
    assert report.collected == 3
    assert (report.total, report.executed, report.passed, report.failed) == (
        executed,
        executed,
        executed,
        0,
    )
    # The previous release synthesized a pass here. A run that executed
    # nothing is now skipped.
    assert report.status == status
    assert report.incomplete is False
    assert report.coverage_map_stale is True


@pytest.mark.parametrize(
    ("state", "base_sha_supplied", "reasons"),
    [
        pytest.param("missing", True, ["coverage_map_missing"], id="heuristic-no-map"),
        pytest.param("stale", True, ["coverage_map_stale"], id="stale-map"),
        pytest.param(
            "missing", False, ["coverage_map_missing", "no_base_sha"], id="no-map-no-base-sha"
        ),
        pytest.param("present", False, ["no_base_sha"], id="map-with-derived-base-sha"),
    ],
)
def test_selection_without_a_usable_coverage_map_falls_back_to_the_full_suite(
    state: CoverageMapState, base_sha_supplied: bool, reasons: list[str]
) -> None:
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(_collect_json()),
            **_execution_steps(_PINNED_FULL_SUITE_ARGV, _pytest_json(passed=_ALL_TESTS)),
        },
    )

    # The selector picked only the worker tests, e.g. by filename heuristic.
    report = _run(
        sandbox,
        selected=_selection(_WORKER_A, _WORKER_B, state=state),
        base_sha_supplied=base_sha_supplied,
    )

    # The full suite runs instead of the selected argv.
    assert sandbox.argvs() == [
        _PINNED_COLLECTION_ARGV,
        _COLLECT_CAT,
        _PINNED_FULL_SUITE_ARGV,
        _PYTEST_CAT,
    ]
    assert report.strategy == "full_suite"
    assert report.strategy_reasons == reasons
    # Counts come from the full suite, not from the two selected tests.
    assert (report.total, report.executed, report.passed) == (3, 3, 3)
    assert report.status == "passed"
    assert report.coverage_map_stale is (state != "present")


# ---------------------------------------------------------------------------
# Collection errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("longrepr", "message", "changed_paths", "broken_import_files"),
    [
        pytest.param(
            _DELETED_MODULE_LONGREPR,
            _DELETED_MODULE_MESSAGE,
            frozenset({"tests/test_orders.py"}),
            frozenset(),
            id="changed-test-file",
        ),
        pytest.param(
            _CHANGED_SOURCE_LONGREPR,
            _CHANGED_SOURCE_MESSAGE,
            frozenset({"src/orders/api.py"}),
            frozenset(),
            id="frame-in-changed-file",
        ),
        pytest.param(
            _DELETED_MODULE_LONGREPR,
            _DELETED_MODULE_MESSAGE,
            frozenset({"src/orders/worker.py"}),
            frozenset({"tests/test_orders.py"}),
            id="broken-import-in-test-file",
        ),
    ],
)
def test_attributable_collection_error_fails_the_report(
    longrepr: str,
    message: str,
    changed_paths: frozenset[str],
    broken_import_files: frozenset[str],
) -> None:
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(_collect_json(("tests/test_orders.py", longrepr))),
            **_execution_steps(_PINNED_FULL_SUITE_ARGV, _pytest_json(passed=_ALL_TESTS)),
        },
    )

    report = _run(
        sandbox,
        selected=_selection(state="missing"),
        changed_paths=changed_paths,
        broken_import_files=broken_import_files,
    )

    assert report.collection_errors == [
        CollectionError(path="tests/test_orders.py", message=message, attributable=True)
    ]
    assert report.failures == [
        _TestResult(
            node_id="tests/test_orders.py",
            outcome="errored",
            duration_ms=0,
            failure_summary=message,
        )
    ]
    assert report.status == "failed"
    # The error changes no count: the three tests that ran all passed.
    assert (report.collected, report.executed, report.passed, report.failed) == (3, 3, 3, 0)
    assert report.incomplete is False
    assert report.incomplete_reasons == []


def test_non_attributable_collection_error_marks_the_report_incomplete() -> None:
    selected_argv = _pinned_selected_argv(_WORKER_A, _WORKER_B)
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(
                _collect_json(("tests/test_legacy.py", _MISSING_DEPENDENCY_LONGREPR))
            ),
            **_execution_steps(selected_argv, _pytest_json(passed=(_WORKER_A, _WORKER_B))),
        },
    )

    report = _run(sandbox, selected=_selection(_WORKER_A, _WORKER_B))

    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT, selected_argv, _PYTEST_CAT]
    assert report.collection_errors == [
        CollectionError(
            path="tests/test_legacy.py",
            message=_MISSING_DEPENDENCY_MESSAGE,
            attributable=False,
        )
    ]
    # Not added to failures, and the status follows the run. The report is
    # marked incomplete instead.
    assert report.failures == []
    assert report.status == "passed"
    assert report.executed == 2
    assert report.incomplete is True
    assert report.incomplete_reasons == ["collection_error"]


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_collection_timeout_issues_no_execution_exec() -> None:
    # Only the collection exec is scripted: a ``cat`` or a test run fails.
    sandbox = _FakeSandbox(_Clock(), {_PINNED_COLLECTION_ARGV: _Step(_timed_out(), seconds=25.0)})

    report = _run(sandbox, selected=_selection(state="missing"))

    # The Collection_Pass gets its 25% share of the 100 s budget.
    assert sandbox.calls == [
        _Call(argv=_PINNED_COLLECTION_ARGV, workdir=_REPO_WORKDIR, timeout_seconds=25.0)
    ]
    assert report.incomplete is True
    assert report.incomplete_reasons == ["collection_timeout"]
    assert report.status == "skipped"
    assert (report.collected, report.total, report.executed) == (0, 0, 0)
    assert report.failures == []
    assert report.strategy == "full_suite"


def test_spent_budget_starts_no_collection_and_counts_as_collection_timeout() -> None:
    sandbox = _FakeSandbox(_Clock(now=100.0), {})

    report = _run(sandbox, selected=_selection(state="missing"))

    assert sandbox.calls == []
    assert report.incomplete_reasons == ["collection_timeout"]
    assert report.status == "skipped"
    assert (report.collected, report.executed) == (0, 0)


def test_execution_timeout_marks_the_report_incomplete() -> None:
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(_collect_json(), seconds=10.0),
            _PINNED_FULL_SUITE_ARGV: _Step(_timed_out(), seconds=90.0),
        },
    )

    report = _run(sandbox, selected=_selection(state="missing"))

    # No report is read back after the timeout.
    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT, _PINNED_FULL_SUITE_ARGV]
    # Collection took 10 s, so execution gets the 90 s left before the deadline.
    assert sandbox.calls[0].timeout_seconds == 25.0
    assert sandbox.calls[2].timeout_seconds == 90.0
    assert sandbox.calls[2].workdir == _REPO_WORKDIR
    assert report.incomplete is True
    assert report.incomplete_reasons == ["execution_timeout"]
    assert report.status == "skipped"
    assert report.collected == 3
    assert (report.total, report.executed) == (0, 0)
    # Kept in failures for display; it changes no count.
    assert report.failures == [
        _TestResult(
            node_id="<sandbox>",
            outcome="errored",
            duration_ms=90_000,
            failure_summary=_SANDBOX_TIMEOUT_ENTRY_SUMMARY,
        )
    ]


def test_no_time_left_for_execution_counts_as_execution_timeout() -> None:
    # 20 s before the deadline the Collection_Pass gets 20 s, less than its
    # 25 s share, and uses all of it.
    sandbox = _FakeSandbox(_Clock(now=80.0), _collection_steps(_collect_json(), seconds=20.0))

    report = _run(sandbox, selected=_selection(state="missing"))

    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT]
    assert sandbox.calls[0].timeout_seconds == 20.0
    assert report.incomplete_reasons == ["execution_timeout"]
    assert report.status == "skipped"
    assert report.collected == 3
    assert report.failures == [
        _TestResult(
            node_id="<sandbox>",
            outcome="errored",
            duration_ms=0,
            failure_summary=_SANDBOX_TIMEOUT_ENTRY_SUMMARY,
        )
    ]


# ---------------------------------------------------------------------------
# Ordering and the no-run strategy
# ---------------------------------------------------------------------------


def test_collection_runs_before_the_selected_execution() -> None:
    selected_argv = _pinned_selected_argv(_WORKER_A, _RETRY)
    sandbox = _FakeSandbox(
        _Clock(),
        {
            **_collection_steps(_collect_json()),
            **_execution_steps(selected_argv, _pytest_json(passed=(_WORKER_A,), failed=(_RETRY,))),
        },
    )

    report = _run(sandbox, selected=_selection(_WORKER_A, _RETRY))

    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT, selected_argv, _PYTEST_CAT]
    assert [call.workdir for call in sandbox.calls if call.argv[0] == "pytest"] == [
        _REPO_WORKDIR,
        _REPO_WORKDIR,
    ]
    assert report.strategy == "selected"
    assert report.strategy_reasons == []
    assert (report.collected, report.total, report.executed) == (3, 2, 2)
    assert (report.passed, report.failed) == (1, 1)
    assert report.status == "failed"
    assert [(f.node_id, f.failure_summary) for f in report.failures] == [
        (_RETRY, _ASSERTION_MESSAGE)
    ]


def test_unreadable_collection_report_fails_closed_before_any_execution() -> None:
    # A conftest import failure: pytest exits 4 and writes no report.
    conftest_output = (
        "ImportError while loading conftest '/workspace/repo/tests/conftest.py'.\n"
        "tests/conftest.py:1: in <module>\n"
        "    import missing_plugin\n"
        "E   ModuleNotFoundError: No module named 'missing_plugin'\n"
    )
    sandbox = _FakeSandbox(
        _Clock(),
        {
            _PINNED_COLLECTION_ARGV: _Step(_done(conftest_output, exit_code=4)),
            _COLLECT_CAT: _Step(
                _done(f"cat: {_COLLECT_JSON_PATH}: No such file or directory\n", exit_code=1)
            ),
        },
    )

    with pytest.raises(CollectionPassError, match="exited 4") as excinfo:
        _run(sandbox, selected=_selection(_WORKER_A))

    assert "No module named 'missing_plugin'" in str(excinfo.value)
    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT]


def test_strategy_none_issues_no_execution_exec() -> None:
    sandbox = _FakeSandbox(_Clock(), _collection_steps(_collect_json()))

    report = _run(
        sandbox,
        selected=_selection(state="missing"),
        python_change=False,
        changed_paths=frozenset({"docs/usage.md"}),
    )

    # The Collection_Pass still runs; no test run starts.
    assert sandbox.argvs() == [_PINNED_COLLECTION_ARGV, _COLLECT_CAT]
    assert report.strategy == "none"
    assert report.strategy_reasons == []
    assert report.status == "passed"
    assert (report.collected, report.total, report.executed) == (3, 0, 0)
    assert report.incomplete is False
    assert report.failures == []
