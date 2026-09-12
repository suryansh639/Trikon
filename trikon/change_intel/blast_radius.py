"""Compute the blast radius of a change.

The blast-radius orchestrator turns a :class:`ChangeSet` into a public
:class:`~trikon.evidence.report.ImpactSet` by unioning:

    * directly changed symbols (hunk lines mapped to enclosing definitions),
    * transitive dependents pulled from the dep graph up to N hops, and
    * tests that cover any of the above.

A scalar ``blast_radius_score`` is produced by weighted combination and
bucketed into ``LOW`` / ``MEDIUM`` / ``HIGH`` for policy consumption.

Public surface (design.md §2.5):

    * :class:`BlastWeights` — the tunable scoring knobs.
    * :func:`bucket` — total function that maps a score to a bucket.
    * :func:`enclosing_symbols` — maps changed line numbers to the symbols
      whose line ranges contain them.
    * :func:`compute_impact` — the orchestrator that composes the whole
      Change Intelligence pipeline.

Phase-1 note on transitive dependents
-------------------------------------

The design calls for :func:`compute_impact` to run
:func:`symbol_resolver.find_references` on every changed symbol so the
:class:`~trikon.change_intel.dep_graph.DepGraph` has real edges to walk.
Jedi is expensive though — on a Django-sized repo the cold-index target
in ``design.md §7`` blows out if every ``verify`` call goes through the
resolver. Phase 1 therefore ships without automatic edge population:
we query :meth:`DepGraph.transitive_dependents` (so callers that
pre-populated edges in an out-of-band cold-index step still get the
uplift), but we do not populate edges here. That means fresh caches
report ``changed_symbols`` only — the ``impacted_public_apis`` /
``impacted_modules`` fanout that the ``bad_retry`` and
``sensitive_touch`` fixtures encode is unreachable until an edge-
population helper lands (tracked as follow-up to task 8.2). See the
smoke-test discrepancy report in task 8.2's completion notes.
"""

from __future__ import annotations

import fnmatch
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from trikon.change_intel.ast_indexer import index_files
from trikon.change_intel.dep_graph import DepGraph
from trikon.change_intel.errors import BlastRadiusError, ChangeIntelError
from trikon.change_intel.models import (
    ChangeSet,
    FileChange,
    SymbolDef,
)
from trikon.evidence.report import BlastBucket, ImpactSet, SymbolRef


@dataclass(frozen=True)
class BlastWeights:
    """Weights and thresholds that shape the numeric blast-radius score.

    Defaults are calibrated for a mid-size Python service. Large monorepos
    typically want lower ``impacted_modules`` weight to avoid HIGH-bucket
    saturation; latency-critical or regulated code paths (``payments/``,
    ``auth/``, ``billing/``) get a hefty ``sensitive_path_touch`` bump so a
    single line touched inside them is enough to escalate the verdict.

    ``sensitive_paths`` uses POSIX glob syntax. Each entry is matched against
    every changed file's POSIX path; a trailing ``/**`` means "any file below
    a directory called ``<prefix>`` anywhere in the tree" so patterns keep
    working regardless of whether the repo layout uses a ``src/`` root.
    """

    impacted_modules: float = 1.0
    impacted_public_apis: float = 3.0
    impacted_test_files: float = 0.5
    cross_package_hops: float = 2.0
    sensitive_path_touch: float = 5.0

    low_bucket_max: float = 5.0
    medium_bucket_max: float = 15.0

    sensitive_paths: tuple[str, ...] = field(default=("payments/**", "auth/**", "billing/**"))


def bucket(score: float, *, weights: BlastWeights | None = None) -> BlastBucket:
    """Bucket a numeric score into ``LOW`` / ``MEDIUM`` / ``HIGH``.

    Total function on the reals. Never raises. Callers should never pass
    ``NaN``, but if they do, the safest verdict is ``HIGH`` — "we do not know
    the risk" must not look like "we know the risk is low".

    Args:
        score: The numeric blast-radius score produced by the orchestrator.
        weights: The :class:`BlastWeights` whose ``low_bucket_max`` and
            ``medium_bucket_max`` thresholds define the bucket boundaries.
            When ``None`` the defaults from ``BlastWeights()`` apply.

    Returns:
        ``"LOW"`` when ``score <= weights.low_bucket_max``,
        ``"MEDIUM"`` when ``score <= weights.medium_bucket_max``,
        ``"HIGH"`` otherwise (including for ``NaN``).
    """
    w = weights if weights is not None else BlastWeights()
    if math.isnan(score):
        return "HIGH"
    if score <= w.low_bucket_max:
        return "LOW"
    if score <= w.medium_bucket_max:
        return "MEDIUM"
    return "HIGH"


