# Requirements Document

## Introduction

Change Intelligence is the subsystem that turns a git change set into a structured `ImpactSet` — the impacted symbols, modules, tests, and a numeric blast-radius score — that every downstream Trikon stage consumes.

It exists as Phase 1 of the 12-week Trikon build because verification, policy, and verdict emission all treat its output as ground truth. If Change Intelligence misidentifies which tests should run, the runner wastes cycles on the wrong ones and the policy engine grades a change against evidence that never touched it. Every reliability claim Trikon makes about unattended agents reduces to a correctness claim about this subsystem.

In non-technical terms: this is the part of Trikon that reads a git diff and decides which existing tests actually need to run, how far the change reaches through the codebase, and whether the surface area is small enough that an autonomous agent should be trusted with it. The requirements below fix the observable behavior of that pipeline. Design decisions (SQLite schema, libcst vs stdlib ast, jedi as the resolver) live in `design.md`.

## Glossary

- **Change_Intelligence**: The `trikon/change_intel/` subsystem, addressed at its public entry points (`parse_diff`, `index_files`, `find_references`, `DepGraph`, `compute_impact`, `bucket`).
- **ChangeSet**: The frozen dataclass produced by `parse_diff`, containing `repo_path`, `base_sha`, `head_sha`, and an ordered tuple of `FileChange` entries.
- **FileChange**: One entry in a ChangeSet — a path, a change kind (added / modified / deleted / renamed), an optional old path, and its hunks.
- **SymbolDef**: The internal frozen dataclass representing a function, class, method, or module-level assignment, including byte-accurate `start_byte`/`end_byte` and `start_line`/`end_line` ranges and a `file_sha`.
- **ImpactSet**: The public Pydantic model returned by `compute_impact`, holding `changed_files`, `changed_symbols`, `impacted_modules`, `impacted_public_apis`, `impacted_tests`, `blast_radius_score`, and `blast_radius_numeric`.
- **DepGraph**: The SQLite-backed directed graph persisting symbols and their `uses` edges in `<repo>/.trikon/state.db`.
- **ChangeIntelError**: The exception base class for every error raised inside `trikon/change_intel/**` (`DiffInputError`, `RepoNotFoundError`, `DiffParseError`, `AstParseError`, `SymbolResolutionError`, `DepGraphError`, `BlastRadiusError`).
- **EMPTY_IMPACT_SET**: The sentinel `ImpactSet` with zero symbols and `blast_radius_score = "HIGH"`, emitted only when Change_Intelligence fails.
- **BlastWeights**: The frozen dataclass of scoring weights (`impacted_modules`, `impacted_public_apis`, `impacted_test_files`, `cross_package_hops`, `sensitive_path_touch`) plus bucket thresholds and sensitive-path globs.

## Requirements

### Requirement 1: Parse git diffs into structured change sets

**User Story:** As a Trikon operator, I want any git change (either two SHAs or a raw unified diff) parsed into a well-typed ChangeSet, so that downstream stages receive stable, ordered inputs.

#### Acceptance Criteria

1. WHEN `parse_diff` is called with a valid `(base_sha, head_sha)` pair, THE Change_Intelligence SHALL return a ChangeSet whose `files` tuple contains exactly one FileChange per modified path in the underlying `git diff` and whose union of hunk added-line and removed-line indices equals the added-line and removed-line index set of that git diff.
2. WHEN `parse_diff` is called with a `diff` string AND separately with `(base_sha, head_sha)` that produce the same underlying unified diff, THE Change_Intelligence SHALL return two ChangeSet values whose `files` tuples are equal element-for-element, ignoring the `base_sha` and `head_sha` fields.

### Requirement 2: Extract Python symbols from source files with byte-accurate ranges

**User Story:** As Trikon, I want every function, class, method, and module-level assignment in changed Python files identified with byte-accurate line ranges, so that diff hunks can be mapped to enclosing symbols.

#### Acceptance Criteria

1. WHEN `index_file` is called on a syntactically-valid Python source file at path `p`, THE Change_Intelligence SHALL return SymbolDef entries such that for every returned entry `d` the byte slice of `p` at `[d.start_byte, d.end_byte]` re-parses to an AST node whose kind matches `d.kind` and whose fully-qualified name matches `d.qualified_name`.
2. WHEN `index_files` is called a second time on a set of paths whose SHA-256 hashes are unchanged since the first call, THE Change_Intelligence SHALL serve the second call from the SQLite cache and SHALL NOT invoke the AST parser for any of those paths.

### Requirement 3: Resolve cross-file symbol references

**User Story:** As Trikon, I want to know which other files reference a given symbol, so that a change to one function can propagate through the graph to all its callers.

