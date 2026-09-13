# Implementation Plan: Verification Runner (Trikon Phase 2)

## Overview

Convert the frozen design in `design.md` into buildable, incremental coding tasks that layer `trikon/verify/**` on top of the Phase-1 change-intelligence tree. Each task ships with types, tests, and mypy-strict cleanliness at that step so the tree is green after every merge.

The build order is: shared exception root + verify data models + SQLite tables + sandbox image (foundation, Wave 1) → LocalDockerSandbox + test selector + static checks + plugin loader (adapters, Wave 2) → `run_verification` orchestrator + coverage builder + CLI + SDK wiring (integration, Wave 3) → integration tests + perf gates + docs + coverage/lint gates (release, Wave 4). Every task cites the requirements it validates (granular sub-clauses) and the exact files it creates or modifies. Test sub-tasks are postfixed with `*` per the Kiro convention; they may be skipped for a fast MVP but are required to hit the Definition of Done.

The subsystem holds three invariants at every green build: **never-fail-open** (every raise site under `trikon/verify/**` is a `VerificationRunnerError` subclass, and every uncaught path at the SDK boundary becomes `require_human`), **pytest-first** (test execution is the primary evidence surface; ruff, mypy, and plugin findings are additive), and **sandbox-isolated** (Docker daemon calls and `docker.errors.*` never escape `trikon/verify/sandbox.py`). Definition of Done: `trikon debug verify --repo examples/sample_repo --base HEAD~1 --head HEAD` produces `verification.tests.status == "failed"` after applying `tests/fixtures/scenarios/bad_retry.patch` and `== "passed"` after applying `tests/fixtures/scenarios/clean_refactor.patch`, end-to-end, in under 60 s cold on a modern developer laptop.

## Tasks

- [x] 1. Establish the shared exception root and Verification Runner error hierarchy

  - [x] 1.1 Create `trikon/exceptions.py` with the `TrikonError` root
    - New module: a single `TrikonError(Exception)` class with the module-level docstring from `design.md §9` explaining why the root is being lifted out of `trikon.change_intel.errors` and what fusing subsystems in Phase 3 means for the boundary.
    - Re-parent `trikon.change_intel.errors.ChangeIntelError` to inherit from `TrikonError` instead of `Exception`. Preserve every existing subclass unchanged — this is a superclass swap, not a rename.
    - Export `TrikonError` from `trikon/__init__.py` at the same visibility as `ChangeIntelError`.
    - _Requirements: 6.1_

  - [x] 1.2 Create `trikon/verify/errors.py` with `VerificationRunnerError` + subclasses
    - Implement the 7-class hierarchy from `design.md §9`: `VerificationRunnerError` (base), `SandboxUnavailableError`, `SandboxExecError`, `SandboxTimeoutError`, `TestSelectionError`, `StaticCheckError`, `PluginLoadError`, `CoverageBuildError`.
    - Each subclass carries the exact docstring from `design.md §9` (including the raise-site guidance for `SandboxUnavailableError`'s socket-path requirement and `SandboxTimeoutError`'s never-raised-past-boundary contract).
    - Re-export every class from `trikon/verify/__init__.py`. This is the only sanctioned raise vocabulary for `trikon/verify/**`.
    - _Requirements: 6.1, 6.2, 6.3_

  - [ ]* 1.3 Write unit tests in `tests/unit/verify/test_errors.py`
    - AST-scan test that walks every `Raise` node under `trikon/verify/**` (excluding `errors.py` itself) and asserts the raised type resolves to a subclass of `VerificationRunnerError`. Skip during Task 1; enable when downstream modules land.
    - Class-hierarchy assertions: `issubclass(SandboxUnavailableError, VerificationRunnerError)`, `issubclass(VerificationRunnerError, TrikonError)`, and same for `ChangeIntelError` (re-parented in 1.1).
    - Importability: `from trikon.verify import VerificationRunnerError, SandboxUnavailableError, …` succeeds for every subclass.
    - `TrikonError` root discoverability: `from trikon import TrikonError` succeeds.
    - _Requirements: 6.1, 6.2_

- [x] 2. Materialize the verify subsystem data models

  - [x] 2.1 Create `trikon/verify/models.py` with the frozen dataclasses
    - Implement `SelectedTests`, `SandboxExecResult`, `StaticTool`, and `CoverageBuildReport` exactly per `design.md §3.7` and the individual signatures in §3.2 / §3.3 / §3.4 / §3.6. All four `frozen=True, slots=True`.
    - Every field is fully typed with no `dict[str, Any]` on the public surface (Phase-1 convention).
    - Re-export every dataclass from `trikon/verify/__init__.py`.
    - _Requirements: 1.1, 3.1_

  - [x] 2.2 Add `DEFAULT_STATIC_TOOLS` tuple to `trikon/verify/models.py`
    - Two-entry tuple: ruff and mypy `StaticTool` values with the exact `argv_template`, `version_command`, and `parse_json` fields from `design.md §3.4`.
    - The tuple lives in `models.py` (not `static_checks.py`) so tests and CLI can import it without pulling in the sandbox module.
    - Re-export from `trikon/verify/__init__.py`.
    - _Requirements: 3.1_

  - [ ]* 2.3 Write unit tests in `tests/unit/verify/test_models.py`
    - Immutability: mutating any field on any of the four dataclasses raises `FrozenInstanceError`.
    - Slot layout: `__slots__` is populated; setting an undeclared attribute raises `AttributeError`.
    - JSON round-trip via `dataclasses.asdict(...)` followed by re-construction preserves every field byte-for-byte for representative values.
    - `DEFAULT_STATIC_TOOLS` has length 2 and the names are exactly `"ruff"` and `"mypy"` in that order.
    - _Requirements: 1.1, 3.1_

