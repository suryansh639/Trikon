# Implementation Plan: Change Intelligence (Trikon Phase 1)

## Overview

Convert the frozen design in `design.md` into buildable, incremental coding tasks. Each task ships with types, tests, and mypy-strict cleanliness at that step so the tree is green after every merge.

The build order is: types → sample repo → DepGraph → AST indexers → diff parser → symbol resolver → blast-radius orchestrator → SDK + CLI → integration tests → CI + perf gates → docs → finalize.

Every task cites the requirements it validates (granular sub-clauses) and the exact files it creates or modifies. Test sub-tasks are postfixed with `*` per the Kiro convention; they may be skipped for a fast MVP but are required to hit the Definition of Done in `design.md §10`.

## Tasks

- [x] 1. Establish the change-intel type foundation

  - [x] 1.1 Write `trikon/change_intel/errors.py`
    - Implement the 7-class exception hierarchy from `design.md §Error Handling`: `ChangeIntelError` (base), `DiffInputError`, `RepoNotFoundError`, `DiffParseError`, `AstParseError` (with `file_path: str`, `line: int | None`), `SymbolResolutionError`, `DepGraphError`, `BlastRadiusError`.
    - Export every class from `trikon/change_intel/__init__.py`.
    - No bare `Exception` / `ValueError` / `RuntimeError` / `sqlite3.Error` may be raised anywhere in `trikon/change_intel/**` — this file is the only sanctioned raise vocabulary.
    - _Requirements: 6.1_

  - [x] 1.2 Write `trikon/change_intel/models.py`
    - Implement the frozen dataclasses from `design.md §Data Models`: `Hunk`, `FileChange`, `ChangeSet` (with `python_files` property), `SymbolDef`, `SymbolRefInternal`.
    - All dataclasses `frozen=True, slots=True`. Line-index and byte-offset fields are `int`; added/removed line indices are `tuple[int, ...]`.
    - Add the `ChangeKind`, `SymbolKind`, `RefKind` `Literal` aliases.
    - Export from `trikon/change_intel/__init__.py`.
    - _Requirements: 1.1, 2.1, 3.1_

  - [x]* 1.3 Write unit tests in `tests/unit/change_intel/test_errors_and_models.py`
    - Assert every module under `trikon/change_intel/**` raises only `ChangeIntelError` subclasses (AST-scan test walks every `Raise` node in the package and asserts the raised type resolves to a subclass of `ChangeIntelError`). Skip during Task 1; enable when downstream modules land.
    - Assert `Hunk`, `FileChange`, `ChangeSet`, `SymbolDef`, `SymbolRefInternal` are frozen (mutation raises `FrozenInstanceError`) and hashable.
    - Assert `ChangeSet.python_files` filters to `.py` and `.pyi` and preserves POSIX-sorted order.
    - _Requirements: 6.1_

