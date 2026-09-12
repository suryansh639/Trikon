"""Unit tests for :mod:`trikon.change_intel.dep_graph`.

Covers Task 3.2 in ``.kiro/specs/change-intelligence/tasks.md``. Every test
uses either ``:memory:`` SQLite (hermetic, no filesystem) or a ``tmp_path``
file-backed DB when the test needs to pre-seed on-disk state (migration
guardrail, cross-connection persistence).

Properties validated:

* **Property 7 / Requirements 4.1** — dep-graph round-trip on a
  ``(file_path, file_sha)`` snapshot.
* **Property 8 / Requirements 4.2** — model-based comparison of
  :meth:`DepGraph.transitive_dependents` against a reference
  implementation using :mod:`networkx`.
* **Property 9 / Requirements 4.3** — file-SHA cache invariant.
* **Requirements 6.1** — every failure surfaces as
  :class:`DepGraphError`, never a raw :class:`sqlite3.Error`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import networkx as nx  # type: ignore[import-untyped]
import pytest
from hypothesis import given, settings

from tests.unit.change_intel.strategies import dep_dag
from trikon.change_intel.dep_graph import CURRENT_SCHEMA_VERSION, DepGraph
from trikon.change_intel.errors import DepGraphError
from trikon.change_intel.models import SymbolDef

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


# ---------------------------------------------------------------------------
# Constants / fixtures
# ---------------------------------------------------------------------------

_MEMORY: Path = Path(":memory:")
"""Sentinel path used for hermetic, in-process SQLite databases."""

_FILE: Path = Path("src/pkg/mod.py")
_SHA1: str = "1" * 64
_SHA2: str = "2" * 64


def _symbol(
    qname: str,
    *,
    file_path: str = "src/pkg/mod.py",
    file_sha: str = _SHA1,
    kind: str = "function",
    start_line: int = 1,
    end_line: int = 1,
    is_public: bool = True,
) -> SymbolDef:
    """Compact ``SymbolDef`` factory keyed on ``qname``.

    Every field but ``qualified_name`` has a sensible default because the
    dep-graph tests only ever exercise identity via
    ``(qualified_name, file_sha)``. Callers override just what the specific
    test cares about.
    """
    return SymbolDef(
        qualified_name=qname,
        kind=kind,  # type: ignore[arg-type]
        file_path=file_path,
        file_sha=file_sha,
        start_line=start_line,
        end_line=end_line,
        start_byte=0,
        end_byte=1,
        is_public=is_public,
    )


@pytest.fixture
def graph() -> Iterator[DepGraph]:
    """Yield a fresh in-memory :class:`DepGraph` and close it after the test."""
    dg = DepGraph(_MEMORY)
    try:
        yield dg
    finally:
        dg.close()


# ---------------------------------------------------------------------------
# Internal helpers — small SQL utilities kept in the test module.
# ---------------------------------------------------------------------------


def _stored_symbols(dg: DepGraph, file_path: Path, file_sha: str) -> set[SymbolDef]:
    """Return every ``SymbolDef`` stored for ``(file_path, file_sha)``.

    We deliberately reach through the private ``_get_conn`` accessor rather
    than adding a public query method to :class:`DepGraph`. The rest of
    Change Intelligence never reads the ``symbols`` table by ``file_path``;
    only these tests need it, so the API stays lean.
    """
    conn = dg._get_conn()  # test-only introspection.
    cursor = conn.execute(
        "SELECT qualified_name, kind, file_path, file_sha, start_line, end_line, "
        "       start_byte, end_byte, is_public "
        "FROM symbols WHERE file_path = ? AND file_sha = ?",
        (file_path.as_posix(), file_sha),
    )
    return {
        SymbolDef(
            qualified_name=str(row[0]),
            kind=row[1],
            file_path=str(row[2]),
            file_sha=str(row[3]),
            start_line=int(row[4]),
            end_line=int(row[5]),
            start_byte=int(row[6]),
            end_byte=int(row[7]),
            is_public=bool(row[8]),
        )
        for row in cursor.fetchall()
    }


def _count(dg: DepGraph, table: str) -> int:
    """Return ``COUNT(*)`` for ``table`` on the graph's connection."""
    conn = dg._get_conn()  # test-only introspection.
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def _networkx_dependents(
    n_nodes: int,
    edges: Iterable[tuple[int, int]],
    seed_indices: Iterable[int],
    max_hops: int,
) -> set[int]:
    """Reference reverse-BFS implementation using :mod:`networkx`.

    Given a forward-edge DAG ``edges`` (``source uses target``), the reverse
    graph has an edge from ``target`` to ``source``. A dependent of ``s`` is
    any node reachable from ``s`` in the reverse graph — exactly what
    :meth:`DepGraph.transitive_dependents` computes. The result excludes the
    seed indices themselves, matching the DepGraph contract.
    """
    g: nx.DiGraph = nx.DiGraph()
    g.add_nodes_from(range(n_nodes))
    g.add_edges_from(edges)
    reversed_g = g.reverse(copy=True)

    result: set[int] = set()
    seeds = set(seed_indices)
    for seed_idx in seeds:
        for hop in range(1, max_hops + 1):
            result.update(nx.descendants_at_distance(reversed_g, seed_idx, hop))
    return result - seeds


