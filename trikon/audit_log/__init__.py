"""Trikon audit log — append-only sink for every Verdict.

The subpackage is deliberately narrow: two re-exports and nothing else.
No ``update_*``, no ``delete_*``, no ``truncate_*``, no ``purge_*``. Any
future function that mutates existing rows is a v0.2+ decision that must
survive design review (Requirement 4.3).

Public surface (Phase 3):

* :func:`trikon.audit_log.db.ensure_audit_tables` — additive-only DDL
  bootstrap. Wired into ``DepGraph._get_conn`` in Task 4.2 so the
  ``audit_log`` sibling table is present on every ``state.db`` open.
* :func:`trikon.audit_log.writer.record_verdict` — single ``INSERT`` +
  ``commit`` per Verdict. Called from ``sdk.verify`` on both the
  happy-path return and the fail-closed return, so no verdict is ever
  lost from the audit trail (Requirement 7.3).

The append-only contract (``design.md §3.5``, ``design.md §8.3``,
Requirement 4.3) is enforced by keeping ``__all__`` frozen at exactly
these two names and by an AST-scan test (Property 7, ``design.md §17``)
that rejects any ``UPDATE`` / ``DELETE`` / ``DROP`` / ``ALTER`` /
``TRUNCATE`` verb appearing in any string literal under
``trikon/audit_log/**``.
"""

from __future__ import annotations

from trikon.audit_log.db import ensure_audit_tables
from trikon.audit_log.writer import record_verdict

__all__ = ["ensure_audit_tables", "record_verdict"]