- [x] 3. Land the SQLite schema for `coverage_map`, `tests_seen`, and `static_baseline`

  - [x] 3.1 Create `trikon/verify/db.py` with `ensure_verify_tables`
    - Public function `ensure_verify_tables(conn: sqlite3.Connection) -> None` that executes the three `CREATE TABLE IF NOT EXISTS` statements from `design.md §4.1` (`coverage_map`, `tests_seen`, `static_baseline`) plus the four indexes on those tables.
    - Additive-only per `design.md §4.2`: no `ALTER` or `DROP` on Phase-1 tables.
    - Every raise is wrapped as `TestSelectionError` at this layer (the reader is `test_selector`) — no bare `sqlite3.Error` escapes.
    - _Requirements: 1.1, 1.3, 3.2, 3.3, 5.1, 5.2_

  - [x] 3.2 Wire `ensure_verify_tables` into `trikon.change_intel.cache.open_connection`
    - Add a `_migrate_verify_tables(conn)` call inside `open_connection` immediately after the existing Phase-1 migration hook, so both subsystems share bootstrap and the state DB is Phase-2-ready from the first `sqlite3.connect`.
    - Preserve the Phase-1 pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY`) unchanged.
    - No behavior change for Phase-1-only callers: `ensure_verify_tables` is idempotent by construction.
    - _Requirements: 1.1, 3.2, 5.1_

  - [ ]* 3.3 Write unit + property tests in `tests/unit/verify/test_db.py`
    - Table existence after `ensure_verify_tables`: query `sqlite_master` for each of the three tables and four indexes and assert the DDL matches `design.md §4.1`.
    - Unique-index shape on `(qualified_name, built_against_sha)` — a second `INSERT OR REPLACE` with the same tuple overwrites in place; an `INSERT` (no `OR REPLACE`) raises `IntegrityError`.
    - Foreign-key discipline: pragma `foreign_keys=ON` propagates through `open_connection` from Task 3.2.
    - Hypothesis property test for `test_ids_json` and `findings_json`: for any generated `list[str]` (node IDs) or list of finding-shaped dicts, `json.loads(json.dumps(x))` round-trips byte-identically and the written row reads back to an equal Python value.
    - **Property: Round-trip JSON payloads** — for any list-of-strings or list-of-dicts payload, `INSERT`ing the payload as JSON and reading it back yields the same Python object. **Validates: Requirements 5.1**
    - _Requirements: 1.1, 1.3, 3.2, 3.3, 5.1, 5.2_

- [x] 4. Ship the pinned sandbox Docker image

  - [x] 4.1 Author `Dockerfile.sandbox` at the repo root
    - Exact content from `design.md §5.1`: `python:3.11-slim@sha256:<pinned-digest>` base, non-root `trikon` user (uid/gid 10001), pinned pip installs of `pip==24.3.1`, `pytest==8.3.3`, `pytest-json-report==1.5.0`, `coverage==7.6.7`, `ruff==0.7.4`, `mypy==1.13.0`, working dir `/workspace`, `CMD ["sleep", "infinity"]`.
    - Env vars: `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`.
    - Every version pin is a version, not a range — bumping any pin must be a deliberate `Dockerfile.sandbox` edit that invalidates `static_baseline` at the tool-version boundary (Requirement 3.3).
    - _Requirements: 2.1, 3.3_

  - [x] 4.2 Add `scripts/build_sandbox_image.sh`
    - Single-line executable shell script: `docker build -t trikon/sandbox:0.1.0 -f Dockerfile.sandbox .`
    - `chmod +x` at commit time; owned by the repo (not a throwaway) so contributors and CI can rebuild the pinned image with one command.
    - _Requirements: 2.1_

  - [x] 4.3 Seed `docs/verification.md` with the image contract
    - Short new file (≤ 30 lines) explaining what `trikon/sandbox:0.1.0` is, which tool versions it pins, why every pin invalidates `static_baseline` when bumped, and how to rebuild it via `scripts/build_sandbox_image.sh`.
    - This file is extended into the full end-user guide in Task 15.1; Task 4.3 lands only the image-contract section.
    - Link from the top of `Dockerfile.sandbox` as a comment for cross-reference.
    - _Requirements: 2.1_

- [x] 5. Implement `LocalDockerSandbox` — the Docker isolation layer

  - [x] 5.1 Replace `trikon/verify/sandbox.py` stub with the full `LocalDockerSandbox` class
    - Constructor, `__enter__` (image-pull + `docker.from_env` health-check), `__exit__` (kill + remove), `mount_repo`, and `exec` exactly per `design.md §3.3` and `§5.2`.
    - Container spec passes every restriction from the `design.md §5.2` table: read-only bind mount, tmpfs at `/workspace/tmp` and `/workspace/pip-cache`, `network_mode="none"` default, `user="10001:10001"`, `cap_drop=["ALL"]`, `no-new-privileges:true`, `read_only=True` root FS, `mem_limit`, `pids_limit`, `cpu_period`/`cpu_quota`.
    - `exec` returns `SandboxExecResult` on every path including non-zero exit; never raises on non-zero exit code (design.md §3.3 contract).
    - Wrap every `docker.errors.*` at the point of call: `docker.from_env` failure → `SandboxUnavailableError` with the attempted socket path in the message; every other `docker.errors.APIError`, image-pull failure, mount validation failure, or `exec_create` / `exec_start` failure → `SandboxExecError`. Consumes the errors module from Task 1.
    - Consumes `SandboxExecResult` from `trikon/verify/models.py` (Task 2.1).
    - _Requirements: 2.1, 6.1, 6.3_

  - [x] 5.2 Implement timeout enforcement in `LocalDockerSandbox.exec`
    - Wall-clock deadline enforcement exactly per `design.md §5.5`: `time.monotonic()` bracket around `exec_start`; on breach, `container.kill(signal="SIGKILL")` and return `SandboxExecResult(exit_code=124, timed_out=True, stderr="sandbox exceeded deadline", ...)`.
    - Timeouts NEVER raise past the module boundary — `SandboxTimeoutError` is defined for completeness but never raised from `exec` (Requirement 2.2 explicit).
    - Deadline defaults to `None` (no timeout); callers pass the per-stage timeout explicitly. Extends `sandbox.py` from Task 5.1.
    - _Requirements: 2.2, 6.1_

  - [x] 5.3 Implement the network-allowlist branch in `LocalDockerSandbox`
    - When `network_allowlist` is non-empty: create a dedicated Docker bridge network at `__enter__` (`docker network create trikon-verify-<uuid> --driver bridge --internal=false`), attach the container to it instead of `network_mode="none"`, then run a one-shot iptables init sidecar inside the container's netns per `design.md §5.3`: `iptables -A OUTPUT -d <cidr> -j ACCEPT` for every allowlist entry, `127.0.0.0/8` allowed, `iptables -A OUTPUT -j DROP` as catch-all.
    - Teardown in `__exit__`: remove container, then remove network. Failure to remove the network is logged at WARNING but does not raise (the container has already been torn down).
    - Hostname resolution happens once at `__enter__` against the host resolver; the resolved IP set is what the iptables rules pin (`design.md §5.3` explicit).
    - Extends `sandbox.py` from Task 5.1 / 5.2.
    - _Requirements: 2.3_

  - [ ]* 5.4 Write unit tests in `tests/unit/verify/test_sandbox.py`
    - `FakeDockerClient` fixture in `tests/unit/verify/strategies.py` that satisfies the `docker.DockerClient` interface used by `LocalDockerSandbox` — `containers.create`, `containers.get`, `images.pull`, `api.exec_create`, `api.exec_start`, `api.exec_inspect`, `networks.create`, `networks.get`.
    - Image-pull happy path: `images.pull("trikon/sandbox:0.1.0")` succeeds → `__enter__` returns without raising.
    - Image-pull failure (`docker.errors.APIError` from `images.pull`) → `SandboxExecError` with the image tag in the message.
    - `docker.from_env()` failure parametrized over three socket variants (`unix:///var/run/docker.sock`, `npipe:////./pipe/docker_engine`, permission denied) → `SandboxUnavailableError` with the attempted socket path in the message.
    - Container timeout via a fake `exec_start` whose response arrives after `time.monotonic() - started > timeout_seconds` → returned `SandboxExecResult(timed_out=True, exit_code=124)` and no exception past the boundary. **Validates: Requirement 2.2**
    - Read-only mount enforcement: assert on the `containers.create` call spec that the `Mount` kwargs include `read_only=True` and the `read_only` root-FS flag is set.
    - `network_mode="none"` is passed by default; when `network_allowlist=("pypi.org",)` a custom network name matching `trikon-verify-<uuid>` is passed instead.
    - Error-wrapping closure: monkeypatch `containers.create` to raise `docker.errors.APIError`; assert `SandboxExecError` propagates and no bare `docker.errors.*` escapes.
    - _Requirements: 2.1, 2.2, 2.3, 6.1, 6.3_

