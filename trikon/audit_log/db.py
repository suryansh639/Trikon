"""Audit-log SQLite schema — Phase-3 sibling table.

This module lands the single table the Policy Engine (Phase 3) writes on
top of the ``<repo>/.trikon/state.db`` file that the Change-Intelligence
subsystem (Phase 1) already owns and the Verification Runner (Phase 2)
extended with its own three sibling tables. Phase-1 tables (``schema_meta``,
``file_index``, ``symbols``, ``edges``) and Phase-2 tables (``coverage_map``,
``tests_seen``, ``static_baseline``) are not touched here.

Tables created here:

* ``audit_log`` — one row per Verdict ``sdk.verify`` has emitted, successful
  or fail-closed. Six columns, per ``design.md §4.1``:

  - ``audit_id TEXT PRIMARY KEY`` — UUID4 in canonical form, matches
    ``Verdict.audit_id``. Duplicate inserts fail with
    :class:`sqlite3.IntegrityError`, giving a cheap tamper detector.
  - ``created_at TEXT NOT NULL`` — ISO-8601 UTC, matches
    ``Verdict.created_at``. Indexed for compliance-audit time-range scans.
  - ``decision TEXT NOT NULL`` with ``CHECK(decision IN ('allow', 'block',
    'require_human', 'warn'))`` — the storage layer enforces the widened
    four-value alphabet. ``warn`` is permitted at the storage layer even
    though ``sdk.verify`` never emits it, keeping the SQL surface agnostic
    to the SDK-boundary invariant.
  - ``matched_rule TEXT`` — nullable; ``NULL`` on fall-through and on the
    fail-closed path.
  - ``reason TEXT NOT NULL`` — always populated. On the fail-closed path
    this carries the failing exception class name plus message so audit
    reviewers can see the cause of ``require_human`` verdicts.
  - ``verdict_json TEXT NOT NULL`` — full ``verdict.model_dump_json()``
    at the emitting build's ``schema_version`` (currently 3; rows written
    by older builds keep their own value). Lossless record; every other
    column is a projection maintained for query performance.

Additive-only invariant
-----------------------

``ensure_audit_tables`` executes only ``CREATE TABLE IF NOT EXISTS`` and
``CREATE INDEX IF NOT EXISTS`` statements. It never issues ``ALTER TABLE``,
``DROP TABLE``, or ``DROP INDEX``, and no ``UPDATE`` / ``DELETE`` / ``DROP``
verb appears anywhere in ``trikon/audit_log/**`` (Requirement 4.3,
``design.md §4.2``). Phase-3 code will not migrate Phase-1 or Phase-2
schema. If a future phase needs to change ``audit_log``, it must bump
``schema_meta.schema_version`` and ship a numbered migration, matching the
discipline the two prior phases established.

The function is idempotent by construction: calling it repeatedly on the
same connection is a no-op after the first success, and pre-existing rows
are preserved.
"""

from __future__ import annotations

import sqlite3

from trikon.policy.errors import AuditLogError

__all__ = ["ensure_audit_tables"]


_DDL_STATEMENTS: tuple[str, ...] = (
    # ---------------------------------------------------------------------
    # audit_log. One row per Verdict emitted by sdk.verify (happy path or
    # fail-closed). Append-only by construction — the writer (Task 8.1)
    # issues only INSERT after this DDL, and Requirement 4.3 forbids any
    # UPDATE / DELETE / DROP surface anywhere in trikon/audit_log/**.
    # ---------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        audit_id      TEXT NOT NULL PRIMARY KEY,
        created_at    TEXT NOT NULL,
        decision      TEXT NOT NULL
            CHECK(decision IN ('allow', 'block', 'require_human', 'warn')),
        matched_rule  TEXT,
        reason        TEXT NOT NULL,
        verdict_json  TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
        ON audit_log(created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_log_decision
        ON audit_log(decision)
    """,
)


def ensure_audit_tables(conn: sqlite3.Connection) -> None:
    """Create the ``audit_log`` table and its two indexes if absent.

    Executes only ``CREATE TABLE IF NOT EXISTS`` and
    ``CREATE INDEX IF NOT EXISTS`` statements, so a repeated call on a
    connection whose schema is already up to date is a no-op and preserves
    every existing row (Requirement 4.4). Phase-1 and Phase-2 tables are
    never touched (``design.md §4.2``).

    Args:
        conn: An open :class:`sqlite3.Connection` to the repo's ``state.db``.
            The caller is expected to have already applied the Phase-1
            pragmas (``journal_mode=WAL``, ``synchronous=NORMAL``,
            ``foreign_keys=ON``, ``temp_store=MEMORY``); this function does
            not reconfigure them.

    Raises:
        AuditLogError: Wraps any :class:`sqlite3.Error` raised while
            executing the DDL. The original exception is preserved on
            ``__cause__`` via ``raise ... from exc`` so the SDK boundary
            can inspect the underlying SQLite failure when translating to
            a ``require_human`` verdict (``design.md §9``).
    """
    try:
        for statement in _DDL_STATEMENTS:
            conn.execute(statement)
    except sqlite3.Error as exc:
        raise AuditLogError("ensure_audit_tables: failed to apply DDL") from exc
