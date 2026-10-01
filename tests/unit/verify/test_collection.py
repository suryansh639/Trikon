"""Example tests for :mod:`trikon.verify.collection`.

Covers the three pure pieces the runner's test stage uses:

* ``parse_collection_report`` against a payload captured from the sandbox
  image's pytest-json-report (leaf counting, failed collectors, ``E`` line
  messages, frame-path stripping) plus malformed payloads;
* ``classify_collection_errors`` for each attribution rule;
* ``TestBudget`` and ``assemble_test_report``, one example per status rule.

The modules are imported whole (``collection.TestBudget``,
``report.TestReport``) so pytest does not try to collect the ``Test*``
classes.
"""

from __future__ import annotations

import json

import pytest

from trikon.evidence import report
from trikon.verify import collection
from trikon.verify.errors import CollectionPassError
from trikon.verify.strategy import StrategyDecision

_DOCKER_PREFIXES = ("/workspace/repo/",)

# Trimmed from a real ``pytest --collect-only --json-report`` run in the
# sandbox image: two passing tests, an ImportError in the test file itself, an
# ImportError raised from a source module, and a SyntaxError.
_BAD_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_bad.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "/usr/local/lib/python3.11/importlib/__init__.py:126: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_bad.py:1: in <module>\n"
    "    from pkg.gone import x\n"
    "E   ModuleNotFoundError: No module named 'pkg.gone'"
)
_FRAME_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_frame.py'.\n"
    "Traceback:\n"
    "/usr/local/lib/python3.11/importlib/__init__.py:126: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_frame.py:1: in <module>\n"
    "    import pkg.mod\n"
    "pkg/mod.py:1: in <module>\n"
    "    import nonexistent_dep\n"
    "E   ModuleNotFoundError: No module named 'nonexistent_dep'"
)
_SYNTAX_LONGREPR = (
    "/usr/local/lib/python3.11/site-packages/_pytest/python.py:493: in importtestmodule\n"
    "    mod = import_path(\n"
    "<frozen importlib._bootstrap>:1204: in _gcd_import\n"
    "    ???\n"
    "/usr/local/lib/python3.11/ast.py:50: in parse\n"
    "    return compile(source, filename, mode, flags,\n"
    'E     File "/workspace/repo/tests/test_syntax.py", line 1\n'
    "E       def broken(\n"
    "E                 ^\n"
    "E   SyntaxError: '(' was never closed"
)
_SANDBOX_REPORT: dict[str, object] = {
    "exitcode": 2,
    "summary": {"total": 0, "collected": 2},
    "collectors": [
        {"nodeid": "", "outcome": "passed", "result": [{"nodeid": ".", "type": "Dir"}]},
        {"nodeid": "pkg", "outcome": "passed", "result": []},
        {
            "nodeid": "tests/test_bad.py",
            "outcome": "failed",
            "result": [],
            "longrepr": _BAD_LONGREPR,
        },
        {
            "nodeid": "tests/test_frame.py",
            "outcome": "failed",
            "result": [],
            "longrepr": _FRAME_LONGREPR,
        },
        {
            "nodeid": "tests/test_ok.py",
            "outcome": "passed",
            "result": [
                {"nodeid": "tests/test_ok.py::test_a", "type": "Function", "lineno": 0},
                {"nodeid": "tests/test_ok.py::test_b", "type": "Function", "lineno": 4},
            ],
        },
        {
            "nodeid": "tests/test_syntax.py",
            "outcome": "failed",
            "result": [],
            "longrepr": _SYNTAX_LONGREPR,
        },
        {
            "nodeid": "tests",
            "outcome": "passed",
            "result": [
                {"nodeid": "tests/test_bad.py", "type": "Module"},
                {"nodeid": "tests/test_frame.py", "type": "Module"},
                {"nodeid": "tests/test_ok.py", "type": "Module"},
                {"nodeid": "tests/test_syntax.py", "type": "Module"},
            ],
        },
        {
            "nodeid": ".",
            "outcome": "passed",
            "result": [
                {"nodeid": "pkg", "type": "Package"},
                {"nodeid": "tests", "type": "Dir"},
            ],
        },
    ],
    "tests": [],
}