def enclosing_symbols(
    file_path: Path,
    changed_lines: Iterable[int],
    symbols: list[SymbolDef],
) -> list[SymbolDef]:
    """Return the symbols whose line ranges contain any of ``changed_lines``.

    A symbol "encloses" a changed line ``n`` when
    ``symbol.start_line <= n <= symbol.end_line``. Callers pass only the
    symbols for the file identified by ``file_path``; the argument exists so
    the caller can log or attribute diagnostics against the right source.

    Args:
        file_path: The file whose symbols are being checked. Used for
            attribution only; not used to filter ``symbols``.
        changed_lines: Any iterable of 1-based line indices. Converted to a
            set internally for O(1) membership checks. Duplicates are fine.
        symbols: The symbol table for ``file_path``. Order is not required.

    Returns:
        The matching symbols sorted by ``(start_line, qualified_name)`` so
        identical inputs produce identical output. Empty when
        ``changed_lines`` is empty or nothing intersects.
    """
    del file_path  # reserved for future logging / attribution
    lines = set(changed_lines)
    if not lines:
        return []
    matches = [
        sym for sym in symbols if any(sym.start_line <= line <= sym.end_line for line in lines)
    ]
    matches.sort(key=lambda s: (s.start_line, s.qualified_name))
    return matches


