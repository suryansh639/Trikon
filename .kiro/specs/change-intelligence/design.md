# Change Intelligence — Design

> Phase 1 of the Trikon 12-week build. Turns a git change set into a structured
> `ImpactSet` (impacted symbols, modules, tests, blast-radius score). This is the
> first buildable subsystem and the input to every downstream stage: verification
> runner (Phase 2), policy engine (Phase 3), verdict emission (Phase 3).
>
> Status: design. No implementation yet. Requirements are frozen against
> `EXECUTION_PLAN.md §Phase 1` and `ARCHITECTURE.md §4.1`.

---

## Overview

### 1.1 What it does

Given a repo path and either `(base_sha, head_sha)` or a unified-diff string, Change Intelligence produces the `trikon.evidence.report.ImpactSet` Pydantic model:

```
parse_diff → ChangeSet
              │
              ▼
          ast_indexer.index_files (SQLite-cached, SHA-keyed)
              │
              ▼
          symbol_resolver.find_references (jedi)
              │
              ▼
          dep_graph.DepGraph.transitive_dependents
              │
              ▼
          blast_radius.compute_impact → ImpactSet
```

Every step has a single, well-typed entrypoint. Every step can fail; every failure raises a subclass of `ChangeIntelError` so the caller can never mistake a bug for a green verdict.

### 1.2 Goals (v0.1)

| # | Goal | Measured by |
| - | ---- | ----------- |
| G1 | Deterministic `ImpactSet` for any (repo, base, head) | Same inputs → byte-identical JSON output on 10 successive runs |
| G2 | Incremental indexing keyed by file SHA-256 | Warm 5-file re-index ≤ 500 ms on Django |
| G3 | Public API is fully type-hinted, mypy-strict clean | `mypy --strict trikon/change_intel/` exits 0 |
| G4 | ≥ 90 % branch coverage on `trikon/change_intel/**` | `coverage report --fail-under=90` |
| G5 | Never fail-open | Every uncaught error path in `sdk.verify` becomes `require_human` |
| G6 | Zero `dict[str, Any]` in the public surface | Verified by a lint rule in CI (`ruff` custom rule + mypy) |

### 1.3 Non-goals (v0.1)

- **Coverage-map-based test selection.** Phase 1 uses filename heuristics (`tests/test_foo.py` covers `foo.py`; same module path); Phase 2 upgrades to a real `coverage.py` map.
- **Dynamic dispatch resolution.** If `getattr(obj, name)()` targets a symbol, we do not chase it. We warn once in the report.
- **Multi-threading.** All stages are single-threaded. Concurrency is a v0.3 concern; the SQLite cache is opened with `check_same_thread=True`.
- **Non-Python source.** `.py` and `.pyi` only. Other extensions land in `ChangeSet.files` but are ignored by every downstream stage.
- **Patch generation.** We keep `libcst` output because we want the option in v1, but Phase 1 does not synthesize edits.
- **Streaming diffs.** The whole diff must fit in memory (bounded at 50 MB; larger raises `DiffInputError`).

---

## Architecture

Change Intelligence is a five-module pipeline. Each module has one public entry point and communicates with the next via frozen dataclasses in `models.py`. The dep graph is the only stateful component; everything else is pure functions over its data.

```
trikon/change_intel/
- diff_parser.py         Git diff -> ChangeSet
- ast_indexer.py         Python source -> [SymbolDef]  (SQLite-cached)
- symbol_resolver.py     SymbolDef -> [SymbolRefInternal]  (jedi)
- dep_graph.py           SQLite-backed uses-graph
- blast_radius.py        ChangeSet -> ImpactSet  (orchestrates)
- models.py              Frozen dataclasses (internal boundary)
- errors.py              Exception hierarchy (never fail-open)
```

The public boundary of the subsystem is one function: `blast_radius.compute_impact(change_set, repo_path, cache_db) -> ImpactSet`. Every consumer (SDK, CLI, MCP server) calls it and nothing else.

The stateful boundary is one SQLite file at `<repo>/.trikon/state.db`. See Data Models for the schema and section 4 for cache-invalidation rules.

---

## Components and Interfaces

Every public function's signature is fixed here. Deviations require a design-doc update.

### 2.1 `trikon/change_intel/diff_parser.py`

```python
def parse_diff(
    repo_path: Path,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
    *,
    max_diff_bytes: int = 50 * 1024 * 1024,
) -> ChangeSet:
    """Parse a git diff into a structured ChangeSet.

    Exactly one of {(base_sha, head_sha), diff} MUST be provided.
    Both provided ⇒ `diff` wins (agent-driven path).

    Raises
    ------
    DiffInputError       — arguments violate the "exactly one of" rule, or diff > max_diff_bytes
    RepoNotFoundError    — repo_path is not a git repository
    DiffParseError       — unified-diff content is malformed
    """
```