- [x] 6. Implement `select_impacted_tests` — coverage-map lookup with filename fallback

  - [x] 6.1 Land the core algorithm in `trikon/verify/test_selector.py`
    - Replace the Phase-0 stub with `select_impacted_tests(conn, impact, *, repo_path, base_sha, now)` returning `SelectedTests` (Task 2.1). Implement steps 1–3 from `design.md §6`: freshest-`built_at` lookup, per-symbol `coverage_map` query keyed by `(qualified_name, base_sha)`, and staleness signal (`> 7d` OR `built_against_sha != base_sha`).
    - `conn` is expected to have Phase-2 tables (Task 3.2 provides that guarantee); the function does not call `ensure_verify_tables` itself.
    - Every raise site: `TestSelectionError` (JSON parse failures on `test_ids_json`, `sqlite3.Error` from the lookup query). Consumes the errors module from Task 1.2.
    - _Requirements: 1.1, 1.3, 6.1_

  - [x] 6.2 Implement the filename-heuristic fallback and deterministic sort
    - Extends `test_selector.py` from Task 6.1 with steps 4–6 from `design.md §6`: `_module_path_for_qname` helper, candidate paths `tests/test_{leaf}.py` and `tests/{pkg}/test_{leaf}.py`, on-disk existence check, union across hits and fallback IDs, `sorted(set(...))` deterministic output.
    - `fallback_reasons` is populated one entry per symbol that missed the cache (populated for the CLI human summary from Task 11).
    - Staleness override: when `map_stale=True`, every symbol routes through the fallback regardless of what rows are present (`design.md §6 Step 1 + Step 3`).
    - _Requirements: 1.2, 1.3_

  - [ ]* 6.3 Write unit + property tests in `tests/unit/verify/test_test_selector.py`
    - Coverage-map hit: seed a row with `test_ids_json=["tests/test_retry.py::test_backoff"]`, impact touches the same symbol at the same `base_sha` → `SelectedTests.node_ids == ("tests/test_retry.py::test_backoff",)`, `coverage_map_stale=False`.
    - Miss + fallback: no seeded row, `tests/test_leaf.py` present on disk → the fallback path fires, `fallback_reasons` is populated with the qualified name.
    - Staleness by age: seed row with `built_at = now - 8 days` → `coverage_map_stale=True`, every symbol routes through the fallback.
    - Staleness by SHA drift: seed row with `built_against_sha != impact.base_sha` → same treatment.
    - Missing symbol (no coverage_map row, no on-disk file) → `node_ids` empty for that symbol, `fallback_reasons` explains why.
    - **Property 1: Coverage-map hit completeness** — for any seeded rows, `node_ids` equals the sorted union of `test_ids_json` arrays. **Validates: Requirements 1.1**
    - **Property 2: Filename-heuristic fallback completeness** — for any miss, the returned `node_ids` includes exactly the on-disk candidate paths that exist. **Validates: Requirements 1.2**
    - **Property 3: Coverage-map staleness signal** — `coverage_map_stale` is True iff age > 7d OR any built_against_sha mismatch. **Validates: Requirements 1.3**
    - _Requirements: 1.1, 1.2, 1.3_

