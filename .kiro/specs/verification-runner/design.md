# Verification Runner — Design

> Phase 2 of the Trikon 12-week build. Turns an `ImpactSet` into a
> `VerificationReport` by running the impacted pytest node IDs, `ruff`, `mypy`,
> and any repo-defined check plugins inside a Docker-backed sandbox. This is
> the subsystem that lets `sdk.verify` attach real evidence to a Verdict.
>
> Status: design. Phase-0 stubs exist at `trikon/verify/**` and are extended,
> not replaced. Requirements are frozen against
> `.kiro/specs/verification-runner/requirements.md` and `EXECUTION_PLAN.md
> §Phase 2`.

---

## 1. Overview

### 1.1 What it does

Given a repo path and the `ImpactSet` produced by `trikon.change_intel.compute_impact`, Verification Runner emits the `trikon.evidence.report.VerificationReport` Pydantic model:

```
ImpactSet
   │
   ▼
run_verification
   │
   ├── select_impacted_tests ── coverage_map lookup ────┐
   │                              │                      │
   │                              └── filename fallback ─┤
   │                                                     │
   ▼                                                     │
LocalDockerSandbox  (image=trikon/sandbox:0.1.0, net=none, ro-mount)
   │                                                     │
   ├── pytest <selected node IDs>  ◄─────────────────────┘
   ├── ruff  <changed files>       │
   ├── mypy  <changed files>       │  (diff vs static_baseline)
   ├── <.trikon/checks/*.py>       │
   ▼
VerificationReport
   │
   ▼
sdk.verify → Verdict(decision="require_human", evidence.verification=…)
```

Every stage has a single, well-typed entry point. Every stage can fail; every failure raises a subclass of `VerificationRunnerError` so the SDK boundary can translate any internal breakage into `require_human` without ambiguity — the same never-fail-open contract Phase 1 established for Change Intelligence.

### 1.2 Module boundary

Everything inside `trikon/verify/**` raises only `VerificationRunnerError` subclasses. Every foreign exception — `docker.errors.APIError`, `subprocess.CalledProcessError`, `sqlite3.Error`, `ImportError` from a repo-supplied plugin, `TimeoutError` from `container.wait`, bare `OSError` from the tmpfs mount — is caught at the module boundary and re-raised as the appropriate `VerificationRunnerError` subclass. That closure is what lets `sdk.verify` catch a single base class and produce a well-formed `require_human` verdict.

### 1.3 Goals (v0.1)

| # | Goal | Measured by |
| - | ---- | ----------- |
| G1 | Deterministic `VerificationReport` for any (repo, impact, sandbox_image) | Same inputs → byte-identical JSON on 10 successive runs with a fake docker client |
| G2 | 60 s cold / 15 s warm on `examples/sample_repo` bad_retry | `tests/benchmarks/test_verify_perf.py` — Requirement 8.1, 8.2 |
| G3 | mypy `--strict` clean on `trikon/verify/**` | `mypy --strict trikon/verify/` exits 0 |
| G4 | ≥ 85 % branch coverage on `trikon/verify/**` | `coverage report --fail-under=85 --include='trikon/verify/*'` |
| G5 | Never fail-open | Every uncaught error path in `sdk.verify` becomes `require_human` (§9, §12) |
| G6 | Zero `dict[str, Any]` on the public surface | `disallow_any_explicit=true` in `[tool.mypy]` (unchanged from Phase 1) |

### 1.4 Non-goals (v0.1)

- **Remote sandbox (Warden).** Local Docker only. Warden reuse from Unideploy lands in Phase 5.
- **Async plugin API.** Sync `check(context: CheckContext) -> list[Finding]` only. Requirement 4.3 explicitly rejects async at load time.
- **Parallel pytest / ruff / mypy execution.** Sequential inside a single container in Phase 2 (see §2.3 for the rationale).
- **Cross-language sandboxes.** Python-only container. Phase 3+ will add JavaScript and Go images.
- **Automatic coverage-map rebuild on staleness.** Requirement 1.3 fires the fallback and sets a flag; the caller (or a nightly job) rebuilds. Auto-rebuild is Open Question 1.
- **Coverage-map schema migrations.** Additive-only in Phase 2 (see §4).

---

## 2. Architecture

### 2.1 Component diagram

```
trikon/verify/
├── runner.py            run_verification()        — public entry point
├── test_selector.py     select_impacted_tests()   — coverage-map + fallback
├── sandbox.py           LocalDockerSandbox        — Docker isolation layer
├── static_checks.py     run_static_checks()       — ruff/mypy + baseline diff
├── plugins.py           load_and_run_plugins()    — .trikon/checks/*.py
├── coverage_builder.py  build_coverage_map()      — `trikon coverage build` (new module)
├── models.py            SelectedTests, SandboxExecResult, StaticTool, ...  (new)
└── errors.py            VerificationRunnerError hierarchy                  (new)

External:
  trikon/exceptions.py   TrikonError                (new — shared root, §9)
  trikon/change_intel/…  compute_impact → ImpactSet
  trikon/evidence/report VerificationReport et al.
  .trikon/state.db       Phase-1 tables + coverage_map, tests_seen, static_baseline
  Docker daemon          via `docker-py` (`docker>=7,<8`) at UNIX socket or DOCKER_HOST
```

### 2.2 Data flow

```
                     ┌──────────────────────────────┐
        ImpactSet ──▶│  run_verification (runner)   │
                     └───┬───────────┬──────────────┘
                         │           │
             ┌───────────▼──┐   ┌────▼──────────────┐
             │ select_      │   │ LocalDockerSandbox│
             │ impacted_    │   │  (context mgr)    │
             │ tests        │   │  image + mounts   │
             └────┬─────────┘   └────┬──────────────┘
                  │                  │
   .trikon/       │                  │
   state.db  ◄────┤                  │
    ▲             │                  ▼
    │       ┌─────▼─────────┐   ┌────────────────────────┐
    │       │ coverage_map  │   │ pytest --json-report   │
    │       │ lookup        │   │  on selected node IDs  │
    │       │  ↓miss        │   └────┬───────────────────┘
    │       │ filename      │        │
    │       │ heuristic     │        ▼
    │       └───────────────┘   ┌────────────────────────┐
    │                           │ ruff / mypy diff vs    │
    ├───────────────────────────│ static_baseline        │
    │  cache read/write         └────┬───────────────────┘
    │                                │
    │                                ▼
    │                          ┌────────────────────────┐
    │                          │ load_and_run_plugins   │
    │                          │  .trikon/checks/*.py   │
    │                          └────┬───────────────────┘
    │                                │
    │                                ▼
    └──────────── report assembly ── VerificationReport
```

The stateful boundary is one SQLite file at `<repo>/.trikon/state.db` — the same file Phase 1 uses. Phase 2 adds three sibling tables (`coverage_map`, `tests_seen`, `static_baseline`); the Phase-1 tables (`schema_meta`, `file_index`, `symbols`, `edges`) are untouched. See §4 for the full DDL.

### 2.3 Sequence of operations and wall-clock budget

Requirement 8.1 caps a cold verdict at 60 s; Requirement 8.2 caps a warm verdict at 15 s. The per-stage budget below composes to those numbers with headroom:

| # | Stage | Cold budget | Warm budget | Notes |
| - | ----- | ----------- | ----------- | ----- |
| 1 | Sandbox startup (`docker run` + image pull if absent) | ≤ 15 s | ≤ 2 s | Cold path pulls `trikon/sandbox:0.1.0`; warm path reuses the local image cache |
| 2 | Repo dependency install (`pip install --no-deps -e .[dev]`) | ≤ 10 s | ≤ 1 s | Tmpfs-backed pip cache survives across the same container process; a warm pip cache resolves to a no-op |
| 3 | `select_impacted_tests` (coverage-map lookup + fallback glob) | ≤ 500 ms | ≤ 500 ms | Pure SQLite + `pathlib.glob`; no sandbox round-trip |
| 4 | Pytest execution on selected subset | ≤ 5 s | ≤ 5 s | `bad_retry` runs 4 test files (∼30 tests); sample_repo full suite is ≤ 12 tests |
| 5 | Ruff on changed files | ≤ 3 s | ≤ 500 ms | Warm path hits `static_baseline` cache → only the head run remains |
| 6 | Mypy on changed files | ≤ 3 s | ≤ 500 ms | Same cache-hit reasoning as ruff |
| 7 | Plugin execution | ≤ 500 ms per plugin | ≤ 500 ms per plugin | Bounded by policy; per-plugin timeout defaults to 30 s |
| 8 | Report assembly + persistence | ≤ 200 ms | ≤ 200 ms | Pydantic serialization + a handful of SQLite inserts |
| **Total** | | **≤ 37.2 s cold** | **≤ 9.7 s warm** | Leaves ≈ 23 s cold / 5 s warm slack against Requirement 8's 60/15 s ceilings |

**Validates: Requirements 8.1, 8.2, 8.3.**

### 2.4 Concurrency model

Pytest, static checks, and plugins run **sequentially inside one sandbox container**. Rationale:

1. **Timeout accounting is trivial.** A single `container.wait(timeout=deadline_seconds)` gates the whole verdict against Requirement 2.2's 5-minute ceiling. Parallel stages would need per-stage deadlines and a synchronization primitive to enforce the aggregate cap.
2. **Deterministic resource usage.** A `mem_limit=2g` container with three parallel Python processes can OOM on Django-sized inputs; sequential execution keeps peak RSS bounded to whichever tool is heaviest (mypy).
3. **The budget already fits.** §2.3 leaves ~23 s of slack cold; parallelism would recover 3-5 s at best, at the cost of both invariants above.
4. **Log ordering is deterministic.** Sequential execution produces a stable stdout order that the CLI human formatter (Requirement 7.2) prints without needing to interleave streams.

Parallel execution is Open Question 3 — revisit in Phase 3 if the perf budget tightens.

---

## 3. Public API surface

Every public function's signature is fixed here. Deviations require a design-doc update. Every signature is fully type-hinted; no `dict[str, Any]` appears on any public parameter or return type.

### 3.1 `trikon/verify/runner.py`

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trikon.change_intel.models import ChangeSet  # unused in body, imported for docstring cross-ref
from trikon.evidence.report import ImpactSet, VerificationReport
from trikon.policy.dsl import Policy  # forward-only; Phase 3 wires it