Contract:
- The returned `ChangeSet.files` is ordered by path (POSIX-sorted), so identical inputs produce byte-identical JSON.
- Renames are surfaced as one `FileChange(change_kind="renamed", old_path=..., path=...)` with hunks against the new path.
- Binary files appear in `ChangeSet.files` with empty `hunks` and are filtered by `.python_files`.

Implementation notes:
- SHA path: `gitpython` (`Repo(repo_path).git.diff(base_sha, head_sha, "--unified=0", "--find-renames")`) → feed to `unidiff.PatchSet`.
- Diff-string path: `unidiff.PatchSet.from_string(diff)`. No shell-out.

### 2.2 `trikon/change_intel/ast_indexer.py`

```python
def index_file(file_path: Path) -> list[SymbolDef]:
    """Index one .py file with libcst. Formatting-preserving; slower.

    Raises
    ------
    AstParseError — file is not valid Python
    """

def index_files(
    file_paths: list[Path],
    *,
    cache_db: Path,
) -> dict[Path, list[SymbolDef]]:
    """Index many files, hitting the SQLite cache for unchanged (path, sha256) pairs.

    - Files whose current SHA matches `symbols.file_sha` are read from cache.
    - Files whose SHA differs (or absent) are re-indexed via `index_file` and
      the cache row for that path is replaced atomically inside a single txn.
    - Files that fail to parse propagate `AstParseError` — the caller decides
      whether to blocklist them; we do NOT swallow parse errors.

    Ordering guarantee: the returned dict iterates in the order of `file_paths`.
    """

def fast_index_symbols(source: str) -> list[SymbolDef]:
    """Fast symbol pass using stdlib `ast`. Non-formatting-preserving.

    Used on the read-only hot path where we do not need round-trippable output.
    ~10× faster than `index_file` on typical inputs.

    Raises
    ------
    AstParseError — source is not valid Python
    """
```

Symbol kinds captured:
- `function` — top-level `def` / `async def`
- `class` — top-level `class`
- `method` — `def` / `async def` inside a class body (any nesting depth)
- `assignment` — module-level `NAME = ...` or `NAME: T = ...`

Not captured in v0.1: lambdas, nested functions, comprehensions, `TypeAlias`.

Qualified name format: `package.module.Class.method`, `package.module.func`, `package.module.NAME`. Module path is derived from `file_path` relative to the nearest `__init__.py`-anchored root.

`is_public` heuristic: no component of the qualified name starts with `_` (excluding `__init__` and dunder methods on classes, which stay public).

### 2.3 `trikon/change_intel/symbol_resolver.py`

```python
def find_references(
    symbol: SymbolDef,
    repo_path: Path,
    *,
    cache_db: Path,
    project: jedi.Project | None = None,
) -> list[SymbolRefInternal]:
    """Find every reference to `symbol` inside `repo_path`.

    Uses jedi's Project + Script APIs. Results are cached by
    (symbol.qualified_name, symbol.file_sha) in the same SQLite database.

    Contract
    --------
    - Deterministic ordering: refs sorted by (referring_file, referring_line).
    - Never raises SymbolResolutionError to the caller for a *single unresolvable
      reference*. Individual jedi failures are logged and skipped.
    - Raises SymbolResolutionError only when the jedi Project itself cannot be
      constructed (missing virtualenv, corrupt sys.path).
    """
```

`SymbolRefInternal` is defined in `models.py` (see §3). It intentionally does **not** overlap with `evidence.report.SymbolRef`, which is the public boundary shape.

### 2.4 `trikon/change_intel/dep_graph.py`

```python
class DepGraph:
    """SQLite-backed directed graph of symbol → symbol "uses" edges.

    The connection is opened lazily on the first mutating call. Concurrent
    writes from multiple processes are serialized by SQLite's default file
    lock; that is fine for the CLI use case, which is always single-process.

    All methods below raise `DepGraphError` on I/O or integrity failures.
    """

    def __init__(self, db_path: Path) -> None: ...

    def upsert_file(self, file_path: Path, file_sha: str) -> None:
        """Record that we saw file_path at file_sha. Called before upsert_symbols."""

    def upsert_symbols(
        self,
        file_path: Path,
        file_sha: str,
        symbols: list[SymbolDef],
    ) -> None:
        """Replace all rows in `symbols` for (file_path, file_sha) atomically.

        Old rows for the same file_path with a *different* file_sha are deleted
        (SHA change ⇒ stale symbols removed). Rows for other files are untouched.
        """

    def upsert_edges(
        self,
        source: SymbolDef,
        targets: list[SymbolDef],
        *,
        kind: str = "uses",
    ) -> None:
        """Record that `source` uses each of `targets`."""

    def transitive_dependents(
        self,
        seeds: list[SymbolDef],
        *,
        max_hops: int = 5,
    ) -> list[SymbolDef]:
        """Return every symbol reachable from `seeds` via the *reverse* edge
        set within `max_hops` BFS layers.

        Ordering: BFS-order (hop 0 first), ties broken by qualified_name.
        max_hops <= 0 raises DepGraphError.
        """

    def file_needs_reindex(self, file_path: Path, current_sha: str) -> bool:
        """True when the stored file_sha differs from `current_sha`, OR the file
        is unknown to the graph."""

    def prune_files(self, keep_paths: set[Path]) -> int:
        """Delete rows for paths not in `keep_paths`. Returns rows deleted.

        Called on cold-index passes to keep the cache from bloating when files
        are deleted or renamed. Not called on the hot path.
        """

    def close(self) -> None: ...
    def __enter__(self) -> "DepGraph": ...
    def __exit__(self, *exc: object) -> None: ...
```