- [x] 7. Implement `run_static_checks` — ruff + mypy with baseline diff

  - [x] 7.1 Land the core algorithm in `trikon/verify/static_checks.py`
    - Replace the Phase-0 stub with `run_static_checks(sandbox, conn, impact, *, repo_path, base_sha, head_sha, tools=DEFAULT_STATIC_TOOLS)` returning `StaticReport`. Implement steps 1, 3, 4, 5 from `design.md §7`: per-tool version capture (`tool.version_command`), head-side tool invocation on `impact.changed_files`, `is_new` diff on the `(path, line, rule_id)` triple only, `StaticReport` assembly with the four aggregate counters.
    - Consumes `DEFAULT_STATIC_TOOLS` from Task 2.2 and `LocalDockerSandbox.exec` from Task 5.1.
    - Every raise site: `StaticCheckError` (worktree failure, tool-version capture failure, `sqlite3.Error` on baseline write).
    - _Requirements: 3.1, 6.1_

  - [x] 7.2 Implement the baseline cache, git-worktree materialization, and per-tool parsers
    - Extends `static_checks.py` from Task 7.1 with step 2 from `design.md §7`: `SELECT ... WHERE base_sha=? AND tool=? AND tool_version=?` cache lookup keyed on the exact triple from Requirement 3.2 / 3.3.
    - Cache miss branch: materialize the base tree via `git worktree add --detach <tmpdir> <base_sha>` on the host (outside the sandbox), bind-mount into the container for the base run, `git worktree remove --force` on any exit path. Worktree removal failure raises `StaticCheckError` and the verdict fails closed.
    - Two parser helpers: `_parse_ruff_json` reads ruff's `--output-format=json` payload; `_parse_mypy_text` reads mypy's line-per-diagnostic output. Ruff parse failure raises `StaticCheckError`; mypy tolerates unparseable trailing lines and drops them at DEBUG log level.
    - Persist baseline via `INSERT INTO static_baseline (base_sha, tool, tool_version, findings_json, computed_at) VALUES (?, ?, ?, ?, ?)` — the `UNIQUE(base_sha, tool, tool_version)` constraint from Task 3.1 makes tool-version bumps invalidate the cache row automatically.
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ]* 7.3 Write unit tests in `tests/unit/verify/test_static_checks.py`
    - Baseline cache miss then hit: prime a mock sandbox executor, call `run_static_checks` twice with the same `(base_sha, tool, tool_version)` — assert the base-run `exec` call fires exactly once across the two calls (second call reads `static_baseline`). **Validates: Requirements 3.2**
    - Tool-version invalidation: seed a row at `tool_version="ruff 0.7.3"`, call with `tool_version="ruff 0.7.4"` → cache miss, new row inserted. **Validates: Requirements 3.3**
    - `is_new` triple discipline: baseline has `(payments.py, 42, "F401")`, head has the same triple with a different `message` → `is_new=False`; head has `(payments.py, 42, "F402")` → `is_new=True`.
    - **Property 5: New-vs-baseline finding delta** — for random baseline/head finding sets, `is_new` is exactly the set subtraction on the triple, and `message` never participates. **Validates: Requirements 3.1**
    - Ruff parse failure (malformed JSON from a corrupt payload) → `StaticCheckError`.
    - Git-worktree cleanup on failure: monkeypatch the base-run `exec` to raise, assert `git worktree remove --force` still runs and no worktree is left on disk.
    - _Requirements: 3.1, 3.2, 3.3_

- [x] 8. Implement `load_and_run_plugins` — sandbox-isolated plugin execution

  - [x] 8.1 Ship `trikon/verify/_plugin_shim.py` — the in-sandbox shim
    - New production module (part of the installed `trikon` package). Implements the four-step shim body from `design.md §8`: read `/workspace/tmp/plugin_input.json`, `importlib.util.spec_from_file_location` on the target plugin path, `inspect.iscoroutinefunction(module.check)` → return `{"error": "async plugins not supported in Phase 2"}`, else call `module.check(ctx)` inside a `try / except Exception as exc: return {"error": repr(exc)}`, write `/workspace/tmp/plugin_output.json`.
    - The shim exits with code 0 on every path so `sandbox.exec` never raises for a plugin fault.
    - Reconstructs an equivalent `CheckContext` inside the sandbox from the serialized JSON input; the user-visible plugin API stays the dataclass form.
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 8.2 Implement `load_and_run_plugins` in `trikon/verify/plugins.py`
    - Replace the Phase-0 stub with the discovery + per-plugin execution flow from `design.md §8`: sorted `.trikon/checks/*.py` glob (skipping `_`-prefixed files), one `_run_one` call per plugin, tuple of `PluginResult` returned in path-sorted order.
    - Bind-mount `_plugin_shim.py` (Task 8.1) into the sandbox at `/workspace/repo/.trikon/_plugin_shim.py`. The source path is resolved from the installed `trikon` package via `importlib.resources.files("trikon.verify") / "_plugin_shim.py"` — never read from the target repo (design.md §8 note 1, security-critical).
    - Serialize `impact` and `plugin_rel_path` to `/workspace/tmp/plugin_input.json` via a scratch `sh -c "cat > ..."` `exec`; read `/workspace/tmp/plugin_output.json` back via `cat`.
    - Missing `.trikon/checks/` directory → empty tuple. Missing shim mount or missing output file → `PluginLoadError` (infrastructure, not a per-plugin failure).
    - _Requirements: 4.1, 4.2_

  - [x] 8.3 Implement per-plugin timeout enforcement and async rejection surfacing
    - Extends `plugins.py` from Task 8.2. `sandbox.exec(("python", "/workspace/repo/.trikon/_plugin_shim.py"), timeout_seconds=per_plugin_timeout_seconds)` — when the returned `SandboxExecResult.timed_out=True`, synthesize a `PluginResult(plugin=<path>, findings=(), error=f"plugin exceeded {per_plugin_timeout_seconds}s timeout")`.
    - Async rejection is reported via the shim's `{"error": "async plugins not supported in Phase 2"}` payload from Task 8.1; the loader propagates that string into `PluginResult.error` verbatim.
    - Default `per_plugin_timeout_seconds=30.0` matching `design.md §3.5`.
    - _Requirements: 4.2, 4.3_

  - [ ]* 8.4 Write unit tests in `tests/unit/verify/test_plugins.py`
    - Happy path: one file in `.trikon/checks/`, returns 3 findings → `PluginResult(findings=(<3>,), error=None)`.
    - Plugin import error (`SyntaxError` in the module body) → `PluginResult(error=starts_with("SyntaxError"))`, subsequent plugin in the same run still executes. **Validates: Requirements 4.2**
    - Plugin runtime exception (raises `RuntimeError` inside `check`) → `PluginResult(error=starts_with("RuntimeError"))`.
    - Async plugin rejection: `async def check` → `PluginResult(error="async plugins not supported in Phase 2")`. **Validates: Requirements 4.3**
    - Plugin timeout: shim sleeps past `per_plugin_timeout_seconds=1.0` → `PluginResult(error="plugin exceeded 1.0s timeout")`.
    - Absent `.trikon/checks/` directory → empty tuple, no exception.
    - **Property 8: Plugin findings preserved verbatim** — for any well-formed plugin returning findings `Fs`, `PluginResult.findings` equals `Fs` modulo public-boundary dictification. **Validates: Requirements 4.1**
    - **Property 9: Plugin-failure isolation** — for any mix of well-formed and failing plugins, every failing plugin has `error` populated, every well-formed plugin still produces its findings, and the returned tuple is sorted by plugin path. **Validates: Requirements 4.2, 4.3**
    - _Requirements: 4.1, 4.2, 4.3_

