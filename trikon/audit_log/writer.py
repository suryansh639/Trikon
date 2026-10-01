"""Audit-log writer — single-writer append-only surface.

This module owns the *only* function that writes to the ``audit_log``
table. It exposes exactly one call — :func:`record_verdict` — plus the
module-level SQL constant that call issues. Together with the DDL
bootstrap in :mod:`trikon.audit_log.db`, these are the entire runtime
surface of ``trikon/audit_log/**``.

Append-only contract
--------------------

The writer emits one statement per call: the ``INSERT INTO audit_log``
literal bound to :data:`_INSERT_AUDIT_LOG`. The SQL is a module-level
constant string, never composed dynamically, so the AST-scan invariant
(``design.md §17``, Property 7) has a single well-known target to grep
for verbs. No function in this module (or anywhere else under
``trikon/audit_log/**``) issues ``UPDATE``, ``DELETE``, ``DROP``,
``ALTER``, or ``TRUNCATE``; there is no ``update_verdict``, no
``delete_verdict``, no ``truncate_audit_log``, no ``purge_older_than``
(Requirement 4.3, ``design.md §8.3``).

Hard-failure contract
---------------------

Both the ``execute`` and the ``commit`` sit inside the *same*
``try`` block, so a corrupt-WAL commit failure translates to
:class:`AuditLogError` the same way an ``INSERT`` error does
(``design.md §8.1``). The SDK boundary treats :class:`AuditLogError`
as a **hard failure** — Requirement 4.5 — and re-raises rather than
fail-closing to ``require_human``. A verdict without an audit trail is
never silently returned.

See ``design.md §3.4``, ``design.md §8.1``, and Requirements 4.1, 4.2,
4.5, 7.3.
"""

from __future__ import annotations

import sqlite3

from trikon.evidence.report import Verdict
from trikon.policy.errors import AuditLogError

__all__ = ["record_verdict"]


# ---------------------------------------------------------------------------
# SQL surface — exactly one statement, defined once, as a module-level
# constant. The AST-scan invariant (Property 7, `design.md §17`) treats this
# name as the sole authoritative string literal for the audit-log write path.
# The column list matches `audit_log`'s six-column shape from `design.md §4.1`
# and the DDL in `trikon.audit_log.db._DDL_STATEMENTS`:
#
#   audit_id      TEXT PRIMARY KEY   — str(verdict.audit_id)
#   created_at    TEXT NOT NULL      — verdict.created_at.isoformat()
#   decision      TEXT NOT NULL      — verdict.decision  (CHECK enforces alphabet)
#   matched_rule  TEXT (nullable)    — verdict.matched_rule (None → SQL NULL)
#   reason        TEXT NOT NULL      — verdict.reason
#   verdict_json  TEXT NOT NULL      — verdict.model_dump_json()  (schema_version=3)
# ---------------------------------------------------------------------------
_INSERT_AUDIT_LOG: str = (
    "INSERT INTO audit_log "
    "(audit_id, created_at, decision, matched_rule, reason, verdict_json) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def record_verdict(conn: sqlite3.Connection, verdict: Verdict) -> None:
    """Append exactly one row to ``audit_log`` for ``verdict``.

    Executes :data:`_INSERT_AUDIT_LOG` with the six-column tuple derived
    from ``verdict``, then commits. Both statements share one
    ``try`` / ``except sqlite3.Error`` closure so that any SQLite failure
    — on the ``INSERT`` itself, on the commit under WAL corruption, or on
    a duplicate ``audit_id`` (:class:`sqlite3.IntegrityError`) — is
    wrapped as :class:`AuditLogError` with the original exception chained
    on ``__cause__`` (``design.md §8.1``, matrix rows 6 and 13).

    Called from :func:`trikon.sdk.verify` on both the happy-path return
    and the fail-closed return, so no Verdict is ever lost from the
    audit trail (Requirement 7.3). The call site sits *outside* the
    SDK-boundary ``try`` / ``except TrikonError`` closure; an
    :class:`AuditLogError` here propagates to the caller rather than
    fail-closing (Requirement 4.5, ``design.md §8.2``).

    Args:
        conn: An open :class:`sqlite3.Connection` to ``.trikon/state.db``.
            The caller is expected to have run
            :func:`trikon.audit_log.db.ensure_audit_tables` on this
            connection so the ``audit_log`` table exists. The commit is
            unconditional — this writer does not participate in a
            caller-owned transaction.
        verdict: The :class:`Verdict` to record. ``verdict.matched_rule``
            may be ``None``; SQLite stores it as ``NULL``. The full JSON
            serialization is captured in the ``verdict_json`` column at
            ``schema_version == 3`` — the six other columns are query
            projections of that lossless record.

    Raises:
        AuditLogError: Wraps any :class:`sqlite3.Error` raised by
            ``conn.execute`` or ``conn.commit``. The message includes
            ``verdict.audit_id`` so audit reviewers can correlate the
            failure with the (lost) verdict; the underlying SQLite
            exception is available on ``__cause__``.
    """
    try:
        conn.execute(
            _INSERT_AUDIT_LOG,
            (
                str(verdict.audit_id),
                verdict.created_at.isoformat(),
                verdict.decision,
                verdict.matched_rule,
                verdict.reason,
                verdict.model_dump_json(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise AuditLogError(f"failed to record verdict {verdict.audit_id}") from exc
