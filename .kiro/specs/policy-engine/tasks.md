# Implementation Plan: Policy Engine (Trikon Phase 3)

## Overview

Convert the frozen design in `design.md` into buildable, incremental coding tasks that layer `trikon/policy/**`, `trikon/audit_log/**`, and the Verdict-shaping edits to `trikon/evidence/report.py` on top of the Phase-1 change-intelligence tree and the Phase-2 verification-runner tree. Each task ships with types, tests, and mypy-strict cleanliness at that step so the tree is green after every merge.

The build order is: `PolicyEvaluationError` hierarchy + `Decision`/`Verdict.warnings`/`schema_version` widening + packaged `default_policy.yaml` + `audit_log` SQLite DDL (foundation, Wave 1) → `_rule_matches` dispatcher + `load_policy`/`default_policy` + `evaluate_policy` warn accumulation + `record_verdict` writer + `format_markdown` layout (adapters, Wave 2) → `sdk.verify` pipeline rewrite + `trikon verify` / `trikon init` CLI wiring (integration, Wave 3) → integration tests against `examples/sample_repo/` + perf smoke tests + docs + coverage/lint gates (release, Wave 4). Every task cites the granular acceptance criteria it validates and the exact files it creates or modifies. Test sub-tasks are postfixed with `*` per the Kiro convention; they may be skipped for a fast MVP but are required to hit the Definition of Done.