- [x] 9. Wire `run_verification` — the orchestrator entry point

  - [x] 9.1 Replace `trikon/verify/runner.py` stub with the orchestrator body
    - Implement `run_verification(repo_path, impact, *, policy, deadline_seconds, sandbox_image, state_db, now)` per `design.md §3.1`. Body opens a `state.db` connection, calls `select_impacted_tests` (Task 6), enters a `LocalDockerSandbox` context (Task 5), then runs `run_static_checks` (Task 7) and `load_and_run_plugins` (Task 8) sequentially inside the same container (`design.md §2.4`).
    - Assemble a `VerificationReport` combining every stage's output; populate `sandbox_ms` and `total_ms` from `time.monotonic()` bracketing.
    - Every raise site above becomes a `VerificationRunnerError` subclass — this function does not catch and swallow; the SDK boundary (Task 12) does.
    - _Requirements: 1.1, 3.1, 4.1, 6.1_

  - [x] 9.2 Implement pytest execution and timeout synthesis
    - Extends `runner.py` from Task 9.1. Pytest exec inside the sandbox: `pytest --json-report --json-report-file=/workspace/tmp/pytest.json <node_ids…>` with `--override-ini="addopts="` to strip any repo-side `addopts`. Parse `/workspace/tmp/pytest.json` into `TestResult` list via `_parse_pytest_json_report` helper.
    - Deadline plumbing: `deadline_seconds` flows to every `sandbox.exec` call — the pytest exec, ruff exec, mypy exec, and each plugin exec each receive their per-stage share of the remaining budget.
    - Sandbox-timeout synthesis: when the pytest `SandboxExecResult.timed_out=True`, synthesize a `TestReport(status="failed", results=(TestResult(node_id="<sandbox>", outcome="errored", failure_summary="sandbox exceeded 5-minute deadline"),))` and return normally — never raise past `run_verification` (Requirement 2.2 explicit). **Validates: Requirement 2.2**
    - Consumes Task 9.1's orchestrator skeleton (same file, follow-on wave).
    - _Requirements: 1.1, 2.2, 6.1_

  - [ ]* 9.3 Write unit tests in `tests/unit/verify/test_runner.py`
    - Report assembly: monkeypatch `select_impacted_tests`, `LocalDockerSandbox`, `run_static_checks`, and `load_and_run_plugins`; assert the returned `VerificationReport` composes each mock's output verbatim into the expected fields.
    - Deadline plumbing: pass `deadline_seconds=1.0`, assert the sandbox `exec` mock receives `timeout_seconds` values that sum to ≤ 1.0 (per-stage share).
    - Timeout synthesis: mock the pytest `exec` to return `SandboxExecResult(timed_out=True)`, assert `run_verification` returns a report with `tests.status == "failed"`, a single `TestResult(node_id="<sandbox>", outcome="errored")`, and no exception. **Validates: Requirement 2.2 / Property 4**
    - Error wrapping: for every dependency (selector, sandbox init, static checks, plugin loader), monkeypatch to raise; assert the raised class is a subclass of `VerificationRunnerError` and propagates unchanged.
    - **Property 4: Timeout produces synthesized failed report, never raises** — under any timeout scenario, no exception escapes `run_verification`. **Validates: Requirements 2.2**
    - _Requirements: 1.1, 2.1, 2.2, 3.1, 4.1, 6.1_