def _index_of(sym: SymbolDef) -> int:
    """Extract the numeric suffix embedded in a strategy-generated qname."""
    # Strategy uses "pkg.mod.n{i}" as the qualified name; the tail is the index.
    tail = sym.qualified_name.rsplit(".", 1)[-1]
    assert tail.startswith("n"), f"unexpected qualified_name: {sym.qualified_name!r}"
    return int(tail[1:])


# ---------------------------------------------------------------------------
# 1. Round-trip — Property 7 / Requirements 4.1
# ---------------------------------------------------------------------------


def test_upsert_symbols_round_trip(graph: DepGraph) -> None:
    """``upsert_symbols(p, h, xs)`` stores exactly ``set(xs)`` for ``(p, h)``.

    Validates: Requirements 4.1 (Property 7).
    """
    xs = [
        _symbol("pkg.mod.alpha", kind="function", start_line=1, end_line=5),
        _symbol("pkg.mod.Beta", kind="class", start_line=10, end_line=30, is_public=True),
        _symbol("pkg.mod.Beta.method", kind="method", start_line=12, end_line=20),
        _symbol("pkg.mod._private", kind="function", start_line=32, end_line=35, is_public=False),
    ]

    graph.upsert_symbols(_FILE, _SHA1, xs)

    stored = _stored_symbols(graph, _FILE, _SHA1)
    assert stored == set(xs)


def test_upsert_symbols_empty_list_is_idempotent(graph: DepGraph) -> None:
    """Passing an empty symbol list still records the file, and adds no rows.

    The ``file_index`` write must happen so a subsequent ``file_needs_reindex``
    knows the file was seen. This matches the Task 5.2 use case where a file
    parses but has no top-level symbols (e.g. a bare ``__init__.py``).
    """
    graph.upsert_symbols(_FILE, _SHA1, [])

    assert graph.file_needs_reindex(_FILE, _SHA1) is False
    assert _stored_symbols(graph, _FILE, _SHA1) == set()


# ---------------------------------------------------------------------------
# 2. transitive_dependents — Property 8 / Requirements 4.2
# ---------------------------------------------------------------------------


@given(dag=dep_dag())
@settings(max_examples=50, deadline=2000)
def test_transitive_dependents_matches_networkx(
    dag: tuple[list[SymbolDef], list[tuple[int, int]]],
) -> None:
    """``DepGraph.transitive_dependents`` equals a networkx reverse-BFS.

    For every generated DAG, insert the nodes and edges into a fresh
    in-memory :class:`DepGraph`, run ``transitive_dependents`` from a fixed
    seed, and compare against the reference implementation. The seed choice
    (last node) maximises non-trivial upstream sets so the property is
    exercised, not vacuous.

    Validates: Requirements 4.2 (Property 8).
    """
    nodes, edges = dag
    n_nodes = len(nodes)
    # The DAG guarantees `nodes[-1]` has the largest index; every edge that
    # touches it comes from a smaller index. That is the richest seed to test
    # reverse-BFS against.
    seed_idx = n_nodes - 1
    max_hops = 5

    dg = DepGraph(_MEMORY)
    try:
        dg.upsert_symbols(_FILE, _SHA1, nodes)
        # Insert every edge as a "uses" relationship. Duplicates are
        # de-duplicated at the strategy layer, so no INSERT OR IGNORE churn.
        for src_idx, tgt_idx in edges:
            dg.upsert_edges(nodes[src_idx], [nodes[tgt_idx]])

        dependents = dg.transitive_dependents([nodes[seed_idx]], max_hops=max_hops)
    finally:
        dg.close()

    got_indices = {_index_of(sym) for sym in dependents}
    expected_indices = _networkx_dependents(n_nodes, edges, [seed_idx], max_hops)
    assert got_indices == expected_indices