- [x] 2. Materialize `examples/sample_repo/` and expected-impact fixtures

  - [x] 2.1 Create the baseline sample repo layout
    - Directory tree per `design.md §8.2`: `examples/sample_repo/{pyproject.toml, .trikon/policy.yaml, src/payments/{__init__.py, gateway.py, retry.py}, src/orders/{__init__.py, worker.py}, src/api/{__init__.py, payments.py}, tests/{test_retry.py, test_gateway.py, test_worker.py, api/test_payments.py}}`.
    - `gateway.charge(...)` is the public-API surface (used by `api.payments` and `orders.worker`). `retry.backoff(...)` is called by `gateway.charge` and by `orders.worker`. Cross-package edges must exist so `transitive_dependents` has something to traverse.
    - `.trikon/policy.yaml` declares `sensitive_paths: ["payments/**"]` and default weights per `BlastWeights`.
    - Baseline `pytest examples/sample_repo/` exits 0 with every test green.
    - _Requirements: 1.1, 5.1_

  - [x] 2.2 Materialize the five change scenarios
    - Store each scenario as a patch file at `tests/fixtures/scenarios/{clean_refactor,bad_retry,sensitive_touch,no_python_change,deleted_file}.patch` applied to the baseline via `git apply` in the test fixture.
    - Scenarios match the table in `design.md §8.2`: `clean_refactor` renames a helper in `orders.worker`; `bad_retry` changes `payments.retry.backoff` timing so `test_worker` fails at runtime; `sensitive_touch` modifies `payments.gateway.charge`; `no_python_change` edits `README.md`; `deleted_file` deletes `orders.worker` and removes callers.
    - Each patch is minimal (no drive-by whitespace) so hunks map cleanly to specific symbols.
    - _Requirements: 1.1, 5.1, 5.3_

  - [x] 2.3 Write expected `ImpactSet` fixtures under `tests/fixtures/expected_impact/`
    - One JSON file per scenario: `{clean_refactor,bad_retry,sensitive_touch,no_python_change,deleted_file}.json`.
    - Each fixture holds the exact expected `ImpactSet` for the scenario: `changed_files`, `changed_symbols` (list of `SymbolRef` in canonical order), `impacted_modules`, `impacted_public_apis`, `impacted_tests`, `blast_radius_score` bucket, and `blast_radius_numeric`.
    - Numbers derive from the default `BlastWeights` in `design.md §2.5` — spelled out in a comment field so future tuning is traceable.
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 3. Implement `DepGraph` and the SQLite state schema

  - [x] 3.1 Write `trikon/change_intel/dep_graph.py`
    - `DepGraph(db_path: Path)` opens SQLite lazily on the first mutating call; sets pragmas `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY` on every connection.
    - DDL exactly per `design.md §4`: `schema_meta`, `file_index`, `symbols` (unique `(qualified_name, file_sha)`, FK to `file_index` with `ON DELETE CASCADE`), `edges` (composite PK `(source_id, target_id, kind)`, FKs with cascade), plus the two indexes on `symbols` and one on `edges(target_id)`.
    - Populate `schema_meta` on first init with `schema_version='1'`, `created_at`, `trikon_version`.
    - Public API: `upsert_file`, `upsert_symbols`, `upsert_edges`, `transitive_dependents` (reverse-edge BFS, BFS-order return, ties by `qualified_name`), `file_needs_reindex`, `prune_files`, `close`, `__enter__`, `__exit__`.
    - `upsert_symbols` runs the three-step transaction from `design.md §4.1`: `INSERT OR REPLACE` file_index, `DELETE FROM symbols WHERE file_path=? AND file_sha != ?`, bulk `INSERT OR IGNORE` new symbols.
    - Schema-version guardrail on open: version `<` current runs migrations from `trikon/change_intel/migrations/`; version `>` current raises `DepGraphError`.
    - Every raise is `DepGraphError` — `sqlite3.Error` never escapes.
    - _Requirements: 4.1, 4.2, 4.3, 6.1_

  - [x]* 3.2 Write unit tests in `tests/unit/change_intel/test_dep_graph.py`
    - Use `:memory:` SQLite so tests are hermetic and fast.
    - Round-trip: `upsert_symbols(p, h, xs)` followed by a query returns a set equal to `set(xs)`. **Property 7 / Validates: Requirements 4.1**.
    - Model-based property test with `hypothesis`: generate random DAGs (≤50 nodes) and seed sets, compare `transitive_dependents(seeds, max_hops=k)` against a reference computed with `networkx.descendants_at_distance` capped at `k`. **Property 8 / Validates: Requirements 4.2**.
    - `file_needs_reindex` returns `True` for unknown paths and for stored-SHA mismatches, `False` when SHA matches. **Property 9 / Validates: Requirements 4.3**.
    - Cascade delete: bumping `file_sha` for a path removes the old symbol rows and their edges (via FK cascade).
    - Migration guardrail: seed `schema_meta.schema_version='9999'`, opening the DB raises `DepGraphError`.
    - `prune_files(keep_paths)` deletes only rows outside `keep_paths`, cascade to symbols and edges.
    - _Requirements: 4.1, 4.2, 4.3, 6.1_

