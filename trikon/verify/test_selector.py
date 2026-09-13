"""Select the pytest tests that exercise the impacted symbols.

The Verification Runner's job on every verdict is to run *only* the tests
that could reasonably observe the change. This module is the routing layer
between an :class:`~trikon.evidence.report.ImpactSet` (produced by the
Change-Intelligence subsystem) and the pytest node-ID list handed to the
sandbox.

Strategy, in the priority order established by ``design.md §6``:

1. **Coverage-map lookup.** For each changed symbol, query the
   ``coverage_map`` table keyed on ``(qualified_name, built_against_sha)``.
   Rows are produced by ``trikon coverage build`` (see Requirement 5.1) and
   record the exact pytest node IDs that touched the symbol on the base
   commit.
2. **Filename heuristic.** Any symbol that missed the map is routed through
   a filename fallback derived from the symbol's on-disk ``file_path`` — the
   candidates are ``tests/test_{leaf}.py`` (flat layout) and
   ``tests/{pkg}/test_{leaf}.py`` (mirrored layout), where ``leaf`` is the
   file stem with any leading ``src/`` prefix stripped. This surfaces at
   least the co-located test module for a brand-new symbol that has never
   been observed by an instrumented run.
3. **Staleness signal.** If the freshest ``built_at`` row is older than seven
   days *or* any symbol missed under the requested ``base_sha``, the whole
   selection is flagged ``coverage_map_stale=True`` and every symbol routes
   through the filename fallback for the remainder of the call
   (Requirement 1.3).

Every raise site in this module wraps its cause in
:class:`~trikon.verify.errors.TestSelectionError`. Requirement 6.1 forbids
bare ``sqlite3.Error`` or ``json.JSONDecodeError`` from escaping
``trikon/verify/**``; the SDK boundary (``design.md §10``) relies on that
closure to translate every internal failure into a ``require_human`` verdict.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from trikon.evidence.report import ImpactSet, SymbolRef
from trikon.verify.errors import TestSelectionError
from trikon.verify.models import SelectedTests

__all__ = ["select_impacted_tests"]


# ---------------------------------------------------------------------------
# Age threshold (design.md §6, Requirement 1.3)
# ---------------------------------------------------------------------------
#
# The freshest ``built_at`` row is considered stale once it is more than
# seven days behind ``now``. The bound is deliberately generous — a coverage
# rebuild is a full-suite pytest run and we do not want CI to trigger one
# on every merge — but tight enough that a symbol added a week ago has had
# at least one opportunity to be observed by an instrumented run.
_COVERAGE_MAP_MAX_AGE: timedelta = timedelta(days=7)


def select_impacted_tests(
    conn: sqlite3.Connection,
    impact: ImpactSet,
    *,
    repo_path: Path,
    base_sha: str | None,
    now: datetime | None = None,
) -> SelectedTests:
    """Return the pytest node IDs to execute for this ``ImpactSet``.

    Implements the full six-step algorithm from ``design.md §6``:

    * Step 1 queries ``MAX(built_at) FROM coverage_map`` and computes an age
      against ``now``; an empty table or an age above
      :data:`_COVERAGE_MAP_MAX_AGE` marks the whole map stale.
    * Step 2 iterates ``impact.changed_symbols`` and, when the map is not
      stale and ``base_sha`` is known, looks up
      ``(qualified_name, built_against_sha)`` rows in ``coverage_map``. A hit
      contributes its parsed ``test_ids_json`` list to the union; a miss is
      recorded on the fallback list with the full :class:`SymbolRef` (we
      need ``file_path`` for the filename heuristic in Step 4).
    * Step 3 sets ``coverage_map_stale`` to ``True`` if the freshest row was
      stale *or* any symbol missed the map (Requirement 1.3 requires the
      staleness signal to propagate to :attr:`TestReport.coverage_map_stale`
      whenever the fallback engaged).
    * Step 4 derives filename-heuristic candidates from each missed symbol's
      ``file_path``: strip any leading ``src/``, take the file stem as the
      test leaf, and check both ``tests/test_{leaf}.py`` (flat layout) and
      ``tests/{pkg}/test_{leaf}.py`` (mirrored layout, one candidate per
      surviving package segment). Only on-disk files under ``repo_path``
      contribute node IDs; missing candidates are skipped silently.
    * Step 5 unions coverage-map hits and fallback IDs into a single
      ``set`` and sorts the result deterministically so the same
      ``ImpactSet`` always yields byte-identical JSON downstream (design
      goal G1).
    * Step 6 assembles the :class:`SelectedTests` return value.
      ``fallback_reasons`` gets one entry per missed symbol regardless of
      whether the filename heuristic found an on-disk candidate — a
      populated reason with no matching node ID still tells the CLI
      formatter (Task 11) that a symbol went unverified.

    Args:
        conn: Open :class:`sqlite3.Connection` to ``<repo>/.trikon/state.db``.
            The caller is expected to have already applied Phase-1 pragmas
            and to have run
            :func:`~trikon.verify.db.ensure_verify_tables` at least once;
            this function does not attempt to create tables itself.
        impact: The change's :class:`ImpactSet`. Only
            ``impact.changed_symbols`` is consumed here.
        repo_path: Repository root. The filename heuristic (Step 4) probes
            for on-disk test files relative to this path; candidates that
            do not resolve to an existing file are dropped silently.
        base_sha: The base commit's SHA. When ``None`` (no reachable base,
            or a bare-repo scenario), every symbol is routed through the
            fallback because the SHA-keyed cache cannot be consulted.
        now: Injectable clock. Defaults to :func:`datetime.now` in UTC so
            unit tests can pin the coverage-map staleness boundary.

    Returns:
        A :class:`SelectedTests` whose ``node_ids`` is a sorted tuple of
        the union of coverage-map hits and on-disk fallback candidates,
        ``coverage_map_stale`` reflects the combined Step 1 + Step 3
        signal, and ``fallback_reasons`` records one
        ``"{qname}: no coverage-map row"`` entry per missed symbol.

    Raises:
        TestSelectionError: When the coverage-map lookup fails with a
            :class:`sqlite3.Error`, or when a ``test_ids_json`` payload is
            malformed (JSON parse failure, non-list shape, non-string node
            ID), or when the stored ``built_at`` value is not a parseable
            ISO-8601 string. In every case the original exception is
            preserved on ``__cause__`` via ``raise ... from`` so the SDK
            boundary can surface the underlying diagnostic
            (``design.md §9.1``).
    """
    resolved_now: datetime = datetime.now(UTC) if now is None else now

    # ------------------------------------------------------------------
    # STEP 1: Determine coverage-map freshness.
    # ------------------------------------------------------------------
    map_stale = _coverage_map_is_stale(conn, resolved_now)

    # ------------------------------------------------------------------
    # STEP 2: For each changed symbol, look up its tests.
    # ------------------------------------------------------------------
    # ``hits`` is keyed by qualified_name so a later diagnostic layer can
    # show which symbol contributed which node IDs; the value tuple is the
    # already-parsed pytest node-ID list from ``test_ids_json``.
    # ``misses`` carries the full :class:`SymbolRef` (not just the qualified
    # name) so Step 4 can consult ``file_path`` when deriving fallback
    # candidates.
    hits: dict[str, tuple[str, ...]] = {}
    misses: list[SymbolRef] = []
    for symbol in impact.changed_symbols:
        qname = symbol.qualified_name
        # Blanket fallback: if the map itself is stale, or we have no base
        # SHA to key the cache on, no per-row lookup can be trusted.
        if map_stale or base_sha is None:
            misses.append(symbol)
            continue
        row_node_ids = _lookup_coverage_row(conn, qname, base_sha)
        if row_node_ids is None:
            misses.append(symbol)
            continue
        hits[qname] = row_node_ids

    # ------------------------------------------------------------------
    # STEP 3: If any symbol fell back OR the freshest row is stale,
    # mark the whole selection stale (Requirement 1.3).
    # ------------------------------------------------------------------
    stale = map_stale or bool(misses)

    # ------------------------------------------------------------------
    # STEP 4: Filename heuristic for the miss set (Requirement 1.2).
    # ------------------------------------------------------------------
    # For every symbol that missed the coverage map, derive
    # ``tests/test_{leaf}.py`` and ``tests/{pkg}/test_{leaf}.py``
    # candidates from ``symbol.file_path``. Only candidates that resolve to
    # an on-disk file under ``repo_path`` contribute node IDs; the
    # ``fallback_reasons`` list records the miss regardless so the CLI can
    # explain the coverage gap.
    fallback_ids: list[str] = []
    for symbol in misses:
        for candidate in _fallback_candidates_for_symbol(repo_path, symbol.file_path):
            if candidate.is_file():
                rel = candidate.relative_to(repo_path)
                fallback_ids.append(PurePosixPath(rel).as_posix())

    # ------------------------------------------------------------------
    # STEPS 5-6: Union, dedupe, sort deterministically, and return.
    # ------------------------------------------------------------------
    node_id_union: set[str] = {nid for node_ids in hits.values() for nid in node_ids}
    node_id_union.update(fallback_ids)

    return SelectedTests(
        node_ids=tuple(sorted(node_id_union)),
        coverage_map_stale=stale,
        fallback_reasons=tuple(
            f"{symbol.qualified_name}: no coverage-map row" for symbol in misses
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coverage_map_is_stale(conn: sqlite3.Connection, now: datetime) -> bool:
    """Return ``True`` when the freshest coverage-map row is missing or old.

    Implements Step 1 of ``design.md §6``. An empty ``coverage_map`` table —
    no full-suite build has ever completed — counts as stale, because there
    is nothing to trust. Otherwise the freshest ``built_at`` is parsed as
    ISO-8601 and compared against :data:`_COVERAGE_MAP_MAX_AGE`.

    Every failure mode of this helper is wrapped in
    :class:`TestSelectionError`: SQLite errors on the ``MAX(built_at)``
    query, malformed non-string payloads, and unparseable ISO-8601 strings.
    Bubbling those raw would let a corrupt state DB escape the module
    boundary and defeat Requirement 6.1.
    """
    try:
        cursor = conn.execute("SELECT MAX(built_at) FROM coverage_map")
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise TestSelectionError(
            "select_impacted_tests: failed to query coverage_map freshness"
        ) from exc

    if row is None or row[0] is None:
        # No coverage-map rows at all — the caller has never run
        # ``trikon coverage build``, or its last build failed and rolled back.
        return True

    freshest_iso = row[0]
    if not isinstance(freshest_iso, str):
        raise TestSelectionError(
            "select_impacted_tests: coverage_map.built_at is not a string: "
            f"{type(freshest_iso).__name__}"
        )

    try:
        freshest = datetime.fromisoformat(freshest_iso)
    except ValueError as exc:
        raise TestSelectionError(
            f"select_impacted_tests: coverage_map.built_at is not ISO-8601: {freshest_iso!r}"
        ) from exc

    # The Phase-1 writer stores UTC-aware timestamps (see
    # ``trikon.change_intel.dep_graph._now_iso``). Defensive normalization:
    # if a naive timestamp somehow made it into the table, coerce it to UTC
    # so the subtraction against ``now`` (which is always aware here) does
    # not raise a bare ``TypeError`` past the module boundary.
    if freshest.tzinfo is None:
        freshest = freshest.replace(tzinfo=UTC)

    return (now - freshest) > _COVERAGE_MAP_MAX_AGE


def _lookup_coverage_row(
    conn: sqlite3.Connection,
    qualified_name: str,
    base_sha: str,
) -> tuple[str, ...] | None:
    """Fetch and parse the ``test_ids_json`` for a ``(qname, base_sha)`` pair.

    Implements the SQL half of Step 2 of ``design.md §6``. Returns:

    * ``None`` on a clean cache miss (no row for the pair), so the caller
      can record a fallback reason and continue.
    * A ``tuple[str, ...]`` of pytest node IDs on a hit.

    Every deserialization failure — SQLite error on the query, malformed
    JSON, non-list JSON root, or a non-string element inside the list — is
    wrapped in :class:`TestSelectionError`. A malformed row is treated as a
    hard error rather than a silent miss because it indicates state-db
    corruption that the SDK boundary should surface as ``require_human``.
    """
    try:
        cursor = conn.execute(
            "SELECT test_ids_json, built_against_sha, built_at "
            "FROM coverage_map "
            "WHERE qualified_name = ? AND built_against_sha = ? "
            "LIMIT 1",
            (qualified_name, base_sha),
        )
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise TestSelectionError(
            "select_impacted_tests: failed to query coverage_map for "
            f"qualified_name={qualified_name!r}"
        ) from exc

    if row is None:
        return None

    test_ids_json = row[0]
    if not isinstance(test_ids_json, str):
        raise TestSelectionError(
            "select_impacted_tests: coverage_map.test_ids_json for "
            f"{qualified_name!r} is not a string: {type(test_ids_json).__name__}"
        )

    try:
        decoded = json.loads(test_ids_json)
    except json.JSONDecodeError as exc:
        raise TestSelectionError(
            "select_impacted_tests: coverage_map.test_ids_json for "
            f"{qualified_name!r} is malformed JSON"
        ) from exc

    if not isinstance(decoded, list):
        raise TestSelectionError(
            "select_impacted_tests: coverage_map.test_ids_json for "
            f"{qualified_name!r} is not a JSON array"
        )

    node_ids: list[str] = []
    for element in decoded:
        if not isinstance(element, str):
            raise TestSelectionError(
                "select_impacted_tests: coverage_map.test_ids_json for "
                f"{qualified_name!r} contains a non-string node ID"
            )
        node_ids.append(element)
    return tuple(node_ids)


def _fallback_candidates_for_symbol(
    repo_path: Path,
    symbol_file_path: str,
) -> tuple[Path, ...]:
    """Return the on-disk candidate test files for a symbol's ``file_path``.

    Implements Step 4 of ``design.md §6``. Given a symbol's source-file
    path (e.g. ``"src/payments/retry.py"`` or ``"api/payments/retry.py"``)
    and the repository root, produce the ordered tuple of test-file
    candidates the caller should ``.is_file()``-check:

    * The flat layout: ``<repo>/tests/test_{leaf}.py``.
    * The mirrored layout, one entry per surviving package prefix:
      ``<repo>/tests/{pkg}/test_{leaf}.py`` and — for deeper nestings such
      as ``src/api/payments/retry.py`` — ``<repo>/tests/api/payments/test_retry.py``.

    ``leaf`` is the file stem after any leading ``src/`` segment is
    stripped. Non-Python files, ``__init__.py``, and paths that resolve
    outside the repository (via a returned :class:`ValueError` from
    :meth:`~pathlib.Path.relative_to`) are skipped by returning an empty
    tuple — the caller records a ``fallback_reasons`` entry for the miss
    regardless, so a symbol without a plausible test file is not silently
    lost.

    The helper does not touch disk; existence checks are the caller's
    responsibility. That split lets unit tests exercise the path-derivation
    logic without materializing a filesystem fixture.
    """
    if not symbol_file_path:
        return ()

    # ``SymbolRef.file_path`` is written as POSIX by the indexer (see
    # ``trikon.change_intel.ast_indexer``) and normalized to a
    # repo-relative posix path by ``compute_impact`` (see
    # ``trikon.change_intel.blast_radius._normalize_file_path``). Defensive
    # normalization here handles absolute paths and Windows-style
    # separators that could arrive from a direct caller in a test.
    normalized = symbol_file_path.replace("\\", "/")
    raw = PurePosixPath(normalized)
    if raw.is_absolute():
        try:
            rel_posix = Path(normalized).resolve().relative_to(repo_path.resolve()).as_posix()
        except (OSError, ValueError):
            return ()
        raw = PurePosixPath(rel_posix)

    parts: tuple[str, ...] = raw.parts
    if not parts:
        return ()

    # Strip a leading ``src/`` layout marker so ``src/payments/retry.py`` and
    # ``payments/retry.py`` produce the same test candidates. This matches
    # the convention used by every layout in ``examples/`` and by the
    # Phase-1 test fixtures.
    if parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return ()

    file_name = parts[-1]
    if not file_name.endswith(".py"):
        # A dependency-graph edge onto a non-Python file (e.g. a resource
        # imported via ``importlib.resources``) cannot map to a pytest
        # module through the filename heuristic.
        return ()

    leaf = file_name[: -len(".py")]
    if not leaf or leaf == "__init__":
        # ``__init__.py`` is a package marker, not a module the heuristic
        # can name a test file after. Skip.
        return ()

    pkg_prefix_parts: tuple[str, ...] = parts[:-1]

    candidates: list[Path] = [repo_path / "tests" / f"test_{leaf}.py"]
    if pkg_prefix_parts:
        pkg_prefix = PurePosixPath(*pkg_prefix_parts)
        candidates.append(repo_path / "tests" / pkg_prefix / f"test_{leaf}.py")
    return tuple(candidates)