def test_transitive_dependents_excludes_seeds(graph: DepGraph) -> None:
    """Seeds never appear in the return value even if edges loop back through them."""
    a = _symbol("pkg.mod.a")
    b = _symbol("pkg.mod.b")
    c = _symbol("pkg.mod.c")
    graph.upsert_symbols(_FILE, _SHA1, [a, b, c])
    # a -> b, b -> c. Seeding on b: only "a" depends on b directly.
    graph.upsert_edges(a, [b])
    graph.upsert_edges(b, [c])

    result = graph.transitive_dependents([b], max_hops=5)

    qnames = {sym.qualified_name for sym in result}
    assert qnames == {"pkg.mod.a"}


def test_transitive_dependents_bfs_layer_ordering(graph: DepGraph) -> None:
    """Hop 1 symbols appear before hop 2, ties inside a hop by qname ascending.

    Contract in ``design.md §2.4``: BFS-order (hop 0 first) with ties broken
    by ``qualified_name``. The seed is hop 0 and is excluded from the result,
    so the first entries are hop-1 dependents.
    """
    target = _symbol("pkg.mod.target")
    hop1_z = _symbol("pkg.mod.zzz_hop1")
    hop1_a = _symbol("pkg.mod.aaa_hop1")
    hop2 = _symbol("pkg.mod.hop2")
    graph.upsert_symbols(_FILE, _SHA1, [target, hop1_z, hop1_a, hop2])
    graph.upsert_edges(hop1_z, [target])
    graph.upsert_edges(hop1_a, [target])
    graph.upsert_edges(hop2, [hop1_z])

    result = graph.transitive_dependents([target], max_hops=5)

    qnames = [sym.qualified_name for sym in result]
    # Hop 1 (alphabetical) then hop 2. Assert both by prefix and full order.
    assert qnames.index("pkg.mod.aaa_hop1") < qnames.index("pkg.mod.zzz_hop1")
    assert qnames.index("pkg.mod.zzz_hop1") < qnames.index("pkg.mod.hop2")


def test_transitive_dependents_respects_max_hops(graph: DepGraph) -> None:
    """``max_hops=1`` returns only direct dependents; deeper layers are dropped."""
    target = _symbol("pkg.mod.target")
    hop1 = _symbol("pkg.mod.hop1")
    hop2 = _symbol("pkg.mod.hop2")
    graph.upsert_symbols(_FILE, _SHA1, [target, hop1, hop2])
    graph.upsert_edges(hop1, [target])
    graph.upsert_edges(hop2, [hop1])

    depth1 = {sym.qualified_name for sym in graph.transitive_dependents([target], max_hops=1)}
    depth5 = {sym.qualified_name for sym in graph.transitive_dependents([target], max_hops=5)}
    assert depth1 == {"pkg.mod.hop1"}
    assert depth5 == {"pkg.mod.hop1", "pkg.mod.hop2"}


@pytest.mark.parametrize("bad_hops", [0, -1, -100])
def test_transitive_dependents_rejects_non_positive_max_hops(
    graph: DepGraph,
    bad_hops: int,
) -> None:
    """``max_hops <= 0`` raises :class:`DepGraphError`, not a silent empty list."""
    a = _symbol("pkg.mod.a")
    graph.upsert_symbols(_FILE, _SHA1, [a])
    with pytest.raises(DepGraphError, match="max_hops"):
        graph.transitive_dependents([a], max_hops=bad_hops)


def test_transitive_dependents_with_unknown_seed_returns_empty(graph: DepGraph) -> None:
    """A seed whose ``(qname, file_sha)`` is not stored contributes nothing.

    This is defensive behaviour: :meth:`transitive_dependents` filters seeds
    to those actually present in ``symbols`` before running BFS, so callers
    who pass a stale ``SymbolDef`` get a well-defined empty result rather
    than a crash.
    """
    stored = _symbol("pkg.mod.stored")
    stranger = _symbol("pkg.mod.stranger")
    graph.upsert_symbols(_FILE, _SHA1, [stored])

    assert graph.transitive_dependents([stranger], max_hops=5) == []