- [x] 10. Implement `build_coverage_map` — the `trikon coverage build` backend

  - [x] 10.1 Create `trikon/verify/coverage_builder.py` with the builder body
    - Implement `build_coverage_map(repo_path, conn, *, sandbox, head_sha)` per `design.md §3.6`. Inside the sandbox: `pytest --collect-only --quiet` → node-ID list; `coverage run --source src -m pytest -q --no-header --override-ini="addopts="` → instrumented run; `coverage json -o /workspace/tmp/coverage.json` → parse the coverage JSON into the `symbol → set(test_ids)` map.
    - Default `sandbox` to a fresh `LocalDockerSandbox()` when omitted; default `head_sha` to the current git HEAD via `git rev-parse HEAD`.
    - Return `CoverageBuildReport` (Task 2.1) with `symbols_indexed`, `test_nodes_seen`, `duration_ms`, `built_against_sha`, `stale_rows_pruned`.
    - Every raise site is `CoverageBuildError`. Consumes Task 5 (`LocalDockerSandbox`), Task 3 (state.db tables), and Task 1.2 (errors).
    - _Requirements: 5.1_

  - [x] 10.2 Implement persistence with single-transaction atomicity
    - Extends `coverage_builder.py` from Task 10.1. Persist every discovered `(qualified_name, test_ids)` pair via `INSERT OR REPLACE INTO coverage_map(qualified_name, test_ids_json, built_at, built_against_sha) VALUES (?, ?, ?, ?)` — the `UNIQUE(qualified_name, built_against_sha)` constraint from Task 3.1 makes rebuilds idempotent.
    - Persist every discovered pytest node ID via `INSERT OR REPLACE INTO tests_seen(test_node_id, last_seen, last_outcome) VALUES (?, ?, 'passed')`.
    - Failure discipline: wrap all writes inside a single `conn` transaction (`conn.execute("BEGIN")` … `conn.commit()`). Any raise (including from pytest exec) rolls back so previously-persisted rows are left untouched, then re-raises as `CoverageBuildError` (Requirement 5.3 explicit).
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ]* 10.3 Write unit tests in `tests/unit/verify/test_coverage_builder.py`
    - Happy path against a fixture repo with two symbols: mock the sandbox to return a synthetic `coverage.json` with `symbol_a → {test_1, test_2}` and `symbol_b → {test_3}`; assert `coverage_map` has 2 rows after the call, `tests_seen` has 3 rows, `built_against_sha` matches the passed `head_sha`, and the returned `CoverageBuildReport.symbols_indexed == 2`.
    - Atomicity on failure: pre-seed `coverage_map` with two rows at `built_against_sha="OLD"`; mock the pytest `exec` to raise mid-way; assert the pre-seeded rows are byte-identical before and after the failed call, and the caller receives `CoverageBuildError`. **Validates: Requirements 5.3 / Property 11**
    - Rebuild idempotence: run `build_coverage_map` twice against the same `head_sha` → row count stays at 2 (INSERT OR REPLACE), `built_at` updates on the second call.
    - **Property 10: Coverage-build population completeness** — for any symbol set `S` and test-node set `T`, after a successful build, `coverage_map` has ≥ 1 row per `s ∈ S` at `built_against_sha=head_sha`, and `tests_seen` has exactly one row per `t ∈ T`. **Validates: Requirements 5.1, 5.2**
    - **Property 11: Coverage-build atomicity on failure** — for any failure mid-run, previously-persisted rows are byte-identical before/after. **Validates: Requirements 5.3**
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 11. Wire the CLI — `trikon debug verify` and `trikon coverage build`

  - [x] 11.1 Author the human formatter at `trikon/evidence/formatters/verify_text.py`
    - `format_verification_verdict(verdict: Verdict) -> str` returning the five-section output from `design.md §11.1`: change summary line, tests block with pass/fail counts and first-5 failing node IDs, ruff new-vs-baseline counts, mypy new-vs-baseline counts, sandbox wall-clock duration, and the terminal `Verdict:` line.
    - Failing-test truncation is exactly 5 entries; if fewer than 5 exist, print all of them without padding.
    - `coverage_map_stale=True` renders the parenthetical `"(coverage map stale — filename-heuristic fallback used)"` line under the tests block.
    - Pure formatter — imports only from `trikon.evidence.report`; no runtime dependency on `trikon/verify/**`.
    - _Requirements: 7.2_

  - [x] 11.2 Extend `trikon/cli.py` with `debug verify` and `coverage build`
    - Add `trikon debug verify --repo <path> --base <sha> --head <sha> [--json] [--policy <path>] [--cache-db <path>]` to the Phase-1 `debug` Typer sub-app. Body calls `sdk.verify(...)` (Task 12), passes the result to `format_verification_verdict` (Task 11.1) on the default path, or `verdict.model_dump_json(indent=2)` when `--json` is set.
    - Add `trikon coverage build --repo <path> [--cache-db <path>]` as a new top-level Typer sub-app. Body calls `build_coverage_map` (Task 10) and prints the `design.md §11.2` summary block on success.
    - Exit codes exactly per `design.md §11.1` and `§11.2` tables: 0 on any well-formed verdict (including `require_human`) or successful coverage build; 1 on uncaught exception or `CoverageBuildError`; 2 on Typer usage error (missing `--base` without `--diff-file`, etc.).
    - Wire both sub-apps into the main `trikon` Typer application so `trikon --help` lists them.
    - _Requirements: 5.1, 7.2, 7.3_

  - [ ]* 11.3 Write CLI unit tests in `tests/unit/verify/test_cli.py`
    - `typer.testing.CliRunner` invocation: `trikon debug verify --repo <tmp> --base <sha> --head <sha>` prints the five-section human output; assert every section header appears in the stdout in order.
    - `--json` flag: stdout is valid JSON parsable to a `Verdict`-shaped dict with `decision`, `evidence`, `audit_id`, `created_at`.
    - Missing `--base` → exit code 2, Typer usage message on stderr.
    - Unreachable Docker → `SandboxUnavailableError` bubbles through `sdk.verify`, produces a `require_human` verdict → CLI exits 0 (a well-formed verdict is a successful invocation).
    - `trikon coverage build` happy path: mock `build_coverage_map` to return a `CoverageBuildReport`, assert the summary block prints and exit code is 0.
    - `trikon coverage build` failure: mock `build_coverage_map` to raise `CoverageBuildError`, assert exit code 1 and the exception name on stderr.
    - **Property 14: CLI human-summary completeness** — for any well-formed `VerificationReport`, all five sections render in order, and `--json` replaces the human formatter with `model_dump_json(indent=2)`. **Validates: Requirements 7.2, 7.3**
    - _Requirements: 5.1, 7.2, 7.3_

- [x] 12. Wire the SDK — `sdk.verify` calls `run_verification`

  - [x] 12.1 Update `trikon/sdk.py::verify` body per `design.md §10`
    - Add the `run_verification(repo, impact, state_db=cache_db)` call inside the existing `try` block, immediately after `compute_impact`.
    - Populate `Evidence(change=impact, verification=verification, policy_results=[])` on the happy path.
    - Keep `decision="require_human"` — the policy engine ships in Phase 3.
    - _Requirements: 7.1_

  - [x] 12.2 Widen the fail-closed except clause to include `VerificationRunnerError`
    - Change `except ChangeIntelError as exc:` to `except (ChangeIntelError, VerificationRunnerError) as exc:` per `design.md §10 AFTER`.
    - Update the fail-closed reason string to name the failing subsystem via `type(exc).__name__`.
    - Confirm `_fail_closed_verdict` returns `evidence.verification == EMPTY_VERIFICATION` — no code change needed to the sentinel itself, only that the fail-closed branch keeps using it.
    - Modifies `sdk.py`; different wave from Task 12.1 to satisfy same-file wave discipline.
    - _Requirements: 6.2_

  - [ ]* 12.3 Write SDK unit tests in `tests/unit/test_sdk_verify_phase2.py`
    - Happy path: monkeypatch `run_verification` to return a populated `VerificationReport`; assert `sdk.verify(...)` returns a `Verdict` with `decision="require_human"` and `evidence.verification` equal to that report.
    - `VerificationRunnerError` catch: monkeypatch `run_verification` to raise `SandboxUnavailableError`, assert the returned `Verdict` has `decision="require_human"`, `evidence.verification == EMPTY_VERIFICATION`, and the reason string names `SandboxUnavailableError`.
    - `ChangeIntelError` catch: monkeypatch `compute_impact` to raise `DepGraphError`, assert the same fail-closed shape holds.
    - **Property 13: Fail-closed at the SDK boundary** — for any `VerificationRunnerError` subclass raised in `run_verification`, `sdk.verify` returns `require_human` + `EMPTY_VERIFICATION`; `allow` is never emitted on this path. **Validates: Requirements 6.2**
    - _Requirements: 6.2, 7.1_