def _failed_collector(nodeid: str, longrepr: str) -> dict[str, object]:
    return {"nodeid": nodeid, "outcome": "failed", "result": [], "longrepr": longrepr}


def _parse_one(
    collector: dict[str, object], prefixes: tuple[str, ...] = _DOCKER_PREFIXES
) -> collection.RawCollectionError:
    outcome = collection.parse_collection_report(
        json.dumps({"collectors": [collector]}), repo_prefixes=prefixes
    )
    assert len(outcome.errors) == 1
    return outcome.errors[0]


# ---------------------------------------------------------------------------
# parse_collection_report
# ---------------------------------------------------------------------------


def test_parse_sandbox_report_counts_leaves_and_failed_collectors() -> None:
    outcome = collection.parse_collection_report(
        json.dumps(_SANDBOX_REPORT), repo_prefixes=_DOCKER_PREFIXES
    )

    assert outcome.timed_out is False
    # Only the two Function items are leaves; every Dir/Package/Module child
    # is itself a collector.
    assert outcome.collected == 2
    assert outcome.errors == (
        collection.RawCollectionError(
            path="tests/test_bad.py",
            message="ModuleNotFoundError: No module named 'pkg.gone'",
            frame_paths=("tests/test_bad.py",),
        ),
        collection.RawCollectionError(
            path="tests/test_frame.py",
            message="ModuleNotFoundError: No module named 'nonexistent_dep'",
            frame_paths=("tests/test_frame.py", "pkg/mod.py"),
        ),
        collection.RawCollectionError(
            path="tests/test_syntax.py",
            message=(
                'File "/workspace/repo/tests/test_syntax.py", line 1\n'
                "    def broken(\n"
                "              ^\n"
                "SyntaxError: '(' was never closed"
            ),
            frame_paths=("tests/test_syntax.py",),
        ),
    )


def test_parse_counts_a_leaf_listed_twice_once() -> None:
    payload = {
        "collectors": [
            {"nodeid": "tests/test_a.py", "outcome": "passed", "result": [{"nodeid": "t::x"}]},
            {"nodeid": "tests/test_b.py", "outcome": "passed", "result": [{"nodeid": "t::x"}]},
        ]
    }
    outcome = collection.parse_collection_report(json.dumps(payload), repo_prefixes=())
    assert outcome.collected == 1


def test_parse_without_collectors_key_collects_nothing() -> None:
    outcome = collection.parse_collection_report(
        json.dumps({"exitcode": 5, "tests": []}), repo_prefixes=_DOCKER_PREFIXES
    )
    assert outcome == collection.CollectionOutcome(timed_out=False, collected=0, errors=())


def test_parse_session_collector_path_and_class_nodeid() -> None:
    session = _parse_one(_failed_collector("", "E   boom"))
    assert session.path == "<session>"

    in_class = _parse_one(_failed_collector("tests/test_a.py::TestX", "E   boom"))
    assert in_class.path == "tests/test_a.py"


def test_parse_message_falls_back_to_first_line_and_is_cut_to_1000() -> None:
    no_e_lines = _parse_one(_failed_collector("tests/t.py", "\n\n  first line  \nsecond"))
    assert no_e_lines.message == "first line"

    long_error = _parse_one(_failed_collector("tests/t.py", "E   " + "x" * 5000))
    assert long_error.message == "x" * 1000

    no_text = _parse_one({"nodeid": "tests/t.py", "outcome": "failed", "result": []})
    assert no_text.message
    assert no_text.frame_paths == ()