# ---------------------------------------------------------------------------
# 3. file_needs_reindex — Property 9 / Requirements 4.3
# ---------------------------------------------------------------------------


def test_file_needs_reindex_true_for_unknown_path(graph: DepGraph) -> None:
    """A path we have never indexed always needs re-indexing.

    Validates: Requirements 4.3 (Property 9, absent-path branch).
    """
    assert graph.file_needs_reindex(_FILE, _SHA1) is True


def test_file_needs_reindex_false_when_sha_matches(graph: DepGraph) -> None:
    """A path we indexed at the current SHA does not need re-indexing.

    Validates: Requirements 4.3 (Property 9, matching-sha branch).
    """
    graph.upsert_file(_FILE, _SHA1)
    assert graph.file_needs_reindex(_FILE, _SHA1) is False


def test_file_needs_reindex_true_on_sha_drift(graph: DepGraph) -> None:
    """A stored SHA that differs from the current SHA triggers re-indexing.

    Validates: Requirements 4.3 (Property 9, sha-mismatch branch).
    """
    graph.upsert_file(_FILE, _SHA1)
    assert graph.file_needs_reindex(_FILE, _SHA2) is True


# ---------------------------------------------------------------------------
# 4. Cascade delete on SHA change
# ---------------------------------------------------------------------------


def test_upsert_symbols_evicts_stale_snapshot_and_edges(graph: DepGraph) -> None:
    """Bumping ``file_sha`` deletes old symbol rows AND their edges via cascade.

    Design contract (``design.md §4.1``): row identity is
    ``(qualified_name, file_sha)``. When the file changes, the SHA changes,
    old symbol rows are deleted, and the ``ON DELETE CASCADE`` foreign key
    on ``edges.source_id`` / ``edges.target_id`` removes their edges too.
    """
    old_a = _symbol("pkg.mod.a", file_sha=_SHA1)
    old_b = _symbol("pkg.mod.b", file_sha=_SHA1)
    graph.upsert_symbols(_FILE, _SHA1, [old_a, old_b])
    graph.upsert_edges(old_a, [old_b])
    assert _count(graph, "symbols") == 2
    assert _count(graph, "edges") == 1

    new_a = _symbol("pkg.mod.a", file_sha=_SHA2)
    graph.upsert_symbols(_FILE, _SHA2, [new_a])

    # Old snapshot vanished; new one holds one row.
    assert _stored_symbols(graph, _FILE, _SHA1) == set()
    assert _stored_symbols(graph, _FILE, _SHA2) == {new_a}
    # Edges wired to the evicted rows are gone via FK cascade.
    assert _count(graph, "edges") == 0


# ---------------------------------------------------------------------------
# 5. Migration guardrail — newer schema versions refuse to open.
# ---------------------------------------------------------------------------


def _seed_schema_meta(db_path: Path, version_value: str) -> None:
    """Write a ``schema_meta`` row with the given ``schema_version`` value.

    Used to fabricate on-disk states the running Trikon should refuse:
    corrupt (non-numeric) values, or numeric values above / below the
    current build's supported range.
    """
    seed = sqlite3.connect(str(db_path))
    try:
        seed.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        seed.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (version_value,),
        )
        seed.commit()
    finally:
        seed.close()


def test_schema_version_newer_than_current_raises(tmp_path: Path) -> None:
    """A ``state.db`` written by a newer Trikon must not silently open.

    Design contract (``design.md §4.2``, §12.1): version ``>`` current raises
    ``DepGraphError`` at open time so the SDK boundary can auto-rebuild the
    cache rather than mis-interpreting unknown columns.
    """
    db_path = tmp_path / "state.db"
    _seed_schema_meta(db_path, "9999")

    dg = DepGraph(db_path)
    try:
        with pytest.raises(DepGraphError) as excinfo:
            # Any DB-touching call triggers _get_conn -> _init_schema.
            dg.upsert_file(_FILE, _SHA1)
    finally:
        dg.close()

    message = str(excinfo.value)
    # Message must mention the guardrail context, not just leak sqlite noise.
    assert "newer Trikon" in message or "schema_version" in message
    assert str(CURRENT_SCHEMA_VERSION) in message or "9999" in message


