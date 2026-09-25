"""Verification-Runner state.db row-level migrations.

This module owns the row-level migration discipline for the Phase-2
sibling tables in ``<repo>/.trikon/state.db`` (``coverage_map``,
``tests_seen``, ``static_baseline``). It is a peer to
:mod:`trikon.verify.db` — that module owns the additive-only DDL
(``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``);
this module owns the row-level state that must be dropped or
transformed on Trikon version bumps.

Phase-2 version marker
----------------------

The marker key is ``schema_meta.verify_schema_version`` (string
representation of an ``int``). It is independent of Phase-1's
``schema_meta.schema_version`` (owned by
:data:`trikon.change_intel.dep_graph.CURRENT_SCHEMA_VERSION`) — the
two versions evolve on separate cadences. A Phase-2 migration does
NOT bump Phase-1's marker, and Phase-1's schema-init does NOT read
Phase-2's marker.

Bugfix history
--------------

* v0.3.6 introduces ``CURRENT_VERIFY_SCHEMA_VERSION = 1``. The
  associated migration drops every row from ``static_baseline`` on
  any state.db that lacks the marker, closing the cache-poisoning
  trap where pre-v0.3.6 Trikon builds could persist an empty
  ``findings_json`` payload. See spec
  ``static-baseline-cache-poisoning-migration``.
"""

from __future__ import annotations

import logging
import sqlite3
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from trikon.verify.errors import VerificationRunnerError

try:
    _TRIKON_VERSION: str = _pkg_version("trikon")
except PackageNotFoundError:
    # Source checkout that was never `pip install`ed — no wheel
    # metadata is available. Fall back to the same sentinel
    # ``trikon/__init__.py`` uses so log messages produced by this
    # module remain PEP 440-valid on editable installs.
    _TRIKON_VERSION = "0.0.0+unknown"

__all__ = [
    "CURRENT_VERIFY_SCHEMA_VERSION",
    "maybe_migrate_verify_state",
]

CURRENT_VERIFY_SCHEMA_VERSION: int = 1

_VERIFY_SCHEMA_VERSION_KEY: str = "verify_schema_version"

_logger = logging.getLogger(__name__)


def maybe_migrate_verify_state(conn: sqlite3.Connection) -> None:
    """Fire the Phase-2 row-level migration on the connection if needed.

    On any state.db whose ``schema_meta`` lacks the
    ``verify_schema_version`` key, this function issues
    ``DELETE FROM static_baseline``, stamps
    ``verify_schema_version = str(CURRENT_VERIFY_SCHEMA_VERSION)``,
    and commits the DELETE + INSERT as a single atomic transaction.
    When the delete affected one or more rows, a single WARNING-level
    log record is emitted on this module's logger naming the count
    dropped and the Trikon version that fired the migration.

    On any state.db whose ``schema_meta`` already carries the marker,
    this function executes exactly one ``SELECT`` (the marker probe)
    plus the idempotent ``CREATE TABLE IF NOT EXISTS schema_meta``
    DDL and returns without side effects — the fast path.

    Args:
        conn: An open :class:`sqlite3.Connection` to the repo's
            ``state.db``. The caller (``_open_state_db``) is expected
            to have already applied the Phase-1 pragmas
            (``journal_mode=WAL``, ``synchronous=NORMAL``,
            ``foreign_keys=ON``, ``temp_store=MEMORY``) and run
            :func:`trikon.verify.db.ensure_verify_tables` so
            ``static_baseline`` is guaranteed present.

    Raises:
        VerificationRunnerError: On any :class:`sqlite3.Error` from
            the version-marker probe, the DELETE, the INSERT, or the
            idempotent ``schema_meta`` DDL; on a corrupt
            ``verify_schema_version`` value that does not parse as an
            integer; or on a stored ``verify_schema_version`` that
            parses to an integer strictly greater than
            :data:`CURRENT_VERIFY_SCHEMA_VERSION` (the state.db was
            written by a newer Trikon build and this build is not
            qualified to read from it). The original exception is
            preserved on ``__cause__`` via ``raise ... from exc``.

    Validates Requirements 1.2, 1.6, 2.1, 2.2, 2.5, 3.1, 4.1, 4.2,
    5.1, 5.2, 5.3, 5.4, 5.5.
    """
    # 1. Idempotently ensure schema_meta exists.
    #
    # In-tree, DepGraph._init_schema always creates schema_meta before
    # _open_state_db runs (compute_impact precedes run_verification in
    # every sdk.verify(...) call). Out-of-tree callers that invoke
    # run_verification directly may open a state.db with no schema_meta
    # table yet — the additive-only CREATE below unbreaks that path
    # without touching in-tree behavior.
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    except sqlite3.Error as exc:
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: failed to ensure schema_meta exists"
        ) from exc

    # 2. Version-marker probe.
    try:
        cursor = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_VERIFY_SCHEMA_VERSION_KEY,),
        )
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: verify_schema_version probe failed"
        ) from exc

    if row is not None:
        raw_value = row[0]
        try:
            stored_version = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise VerificationRunnerError(
                f"maybe_migrate_verify_state: corrupt verify_schema_version={raw_value!r}"
            ) from exc

        if stored_version > CURRENT_VERIFY_SCHEMA_VERSION:
            raise VerificationRunnerError(
                f"maybe_migrate_verify_state: state.db was written by a newer "
                f"Trikon (verify_schema_version={stored_version}, this build "
                f"supports up to {CURRENT_VERIFY_SCHEMA_VERSION})"
            )
        # stored_version <= CURRENT_VERIFY_SCHEMA_VERSION → fast path.
        return

    # 3. No marker present → migration fires. DELETE + INSERT run inside a
    # single BEGIN IMMEDIATE / COMMIT bracket so the two statements land
    # atomically (or roll back together on any sqlite3.Error).
    try:
        conn.execute("BEGIN IMMEDIATE")
        delete_cursor = conn.execute("DELETE FROM static_baseline")
        dropped_rows: int = delete_cursor.rowcount
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
            (_VERIFY_SCHEMA_VERSION_KEY, str(CURRENT_VERIFY_SCHEMA_VERSION)),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        # Best-effort rollback so the DELETE does not partially land
        # on a mid-transaction failure. Deliberate nested try/except/pass
        # (per design.md §3.1) — a contextlib.suppress rewrite would elide
        # the intentional swallow of a rollback-on-a-rollback failure that
        # we already have a wrapped VerificationRunnerError queued to raise.
        try:  # noqa: SIM105
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: static_baseline drop-and-stamp failed"
        ) from exc

    # 4. Notice — fires iff the DELETE affected one or more rows.
    if dropped_rows > 0:
        _logger.warning(
            "Trikon v%s dropped %d stale row(s) from static_baseline "
            "(Trikon < 0.3.6 pre-migration cache; see spec "
            "static-baseline-cache-poisoning-migration).",
            _TRIKON_VERSION,
            dropped_rows,
        )