- [ ] 13. Integration tests against `examples/sample_repo/`

  - [ ]* 13.1 Write `tests/integration/verify/test_sample_repo_bad_retry.py`
    - Apply `tests/fixtures/scenarios/bad_retry.patch` to a fresh temp clone of `examples/sample_repo/`, commit both revisions, call `sdk.verify(repo, base_sha, head_sha)`.
    - Assert `verdict.evidence.verification.tests.status == "failed"` and the failing `TestResult.node_id` set includes `tests/test_worker.py::test_backoff_shape`.
    - Assert `verdict.evidence.verification.static.new_errors == 0` (the retry change is a logic bug, not a lint issue).
    - Marker `@pytest.mark.integration`, requires a live Docker daemon; skipped by default in the CI unit workflow.
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 7.1_

  - [ ]* 13.2 Write `tests/integration/verify/test_sample_repo_clean_refactor.py`
    - Apply `tests/fixtures/scenarios/clean_refactor.patch` to a fresh temp clone of `examples/sample_repo/`, call `sdk.verify(...)`.
    - Assert `verdict.evidence.verification.tests.status == "passed"` and every `TestResult.outcome == "passed"`.
    - Assert `verdict.evidence.verification.plugins` runs cleanly (no `error` fields populated on `PluginResult`).
    - Marker `@pytest.mark.integration`.
    - _Requirements: 1.1, 4.1, 7.1_

  - [ ]* 13.3 Write `tests/integration/verify/test_coverage_build.py`
    - Run `trikon coverage build --repo <tmp copy of sample_repo>` via `CliRunner`.
    - Assert `state.db.coverage_map` has ≥ 1 row per symbol under `src/**` after the build; assert `tests_seen` has exactly one row per test node ID under `tests/**`.
    - Assert `built_against_sha` equals `git rev-parse HEAD` on the temp clone and `built_at` is within 5 s of test wall time.
    - Assert the CLI exit code is 0 and the summary block appears in stdout.
    - Marker `@pytest.mark.integration`.
    - _Requirements: 5.1, 5.2_

  - [ ]* 13.4 Write `tests/integration/verify/test_network_allowlist.py`
    - Instantiate `LocalDockerSandbox(network_allowlist=("pypi.org",))`, exec `curl -sS -o /dev/null -w "%{http_code}" https://pypi.org/` inside → non-error HTTP code.
    - Exec `curl -sS --connect-timeout 3 -o /dev/null https://example.com/` inside → connection failure (iptables catch-all DROP).
    - Marker `@pytest.mark.integration` AND `@pytest.mark.perf` — opt-in; requires a live Docker daemon with iptables capability; runs in the nightly `perf` workflow, not the PR-blocking unit workflow.
    - _Requirements: 2.3_

- [x] 14. Perf smoke tests and CI wiring

  - [ ]* 14.1 Author `tests/benchmarks/test_verify_perf.py`
    - Three `pytest-benchmark` cases with `@pytest.mark.perf`:
      - Cold verdict on `examples/sample_repo/` bad_retry scenario, target ≤ 60 s, CI fail > 60 s. **Validates: Requirements 8.1**
      - Warm verdict on the same `(base_sha, head_sha)` pair with `static_baseline` cache primed and a fresh `coverage_map`, target ≤ 15 s, CI fail > 15 s. **Validates: Requirements 8.2**
      - `trikon coverage build --repo examples/sample_repo/`, target ≤ 30 s, CI fail > 30 s. **Validates: Requirements 8.3**
    - Baseline files stored in `tests/benchmarks/.benchmarks/`; regressions > 20 % against baseline fail the assertion.
    - _Requirements: 8.1, 8.2, 8.3_

  - [x] 14.2 Register `perf` and `integration` markers in `pyproject.toml`
    - Extend `[tool.pytest.ini_options]` `markers` list with `"perf: nightly perf benchmarks; opt-in"` and `"integration: requires a live Docker daemon; opt-in"`.
    - Extend the default `addopts` with `-m "not perf and not integration"` so `pytest` runs unit tests only by default; nightly and integration jobs override with `-m perf` or `-m integration`.
    - _Requirements: 8.1_

  - [x] 14.3 Add the nightly `perf` job to `.github/workflows/ci.yml`
    - Append a `perf` job matching the Phase-1 `perf` job shape: `runs-on: ubuntu-latest`, `needs: lint-type-test`, triggered by `schedule: - cron: '0 6 * * *'`, sets up Docker Buildx, builds `trikon/sandbox:0.1.0` via `scripts/build_sandbox_image.sh`, runs `pytest tests/benchmarks/ -m perf --benchmark-only --benchmark-max-time=60`.
    - Also add an `integration` sub-job under the same workflow that runs `pytest tests/integration/verify/ -m integration` with the same Docker setup.
    - Gate on the Requirement 8 wall-clock ceilings from Task 14.1.
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 15. Documentation — end-user guide, MDX pages, CHANGELOG, README

  - [x] 15.1 Extend `docs/verification.md` into the full end-user guide
    - Extends the image-contract skeleton from Task 4.3 with sections on: what Phase 2 does, running `trikon coverage build` (with sample_repo copy-paste example), running `trikon debug verify` (default + `--json`), authoring `.trikon/checks/*.py` plugins (dataclass API, async prohibition, per-plugin timeout), and configuring the network allowlist via policy.
    - Show the expected `VerificationReport` JSON for the `bad_retry` and `clean_refactor` scenarios so a reader can eyeball what "correct" looks like.
    - Cross-link to `docs/change_intel.md` for Phase-1 context.
    - _Requirements: 5.1, 7.2, 7.3_

  - [x] 15.2 Author `docs-site/pages/verification/*.mdx` — three MDX pages
    - `overview.mdx`: what verification does, the pytest-first invariant, the sandbox isolation story, and where it fits in the Trikon pipeline.
    - `plugin-authoring.mdx`: dataclass API for `check(context: CheckContext) -> list[Finding]`, async prohibition, per-plugin timeout, worked example that flags direct-SQL usage.
    - `troubleshooting.mdx`: common failure modes — Docker daemon unreachable (surface the socket-path diagnostic from `SandboxUnavailableError`), coverage-map stale, plugin timeout, network-allowlist misses.
    - Match the Phase-1 Mintlify structure and frontmatter shape used by `docs-site/concepts/*.mdx`.
    - _Requirements: 7.2_

  - [x] 15.3 Update `CHANGELOG.md` with the Phase 2 section
    - New `## [0.2.0] — Phase 2: Verification Runner` heading enumerating every user-visible surface: `trikon debug verify` command (default + `--json`), `trikon coverage build` command, `sdk.verify` returning populated `evidence.verification`, `.trikon/checks/*.py` plugin API, the `trikon/sandbox:0.1.0` pinned image, and the three new state.db tables.
    - Cross-link the release to `docs/verification.md` for detail.
    - _Requirements: 5.1, 7.2_

  - [x] 15.4 Extend `README.md`'s "What Trikon does today" section
    - Add a "Verification (Phase 2)" bullet naming test execution + static analysis + custom plugins running in an isolated Docker sandbox, with the `trikon debug verify --repo examples/sample_repo` invocation as the one-line demo.
    - Add `trikon coverage build` under the Quickstart section as the prerequisite step for warm-path verdicts.
    - _Requirements: 5.1, 7.2_