The subsystem holds three invariants at every green build: **never-fail-open** (every raise site under `trikon/policy/**` and `trikon/audit_log/**` is a `PolicyEvaluationError` subclass, and every uncaught `TrikonError` at the SDK boundary — except `AuditLogError` — becomes `require_human` with `EMPTY_IMPACT_SET` + `EMPTY_VERIFICATION`), **first-match-terminal** (the first matching rule with a terminal `then` in declaration order wins; warn rules accumulate reasons without terminating), and **append-only-audit** (`trikon/audit_log/**` emits only `INSERT` after the initial `CREATE TABLE IF NOT EXISTS` — no `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `TRUNCATE`, enforced by an AST-scan test). Definition of Done: `trikon verify --repo examples/sample_repo --base HEAD~1 --head HEAD` produces exit code `1` (`decision == "block"`, `matched_rule == "impacted tests failed"`) after applying `tests/fixtures/scenarios/bad_retry.patch`, exit code `0` (`decision == "allow"`, `matched_rule == "green, low-blast auto-allow"`) after applying `tests/fixtures/scenarios/clean_refactor.patch`, and exit code `2` (`decision == "require_human"`, `matched_rule == "sensitive path requires human"`) after applying a patch under `payments/` — end-to-end, with an `audit_log` row persisted for every verdict.

## Tasks

- [x] 1. Establish the `PolicyEvaluationError` hierarchy under `trikon/policy/`

  - [x] 1.1 Create `trikon/policy/errors.py` with the four-class hierarchy
    - New module: `PolicyEvaluationError(TrikonError)` base, plus `PolicyLoadError`, `RuleMatchError`, and `AuditLogError` subclasses. Every class carries the exact docstring from `design.md §9` — including the raise-site guidance for `PolicyLoadError`'s YAML/Pydantic/`OSError` chaining, `RuleMatchError`'s "policy-authoring error, not a data error" framing, and `AuditLogError`'s "hard failure at the SDK boundary" contract.
    - Base class is directly raisable for evaluator-internal defensive raises (documented as should-be-unreachable in `design.md §9.1`); every subsystem subclass is what real raise sites use.
    - Parent class `TrikonError` is already lifted by the Phase-2 Task 1.1 — no re-parenting needed in Phase 3.
    - _Requirements: 7.1_

  - [x] 1.2 Re-export the hierarchy from `trikon/policy/__init__.py`
    - Add `from trikon.policy.errors import PolicyEvaluationError, PolicyLoadError, RuleMatchError, AuditLogError` and extend `__all__` with the four names.
    - This is the only sanctioned raise vocabulary for `trikon/policy/**` and `trikon/audit_log/**`; the audit-log subpackage re-imports the same names from here rather than defining its own error tree (`design.md §9`).
    - _Requirements: 7.1_

  - [ ]* 1.3 Write unit tests in `tests/unit/policy/test_errors.py`
    - Class-hierarchy assertions: `issubclass(PolicyLoadError, PolicyEvaluationError)`, `issubclass(RuleMatchError, PolicyEvaluationError)`, `issubclass(AuditLogError, PolicyEvaluationError)`, `issubclass(PolicyEvaluationError, TrikonError)`.
    - Importability: `from trikon.policy import PolicyEvaluationError, PolicyLoadError, RuleMatchError, AuditLogError` succeeds for every subclass.
    - Instantiability: every subclass constructs cleanly from a single string arg and carries the arg through `str(exc)`.
    - AST-scan seed test that walks every `Raise` node under `trikon/policy/**` and `trikon/audit_log/**` (excluding `errors.py` itself) and asserts the raised type resolves to a subclass of `PolicyEvaluationError`. Skip during Task 1; enable when downstream modules land.
    - _Requirements: 7.1_

- [x] 2. Widen `Decision`, add `Verdict.warnings`, bump `schema_version` in `trikon/evidence/report.py`

  - [x] 2.1 Widen `Decision` to the four-value literal and widen `RuleResult.would_emit`
    - Replace `Decision = Literal["allow", "block", "require_human"]` with `Decision = Literal["allow", "block", "require_human", "warn"]` per `design.md §3.6`.
    - Add `PolicyDecision = Decision` — documented alias (identical `Literal`) so downstream code can annotate "rule outcome may include warn" as distinct from "terminal verdict decision" without runtime divergence.
    - Widen `RuleResult.would_emit: Decision | None` to reference the widened `Decision`; a warn rule that fires still reports `would_emit="warn"` in its trace entry (Requirement 3.4).
    - The widening is source-compatible: every existing consumer that asserts `decision in ("allow", "block", "require_human")` continues to work, because `sdk.verify` never emits `decision == "warn"` on any path — Property 12 pins this at the SDK boundary.
    - _Requirements: 3.4_

  - [x] 2.2 Add `Verdict.warnings: list[str]` and bump `Verdict.schema_version` default `1 → 2`
    - Add `warnings: list[str] = Field(default_factory=list)` — never `None`; a warn-empty Verdict carries `warnings == []` (Requirement 3.3).
    - Change `schema_version: int = 1` to `schema_version: int = 2` per `design.md §3.6`. Downstream consumers pinned to schema_version=1 will see the version bump before they see an unfamiliar `warnings` key (Requirement 4.2).
    - `_fail_closed_verdict` picks up both defaults automatically — no edit to the helper is needed (`design.md §10.1`, §7.4).
    - _Requirements: 3.3, 3.4, 4.2_

  - [ ]* 2.3 Write unit tests in `tests/unit/evidence/test_report_widening.py`
    - Model round-trip: construct a Verdict with `warnings=["foo", "bar"]`, `model_dump_json()` then `model_validate_json()` — assert `warnings` order preserved and `schema_version == 2`.
    - `warnings` default: constructing a Verdict without passing `warnings` yields `verdict.warnings == []` (never `None`).
    - `schema_version` default: constructing a Verdict without passing `schema_version` yields `verdict.schema_version == 2` — Property 12.
    - `Decision` widening: `Verdict(decision="warn", ...)` constructs cleanly (defensive — never emitted by `sdk.verify` but the Pydantic type must accept it for forward compat).
    - `RuleResult.would_emit="warn"` constructs cleanly.
    - Backward-compat within reason: a v1-shaped JSON payload lacking `warnings` and `schema_version` deserializes via `Verdict.model_validate_json(...)` with `warnings=[]` and `schema_version=2` populated by the defaults.
    - **Property 12: `schema_version == 2` on every emitted Verdict.** For any Verdict constructed by `evaluate_policy` or the fail-closed helper, `verdict.schema_version == 2` and `Verdict.model_validate_json(verdict.model_dump_json()).schema_version == 2`. **Validates: Requirements 3, 4.2**
    - _Requirements: 3.3, 3.4, 4.2_

- [x] 3. Ship the packaged `default_policy.yaml` inside the wheel

  - [x] 3.1 Copy `examples/policies/default.yaml` → `trikon/policy/default_policy.yaml` verbatim
    - Byte-identical copy of the 6-rule policy — same `sensitive_paths`, same weights, same rule declaration order. The `examples/` copy is retained for documentation and for `trikon init` diagnostic messages; the runtime path is the packaged copy (`design.md §6.3`).
    - Include a top-of-file comment: `# Runtime default policy. Do not edit — sync from examples/policies/default.yaml.`
    - _Requirements: 2.5_

  - [x] 3.2 Wire the packaged YAML into `pyproject.toml`
    - Check the current build backend (hatchling per Phase-1/2 `pyproject.toml`). Add a `[tool.hatch.build.targets.wheel.force-include]` block containing exactly `"trikon/policy/default_policy.yaml" = "trikon/policy/default_policy.yaml"` per `design.md §6.3`. If the build backend is not hatchling, use the setuptools-equivalent `package-data` entry that resolves via `importlib.resources.files("trikon.policy")`.
    - Rationale for `force-include` over a blanket `package_data` entry: hatchling by default excludes non-Python files from `packages = ["trikon"]`. The single-line rule that names both source and destination path is the smallest surface that captures the intent (`design.md §6.3`).
    - _Requirements: 2.5, 5.4_

  - [ ]* 3.3 Write a unit test in `tests/unit/policy/test_default_policy_resource.py`
    - `importlib.resources.files("trikon.policy") / "default_policy.yaml"` reads the file successfully and returns bytes of non-zero length.
    - `yaml.safe_load(resource.read_text(encoding="utf-8"))` returns a mapping with a `rules` key whose value is a list of length 6 (matches the 6-rule default policy shape).
    - Skip this test when running against a source checkout without an installed wheel — the marker `@pytest.mark.skipif(not resource.is_file(), reason="wheel not installed")` protects the CI unit workflow. On the packaged CI wheel job (Task 15.3), the test runs.
    - _Requirements: 2.5, 5.4_

- [x] 4. Land the `audit_log` SQLite schema and wire it into the state-DB open path

  - [x] 4.1 Create `trikon/audit_log/__init__.py` and `trikon/audit_log/db.py`
    - New subpackage. `__init__.py` starts with only the module docstring from `design.md §3.5` framing the append-only contract — the `record_verdict` re-export lands in Task 8.1 to satisfy same-file wave discipline.
    - `db.py` defines `_DDL_STATEMENTS: tuple[str, ...]` with exactly three entries: `CREATE TABLE IF NOT EXISTS audit_log (…)` per `design.md §4.1` (six columns: `audit_id TEXT PK`, `created_at TEXT NOT NULL`, `decision TEXT NOT NULL CHECK(decision IN ('allow','block','require_human','warn'))`, `matched_rule TEXT`, `reason TEXT NOT NULL`, `verdict_json TEXT NOT NULL`), `CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)`, and `CREATE INDEX IF NOT EXISTS idx_audit_log_decision ON audit_log(decision)`.
    - `ensure_audit_tables(conn: sqlite3.Connection) -> None` iterates `_DDL_STATEMENTS` under a single `try / except sqlite3.Error as exc: raise AuditLogError(...) from exc` closure. No other DDL surface exists in the module (`design.md §4.2`).
    - _Requirements: 4.1, 4.3, 4.4_

  - [x] 4.2 Wire `ensure_audit_tables` into `DepGraph._get_conn`
    - In `trikon/change_intel/dep_graph.py::_get_conn`, immediately after the existing `ensure_verify_tables(conn)` call from Phase-2 Task 3.2, add `ensure_audit_tables(conn)` inside the same `try` block that closes the connection on failure — matches the shape in `design.md §4.3`.
    - Preserve the Phase-1 pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY`) unchanged.
    - No behavior change for Phase-1/2-only callers: `ensure_audit_tables` is idempotent by construction (`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` only).
    - _Requirements: 4.1, 4.4, 7.1_

  - [ ]* 4.3 Write unit tests in `tests/unit/audit_log/test_db.py`
    - Table existence after `ensure_audit_tables`: query `sqlite_master` for `audit_log` and the two indexes; assert the DDL matches `design.md §4.1` column-by-column.
    - Idempotence: call `ensure_audit_tables` twice on the same connection — no error, no schema drift, no row loss. Pre-seed one row before the second call; assert the row survives.
    - `CHECK(decision IN (…))` constraint: `INSERT INTO audit_log(…) VALUES (…, 'nope', …)` raises `sqlite3.IntegrityError` at the DB layer.
    - Error wrap: monkeypatch `conn.execute` to raise `sqlite3.OperationalError` → `ensure_audit_tables` raises `AuditLogError` with the `sqlite3.Error` on `__cause__` (design matrix row #10).
    - Bootstrap hook: after `DepGraph._get_conn` opens a fresh state DB, `audit_log` exists alongside the Phase-1/2 tables (`schema_meta`, `file_index`, `symbols`, `edges`, `coverage_map`, `tests_seen`, `static_baseline`) — assert every table is present in one query.
    - _Requirements: 4.1, 4.3, 4.4_

- [x] 5. Implement `_rule_matches` — inline dispatcher, condition helpers, shape guards

  - [x] 5.1 Replace the `_rule_matches` stub in `trikon/policy/evaluator.py` with the full inline dispatcher and five condition helpers
    - Dispatcher body per `design.md §5.2`: empty `when` short-circuits to `True` (Requirement 1.7); every key in `rule.when` routes to one of five `_match_*` helpers via an `if`/`elif` chain; the trailing `else` branch raises `RuleMatchError` naming the offending key and rule (Property 5); multi-key `when` ANDs across conditions with short-circuit on the first `False` (Requirement 1.6).
    - Five helper functions per `design.md §5.4-§5.5`: `_match_any_path(patterns, change)` and `_match_no_path(patterns, change)` use `pathlib.PurePosixPath.match` to honor `**` recursion on POSIX-style `change.changed_files`; `_match_blast_radius(expected, change)` and `_match_tests_status(expected, verification)` are literal string equality; `_match_new_errors(op_dict, verification)` uses the three-entry `_NEW_ERRORS_OPERATORS: dict[str, Callable[[int, int], bool]]` table keyed on `"eq" | "gt" | "lt"` from `operator.eq`/`gt`/`lt` and counts `verification.static.findings` with `is_new == True`.
    - Three shape-guard helpers — `_expect_list_str(value, key)`, `_expect_str(value, key)`, `_expect_operator_dict(value, key)` — perform Pydantic-style shape checks and raise `RuleMatchError` on any mismatch so the matcher functions have concrete signatures without `dict[str, Any]` leaking in (`design.md §5.2`).
    - `_match_new_errors` also raises `RuleMatchError` on: multi-key operator dict (`len(op_dict) != 1`), unknown operator name, non-int threshold — matches the three explicit shape violations in `design.md §5.5`.
    - Every raise site wraps as `RuleMatchError` with the offending value on `__cause__` when a `KeyError`/`TypeError` is caught; no bare `Exception`, `ValueError`, or `KeyError` escapes the module boundary (Requirement 7.1).
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 7.1_

  - [ ]* 5.2 Write unit + property tests in `tests/unit/policy/test_evaluator_conditions.py`
    - `any_path_matches`: hits (`["src/auth/token.py"]` against `["auth/**"]` → True), misses (`["src/util/log.py"]` against `["auth/**"]` → False), empty pattern list → False, `**` recursion (`["a/b/c/d.py"]` against `["a/**/*.py"]` → True), empty `changed_files` → False.
    - `no_path_matches`: dual — same inputs as `any_path_matches`, negated result.
    - `change.blast_radius.score`: parametrized over the three bucket values `{"LOW","MEDIUM","HIGH"}` × three observed values → nine combinations, three matches, six misses.
    - `verification.tests.status`: parametrized over `{"passed","failed","skipped"}` × three observed → same nine-combination table.
    - `verification.static.new_errors`: parametrized over `(eq, gt, lt) × N ∈ {0, 1, 5} × observed count ∈ {0, 1, 5}` — 27 combinations against the reference `operator.{eq,gt,lt}` results.
    - Empty `when` (`{}`) — unconditional match (Property 4).
    - Multi-condition AND — a rule with both `any_path_matches` (matches) and `verification.tests.status` (mismatches) → False; both matches → True.
    - Shape mismatches raise `RuleMatchError`: `any_path_matches: "foo.py"` (string not list), `verification.static.new_errors: {"eq": 1, "gt": 2}` (multi-key), `verification.static.new_errors: {"neq": 0}` (unknown operator), `verification.static.new_errors: {"eq": "one"}` (non-int threshold), unknown top-level key (`actor.agent_id: "gpt-5"`).
    - **Property 1: Condition-dispatch completeness.** For any rule whose `when` clause contains exactly one recognized key with a valid argument shape, `_rule_matches` returns the reference matcher's value. **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**
    - **Property 4: Empty-when unconditional match.** For any rule with `r.when == {}` and any evidence, `_rule_matches` returns True. **Validates: Requirements 1.7**
    - **Property 5: Unknown condition key raises `RuleMatchError`.** For any rule whose `when` contains a key outside the five recognized names, `_rule_matches` raises `RuleMatchError` naming the offending key and rule; no other exception type escapes. **Validates: Requirements 7.1**
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 7.1_

- [x] 6. Implement `load_policy` and `default_policy` in `trikon/policy/loader.py`

  - [x] 6.1 Replace the `load_policy` and `default_policy` stubs with the full 5-step algorithm
    - `load_policy(repo_path, policy_path)` per `design.md §6.1`: resolve `policy_path` against `repo_path` when relative, return `default_policy()` when the resolved file does not exist (Requirement 2.2, no exception on the missing-file path), `resolved.read_text(encoding="utf-8")` under `except OSError → PolicyLoadError`, `yaml.safe_load(raw)` under `except yaml.YAMLError → PolicyLoadError` (Requirement 2.4), raise `PolicyLoadError` when the parsed mapping is `None` (empty file), `Policy.model_validate(parsed)` under `except pydantic.ValidationError → PolicyLoadError` (Requirement 2.3), return the validated `Policy`.
    - `default_policy()` per `design.md §6.2`: locate the packaged YAML via `importlib.resources.files("trikon.policy") / "default_policy.yaml"`, read via `resource.read_text(encoding="utf-8")`, parse and validate under the same YAML/Pydantic error-wrapping discipline; any failure surfaces as `PolicyLoadError` and indicates a broken wheel (Requirement 2.5).
    - Every foreign exception (`OSError`, `yaml.YAMLError`, `pydantic.ValidationError`, `ModuleNotFoundError`, `FileNotFoundError`) is caught at the module boundary and re-raised as `PolicyLoadError` with the original on `__cause__` (Requirement 7.1). Consumes the errors module from Task 1.1.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ]* 6.2 Write unit + property tests in `tests/unit/policy/test_loader.py`
    - Happy path: on-disk `.trikon/policy.yaml` with a valid 6-rule policy → returned `Policy` is Pydantic-equal (`model_dump()` byte-identical) to the parsed YAML.
    - Missing file → `default_policy()` (Requirement 2.2). Assert `load_policy(repo, Path("/tmp/does_not_exist.yaml")).model_dump() == default_policy().model_dump()`.
    - Malformed YAML (unbalanced quotes, tabs mixed) → `PolicyLoadError` with `yaml.YAMLError` on `__cause__` (Requirement 2.4).
    - Empty file (zero bytes) → `PolicyLoadError` (`parsed is None` branch).
    - `pydantic.ValidationError`: missing `rules` field → `PolicyLoadError` with `ValidationError` on `__cause__` (Requirement 2.3); wrong type on `rules` (`rules: "not a list"`) → same; `version: 99` → same.
    - `OSError` on read: monkeypatch `Path.read_text` to raise `PermissionError` → `PolicyLoadError` with `OSError` on `__cause__`.
    - `default_policy()` happy path: `default_policy().rules` has length 6 and the rule names match the packaged YAML.
    - Broken wheel simulation: monkeypatch `importlib.resources.files` to raise `ModuleNotFoundError` → `PolicyLoadError`.
    - **Property 6: Default-policy round-trip equivalence.** For any invocation, `default_policy().model_dump() == Policy.model_validate(yaml.safe_load(importlib.resources.files("trikon.policy").joinpath("default_policy.yaml").read_text())).model_dump()`; further, `load_policy(repo, Path("does_not_exist.yaml")).model_dump() == default_policy().model_dump()`. **Validates: Requirements 2.2, 2.5, 5.4**
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 7. Extend `evaluate_policy` with warn accumulation and preserve first-match-terminal semantics

  - [x] 7.1 Add the warn-accumulation branch and populate `Verdict.warnings` in `trikon/policy/evaluator.py::evaluate_policy`
    - Extend the existing terminal-first loop from Phase 0 with the three deltas in `design.md §7.1`: initialize `warnings: list[str] = []` at the top of the function; inside the per-rule loop, after the `RuleResult` append, `continue` when `matched is False`; branch on `rule.then == "warn"` to append `rule.reason or f"Rule '{rule.name}' warned."` to `warnings` and `continue` (non-terminal); otherwise the existing `matched and rule.then in TERMINAL and decision is None` gate assigns `decision`, `matched_rule_name`, and `reason` for the first-match-terminal winner (Requirement 3.1).
    - `Verdict` construction passes `warnings=warnings` to the Pydantic model; `schema_version=2` picks up the model default from Task 2.2 (no explicit set here).
    - Fall-through unchanged: when `decision is None` at end of loop, `decision = "require_human"`, `matched_rule = None`, `reason = "No rule matched; defaulting to require_human."` (Requirement 3.2).
    - Every `RuleResult` in `evidence.policy_results` gets `matched`, `would_emit`, and `reason` populated regardless of whether the rule fired — `len(evidence.policy_results) == len(policy.rules)` in declaration order (Requirement 3.4).
    - `_fail_closed_verdict` is untouched — it constructs its own Verdict without going through `evaluate_policy`, and its `warnings` list is `[]` by model default because the policy engine did not run (§7.4).
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [ ]* 7.2 Write unit + property tests in `tests/unit/policy/test_evaluator_terminal.py`
    - First-match-terminal wins (Property 2): two terminal rules both match — the earlier `then` becomes `decision`; the later match still appears in `evidence.policy_results` with `matched=True` and `would_emit=<its then>` but does not overwrite the winner.
    - Fall-through (Requirement 3.2): no rule matches → `decision == "require_human"`, `matched_rule is None`, `reason.startswith("No rule matched")`.
    - Warn accumulation (Property 3): three warn rules match at positions 2, 4, 5 in `policy.rules` → `verdict.warnings` equals the reasons of rules 2, 4, 5 in that order; a warn rule with `reason=None` contributes `"Rule '<name>' warned."` verbatim.
    - Warn-only match sets no terminal decision: a policy with only warn rules matching → `decision == "require_human"` (fall-through), `matched_rule is None`, `warnings` non-empty.
    - Every rule leaves a trace (Requirement 3.4): `len(verdict.evidence.policy_results) == len(policy.rules)`, entries appear in declaration order, `RuleResult.would_emit` is `None` when `matched is False` and equals `rule.then` (including `"warn"`) when `matched is True`.
    - Fail-closed `warnings` is `[]`: `_fail_closed_verdict(reason="…")` returns a Verdict with `warnings == []` and `schema_version == 2` (regression against §7.4).
    - **Property 2: First-match-terminal wins.** For any Policy and any evidence, if at least one rule with terminal `then` matches, `verdict.decision` equals the `then` of the first such rule in declaration order and `matched_rule` equals its name. **Validates: Requirements 3.1**
    - **Property 3: Warn accumulation preserves declaration order.** `Verdict.warnings` equals `[r.reason or f"Rule '{r.name}' warned." for r in policy.rules if r.then == "warn" and _rule_matches(r, change, verification)]` in declaration order. **Validates: Requirements 3.3**
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 8. Implement `record_verdict` — the append-only audit-log writer

  - [x] 8.1 Create `trikon/audit_log/writer.py` and re-export from `trikon/audit_log/__init__.py`
    - `record_verdict(conn, verdict)` per `design.md §8.1`: single-statement `conn.execute(_INSERT_AUDIT_LOG, (str(verdict.audit_id), verdict.created_at.isoformat(), verdict.decision, verdict.matched_rule, verdict.reason, verdict.model_dump_json()))` followed by `conn.commit()`, all inside one `try / except sqlite3.Error as exc: raise AuditLogError(...) from exc` closure.
    - `_INSERT_AUDIT_LOG` is a module-level constant string (never composed dynamically) so the AST scan for `UPDATE|DELETE|DROP` (Property 7) has a single well-known target to grep. Includes the six columns from `design.md §4.1`.
    - Extend `trikon/audit_log/__init__.py` from Task 4.1 with `from trikon.audit_log.writer import record_verdict` and set `__all__ = ["ensure_audit_tables", "record_verdict"]` — exactly two re-exports, no more (Requirement 4.3). No `update_*`, `delete_*`, `truncate_*`, or `purge_*` function is defined anywhere in `trikon/audit_log/**`.
    - _Requirements: 4.1, 4.2, 4.5, 7.3_

  - [ ]* 8.2 Write unit tests + append-only closure test in `tests/unit/audit_log/test_writer.py` and `tests/unit/audit_log/test_append_only_closure.py`
    - Happy path: one Verdict in, one row out. Assert every column value matches the corresponding `Verdict` field (`audit_id` as string, `created_at` as ISO-8601 UTC, `decision`, `matched_rule` — `None` → SQL `NULL`, `reason`, `verdict_json`).
    - `verdict_json` round-trip: `Verdict.model_validate_json(row.verdict_json).model_dump() == verdict.model_dump()` — the JSON column is a lossless serialization at `schema_version == 2` (Requirement 4.2).
    - Duplicate `audit_id`: second INSERT with the same UUID raises `sqlite3.IntegrityError` on the PRIMARY KEY, wrapped as `AuditLogError` with `IntegrityError` on `__cause__` (design matrix row #13).
    - Fault injection: monkeypatch `conn.execute` to raise `sqlite3.OperationalError` (disk full simulation) → `AuditLogError` chained (design matrix row #6). Monkeypatch `conn.commit` to raise the same → same treatment.
    - `verdict_json` shape at fail-closed: pass in a Verdict from `_fail_closed_verdict` (`decision == "require_human"`, `warnings == []`, `evidence.change == EMPTY_IMPACT_SET`, `evidence.verification == EMPTY_VERIFICATION`) → the row lands with the full JSON payload (Requirement 7.3).
    - `test_append_only_closure.py` — **Property 7: Audit-log append-only closure (static).** Static AST scan of `trikon/audit_log/**` for `str` literals containing SQL verbs `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE` (case-insensitive, matched as whole SQL tokens). Only `INSERT`, `CREATE`, and `SELECT` (for smoke-test count queries in tests, if any land inside the production tree) are permitted. Assert `trikon.audit_log.__all__ == ["ensure_audit_tables", "record_verdict"]`. **Validates: Requirements 4.3**
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 7.3_

- [x] 9. Implement `format_markdown` — the full 7-block GitHub-flavored layout

  - [x] 9.1 Replace the one-liner stub in `trikon/evidence/formatters/markdown.py` with the full block layout
    - Six blocks joined by `"\n\n"` per `design.md §12.1`: Header (decision icon from `{"allow": "✅", "block": "🛑", "require_human": "🔍", "warn": "⚠️"}` + `DECISION_UPPER` + backtick-wrapped `matched_rule` or literal `` `<no rule matched>` `` when `None`; reason on a `>` blockquote line); "Focus your review on" (up to `min(5, len(changed_files))` backtick-wrapped file paths from `verdict.evidence.change.changed_files` in list order; on an empty list, a single italic bullet `- _no files_` keeps the section shape stable); "Impact" (four bullets — `len(changed_files)`, `len(impacted_modules)`, `len(impacted_public_apis)`, and `f"{blast_radius_score} ({blast_radius_numeric:.2f})"`); "Verification" (`f"{passed} passed · {failed} failed · {skipped} skipped"` top line, then up to 5 failing entries as `` - `<node_id>` — <failure_summary> `` — dash-separator omitted when `failure_summary is None`; sub-list header omitted when `failures` is empty); "Warnings" (**conditional** — emitted iff `verdict.warnings` non-empty; every warning verbatim in list order, no truncation); Footer (`---\n_Audit id_: `<uuid>` · _Schema version_: `<int>``).
    - Pure function: no I/O (`open`, `read_text`, `write_text`, `socket`, HTTP), no template engine (`jinja2`, `mako`, `chevron`, `string.Template`); only stdlib and the already-imported Pydantic models. `str.format`, f-strings, `"\n".join`, and list slicing carry the whole thing (Requirement 6.6).
    - Handle empty impact set gracefully: `EMPTY_IMPACT_SET` renders Impact with `0 · 0 · 0 · HIGH (1.00)`; Focus renders the single italic `- _no files_` bullet; Verification renders `0 passed · 0 failed · 0 skipped` with the failing-tests sub-list omitted. Fail-closed rendering never crashes.
    - Handle empty warnings: `verdict.warnings == []` → the entire `## Warnings` block is omitted; no empty header, no dangling separator.
    - Handle empty failures: `verdict.evidence.verification.tests.failures == []` → the "First 5 failing tests" sub-list header is omitted; the pass/fail/skip top line stands alone.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ]* 9.2 Write unit tests + purity test in `tests/unit/evidence/formatters/test_markdown.py` and `tests/unit/evidence/formatters/test_markdown_purity.py`
    - Header for each of `allow`, `block`, `require_human` — the icon matches, the backtick-wrapped rule name appears, the reason appears on a `>` blockquote line.
    - Fall-through header: `matched_rule is None` → the literal string `` `<no rule matched>` `` appears in the header.
    - "Focus your review on" — parametrize `changed_files` over lengths 0, 1, 5, 10: length 0 renders the `- _no files_` bullet; length 10 slices to 5 in list order (no sort, no dedup); every path is backtick-wrapped.
    - "Impact" — every bullet appears; blast-radius line contains bucket + numeric with two-decimal precision (`f"{score:.2f}"`).
    - "Verification" pass/fail/skip counts appear with `·` separators; failure sub-list appears iff `failures` non-empty; slice to 5; entry without `failure_summary` omits the ` — ` suffix.
    - "Warnings" — absent iff `warnings == []`; present with every warning verbatim in list order when non-empty; no truncation for lists ≤ 100.
    - Footer — `audit_id` and `schema_version` both appear; every emitted Verdict carries `schema_version == 2`.
    - `test_markdown_purity.py` — grep the source of `trikon/evidence/formatters/markdown.py` for forbidden imports (`jinja2`, `mako`, `chevron`, `string.Template`, `open`, `Path(...).read_text()`, `socket`, `httpx`, `requests`). Fail if any appear in the module source (Requirement 6.6).
    - **Property 11: Markdown-section completeness.** For any well-formed Verdict, `format_markdown` returns a string containing, in order: the header (icon + `matched_rule` display + reason), the "Focus your review on" section (up to 5 paths in list order), the "Impact" section (four counts + blast-radius bucket + numeric), the "Verification" section (pass/fail/skip + up to 5 failing node IDs with `failure_summary`), a "Warnings" section iff `warnings` non-empty, and a footer with `audit_id` and `schema_version`. No template engine and no I/O call appears in the formatter's source. **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6**
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [x] 10. Wire the SDK — `sdk.verify` runs the full pipeline and persists an audit row on every return path

  - [x] 10.1 Widen the fail-closed except clause to `TrikonError` and add `load_policy` + `evaluate_policy` calls inside the try block in `trikon/sdk.py::verify`
    - Change `except (ChangeIntelError, VerificationRunnerError) as exc:` to `except TrikonError as exc:` per `design.md §10 AFTER` — all three subsystem base classes now inherit from `TrikonError` (Phase-1 re-parenting + Phase-2 addition + Phase-3 addition via `PolicyEvaluationError(TrikonError)` from Task 1.1), so a single catch clause suffices.
    - Add the policy stage inside the same `try` block, immediately after `run_verification`: `policy = load_policy(repo, Path(policy_path))` followed by `verdict = evaluate_policy(policy, impact, verification)`. Retire the hardcoded `decision="require_human"` and the `_PHASE_2_REASON` constant — the Verdict returned on the happy path is now the one `evaluate_policy` produced (real decision, real matched_rule, real reason, real warnings).
    - Update the fail-closed reason string to name the failing subsystem via `type(exc).__name__` — `_fail_closed_verdict(reason=f"{type(exc).__name__}: {exc}")` — so audit reviewers can distinguish `PolicyLoadError`, `RuleMatchError`, `ChangeIntelError`, and `VerificationRunnerError` from the persisted `reason` column.
    - `_fail_closed_verdict` is unchanged in shape — Phase 3 changes only the `Verdict` model defaults (`schema_version=2`, `warnings=[]`), which the helper picks up automatically (`design.md §10.1`, §7.4).
    - _Requirements: 5.1, 7.1, 7.2, 7.4_

  - [x] 10.2 Add the audit-log writer call outside the try/except in `trikon/sdk.py::verify`
    - After the `try`/`except` block completes (either happy path or fail-closed path both bind `verdict`), open a `sqlite3.connect(str(state_db))` connection where `state_db = cache_db if cache_db is not None else repo / ".trikon" / "state.db"`; create the parent directory via `state_db.parent.mkdir(parents=True, exist_ok=True)`; call `ensure_audit_tables(conn)` (idempotent — Task 4.1) then `record_verdict(conn, verdict)` (Task 8.1); close the connection in a `finally`.
    - `record_verdict` is called **outside** the `try`/`except TrikonError` block per Requirement 4.5 — an `AuditLogError` from this call re-raises to the caller rather than being caught and swallowed. A verdict without an audit trail is a hard failure, not a silent degrade (`design.md §8.2`, §9.1, matrix row #6).
    - Modifies `sdk.py`; different wave from Task 10.1 to satisfy same-file wave discipline.
    - _Requirements: 4.5, 7.3_

  - [ ]* 10.3 Write SDK unit tests in `tests/unit/test_sdk_verify_phase3.py`
    - Happy path: monkeypatch `run_verification` and `evaluate_policy` to return populated evidence and a `decision="allow"` Verdict; assert `sdk.verify(...)` returns that Verdict verbatim and a row lands in `audit_log` with the matching `audit_id`.
    - `PolicyLoadError` catch: monkeypatch `load_policy` to raise `PolicyLoadError`; assert the returned Verdict has `decision == "require_human"`, `evidence.change == EMPTY_IMPACT_SET`, `evidence.verification == EMPTY_VERIFICATION`, `reason.startswith("PolicyLoadError:")`, and the audit row is still written on the fail-closed path (Requirement 7.3).
    - `RuleMatchError` catch: monkeypatch `evaluate_policy` to raise `RuleMatchError`; same fail-closed shape.
    - `VerificationRunnerError` catch: same shape (Phase-2 regression preserved).
    - `ChangeIntelError` catch: same shape (Phase-1 regression preserved).
    - `AuditLogError` re-raises: monkeypatch `record_verdict` to raise `AuditLogError`; assert `sdk.verify(...)` raises rather than returning a Verdict (Requirement 4.5, hard failure).
    - `allow` is never emitted on any error path — for every `TrikonError` subclass raised inside the try block, the returned decision is `require_human`.
    - **Property 8: Audit row lands on every SDK return path.** For any `sdk.verify(...)` that returns a Verdict, `SELECT COUNT(*) FROM audit_log WHERE audit_id = ?` with the returned Verdict's `audit_id` yields `1`; `Verdict.model_validate_json(SELECT verdict_json ...)` reproduces the returned Verdict's `model_dump()`. **Validates: Requirements 4.1, 4.2, 7.3**
    - **Property 9: Fail-closed at the SDK boundary — never `allow`.** For any `TrikonError` subclass except `AuditLogError`, the returned Verdict has `decision == "require_human"` and both sentinels; `AuditLogError` re-raises. **Validates: Requirements 7.1, 7.2, 7.4**
    - _Requirements: 5.1, 7.1, 7.2, 7.3, 7.4, 4.5_

- [x] 11. Wire the CLI — `trikon verify` and `trikon init`, and preserve `trikon debug verify`

  - [x] 11.1 Replace the `trikon verify` stub in `trikon/cli.py` with the full command body
    - Signature per `design.md §11.1`: `verify(repo: Path, base: str | None, head: str | None, diff_file: Path | None, policy: Path = Path(".trikon/policy.yaml"), output: str = "markdown")`. Validate `output in ("markdown", "json")`, else print usage error to stderr and `raise typer.Exit(code=2)`.
    - Body: read `diff_file` when passed, call `sdk_verify(repo, base_sha=base, head_sha=head, diff=diff_content, policy_path=policy)`, print `verdict.model_dump_json(indent=2)` when `output == "json"` else `format_markdown(verdict)` (Task 9.1), then `raise typer.Exit(code=_EXIT_CODE_FOR[verdict.decision])`.
    - `_EXIT_CODE_FOR: dict[str, int] = {"allow": 0, "block": 1, "require_human": 2, "warn": 2}` — `warn` mapped to `2` for forward compat even though `sdk.verify` never emits it (§11.1 note).
    - Replace the `raise NotImplementedError` stub from Phase 2; preserve `trikon debug verify` as the developer diagnostic surface — Requirement 5.5 is explicit that `debug verify` always exits `0`, and its Phase-2 body is untouched by Phase 3 (its output does get richer since the Verdict now carries a real `decision`, `matched_rule`, and `warnings`, but the exit-code contract is unchanged).
    - _Requirements: 5.1, 5.2, 5.3, 5.5_

  - [x] 11.2 Add the `trikon init` command in `trikon/cli.py`
    - Signature per `design.md §11.2`: `init(repo: Path = Path.cwd(), force: bool = False)`. Body: `target = repo / ".trikon" / "policy.yaml"`; refuse-overwrite guard (`target.exists() and not force` → print `Error: {target} already exists. Use --force to overwrite.` to stderr and `raise typer.Exit(code=1)`); `target.parent.mkdir(parents=True, exist_ok=True)`; read the packaged YAML via `importlib.resources.files("trikon.policy") / "default_policy.yaml"` under `except (OSError, FileNotFoundError)` → error message + exit 1 (broken wheel diagnostic); `target.write_text(content, encoding="utf-8")`; `typer.echo(f"Wrote {target}")`; exit 0.
    - Exit-code contract per `design.md §11.2` table: success → 0; existing file without `--force` → 1; missing packaged resource → 1; Typer usage error → 2.
    - Different wave from Task 11.1 to satisfy same-file (`cli.py`) wave discipline.
    - _Requirements: 5.4_

  - [ ]* 11.3 Write CLI unit tests in `tests/unit/cli/test_verify_and_init.py`
    - `typer.testing.CliRunner` invocation: `trikon verify --repo <tmp> --base <sha> --head <sha>` prints the six-block markdown output (assert every section header appears in the stdout in order); exit code equals `_EXIT_CODE_FOR[verdict.decision]`.
    - `--output json` flag: stdout is valid JSON parsable to a Verdict-shaped dict with `decision`, `evidence`, `audit_id`, `created_at`, `warnings`, `schema_version == 2`; exit code still decision-based (Requirement 5.3).
    - Exit codes parametrized per Requirement 5.2: mock `sdk.verify` to return `decision="allow"` → exit 0; `"block"` → exit 1; `"require_human"` → exit 2.
    - `--output` validation: `--output xml` → exit 2 with a usage message on stderr.
    - `trikon init` happy path: fresh temp repo → `.trikon/policy.yaml` exists after the call and is byte-identical to `importlib.resources.files("trikon.policy") / "default_policy.yaml"`; exit code 0.
    - `trikon init` refuse-overwrite: `.trikon/policy.yaml` already present, no `--force` → exit 1, file unchanged.
    - `trikon init --force`: exit 0, file overwritten with packaged content.
    - `trikon debug verify` regression: mock `sdk.verify` to return `decision="block"` → exit code stays `0` (Requirement 5.5, distinct from `trikon verify`).
    - **Property 10: Exit-code semantics parametrized by decision and output format.** For any decision `d ∈ {"allow","block","require_human"}` and any `--output` value in `{"markdown","json"}`, the process exit code of `trikon verify` equals `{"allow":0,"block":1,"require_human":2}[d]`; the mapping is independent of `--output`. `trikon debug verify` always exits `0`. **Validates: Requirements 5.2, 5.3, 5.5**
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

- [ ] 12. Integration tests against `examples/sample_repo/`

  - [ ]* 12.1 Write `tests/integration/policy/test_sample_repo_bad_retry.py`
    - Apply `tests/fixtures/scenarios/bad_retry.patch` to a fresh temp clone of `examples/sample_repo/`, commit both revisions, call `sdk.verify(repo, base_sha, head_sha)`.
    - Assert `verdict.decision == "block"`, `verdict.matched_rule == "impacted tests failed"`, and `verdict.reason.startswith("One or more impacted tests failed.")` — matches the default policy's terminal rule for impacted-test failures (`design.md §14.3`).
    - Assert `verdict.evidence.verification.tests.status == "failed"` (Phase-2 regression) and the failing `TestResult.node_id` set includes `tests/test_worker.py::test_backoff_shape`.
    - Assert the `audit_log` table contains exactly one new row with `verdict.audit_id`.
    - Marker `@pytest.mark.integration`, requires a live Docker daemon (inherited from Phase-2 sandbox); skipped by default in the CI unit workflow.
    - _Requirements: 1.1, 1.4, 3.1, 4.1, 5.1_

  - [ ]* 12.2 Write `tests/integration/policy/test_sample_repo_clean_refactor.py`
    - Apply `tests/fixtures/scenarios/clean_refactor.patch` to a fresh temp clone, call `sdk.verify(...)`.
    - Assert `verdict.decision == "allow"`, `verdict.matched_rule == "green, low-blast auto-allow"` — matches the default policy's LOW-blast + tests-passed auto-allow rule.
    - Assert `verdict.evidence.change.blast_radius_score == "LOW"` and `verdict.evidence.verification.tests.status == "passed"`.
    - `verdict.warnings` may be empty or contain rule-driven notices — do not over-constrain; assert only that no `error` field is populated on any `PluginResult`.
    - Assert the audit row lands.
    - Marker `@pytest.mark.integration`.
    - _Requirements: 1.1, 1.3, 1.4, 3.1, 4.1, 5.1_

  - [ ]* 12.3 Write `tests/integration/policy/test_sample_repo_sensitive.py`
    - Apply a small patch touching a file under `examples/sample_repo/src/api/payments.py` (a `sensitive_paths` entry from the default policy), call `sdk.verify(...)`.
    - Assert `verdict.decision == "require_human"`, `verdict.matched_rule == "sensitive path requires human"` — matches the default policy's `any_path_matches: ["payments/**", "auth/**", "billing/**"]` terminal rule.
    - Assert the rule fires **regardless** of `verification.tests.status` — a sensitive-path change with green tests still requires a human.
    - Assert the audit row lands.
    - Marker `@pytest.mark.integration`.
    - _Requirements: 1.1, 3.1, 4.1, 5.1_

  - [ ]* 12.4 Write `tests/integration/policy/test_never_fail_open.py`
    - Fault-inject `PolicyEvaluationError` subclasses at each raise site and assert the SDK boundary produces `require_human` + persists the audit row (Property 9). Four parametrized sub-cases:
      1. Monkeypatch `parse_diff` to raise `ChangeIntelError` (Phase-1 subsystem) → `decision=="require_human"`, `evidence.change == EMPTY_IMPACT_SET`, audit row present.
      2. Monkeypatch `load_policy` to raise `PolicyLoadError` → same.
      3. Monkeypatch `evaluate_policy` internal to raise `RuleMatchError` → same.
      4. Monkeypatch `record_verdict` to raise `AuditLogError` → `AuditLogError` propagates past `sdk.verify` (hard failure per Requirement 4.5); no verdict returned; no silent degrade.
    - Assert `decision == "allow"` never appears in any of the four fault-injected outcomes (Requirement 7.4).
    - Marker `@pytest.mark.integration`.
    - **Property 9: Fail-closed at the SDK boundary — never `allow`.** For any `TrikonError` subclass except `AuditLogError`, `sdk.verify` returns `require_human` + `EMPTY_IMPACT_SET` + `EMPTY_VERIFICATION`; `allow` is never emitted on this path. **Validates: Requirements 7.1, 7.2, 7.4**
    - _Requirements: 4.5, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 12.5 Write `tests/integration/policy/test_trikon_init.py`
    - End-to-end `trikon init` + `trikon verify` round-trip via `CliRunner` in a temp repo:
      1. First `trikon init --repo <tmp>` → `.trikon/policy.yaml` created, byte-identical to the packaged `default_policy.yaml` (Property 6).
      2. Second `trikon init` without `--force` → exit code 1, file unchanged.
      3. Second `trikon init --force` → exit code 0, file overwritten with packaged content.
      4. Subsequent `trikon verify --repo <tmp> --base HEAD~1 --head HEAD` uses the freshly-written policy (assert the same 6 rule names appear in `verdict.evidence.policy_results`).
    - Marker `@pytest.mark.integration`.
    - _Requirements: 2.5, 5.4_

- [ ] 13. Perf smoke tests — Requirement 8 wall-clock ceilings

  - [ ]* 13.1 Author `tests/benchmarks/test_policy_perf.py` with three `pytest-benchmark` cases
    - Each case is `@pytest.mark.perf`, opt-in; runs in the nightly `perf` job (Task 15.3), not the PR-blocking unit workflow.
      - `evaluate_policy` on the 6-rule default policy with `change` and `verification` derived from `examples/sample_repo/`, target ≤ 100 ms, CI fail > 100 ms. **Validates: Requirements 8.1**
      - `load_policy` on a real on-disk `.trikon/policy.yaml` up to 10 KB in size, target ≤ 50 ms, CI fail > 50 ms. **Validates: Requirements 8.2**
      - `format_markdown` on a fully-populated Verdict (matched rule, three warnings, non-empty impact set with 5 files, non-empty verification report with 5 failing tests), target ≤ 10 ms, CI fail > 10 ms. **Validates: Requirements 8.3**
    - Baseline files stored in `tests/benchmarks/.benchmarks/`; regressions > 20 % against baseline fail the assertion.
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 14. Documentation — end-user guide, MDX pages, CHANGELOG, README

  - [x] 14.1 Author `docs/policy.md` — the end-user guide for the policy DSL
    - New file covering: what Phase 3 does (turn `ImpactSet` + `VerificationReport` into a terminal `Verdict`), the five condition types with worked YAML examples for each (`any_path_matches`, `no_path_matches`, `change.blast_radius.score`, `verification.tests.status`, `verification.static.new_errors`), the four `then` values (`allow`, `block`, `require_human`, `warn`) with first-match-terminal semantics explained, warn accumulation into `Verdict.warnings`, and the missing-file fallback to the packaged `default_policy.yaml`.
    - Show the expected `Verdict` JSON for the `bad_retry`, `clean_refactor`, and `sensitive` sample_repo scenarios so a reader can eyeball what "correct" looks like.
    - Cross-link to `docs/change_intel.md` (Phase 1) and `docs/verification.md` (Phase 2).
    - _Requirements: 5.1, 5.4, 6.1_

  - [x] 14.2 Author three Mintlify MDX pages under `docs-site/policy/` and update `docs-site/docs.json`
    - `overview.mdx`: what the policy engine does, the never-fail-open + first-match-terminal + append-only-audit invariants, and where it fits in the Trikon pipeline (parse_diff → compute_impact → run_verification → load_policy → evaluate_policy → record_verdict).
    - `authoring.mdx`: policy DSL reference — full `Policy` schema, every condition type with a worked example, warn-vs-terminal semantics, worked example ending in a `require_human` fall-through rule for policy hygiene.
    - `troubleshooting.mdx`: common failure modes — malformed YAML surfaces as `PolicyLoadError`, unknown condition key surfaces as `RuleMatchError`, missing packaged `default_policy.yaml` (broken wheel), audit-log disk-full failures (`AuditLogError` re-raises), `trikon verify` exit-code interpretation for CI jobs.
    - Match the Phase-1/2 Mintlify structure and frontmatter shape used by `docs-site/concepts/*.mdx` and `docs-site/pages/verification/*.mdx`. Update `docs-site/docs.json` nav to add a "Policy" section with the three new pages.
    - _Requirements: 5.1, 6.1_

  - [x] 14.3 Update `CHANGELOG.md` with the Phase 3 section
    - New `## [0.3.0] — Phase 3: Policy Engine` heading enumerating every user-visible surface: `trikon verify` command (decision-based exit codes 0/1/2, `--output markdown|json`), `trikon init` command (packaged `default_policy.yaml` scaffold, `--force` guard), `sdk.verify` returning a real evaluated Verdict with `matched_rule`, `reason`, and `warnings` populated, the new `audit_log` SQLite table, the four-value `Decision` widening + `Verdict.warnings` + `schema_version` bump `1 → 2`, the `PolicyEvaluationError` hierarchy, the `format_markdown` 7-block layout, and the never-fail-open + append-only-audit invariants.
    - Cross-link the release to `docs/policy.md` for detail.
    - _Requirements: 5.1, 6.1_

  - [x] 14.4 Extend `README.md`'s "What Trikon does today" section
    - Add a "Policy Engine (Phase 3)" bullet naming the terminal `decision` + `matched_rule` + `reason` + `warnings` emission, running in an append-only `audit_log` SQLite table, with `trikon verify --repo examples/sample_repo --base HEAD~1 --head HEAD` as the one-line demo (exit code 1 on `bad_retry`, 0 on `clean_refactor`, 2 on a sensitive-path change).
    - Add `trikon init` under the Quickstart section as the "scaffold a starter policy" step.
    - Rewrite the "What Trikon does today" summary line to name the policy engine explicitly instead of the Phase-2 "hardcoded require_human" placeholder.
    - _Requirements: 5.1, 5.4_

- [x] 15. Coverage and lint gates — enforce 85 % branch coverage and mypy-strict cleanliness

  - [x] 15.1 Extend `pyproject.toml` coverage config
    - Extend `[tool.coverage.report]` `include` filter to add `"trikon/policy/*"` and `"trikon/audit_log/*"`; set `fail_under = 85` (same floor as Phase-2's `trikon/verify/**`, documented in `design.md §14.5`: the 5 % gap under Phase-1's 90 % floor is reserved for the `except OSError` defensive branches in `load_policy` and the defensive `PolicyEvaluationError` raise in `evaluate_policy`).
    - Extend `[tool.coverage.run]` `source` list to include `"trikon.policy"` and `"trikon.audit_log"`.
    - Different wave from Task 3.2 to satisfy same-file (`pyproject.toml`) wave discipline.
    - _Requirements: 7.1_

  - [x] 15.2 Achieve `mypy --strict trikon/policy/ trikon/audit_log/` cleanliness and `ruff check` / `ruff format --check` cleanliness
    - Run `mypy --strict trikon/policy/ trikon/audit_log/` and fix any red squiggles introduced across Tasks 1–11 (`disallow_any_explicit=true` is the Phase-1 config default — no `dict[str, Any]` on any public parameter or return type).
    - Run `ruff check trikon/policy/ trikon/audit_log/` and `ruff format --check trikon/policy/ trikon/audit_log/`; fix any remaining findings introduced across Tasks 1–11.
    - No new `[tool.mypy]` `ignore_missing_imports` overrides are expected — `trikon.policy` and `trikon.audit_log` consume only stdlib, `pydantic`, and `yaml` (already declared for Phase 0).
    - The exit-0 outcome of `mypy --strict trikon/policy/ trikon/audit_log/` and `ruff check trikon/policy/ trikon/audit_log/` is the gate.
    - _Requirements: 7.1_

  - [x] 15.3 Update `.github/workflows/ci.yml` to include the new paths and register the perf/integration jobs
    - The Phase-1 `lint-type-test` job's `trikon/ tests/` glob already covers `trikon/policy/` and `trikon/audit_log/`; verify by inspecting the workflow file and add explicit path entries only if the glob is narrower than expected. Extend the mypy invocation with `trikon/policy/ trikon/audit_log/` explicitly to match the Phase-2 pattern.
    - Extend the nightly `perf` job (added by Phase-2 Task 14.3) to also run `pytest tests/benchmarks/test_policy_perf.py -m perf --benchmark-only --benchmark-max-time=1` — the Requirement 8 ceilings are much tighter (100 ms / 50 ms / 10 ms) than Phase 2's minute-scale ceilings, so `--benchmark-max-time=1` is sufficient.
    - Extend the `integration` sub-job (added by Phase-2 Task 14.3) to also run `pytest tests/integration/policy/ -m integration` with the same Docker setup.
    - Gate on the Requirement 8 wall-clock ceilings from Task 13.1.
    - _Requirements: 7.1, 8.1, 8.2, 8.3_

## Notes

- Sub-tasks marked `*` are optional in the sense of Kiro's "skip for a fast MVP" convention. In practice, every `*` test task is required to hit the Definition of Done — the marker signals "test code, not implementation code", not "throwaway".
- Each property test cites its numbered Correctness Property from `design.md §17` and the specific `Requirements` clause it validates, so traceability from acceptance criterion → property → test file is one grep.
- Wave-1 tasks (1–4) establish the foundation: exception hierarchy, `Decision`/`Verdict` widening + `schema_version` bump, packaged `default_policy.yaml`, `audit_log` SQLite DDL. They land in parallel and unblock everything else.
- Wave-2 tasks (5–9) are the five subsystem modules: rule dispatch, policy loader, warn-accumulating evaluator, audit-log writer, and Markdown formatter. Same-file wave discipline separates `evaluator.py`'s dispatcher (Task 5.1) from its warn-accumulation edit (Task 7.1), and `sdk.py`'s exception-widening (Task 10.1) from its audit-write (Task 10.2), across the dependency graph.
- Wave-3 tasks (10–11) fuse the adapters into `sdk.verify` and wire the two new CLI commands. Task 10 depends on Tasks 5–9; Task 11 depends on Task 9 (`format_markdown`) and Task 10 (`sdk.verify` pipeline).
- Wave-4 tasks (12–15) are release-layer: integration tests against `sample_repo`, perf benchmarks, docs, and coverage/lint gates. Every integration test carries `@pytest.mark.integration` so the PR-blocking unit workflow stays fast; perf tests carry `@pytest.mark.perf` and run nightly. The Phase-2 marker registration in `pyproject.toml` already covers both markers — no new marker registration is needed in Phase 3.
- Phase 3 is the terminal shipping increment for the policy layer. The next scoped work (hash-chained audit log, actor/time-of-day conditions, retention policy, remote policy sources, per-condition rule trace) is captured in `design.md §18` as out-of-scope for v0.1 and deferred to Phase 4+.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "4.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "4.2"] },
    { "id": 2, "tasks": ["1.3", "2.3", "3.3", "4.3"] },
    { "id": 3, "tasks": ["5.1", "6.1", "8.1", "9.1", "11.2"] },
    { "id": 4, "tasks": ["7.1", "8.2", "9.2", "10.1"] },
    { "id": 5, "tasks": ["5.2", "6.2", "7.2", "10.2"] },
    { "id": 6, "tasks": ["10.3", "11.1"] },
    { "id": 7, "tasks": ["11.3", "12.1", "12.2", "12.3", "12.4", "12.5", "13.1", "14.1", "14.2", "14.3", "14.4"] },
    { "id": 8, "tasks": ["15.1"] },
    { "id": 9, "tasks": ["15.2"] },
    { "id": 10, "tasks": ["15.3"] }
  ]
}
```