### 2.5 `trikon/change_intel/blast_radius.py`

```python
@dataclass(frozen=True)
class BlastWeights:
    impacted_modules: float = 1.0
    impacted_public_apis: float = 3.0
    impacted_test_files: float = 0.5
    cross_package_hops: float = 2.0
    sensitive_path_touch: float = 5.0

    # Configuration knobs
    low_bucket_max: float = 5.0
    medium_bucket_max: float = 15.0

    # Sensitive-path globs (POSIX). Callers override per-repo via policy.
    sensitive_paths: tuple[str, ...] = ("payments/**", "auth/**", "billing/**")


def compute_impact(
    change_set: ChangeSet,
    repo_path: Path,
    *,
    cache_db: Path,
    weights: BlastWeights | None = None,
    max_hops: int = 5,
) -> ImpactSet:
    """Given a ChangeSet, produce the full ImpactSet.

    Steps:
        1. index_files on all changed .py files (populates cache + graph).
        2. For each hunk, map changed lines → enclosing SymbolDef via
           `enclosing_symbols` (byte-range containment).
        3. Query DepGraph.transitive_dependents for the enclosing symbols.
        4. Union into impacted_modules (module of each impacted symbol) and
           impacted_public_apis (`is_public=True` subset).
        5. Filename-heuristic test selection (v0.1): for each impacted module
           `pkg.mod`, look for `tests/test_mod.py`, `tests/{pkg}/test_mod.py`,
           and any test file that itself was modified.
        6. Compute numeric score = weighted sum; `bucket(score)`.

    Raises
    ------
    BlastRadiusError — any downstream call fails; the original error is chained.
    """


def bucket(score: float, *, weights: BlastWeights | None = None) -> BlastBucket:
    """Total function on floats. Returns "LOW" | "MEDIUM" | "HIGH"."""


def enclosing_symbols(
    file_path: Path,
    changed_lines: Iterable[int],
    symbols: list[SymbolDef],
) -> list[SymbolDef]:
    """Return every SymbolDef whose [start_line, end_line] interval intersects
    any of `changed_lines`. Deterministic order: by (start_line, qualified_name).
    """
```

The public `ImpactSet` and `SymbolRef` shapes come from `trikon/evidence/report.py` unchanged. `blast_radius.compute_impact` is the only place internal `SymbolDef` gets translated into public `SymbolRef`:

```python
def _to_public(sym: SymbolDef) -> SymbolRef:
    return SymbolRef(
        qualified_name=sym.qualified_name,
        file_path=sym.file_path,
        kind=sym.kind,  # "function" | "class" | "method" | "assignment"
    )
```

---

## Data Models

New file. Everything internal is a **frozen dataclass** (immutable, hashable, cheap to construct). Public types stay in `trikon/evidence/report.py` as Pydantic models. This split matters because internal models cross hot paths (millions of instantiations on a big repo) and Pydantic v2 validation, while fast, is still an order of magnitude slower than dataclasses.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ChangeKind = Literal["added", "modified", "deleted", "renamed"]
SymbolKind = Literal["function", "class", "method", "assignment"]
RefKind    = Literal["call", "import", "attribute_access"]


@dataclass(frozen=True, slots=True)
class Hunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    added_lines: tuple[int, ...] = field(default_factory=tuple)
    removed_lines: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str                       # POSIX, relative to repo_path
    change_kind: ChangeKind
    old_path: str | None            # set iff change_kind == "renamed"
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True, slots=True)
class ChangeSet:
    repo_path: Path
    base_sha: str | None
    head_sha: str | None
    files: tuple[FileChange, ...]

    @property
    def python_files(self) -> tuple[FileChange, ...]:
        return tuple(f for f in self.files if f.path.endswith((".py", ".pyi")))


@dataclass(frozen=True, slots=True)
class SymbolDef:
    qualified_name: str             # "pkg.mod.Class.method"
    kind: SymbolKind
    file_path: str                  # POSIX, relative to repo_path
    file_sha: str                   # sha256 of the file at index time
    start_line: int                 # 1-based, inclusive
    end_line: int                   # 1-based, inclusive
    start_byte: int
    end_byte: int
    is_public: bool


