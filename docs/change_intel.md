# Change Intelligence — Engineer's Walkthrough

Change Intelligence is Phase 1 of Trikon. It turns a git change set into a structured `ImpactSet` — impacted symbols, modules, tests, and a numeric blast-radius score. Every later Trikon phase (verification runner, policy engine, verdict emission) consumes its output.

For the frozen contract, see [`../.kiro/specs/change-intelligence/design.md`](../.kiro/specs/change-intelligence/design.md). This page is the guided tour.

## The pipeline

```
parse_diff → index_files → find_references → DepGraph.transitive_dependents → compute_impact → ImpactSet
 (diff_parser) (ast_indexer) (symbol_resolver)      (dep_graph)              (blast_radius)
```

Five modules, each with one public entry point, communicating via frozen dataclasses in `trikon/change_intel/models.py`. `compute_impact` orchestrates the rest; every other consumer (SDK, CLI, MCP tool) calls it and nothing else.

## Running it on a real repo

The `trikon debug impact` command exercises the whole pipeline and prints the resulting `ImpactSet`:

```bash
trikon debug impact --repo examples/sample_repo --base HEAD~1 --head HEAD
```

Applied against the `clean_refactor` scenario (see below), the output is:

```json
{
  "changed_files": ["src/orders/worker.py"],
  "changed_symbols": [
    {"qualified_name": "orders.worker.PaymentWorker.process", "file_path": "src/orders/worker.py", "kind": "method"},
    {"qualified_name": "orders.worker._time_since",           "file_path": "src/orders/worker.py", "kind": "function"}
  ],
  "impacted_modules": ["orders"],
  "impacted_public_apis": [],
  "impacted_tests": ["tests/test_worker.py"],
  "blast_radius_score": "LOW",
  "blast_radius_numeric": 1.5
}
```

Full expected outputs for every scenario live in [`../tests/fixtures/expected_impact/`](../tests/fixtures/expected_impact/).

## The five modules

- **`diff_parser.py`** — `parse_diff(repo, base_sha, head_sha)` (or a raw diff string) → `ChangeSet`. `gitpython` shells out for the SHA path, `unidiff` parses in-memory. Renames are surfaced as `change_kind="renamed"`.
- **`ast_indexer.py`** — Two paths. `fast_index_symbols(source)` uses stdlib `ast` for hot reads. `index_files(paths, cache_db=...)` uses `libcst` and caches results in SQLite keyed by `(path, sha256)`; unchanged files skip re-parse.
- **`symbol_resolver.py`** — `find_references(symbol, repo)` uses `jedi` to locate call sites across the tree. Per-reference failures are logged and skipped; only a broken `jedi.Project` raises.
- **`dep_graph.py`** — `DepGraph` wraps `<repo>/.trikon/state.db`. Symbol rows are identified by `(qualified_name, file_sha)`, so a SHA bump evicts stale symbols via `ON DELETE CASCADE`. `transitive_dependents(seeds, max_hops)` is a reverse-edge BFS.
- **`blast_radius.py`** — `compute_impact(change_set, repo, cache_db=...)` runs the six-step orchestration and returns the `ImpactSet`. The scoring weights and bucket thresholds live on `BlastWeights`.

## Phase 1 vs future phases

Phase 1 wires change-intelligence only. Specifically:

- `sdk.verify` always returns `decision="require_human"` because there is no verification evidence to grade and no policy to consult yet.
- Dep-graph edges are **not** auto-populated during `compute_impact`. On a fresh cache the transitive fanout is empty; the seed set drives the whole impact. The docstring on `compute_impact` calls this out.
- No verification runner, no policy engine, no `trikon verify` CLI. Those land in Phases 2 and 3 — see [`../EXECUTION_PLAN.md`](../EXECUTION_PLAN.md).

## Never fail-open

Every exception raised inside `trikon/change_intel/**` is a subclass of `ChangeIntelError`. The SDK boundary catches the base class and returns a `Verdict` backed by the module-level sentinel `EMPTY_IMPACT_SET` — zero symbols, `blast_radius_score="HIGH"`. `HIGH`, not `LOW`, because "we don't know what changed" must never look like "nothing changed". The closure invariant is asserted in `tests/integration/change_intel/test_never_fail_open.py`.

## The five sample scenarios

Each scenario is a unified-diff patch against [`../examples/sample_repo/`](../examples/sample_repo/), stored in [`../tests/fixtures/scenarios/`](../tests/fixtures/scenarios/) with its expected `ImpactSet` in [`../tests/fixtures/expected_impact/`](../tests/fixtures/expected_impact/):

- **`clean_refactor`** — extracts a private helper in `orders.worker`. `LOW`.
- **`bad_retry`** — retimes `payments.retry.with_backoff`; fans out to the whole payments/orders/api graph. `HIGH` (also on sensitive path).
- **`sensitive_touch`** — adds a `currency` field to `payments.gateway.charge` and `ChargeResult`. `HIGH`.
- **`no_python_change`** — appends to `README.md` only. `LOW` with zero impacted symbols.
- **`deleted_file`** — removes `src/orders/worker.py` and its `orders/__init__.py` export. `MEDIUM`.

## Where to go from here

- [`../.kiro/specs/change-intelligence/design.md`](../.kiro/specs/change-intelligence/design.md) — the frozen spec (data models, SQLite schema, correctness properties).
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the broader Trikon architecture and how Phase 1 fits into it.
- [`../tests/unit/change_intel/`](../tests/unit/change_intel/) and [`../tests/integration/change_intel/`](../tests/integration/change_intel/) — concrete usage examples.