def test_schema_version_older_than_current_raises(tmp_path: Path) -> None:
    """An older-than-current schema version triggers the (empty) migration path.

    Phase 1 ships zero migrations, so :meth:`DepGraph._run_migrations` is a
    guard that surfaces the mismatch. Once real migrations land, this test
    will need to grow to also cover the happy path.
    """
    db_path = tmp_path / "state.db"
    _seed_schema_meta(db_path, "0")

    dg = DepGraph(db_path)
    try:
        with pytest.raises(DepGraphError, match="No migration path"):
            dg.upsert_file(_FILE, _SHA1)
    finally:
        dg.close()


def test_schema_version_corrupt_value_raises(tmp_path: Path) -> None:
    """A non-numeric ``schema_version`` value is surfaced as a clear error.

    Guards against a partially-written or hand-edited state file. Falling
    back to "assume current" would risk misinterpreting rows written by an
    incompatible build.
    """
    db_path = tmp_path / "state.db"
    _seed_schema_meta(db_path, "not-a-version")

    dg = DepGraph(db_path)
    try:
        with pytest.raises(DepGraphError, match="corrupt schema_meta"):
            dg.upsert_file(_FILE, _SHA1)
    finally:
        dg.close()


# ---------------------------------------------------------------------------
# 6. prune_files — cold-index cleanup with cascade.
# ---------------------------------------------------------------------------


def test_prune_files_keeps_only_specified_paths_and_cascades(graph: DepGraph) -> None:
    """``prune_files({keep})`` deletes every other file, plus its symbols and edges.

    Return value equals the number of ``file_index`` rows removed. Symbol and
    edge rows for the pruned paths disappear via the FK cascade — the caller
    never has to run a manual cleanup.
    """
    file1 = Path("src/pkg/a.py")
    file2 = Path("src/pkg/b.py")
    file3 = Path("src/pkg/c.py")
    sym1 = _symbol("pkg.a.func", file_path=file1.as_posix())
    sym2 = _symbol("pkg.b.func", file_path=file2.as_posix())
    sym3 = _symbol("pkg.c.func", file_path=file3.as_posix())

    graph.upsert_symbols(file1, _SHA1, [sym1])
    graph.upsert_symbols(file2, _SHA1, [sym2])
    graph.upsert_symbols(file3, _SHA1, [sym3])
    # Cross-file edge so the cascade has something interesting to remove.
    graph.upsert_edges(sym2, [sym1])
    assert _count(graph, "file_index") == 3
    assert _count(graph, "symbols") == 3
    assert _count(graph, "edges") == 1

    removed = graph.prune_files({file1})

    assert removed == 2
    assert _count(graph, "file_index") == 1
    # Only sym1 survives; sym2 / sym3 removed via cascade.
    assert _count(graph, "symbols") == 1
    # The edge sym2 -> sym1 dies because sym2 was cascaded out.
    assert _count(graph, "edges") == 0


def test_prune_files_empty_keep_set_wipes_everything(graph: DepGraph) -> None:
    """``prune_files(set())`` clears the graph entirely."""
    file1 = Path("src/pkg/a.py")
    graph.upsert_symbols(file1, _SHA1, [_symbol("pkg.a.func", file_path=file1.as_posix())])
    assert _count(graph, "file_index") == 1

    removed = graph.prune_files(set())

    assert removed == 1
    assert _count(graph, "file_index") == 0
    assert _count(graph, "symbols") == 0


# ---------------------------------------------------------------------------
# 7. Context manager — connection closes on exit, even after exceptions.
# ---------------------------------------------------------------------------


def test_context_manager_closes_connection_on_normal_exit() -> None:
    """``with DepGraph(...) as g:`` releases the SQLite connection at exit."""
    with DepGraph(_MEMORY) as dg:
        dg.upsert_file(_FILE, _SHA1)
        assert dg._conn is not None
    assert dg._conn is None


