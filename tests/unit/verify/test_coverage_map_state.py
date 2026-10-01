"""Unit tests for ``SelectedTests.coverage_map_state``.

Covers task 7.4 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``
(Requirement 1.4). :func:`select_impacted_tests` classifies the map that
backed a selection:

* ``"missing"`` when the ``coverage_map`` table has no rows;
* ``"stale"`` when the freshest ``built_at`` is more than seven days old, or
  when any changed symbol missed the map (including ``base_sha=None``);
* ``"present"`` otherwise.

``coverage_map_stale`` keeps its previous-release meaning and must always
equal ``coverage_map_state != "present"``. Every test compares the whole
:class:`SelectedTests` value, so both fields are pinned in each case.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trikon.evidence.report import ImpactSet, SymbolRef
from trikon.verify.db import ensure_verify_tables
from trikon.verify.errors import TestSelectionError as _SelectionError
from trikon.verify.models import SelectedTests
from trikon.verify.test_selector import select_impacted_tests

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
_BASE_SHA = "a" * 40
_OTHER_SHA = "b" * 40

_RETRY = SymbolRef(
    qualified_name="payments.retry.retry_payment",
    file_path="src/payments/retry.py",
    kind="function",
)
_WORKER = SymbolRef(
    qualified_name="orders.worker.process",
    file_path="src/orders/worker.py",
    kind="function",
)

_RETRY_IDS = ("tests/test_retry.py::test_retries_once",)
_WORKER_IDS = ("tests/test_worker.py::test_process", "tests/test_worker.py::test_idle")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """An in-memory state DB with the Verification-Runner tables applied."""
    connection = sqlite3.connect(":memory:")
    ensure_verify_tables(connection)
    try:
        yield connection
    finally:
        connection.close()


def _impact(*symbols: SymbolRef) -> ImpactSet:
    """Build an :class:`ImpactSet` whose only meaningful field is ``changed_symbols``."""
    return ImpactSet(
        changed_files=sorted({symbol.file_path for symbol in symbols}),
        changed_symbols=list(symbols),
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=0.0,
    )


def _insert_row(
    conn: sqlite3.Connection,
    symbol: SymbolRef,
    test_ids: tuple[str, ...],
    *,
    built_at: datetime,
    sha: str = _BASE_SHA,
) -> None:
    """Insert one ``coverage_map`` row the way the coverage builder writes it."""
    conn.execute(
        "INSERT INTO coverage_map "
        "(qualified_name, test_ids_json, built_at, built_against_sha) "
        "VALUES (?, ?, ?, ?)",
        (symbol.qualified_name, json.dumps(list(test_ids)), built_at.isoformat(), sha),
    )


def _reason(symbol: SymbolRef) -> str:
    return f"{symbol.qualified_name}: no coverage-map row"


# ---------------------------------------------------------------------------
# Empty map
# ---------------------------------------------------------------------------


def test_empty_map_is_missing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """No rows at all: every symbol falls back and the state is ``missing``."""
    selected = select_impacted_tests(
        conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=(),
        coverage_map_stale=True,
        fallback_reasons=(_reason(_RETRY),),
        coverage_map_state="missing",
    )


def test_empty_map_without_changed_symbols_is_still_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """``missing`` describes the map, so it holds even when nothing needs a lookup."""
    selected = select_impacted_tests(
        conn, _impact(), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=(),
        coverage_map_stale=True,
        fallback_reasons=(),
        coverage_map_state="missing",
    )


# ---------------------------------------------------------------------------
# Old map
# ---------------------------------------------------------------------------


def test_old_map_is_stale_even_when_every_symbol_has_a_row(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An eight-day-old map is not consulted; the filename heuristic runs instead."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=8))
    heuristic_file = tmp_path / "tests" / "test_retry.py"
    heuristic_file.parent.mkdir()
    heuristic_file.write_text("def test_placeholder() -> None:\n    pass\n", newline="\n")

    selected = select_impacted_tests(
        conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=("tests/test_retry.py",),
        coverage_map_stale=True,
        fallback_reasons=(_reason(_RETRY),),
        coverage_map_state="stale",
    )


@pytest.mark.parametrize(
    ("age", "expected_state"),
    [
        (timedelta(days=7), "present"),
        (timedelta(days=7, seconds=1), "stale"),
    ],
    ids=["exactly-seven-days", "just-over-seven-days"],
)
def test_map_age_boundary(
    conn: sqlite3.Connection, tmp_path: Path, age: timedelta, expected_state: str
) -> None:
    """A map exactly seven days old is still fresh; one second older is stale."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - age)

    selected = select_impacted_tests(
        conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected.coverage_map_state == expected_state
    assert selected.coverage_map_stale is (expected_state != "present")


# ---------------------------------------------------------------------------
# Fresh map
# ---------------------------------------------------------------------------


def test_fresh_map_with_every_symbol_hit_is_present(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Every symbol has a row under ``base_sha``: the map backs the selection."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=1))
    _insert_row(conn, _WORKER, _WORKER_IDS, built_at=_NOW - timedelta(hours=1))

    selected = select_impacted_tests(
        conn, _impact(_RETRY, _WORKER), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=tuple(sorted(_RETRY_IDS + _WORKER_IDS)),
        coverage_map_stale=False,
        fallback_reasons=(),
        coverage_map_state="present",
    )


def test_fresh_map_without_changed_symbols_is_present(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A fresh map with nothing to look up has no misses, so it is ``present``."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=1))

    selected = select_impacted_tests(
        conn, _impact(), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=(),
        coverage_map_stale=False,
        fallback_reasons=(),
        coverage_map_state="present",
    )


# ---------------------------------------------------------------------------
# Symbol misses on a fresh map
# ---------------------------------------------------------------------------


def test_fresh_map_with_one_symbol_miss_is_stale(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """One missed symbol makes the state ``stale``; the hit still contributes its tests."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=1))

    selected = select_impacted_tests(
        conn, _impact(_RETRY, _WORKER), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=_RETRY_IDS,
        coverage_map_stale=True,
        fallback_reasons=(_reason(_WORKER),),
        coverage_map_state="stale",
    )


def test_fresh_map_row_for_another_base_sha_is_a_miss(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A row built against a different SHA does not match the requested ``base_sha``."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=1), sha=_OTHER_SHA)

    selected = select_impacted_tests(
        conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=(),
        coverage_map_stale=True,
        fallback_reasons=(_reason(_RETRY),),
        coverage_map_state="stale",
    )


def test_fresh_map_without_base_sha_is_stale(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """With no base SHA the SHA-keyed map cannot be consulted, so every symbol misses."""
    _insert_row(conn, _RETRY, _RETRY_IDS, built_at=_NOW - timedelta(days=1))

    selected = select_impacted_tests(
        conn, _impact(_RETRY), repo_path=tmp_path, base_sha=None, now=_NOW
    )

    assert selected == SelectedTests(
        node_ids=(),
        coverage_map_stale=True,
        fallback_reasons=(_reason(_RETRY),),
        coverage_map_state="stale",
    )


# ---------------------------------------------------------------------------
# Freshness raise sites
# ---------------------------------------------------------------------------


def test_sqlite_error_on_freshness_query_is_wrapped(tmp_path: Path) -> None:
    """A state DB without ``coverage_map`` raises ``TestSelectionError`` from the SQLite error."""
    bare = sqlite3.connect(":memory:")
    try:
        with pytest.raises(_SelectionError) as excinfo:
            select_impacted_tests(
                bare, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
            )
    finally:
        bare.close()

    assert isinstance(excinfo.value.__cause__, sqlite3.Error)


def test_non_string_built_at_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A BLOB keeps its type under TEXT affinity and must not be parsed as a timestamp."""
    conn.execute(
        "INSERT INTO coverage_map "
        "(qualified_name, test_ids_json, built_at, built_against_sha) "
        "VALUES (?, ?, ?, ?)",
        (_RETRY.qualified_name, json.dumps(list(_RETRY_IDS)), b"\x00\x01", _BASE_SHA),
    )

    with pytest.raises(_SelectionError, match="not a string"):
        select_impacted_tests(
            conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
        )


def test_non_iso_built_at_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """An unparseable ``built_at`` raises ``TestSelectionError`` from the ``ValueError``."""
    conn.execute(
        "INSERT INTO coverage_map "
        "(qualified_name, test_ids_json, built_at, built_against_sha) "
        "VALUES (?, ?, ?, ?)",
        (_RETRY.qualified_name, json.dumps(list(_RETRY_IDS)), "last tuesday", _BASE_SHA),
    )

    with pytest.raises(_SelectionError, match="not ISO-8601") as excinfo:
        select_impacted_tests(
            conn, _impact(_RETRY), repo_path=tmp_path, base_sha=_BASE_SHA, now=_NOW
        )

    assert isinstance(excinfo.value.__cause__, ValueError)