def test_parse_strips_host_prefix_and_drops_frames_outside_the_repo() -> None:
    longrepr = (
        "C:\\Python311\\Lib\\importlib\\__init__.py:126: in import_module\n"
        "C:\\work\\repo\\tests\\test_a.py:3: in <module>\n"
        "C:\\work\\repo\\src\\pkg\\a.py:7: in <module>\n"
        "../outside/b.py:2: in <module>\n"
        "./src/pkg/c.py:9: in helper\n"
        "src/pkg/a.py:8: in <module>\n"
        "E   ImportError: nope"
    )
    error = _parse_one(_failed_collector("tests/test_a.py", longrepr), ("C:\\work\\repo",))
    assert error.frame_paths == ("tests/test_a.py", "src/pkg/a.py", "src/pkg/c.py")


def test_parse_rejects_undecodable_json() -> None:
    with pytest.raises(CollectionPassError, match="decode failed"):
        collection.parse_collection_report("{not json", repo_prefixes=_DOCKER_PREFIXES)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"collectors": {}},
        {"collectors": [1]},
        {"collectors": [{"outcome": "passed", "result": []}]},
        {"collectors": [{"nodeid": "a", "result": []}]},
        {"collectors": [{"nodeid": "a", "outcome": "passed"}]},
        {"collectors": [{"nodeid": "a", "outcome": "passed", "result": ["x"]}]},
        {"collectors": [{"nodeid": "a", "outcome": "passed", "result": [{"type": "Function"}]}]},
        {"collectors": [{"nodeid": "a", "outcome": "failed", "result": [], "longrepr": 3}]},
    ],
)
def test_parse_rejects_wrong_shapes(payload: object) -> None:
    with pytest.raises(CollectionPassError):
        collection.parse_collection_report(json.dumps(payload), repo_prefixes=_DOCKER_PREFIXES)


# ---------------------------------------------------------------------------
# classify_collection_errors
# ---------------------------------------------------------------------------


def test_classify_marks_each_attribution_rule() -> None:
    errors = (
        collection.RawCollectionError("tests/test_changed.py", "m1", ()),
        collection.RawCollectionError("tests/test_frame.py", "m2", ("src/changed.py",)),
        collection.RawCollectionError("tests/test_broken.py", "m3", ()),
        collection.RawCollectionError("tests/test_other.py", "m4", ("src/other.py",)),
    )
    classified = collection.classify_collection_errors(
        errors,
        changed_paths=frozenset({"tests/test_changed.py", "src/changed.py"}),
        broken_import_files=frozenset({"tests/test_broken.py"}),
    )
    assert [(e.path, e.message, e.attributable) for e in classified] == [
        ("tests/test_changed.py", "m1", True),
        ("tests/test_frame.py", "m2", True),
        ("tests/test_broken.py", "m3", True),
        ("tests/test_other.py", "m4", False),
    ]


# ---------------------------------------------------------------------------
# TestBudget
# ---------------------------------------------------------------------------


def test_budget_timeouts() -> None:
    budget = collection.TestBudget(deadline_at=100.0, total_seconds=40.0)

    assert budget.collection_timeout(60.0) == 10.0  # the 25% share
    assert budget.collection_timeout(95.0) == 5.0  # what is left
    assert budget.collection_timeout(120.0) == 0.0
    assert budget.execution_timeout(70.0) == 30.0
    assert budget.execution_timeout(120.0) == 0.0


# ---------------------------------------------------------------------------
# assemble_test_report
# ---------------------------------------------------------------------------

_SELECTED = StrategyDecision(strategy="selected", reasons=(), node_ids=("tests/test_a.py",))
_FULL = StrategyDecision(
    strategy="full_suite", reasons=("coverage_map_missing", "no_base_sha"), node_ids=()
)
_NONE = StrategyDecision(strategy="none", reasons=(), node_ids=())
_CLEAN = collection.CollectionOutcome(timed_out=False, collected=7, errors=())
_ATTRIBUTABLE = report.CollectionError(path="tests/test_a.py", message="boom", attributable=True)
_UNRELATED = report.CollectionError(path="tests/test_z.py", message="other", attributable=False)


