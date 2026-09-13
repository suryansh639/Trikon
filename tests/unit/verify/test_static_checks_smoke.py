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


def test_parse_ruff_json_malformed_raises_static_check_error() -> None:
    """Non-JSON stdout raises StaticCheckError (design.md §9.1)."""
    with pytest.raises(StaticCheckError, match="ruff JSON parse failure"):
        _parse_ruff_json("this is not json{")


def test_parse_ruff_json_non_array_top_level_raises() -> None:
    """A JSON object at the top level (not an array) is a parse failure."""
    with pytest.raises(StaticCheckError, match="expected top-level array"):
        _parse_ruff_json('{"code": "F401"}')


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
            "message": (
                'Argument 1 to "process" has incompatible type "str"; '
                'expected "int"'
            ),
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