- [x] 4. Implement the fast AST indexer path

  - [x] 4.1 Write `trikon/change_intel/ast_indexer.py::fast_index_symbols`
    - Signature: `fast_index_symbols(source: str) -> list[SymbolDef]`.
    - Use stdlib `ast.parse` with `type_comments=False`. Walk the tree emitting `SymbolDef` for `FunctionDef`, `AsyncFunctionDef`, `ClassDef`, methods (any nesting depth inside a class body), and module-level `Assign` / `AnnAssign` with a single `Name` target.
    - Compute `start_byte` / `end_byte` from `ast.get_source_segment` byte offsets (encode source once as UTF-8, index via `col_offset` + newline table). `start_line` / `end_line` are 1-based inclusive.
    - `qualified_name` uses the caller-supplied module path (accept an optional `module_path: str = ""` kw arg; the indexer prepends it for top-level names and threads it through class bodies).
    - `is_public` is `True` iff no component of the qualified name starts with `_`, with the exceptions carved out in `design.md §2.2` (`__init__`, dunder methods).
    - Raise `AstParseError(file_path=..., line=e.lineno)` on `SyntaxError`.
    - _Requirements: 2.1, 6.1_

  - [x]* 4.2 Write property test in `tests/unit/change_intel/test_ast_indexer_fast.py`
    - Hypothesis strategy `python_source()` in `tests/unit/change_intel/strategies.py` generates syntactically-valid Python programs (short — one class + methods, or 2-3 top-level functions).
    - **Property 3: Symbol extraction soundness** — for every `SymbolDef d` returned, the UTF-8 byte slice `source_bytes[d.start_byte:d.end_byte]` re-parses to an AST node whose kind matches `d.kind` and whose derived qualified name matches `d.qualified_name`. **Validates: Requirements 2.1**.
    - `@settings(max_examples=100, deadline=1000)`.
    - _Requirements: 2.1_

- [x] 5. Implement the cached AST indexer path

  - [x] 5.1 Write `trikon/change_intel/ast_indexer.py::index_file`
    - Signature: `index_file(file_path: Path) -> list[SymbolDef]`.
    - Use `libcst.parse_module` and a `libcst.CSTVisitor` to emit `SymbolDef` for the same symbol kinds as `fast_index_symbols`. Byte ranges via `libcst.metadata.PositionProvider` + a newline-index table over the file bytes.
    - Derive `module_path` from `file_path` by walking up to the nearest `__init__.py`-anchored root.
    - Compute `file_sha` = `hashlib.sha256(file_bytes).hexdigest()` and thread it into each returned `SymbolDef`.
    - Raise `AstParseError(file_path=str(file_path), line=e.raw_line)` on `libcst.ParserSyntaxError`.
    - _Requirements: 2.1, 6.1_

  - [x] 5.2 Write `trikon/change_intel/ast_indexer.py::index_files`
    - Signature: `index_files(file_paths: list[Path], *, cache_db: Path) -> dict[Path, list[SymbolDef]]`.
    - Open `DepGraph(cache_db)`; for each path, compute current SHA and call `dep_graph.file_needs_reindex(path, sha)`. Cache hit ⇒ read symbols from the `symbols` table filtered by `(file_path, file_sha)`. Cache miss ⇒ call `index_file` and then `dep_graph.upsert_symbols(path, sha, symbols)` inside a single transaction.
    - Return dict iteration order matches the order of `file_paths`.
    - Propagate `AstParseError`; do NOT swallow parse errors.
    - _Requirements: 2.1, 2.2, 4.1_

  - [x]* 5.3 Write cache-hit property test in `tests/unit/change_intel/test_ast_indexer_cached.py`
    - **Property 4: Incremental-index idempotence and cache-hit** — call `index_files([f], cache_db=db)` twice; second call returns byte-identical `SymbolDef` list AND the `libcst` parser is never invoked (monkeypatch `libcst.parse_module` to raise a sentinel `RuntimeError` on entry; second call must not trip it). **Validates: Requirements 2.2**.
    - Second test: mutate the file between calls; assert the parser IS invoked and the returned symbol set updates to reflect the mutation.
    - _Requirements: 2.2_

