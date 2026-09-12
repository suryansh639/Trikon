"""SQLite-backed symbol dependency graph.

The graph is directed:

* Nodes are :class:`~trikon.change_intel.models.SymbolDef` values — functions,
  classes, methods, and module-level assignments.
* Edges express *uses*: ``source uses target`` means the source symbol
  references the target somewhere inside its own definition.

Storage lives at ``<repo>/.trikon/state.db``. The graph is never held in memory
in full — traversals push work back into SQLite. The schema, cache-invalidation
strategy, and rollback plan are frozen in ``design.md §4``. Cache identity is
``(qualified_name, file_sha)`` on ``symbols`` and ``(path)`` on ``file_index``;
a file mutation changes ``file_sha`` and therefore evicts every symbol row
belonging to the old snapshot via a targeted ``DELETE`` inside a single
transaction. Foreign keys with ``ON DELETE CASCADE`` propagate that eviction
into the ``edges`` table for free.

Every failure surfaces as :class:`~trikon.change_intel.errors.DepGraphError`
so that the SDK boundary can convert dependency-graph outages into a
``require_human`` verdict without needing to know :mod:`sqlite3`'s exception
vocabulary.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from trikon.change_intel.errors import DepGraphError
from trikon.change_intel.models import SymbolDef, SymbolKind

if TYPE_CHECKING:
    from types import TracebackType


CURRENT_SCHEMA_VERSION: int = 1
"""Schema version emitted by this Trikon build. Bump only when the DDL below
changes shape in a way that older Trikons cannot read."""


_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_index (
        path        TEXT PRIMARY KEY,
        file_sha    TEXT NOT NULL,
        indexed_at  TEXT NOT NULL,
        byte_size   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS symbols (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        qualified_name     TEXT NOT NULL,
        kind               TEXT NOT NULL CHECK (
                               kind IN ('function','class','method','assignment')),
        file_path          TEXT NOT NULL,
        file_sha           TEXT NOT NULL,
        start_line         INTEGER NOT NULL,
        end_line           INTEGER NOT NULL,
        start_byte         INTEGER NOT NULL,
        end_byte           INTEGER NOT NULL,
        is_public          INTEGER NOT NULL CHECK (is_public IN (0, 1)),
        UNIQUE (qualified_name, file_sha),
        FOREIGN KEY (file_path) REFERENCES file_index(path) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_symbols_qname     ON symbols(qualified_name)",
    """
    CREATE TABLE IF NOT EXISTS edges (
        source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        kind      TEXT NOT NULL DEFAULT 'uses',
        PRIMARY KEY (source_id, target_id, kind),
        FOREIGN KEY (source_id) REFERENCES symbols(id) ON DELETE CASCADE,
        FOREIGN KEY (target_id) REFERENCES symbols(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)",
)