@dataclass(frozen=True, slots=True)
class SymbolRefInternal:
    """Internal reference model. Distinct from evidence.report.SymbolRef,
    which is the public boundary."""
    target_qualified_name: str
    target_file_path: str
    referring_file: str
    referring_line: int
    kind: RefKind
```

Reuse from `trikon/evidence/report.py` (unchanged in Phase 1):

- `ImpactSet` — Pydantic, boundary output of `compute_impact`.
- `SymbolRef` — Pydantic, embedded in `ImpactSet`.
- `BlastBucket` — the `Literal["LOW", "MEDIUM", "HIGH"]` alias.

---

## 4. SQLite schema (`<repo>/.trikon/state.db`)

Single database file. Same file will host the coverage map in Phase 2; namespacing is by table prefix, not by file.

```sql
-- =========================================================================
-- Meta / versioning.
-- =========================================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Populated on first init:
--   ('schema_version', '1')
--   ('created_at',     ISO-8601 UTC)
--   ('trikon_version', trikon.__version__)


-- =========================================================================
-- File index. One row per file path we have ever seen.
-- =========================================================================
CREATE TABLE IF NOT EXISTS file_index (
    path        TEXT PRIMARY KEY,          -- POSIX relative path
    file_sha    TEXT NOT NULL,             -- hex sha256 of the file's bytes
    indexed_at  TEXT NOT NULL,             -- ISO-8601 UTC
    byte_size   INTEGER NOT NULL
);


-- =========================================================================
-- Symbol table. One row per symbol per (path, file_sha) pair.
-- =========================================================================
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
);
CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_qname     ON symbols(qualified_name);


-- =========================================================================
-- Edges. "source uses target". Undirected traversal is done by JOIN.
-- =========================================================================
CREATE TABLE IF NOT EXISTS edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'uses',
    PRIMARY KEY (source_id, target_id, kind),
    FOREIGN KEY (source_id) REFERENCES symbols(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);