- [x] 6. Implement the diff parser

  - [x] 6.1 Write `trikon/change_intel/diff_parser.py::parse_diff`
    - Signature and contract per `design.md §2.1`. Exactly one of `{(base_sha, head_sha), diff}` — violating this raises `DiffInputError`.
    - SHA path: `gitpython`'s `Repo(repo_path).git.diff(base_sha, head_sha, "--unified=0", "--find-renames")` piped to `unidiff.PatchSet`. Diff-string path: `unidiff.PatchSet.from_string(diff)`.
    - Enforce `max_diff_bytes` (default 50 MB) — raise `DiffInputError` above the limit.
    - Missing repo ⇒ `RepoNotFoundError`. Malformed diff ⇒ `DiffParseError`.
    - Build `ChangeSet.files` POSIX-sorted by path. Populate `Hunk.added_lines` / `removed_lines` from `unidiff.PatchedFile` line-index enumeration. Renames surface with `change_kind="renamed"` and `old_path` set.
    - Add `unidiff>=0.7.5,<1` to `pyproject.toml` runtime deps (done in Task 11.3 if not already).
    - _Requirements: 1.1, 1.2, 6.1_

  - [x]* 6.2 Write unit tests in `tests/unit/change_intel/test_diff_parser.py`
    - Build a temporary git repo with `dulwich` or `subprocess.run(["git", ...])`. Commit two revisions, then call `parse_diff` in both SHA-mode and diff-string-mode over the same underlying diff.
    - **Property 1: Diff completeness** — union of hunk added-line and removed-line indices equals the added/removed line index set of `git diff --unified=0`. **Validates: Requirements 1.1**.
    - **Property 2: Diff-input equivalence** — SHA-mode and diff-string-mode return `ChangeSet.files` tuples equal element-for-element (ignoring `base_sha` / `head_sha`). **Validates: Requirements 1.2**.
    - Errors: exactly-one violation → `DiffInputError`; non-repo path → `RepoNotFoundError`; oversized diff → `DiffInputError`; malformed diff string → `DiffParseError`.
    - Rename detection: rename a file between commits, assert exactly one `FileChange(change_kind="renamed", old_path=...)`.
    - _Requirements: 1.1, 1.2, 6.1_

- [x] 7. Implement the symbol resolver

  - [x] 7.1 Write `trikon/change_intel/symbol_resolver.py::find_references`
    - Signature per `design.md §2.3`.
    - Construct `jedi.Project(path=str(repo_path))` inside a `try` — failure raises `SymbolResolutionError` at the entry point (this is the only place the exception is raised).
    - For each candidate reference site returned by `jedi.Script(...).get_references()`, wrap the per-reference lookup in a narrow `try / except (jedi.InternalError, Exception)` — log at DEBUG, skip, continue.
    - Filter out references originating from within the target symbol's own definition (same `file_path`, line inside `[symbol.start_line, symbol.end_line]`).
    - Sort results by `(referring_file, referring_line)` for deterministic output.
    - _Requirements: 3.1, 3.2, 6.1_

  - [x]* 7.2 Write unit tests in `tests/unit/change_intel/test_symbol_resolver.py`
    - Build a synthetic 4-file fixture repo in `tmp_path` with a known reference graph (e.g., `a.py::foo` called from `b.py` line 3 and `c.py` line 7, plus a self-recursive reference in `a.py` line 12).
    - **Property 5: Reference finding soundness and completeness** — `find_references` returns exactly the known references, no missing, no spurious, modulo the self-reference filter. **Validates: Requirements 3.1**.
    - **Property 6: No-crash invariant** — inject a file with a valid syntax but broken import (`from nonexistent_pkg import foo`); assert `find_references` still returns without raising, yields the resolvable refs, and skips the broken site.
    - Monkeypatch `jedi.Project` to raise on construction; assert `SymbolResolutionError` propagates from the entry point.
    - _Requirements: 3.1, 3.2_