class DepGraph:
    """SQLite-backed directed graph of symbol → symbol *uses* edges.

    The connection is opened lazily on the first call that needs it. That
    matters because :class:`DepGraph` is instantiated on every ``sdk.verify``
    invocation, but some code paths (for example an early
    :func:`~trikon.change_intel.diff_parser.parse_diff` failure) never touch
    the graph and should not pay the SQLite open cost.

    The graph is single-threaded by design (see ``design.md §1.3`` non-goals);
    the connection is opened with SQLite's default ``check_same_thread=True``
    so accidental cross-thread use raises immediately instead of corrupting
    state.
    """

    __slots__ = ("_conn", "_db_path")

    def __init__(self, db_path: Path) -> None:
        """Record the on-disk path but do NOT open the connection yet.

        The connection is opened by :meth:`_get_conn` on the first mutating
        or querying call.
        """
        self._db_path: Path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> DepGraph:
        """Return ``self`` so ``with DepGraph(path) as g:`` binds correctly."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the SQLite connection on exit, whether or not the body raised."""
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection if it is open.

        Idempotent: safe to call more than once, and safe to call before any
        connection was ever opened.
        """
        if self._conn is not None:
            # Best-effort close. Nothing sensible to do if the OS refuses to
            # release the handle; the graph object is being discarded.
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public API: mutations
    # ------------------------------------------------------------------

    def upsert_file(self, file_path: Path, file_sha: str) -> None:
        """Record that we saw ``file_path`` at ``file_sha``.

        This inserts (or replaces) the row in ``file_index``. It is safe to
        call independently of :meth:`upsert_symbols`; :meth:`upsert_symbols`
        performs the same insert internally so callers that already know
        their symbol list do not need to double-write.

        ``byte_size`` is populated from :meth:`Path.stat` when the file
        exists on disk, and falls back to ``0`` otherwise. The column exists
        for observability only — no query in this module depends on it.
        """
        conn = self._get_conn()
        path_str = _to_posix(file_path)
        byte_size = _safe_byte_size(file_path)
        indexed_at = _now_iso()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO file_index (path, file_sha, indexed_at, byte_size) "
                "VALUES (?, ?, ?, ?)",
                (path_str, file_sha, indexed_at, byte_size),
            )
        except sqlite3.Error as exc:
            raise DepGraphError(f"upsert_file failed for {path_str!r}") from exc

    def upsert_symbols(
        self,
        file_path: Path,
        file_sha: str,
        symbols: list[SymbolDef],
    ) -> None:
        """Atomically replace the symbol set for a ``(file_path, file_sha)`` pair.

        The three-step transaction from ``design.md §4.1`` runs as one
        ``BEGIN IMMEDIATE ... COMMIT`` unit so no reader ever observes a
        partial update:

        1. ``INSERT OR REPLACE INTO file_index`` for the new SHA.
        2. ``DELETE FROM symbols WHERE file_path = ? AND file_sha != ?`` — the
           foreign-key cascade removes the old edges for us.
        3. Bulk ``INSERT OR IGNORE INTO symbols`` for the new snapshot.

        ``INSERT OR IGNORE`` is intentional: if two symbols share a
        ``(qualified_name, file_sha)`` (e.g. reflective duplicates from a
        code-generator), we keep the first and drop the rest deterministically.
        """
        conn = self._get_conn()
        path_str = _to_posix(file_path)
        byte_size = _safe_byte_size(file_path)
        if byte_size == 0 and symbols:
            # Fall back to the largest end_byte we observed. That is not the
            # true file size but it is a sane monotonic proxy for tests and
            # for the in-memory smoke path.
            byte_size = max(sym.end_byte for sym in symbols)
        indexed_at = _now_iso()

        rows: list[tuple[str, str, str, str, int, int, int, int, int]] = [
            (
                sym.qualified_name,
                sym.kind,
                sym.file_path,
                sym.file_sha,
                sym.start_line,
                sym.end_line,
                sym.start_byte,
                sym.end_byte,
                1 if sym.is_public else 0,
            )
            for sym in symbols
        ]

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise DepGraphError(
                f"upsert_symbols: could not begin transaction for {path_str!r}"
            ) from exc

        try:
            conn.execute(
                "INSERT OR REPLACE INTO file_index (path, file_sha, indexed_at, byte_size) "
                "VALUES (?, ?, ?, ?)",
                (path_str, file_sha, indexed_at, byte_size),
            )
            conn.execute(
                "DELETE FROM symbols WHERE file_path = ? AND file_sha != ?",
                (path_str, file_sha),
            )
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO symbols "
                    "(qualified_name, kind, file_path, file_sha, "
                    " start_line, end_line, start_byte, end_byte, is_public) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _safe_rollback(conn)
            raise DepGraphError(f"upsert_symbols failed for {path_str!r}") from exc

    def upsert_edges(
        self,
        source: SymbolDef,
        targets: list[SymbolDef],
        *,
        kind: str = "uses",
    ) -> None:
        """Record that ``source`` uses each symbol in ``targets``.

        Both ``source`` and every target must already exist in the ``symbols``
        table — call :meth:`upsert_symbols` for their file first. Missing
        endpoints raise :class:`DepGraphError` rather than silently dropping
        the edge, because a silent drop would break the "seed graph then
        traverse" contract that :meth:`transitive_dependents` relies on.

        ``INSERT OR IGNORE`` makes repeated calls a no-op — the composite
        primary key ``(source_id, target_id, kind)`` de-duplicates on the DB
        side.
        """
        conn = self._get_conn()

        try:
            source_id = self._lookup_symbol_id(conn, source)
        except sqlite3.Error as exc:
            raise DepGraphError("upsert_edges: source lookup failed") from exc
        if source_id is None:
            raise DepGraphError(
                f"upsert_edges: source symbol {source.qualified_name!r} "
                f"is not indexed at file_sha={source.file_sha!r}"
            )

        rows: list[tuple[int, int, str]] = []
        for target in targets:
            try:
                target_id = self._lookup_symbol_id(conn, target)
            except sqlite3.Error as exc:
                raise DepGraphError("upsert_edges: target lookup failed") from exc
            if target_id is None:
                raise DepGraphError(
                    f"upsert_edges: target symbol {target.qualified_name!r} "
                    f"is not indexed at file_sha={target.file_sha!r}"
                )
            rows.append((source_id, target_id, kind))

        if not rows:
            return

        try:
            conn.executemany(
                "INSERT OR IGNORE INTO edges (source_id, target_id, kind) VALUES (?, ?, ?)",
                rows,
            )
        except sqlite3.Error as exc:
            raise DepGraphError("upsert_edges: insert failed") from exc

    def prune_files(self, keep_paths: set[Path]) -> int:
        """Delete every ``file_index`` row whose path is not in ``keep_paths``.

        Returns the number of rows deleted. The foreign-key cascade removes
        the associated ``symbols`` and ``edges`` rows automatically. Called
        on cold-index passes only — the hot path never prunes, because the
        overhead of ``NOT IN`` on a large ``keep_paths`` set is not worth it
        for a small number of file deletions.
        """
        conn = self._get_conn()
        keep_strs = {_to_posix(p) for p in keep_paths}
        try:
            if keep_strs:
                placeholders = ",".join("?" * len(keep_strs))
                cursor = conn.execute(
                    f"DELETE FROM file_index WHERE path NOT IN ({placeholders})",
                    tuple(keep_strs),
                )
            else:
                cursor = conn.execute("DELETE FROM file_index")
            return int(cursor.rowcount)
        except sqlite3.Error as exc:
            raise DepGraphError("prune_files failed") from exc

    # ------------------------------------------------------------------
    # Public API: queries
    # ------------------------------------------------------------------

    def get_symbols(self, file_path: Path, file_sha: str) -> list[SymbolDef]:
        """Return every ``SymbolDef`` stored for ``(file_path, file_sha)``.

        Rows are returned in source order (``start_byte`` ascending, ties
        broken by ``qualified_name`` for full determinism). An empty list is
        returned when the ``(path, sha)`` pair has never been indexed OR when
        the cache holds a different snapshot for the same path; the caller
        is expected to have consulted :meth:`file_needs_reindex` first, so
        an empty return here typically means "the file legitimately has
        zero indexable symbols" (an empty module, for example).

        Uses the ``UNIQUE (qualified_name, file_sha)`` constraint plus the
        ``idx_symbols_file_path`` index for a covered lookup — the query
        touches at most one file's rows regardless of graph size.
        """
        conn = self._get_conn()
        path_str = _to_posix(file_path)
        try:
            cursor = conn.execute(
                "SELECT qualified_name, kind, file_path, file_sha, "
                "       start_line, end_line, start_byte, end_byte, is_public "
                "FROM symbols "
                "WHERE file_path = ? AND file_sha = ? "
                "ORDER BY start_byte ASC, qualified_name ASC",
                (path_str, file_sha),
            )
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            raise DepGraphError(f"get_symbols failed for {path_str!r}") from exc

        return [
            SymbolDef(
                qualified_name=str(row[0]),
                kind=_narrow_symbol_kind(str(row[1])),
                file_path=str(row[2]),
                file_sha=str(row[3]),
                start_line=int(row[4]),
                end_line=int(row[5]),
                start_byte=int(row[6]),
                end_byte=int(row[7]),
                is_public=bool(row[8]),
            )
            for row in rows
        ]

    def file_needs_reindex(self, file_path: Path, current_sha: str) -> bool:
        """Return ``True`` when the on-disk SHA disagrees with the cache.

        ``True`` is returned when the file is unknown (never indexed) OR when
        the stored SHA differs from ``current_sha``. ``False`` means the
        cache is fresh and the caller may read symbols directly.
        """
        conn = self._get_conn()
        path_str = _to_posix(file_path)
        try:
            cursor = conn.execute(
                "SELECT file_sha FROM file_index WHERE path = ?",
                (path_str,),
            )
            row = cursor.fetchone()
        except sqlite3.Error as exc:
            raise DepGraphError(f"file_needs_reindex failed for {path_str!r}") from exc
        if row is None:
            return True
        stored_sha = row[0]
        return bool(stored_sha != current_sha)

    def transitive_dependents(
        self,
        seeds: list[SymbolDef],
        *,
        max_hops: int = 5,
    ) -> list[SymbolDef]:
        """Return every symbol reachable from ``seeds`` via *reverse* edges.

        "Reverse edge" traversal: an edge ``(source, target, kind)`` says the
        source *uses* the target, so a symbol that depends on ``target`` is a
        ``source`` for that ``target``. This method walks that direction —
        edges pointing INTO the visited set expand outward to their sources.

        Ordering: BFS-order (hop 1 first, then hop 2, ...); ties inside a
        single hop are broken by ``qualified_name`` ascending. The seed
        symbols themselves are never included in the return value — the
        caller already knows about them.

        ``max_hops`` bounds the BFS depth. ``max_hops <= 0`` raises
        :class:`DepGraphError` because a zero-hop dependents query has no
        useful meaning and almost certainly indicates caller bug.
        """
        if max_hops <= 0:
            raise DepGraphError(f"transitive_dependents: max_hops must be positive, got {max_hops}")

        conn = self._get_conn()

        try:
            seed_ids: set[int] = set()
            for seed in seeds:
                sid = self._lookup_symbol_id(conn, seed)
                if sid is not None:
                    seed_ids.add(sid)
            if not seed_ids:
                return []

            visited: set[int] = set(seed_ids)
            frontier: set[int] = set(seed_ids)
            ordered_ids: list[int] = []

            for _hop in range(max_hops):
                if not frontier:
                    break
                frontier_placeholders = ",".join("?" * len(frontier))
                candidates_cursor = conn.execute(
                    f"SELECT DISTINCT source_id FROM edges "
                    f"WHERE target_id IN ({frontier_placeholders})",
                    tuple(frontier),
                )
                candidate_ids = {int(row[0]) for row in candidates_cursor.fetchall()}
                next_frontier = candidate_ids - visited
                if not next_frontier:
                    break

                # Sort this BFS layer by qualified_name for determinism.
                next_placeholders = ",".join("?" * len(next_frontier))
                sorted_cursor = conn.execute(
                    f"SELECT id FROM symbols "
                    f"WHERE id IN ({next_placeholders}) "
                    f"ORDER BY qualified_name ASC, id ASC",
                    tuple(next_frontier),
                )
                layer_ordered = [int(row[0]) for row in sorted_cursor.fetchall()]
                ordered_ids.extend(layer_ordered)

                visited.update(next_frontier)
                frontier = next_frontier

            if not ordered_ids:
                return []

            hydrated = self._hydrate_symbols(conn, ordered_ids)
            return hydrated
        except sqlite3.Error as exc:
            raise DepGraphError("transitive_dependents failed") from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the live :class:`sqlite3.Connection`, opening it if needed.

        Applies the four pragmas required by ``design.md §4`` on every fresh
        connection (SQLite pragmas are per-connection, not per-database).
        Runs schema init / version guardrails on the first open.
        """
        if self._conn is not None:
            return self._conn

        try:
            conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,  # manual transaction control
            )
        except sqlite3.Error as exc:
            raise DepGraphError(f"Failed to open SQLite database at {self._db_path}") from exc

        try:
            # Pragmas are per-connection. They must run every open.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA temp_store = MEMORY")
        except sqlite3.Error as exc:
            conn.close()
            raise DepGraphError("Failed to set SQLite pragmas") from exc

        self._conn = conn
        try:
            self._init_schema(conn)
        except DepGraphError:
            # Schema init failed. Drop the connection so the next call retries
            # cleanly (or raises again against a broken DB).
            self.close()
            raise
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables or validate the on-disk schema version.

        First open of a fresh database creates the DDL from
        :data:`_DDL_STATEMENTS` and seeds ``schema_meta``. Subsequent opens
        check that the stored ``schema_version`` is compatible:

        * equal → nothing to do;
        * lower → run migrations (Phase 1 ships none, so this raises);
        * higher → refuse to open (the DB was written by a newer Trikon).
        """
        try:
            meta_cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            )
            has_meta = meta_cursor.fetchone() is not None

            if has_meta:
                version_cursor = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                )
                version_row = version_cursor.fetchone()
                if version_row is not None:
                    raw_version = version_row[0]
                    try:
                        stored_version = int(raw_version)
                    except (TypeError, ValueError) as exc:
                        raise DepGraphError(
                            f"corrupt schema_meta: schema_version={raw_version!r}"
                        ) from exc

                    if stored_version > CURRENT_SCHEMA_VERSION:
                        raise DepGraphError(
                            f"state.db was written by a newer Trikon "
                            f"(schema_version={stored_version}, "
                            f"this build supports up to {CURRENT_SCHEMA_VERSION}); "
                            f"upgrade Trikon or delete .trikon/state.db"
                        )
                    if stored_version < CURRENT_SCHEMA_VERSION:
                        self._run_migrations(stored_version)

                    # Compatible version. Make sure every table is present —
                    # a partial init could have left something out.
                    self._create_tables(conn)
                    return

            # Fresh DB (or one without schema_meta seeded). Create everything
            # and stamp the metadata inside a single transaction so callers
            # never see a half-initialized schema.
            self._create_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(CURRENT_SCHEMA_VERSION)),
                        ("created_at", _now_iso()),
                        ("trikon_version", _get_trikon_version()),
                    ],
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                _safe_rollback(conn)
                raise
        except sqlite3.Error as exc:
            raise DepGraphError("Failed to initialize dep-graph schema") from exc

    @staticmethod
    def _create_tables(conn: sqlite3.Connection) -> None:
        """Execute every DDL statement in :data:`_DDL_STATEMENTS` once.

        All statements are ``IF NOT EXISTS`` so this is idempotent.
        """
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)

    def _run_migrations(self, from_version: int) -> None:
        """Apply migrations from ``from_version`` up to :data:`CURRENT_SCHEMA_VERSION`.

        Phase 1 ships with zero migrations, so any call here means the DB was
        written by an older Trikon that supported a lower schema version —
        which does not exist. We surface the mismatch as an error so callers
        can auto-rebuild (see ``design.md §12.2``).
        """
        raise DepGraphError(
            f"No migration path from schema_version={from_version} to {CURRENT_SCHEMA_VERSION}"
        )

    @staticmethod
    def _lookup_symbol_id(conn: sqlite3.Connection, sym: SymbolDef) -> int | None:
        """Return the ``symbols.id`` for ``sym`` or ``None`` if it is not stored.

        Lookup key matches the ``UNIQUE (qualified_name, file_sha)`` constraint
        so at most one row can ever match.
        """
        cursor = conn.execute(
            "SELECT id FROM symbols WHERE qualified_name = ? AND file_sha = ?",
            (sym.qualified_name, sym.file_sha),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return int(row[0])

    @staticmethod
    def _hydrate_symbols(conn: sqlite3.Connection, ordered_ids: list[int]) -> list[SymbolDef]:
        """Fetch ``SymbolDef`` rows for ``ordered_ids`` preserving BFS order."""
        placeholders = ",".join("?" * len(ordered_ids))
        cursor = conn.execute(
            f"SELECT id, qualified_name, kind, file_path, file_sha, "
            f"       start_line, end_line, start_byte, end_byte, is_public "
            f"FROM symbols WHERE id IN ({placeholders})",
            tuple(ordered_ids),
        )
        by_id: dict[int, SymbolDef] = {
            int(row[0]): SymbolDef(
                qualified_name=str(row[1]),
                kind=_narrow_symbol_kind(str(row[2])),
                file_path=str(row[3]),
                file_sha=str(row[4]),
                start_line=int(row[5]),
                end_line=int(row[6]),
                start_byte=int(row[7]),
                end_byte=int(row[8]),
                is_public=bool(row[9]),
            )
            for row in cursor.fetchall()
        }
        return [by_id[sid] for sid in ordered_ids if sid in by_id]


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _to_posix(path: Path) -> str:
    """Normalize a :class:`Path` to a POSIX-string key.

    All DB keys are POSIX so a repo indexed on Linux and re-opened on Windows
    (or vice versa) still hits the cache.
    """
    return path.as_posix()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _safe_byte_size(path: Path) -> int:
    """Return the file's byte size, or ``0`` if it cannot be stat-ed.

    ``file_index.byte_size`` is metadata-only, so falling back to ``0`` when
    the path does not exist on disk (in-memory tests, deleted files) is safe.
    """
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """Best-effort ``ROLLBACK`` on a connection whose transaction has failed.

    If rollback itself fails the connection is already broken; the caller is
    about to raise a :class:`DepGraphError` anyway, so there is nothing more
    to do.
    """
    with contextlib.suppress(sqlite3.Error):
        conn.execute("ROLLBACK")


def _narrow_symbol_kind(kind: str) -> SymbolKind:
    """Narrow a raw string from SQLite into the ``SymbolKind`` ``Literal``.

    The ``CHECK`` constraint on ``symbols.kind`` guarantees only the four
    valid strings can land in the column, so in practice this always
    returns via one of the four branches. If the database is corrupt or has
    been tampered with, we surface :class:`DepGraphError` instead of letting
    an unexpected string propagate as a ``SymbolKind``.
    """
    if kind == "function":
        return "function"
    if kind == "class":
        return "class"
    if kind == "method":
        return "method"
    if kind == "assignment":
        return "assignment"
    raise DepGraphError(f"corrupt symbols row: unexpected kind={kind!r}")


def _get_trikon_version() -> str:
    """Return ``trikon.__version__`` if importable, else ``'unknown'``.

    Imported lazily to avoid a circular import between :mod:`trikon` and
    :mod:`trikon.change_intel.dep_graph`.
    """
    try:
        from trikon import __version__ as trikon_version
    except ImportError:
        return "unknown"
    return str(trikon_version)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DepGraph",
]