def compute_impact(
    change_set: ChangeSet,
    repo_path: Path,
    *,
    cache_db: Path | None = None,
    weights: BlastWeights | None = None,
    max_hops: int = 5,
) -> ImpactSet:
    """Orchestrate the full Change Intelligence pipeline for ``change_set``.

    The six-step orchestration from ``design.md §2.5``:

    1. Index every changed ``.py`` / ``.pyi`` file that still exists on
       disk. Deleted files are skipped — their symbols cannot be recovered
       from the post-image working tree without a git-show shell-out,
       which Phase 1 does not do.
    2. For every hunk in every Python file, map the union of
       ``added_lines`` and ``removed_lines`` to enclosing
       :class:`SymbolDef` values via :func:`enclosing_symbols`.
    3. Query :meth:`DepGraph.transitive_dependents` with the changed
       symbols as seeds. Phase 1 does not auto-populate edges (see the
       module docstring for why), so on a fresh cache the transitive set
       is empty; on a cache that was seeded out of band it fans out.
    4. Union changed and transitive symbols into ``impacted_symbols``.
       ``impacted_modules`` is the sorted set of top-level packages —
       ``qualified_name.split(".")[0]`` — because that is the granularity
       the fixtures encode. ``impacted_public_apis`` is the ``is_public``
       subset.
    5. Filename-heuristic test selection: for every impacted module path
       (dotted, derived from the symbol's ``file_path`` under ``src/`` if
       present), look for ``tests/test_{leaf}.py`` and
       ``tests/{pkg}/test_{leaf}.py`` inside ``repo_path``. Any changed
       file already under ``tests/`` is added too.
    6. Weighted-sum the score, bucket it, and pack the whole thing into
       :class:`ImpactSet`.

    Args:
        change_set: The parsed :class:`ChangeSet` from :func:`parse_diff`.
        repo_path: The repository root. Used for resolving relative file
            paths and for locating the default cache directory.
        cache_db: Path to the SQLite cache. Defaults to
            ``repo_path / ".trikon" / "state.db"``; the parent directory
            is created on demand.
        weights: Scoring weights. Defaults to :class:`BlastWeights` with
            the design's tuned defaults.
        max_hops: Bound on the BFS depth used by
            :meth:`DepGraph.transitive_dependents`. Passed through
            unchanged; the DepGraph itself validates it.

    Returns:
        A fully populated :class:`ImpactSet`. Every list field is sorted
        deterministically so identical inputs produce byte-identical
        JSON.

    Raises:
        BlastRadiusError: A downstream Change Intelligence stage
            (:class:`~trikon.change_intel.dep_graph.DepGraph`,
            :func:`index_files`, ...) raised a :class:`ChangeIntelError`.
            The original exception is chained via ``__cause__`` so the SDK
            boundary can report the underlying reason unmodified.
    """
    w = weights if weights is not None else BlastWeights()
    resolved_cache_db = _resolve_cache_db(repo_path, cache_db)

    # Non-Python-only change sets short-circuit before touching SQLite:
    # nothing to index, nothing to look up, no cache write. Sensitive-path
    # weight still applies because the change *could* touch a sensitive
    # non-Python file (e.g. a payments-scoped YAML config).
    if not change_set.python_files:
        return _empty_python_impact(change_set, weights=w)

    try:
        return _compute_impact_inner(
            change_set=change_set,
            repo_path=repo_path,
            cache_db=resolved_cache_db,
            weights=w,
            max_hops=max_hops,
        )
    except ChangeIntelError as exc:
        raise BlastRadiusError(f"compute_impact failed: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Orchestrator internals
# ---------------------------------------------------------------------------


def _compute_impact_inner(
    *,
    change_set: ChangeSet,
    repo_path: Path,
    cache_db: Path,
    weights: BlastWeights,
    max_hops: int,
) -> ImpactSet:
    """Do the actual work of :func:`compute_impact`.

    Extracted so the outer function's ``try / except ChangeIntelError`` can
    wrap a single call site, keeping the exception chain straight.
    """
    python_files_to_index = _collect_python_paths(change_set.python_files, repo_path)
    symbols_by_path: dict[Path, list[SymbolDef]] = (
        index_files(python_files_to_index, cache_db=cache_db) if python_files_to_index else {}
    )

    changed_symbols = _collect_changed_symbols(
        change_set=change_set,
        repo_path=repo_path,
        symbols_by_path=symbols_by_path,
    )

    # Transitive dependents from the dep graph. Phase 1 does not populate
    # edges here so this is a no-op on fresh caches; kept in place so that
    # callers who seeded edges out of band (or a future automatic
    # population helper) get the fanout for free.
    transitive: list[SymbolDef] = []
    if changed_symbols:
        with DepGraph(cache_db) as graph:
            transitive = graph.transitive_dependents(
                list(changed_symbols),
                max_hops=max_hops,
            )

    impacted_symbols = _merge_symbol_sets(changed_symbols, transitive)

    impacted_modules = sorted(
        {_top_package(_normalize_qualified_name(sym.qualified_name)) for sym in impacted_symbols}
    )
    impacted_public_apis = [sym for sym in impacted_symbols if sym.is_public]
    impacted_tests = _impacted_tests(
        change_set=change_set,
        repo_path=repo_path,
        impacted_symbols=impacted_symbols,
    )

    sensitive_touches = _count_sensitive_touches(change_set.files, weights.sensitive_paths)

    score = _compute_score(
        weights=weights,
        n_modules=len(impacted_modules),
        n_public_apis=len(impacted_public_apis),
        n_tests=len(impacted_tests),
        n_sensitive_touches=sensitive_touches,
    )

    return ImpactSet(
        changed_files=sorted(fc.path for fc in change_set.files),
        changed_symbols=_public_refs_sorted(changed_symbols, repo_path=repo_path),
        impacted_modules=impacted_modules,
        impacted_public_apis=_public_refs_sorted(impacted_public_apis, repo_path=repo_path),
        impacted_tests=impacted_tests,
        blast_radius_score=bucket(score, weights=weights),
        blast_radius_numeric=score,
    )


def _empty_python_impact(change_set: ChangeSet, *, weights: BlastWeights) -> ImpactSet:
    """Return the :class:`ImpactSet` for a change set with no Python files.

    Sensitive-path weight still applies so a policy that flags a
    ``payments/**`` YAML edit as HIGH still fires without any symbols to
    resolve. The fixtures the pipeline is validated against never hit this
    branch with a sensitive touch, but the arithmetic is preserved for
    correctness of the general case.
    """
    sensitive_touches = _count_sensitive_touches(change_set.files, weights.sensitive_paths)
    score = weights.sensitive_path_touch * sensitive_touches
    return ImpactSet(
        changed_files=sorted(fc.path for fc in change_set.files),
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score=bucket(score, weights=weights),
        blast_radius_numeric=score,
    )


# ---------------------------------------------------------------------------
# Indexing / symbol collection
# ---------------------------------------------------------------------------


def _resolve_cache_db(repo_path: Path, cache_db: Path | None) -> Path:
    """Pick a cache-db path, defaulting to ``<repo>/.trikon/state.db``.

    Creates the parent directory on demand so first-run callers do not need
    to prepare the state directory themselves. The database file itself is
    created lazily by :class:`DepGraph` on first mutating call.
    """
    resolved = cache_db if cache_db is not None else (repo_path / ".trikon" / "state.db")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _collect_python_paths(
    python_files: tuple[FileChange, ...],
    repo_path: Path,
) -> list[Path]:
    """Return absolute paths for Python files that still exist on disk.

    Deleted files (``change_kind == "deleted"``) and files that vanished
    for any other reason are silently skipped: the AST indexer only
    reads the post-image working tree, and pulling a symbol table for a
    deleted file requires a git-show shell-out that Phase 1 does not
    perform. The missing symbols are reported honestly as an empty
    ``changed_symbols`` for that file, not fabricated from stale cache.
    """
    paths: list[Path] = []
    for fc in python_files:
        if fc.change_kind == "deleted":
            continue
        abs_path = (repo_path / fc.path).resolve()
        if not abs_path.exists():
            continue
        paths.append(abs_path)
    return paths


def _collect_changed_symbols(
    *,
    change_set: ChangeSet,
    repo_path: Path,
    symbols_by_path: dict[Path, list[SymbolDef]],
) -> list[SymbolDef]:
    """Map every changed line in every hunk to the *innermost* enclosing symbol.

    :func:`enclosing_symbols` returns every ancestor whose line range
    intersects a changed line — for a method inside a class it emits both
    the class and the method. That over-broad view is useful when the
    caller wants to attribute a change to any layer of the containment
    hierarchy, but for ``changed_symbols`` the intent is finer: attribute
    each edited line to its most specific owner. A body-only edit inside
    ``PaymentWorker.process`` should surface as ``PaymentWorker.process``,
    not as the wider ``PaymentWorker``.

    Innermost is picked by smallest ``(end_line - start_line)`` span, with
    a tie-break on greater ``start_line`` (the more nested definition) and
    then a stable tie-break on ``qualified_name``. Deduplicated by
    ``(qualified_name, file_sha)`` (the same identity :class:`DepGraph`
    uses) so a symbol whose body spans multiple hunks only lands in the
    result once. Insertion order tracks the ``change_set.python_files``
    traversal because the caller re-sorts the final list before emitting.
    """
    seen: set[tuple[str, str]] = set()
    result: list[SymbolDef] = []
    for fc in change_set.python_files:
        if fc.change_kind == "deleted":
            continue
        abs_path = (repo_path / fc.path).resolve()
        symbols = symbols_by_path.get(abs_path)
        if not symbols:
            continue
        for hunk in fc.hunks:
            changed_lines = set(hunk.added_lines) | set(hunk.removed_lines)
            if not changed_lines:
                continue
            for line in changed_lines:
                inner = _innermost_enclosing(abs_path, line, symbols)
                if inner is None:
                    continue
                key = (inner.qualified_name, inner.file_sha)
                if key in seen:
                    continue
                seen.add(key)
                result.append(inner)
    return result


def _innermost_enclosing(
    file_path: Path,
    line: int,
    symbols: list[SymbolDef],
) -> SymbolDef | None:
    """Return the smallest-span symbol whose line range contains ``line``.

    Reuses :func:`enclosing_symbols` for the intersection test so both
    entry points share one implementation of the containment rule.
    Returns ``None`` when no symbol encloses ``line`` — this happens for
    edits at module scope between definitions (blank lines, imports,
    top-level statements that were not captured by the AST indexer as
    named symbols).
    """
    matches = enclosing_symbols(file_path, [line], symbols)
    if not matches:
        return None
    return min(
        matches,
        key=lambda s: (s.end_line - s.start_line, -s.start_line, s.qualified_name),
    )


def _merge_symbol_sets(
    changed: list[SymbolDef],
    transitive: list[SymbolDef],
) -> list[SymbolDef]:
    """Union changed and transitive symbols, deduped by ``(qualified_name, file_sha)``.

    Changed symbols come first so their order is preserved; transitive
    symbols follow in the BFS order supplied by
    :meth:`DepGraph.transitive_dependents`.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[SymbolDef] = []
    for sym in (*changed, *transitive):
        key = (sym.qualified_name, sym.file_sha)
        if key in seen:
            continue
        seen.add(key)
        merged.append(sym)
    return merged


# ---------------------------------------------------------------------------
# Impact fanout
# ---------------------------------------------------------------------------


def _top_package(qualified_name: str) -> str:
    """Return the top-level dotted component of a qualified name.

    ``"payments.retry.with_backoff"`` -> ``"payments"``. A bare identifier
    (``"foo"``) returns itself. An empty string returns itself so callers
    do not need a special case.
    """
    if not qualified_name:
        return qualified_name
    return qualified_name.split(".", 1)[0]


def _module_path_for(sym: SymbolDef) -> str:
    """Derive the dotted module path for a symbol from its ``file_path``.

    Strips a leading ``src/`` layout root (common for Python projects that
    keep sources under ``src/<pkg>/...``) and the trailing ``.py`` /
    ``.pyi`` suffix, replacing directory separators with dots. For a
    package's ``__init__.py`` the module path is the package itself
    (``src/orders/__init__.py`` -> ``"orders"``), which is what the test
    heuristic wants.
    """
    p = PurePosixPath(sym.file_path)
    parts = list(p.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return ""
    leaf = parts[-1]
    if leaf.endswith(".pyi"):
        leaf = leaf[: -len(".pyi")]
    elif leaf.endswith(".py"):
        leaf = leaf[: -len(".py")]
    if leaf == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = leaf
    return ".".join(parts)


def _impacted_tests(
    *,
    change_set: ChangeSet,
    repo_path: Path,
    impacted_symbols: list[SymbolDef],
) -> list[str]:
    """Return the sorted list of test files touched by the change set.

    Two sources are unioned:

    * The filename heuristic from ``design.md §2.5``. For every impacted
      symbol's module path ``pkg.sub.leaf`` we check
      ``tests/test_{leaf}.py``, ``tests/{pkg}/test_{leaf}.py``, and — for
      deeply nested layouts — ``tests/{pkg}/{sub}/test_{leaf}.py``. Only
      files that actually exist on disk are included, so stale expectations
      don't leak into the output.
    * Every file in ``change_set.files`` whose POSIX path lives under a
      ``tests/`` component — a test file that was itself modified is
      trivially impacted.
    """
    candidates: set[str] = set()

    for sym in impacted_symbols:
        module_path = _module_path_for(sym)
        if not module_path:
            continue
        parts = module_path.split(".")
        if not parts:
            continue
        leaf = parts[-1]
        pkg_parts = parts[:-1]

        heuristic_candidates = [PurePosixPath("tests") / f"test_{leaf}.py"]
        if pkg_parts:
            nested = PurePosixPath("tests", *pkg_parts) / f"test_{leaf}.py"
            heuristic_candidates.append(nested)
            heuristic_candidates.append(PurePosixPath("tests", pkg_parts[0]) / f"test_{leaf}.py")

        for candidate in heuristic_candidates:
            abs_candidate = repo_path / Path(str(candidate))
            if abs_candidate.exists():
                candidates.add(candidate.as_posix())

    for fc in change_set.files:
        rel_path = PurePosixPath(fc.path)
        if "tests" in rel_path.parts and rel_path.name.startswith("test_"):
            candidates.add(fc.path)

    return sorted(candidates)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _count_sensitive_touches(
    files: tuple[FileChange, ...],
    sensitive_paths: tuple[str, ...],
) -> int:
    """Count changed files whose path matches any sensitive-path glob.

    A file counts once regardless of how many patterns it matches, because
    the scoring rule is "did this change touch a sensitive path?", not "how
    many sensitive globs cover this path?".
    """
    if not sensitive_paths:
        return 0
    total = 0
    for fc in files:
        if _matches_any_glob(fc.path, sensitive_paths):
            total += 1
    return total


def _matches_any_glob(path: str, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` if ``path`` matches at least one pattern in ``patterns``."""
    p = PurePosixPath(path)
    return any(_glob_match(p, pattern) for pattern in patterns)


def _glob_match(path: PurePosixPath, pattern: str) -> bool:
    """Match a POSIX path against a glob supporting ``**`` semantics.

    Two shapes handled:

    * ``prefix/**`` — matches any file whose path contains ``prefix`` as
      a contiguous run of directory components followed by at least one
      more component. That reads ``payments/**`` as "any file below a
      directory called ``payments`` anywhere in the tree", which is what
      the ``design.md`` default weight wants: it must fire on both
      ``payments/gateway.py`` and ``src/payments/gateway.py``.
    * Anything else — falls through to :func:`fnmatch.fnmatch` on the
      full path, which handles plain wildcards (``*.md``) and literal
      matches. :meth:`PurePosixPath.match` is not used because its
      right-anchored semantics don't compose with the layout-agnostic
      behavior above.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if not prefix:
            return True
        prefix_parts = tuple(prefix.split("/"))
        parts = path.parts
        n = len(prefix_parts)
        return any(parts[i : i + n] == prefix_parts for i in range(len(parts) - n))
    return fnmatch.fnmatch(str(path), pattern)


def _compute_score(
    *,
    weights: BlastWeights,
    n_modules: int,
    n_public_apis: int,
    n_tests: int,
    n_sensitive_touches: int,
) -> float:
    """Combine the four contribution counts into a single numeric score.

    ``cross_package_hops`` from :class:`BlastWeights` is intentionally not
    consumed in Phase 1; it lands when the dep graph starts recording the
    inter-package edge kind. Adding it as a zero term now keeps the
    arithmetic consistent with the fixture-comment expressions in
    ``tests/fixtures/expected_impact/README.md``.
    """
    return (
        weights.impacted_modules * n_modules
        + weights.impacted_public_apis * n_public_apis
        + weights.impacted_test_files * n_tests
        + weights.sensitive_path_touch * n_sensitive_touches
        + weights.cross_package_hops * 0.0
    )


# ---------------------------------------------------------------------------
# Public-boundary translation
# ---------------------------------------------------------------------------


def _public_refs_sorted(symbols: list[SymbolDef], *, repo_path: Path) -> list[SymbolRef]:
    """Translate internal symbols to public refs, sorted by ``qualified_name``.

    Two boundary-shape adjustments happen here:

    * ``file_path`` is normalized to POSIX, repo-relative. Internal
      :class:`SymbolDef` values carry whatever POSIX string
      :func:`~trikon.change_intel.ast_indexer.index_file` produced — on
      the batch code path that path is absolute because
      :func:`~trikon.change_intel.ast_indexer.index_files` receives
      absolute :class:`Path` arguments. The public :class:`ImpactSet`
      contract is repo-relative POSIX (that's what the fixtures encode),
      so we strip the repo prefix here.
    * ``qualified_name`` has any ``.__init__.`` segment collapsed away.
      The AST indexer's module-path inference includes ``__init__`` as a
      component when it walks up ``__init__.py`` anchors, which produces
      names like ``orders.__init__.__all__``. Python callers refer to
      those symbols as ``orders.__all__``; the boundary shape follows
      the caller convention.

    Sorting here (rather than expecting the caller to do it) is
    intentional: every list field in the returned :class:`ImpactSet` is
    sorted for deterministic JSON, and centralizing the sort keeps drift
    between ``changed_symbols`` and ``impacted_public_apis`` impossible.
    """
    return [
        _to_public_ref(sym, repo_path=repo_path)
        for sym in sorted(symbols, key=lambda s: _normalize_qualified_name(s.qualified_name))
    ]


def _to_public_ref(sym: SymbolDef, *, repo_path: Path) -> SymbolRef:
    """Build a :class:`SymbolRef` with normalized path and qualified name.

    Extracted from :func:`_public_refs_sorted` so :func:`compute_impact`
    can reuse it without paying for the enclosing sort when it only
    needs one translated symbol.
    """
    return SymbolRef(
        qualified_name=_normalize_qualified_name(sym.qualified_name),
        file_path=_normalize_file_path(sym.file_path, repo_path=repo_path),
        kind=sym.kind,
    )


def _normalize_qualified_name(qualified_name: str) -> str:
    """Collapse ``.__init__.`` segments produced by the AST indexer.

    ``orders.__init__.__all__`` -> ``orders.__all__``. A qualified name
    that ends with ``.__init__`` (unlikely but possible for a symbol
    literally named ``__init__`` at package scope) is left alone; the
    replacement targets only the mid-path segment.
    """
    return qualified_name.replace(".__init__.", ".")


def _normalize_file_path(file_path: str, *, repo_path: Path) -> str:
    """Return ``file_path`` as a POSIX path relative to ``repo_path``.

    Falls back to the unmodified input when the path is already relative
    (no repo prefix to strip) or when it is somehow outside the repo
    (which should not happen in practice — the indexer only visits
    files the caller asked for, and the caller derives every path from
    the change set relative to ``repo_path``).
    """
    p = Path(file_path)
    if not p.is_absolute():
        return PurePosixPath(file_path).as_posix()
    try:
        rel = p.resolve().relative_to(repo_path.resolve())
    except ValueError:
        return PurePosixPath(file_path).as_posix()
    return rel.as_posix()


__all__ = [
    "BlastRadiusError",
    "BlastWeights",
    "bucket",
    "compute_impact",
    "enclosing_symbols",
]