- [x] 8. Implement the blast-radius orchestrator

  - [x] 8.1 Write `trikon/change_intel/blast_radius.py` helpers
    - `BlastWeights` frozen dataclass exactly per `design.md §2.5` (default weights, bucket thresholds, sensitive-path globs).
    - `bucket(score: float, *, weights: BlastWeights | None = None) -> BlastBucket` — total function; `score <= low_bucket_max` ⇒ `"LOW"`, `<= medium_bucket_max` ⇒ `"MEDIUM"`, else `"HIGH"`.
    - `enclosing_symbols(file_path: Path, changed_lines: Iterable[int], symbols: list[SymbolDef]) -> list[SymbolDef]` — return every `SymbolDef` whose `[start_line, end_line]` intersects any `changed_lines`, sorted by `(start_line, qualified_name)`.
    - Every raise is `BlastRadiusError`.
    - _Requirements: 5.1, 5.2, 6.1_

  - [x] 8.2 Write `trikon/change_intel/blast_radius.py::compute_impact`
    - Signature per `design.md §2.5`. Orchestrates the six numbered steps: `index_files` on all changed `.py` files; map each hunk's changed lines to enclosing `SymbolDef` via `enclosing_symbols`; query `DepGraph.transitive_dependents(seeds, max_hops)`; union into `impacted_modules` and `impacted_public_apis`; filename-heuristic test selection (v0.1) — `tests/test_{mod}.py`, `tests/{pkg}/test_{mod}.py`, plus test files modified in the change set; compute weighted score and bucket.
    - Sensitive-path detection uses `pathlib.PurePosixPath.match` against every glob in `weights.sensitive_paths`; each match adds `weights.sensitive_path_touch` before bucket saturation.
    - Translate internal `SymbolDef` to public `evidence.report.SymbolRef` via the `_to_public` helper spelled out in `design.md §2.5`.
    - Wrap every downstream call in a single `try / except ChangeIntelError` and re-raise as `BlastRadiusError` with `__cause__` chained.
    - Files modified: create `trikon/change_intel/blast_radius.py`; ensure `trikon/change_intel/__init__.py` re-exports `compute_impact`, `bucket`, `BlastWeights`.
    - _Requirements: 5.1, 5.2, 5.3, 6.1_

  - [x]* 8.3 Write property + unit tests in `tests/unit/change_intel/test_blast_radius.py`
    - **Property 10: Impact soundness on changed symbols** — hypothesis-generated `ChangeSet` values over a small fixture repo; assert every `SymbolDef` whose line range intersects any hunk's changed lines appears in `compute_impact(...).changed_symbols`. **Validates: Requirements 5.1**.
    - **Property 11: Score-bucket monotonicity** — for any `x <= y`, `bucket(x) <= bucket(y)` under the `LOW < MEDIUM < HIGH` order. **Validates: Requirements 5.2**.
    - **Property 12: Sensitive-path weight monotonicity** — for any `ChangeSet C`, `score(C ∪ sensitive_touch) >= score(C) + weights.sensitive_path_touch`, measured before HIGH saturation. **Validates: Requirements 5.3**.
    - Unit tests: `enclosing_symbols` on hand-built intersections (interval-endpoint edge cases, empty `changed_lines`, symbols spanning multiple hunks).
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 9. Wire the SDK entry point and CLI debug command

  - [x] 9.1 Extend `trikon/sdk.py` with a Phase-1 `verify` implementation
    - Implement `verify(repo_path: Path, base_sha: str, head_sha: str, *, cache_db: Path | None = None) -> Verdict` for the change-intel path only. Verification runner and policy engine stay stubbed (Phase 2/3).
    - Body: `parse_diff` → `compute_impact` → return `Verdict(decision="require_human", reason="Phase 1: change-intel only", evidence=Evidence(change=impact, verification=EMPTY_VERIFICATION, policy_results=[]), ...)`.
    - Wrap the whole path in a single `try / except ChangeIntelError`; on catch, return the never-fail-open verdict per `design.md §5.1`: `decision="require_human"`, `evidence.change=EMPTY_IMPACT_SET` with `blast_radius_score="HIGH"`.
    - Define `EMPTY_IMPACT_SET` (in `trikon/evidence/report.py` if not already present) as a module-level singleton with zero symbols and `HIGH` bucket.
    - _Requirements: 5.1, 5.2, 6.1, 6.2_

  - [x] 9.2 Add `trikon debug impact` to `trikon/cli.py`
    - New Typer sub-app `debug` with an `impact` command taking `--repo`, `--base`, `--head`, optional `--cache-db`.
    - Body calls `sdk.verify(...)` and prints `verdict.evidence.change.model_dump_json(indent=2)`. Exit code 0 on success; any uncaught exception exits 1 with the exception name on stderr.
    - Wire the sub-app into the main `trikon` Typer application so `trikon --help` lists `debug`.
    - _Requirements: 5.1, 6.2_

