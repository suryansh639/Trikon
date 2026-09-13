"""Task-7.1 smoke test — exercise the two parser helpers with synthetic input.

Not a full unit-test suite (that's Task 7.3, marked ``*`` in the plan). This
file is a landing check-in for Task 7.1: it confirms that
:func:`trikon.verify.static_checks._parse_ruff_json` and
:func:`trikon.verify.static_checks._parse_mypy_text` return the expected
``list[dict[str, str | int]]`` shape on canonical inputs.

Delete or fold into ``tests/unit/verify/test_static_checks.py`` when Task 7.3
lands the full suite (baseline cache miss/hit, tool-version invalidation,
``is_new`` triple discipline, and the property tests).
"""

from __future__ import annotations

import json
import logging

import pytest

from trikon.verify.errors import StaticCheckError
from trikon.verify.static_checks import (
    _parse_mypy_text,
    _parse_ruff_json,
)

# ---------------------------------------------------------------------------
# _parse_ruff_json
# ---------------------------------------------------------------------------


def test_parse_ruff_json_returns_expected_shape() -> None:
    """Canonical ruff JSON payload projects into the parser's finding shape."""
    payload = [
        {
            "code": "F401",
            "filename": "src/api/payments.py",
            "location": {"row": 1, "column": 1},
            "end_location": {"row": 1, "column": 20},
            "message": "'os' imported but unused",
            "url": "https://docs.astral.sh/ruff/rules/unused-import/",
        },
        {
            "code": "E501",
            "filename": "src/api/payments.py",
            "location": {"row": 42, "column": 100},
            "end_location": {"row": 42, "column": 120},
            "message": "line too long",
        },
    ]
    stdout = json.dumps(payload)

    parsed = _parse_ruff_json(stdout)

    assert parsed == [
        {
            "path": "src/api/payments.py",
            "line": 1,
            "rule_id": "F401",
            "message": "'os' imported but unused",
            "severity": "error",
        },
        {
            "path": "src/api/payments.py",
            "line": 42,
            "rule_id": "E501",
            "message": "line too long",
            "severity": "error",
        },
    ]


def test_parse_ruff_json_empty_stdout_returns_empty_list() -> None:
    """Empty stdout (ruff found nothing) returns an empty finding list."""
    assert _parse_ruff_json("") == []
    assert _parse_ruff_json("   \n\n  ") == []


def test_parse_ruff_json_malformed_array_raises_static_check_error() -> None:
    """Malformed JSON that starts with ``[`` still raises (genuine parse bug)."""
    with pytest.raises(StaticCheckError, match="ruff JSON parse failure"):
        _parse_ruff_json("[{invalid json without closing")


def test_parse_ruff_json_non_bracket_stdout_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stdout that does not begin with ``[`` is tolerated as no findings (Bug E).

    :class:`LocalDockerSandbox` merges stderr into stdout via docker-py's
    combined-stream ``exec_start``. A failing ruff run (e.g. read-only
    ``.ruff_cache`` path) lands its error text on ``stdout``; the parser
    logs a WARNING and returns ``[]`` rather than raising and sinking the
    whole verdict.
    """
    stdout = (
        "error: Failed to initialize cache at /workspace/repo/.ruff_cache: "
        "Read-only file system (os error 30)\n"
        "ruff failed\n"
        '  Cause: No such file or directory (os error 2) at path "..."\n'
    )
    with caplog.at_level(logging.WARNING, logger="trikon.verify.static_checks"):
        assert _parse_ruff_json(stdout) == []
    assert any(
        "does not begin with a JSON array" in record.getMessage() for record in caplog.records
    )


def test_parse_ruff_json_top_level_object_returns_empty() -> None:
    """A JSON object at top level does not start with ``[`` and is tolerated.

    Ruff never emits a top-level object under ``--output-format=json``, so
    the pre-Bug-E "expected top-level array" raise carried no real
    diagnostic value in production. The Bug-E tolerance policy folds this
    case into branch (2) of :func:`_parse_ruff_json` — warning-and-empty.
    """
    assert _parse_ruff_json('{"code": "F401"}') == []


def test_parse_ruff_json_tolerates_utf8_bom() -> None:
    """A leading UTF-8 BOM is stripped before the ``[`` heuristic fires."""
    payload = "\ufeff[]"
    assert _parse_ruff_json(payload) == []


# ---------------------------------------------------------------------------
# _parse_mypy_text
# ---------------------------------------------------------------------------


def test_parse_mypy_text_returns_expected_shape() -> None:
    """Canonical mypy diagnostic line projects into the finding shape."""
    stdout = (
        "src/api/payments.py:42:5: error: "
        'Argument 1 to "process" has incompatible type "str"; '
        'expected "int"  [arg-type]\n'
        "src/api/payments.py:44:1: note: Consider using cast\n"
    )

    parsed = _parse_mypy_text(stdout)

    assert parsed == [
        {
            "path": "src/api/payments.py",
            "line": 42,
            "rule_id": "arg-type",
            "message": ('Argument 1 to "process" has incompatible type "str"; expected "int"'),
            "severity": "error",
        },
        {
            "path": "src/api/payments.py",
            "line": 44,
            "rule_id": "",
            "message": "Consider using cast",
            "severity": "note",
        },
    ]


def test_parse_mypy_text_skips_summary_lines() -> None:
    """Trailing summary lines without a ``line:col`` position are dropped."""
    stdout = (
        "src/api/payments.py:42:5: error: bad type  [arg-type]\n"
        "\n"
        "Found 1 error in 1 file (checked 1 source file)\n"
    )
    parsed = _parse_mypy_text(stdout)
    assert len(parsed) == 1
    assert parsed[0]["rule_id"] == "arg-type"


def test_parse_mypy_text_empty_stdout_returns_empty_list() -> None:
    """Empty mypy stdout returns an empty finding list."""
    assert _parse_mypy_text("") == []


def test_parse_mypy_text_line_without_column_still_parses() -> None:
    """Mypy diagnostics without a column are still recognized."""
    stdout = "src/api/payments.py:42: error: bad type  [arg-type]\n"
    parsed = _parse_mypy_text(stdout)
    assert parsed == [
        {
            "path": "src/api/payments.py",
            "line": 42,
            "rule_id": "arg-type",
            "message": "bad type",
            "severity": "error",
        },
    ]