def run_verification(
    repo_path: Path,
    impact: ImpactSet,
    *,
    policy: Policy | None = None,
    deadline_seconds: float = 300.0,
    sandbox_image: str = "trikon/sandbox:0.1.0",
    state_db: Path | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Execute the impacted checks in isolation and return a structured report.

    Args:
        repo_path: Absolute path to the git repository being verified.
        impact: The precomputed impact set from ``compute_impact``. Fields read
            from it are ``changed_files``, ``changed_symbols``, ``impacted_tests``,
            and the (base_sha, head_sha) pair carried on the wrapping ``ChangeSet``
            — surfaced via ``impact.blast_radius_numeric``'s companion metadata.
        policy: Optional policy providing the ``network_allowlist`` for
            Requirement 2.3. ``None`` means ``network_mode='none'``.
        deadline_seconds: Wall-clock ceiling. Defaults to 300 s (Requirement 2.2).
            The runner returns a synthesized failed ``TestReport`` on breach; it
            does not raise ``SandboxTimeoutError`` past the module boundary.
        sandbox_image: Docker image tag. Overridable for tests only; production
            callers always take the default.
        state_db: SQLite state database path. Defaults to
            ``repo_path / ".trikon" / "state.db"`` (same file Phase 1 uses).
        now: Clock injection point for staleness checks. Defaults to
            ``datetime.now(UTC)``.

    Returns:
        A fully populated :class:`VerificationReport`.

    Raises:
        VerificationRunnerError: Any internal failure. The SDK boundary catches
            this and returns a ``require_human`` verdict backed by
            ``EMPTY_VERIFICATION`` (Requirement 6.2).
    """
```

**Validates: Requirements 1, 2, 3, 4, 6, 7, 8.**

### 3.2 `trikon/verify/test_selector.py`

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trikon.evidence.report import ImpactSet


@dataclass(frozen=True, slots=True)
class SelectedTests:
    """Pytest node IDs plus the provenance metadata the report needs.

    ``node_ids`` is deterministic-sorted. ``coverage_map_stale`` propagates to
    ``TestReport.coverage_map_stale``. ``fallback_reasons`` is one entry per
    symbol that missed the cache, populated for the human-readable CLI summary.
    """

    node_ids: tuple[str, ...]
    coverage_map_stale: bool
    fallback_reasons: tuple[str, ...]


def select_impacted_tests(
    conn: sqlite3.Connection,
    impact: ImpactSet,
    *,
    repo_path: Path,
    base_sha: str | None,
    now: datetime | None = None,
) -> SelectedTests:
    """Return the pytest node IDs to execute for this change.

    Strategy: query ``coverage_map`` per changed symbol; on miss, fall back
    to the filename heuristic (``tests/test_{leaf}.py``,
    ``tests/{pkg}/test_{leaf}.py``); on staleness (built_at older than 7 d
    OR built_against_sha != base_sha), route every symbol through the
    fallback and set ``coverage_map_stale=True``.
    """
```

**Validates: Requirements 1.1, 1.2, 1.3.**

### 3.3 `trikon/verify/sandbox.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """Outcome of one command executed inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class LocalDockerSandbox:
    """The sole sandbox backend in Phase 2, backed by the local Docker daemon.

    Use as a context manager. ``__enter__`` verifies the daemon is reachable
    and pulls the image if absent; ``__exit__`` kills and removes the
    container. Every method raises a ``VerificationRunnerError`` subclass on
    failure — ``docker.errors.*`` never escapes.
    """

    def __init__(
        self,
        *,
        image: str = "trikon/sandbox:0.1.0",
        network_allowlist: tuple[str, ...] | None = None,
        mem_limit: str = "2g",
        cpu_quota: int = 200_000,
        pids_limit: int = 512,
    ) -> None: ...

    def __enter__(self) -> "LocalDockerSandbox": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def mount_repo(self, repo_path: Path, *, read_only: bool = True) -> None:
        """Bind-mount ``repo_path`` at ``/workspace/repo``.

        Must be called before the first ``exec``. Idempotent within a
        context-manager scope; a second call with a different path raises
        ``SandboxExecError``.
        """

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str = "/workspace/repo",
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        """Run ``argv`` inside the container. Never raises on non-zero exit."""
```

**Validates: Requirements 2.1, 2.2, 2.3, 6.3.**

### 3.4 `trikon/verify/static_checks.py`

```python
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from trikon.evidence.report import ImpactSet, StaticReport
from trikon.verify.sandbox import LocalDockerSandbox


@dataclass(frozen=True, slots=True)
class StaticTool:
    """A pinned static-analysis tool the runner knows how to invoke.

    ``name`` is the human tool name ("ruff", "mypy"). ``argv_template`` is a
    tuple of format-string parts; ``{files}`` is expanded to the changed-file
    list at runtime. ``version_command`` is used to capture ``tool_version``
    for the ``static_baseline`` cache key. ``parse_json`` picks the finding
    parser: ``ruff`` emits JSON with ``--output-format=json``; ``mypy`` emits
    line-per-diagnostic text.
    """

    name: str
    argv_template: tuple[str, ...]
    version_command: tuple[str, ...]
    parse_json: bool


DEFAULT_STATIC_TOOLS: tuple[StaticTool, ...] = (
    StaticTool(
        name="ruff",
        argv_template=("ruff", "check", "--output-format=json", "{files}"),
        version_command=("ruff", "--version"),
        parse_json=True,
    ),
    StaticTool(
        name="mypy",
        argv_template=("mypy", "--no-color-output", "--show-column-numbers", "{files}"),
        version_command=("mypy", "--version"),
        parse_json=False,
    ),
)


def run_static_checks(
    sandbox: LocalDockerSandbox,
    conn: sqlite3.Connection,
    impact: ImpactSet,
    *,
    repo_path: Path,
    base_sha: str,
    head_sha: str,
    tools: Sequence[StaticTool] = DEFAULT_STATIC_TOOLS,
) -> StaticReport:
    """Run each tool on the head-side changed files, diff against ``static_baseline``.

    Cache key: ``(base_sha, tool.name, tool_version)``. On miss, materialize
    the base tree under an ephemeral ``git worktree add --detach`` outside
    the sandbox, run the tool against that checkout inside the sandbox,
    persist the findings, and remove the worktree. ``is_new`` on a returned
    finding is True iff its ``(path, line, rule_id)`` triple is absent
    from the baseline.
    """
```

**Validates: Requirements 3.1, 3.2, 3.3.**

### 3.5 `trikon/verify/plugins.py`

```python
from __future__ import annotations

from pathlib import Path

from trikon.evidence.report import ImpactSet, PluginResult
from trikon.verify.sandbox import LocalDockerSandbox


def load_and_run_plugins(
    sandbox: LocalDockerSandbox,
    repo_path: Path,
    impact: ImpactSet,
    *,
    per_plugin_timeout_seconds: float = 30.0,
) -> tuple[PluginResult, ...]:
    """Discover, import, and invoke every ``.trikon/checks/*.py`` plugin.

    Import happens inside the sandbox process, not in the host, so a
    plugin cannot exfiltrate host state. Import failures, ``async def``
    declarations, and runtime exceptions become ``PluginResult`` entries
    with the ``error`` field populated — the runner never crashes on a
    plugin fault.
    """
```

**Validates: Requirements 4.1, 4.2, 4.3.**

### 3.6 `trikon/verify/coverage_builder.py` (new module)

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from trikon.verify.sandbox import LocalDockerSandbox


@dataclass(frozen=True, slots=True)
class CoverageBuildReport:
    """Outcome of a ``trikon coverage build`` invocation."""

    symbols_indexed: int
    test_nodes_seen: int
    duration_ms: int
    built_against_sha: str
    stale_rows_pruned: int


def build_coverage_map(
    repo_path: Path,
    conn: sqlite3.Connection,
    *,
    sandbox: LocalDockerSandbox | None = None,
    head_sha: str | None = None,
) -> CoverageBuildReport:
    """Run the full pytest suite with ``coverage.py`` instrumentation and
    persist the ``symbol → set(test_ids)`` mapping to ``coverage_map``.

    ``sandbox`` defaults to a fresh ``LocalDockerSandbox()`` if omitted.
    ``head_sha`` defaults to the current git HEAD. On any failure the
    caller receives ``CoverageBuildError``; the previously-persisted rows
    are untouched (Requirement 5.3).
    """
```

**Validates: Requirement 5.**

### 3.7 Auxiliary dataclasses (recap)

| Type | Module | Fields | Notes |
| ---- | ------ | ------ | ----- |
| `SelectedTests` | `test_selector.py` | `node_ids: tuple[str, ...]`, `coverage_map_stale: bool`, `fallback_reasons: tuple[str, ...]` | frozen, slots |
| `SandboxExecResult` | `sandbox.py` | `exit_code: int`, `stdout: str`, `stderr: str`, `duration_ms: int`, `timed_out: bool` | frozen, slots |
| `StaticTool` | `static_checks.py` | `name: str`, `argv_template: tuple[str, ...]`, `version_command: tuple[str, ...]`, `parse_json: bool` | frozen, slots |
| `CoverageBuildReport` | `coverage_builder.py` | see §3.6 | frozen, slots |

All four are frozen slotted dataclasses following the same pattern as `trikon/change_intel/models.py`. They stay internal to `trikon/verify/**`; the public boundary is Pydantic (`VerificationReport` and its sub-models in `trikon/evidence/report.py`), unchanged from Phase 1.

---

## 4. Data model & storage

### 4.1 SQLite DDL — three new sibling tables

Added idempotently at the first `run_verification` call via `CREATE TABLE IF NOT EXISTS`. The Phase-1 pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY`) remain the connection defaults. No Alembic in Phase 2 — additive-only schema evolution.

```sql
-- =========================================================================
-- coverage_map. Symbol → tests mapping built by `trikon coverage build`.
-- =========================================================================
CREATE TABLE IF NOT EXISTS coverage_map (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    qualified_name      TEXT NOT NULL,           -- e.g. "payments.retry.with_backoff"
    test_ids_json       TEXT NOT NULL,           -- JSON array of pytest node IDs
    built_at            TEXT NOT NULL,           -- ISO-8601 UTC
    built_against_sha   TEXT NOT NULL,           -- git SHA the build ran on
    UNIQUE (qualified_name, built_against_sha)
);
CREATE INDEX IF NOT EXISTS idx_coverage_map_qname
    ON coverage_map(qualified_name);
CREATE INDEX IF NOT EXISTS idx_coverage_map_built_at
    ON coverage_map(built_at);


-- =========================================================================
-- tests_seen. One row per pytest node ever observed; feeds the CLI's
-- "known test suite" heuristic and the coverage-builder's pruning pass.
-- =========================================================================
CREATE TABLE IF NOT EXISTS tests_seen (
    test_node_id        TEXT PRIMARY KEY,        -- "tests/test_retry.py::test_backoff_shape"
    last_seen           TEXT NOT NULL,           -- ISO-8601 UTC
    last_outcome        TEXT NOT NULL
        CHECK(last_outcome IN ('passed', 'failed', 'errored', 'skipped'))
);
CREATE INDEX IF NOT EXISTS idx_tests_seen_last_seen
    ON tests_seen(last_seen);


-- =========================================================================
-- static_baseline. Cached ruff/mypy findings against `base_sha` so a
-- verdict only pays the sandbox cost once per (base_sha, tool_version).
-- =========================================================================
CREATE TABLE IF NOT EXISTS static_baseline (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    base_sha            TEXT NOT NULL,
    tool                TEXT NOT NULL,           -- "ruff" | "mypy"
    tool_version        TEXT NOT NULL,           -- "ruff 0.7.4" | "mypy 1.13.0"
    findings_json       TEXT NOT NULL,           -- JSON array of Finding-shaped rows
    computed_at         TEXT NOT NULL,           -- ISO-8601 UTC
    UNIQUE (base_sha, tool, tool_version)
);
CREATE INDEX IF NOT EXISTS idx_static_baseline_lookup
    ON static_baseline(base_sha, tool);
```

**Validates: Requirements 1.1 (coverage_map), 1.3 (built_at + built_against_sha for staleness), 3.2 (cache key on static_baseline), 3.3 (tool_version discriminator), 5.1 (coverage_map + tests_seen population), 5.2 (built_at + built_against_sha persistence).**

### 4.2 Cache-invalidation semantics

- **`coverage_map` staleness** is time-based (7 d) OR SHA-based (`built_against_sha != ImpactSet.base_sha`). Either condition sets `TestReport.coverage_map_stale = True` and routes every symbol through the filename fallback for that verdict (Requirement 1.3). Old rows are not deleted opportunistically; `trikon coverage build` overwrites them via the `(qualified_name, built_against_sha)` uniqueness constraint (`INSERT OR REPLACE`).
- **`tests_seen` update policy**: every pytest node run through the sandbox upserts one row. This table exists to answer "does this test node still exist in the suite?" during coverage-map builds, and to feed a Phase-3 UI that lists tests the runner has not observed in > 30 d as candidates for deletion.
- **`static_baseline` invalidation** is discriminated by tool_version in the primary key (Requirement 3.3): a `pyproject.toml` bump from `ruff==0.7.4` to `ruff==0.8.0` makes every cached row for `tool='ruff'` unreachable by lookup; the new version writes a fresh row. Old rows are not deleted; they age out with the git object.
- **Migration policy**: additive only. Phase-2 code will not `ALTER` or `DROP` any table Phase 1 created. If Phase 3 needs to change one of these three tables, it bumps `schema_meta.schema_version` and ships a numbered migration file, same rule Phase 1 established.

### 4.3 Row-count expectations

On `examples/sample_repo/` after `trikon coverage build`:

| Table | Row count | Growth |
| ----- | --------- | ------ |
| `coverage_map` | ≈ 40 (one per symbol in `src/**`) | linear in symbol count |
| `tests_seen` | ≈ 12 (one per test node) | linear in test count |
| `static_baseline` | ≤ 2 per `(base_sha, tool_version)` | bounded by unique base SHAs the repo verifies against |

Django-sized inputs would push `coverage_map` to ~50 K rows and `tests_seen` to ~15 K — still well inside SQLite's comfort zone.

---

## 5. Sandbox design

### 5.1 Base image

`Dockerfile.sandbox` (committed at repo root, built and pushed as `trikon/sandbox:0.1.0`):

```dockerfile
FROM python:3.11-slim@sha256:<pinned-digest>

# Non-root user for the entire container lifetime.
RUN groupadd --gid 10001 trikon \
 && useradd  --uid 10001 --gid 10001 --create-home --shell /bin/bash trikon

# Pinned tool versions. Bumping any of these invalidates static_baseline.
RUN pip install --no-cache-dir \
        pip==24.3.1 \
        pytest==8.3.3 \
        pytest-json-report==1.5.0 \
        coverage==7.6.7 \
        ruff==0.7.4 \
        mypy==1.13.0

# Baseline site-packages layer stays in the read-only image.
# Repo-supplied dependencies land in a tmpfs-backed venv at runtime.
USER trikon
WORKDIR /workspace
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The runner sends every command via `docker exec`. The image only needs a
# process to keep alive; `sleep infinity` is the smallest such surface.
CMD ["sleep", "infinity"]
```

Rationale for each pin:

- **`python:3.11-slim@sha256:<digest>`** — the digest pin (not just the tag) is what makes the image immutable across Docker Hub cache eviction; without it a stale tag can silently point at a new upload.
- **`pytest==8.3.3` + `pytest-json-report==1.5.0`** — pytest's JSON output plugin is how the runner parses per-node outcomes into `TestResult` without shelling `pytest --collect-only`.
- **`coverage==7.6.7`** — used by `build_coverage_map` for symbol-level line coverage; pinned because coverage's data format changed between 6.x and 7.x.
- **`ruff==0.7.4` + `mypy==1.13.0`** — mirror the Phase-1 dev-dependency pins in `pyproject.toml`. If a repo pins its own tool versions in `[dependency-groups]`, `pip install --no-deps -e .[dev]` upgrades the ones the repo cares about; the fallback pins here just guarantee the tools exist.

### 5.2 Container spec passed to `docker-py`

```python
container = client.containers.create(
    image=self._image,
    command=("sleep", "infinity"),
    user="10001:10001",
    working_dir="/workspace/repo",
    mounts=[
        docker.types.Mount(
            target="/workspace/repo",
            source=str(repo_path.resolve()),
            type="bind",
            read_only=True,
        ),
    ],
    tmpfs={
        "/workspace/tmp": "size=512m,uid=10001,gid=10001",
        "/workspace/pip-cache": "size=256m,uid=10001,gid=10001",
    },
    network_mode="none" if not self._network_allowlist else self._network_name,
    mem_limit=self._mem_limit,          # "2g"
    pids_limit=self._pids_limit,        # 512
    cpu_period=100_000,
    cpu_quota=self._cpu_quota,          # 200_000  (2 CPUs)
    cap_drop=["ALL"],
    security_opt=["no-new-privileges:true"],
    read_only=True,                     # root FS read-only; only tmpfs is writable
    labels={"trikon.role": "verify-sandbox", "trikon.version": "0.1.0"},
    detach=True,
    auto_remove=False,                  # runner controls lifecycle explicitly
)
```

Every restriction validates a specific requirement:

| Setting | Requirement |
| ------- | ----------- |
| `read_only=True` bind mount + `read_only=True` root FS | 2.1 (repository mounted read-only) |
| `tmpfs=/workspace/tmp` | 2.1 ("tmpfs volume for outputs") |
| `network_mode="none"` default | 2.1 (network none) |
| Allowlist branch uses custom network | 2.3 (egress restricted to allowlist) |
| `user="10001:10001"`, `cap_drop=["ALL"]`, `no-new-privileges` | Defense-in-depth on top of 2.1 |
| `mem_limit`, `pids_limit`, `cpu_quota` | Resource ceiling so a runaway pytest can't wedge the host |

### 5.3 Network allowlist (Requirement 2.3)

When `policy.network_allowlist` is non-empty:

1. Create a dedicated Docker bridge network at sandbox init: `docker network create trikon-verify-<uuid> --driver bridge --internal=false`.
2. Attach the container to that network instead of `network=none`.
3. Emit an iptables `OUTPUT` chain rule set inside the container's network namespace via a one-shot init sidecar: `iptables -A OUTPUT -d <cidr> -j ACCEPT` for each entry, then `iptables -A OUTPUT -j DROP` as the catch-all. Localhost (`127.0.0.0/8`) is always allowed.
4. On sandbox teardown, remove the container, then remove the network.

Allowlist entries are hostnames or CIDRs. Hostnames are resolved once at sandbox entry against the host resolver; the resolved IP set is what iptables rules pin. This is intentionally coarse — an allowlist of `pypi.org` cannot follow CNAME churn — but matches the Requirement-2.3 contract of "outbound egress for exactly that allowlist" without requiring an in-container DNS proxy.

### 5.4 Startup and dependency install

Inside `LocalDockerSandbox.__enter__`, after the container is `start()`ed but before returning to the caller:

```python
# 1. Verify the mount landed. If the runner sees an empty /workspace/repo,
#    something upstream (bad path, denied permission) failed silently.
self.exec(("test", "-d", "/workspace/repo/.git"), timeout_seconds=5.0)

# 2. Install the repo's dev dependencies. `--no-deps` matches Phase-1 tooling
#    already resident in the image; `-e .[dev]` picks up the repo's own pins.
result = self.exec(
    ("pip", "install", "--no-deps", "-e", ".[dev]"),
    workdir="/workspace/repo",
    env=(("PIP_CACHE_DIR", "/workspace/pip-cache"),),
    timeout_seconds=60.0,
)
if result.exit_code != 0:
    raise SandboxExecError(f"dep install failed: {result.stderr[:2000]}")
```

The pip cache lives in tmpfs. Two consecutive verdicts in the same container reuse it; the container itself is disposed on `__exit__`, so cross-verdict cache reuse requires the host-level Docker layer cache (which Docker manages autonomously). Good enough for the 15 s warm budget in §2.3.

### 5.5 Timeout enforcement (Requirement 2.2)

```python
def exec(self, argv, *, timeout_seconds=None):
    exec_id = self._client.api.exec_create(
        self._container.id, cmd=list(argv), user="10001:10001", workdir=workdir,
    )["Id"]
    started = time.monotonic()
    try:
        output = self._client.api.exec_start(exec_id, detach=False, stream=False)
    except docker.errors.APIError as exc:
        raise SandboxExecError(f"exec_start failed: {exc}") from exc
    inspect = self._client.api.exec_inspect(exec_id)
    if timeout_seconds is not None and (time.monotonic() - started) > timeout_seconds:
        # Signal the runner to synthesize an errored TestResult; do NOT raise
        # past the module boundary (Requirement 2.2 explicit).
        self._container.kill(signal="SIGKILL")
        return SandboxExecResult(
            exit_code=124, stdout="", stderr="sandbox exceeded deadline",
            duration_ms=int(timeout_seconds * 1000), timed_out=True,
        )
    return SandboxExecResult(
        exit_code=int(inspect["ExitCode"] or 0),
        stdout=output.decode("utf-8", errors="replace"),
        stderr="",
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=False,
    )
```

The runner's post-processing checks `result.timed_out`; when true, it emits a `TestReport(status="failed", failures=[TestResult(node_id="<sandbox>", outcome="errored", failure_summary="sandbox exceeded 5-minute deadline")])` and returns. `SandboxTimeoutError` is defined in the hierarchy for completeness but is never raised out of the module boundary.

**Validates: Requirements 2.1, 2.2, 2.3, 6.3.**

---

## 6. Test-selection algorithm

Pseudocode for `select_impacted_tests`:

```
INPUT:  conn (SQLite connection to state.db)
        impact (ImpactSet)
        repo_path (Path)
        base_sha (str | None)
        now (datetime, default utcnow)

STEP 1: Determine coverage-map freshness
  freshest_built_at ← SELECT MAX(built_at) FROM coverage_map
  if freshest_built_at is None:
      map_stale ← True                            # no map ever built
  else:
      age ← now - parse_iso8601(freshest_built_at)
      map_stale ← (age > timedelta(days=7))

STEP 2: For each changed symbol, look up its tests
  hits ← {}                                       # dict[symbol_qualified_name, tuple[node_id, ...]]
  misses ← []                                     # list[symbol_qualified_name]
  for sym in impact.changed_symbols:
      if map_stale or base_sha is None:
          misses.append(sym.qualified_name)
          continue
      row ← SELECT test_ids_json, built_against_sha, built_at
              FROM coverage_map
              WHERE qualified_name = ? AND built_against_sha = ?
              LIMIT 1
              PARAMS (sym.qualified_name, base_sha)
      if row is None:
          misses.append(sym.qualified_name)
          continue
      hits[sym.qualified_name] ← tuple(json.loads(row.test_ids_json))

STEP 3: If any symbol fell back OR the freshest row is stale, mark stale
  stale ← map_stale or bool(misses)

STEP 4: Filename heuristic for the miss set
  fallback_ids ← []
  for qname in misses:
      module_path ← _module_path_for_qname(qname)      # "payments.retry.with_backoff" → "payments/retry"
      leaf        ← module_path.rsplit("/", 1)[-1]
      pkg_prefix  ← module_path.rsplit("/", 1)[0] if "/" in module_path else ""
      candidates  ← [
          repo_path / "tests" / f"test_{leaf}.py",
          repo_path / "tests" / pkg_prefix / f"test_{leaf}.py" if pkg_prefix else None,
      ]
      for f in candidates:
          if f is not None and f.is_file():
              fallback_ids.append(str(f.relative_to(repo_path).as_posix()))

STEP 5: Union, dedupe, sort deterministically
  node_ids ← sorted(set(
      *[nid for tests in hits.values() for nid in tests],
      *fallback_ids,
  ))

STEP 6: Return
  RETURN SelectedTests(
      node_ids=tuple(node_ids),
      coverage_map_stale=stale,
      fallback_reasons=tuple(f"{q}: no coverage-map row" for q in misses),
  )
```

**Validates: Requirements 1.1 (Step 2 hits), 1.2 (Step 4 fallback), 1.3 (Step 1 + Step 3 staleness signal).**

Complexity: `O(|changed_symbols|)` SQLite lookups plus `O(|misses|)` disk stat calls. On the design budget (≤ 500 ms in §2.3) `|changed_symbols|` is small — a typical 3-file PR has < 20 changed symbols — so the algorithm is round-trip-bound, not compute-bound. `sqlite3.Connection` is opened once by `run_verification` and reused for the whole verdict.

---

## 7. Static-check diffing algorithm

Pseudocode for `run_static_checks`:

```
INPUT:  sandbox, conn, impact, repo_path, base_sha, head_sha, tools

STEP 1: For each tool, capture its version once per call
  tool_versions ← {}
  for tool in tools:
      r ← sandbox.exec(tool.version_command, timeout_seconds=10.0)
      tool_versions[tool.name] ← r.stdout.strip()   # e.g. "ruff 0.7.4"

STEP 2: For each tool, resolve or build the baseline
  baselines ← {}
  for tool in tools:
      row ← SELECT findings_json
              FROM static_baseline
              WHERE base_sha=? AND tool=? AND tool_version=?
              PARAMS (base_sha, tool.name, tool_versions[tool.name])
      if row is not None:
          baselines[tool.name] ← json.loads(row.findings_json)
          continue
      # Cache miss: materialize the base tree and run the tool against it.
      worktree ← git_worktree_add(repo_path, base_sha)
      try:
          base_findings ← _run_and_parse(sandbox, tool, worktree, impact.changed_files)
      finally:
          git_worktree_remove(worktree)
      baselines[tool.name] ← base_findings
      conn.execute(
          "INSERT INTO static_baseline (base_sha, tool, tool_version, findings_json, computed_at) "
          "VALUES (?, ?, ?, ?, ?)",
          (base_sha, tool.name, tool_versions[tool.name],
           json.dumps(base_findings), now_iso8601()),
      )
      conn.commit()

STEP 3: Run each tool at HEAD against the changed files
  head_by_tool ← {}
  for tool in tools:
      head_by_tool[tool.name] ← _run_and_parse(sandbox, tool, repo_path, impact.changed_files)

STEP 4: Compute is_new by set subtraction on (path, line, rule_id)
  findings ← []
  new_errors, new_warnings, preexisting_errors ← 0, 0, 0
  for tool in tools:
      base_keys ← { (f.path, f.line, f.rule_id) for f in baselines[tool.name] }
      for f in head_by_tool[tool.name]:
          key ← (f.path, f.line, f.rule_id)
          f_out ← _finding_to_public_dict(f, is_new=(key not in base_keys))
          findings.append(f_out)
          if f_out["is_new"]:
              if f.severity == "error":   new_errors += 1
              else:                        new_warnings += 1
          elif f.severity == "error":     preexisting_errors += 1

STEP 5: Return
  RETURN StaticReport(
      tools_run=[t.name for t in tools],
      new_errors=new_errors,
      new_warnings=new_warnings,
      preexisting_errors=preexisting_errors,
      findings=findings,
  )
```

Match rule for `is_new`: `(path, line, rule_id)` triple only. `message` deliberately does not participate because ruff and mypy sometimes reformat messages between patch versions (e.g. quoting style, snippet width) without the underlying diagnostic changing. Using message as a discriminator would produce false-positive "new" findings on tool upgrades — the exact pathology Requirement 3.3 asks us to invalidate at the tool-version boundary, not paper over per-finding.

**Git worktree**: base-tree materialization uses `git worktree add --detach <tmpdir> <base_sha>` on the host (outside the sandbox) so it does not pay the sandbox spin-up tax; the worktree path is then bind-mounted into the sandbox for the tool run. On any failure we still call `git worktree remove --force`, and if that fails we raise `StaticCheckError` and let the verdict fail closed.

**Validates: Requirements 3.1, 3.2, 3.3.**

---

## 8. Plugin loader

Pseudocode for `load_and_run_plugins`:

```
INPUT:  sandbox, repo_path, impact, per_plugin_timeout_seconds

STEP 1: Discover
  plugin_files ← sorted(
      (repo_path / ".trikon" / "checks").glob("*.py")
      if (repo_path / ".trikon" / "checks").is_dir()
      else []
  )
  plugin_files ← [p for p in plugin_files if not p.name.startswith("_")]

STEP 2: For each plugin, invoke it inside the sandbox
  results ← []
  for path in plugin_files:
      rel = path.relative_to(repo_path).as_posix()
      results.append(_run_one(sandbox, repo_path, rel, impact,
                              timeout_seconds=per_plugin_timeout_seconds))
  return tuple(results)


FUNCTION _run_one(sandbox, repo_path, plugin_rel_path, impact, *, timeout_seconds):
  # The plugin is imported *inside* the sandbox, not in the host process.
  # We serialize the ImpactSet + CheckContext seed to /workspace/tmp/plugin_input.json,
  # then run a shim script that:
  #   1. importlib.util.spec_from_file_location(...)
  #   2. inspect.iscoroutinefunction(module.check) → reject
  #   3. call module.check(ctx), catch, dump findings JSON to /workspace/tmp/plugin_output.json.
  #
  # Failures at any of steps 1–3 are recorded in the JSON output as
  # {"error": "<repr>"} rather than propagating; the sandbox process
  # exits with code 0 either way so exec never raises past the boundary.
  input_json ← serialize(impact, repo_path, plugin_rel_path)
  sandbox.exec(("mkdir", "-p", "/workspace/tmp"), timeout_seconds=5.0)
  sandbox.exec(("sh", "-c", f"cat > /workspace/tmp/plugin_input.json"),
               stdin=input_json, timeout_seconds=5.0)
  exec_result ← sandbox.exec(
      ("python", "/workspace/repo/.trikon/_plugin_shim.py"),
      timeout_seconds=timeout_seconds,
  )
  if exec_result.timed_out:
      return PluginResult(plugin=plugin_rel_path, findings=[],
                          error=f"plugin exceeded {timeout_seconds}s timeout")
  output ← sandbox.exec(("cat", "/workspace/tmp/plugin_output.json"), timeout_seconds=5.0)
  parsed ← json.loads(output.stdout)
  if "error" in parsed:
      return PluginResult(plugin=plugin_rel_path, findings=[], error=parsed["error"])
  return PluginResult(plugin=plugin_rel_path,
                      findings=[_finding_to_public_dict(f) for f in parsed["findings"]])
```

Two design notes:

1. **`.trikon/_plugin_shim.py` is a Trikon-owned file materialized inside the sandbox at container start** (bind-mounted from the installed `trikon` package via a second read-only mount at `/workspace/repo/.trikon/_plugin_shim.py`). We do not read the shim's source from the repo, so a malicious `.trikon/_plugin_shim.py` in a target repo cannot override the loader. The bind-mount source is the on-disk path of the installed `trikon.verify._plugin_shim` module.
2. **`CheckContext`** already exists at `trikon/verify/plugins.py` as a plain dataclass with `repo_path`, `changed_files`, and `read_bytes`. The shim reconstructs an equivalent object inside the sandbox from the serialized JSON input. The user-visible plugin API is the dataclass form; the wire format is JSON.

Async rejection (Requirement 4.3) is a one-liner in the shim:

```python
if inspect.iscoroutinefunction(module.check):
    return {"error": "async plugins not supported in Phase 2"}
```

Load failures, `check` missing, `check` not callable, and any exception raised by `check` are all caught by the shim's outer `try/except Exception as exc: return {"error": repr(exc)}`. The runner never sees a Python traceback from a plugin.

**Validates: Requirements 4.1, 4.2, 4.3.**

---

## 9. `VerificationRunnerError` hierarchy

Two files change: a new `trikon/exceptions.py` for the shared `TrikonError` root, and a new `trikon/verify/errors.py` for the Phase-2 subclasses. `trikon/change_intel/errors.ChangeIntelError` is re-parented under `TrikonError` so both subsystems share one root the SDK boundary can catch when Phase 3 rolls up the two `try` blocks into one.

```python
# trikon/exceptions.py  (NEW)
"""Shared exception root for every Trikon subsystem.

Phase 1 introduced ``trikon.change_intel.errors.ChangeIntelError`` as a
direct subclass of ``Exception``; Phase 2 lifts the common root out so
``sdk.verify`` can catch a single class when Phase 3 fuses the
change-intel and verification try blocks. ``ChangeIntelError`` is
re-parented under ``TrikonError`` here (see the docstring in
``trikon/change_intel/errors.py`` for the compatibility shim)."""

from __future__ import annotations


class TrikonError(Exception):
    """Root of the Trikon exception tree."""


# trikon/verify/errors.py  (NEW)
"""Exception hierarchy for the Verification Runner subsystem.

Every raise site under ``trikon.verify`` MUST use one of the classes
defined here. Nothing raises bare ``Exception``, ``ValueError``,
``docker.errors.*``, ``subprocess.CalledProcessError``, or
``sqlite3.Error`` past the module boundary — that closure is what lets
``trikon.sdk.verify`` translate any internal failure into a
``require_human`` verdict without ambiguity.

See requirements.md §Requirement 6 and design.md §12."""

from __future__ import annotations

from trikon.exceptions import TrikonError


class VerificationRunnerError(TrikonError):
    """Base class for every error raised by :mod:`trikon.verify`."""


class SandboxUnavailableError(VerificationRunnerError):
    """The Docker daemon cannot be reached.

    Raised on ``docker.from_env`` failure (socket missing, daemon down,
    permission denied). The diagnostic message MUST include the socket
    path that was attempted (Requirement 6.3)."""


class SandboxExecError(VerificationRunnerError):
    """A non-timeout container operation failed.

    Raised for ``docker.errors.APIError``, image-pull failures, mount
    validation failures, OOM kills detected via ``container.attrs['State']['OOMKilled']``,
    and any ``exec_create`` / ``exec_start`` failure."""


class SandboxTimeoutError(VerificationRunnerError):
    """Container exceeded its wall-clock deadline.

    Defined for completeness but never raised past the module boundary
    (Requirement 2.2). The runner internally converts a timed-out
    ``SandboxExecResult`` into a synthesized failed ``TestReport`` and
    returns normally. Present in the hierarchy so unit tests can
    parametrize by exception class without a special case."""


class TestSelectionError(VerificationRunnerError):
    """``select_impacted_tests`` could not query the coverage map.

    Raised on ``sqlite3.Error`` from the ``coverage_map`` lookup, and on
    malformed ``test_ids_json`` payloads (JSON parse failure or shape
    mismatch)."""


class StaticCheckError(VerificationRunnerError):
    """``run_static_checks`` failed to produce a report.

    Raised on git-worktree materialization failure, tool-version capture
    failure, JSON parse failure of ruff's ``--output-format=json`` output,
    and any ``sqlite3.Error`` writing the baseline row."""


class PluginLoadError(VerificationRunnerError):
    """Plugin discovery or shim invocation failed at the sandbox level.

    Note this is NOT raised for per-plugin failures — those are recorded
    on the returned ``PluginResult``. This class is reserved for
    infrastructure problems the runner cannot recover from (shim missing,
    ``.trikon/checks/`` unreadable, JSON output file missing)."""


class CoverageBuildError(VerificationRunnerError):
    """``build_coverage_map`` failed during test collection or execution.

    On any raise, the caller's previously-persisted ``coverage_map`` /
    ``tests_seen`` rows are left untouched (Requirement 5.3)."""
```

### 9.1 Raise sites and boundary behavior

| Class | Raise site | Boundary behavior |
| ----- | ---------- | ----------------- |
| `SandboxUnavailableError` | `LocalDockerSandbox.__enter__` when `docker.from_env()` raises | Bubbles to `sdk.verify`; verdict is `require_human` + `EMPTY_VERIFICATION` |
| `SandboxExecError` | `LocalDockerSandbox.exec`, `mount_repo`, dep-install step | Bubbles to `sdk.verify`; same treatment |
| `SandboxTimeoutError` | Reserved; **not raised** past the runner. Runner synthesizes a failed `TestReport` instead. | Verdict returns normally with `verification.tests.status="failed"` (Requirement 2.2) |
| `TestSelectionError` | `select_impacted_tests` | Bubbles; verdict is `require_human` |
| `StaticCheckError` | `run_static_checks` | Bubbles; verdict is `require_human` |
| `PluginLoadError` | `load_and_run_plugins` infrastructure paths only | Bubbles; verdict is `require_human` |
| `CoverageBuildError` | `build_coverage_map` | Bubbles to the CLI (`trikon coverage build`), which prints and exits 1 |

Per-plugin failures (Requirement 4.2) never raise `PluginLoadError`; they are recorded on the individual `PluginResult` and execution continues with the next plugin.

**Validates: Requirements 6.1, 6.2, 6.3.**

---

## 10. SDK integration

Before/after of `trikon/sdk.py::verify`:

```python
# BEFORE (Phase 1)
def verify(repo_path, base_sha=None, head_sha=None, diff=None, *,
           policy_path=".trikon/policy.yaml", cache_db=None) -> Verdict:
    try:
        change_set = parse_diff(repo_path, base_sha=base_sha,
                                head_sha=head_sha, diff=diff)
        impact = compute_impact(change_set, repo_path, cache_db=cache_db)
    except ChangeIntelError as exc:
        return _fail_closed_verdict(
            reason=f"change-intel error: {type(exc).__name__}: {exc}",
        )
    return Verdict(
        decision="require_human",
        reason=_PHASE_1_REASON,
        matched_rule=None,
        evidence=Evidence(
            change=impact,
            verification=EMPTY_VERIFICATION,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )
```

```python
# AFTER (Phase 2)
def verify(repo_path, base_sha=None, head_sha=None, diff=None, *,
           policy_path=".trikon/policy.yaml", cache_db=None) -> Verdict:
    repo = Path(repo_path)
    try:
        change_set = parse_diff(repo, base_sha=base_sha,
                                head_sha=head_sha, diff=diff)
        impact = compute_impact(change_set, repo, cache_db=cache_db)
        verification = run_verification(
            repo,
            impact,
            state_db=cache_db,
        )
    except (ChangeIntelError, VerificationRunnerError) as exc:
        return _fail_closed_verdict(
            reason=f"{type(exc).__name__}: {exc}",
        )
    return Verdict(
        decision="require_human",   # Phase 3 will run the policy engine here
        reason=_PHASE_2_REASON,
        matched_rule=None,
        evidence=Evidence(
            change=impact,
            verification=verification,
            policy_results=[],
        ),
        audit_id=uuid4(),
        created_at=datetime.now(UTC),
    )
```

Three deltas:

1. One additional call to `run_verification` inside the same `try` block.
2. The `except` clause widens from `ChangeIntelError` to `(ChangeIntelError, VerificationRunnerError)`. Both are subclasses of the new `TrikonError` root, so Phase 3 will collapse this to `except TrikonError as exc:` when the policy engine adds a third subsystem.
3. `evidence.verification` now carries a real report on the happy path. The `EMPTY_VERIFICATION` sentinel remains embedded on the fail-closed path.

Decision stays `require_human` because the policy engine (Phase 3) has not shipped — the SDK has verification evidence but nothing to grade it against.

**Validates: Requirements 6.2, 7.1.**

---

## 11. CLI surfaces

### 11.1 `trikon debug verify`

```
trikon debug verify --repo <path> --base <sha> --head <sha> [--json]
                    [--policy <path>] [--cache-db <path>]
```

Human-formatter output (default):

```
Trikon verification for <repo> (base: abc123 → head: def456)

Change: 3 files, 5 symbols, MEDIUM blast radius

Tests:  passed=27  failed=2  skipped=1  (30 total, 4.1 s)
        First 5 failing tests:
          tests/test_worker.py::test_backoff_shape
          tests/test_worker.py::test_backoff_max_retries
        (coverage map fresh)

Static: ruff  new=1  preexisting=4      (0.3 s)
        mypy  new=0  preexisting=2      (1.9 s)

Plugins: no_direct_sql   1 finding
         audit_metadata  0 findings

Sandbox: 8.4 s wall clock (started, ran, torn down cleanly)

Verdict: require_human — Phase 2: policy engine ships in Phase 3
```

`--json` output:

```
$ trikon debug verify --repo … --base … --head … --json
{
  "decision": "require_human",
  "reason": "Phase 2: policy engine ships in Phase 3",
  ...
  "evidence": {
    "change": { ... ImpactSet ... },
    "verification": {
      "tests": { ... TestReport ... },
      "static": { ... StaticReport ... },
      "plugins": [ ... PluginResult, ... ],
      "sandbox_ms": 8391,
      "total_ms": 9204
    },
    "policy_results": []
  },
  "audit_id": "…",
  "created_at": "2025-11-19T12:34:56.789Z",
  "schema_version": 1
}
```

Implementation extends the Phase-1 `trikon debug` Typer sub-app in `trikon/cli.py`. Exit codes:

| Condition | Exit code |
| --------- | --------- |
| Any well-formed `Verdict` (including `require_human`) | **0** — a valid verdict is a successful CLI invocation |
| Uncaught Python exception (i.e. the never-fail-open closure escaped) | **1** — should be unreachable; alerting fires if this happens in CI |
| CLI usage error (missing `--base` without `--diff-file`) | **2** — matches the existing `trikon debug impact` convention |

### 11.2 `trikon coverage build`

```
trikon coverage build --repo <path> [--cache-db <path>]
```

Runs `build_coverage_map`. Output:

```
$ trikon coverage build --repo examples/sample_repo
Building coverage map for examples/sample_repo (HEAD: def456)…
Ran 12 tests in 8.7 s
Indexed 40 symbols → 12 tests
Wrote 40 rows to coverage_map, 12 rows to tests_seen
Coverage map is fresh at 2025-11-19T12:34:56Z
```

Exit codes:

| Condition | Exit code |
| --------- | --------- |
| Success | 0 |
| `CoverageBuildError` | 1 |
| CLI usage error | 2 |

**Validates: Requirements 5.1, 7.2, 7.3.**

---

## 12. Error handling & fail-closed matrix

Every failure mode the sandbox stack can produce, where it is detected, what class it becomes, and what the SDK boundary emits.

| Failure mode | Detected at | Raised as | SDK-boundary outcome |
| ------------ | ----------- | --------- | -------------------- |
| Docker daemon down / socket missing | `LocalDockerSandbox.__enter__` (`docker.from_env`) | `SandboxUnavailableError` (msg includes socket path) | `require_human`, `EMPTY_VERIFICATION` |
| User lacks permission on Docker socket | `docker.from_env` → `PermissionError` | `SandboxUnavailableError` | `require_human` |
| Image pull failure (network, registry auth, missing tag) | Sandbox init `client.images.pull` | `SandboxExecError` (msg includes image tag + registry error) | `require_human` |
| Bind-mount rejected (path outside allowed roots) | `containers.create` | `SandboxExecError` | `require_human` |
| Container OOM-kill (`State.OOMKilled=true`) | `container.wait` | `SandboxExecError` (msg names the tool that OOMed) | `require_human` |
| Container exceeds `deadline_seconds` | `container.wait(timeout=…)` in `exec` | **Not raised** — synthesized failed `TestReport` (Requirement 2.2) | Verdict returns normally with `tests.status="failed"` |
| `git worktree add` fails (base sha absent, disk full) | `run_static_checks` | `StaticCheckError` | `require_human` |
| Ruff parse error (repo file is not valid Python) | Ruff exits non-zero with unparseable JSON | `StaticCheckError` on JSON parse; empty findings otherwise | `require_human` on JSON parse; otherwise proceeds with `is_new=True` for whatever ruff did emit |
| Mypy install failure inside the sandbox | Dep-install step | `SandboxExecError` | `require_human` |
| Plugin import error (`ImportError`, `SyntaxError`, missing `check`) | Inside plugin shim | Recorded on `PluginResult.error`; **not raised** | Verdict returns normally with the failed plugin recorded |
| Plugin runtime exception | Inside plugin shim `try/except` | Recorded on `PluginResult.error` | Same |
| Plugin `async def check` | Inside plugin shim `inspect.iscoroutinefunction` | Recorded on `PluginResult.error` (Requirement 4.3) | Same |
| Plugin infrastructure failure (shim missing, output JSON missing) | `load_and_run_plugins` | `PluginLoadError` | `require_human` |
| `coverage_map` DB write error (disk full, corruption) | `build_coverage_map` | `CoverageBuildError` | CLI exit 1 |
| Malformed `coverage_map.test_ids_json` (JSON parse failure) | `select_impacted_tests` | `TestSelectionError` | `require_human` |
| `static_baseline` DB write error | `run_static_checks` | `StaticCheckError` | `require_human` |
| SQLite `state.db` locked by another process | Any query on `conn` | Wrapped as the subsystem's error (`TestSelectionError`, `StaticCheckError`, `CoverageBuildError`) | `require_human` |

The invariant, encoded as a closure test: for every subclass of `VerificationRunnerError`, `sdk.verify` returns a `Verdict` with `decision == "require_human"` and `evidence.verification == EMPTY_VERIFICATION`. `allow` is never emitted on this path.

**Validates: Requirement 6 in full.**

---

## 13. Testing strategy

### 13.1 Layout

```
tests/
├── unit/
│   └── verify/
│       ├── test_test_selector.py
│       ├── test_sandbox.py
│       ├── test_static_checks.py
│       ├── test_plugins.py
│       ├── test_runner.py
│       └── strategies.py            # hypothesis generators
├── integration/
│   └── verify/
│       ├── test_sample_repo_bad_retry.py
│       ├── test_sample_repo_clean_refactor.py
│       ├── test_coverage_build.py
│       └── test_network_allowlist.py           # perf-marker, opt-in
└── benchmarks/
    └── test_verify_perf.py
```

### 13.2 Unit tests per module

- **`test_test_selector.py`**
  - Coverage-map hit: seeded row → exact node IDs returned; `coverage_map_stale=False`.
  - Coverage-map miss + fallback: no seeded row, on-disk `tests/test_leaf.py` present → fallback fires; `fallback_reasons` populated.
  - Staleness by age: seed row with `built_at = now - 8 days` → `coverage_map_stale=True`, every symbol routes through fallback.
  - Staleness by SHA drift: seed row with `built_against_sha != impact.base_sha` → same treatment.
  - Missing symbol (no row, no on-disk test file) → `node_ids` empty, `fallback_reasons` explains why.
- **`test_sandbox.py`**
  - Image absent locally + registry unreachable → `SandboxExecError`.
  - `docker.from_env()` raises → `SandboxUnavailableError` with socket path in message (parametrized over `unix:///var/run/docker.sock` and `npipe:////./pipe/docker_engine`).
  - Container timeout via a fake docker client whose `exec_start` sleeps past the deadline → `SandboxExecResult(timed_out=True, exit_code=124)`, no exception.
  - Read-only mount enforcement → `docker.types.Mount` kwargs include `read_only=True` (assertion on the call spec).
  - `network_mode='none'` when no allowlist; custom network name when allowlist present.
- **`test_static_checks.py`**
  - Baseline cache miss then hit: first call inserts a row, second call skips the base run (mock the sandbox executor and assert the base-run `exec` was called exactly once across two calls).
  - Tool-version invalidation: prime a row at `ruff 0.7.3`, run with `tool_version="ruff 0.7.4"` → cache miss, new row inserted.
  - No baseline for new files: `impact.changed_files` includes a file absent from `base_sha` → the baseline run reports zero findings for it; the head run's findings are all `is_new=True`.
  - Ruff JSON parse failure → `StaticCheckError`.
- **`test_plugins.py`**
  - Single plugin happy path: one file in `.trikon/checks/`, returns 3 findings → `PluginResult(findings=[…3…], error=None)`.
  - Plugin import error (`SyntaxError`) → `PluginResult(error="SyntaxError: …")`, subsequent plugin still runs.
  - Plugin runtime exception (raises `RuntimeError` inside `check`) → `PluginResult(error="RuntimeError: …")`.
  - Async plugin rejection: `async def check` → `PluginResult(error="async plugins not supported in Phase 2")`.
  - Plugin timeout: `check` sleeps past `per_plugin_timeout_seconds` → `PluginResult(error="plugin exceeded 30s timeout")`.
- **`test_runner.py`**
  - Report assembly: mock `select_impacted_tests`, `run_static_checks`, `load_and_run_plugins`; assert the returned `VerificationReport` composes their outputs verbatim.
  - Deadline plumbing: `deadline_seconds=1.0` reaches the sandbox `exec` timeout arg (assertion on the call spec).
  - Error wrapping: monkeypatch each dependency to raise; assert every raise site is caught and re-raised as a `VerificationRunnerError` subclass.

### 13.3 Integration tests (`tests/integration/verify/`)

- **`test_sample_repo_bad_retry.py`** — apply `bad_retry.patch` to a fresh clone of `examples/sample_repo/`, run `sdk.verify(repo, base_sha, head_sha)`, assert:
  - `verdict.evidence.verification.tests.status == "failed"`.
  - The failing node IDs include `tests/test_worker.py::test_backoff_shape`.
  - `verdict.evidence.verification.static.new_errors == 0` (the retry change is a logic bug, not a lint issue).
- **`test_sample_repo_clean_refactor.py`** — apply `clean_refactor.patch`, assert `verification.tests.status == "passed"`.
- **`test_coverage_build.py`** — run `trikon coverage build` end-to-end, assert:
  - `state.db.coverage_map` has ≥ 1 row per symbol in `src/**`.
  - `state.db.tests_seen` has one row per test node in `tests/**`.
  - `built_at` is within 5 s of test wall time.
  - `built_against_sha` equals `git rev-parse HEAD`.
- **`test_network_allowlist.py`** — perf-marker (`@pytest.mark.integration`) test that requires a live Docker daemon. Runs a sandbox with `network_allowlist=("pypi.org",)`, `curl pypi.org` inside → exit 0; `curl example.com` inside → non-zero.

### 13.4 Perf smoke test (`@pytest.mark.perf`, opt-in)

- Cold verdict on `sample_repo` bad_retry ≤ 60 s (Requirement 8.1).
- Warm verdict on same input ≤ 15 s (Requirement 8.2).
- `trikon coverage build` on `sample_repo` ≤ 30 s (Requirement 8.3).

Runs in the nightly CI job, not on every PR — matches the Phase-1 `perf` job in `.github/workflows/ci.yml`.

### 13.5 Coverage target

`coverage report --fail-under=85 --include='trikon/verify/*'`.

Phase 1 hit 90 %; Phase 2 lowers the bar to 85 % because sandbox code has ~50 lines of Docker-daemon-alive paths that are unreachable in unit tests and are exercised only by the integration tier. The 5 % gap is those code paths.

---

## 14. Performance analysis

Per-target expected cost on `examples/sample_repo` bad_retry, matched against Requirement 8:

| Requirement | Target | Expected (measured on Ryzen-7 dev laptop) | Slack |
| ----------- | ------ | ----------------------------------------- | ----- |
| 8.1 First-ever verdict (cold) | ≤ 60 s | ~37 s (§2.3 stage sum, cold column) | ~23 s |
| 8.2 Second verdict (warm) | ≤ 15 s | ~10 s (§2.3 stage sum, warm column) | ~5 s |
| 8.3 `trikon coverage build` on sample_repo | ≤ 30 s | ~9 s (12 tests × ~750 ms w/ coverage instrumentation) | ~21 s |
| 2.2 Per-verdict wall-clock ceiling | ≤ 300 s (5 min) | Enforced by `deadline_seconds=300.0` default in `run_verification` | Hard cap |

Design levers if any target slips:

1. **Warm-path optimization: skip pip install when it is a no-op.** Detect an unchanged `pyproject.toml` since the last verdict via `state.db` and skip stage 2 in §2.3. Recovers ~1 s warm.
2. **Skip static analysis when `impact.changed_files` contains no `.py`.** Trivial early-out; recovers ~4 s cold on doc-only PRs.
3. **Concurrent ruff + mypy** — deferred (§2.4 Non-goal). Recovers ~3 s cold if enabled. Reintroduces the timeout-accounting complexity we set aside.
4. **Sandbox image size reduction** — the 0.1.0 image is ~250 MB; the pull cost on stage 1 cold dominates. Distilling to `python:3.11-alpine` cuts to ~90 MB and ~5 s cold on typical broadband. Deferred pending glibc-vs-musl compat check for `mypy` (mypy's precompiled binary in the wheel is glibc-only).

**Validates: Requirement 8 in full.**

---

## 15. Alternatives considered

- **Coverage map in a separate `coverage.db`.** Rejected: consistency argues for a single `state.db` shared with Phase 1. Two files means two locking regimes and two backup surfaces for no gain.
- **Async plugin API in Phase 2.** Rejected: the Phase-0 stub is sync, the sandbox shim would need an event loop, and per-plugin timeouts get harder to bound. Requirement 4.3 explicitly makes async a load-time rejection.
- **Parallel pytest / ruff / mypy execution.** Rejected in Phase 2 for the reasons in §2.4 (timeout accounting, OOM headroom, budget slack sufficient). Revisit in Phase 3.
- **Warden sandbox integration.** Deferred to Phase 5 per `EXECUTION_PLAN.md`. The Phase-0 stub `UnideployWardenSandbox` in `trikon/verify/sandbox.py` remains a stub; Phase 2 does not import it and does not expose it via `run_verification`.
- **Recomputing static baseline every verdict.** Rejected: 2× sandbox cost per verdict (base run + head run) blows through the 15 s warm budget in §2.3. Cached per `(base_sha, tool, tool_version)`.
- **JSON-file coverage-map format.** Rejected: harder to diff, harder to query, inconsistent with the Phase-1 SQLite-in-`state.db` choice.
- **Host-process plugin execution (skip sandbox for `.trikon/checks/*.py`).** Rejected: a repo-supplied plugin is untrusted code, and executing it in the host process defeats the network isolation Requirement 2 is written for. Sandbox roundtrip cost is bounded by the per-plugin 500 ms budget in §2.3.
- **`pytest-testmon` for test selection.** Rejected: testmon's coverage database is a fourth SQLite file with its own schema drift, and its selection algorithm assumes an unbroken chain of green runs. We reproduce the useful subset (symbol → tests) in our own `coverage_map` table, keyed by a git SHA rather than a hash of every intermediate run, so a merged pull request never invalidates the map.

---

## 16. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property numbering aligns to the prework analysis stored in context. Every property below maps to one or more acceptance criteria in `requirements.md`.

### Property 1: Coverage-map hit completeness

*For any* repository state where every symbol in `impact.changed_symbols` has a `coverage_map` row keyed by `impact.base_sha`, `select_impacted_tests(conn, impact, base_sha=…)` returns a `SelectedTests` whose `node_ids` equals the sorted union of the `test_ids_json` arrays across those rows, with `coverage_map_stale = False`.

**Validates: Requirements 1.1**

### Property 2: Filename-heuristic fallback completeness

*For any* impacted symbol `s` with no `coverage_map` row for `(s.qualified_name, base_sha)`, if `tests/test_{leaf}.py` or `tests/{pkg}/test_{leaf}.py` exists on disk (where `leaf` and `pkg` are derived from `s.file_path`), those file paths appear in the returned `node_ids`; if neither exists, `s` contributes no node IDs and appears in `fallback_reasons`.

**Validates: Requirements 1.2**

### Property 3: Coverage-map staleness signal

*For any* `(freshest_built_at, base_sha_match)` pair, `select_impacted_tests` sets `coverage_map_stale = True` if and only if `(now - freshest_built_at) > timedelta(days=7)` OR any row's `built_against_sha != impact.base_sha`; a stale run routes every impacted symbol through the filename heuristic regardless of what rows are present.

**Validates: Requirements 1.3**

### Property 4: Timeout produces synthesized failed report, never raises

*For any* pytest invocation that exceeds `deadline_seconds`, `run_verification` returns a `VerificationReport` whose `tests.status == "failed"` and whose `tests.failures` contains exactly one `TestResult` with `outcome == "errored"` and `failure_summary` starting with `"sandbox exceeded"`; no exception escapes `run_verification`.

**Validates: Requirements 2.2**

### Property 5: New-vs-baseline finding delta

*For any* baseline finding set `B` and head finding set `H`, `run_static_checks` marks a finding `f ∈ H` with `is_new = True` if and only if the triple `(f.path, f.line, f.rule_id)` is absent from `{(b.path, b.line, b.rule_id) | b ∈ B}`; the finding's `message` field does not participate in the comparison.

**Validates: Requirements 3.1**

### Property 6: Static-baseline cache reuse

*For any* pair of successive verdicts against the same `(base_sha, tool, tool_version)`, the runner invokes the tool inside the sandbox against `base_sha` exactly once (on the first verdict); the second verdict reads the baseline from `static_baseline` and does not re-execute the tool against `base_sha`.

**Validates: Requirements 3.2**

### Property 7: Tool-version invalidates baseline cache

*For any* change to the pinned version of a static tool (as reported by its `version_command`), the next lookup against `static_baseline` for that tool misses regardless of `base_sha`, and a fresh baseline row is written under the new `tool_version`.

**Validates: Requirements 3.3**

### Property 8: Plugin findings preserved verbatim

*For any* set of `.trikon/checks/*.py` plugins each returning a list of `Finding` values, the returned `VerificationReport.plugins` contains one `PluginResult` per plugin file, and each `PluginResult.findings` equals the plugin's returned list (modulo dict-ification at the public boundary) with `error = None`.

**Validates: Requirements 4.1**

### Property 9: Plugin-failure isolation

*For any* mix of well-formed and failing plugins in `.trikon/checks/`, every failing plugin produces a `PluginResult` with a populated `error` field, every well-formed plugin still runs and produces its findings, and the order of returned `PluginResult` entries matches the sorted order of plugin file paths.

**Validates: Requirements 4.2, 4.3**

### Property 10: Coverage-build population completeness

*For any* successful `trikon coverage build` run against a repository whose test suite exercises symbol set `S` and produces test-node set `T`, the resulting `coverage_map` contains at least one row for every `s ∈ S` (keyed by `built_against_sha = git HEAD`), and `tests_seen` contains exactly one row per `t ∈ T` with `last_seen` set to the build time.

**Validates: Requirements 5.1, 5.2**

### Property 11: Coverage-build atomicity on failure

*For any* failure raised during a `build_coverage_map` invocation (sandbox failure, pytest collection error, SQLite write error), the previously-persisted `coverage_map` and `tests_seen` rows are byte-identical before and after the failed call, and the caller receives a `CoverageBuildError`.

**Validates: Requirements 5.3**

### Property 12: Error-hierarchy closure (static)

*For any* raise site within `trikon/verify/**` (enumerated by AST scan of the module tree), the exception class raised is a subclass of `VerificationRunnerError`; no bare `Exception`, `ValueError`, `docker.errors.*`, `subprocess.CalledProcessError`, or `sqlite3.Error` escapes the module boundary.

**Validates: Requirements 6.1**

### Property 13: Fail-closed at the SDK boundary (dynamic)

*For any* subclass of `VerificationRunnerError` raised inside `run_verification`, `sdk.verify` returns a `Verdict` with `decision == "require_human"` and `evidence.verification == EMPTY_VERIFICATION`; `allow` is never emitted on this path.

**Validates: Requirements 6.2**

### Property 14: CLI human-summary completeness

*For any* well-formed `VerificationReport`, the `trikon debug verify` human formatter emits five sections in order — change summary, test pass/fail counts with the first 5 failing node IDs, ruff new-vs-baseline counts, mypy new-vs-baseline counts, and sandbox wall-clock duration — and the `--json` flag replaces that formatter with `print(verdict.model_dump_json(indent=2))`.

**Validates: Requirements 7.2, 7.3**

---

## 17. Open questions for Phase 3+ (out of scope)

Written down here so we do not have to remember them. None block starting Phase 2.

1. **Cross-language sandboxes.** Node, Go, and Rust images live at `trikon/sandbox-node:*`, `trikon/sandbox-go:*`, etc. Selection happens on `impact.changed_files` extension histogram; multi-language changes fan out to multiple containers. Deferred to Phase 4.
2. **Coverage-map auto-rebuild on staleness.** Requirement 1.3 currently fires the fallback and flips a flag; the rebuild is manual (`trikon coverage build`) or a nightly job. Auto-rebuild would need a policy for when to spend the extra sandbox time — probably "if the last full build is > 7 days old AND the verdict is `allow`-eligible under the policy engine". Deferred to Phase 3 pending policy-engine design.
3. **Remote sandbox (Warden reuse from Unideploy).** The Phase-0 stub `UnideployWardenSandbox` is unused in Phase 2; Phase 5 wires it as an alternate backend controlled by a `sandbox_backend` policy key. Warden's mTLS bootstrap and network model differ enough that the abstraction will not be a straight refactor.
4. **Structured plugin manifest instead of module discovery.** A `.trikon/checks/checks.yaml` file declaring plugins by name, entry point, and enabled flag would replace glob discovery. Better UX for turning checks off temporarily without deleting the module. Deferred to Phase 3.
5. **Coverage-map delta on a successful verdict.** Every green verdict has a fresh `symbol → test_ids` observation the runner could fold back into `coverage_map` without paying a full-suite rebuild. Needs a strategy for reconciling deltas against a periodically-rebuilt full map. Deferred.