- [x] 10. Integration tests against `examples/sample_repo/`

  - [x]* 10.1 Write `tests/integration/change_intel/test_end_to_end_sample_repo.py`
    - For each of the five scenarios (`clean_refactor`, `bad_retry`, `sensitive_touch`, `no_python_change`, `deleted_file`): apply the patch to a temp copy of `examples/sample_repo/`, commit both baseline and patched revisions, call `sdk.verify(...)`, and assert `verdict.evidence.change` equals the fixture at `tests/fixtures/expected_impact/{scenario}.json` (compared via `ImpactSet.model_dump()` after canonicalizing list orderings).
    - Sensitive-path scenario must land in the `HIGH` bucket; `no_python_change` must return zero impacted symbols with `changed_files=["README.md"]`.
    - _Requirements: 1.1, 5.1, 5.2, 5.3_

  - [x]* 10.2 Write `tests/integration/change_intel/test_never_fail_open.py`
    - **Property 13: Error-hierarchy closure** — AST-scan `trikon/change_intel/**` for every `Raise` node and assert the raised type is a subclass of `ChangeIntelError`. **Validates: Requirements 6.1**.
    - Never-fail-open integration: monkeypatch `trikon.change_intel.blast_radius.compute_impact` to raise each `ChangeIntelError` subclass in turn; assert `sdk.verify(...)` returns a `Verdict` with `decision="require_human"` AND `evidence.change == EMPTY_IMPACT_SET` (same identity or equal value) AND `evidence.change.blast_radius_score == "HIGH"`. **Validates: Requirements 6.2**.
    - _Requirements: 6.1, 6.2_

- [x] 11. Land CI + tooling baselines

  - [x] 11.1 Create `.github/workflows/ci.yml`
    - Contents match `design.md §9.1` verbatim: `lint-type-test` job (matrix on 3.11, 3.12) running `ruff check`, `ruff format --check`, `mypy --strict trikon/change_intel/ trikon/evidence/`, `pytest tests/unit/ --cov=trikon.change_intel --cov-branch`, `coverage report --fail-under=90 --include='trikon/change_intel/*'`; `integration` job depending on it running `pytest tests/integration/`.
    - Do NOT include the `perf` job yet — Task 12.2 wires it after benchmarks exist.
    - _Requirements: 2.1, 4.1, 5.1, 6.1_

  - [x] 11.2 Create `.pre-commit-config.yaml`
    - Contents match `design.md §9.2`: `ruff` + `ruff-format`, `mypy` limited to `^trikon/change_intel/` with `--strict` and `additional_dependencies: [pydantic, types-PyYAML]`, standard `pre-commit-hooks` (check-yaml, check-added-large-files max 500 KB, end-of-file-fixer, trailing-whitespace).
    - _Requirements: 2.1, 6.1_

  - [x] 11.3 Extend `pyproject.toml`
    - Add runtime dep `unidiff>=0.7.5,<1`.
    - Add dev deps `hypothesis>=6.100,<7`, `pytest-benchmark>=4,<5`, `networkx>=3,<4` (used only in the DepGraph model-based test).
    - Merge the `[tool.mypy]` block from `design.md §9.3` (`disallow_any_explicit`, `plugins = ["pydantic.mypy"]`, per-module `ignore_missing_imports` overrides for jedi/libcst/unidiff/git).
    - Add `[tool.coverage.run]` with `branch = true, source = ["trikon"]` and `[tool.coverage.report]` with the exclusion list.
    - _Requirements: 2.1, 6.1_

