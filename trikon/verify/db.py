"""Verification-Runner SQLite schema — Phase-2 sibling tables.

This module lands the three tables the Verification Runner (Phase 2) reads and
writes on top of the ``<repo>/.trikon/state.db`` file the Change-Intelligence
subsystem already owns. Phase-1 tables (``schema_meta``, ``file_index``,
``symbols``, ``edges``) are not touched.

Tables created here:

* ``coverage_map`` — symbol → tests mapping produced by ``trikon coverage
  build``. Keyed on ``(qualified_name, built_against_sha)`` so a rebuild for
  a new base SHA writes fresh rows without evicting older ones; staleness is
  decided by ``built_at`` (age) and ``built_against_sha`` (drift) at read
  time, per ``design.md §4.2``.
* ``tests_seen`` — one row per pytest node ever executed through the
  sandbox. Feeds the coverage-builder's pruning pass and a future
  "unobserved-in-30-days" UI signal. ``last_outcome`` is constrained to the
  four pytest terminal statuses.
* ``static_baseline`` — cached ruff / mypy findings against ``base_sha``,
  keyed on ``(base_sha, tool, tool_version)``. A ``pyproject.toml`` bump of
  either tool's pinned version makes every prior cache row unreachable by
  lookup (Requirement 3.3) without needing an explicit delete.

Additive-only invariant
-----------------------

``ensure_verify_tables`` executes only ``CREATE TABLE IF NOT EXISTS`` and
``CREATE INDEX IF NOT EXISTS`` statements. It never issues ``ALTER TABLE``,
``DROP TABLE``, or ``DROP INDEX``. Phase-2 code will not migrate Phase-1
schema. If a future phase needs to change one of these three tables, it must
bump ``schema_meta.schema_version`` and ship a numbered migration, matching
the discipline Phase 1 established (``design.md §4.2``).

The function is idempotent by construction: calling it repeatedly on the same
connection is a no-op after the first success.
"""

from __future__ import annotations

import sqlite3

from trikon.verify.errors import TestSelectionError

__all__ = ["ensure_verify_tables"]


_DDL_STATEMENTS: tuple[str, ...] = (
    # ---------------------------------------------------------------------
    # coverage_map. Symbol → tests mapping built by `trikon coverage build`.
    # ---------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS coverage_map (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        qualified_name      TEXT NOT NULL,
        test_ids_json       TEXT NOT NULL,
        built_at            TEXT NOT NULL,
        built_against_sha   TEXT NOT NULL,
        UNIQUE (qualified_name, built_against_sha)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_coverage_map_qname
        ON coverage_map(qualified_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_coverage_map_built_at
        ON coverage_map(built_at)
    """,
    # ---------------------------------------------------------------------
    # tests_seen. One row per pytest node ever observed.
    # ---------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tests_seen (
        test_node_id        TEXT PRIMARY KEY,
        last_seen           TEXT NOT NULL,
        last_outcome        TEXT NOT NULL
            CHECK(last_outcome IN ('passed', 'failed', 'errored', 'skipped'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tests_seen_last_seen
        ON tests_seen(last_seen)
    """,
    # ---------------------------------------------------------------------
    # static_baseline. Cached ruff/mypy findings per (base_sha, tool_version).
    # ---------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS static_baseline (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        base_sha            TEXT NOT NULL,
        tool                TEXT NOT NULL,
        tool_version        TEXT NOT NULL,
        findings_json       TEXT NOT NULL,
        computed_at         TEXT NOT NULL,
        UNIQUE (base_sha, tool, tool_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_static_baseline_lookup
        ON static_baseline(base_sha, tool)
    """,
)


def ensure_verify_tables(conn: sqlite3.Connection) -> None:
    """Create the three Verification-Runner tables and their indexes.

    Executes only ``CREATE TABLE IF NOT EXISTS`` and
    ``CREATE INDEX IF NOT EXISTS`` statements, so a repeated call on a
    connection whose schema is already up to date is a no-op. Phase-1 tables
    are never touched (``design.md §4.2``).

    Args:
        conn: An open :class:`sqlite3.Connection` to the repo's ``state.db``.
            The caller is expected to have already applied the Phase-1
            pragmas (``journal_mode=WAL``, ``synchronous=NORMAL``,
            ``foreign_keys=ON``, ``temp_store=MEMORY``); this function does
            not reconfigure them.

    Raises:
        TestSelectionError: Wraps any :class:`sqlite3.Error` raised while
            executing the DDL. The original exception is preserved on
            ``__cause__`` via ``raise ... from exc`` so the SDK boundary
            can inspect the underlying SQLite failure when translating to
            a ``require_human`` verdict (``design.md §9``).
    """
    try:
        for statement in _DDL_STATEMENTS:
            conn.execute(statement)
    except sqlite3.Error as exc:
        raise TestSelectionError("ensure_verify_tables: failed to apply DDL") from exc