#### Acceptance Criteria

1. WHEN `find_references` is called against a synthetic repository with a known reference graph, THE Change_Intelligence SHALL return a `SymbolRefInternal` set equal to the true set of references to the target symbol, with no missing references and no spurious references, modulo references originating from within the target symbol's own definition.
2. IF a single reference lookup fails because of a broken import, missing virtualenv metadata, or any per-reference jedi failure, THEN THE Change_Intelligence SHALL skip that reference, continue iterating, and return the successfully-resolved references; the Change_Intelligence SHALL raise `SymbolResolutionError` only when the underlying `jedi.Project` itself cannot be constructed for the repository.

### Requirement 4: Persist an incremental dep graph in SQLite

**User Story:** As Trikon, I want the symbol graph persisted between runs so re-parsing unchanged files is skipped, and blast-radius queries against a large codebase complete in milliseconds.

#### Acceptance Criteria

1. WHEN `DepGraph.upsert_symbols(file_path, file_sha, symbols)` is followed by a query for all symbols at `(file_path, file_sha)`, THE Change_Intelligence SHALL return a set of SymbolDef values equal to the input `symbols` set.
2. WHEN `DepGraph.transitive_dependents(seeds, max_hops=k)` is called against a DAG of symbols with edge set `E` and seed set `S`, THE Change_Intelligence SHALL return the set of nodes reachable from `S` via reverse-edge breadth-first search within `k` layers.
3. WHEN `DepGraph.file_needs_reindex(path, current_sha)` is called, THE Change_Intelligence SHALL return `True` if `path` is absent from `file_index` OR the stored `file_sha` differs from `current_sha`, and SHALL return `False` otherwise.

### Requirement 5: Compute a blast-radius score and impacted-test set

**User Story:** As Trikon, I want changed symbols turned into a full ImpactSet with a numeric score, a bucket of LOW / MEDIUM / HIGH, and a set of tests that should run, so that the verification runner and policy engine have a single canonical input.

#### Acceptance Criteria

1. WHEN `compute_impact(change_set, repo_path)` is called, THE Change_Intelligence SHALL include in the returned `ImpactSet.changed_symbols` every SymbolDef whose `[start_line, end_line]` interval intersects any changed line in any hunk of any FileChange in `change_set`.
2. WHEN `bucket(x)` and `bucket(y)` are called for any two scores `x` and `y` with `x <= y`, THE Change_Intelligence SHALL order `bucket(x)` no higher than `bucket(y)` under the ranking `LOW < MEDIUM < HIGH`.
3. WHEN a ChangeSet touches at least one path matching an entry in `BlastWeights.sensitive_paths`, THE Change_Intelligence SHALL produce a numeric score at least equal to the score of the same ChangeSet without the sensitive-path touch plus `BlastWeights.sensitive_path_touch`, measured before bucket saturation at the HIGH ceiling.

### Requirement 6: Never fail-open

**User Story:** As a platform team, I want Trikon safe to deploy in unattended mode, so that any internal error produces `require_human` and never `allow`.

#### Acceptance Criteria

1. THE Change_Intelligence SHALL raise only subclasses of `ChangeIntelError` at every raise site inside `trikon/change_intel/**`; no bare `Exception`, `ValueError`, `RuntimeError`, or `sqlite3.Error` SHALL escape the module boundary.
2. IF any `ChangeIntelError` propagates to `sdk.verify`, THEN THE Trikon SDK SHALL return a Verdict whose `decision` equals `"require_human"` and whose `evidence.change` equals `EMPTY_IMPACT_SET` with `blast_radius_score = "HIGH"`.

### Requirement 7: Meet performance targets on production-scale repositories

**User Story:** As a developer running Trikon in an editor MCP flow, I want verdicts to feel real-time on typical changes and finish in under a minute on cold-cache large-repo scans.

#### Acceptance Criteria

1. WHEN Change_Intelligence performs a cold index of the Django repository (~250K lines of code) on a modern laptop (Ryzen-7-tier CPU, NVMe SSD), THE Change_Intelligence SHALL complete indexing in 30 seconds or less; the CI perf job SHALL fail if the same operation exceeds 45 seconds.
2. WHEN Change_Intelligence re-indexes 5 changed files after a completed cold pass, THE Change_Intelligence SHALL complete the re-index in 500 milliseconds or less.
3. WHEN `compute_impact` is called on a 3-file change against `examples/sample_repo`, THE Change_Intelligence SHALL complete in 200 milliseconds or less; WHEN `DepGraph.transitive_dependents` is called at `max_hops = 5` on the fully-indexed Django graph, THE Change_Intelligence SHALL complete with a p95 latency of 100 milliseconds or less.
