"""Unit tests for :mod:`trikon.verify.state_migrations`.

Task 2.1 for the ``static-baseline-cache-poisoning-migration`` spec.
The Phase-2 row-level migration lives at
:func:`trikon.verify.state_migrations.maybe_migrate_verify_state`
(landed by Task 1.1) and is wired into
:func:`trikon.verify.runner._open_state_db` immediately after
:func:`trikon.verify.db.ensure_verify_tables` (landed by Task 1.2).
The v0.3.6 discipline is: drop every ``static_baseline`` row on any
state.db that lacks the ``schema_meta.verify_schema_version`` marker,
stamp the marker inside the same transaction, and emit a single
WARNING record on ``trikon.verify.state_migrations`` when the delete
affected one or more rows. Marker-present databases fast-path via a
single SELECT probe (plus the idempotent ``CREATE TABLE IF NOT EXISTS
schema_meta`` DDL) and touch no rows.

Four test groups, spelled out verbatim in tasks.md §2.1:

* **Group A — hypothesis property tests** (3 functions). Drive
  ``maybe_migrate_verify_state`` with hypothesis-generated row sets
  and integer row counts to lock the three deliverable-named
  correctness properties from design.md §4:
  Property 1 (atomic drop-and-stamp on pre-v0.3.6 state.db),
  Property 2 (marker-present state.db fast-path no-op),
  Property 3 (notice-firing biconditional). Each property test
  builds its own fresh in-memory connection per hypothesis example
  so state does not leak across iterations.

* **Group B — edge-case tests** (6 functions). Exercise the migration's
  non-happy-path branches: schema_meta-absent state.db,
  future-version marker refuse, corrupt marker parse failure,
  sqlite-error on the DELETE (rows preserved), sqlite-error on the
  INSERT (DELETE rolled back), and idempotence on a second call.

* **Group C — wiring tests** (2 functions). Confirm
  ``_open_state_db`` calls ``maybe_migrate_verify_state`` after
  ``ensure_verify_tables`` on the same connection and closes the
  connection when the migration raises.

* **Group D — public-surface test** (1 function). Locks
  ``__all__``, ``CURRENT_VERIFY_SCHEMA_VERSION``, and the
  :func:`inspect.signature` shape of
  :func:`maybe_migrate_verify_state`.

Every test runs without a live Docker daemon, without a network
connection, and without a real ruff / mypy binary. The
:func:`in_memory_verify_db` fixture is the only backing store — an
in-memory SQLite connection with the four Phase-1 pragmas applied and
the three Phase-2 tables created.

Validates: Requirements 1.1, 1.2, 1.4, 1.6, 2.1, 2.2, 2.5, 3.1, 3.2,
3.3, 4.1, 4.2, 4.4, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.9, 6.10, 7.1, 7.2,
7.3, 7.4, 7.5, 7.6.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import trikon
from trikon.verify import runner as _runner
from trikon.verify import state_migrations as _state_migrations
from trikon.verify.db import ensure_verify_tables
from trikon.verify.errors import VerificationRunnerError
from trikon.verify.state_migrations import (
    CURRENT_VERIFY_SCHEMA_VERSION,
    maybe_migrate_verify_state,
)

# ``trikon/__init__.py`` re-exports :func:`trikon.sdk.verify` as ``trikon.verify``,
# which shadows the ``trikon.verify`` subpackage during attribute walks (see the
# note in ``tests/unit/verify/test_local_sandbox_cache_dir_strip.py``). We bind
# the ``trikon.verify.runner`` and ``trikon.verify.state_migrations`` module
# objects to the ``_runner`` / ``_state_migrations`` locals above and use those
# for every :meth:`monkeypatch.setattr` call and every public-surface probe
# below, avoiding the dotted-string path walk that would resolve
# ``trikon.verify`` to the function.

# ---------------------------------------------------------------------------
# Module-level wrapper classes (Group A Property 2 + Group B fault tests +
# Group C wiring tests). Deliberately not nested inside test bodies — the
# fixture-discipline section of tasks.md §2.1 pins them as module-level so
# multiple tests can share the same wrapper contract.
# ---------------------------------------------------------------------------


class SpyConnection:
    """Wrap a :class:`sqlite3.Connection` and record every SQL verb.

    The wrapper forwards every :meth:`execute` call to the underlying
    connection and appends the leading verb of ``sql`` (uppercased —
    ``"SELECT"`` / ``"CREATE"`` / ``"DELETE"`` / ``"INSERT"`` /
    ``"BEGIN"`` / ``"COMMIT"`` / ``"ROLLBACK"`` / ``"PRAGMA"``) into
    :attr:`recorded_verbs`. :meth:`close` calls are counted into
    :attr:`close_call_count` and forwarded so the underlying handle is
    released on teardown.

    Callers use :func:`typing.cast` to hand the spy to APIs typed
    against :class:`sqlite3.Connection` (the wrapper is not a
    subclass — ``sqlite3.Connection.__init__`` is not stable enough
    to inherit from cleanly).
    """

    def __init__(self, underlying: sqlite3.Connection) -> None:
        """Record the underlying connection and initialize empty counters."""
        self._underlying: sqlite3.Connection = underlying
        self.recorded_verbs: list[str] = []
        self.close_call_count: int = 0

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        """Forward the ``execute`` to the underlying connection, recording the leading SQL verb."""
        stripped = sql.strip()
        verb = stripped.split()[0].upper() if stripped else ""
        self.recorded_verbs.append(verb)
        return self._underlying.execute(sql, parameters)

    def close(self) -> None:
        """Record the close call and release the underlying connection."""
        self.close_call_count += 1
        self._underlying.close()


class FaultInjectingConnection:
    """Wrap a :class:`sqlite3.Connection` and raise on statements matching a prefix.

    On any :meth:`execute` whose ``sql`` (uppercased, whitespace-
    stripped) starts with :attr:`_fault_prefix` (also uppercased),
    the wrapper raises :class:`sqlite3.OperationalError` carrying
    :attr:`_error_msg`. Every other :meth:`execute` is forwarded to
    the underlying connection unchanged. This is the vehicle for the
    "sqlite error on DELETE" and "sqlite error on INSERT" edge tests
    in Group B — the fault fires on one specific statement inside
    ``maybe_migrate_verify_state`` while every other statement
    (``CREATE TABLE IF NOT EXISTS schema_meta``, the marker probe
    SELECT, ``BEGIN IMMEDIATE``, ``ROLLBACK``) runs against the real
    connection so the rollback semantics remain observable on the
    underlying store.
    """

    def __init__(
        self,
        underlying: sqlite3.Connection,
        fault_prefix: str,
        error_msg: str,
    ) -> None:
        """Record the underlying handle and the case-insensitive fault trigger."""
        self._underlying: sqlite3.Connection = underlying
        self._fault_prefix: str = fault_prefix.upper()
        self._error_msg: str = error_msg

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        """Raise :class:`sqlite3.OperationalError` iff ``sql`` starts with the fault prefix."""
        if sql.strip().upper().startswith(self._fault_prefix):
            raise sqlite3.OperationalError(self._error_msg)
        return self._underlying.execute(sql, parameters)

    def close(self) -> None:
        """Forward the close to the underlying connection."""
        self._underlying.close()


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


def _open_verify_db() -> sqlite3.Connection:
    """Return a fresh in-memory :class:`sqlite3.Connection` with verify tables.

    Opens ``sqlite3.connect(":memory:")`` with ``isolation_level=None``
    (so the explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` inside
    :func:`maybe_migrate_verify_state` runs unshadowed by Python's
    auto-transaction management), applies the four Phase-1 pragmas
    mirroring :func:`trikon.verify.runner._open_state_db`, and runs
    :func:`ensure_verify_tables` so ``static_baseline`` /
    ``coverage_map`` / ``tests_seen`` exist. ``schema_meta`` is NOT
    created — the migration owns that DDL.

    Used by Group A property tests, which need a fresh connection per
    hypothesis example (a function-scoped fixture would leak state
    across iterations).
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    ensure_verify_tables(conn)
    return conn


@pytest.fixture()
def in_memory_verify_db() -> Iterator[sqlite3.Connection]:
    """Yield an in-memory :class:`sqlite3.Connection` post-``ensure_verify_tables``.

    Mirrors the shape of the runner's ``_open_state_db``: pragmas
    (``journal_mode=WAL``, ``synchronous=NORMAL``, ``foreign_keys=ON``,
    ``temp_store=MEMORY``), ``isolation_level=None`` so
    :func:`maybe_migrate_verify_state`'s explicit
    ``BEGIN IMMEDIATE`` / ``COMMIT`` is not shadowed by Python's
    auto-transaction management, and :func:`ensure_verify_tables` so
    the Phase-2 tables exist. ``schema_meta`` is NOT seeded — the
    migration owns that DDL. Tears down via ``conn.close()``.

    Used by Group B edge-case tests; Group A property tests create
    their own fresh connection per hypothesis example via
    :func:`_open_verify_db`.
    """
    conn = _open_verify_db()
    try:
        yield conn
    finally:
        conn.close()


def _seed_row(
    conn: sqlite3.Connection,
    base_sha: str,
    tool: str,
    tool_version: str,
    findings_json: str,
) -> None:
    """Insert one ``static_baseline`` row with a fixed ``computed_at`` timestamp.

    The ``computed_at`` column is required by the DDL but is not part
    of the ``(base_sha, tool, tool_version)`` uniqueness key, so a
    single fixed value is sufficient across all seed calls.
    """
    conn.execute(
        "INSERT INTO static_baseline "
        "(base_sha, tool, tool_version, findings_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (base_sha, tool, tool_version, findings_json, "2024-01-01T00:00:00+00:00"),
    )


def _seed_marker(conn: sqlite3.Connection, version_value: str) -> None:
    """Create ``schema_meta`` (if needed) and insert the ``verify_schema_version`` row."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        ("verify_schema_version", version_value),
    )


# Hypothesis row-tuple strategy used by Properties 1 and 2. Each tuple is
# ``(base_sha, tool, tool_version, findings_json)``; ``unique_by`` honors the
# ``UNIQUE (base_sha, tool, tool_version)`` constraint on ``static_baseline``.
_ROW_STRATEGY: st.SearchStrategy[tuple[str, str, str, str]] = st.tuples(
    st.text(min_size=1, max_size=40).map(str.strip).filter(bool),
    st.sampled_from(["ruff", "mypy"]),
    st.text(min_size=0, max_size=30),
    st.one_of(st.just("[]"), st.text(min_size=0, max_size=50)),
)

_ROW_LIST_STRATEGY: st.SearchStrategy[list[tuple[str, str, str, str]]] = st.lists(
    _ROW_STRATEGY,
    min_size=0,
    max_size=25,
    unique_by=lambda t: (t[0], t[1], t[2]),
)


# ===========================================================================
# Group A — hypothesis property tests
# ===========================================================================


@settings(max_examples=100, deadline=None)
@given(rows=_ROW_LIST_STRATEGY)
def test_property_migration_drops_every_prev036_row_atomically(
    rows: list[tuple[str, str, str, str]],
) -> None:
    """Feature: static-baseline-cache-poisoning-migration, Property 1: migration drops every pre-v0.3.6 row atomically."""
    conn = _open_verify_db()
    try:
        for base_sha, tool, tool_version, findings_json in rows:
            _seed_row(conn, base_sha, tool, tool_version, findings_json)

        maybe_migrate_verify_state(conn)

        count_row = conn.execute("SELECT COUNT(*) FROM static_baseline").fetchone()
        assert count_row is not None
        assert count_row[0] == 0

        marker_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ("verify_schema_version",),
        ).fetchone()
        assert marker_row == ("1",)
    finally:
        conn.close()


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_ROW_LIST_STRATEGY)
def test_property_marker_present_state_db_is_fast_path_no_op(
    rows: list[tuple[str, str, str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Feature: static-baseline-cache-poisoning-migration, Property 2: marker-present state.db is a fast-path no-op."""
    conn = _open_verify_db()
    try:
        _seed_marker(conn, "1")
        for base_sha, tool, tool_version, findings_json in rows:
            _seed_row(conn, base_sha, tool, tool_version, findings_json)

        before = list(
            conn.execute(
                "SELECT base_sha, tool, tool_version, findings_json "
                "FROM static_baseline ORDER BY id"
            )
        )

        spy = SpyConnection(conn)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="trikon.verify.state_migrations"):
            maybe_migrate_verify_state(cast(sqlite3.Connection, spy))

        after = list(
            conn.execute(
                "SELECT base_sha, tool, tool_version, findings_json "
                "FROM static_baseline ORDER BY id"
            )
        )
        assert after == before

        warning_records = [
            record
            for record in caplog.records
            if record.name == "trikon.verify.state_migrations"
            and record.levelno == logging.WARNING
        ]
        assert warning_records == []

        non_allowed_verbs = [
            verb for verb in spy.recorded_verbs if verb not in {"CREATE", "SELECT"}
        ]
        assert len(non_allowed_verbs) <= 1, (
            f"marker-present migration emitted disallowed verbs: "
            f"{spy.recorded_verbs}"
        )
    finally:
        conn.close()


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(n=st.integers(min_value=0, max_value=50))
def test_property_notice_fires_iff_dropped_rows_positive(
    n: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Feature: static-baseline-cache-poisoning-migration, Property 3: notice fires iff DELETE dropped ≥1 row."""
    conn = _open_verify_db()
    try:
        for i in range(n):
            _seed_row(conn, f"sha{i:040x}", "ruff", "0.7.4", "[]")

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="trikon.verify.state_migrations"):
            maybe_migrate_verify_state(conn)

        warning_records = [
            record
            for record in caplog.records
            if record.name == "trikon.verify.state_migrations"
            and record.levelno == logging.WARNING
        ]

        if n > 0:
            assert len(warning_records) == 1
            message = warning_records[0].getMessage()
            assert str(n) in message
            assert trikon.__version__ in message
            assert "static-baseline-cache-poisoning-migration" in message
        else:
            assert warning_records == []
    finally:
        conn.close()


# ===========================================================================
# Group B — edge-case tests
# ===========================================================================


def test_schema_meta_absent_migration_creates_it_idempotently(
    in_memory_verify_db: sqlite3.Connection,
) -> None:
    """Migration creates ``schema_meta`` and stamps the marker on a bare verify DB.

    :func:`ensure_verify_tables` builds only the three Phase-2 tables;
    ``schema_meta`` is Phase-1 territory. Out-of-tree callers that
    open ``_open_state_db`` on a state.db never touched by
    Change-Intelligence would hit a ``no such table: schema_meta``
    error without the idempotent CREATE at the top of
    :func:`maybe_migrate_verify_state`. Locks Requirement 1.6.
    """
    maybe_migrate_verify_state(in_memory_verify_db)

    table_row = in_memory_verify_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    assert table_row is not None
    assert table_row[0] == "schema_meta"

    marker_row = in_memory_verify_db.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        ("verify_schema_version",),
    ).fetchone()
    assert marker_row == ("1",)


def test_future_version_state_db_raises_verification_runner_error(
    in_memory_verify_db: sqlite3.Connection,
) -> None:
    """A future-version marker refuses to open. Locks Requirement 2.5."""
    _seed_marker(in_memory_verify_db, "2")

    with pytest.raises(VerificationRunnerError) as excinfo:
        maybe_migrate_verify_state(in_memory_verify_db)

    message = str(excinfo.value)
    assert "2" in message
    assert str(CURRENT_VERIFY_SCHEMA_VERSION) in message


def test_corrupt_marker_value_raises_verification_runner_error(
    in_memory_verify_db: sqlite3.Connection,
) -> None:
    """A non-integer marker value raises with the ``__cause__`` preserved. Locks Requirement 5.4."""
    _seed_marker(in_memory_verify_db, "not-an-int")

    with pytest.raises(VerificationRunnerError) as excinfo:
        maybe_migrate_verify_state(in_memory_verify_db)

    assert isinstance(excinfo.value.__cause__, (TypeError, ValueError))
    message = str(excinfo.value)
    assert "not-an-int" in message


def test_sqlite_error_on_delete_wraps_and_rows_preserved(
    in_memory_verify_db: sqlite3.Connection,
) -> None:
    """``sqlite3.OperationalError`` on the DELETE wraps as ``VerificationRunnerError`` and preserves rows.

    Locks Requirement 5.2 — the DELETE branch of the atomic
    drop-and-stamp transaction. The fault fires before the DELETE
    lands on the underlying connection; the marker INSERT never
    runs; the ROLLBACK forwards through the wrapper and unwinds the
    ``BEGIN IMMEDIATE`` that the migration issued on the underlying.
    """
    _seed_row(in_memory_verify_db, "sha-a", "ruff", "0.7.4", "[]")
    _seed_row(in_memory_verify_db, "sha-b", "ruff", "0.9.10", "[]")
    _seed_row(in_memory_verify_db, "sha-c", "mypy", "1.11.2", "[]")

    wrapper = FaultInjectingConnection(
        in_memory_verify_db,
        fault_prefix="DELETE FROM static_baseline",
        error_msg="mocked-delete-failure",
    )

    with pytest.raises(VerificationRunnerError) as excinfo:
        maybe_migrate_verify_state(cast(sqlite3.Connection, wrapper))

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)

    count_row = in_memory_verify_db.execute(
        "SELECT COUNT(*) FROM static_baseline"
    ).fetchone()
    assert count_row is not None
    assert count_row[0] == 3

    marker_row = in_memory_verify_db.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        ("verify_schema_version",),
    ).fetchone()
    assert marker_row is None


def test_sqlite_error_on_insert_wraps_and_delete_rolled_back(
    in_memory_verify_db: sqlite3.Connection,
) -> None:
    """``sqlite3.OperationalError`` on the INSERT rolls back the DELETE. Locks Requirement 5.3.

    The DELETE lands on the underlying connection first; the fault
    fires on ``INSERT INTO schema_meta``; the migration's except
    handler issues ROLLBACK; the DELETE is unwound and the three
    seeded rows survive.
    """
    _seed_row(in_memory_verify_db, "sha-a", "ruff", "0.7.4", "[]")
    _seed_row(in_memory_verify_db, "sha-b", "ruff", "0.9.10", "[]")
    _seed_row(in_memory_verify_db, "sha-c", "mypy", "1.11.2", "[]")

    wrapper = FaultInjectingConnection(
        in_memory_verify_db,
        fault_prefix="INSERT INTO schema_meta",
        error_msg="mocked-insert-failure",
    )

    with pytest.raises(VerificationRunnerError) as excinfo:
        maybe_migrate_verify_state(cast(sqlite3.Connection, wrapper))

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)

    count_row = in_memory_verify_db.execute(
        "SELECT COUNT(*) FROM static_baseline"
    ).fetchone()
    assert count_row is not None
    assert count_row[0] == 3


def test_migration_is_idempotent_on_second_call(
    in_memory_verify_db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second call on the same connection is a fast-path no-op. Locks Requirement 3.2."""
    _seed_row(in_memory_verify_db, "sha-a", "ruff", "0.7.4", "[]")
    _seed_row(in_memory_verify_db, "sha-b", "ruff", "0.9.10", "[]")
    _seed_row(in_memory_verify_db, "sha-c", "mypy", "1.11.2", "[]")

    with caplog.at_level(logging.WARNING, logger="trikon.verify.state_migrations"):
        maybe_migrate_verify_state(in_memory_verify_db)
        first_warnings = [
            record
            for record in caplog.records
            if record.name == "trikon.verify.state_migrations"
            and record.levelno == logging.WARNING
        ]
        assert len(first_warnings) == 1

        caplog.clear()
        maybe_migrate_verify_state(in_memory_verify_db)
        second_warnings = [
            record
            for record in caplog.records
            if record.name == "trikon.verify.state_migrations"
            and record.levelno == logging.WARNING
        ]
        assert second_warnings == []

    marker_row = in_memory_verify_db.execute(
        "SELECT value FROM schema_meta WHERE key = ?",
        ("verify_schema_version",),
    ).fetchone()
    assert marker_row == ("1",)

    count_row = in_memory_verify_db.execute(
        "SELECT COUNT(*) FROM static_baseline"
    ).fetchone()
    assert count_row is not None
    assert count_row[0] == 0


# ===========================================================================
# Group C — ``_open_state_db`` wiring tests
# ===========================================================================


def test_open_state_db_calls_migration_after_ensure_verify_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``_open_state_db`` invokes ``maybe_migrate_verify_state`` after ``ensure_verify_tables``.

    Locks Requirement 1.1 — the migration is wired into the sole
    state-db opener the Verification Runner uses, and it runs strictly
    after ``ensure_verify_tables`` so ``static_baseline`` is guaranteed
    present when the DELETE fires.
    """
    call_order: list[str] = []
    seen_conns: list[sqlite3.Connection] = []

    def fake_ensure(conn: sqlite3.Connection) -> None:
        call_order.append("ensure_verify_tables")
        seen_conns.append(conn)

    def fake_migrate(conn: sqlite3.Connection) -> None:
        call_order.append("maybe_migrate_verify_state")
        seen_conns.append(conn)

    monkeypatch.setattr(_runner, "ensure_verify_tables", fake_ensure)
    monkeypatch.setattr(_runner, "maybe_migrate_verify_state", fake_migrate)

    conn = _runner._open_state_db(tmp_path / "state.db")
    try:
        assert call_order == ["ensure_verify_tables", "maybe_migrate_verify_state"]
        assert len(seen_conns) == 2
        assert seen_conns[0] is seen_conns[1]
        assert seen_conns[0] is conn
    finally:
        conn.close()


def test_open_state_db_closes_connection_when_migration_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``_open_state_db`` closes the connection before re-raising migration failures.

    Locks Requirement 5.6 — a raise from ``maybe_migrate_verify_state``
    routes through the ``except VerificationRunnerError: conn.close();
    raise`` block, so the partially-configured handle is released
    before the exception surfaces to ``run_verification``.
    """
    real_connect = sqlite3.connect
    spy_holder: list[SpyConnection] = []

    def fake_connect(
        database: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        underlying = real_connect(database)
        spy = SpyConnection(underlying)
        spy_holder.append(spy)
        return cast(sqlite3.Connection, spy)

    def raising_migrate(conn: sqlite3.Connection) -> None:
        del conn
        raise VerificationRunnerError("mocked-migration-failure")

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    monkeypatch.setattr(_runner, "maybe_migrate_verify_state", raising_migrate)

    with pytest.raises(VerificationRunnerError) as excinfo:
        _runner._open_state_db(tmp_path / "state.db")

    assert "mocked-migration-failure" in str(excinfo.value)
    assert len(spy_holder) == 1
    assert spy_holder[0].close_call_count == 1


# ===========================================================================
# Group D — public-surface test
# ===========================================================================


def test_state_migrations_public_surface_is_exactly_two_symbols() -> None:
    """Locks the ``__all__`` set, the constant value, and the function shape.

    Validates Requirements 6.9 and 6.10 — the module exposes exactly
    two names on its public surface, the version constant is ``1`` in
    v0.3.6, and :func:`maybe_migrate_verify_state` has the concrete
    ``(conn: sqlite3.Connection) -> None`` signature (no
    ``dict[str, object]`` widening, no keyword-only parameter drift).
    """
    assert set(_state_migrations.__all__) == {
        "CURRENT_VERIFY_SCHEMA_VERSION",
        "maybe_migrate_verify_state",
    }

    assert _state_migrations.CURRENT_VERIFY_SCHEMA_VERSION == 1

    # ``state_migrations.py`` uses ``from __future__ import annotations``, so
    # annotations are stored as strings; ``eval_str=True`` resolves them back
    # to the real classes for identity comparison.
    signature = inspect.signature(
        _state_migrations.maybe_migrate_verify_state, eval_str=True
    )
    parameters = list(signature.parameters.values())
    assert len(parameters) == 1
    (only_param,) = parameters
    assert only_param.annotation is sqlite3.Connection
    assert signature.return_annotation is None