- [x] 16. Coverage and lint gates — enforce 85 % branch coverage and mypy-strict cleanliness

  - [x] 16.1 Extend `pyproject.toml` coverage config
    - Extend `[tool.coverage.report]` `include` filter to add `"trikon/verify/*"`, and set `fail_under = 85`.
    - Extend `[tool.coverage.run]` `source` list to include `"trikon.verify"`.
    - Rationale for the 85 % floor (below Phase-1's 90 %) is documented in `design.md §13.5`: sandbox code has ~50 lines of Docker-daemon-alive paths unreachable from unit tests.
    - _Requirements: 6.1_

  - [x] 16.2 Achieve `mypy --strict trikon/verify/` cleanliness
    - Run `mypy --strict trikon/verify/` and fix any red squiggles introduced across Tasks 1–12.
    - Add per-module `ignore_missing_imports = true` overrides to `[tool.mypy]` for `docker.*` and `coverage.*` (matching the Phase-1 pattern for jedi/libcst/unidiff/git).
    - The exit-0 outcome of `mypy --strict trikon/verify/ trikon/exceptions.py` is the gate.
    - _Requirements: 6.1_

  - [x] 16.3 Achieve `ruff check` and `ruff format --check` cleanliness on `trikon/verify/`
    - Run `ruff check trikon/verify/` and `ruff format --check trikon/verify/`; fix any remaining findings introduced across Tasks 1–12.
    - Extend the `lint-type-test` job in `.github/workflows/ci.yml` to include `trikon/verify/` and `trikon/exceptions.py` in the ruff and mypy invocations.
    - All three gates (coverage, mypy, ruff) green in the unit CI workflow before merge.
    - _Requirements: 6.1_

## Notes

- Sub-tasks marked `*` are optional in the sense of Kiro's "skip for a fast MVP" convention. In practice, every `*` test task is required to hit the Definition of Done — the marker signals "test code, not implementation code", not "throwaway".
- Each property test cites its numbered Correctness Property from `design.md §16` and the specific `Requirements` clause it validates, so traceability from acceptance criterion → property → test file is one grep.
- Wave-1 tasks (1–4) establish the foundation the rest of the phase builds on: exception hierarchy, data models, SQLite tables, sandbox image. They land in parallel and unblock everything else.
- Wave-2 tasks (5–8) are the four adapter modules. Each is independent at the file level; sub-tasks within an adapter serialize across waves because they share the same source file.
- Wave-3 tasks (9–12) fuse the adapters into `run_verification`, wire the coverage-builder, extend the CLI, and update the SDK. Task 9 depends on Tasks 5–8; Task 12 depends on Task 9.
- Wave-4 tasks (13–16) are release-layer: integration tests against `sample_repo`, perf benchmarks, docs, and coverage/lint gates. Every integration test carries `@pytest.mark.integration` so the PR-blocking unit workflow stays fast; perf tests carry `@pytest.mark.perf` and run nightly.
- The task graph does not include Phase 3 (policy engine). `sdk.verify` intentionally returns `require_human` at the end of Phase 2 because there is no policy grader yet — the verdict now carries real evidence in `evidence.verification`, which is the shipping increment.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "4.1", "4.2"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "4.3"] },
    { "id": 2, "tasks": ["1.3", "2.3", "3.3", "8.1"] },
    { "id": 3, "tasks": ["5.1", "6.1", "7.1", "8.2", "11.1"] },
    { "id": 4, "tasks": ["5.2", "6.2", "7.2", "8.3"] },
    { "id": 5, "tasks": ["5.3", "5.4", "6.3", "7.3", "8.4"] },
    { "id": 6, "tasks": ["9.1", "10.1", "12.1"] },
    { "id": 7, "tasks": ["9.2", "10.2", "11.2", "12.2"] },
    { "id": 8, "tasks": ["9.3", "10.3", "11.3", "12.3"] },
    { "id": 9, "tasks": ["13.1", "13.2", "13.3", "13.4", "14.1", "14.2", "14.3", "15.2", "15.3", "15.4"] },
    { "id": 10, "tasks": ["15.1", "16.1"] },
    { "id": 11, "tasks": ["16.2"] },
    { "id": 12, "tasks": ["16.3"] }
  ]
}
```
