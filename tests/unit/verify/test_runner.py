"""Unit tests for :mod:`trikon.verify.runner` helpers.

Does ``_parse_pytest_json_report`` count a failed collector as a test failure?
The Full_Suite_Run passes ``--continue-on-collection-errors``, so a broken
test file is reported next to the tests that did run. The parser is
deliberately left unchanged by the engine fail-safe work, and the
Sample_Repo_Suite's ``deleted_file`` counts depend on the answer.

Answer: no. pytest-json-report puts the failed collector only in
``collectors``. ``tests`` holds only the items that ran, and ``summary``
carries no error count. The parser reads ``tests``, so the collection error
adds nothing to ``failed``; the runner reports it through the Collection_Pass
instead.

The payload below is a trimmed capture from the sandbox image
(pytest 8.3.3, pytest-json-report 1.5.0): ``pytest -p no:cacheprovider
--override-ini=addopts= --continue-on-collection-errors --json-report`` on a
repo with two passing tests and a test file that imports a missing module.
pytest exited 1 and printed ``2 passed, 1 error``.
"""

from __future__ import annotations

import json

from trikon.verify.runner import _parse_pytest_json_report

_BAD_LONGREPR = (
    "ImportError while importing test module '/workspace/repo/tests/test_bad.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "/usr/local/lib/python3.11/importlib/__init__.py:126: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_bad.py:1: in <module>\n"
    "    from gone import x\n"
    "E   ModuleNotFoundError: No module named 'gone'"
)

_FULL_SUITE_REPORT: dict[str, object] = {
    "exitcode": 1,
    "root": "/workspace/repo",
    "summary": {"passed": 2, "total": 2, "collected": 2},
    "collectors": [
        {"nodeid": "", "outcome": "passed", "result": [{"nodeid": ".", "type": "Dir"}]},
        {
            "nodeid": "tests/test_bad.py",
            "outcome": "failed",
            "result": [],
            "longrepr": _BAD_LONGREPR,
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
            "nodeid": "tests",
            "outcome": "passed",
            "result": [
                {"nodeid": "tests/test_bad.py", "type": "Module"},
                {"nodeid": "tests/test_ok.py", "type": "Module"},
            ],
        },
        {"nodeid": ".", "outcome": "passed", "result": [{"nodeid": "tests", "type": "Dir"}]},
    ],
    "tests": [
        {
            "nodeid": "tests/test_ok.py::test_a",
            "lineno": 0,
            "outcome": "passed",
            "setup": {"duration": 0.0009, "outcome": "passed"},
            "call": {"duration": 0.0005, "outcome": "passed"},
            "teardown": {"duration": 0.0004, "outcome": "passed"},
        },
        {
            "nodeid": "tests/test_ok.py::test_b",
            "lineno": 4,
            "outcome": "passed",
            "setup": {"duration": 0.0002, "outcome": "passed"},
            "call": {"duration": 0.0001, "outcome": "passed"},
            "teardown": {"duration": 0.0001, "outcome": "passed"},
        },
    ],
}


def test_failed_collector_is_not_counted_as_a_test_failure() -> None:
    tests = _parse_pytest_json_report(json.dumps(_FULL_SUITE_REPORT), coverage_map_stale=True)

    assert (tests.total, tests.passed, tests.failed, tests.skipped) == (2, 2, 0, 0)
    assert tests.status == "passed"
    assert tests.failures == []
    assert tests.coverage_map_stale is True