def _execution(*, passed: int = 0, failed: int = 0, skipped: int = 0) -> report.TestReport:
    failures = [
        report.TestResult(node_id=f"tests/test_a.py::f{i}", outcome="failed", duration_ms=1)
        for i in range(failed)
    ]
    return report.TestReport(
        status="failed" if failed else "passed",
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_ms=1234,
        failures=failures,
    )


def _assemble(
    *,
    python_change: bool = True,
    decision: StrategyDecision = _FULL,
    outcome: collection.CollectionOutcome = _CLEAN,
    classified: tuple[report.CollectionError, ...] = (),
    execution: report.TestReport | None = None,
    execution_timed_out: bool = False,
    coverage_map_stale: bool = True,
) -> report.TestReport:
    return collection.assemble_test_report(
        python_change=python_change,
        decision=decision,
        collection=outcome,
        classified=classified,
        execution=execution,
        execution_timed_out=execution_timed_out,
        coverage_map_stale=coverage_map_stale,
    )


def test_assemble_copies_counts_strategy_and_collection_fields() -> None:
    tests = _assemble(execution=_execution(passed=3, skipped=2))

    assert tests.status == "passed"
    assert (tests.total, tests.passed, tests.failed, tests.skipped) == (5, 3, 0, 2)
    assert tests.executed == 3
    assert tests.duration_ms == 1234
    assert tests.collected == 7
    assert tests.strategy == "full_suite"
    assert tests.strategy_reasons == ["coverage_map_missing", "no_base_sha"]
    assert tests.coverage_map_stale is True
    assert tests.incomplete is False
    assert tests.incomplete_reasons == []


def test_assemble_attributable_error_fails_without_changing_counts() -> None:
    execution = _execution(passed=4)
    tests = _assemble(classified=(_ATTRIBUTABLE, _UNRELATED), execution=execution)

    assert tests.status == "failed"
    assert (tests.total, tests.passed, tests.failed, tests.executed) == (4, 4, 0, 4)
    assert tests.failures == [
        report.TestResult(
            node_id="tests/test_a.py", outcome="errored", duration_ms=0, failure_summary="boom"
        )
    ]
    assert tests.collection_errors == [_ATTRIBUTABLE, _UNRELATED]
    assert tests.incomplete_reasons == ["collection_error"]
    assert tests.incomplete is True


def test_assemble_strategy_none_for_non_python_change_passes_on_zero() -> None:
    tests = _assemble(python_change=False, decision=_NONE)

    assert tests.status == "passed"
    assert (tests.total, tests.executed, tests.strategy) == (0, 0, "none")


def test_assemble_strategy_none_for_python_change_is_skipped() -> None:
    assert _assemble(python_change=True, decision=_NONE).status == "skipped"


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        (None, "skipped"),
        (_execution(passed=2), "skipped"),
        (_execution(passed=2, failed=1), "failed"),
    ],
)
def test_assemble_execution_timeout(execution: report.TestReport | None, expected: str) -> None:
    tests = _assemble(decision=_SELECTED, execution=execution, execution_timed_out=True)
    assert tests.status == expected
    assert tests.incomplete_reasons == ["execution_timeout"]


def test_assemble_reasons_follow_the_fixed_order() -> None:
    timed_out = collection.CollectionOutcome(timed_out=True, collected=0, errors=())
    tests = _assemble(outcome=timed_out, classified=(_UNRELATED,), execution_timed_out=True)

    assert tests.status == "skipped"
    assert tests.incomplete_reasons == [
        "collection_timeout",
        "execution_timeout",
        "collection_error",
    ]


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        (_execution(passed=5, failed=1), "failed"),
        (_execution(passed=1), "passed"),
        (_execution(skipped=3), "skipped"),
        (None, "skipped"),
    ],
)
def test_assemble_completed_run_status(execution: report.TestReport | None, expected: str) -> None:
    tests = _assemble(execution=execution)
    assert tests.status == expected
    assert tests.executed == tests.passed + tests.failed