def test_context_manager_closes_connection_after_exception() -> None:
    """The connection is released even when the ``with`` body raises."""

    class _BoomError(RuntimeError):
        pass

    dg = DepGraph(_MEMORY)
    with pytest.raises(_BoomError), dg:
        dg.upsert_file(_FILE, _SHA1)
        raise _BoomError("body failed")

    assert dg._conn is None


def test_close_is_idempotent(graph: DepGraph) -> None:
    """Calling :meth:`DepGraph.close` twice is safe and leaves the graph closed."""
    graph.upsert_file(_FILE, _SHA1)
    graph.close()
    graph.close()  # must not raise
    assert graph._conn is None


# ---------------------------------------------------------------------------
# 8. Edge upsert with missing endpoints.
# ---------------------------------------------------------------------------


def test_upsert_edges_raises_when_target_is_missing(graph: DepGraph) -> None:
    """A target that is not in the ``symbols`` table triggers :class:`DepGraphError`.

    Silently dropping an unresolved target would break the "seed graph then
    traverse" contract that :meth:`transitive_dependents` relies on. See
    ``design.md §2.4`` and the module docstring in ``dep_graph.py``.
    """
    source = _symbol("pkg.mod.source")
    missing = _symbol("pkg.mod.missing")
    graph.upsert_symbols(_FILE, _SHA1, [source])  # note: `missing` is NOT indexed

    with pytest.raises(DepGraphError, match="target symbol"):
        graph.upsert_edges(source, [missing])


def test_upsert_edges_raises_when_source_is_missing(graph: DepGraph) -> None:
    """An unindexed source symbol is a caller bug — surface it, do not drop the edge."""
    source = _symbol("pkg.mod.source")
    target = _symbol("pkg.mod.target")
    graph.upsert_symbols(_FILE, _SHA1, [target])  # source is NOT indexed

    with pytest.raises(DepGraphError, match="source symbol"):
        graph.upsert_edges(source, [target])


def test_upsert_edges_empty_target_list_is_noop(graph: DepGraph) -> None:
    """Passing an empty target list is legal and stores no rows."""
    source = _symbol("pkg.mod.source")
    graph.upsert_symbols(_FILE, _SHA1, [source])
    graph.upsert_edges(source, [])
    assert _count(graph, "edges") == 0


def test_upsert_edges_deduplicates_on_repeated_calls(graph: DepGraph) -> None:
    """Repeated ``upsert_edges`` calls with the same endpoints stay at one row.

    The composite primary key ``(source_id, target_id, kind)`` handles dedup
    on the DB side via ``INSERT OR IGNORE``.
    """
    source = _symbol("pkg.mod.source")
    target = _symbol("pkg.mod.target")
    graph.upsert_symbols(_FILE, _SHA1, [source, target])

    graph.upsert_edges(source, [target])
    graph.upsert_edges(source, [target])
    graph.upsert_edges(source, [target])

    assert _count(graph, "edges") == 1


# ---------------------------------------------------------------------------
# 9. Data-integrity guardrails — corrupt column values surface as DepGraphError.
# ---------------------------------------------------------------------------


def test_hydrate_symbols_rejects_corrupt_kind(graph: DepGraph) -> None:
    """A ``kind`` value outside the four allowed literals surfaces as :class:`DepGraphError`.

    The ``CHECK`` constraint prevents this in practice, but the private
    ``_narrow_symbol_kind`` guard is the last line of defence if a foreign
    tool writes to ``state.db``. We bypass the constraint by disabling it
    with a raw connection, then let :meth:`transitive_dependents` walk over
    the tainted row.
    """
    source = _symbol("pkg.mod.source")
    target = _symbol("pkg.mod.target")
    graph.upsert_symbols(_FILE, _SHA1, [source, target])
    graph.upsert_edges(source, [target])

    # Force a bad `kind` value directly — bypass the CHECK constraint by
    # running a raw UPDATE. SQLite honours CHECK on INSERT/UPDATE, so we
    # instead poison the row with a value that passes the check character-set
    # by using PRAGMA-driven bypass: dropping and reinserting via a temp
    # table. Simplest path: disable the check with `PRAGMA ignore_check_constraints`.
    conn = graph._get_conn()
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE symbols SET kind = 'not-a-kind' WHERE qualified_name = ?",
        (source.qualified_name,),
    )

    with pytest.raises(DepGraphError, match="corrupt symbols row"):
        graph.transitive_dependents([target], max_hops=5)