- [x] 12. Performance benchmark suite and CI gate

  - [x]* 12.1 Write `tests/benchmarks/test_perf.py`
    - Four `pytest-benchmark` cases matching `design.md §7`:
      - Cold-index Django (~250K LOC) target ≤ 30 s, CI fail > 45 s. **Validates: Requirements 7.1**.
      - Warm re-index of 5 changed files after a cold pass target ≤ 500 ms, CI fail > 1000 ms. **Validates: Requirements 7.2**.
      - `compute_impact` on a 3-file change against `examples/sample_repo/` target ≤ 200 ms, CI fail > 400 ms. **Validates: Requirements 7.3**.
      - `DepGraph.transitive_dependents(max_hops=5)` on the fully-indexed Django graph target ≤ 100 ms p95, CI fail > 200 ms. **Validates: Requirements 7.3**.
    - Django path resolves from `TRIKON_DJANGO_PATH`; if unset, the Django benchmarks are skipped and only `compute_impact` on `sample_repo` runs.
    - Baseline files stored in `tests/benchmarks/.benchmarks/`; regressions > 20 % against baseline fail the assertion.
    - _Requirements: 7.1, 7.2, 7.3_

  - [x] 12.2 Add the `perf` job to `.github/workflows/ci.yml`
    - Append the `perf` job from `design.md §9.1` after the `integration` job — runs on `pull_request`, `needs: lint-type-test`, uses `actions/cache` keyed by `django-mainline-{YYYY-MM}`, runs `pytest tests/benchmarks/ --benchmark-only --benchmark-max-time=60`.
    - Gate on the ≤ 45 s / ≤ 1000 ms / ≤ 400 ms / ≤ 200 ms CI ceilings from Req 7.
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 13. Documentation

  - [x] 13.1 Write `docs/change_intel.md`
    - 1-page prose walkthrough: what the subsystem does, the five modules, the `state.db` schema summary, the `trikon debug impact` command with a copy-pasteable example against `examples/sample_repo/`.
    - Show the expected `ImpactSet` JSON for the `bad_retry` scenario so a reader can eyeball what "correct" looks like.
    - Link from `README.md`'s Documentation section.
    - _Requirements: 1.1, 5.1_

  - [x] 13.2 Refresh public-facing docs
    - Update `docs-site/concepts/blast-radius.mdx` (or create it if the file does not yet exist) with the same conceptual walkthrough scoped to product users. Cross-link to `docs/change_intel.md` for engineering-level detail.
    - Add a "Change Intelligence (Phase 1)" bullet to `README.md`'s feature list with the `trikon debug impact` invocation.
    - _Requirements: 5.1_

- [x] 14. Finalize Phase 1

  - [x] 14.1 Run full quality gates and close out DoD
    - `mypy --strict trikon/change_intel/ trikon/evidence/` exits 0.
    - `ruff check trikon/ tests/` and `ruff format --check trikon/ tests/` exit 0.
    - `pytest tests/unit/change_intel/ tests/integration/change_intel/` all green.
    - `coverage report --fail-under=90 --include='trikon/change_intel/*'` passes.
    - `trikon debug impact --repo examples/sample_repo --base HEAD~1 --head HEAD` prints the expected `ImpactSet` for the applied scenario.
    - Flip every `[ ]` in `EXECUTION_PLAN.md §Phase 1` and `design.md §10 Definition of Done` to `[x]`.
    - Ensure all tests pass, ask the user if questions arise.
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 7.1, 7.2, 7.3_

## Notes

- Sub-tasks marked `*` are optional in the sense of Kiro's "skip for a fast MVP" convention. In practice, every `*` test task is required to hit the DoD in `design.md §10` — the marker signals "test code, not implementation code", not "throwaway".
- Each property test cites its numbered Correctness Property from `design.md §11` and the specific `Requirements` clause it validates, so traceability from acceptance criterion → property → test file is one grep.
- Checkpoints are folded into the numbered flow: 9.2 is the first end-to-end runnable checkpoint (`trikon debug impact` works on `sample_repo`); 10.2 is the safety checkpoint (never-fail-open holds); 14.1 is the release checkpoint.
- The task graph does not include Phase 2 (verification runner) or Phase 3 (policy engine). `sdk.verify` intentionally returns `require_human` in Phase 1 because there is no verification evidence to grade yet.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1", "11.3"] },
    { "id": 1, "tasks": ["1.3", "2.2", "3.1", "4.1", "6.1", "11.1", "11.2"] },
    { "id": 2, "tasks": ["2.3", "3.2", "4.2", "5.1", "6.2", "7.1"] },
    { "id": 3, "tasks": ["5.2", "7.2", "8.1"] },
    { "id": 4, "tasks": ["5.3", "8.2"] },
    { "id": 5, "tasks": ["8.3", "9.1"] },
    { "id": 6, "tasks": ["9.2", "12.1"] },
    { "id": 7, "tasks": ["10.1", "10.2", "13.1"] },
    { "id": 8, "tasks": ["12.2", "13.2"] },
    { "id": 9, "tasks": ["14.1"] }
  ]
}
```