-- =========================================================================
-- Pragmas set on every connection open.
-- =========================================================================
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store   = MEMORY;
```

### 4.1 Cache invalidation strategy

- The **row identity** of a symbol is `(qualified_name, file_sha)`. If the file changes, `file_sha` changes, so the new symbol row is a *different* row, not an update.
- On every `upsert_symbols(file_path, file_sha, symbols)`:
  1. `INSERT OR REPLACE INTO file_index (path, file_sha, indexed_at, byte_size)`
  2. `DELETE FROM symbols WHERE file_path = ? AND file_sha != ?` — evict stale symbols for this file
  3. Bulk `INSERT OR IGNORE INTO symbols` — new rows only, because `(qualified_name, file_sha)` is unique
  4. `ON DELETE CASCADE` on the FKs means edge rows tied to evicted symbols are auto-removed
- `file_needs_reindex(path, sha)` is a single-row lookup: `SELECT 1 FROM file_index WHERE path=? AND file_sha=?`. Absent ⇒ reindex; sha differs ⇒ reindex.
- `prune_files(keep_paths)` is only called from the cold-index pass. It deletes `file_index` rows outside `keep_paths`, and the FK cascade removes their symbols and edges.

### 4.2 Migration

Schema is versioned via `schema_meta.schema_version`. On open:
- If the row is missing, we create tables from scratch and set version = 1.
- If the version is < CURRENT, we run migration scripts under `trikon/change_intel/migrations/`. Phase 1 ships with none.
- If the version is > CURRENT (user downgraded Trikon), we raise `DepGraphError("state.db was written by a newer Trikon; upgrade or delete it")`.

---

## Error Handling

New file. One base class, one subclass per module. Every raise in `change_intel/**` uses one of these. Nothing raises bare `ValueError` / `RuntimeError` / `sqlite3.Error` past the module boundary.

```python
class ChangeIntelError(Exception):
    """Base for every error raised by trikon.change_intel."""

class DiffInputError(ChangeIntelError):
    """Bad arguments to parse_diff (both/neither of {SHAs, diff}, oversized diff)."""

class RepoNotFoundError(ChangeIntelError):
    """repo_path is not a git repository."""

class DiffParseError(ChangeIntelError):
    """Unified diff content is malformed."""

class AstParseError(ChangeIntelError):
    """A Python source file failed to parse.

    Attributes: file_path: str, line: int | None
    """

class SymbolResolutionError(ChangeIntelError):
    """jedi project could not be constructed for the repo."""

class DepGraphError(ChangeIntelError):
    """SQLite operation failed, schema drift, or invalid graph query."""

class BlastRadiusError(ChangeIntelError):
    """compute_impact failed; wraps the underlying ChangeIntelError."""
```

### 5.1 Never fail-open

The SDK boundary (`trikon.sdk.verify`) wraps every call into `change_intel` in a single `try / except ChangeIntelError` block. Any hit produces:

```python
Verdict(
    decision="require_human",
    reason=f"Change intelligence failed: {type(e).__name__}: {e}",
    matched_rule=None,
    evidence=Evidence(
        change=EMPTY_IMPACT_SET,        # sentinel; not "unknown-is-OK"
        verification=EMPTY_VERIFICATION,
        policy_results=[],
    ),
    audit_id=uuid4(),
    created_at=datetime.now(UTC),
)
```

Note the sentinel `EMPTY_IMPACT_SET` — an `ImpactSet` with zero symbols and `blast_radius_score="HIGH"`. `HIGH`, not `LOW`, because "we don't know what changed" must never look like "nothing changed".

The invariant, encoded as an integration test: **for every subclass of `ChangeIntelError` raised anywhere on the change-intel path, `sdk.verify` returns a `Verdict` with `decision == "require_human"`. `allow` is never emitted on this path.**

---

## 6. Dependencies

Confirmed pins in `pyproject.toml` (all already listed at compatible major versions):

| Package | Pin | Use |
| ------- | --- | --- |
| `gitpython` | `>=3.1,<4` | SHA-range → diff. `Repo.git.diff` shell-out. |
| `unidiff` | `>=0.7.5,<1` | Parse unified-diff strings. **Add**: not currently pinned. |
| `libcst` | `>=1.5,<2` | Formatting-preserving symbol index (cold path). |
| `jedi` | `>=0.19,<0.20` | Cross-file reference finder. |
| `pydantic` | `>=2.9,<3` | `ImpactSet` / `SymbolRef` (already listed). |
| `pyyaml` | `>=6,<7` | Not used here; loaded by policy engine. |

Dev-only:

| Package | Pin | Use |
| ------- | --- | --- |
| `hypothesis` | `>=6.100,<7` | Property tests for §2 signatures. **Add**: not currently pinned. |
| `pytest` | `>=8,<9` | Already listed. |
| `pytest-cov` | `>=6,<7` | Already listed. |
| `ruff` | `>=0.7,<1` | Already listed. |
| `mypy` | `>=1.13,<2` | Already listed. |

Two additions land in `pyproject.toml` in this phase: `unidiff` (runtime) and `hypothesis` (dev-optional).

---

## 7. Performance targets

Concrete, measurable, and gated by the benchmark suite in `tests/benchmarks/`. All measured on a Ryzen-7-tier laptop with NVMe SSD (representative dev machine).

| Operation | Target | Fail-CI-if |
| --------- | ------ | ---------- |
| Cold-index Django (`django/django@main`, ~250K LOC) | ≤ 30 s | > 45 s |
| Warm-index 5 changed files after cold pass | ≤ 500 ms | > 1000 ms |
| `compute_impact` on a 3-file change (sample_repo) | ≤ 200 ms | > 400 ms |
| `DepGraph.transitive_dependents` at 5 hops on Django graph | ≤ 100 ms p95 | > 200 ms |
| SQLite `state.db` size after full Django index | ≤ 60 MB | > 100 MB |
| Memory RSS during cold Django index | ≤ 800 MB peak | > 1500 MB |

Enforcement path:
- `tests/benchmarks/test_perf.py` uses `pytest-benchmark` with `--benchmark-max-time=60` and stores baselines in `tests/benchmarks/.benchmarks/`.
- CI job `perf` runs on every PR to `main`. Fails on regression > 20 % against the previous baseline.
- Django clone is cached via `actions/cache` keyed by `django@sha`.

Design levers if targets slip:
1. Move `symbol_resolver` off jedi for the hot cases (import graph only, no attribute chains). Jedi is the dominant cost.
2. Precompile the SQLite statements once per `DepGraph` instance (`sqlite3.Connection.cursor().executemany` with prepared strings).
3. Fall back to `fast_index_symbols` (stdlib `ast`) universally; keep `libcst` only for the eventual patch-generation path.

If all three levers land and we still miss on Django, the target is wrong, not the code — we revise this table.

---

## Testing Strategy

### 8.1 Layout

```
tests/
├── unit/
│   └── change_intel/
│       ├── test_diff_parser.py
│       ├── test_ast_indexer.py
│       ├── test_symbol_resolver.py
│       ├── test_dep_graph.py
│       ├── test_blast_radius.py
│       └── test_models.py
├── integration/
│   └── change_intel/
│       ├── test_end_to_end_sample_repo.py
│       └── test_django_smoke.py         # skipped unless TRIKON_DJANGO_PATH set
└── benchmarks/
    └── test_perf.py
```

### 8.2 `examples/sample_repo/`

A ~30-file Python project committed inside this repo, structured to exercise every stage:

```
examples/sample_repo/
├── .trikon/policy.yaml
├── pyproject.toml
├── src/
│   ├── payments/
│   │   ├── __init__.py
│   │   ├── gateway.py      # public API surface
│   │   └── retry.py
│   ├── orders/
│   │   ├── __init__.py
│   │   └── worker.py       # transitively uses payments.gateway
│   └── api/
│       ├── __init__.py
│       └── payments.py
└── tests/
    ├── test_retry.py
    ├── test_gateway.py
    ├── test_worker.py
    └── api/
        └── test_payments.py
```

Seeded scenarios, each recorded as a git branch inside a submodule (or synthesized at test time via `dulwich`):

| Scenario | Base → Head | Expected verdict shape |
| -------- | ----------- | ---------------------- |
| `clean_refactor` | rename an internal helper in `orders.worker` | 1 impacted module, 1 impacted test, LOW |
| `bad_retry` | change `payments.retry.backoff` timing so `test_worker` fails | 3 impacted modules, 2 impacted public APIs, MEDIUM |
| `sensitive_touch` | modify `src/payments/gateway.py::charge` | 4 impacted modules, HIGH (sensitive-path weight) |
| `no_python_change` | touch `README.md` only | zero impacted symbols, `changed_files=["README.md"]` |
| `deleted_file` | delete `orders.worker` and remove callers | 2 impacted modules, MEDIUM |

Each scenario has assertions on the exact `ImpactSet` — not fuzzy "contains something".

### 8.3 Unit tests — property-based

`hypothesis` strategies live in `tests/unit/change_intel/strategies.py`:

- `python_source()` — generates valid Python (short programs, single class + methods) via a hand-rolled AST-emitting strategy.
- `changesets()` — generates `ChangeSet` values with arbitrary hunk placement inside a fixture repo.
- `dep_graphs()` — generates random DAGs of up to N symbols for `DepGraph` model-based tests.

Property tests keyed to Correctness Properties §11. Minimum 100 iterations each (`@settings(max_examples=100, deadline=1000)`).

### 8.4 Coverage gate

- `coverage report --fail-under=90` on `trikon/change_intel/**` in CI.
- Branch coverage on (not line coverage) — set in `pyproject.toml` `[tool.coverage.run] branch = true`.
- `# pragma: no cover` allowed only on `if TYPE_CHECKING:` blocks and `__enter__/__exit__` protocol methods that just delegate.

### 8.5 Integration tests

- `test_end_to_end_sample_repo.py` — runs `sdk.verify` end-to-end against every scenario above. This is the "does the whole pipeline light up" test.
- `test_django_smoke.py` — clones (or reads from `TRIKON_DJANGO_PATH`) Django, indexes cold, runs `compute_impact` on 5 known historical PRs, asserts sensible impact sets (defined as: the file that owned the actual bug fix appears in `changed_symbols`). Skipped unless the env var is set; runs in the nightly job, not per-PR.

---

## 9. CI + tooling baselines

These ship *alongside* the Phase 1 code — the design-of-done includes green CI.

### 9.1 `.github/workflows/ci.yml`

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  lint-type-test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
      - run: pip install -e ".[dev]"
      - run: ruff check trikon/ tests/
      - run: ruff format --check trikon/ tests/
      - run: mypy --strict trikon/change_intel/ trikon/evidence/
      - run: pytest tests/unit/ --cov=trikon.change_intel --cov-branch --cov-report=xml
      - run: coverage report --fail-under=90 --include='trikon/change_intel/*'

  integration:
    runs-on: ubuntu-latest
    needs: lint-type-test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest tests/integration/ -v

  perf:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    needs: lint-type-test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: /tmp/django
          key: django-mainline-2026-09
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest tests/benchmarks/ --benchmark-only --benchmark-max-time=60
```

### 9.2 `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks:
      - id: mypy
        files: ^trikon/change_intel/
        args: [--strict]
        additional_dependencies: [pydantic, types-PyYAML]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-yaml
      - id: check-added-large-files
        args: [--maxkb=500]
      - id: end-of-file-fixer
      - id: trailing-whitespace
```

### 9.3 `pyproject.toml` additions

```toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_unused_ignores = true
disallow_any_explicit = true      # no `Any` unless justified by inline noqa
plugins = ["pydantic.mypy"]

[tool.mypy.overrides]
module = ["jedi.*", "libcst.*", "unidiff.*", "git.*"]
ignore_missing_imports = true

[tool.coverage.run]
branch = true
source = ["trikon"]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

---

## 10. Definition of Done

Every item measurable. No "should" verbs.

- [x] `parse_diff`, `index_files`, `find_references`, `DepGraph.*`, `compute_impact`, `bucket`, `enclosing_symbols` fully implemented — no `NotImplementedError` in `trikon/change_intel/**`.
- [x] `trikon/change_intel/models.py` and `trikon/change_intel/errors.py` created and exported from `trikon/change_intel/__init__.py`.
- [x] `examples/sample_repo/` committed with 5 scenarios and their expected-impact-set fixtures under `tests/fixtures/expected_impact/`.
- [x] `mypy --strict trikon/change_intel/ trikon/evidence/` exits 0.
- [x] `ruff check trikon/ tests/` and `ruff format --check trikon/ tests/` exit 0.
- [x] `pytest tests/unit/change_intel/ tests/integration/change_intel/` — all green.
- [x] `coverage report --fail-under=90 --include='trikon/change_intel/*'` passes.
- [x] `trikon debug impact --repo examples/sample_repo --base HEAD~1 --head HEAD` prints a valid `ImpactSet` JSON matching the fixture for that scenario.
- [x] Perf benchmarks documented and green:
    - Cold Django index ≤ 30 s
    - Warm 5-file re-index ≤ 500 ms
    - `compute_impact` on 3-file change ≤ 200 ms
    - `transitive_dependents` p95 ≤ 100 ms
- [x] `.github/workflows/ci.yml`, `.pre-commit-config.yaml` committed and passing on `main`.
- [x] `docs/change_intel.md` — a 1-page prose walkthrough with a worked example, linked from `README.md`.
- [x] Phase 1 checkboxes in `EXECUTION_PLAN.md` §Phase 1 flipped to `[x]`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Numbered acceptance criteria referenced below come from `EXECUTION_PLAN.md §Phase 1` and `ARCHITECTURE.md §4.1`. The mapping to criterion numbers follows the prework analysis stored in context.

### Property 1: Diff completeness

*For any* repository `R` and commit pair `(base, head)` where the git diff modifies file set `F` with line set `L(f)` per file, `parse_diff(R, base, head)` returns a `ChangeSet` whose `files` contains one `FileChange` per `f ∈ F` and whose union of hunk added/removed line indices equals `L(f)`.

**Validates: Requirements 1.1**

### Property 2: Diff-input equivalence

*For any* repository `R` and commit pair `(base, head)`, `parse_diff(R, base_sha=base, head_sha=head)` and `parse_diff(R, diff=git_diff_string(R, base, head))` produce `ChangeSet` values that are equal on the `files` tuple (ignoring the `base_sha` / `head_sha` fields, which are `None` in the diff-string path).

**Validates: Requirements 1.2**

### Property 3: Symbol extraction soundness

*For any* syntactically-valid Python source `S` at path `p`, for every `SymbolDef` `d` returned by `index_file(p)`, the byte slice `S[d.start_byte : d.end_byte]` parses to an AST node whose kind is `d.kind` and whose fully-qualified name is `d.qualified_name`.

**Validates: Requirements 2.1**

### Property 4: Incremental-index idempotence and cache-hit

*For any* file `f` and SQLite cache `db`, `index_files([f], cache_db=db)` called twice in succession returns byte-identical `SymbolDef` lists; further, on the second call, the underlying `libcst` parser is never invoked (verified by monkeypatched sentinel that raises on entry).

**Validates: Requirements 2.2**

### Property 5: Reference finding soundness and completeness

*For any* synthetic repository `R` with a known reference graph `G_true` where symbol `s` is referenced from a fixed set of locations `Refs(s)`, `find_references(s, R)` returns a list whose set equals `Refs(s)` (no missing refs, no spurious refs) modulo references from within the definition itself.

**Validates: Requirements 3.1**

### Property 6: Reference-resolver no-crash invariant

*For any* repository `R` (including malformed inputs with syntactically-valid but semantically-broken imports), `find_references(s, R)` terminates without raising, unless the jedi `Project` itself fails to construct — in which case a `SymbolResolutionError` is raised at the entry point, never mid-iteration.

**Validates: Requirements 3.2**

### Property 7: Dep-graph round-trip

*For any* file path `p`, file SHA `h`, and list of `SymbolDef` values `xs` whose `file_path == p` and `file_sha == h`, `DepGraph.upsert_symbols(p, h, xs)` followed by a query for all symbols at `(p, h)` returns a set equal to `set(xs)`.

**Validates: Requirements 4.1**

### Property 8: Transitive-dependents correctness (model-based)

*For any* randomly generated DAG of symbols `G` with edge set `E`, seed set `S ⊆ Nodes(G)`, and `max_hops = k`, `DepGraph.transitive_dependents(S, max_hops=k)` returns a set equal to the set of nodes reachable from `S` via reverse-edge BFS within `k` layers, computed by a reference implementation (`networkx.descendants_at_distance` capped at `k`).

**Validates: Requirements 4.2**

### Property 9: File-SHA cache invariant

*For any* file path `p` and current SHA `h_c`, `DepGraph.file_needs_reindex(p, h_c)` returns `True` if and only if `p` is absent from `file_index` OR the stored `file_sha` differs from `h_c`.

**Validates: Requirements 4.3**

### Property 10: Impact soundness on changed symbols

*For any* `ChangeSet` `C` and repository `R`, every `SymbolDef` `s` in `R` whose `[start_line, end_line]` interval intersects the set of lines added or removed by any hunk in `C.files` appears in `compute_impact(C, R).changed_symbols` (mapped to public `SymbolRef`).

**Validates: Requirements 5.1**

### Property 11: Score-bucket monotonicity

*For any* two scores `x` and `y` with `x <= y`, `bucket(x)` orders no higher than `bucket(y)` under the ranking `LOW < MEDIUM < HIGH`; equivalently, `bucket` is monotone non-decreasing on the reals.

**Validates: Requirements 5.2**

### Property 12: Sensitive-path weight monotonicity

*For any* `ChangeSet` `C` and `BlastWeights` `W`, the numeric score of a change set `C ∪ (touch of a sensitive path)` is at least `score(C) + W.sensitive_path_touch` (measured before bucket saturation; equal at the HIGH ceiling).

**Validates: Requirements 5.3**

### Property 13: Error-hierarchy closure

*For any* raise site within `trikon/change_intel/**` (enumerated by AST scan of the module tree), the exception class raised is a subclass of `ChangeIntelError`. No bare `Exception`, `ValueError`, `RuntimeError`, or `sqlite3.Error` escapes the module boundary.

**Validates: Requirements 6.1**

---

## 12. Rollback plan — SQLite schema mistakes

The dep-graph schema is the highest-risk piece of Phase 1 because it persists to user machines. If we ship v0.1 with a wrong shape (missing index, wrong column type, unhandled cascade), rollback must be automatic and non-destructive.

### 12.1 Detection

- On every `DepGraph.__init__`, we read `schema_meta.schema_version` and compare to `CURRENT_SCHEMA_VERSION = 1`.
- If the on-disk version is `<` current, we run migrations under `trikon/change_intel/migrations/vN__description.sql` sequentially inside a single transaction. Failure rolls the transaction back and raises `DepGraphError`.
- If the on-disk version is `>` current, we raise immediately with a clear message. No auto-downgrade.
- If any migration fails partway, the connection is closed, the caller sees a `DepGraphError`, and the next section applies.

### 12.2 Reversal

The cache is **derivable** — it is a materialization of git state, nothing else. That property is the entire rollback plan.

Three-tier reversal, in order of aggressiveness:

1. **Auto-rebuild.** If `DepGraph.__init__` raises `DepGraphError("schema mismatch")`, the SDK's caller (`sdk.verify`) catches, moves `state.db` to `state.db.corrupt.{ISO8601}`, and re-invokes with a fresh DB. The user sees a one-line warning: `"Trikon dep-graph cache reset (was: schema_version=1, is: 2). Cold re-index on next verify."` No verdict change.
2. **Manual nuke.** `trikon debug reset-cache` command deletes `.trikon/state.db` and any `.corrupt.*` siblings. Documented in `docs/troubleshooting.md`.
3. **Version pin.** `pyproject.toml` records `trikon.change_intel.__cache_schema__ = 1`. If a customer needs to stay on the old schema (unlikely but possible for a Phase-2 preview that regressed), we ship a `pip install trikon==0.1.<last>` back-out, and the schema-version check on their existing `state.db` will still pass because we do not delete migrations for older versions.

### 12.3 What we will not do

- We will not add ad-hoc `ALTER TABLE` migrations without bumping `schema_version`. Every schema change is a new numbered migration file. This is a hard rule.
- We will not attempt to migrate a corrupt or foreign-written `state.db`. The rebuild path is cheap enough (Django cold-index is 30 s) that anything smarter than "delete and re-index" is not worth the risk.
- We will not persist anything that cannot be regenerated. If a future field requires user input to recompute (e.g., a manually-tagged sensitive path), it lives in `.trikon/policy.yaml`, not in `state.db`.

---

## 13. Open design questions (defer to task phase)

Written down here so we do not have to remember them. None block starting Phase 1.

1. **Fixture-heavy pytest test selection.** Filename heuristic is the v0.1 answer. Phase 2's coverage-map path may need to fork `pytest-impacted` for fixture-aware selection. Decision by end of week 3.
2. **Rename detection accuracy.** `gitpython` uses `--find-renames` at the default 50 % threshold. That misses aggressive refactors. If sample_repo's `deleted_file` scenario shows this hurting impact accuracy, bump the threshold in `parse_diff` to 30 %.
3. **Large-file exclusion.** Generated files, migrations, and vendored deps can bloat the index. Phase 1 defers this to policy (`.trikon/policy.yaml` `exclude_paths`), applied at `parse_diff` time. If real repos push size over the 800 MB RSS target, we add an in-code default exclusion (e.g., skip files > 100 KB) with a config override.
4. **`libcst` vs stdlib `ast` on the cold path.** The design says libcst for `index_file`, ast for `fast_index_symbols`. If profiling shows libcst is > 5× stdlib for cold Django, we flip the default and keep libcst only for the eventual patch-generation path. Decide after benchmark run in week 3.
