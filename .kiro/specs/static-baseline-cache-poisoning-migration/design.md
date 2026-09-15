# Design Document

## 1. Overview

Trikon v0.3.5 landed the head-side `--cache-dir=` argv strip in `LocalSubprocessSandbox.exec` that fixed the `--no-sandbox` static-analysis divergence on repositories with no pre-existing `.trikon/state.db`. Post-ship verification against `trikon==0.3.5` on PyPI + `suryansh639/trikon:0.3.5` on Docker Hub surfaced a second-order trap the v0.3.5 fix does not close: on a repository that carries a state.db written by any pre-v0.3.5 Trikon build, the `static_baseline` table holds rows whose `findings_json` payload is `[]`, and v0.3.5's fixed head-side finding-diff sees an empty baseline where it should see 97 real preexisting findings. Every real head-side finding then classes as `is_new=True`, silently inverting the counters and the terminal `decision`.

Concrete repro from the v0.3.5 post-ship checkpoint on WSL Ubuntu-22.04:

| Field | Fresh state.db (correct) | Pre-v0.3.5 state.db (poisoned) |
|---|---|---|
| `decision` | `block` | inverted (verdict-rule dependent) |
| `matched_rule` | `new static-analysis errors` | dependent on aggregate counts |
| `new_errors` | 33 | 130 |
| `new_warnings` | 8 | 0 |
| `preexisting_errors` | 97 | 0 |
| Required user action | none | `rm ~/click/.trikon/state.db` |

The required-user-action row is the tell. A user reading the v0.3.5 changelog has no reason to expect they must delete their state.db to observe the fix. The observed divergence is silent — the return value of `sdk.verify(...)` is well-formed, the audit-log row is well-formed, no error is raised — so a CI pipeline that trusts the counters will see wrong data and take the wrong action on the first post-upgrade run.

The fix in this spec is a one-time-per-user schema-versioning migration hosted in a new module `trikon.verify.state_migrations`. The migration is hooked into `trikon.verify.runner._open_state_db` (the Verification-Runner's state-db opener) immediately after `ensure_verify_tables(conn)` and immediately before the connection is returned. It introduces a Phase-2-specific version marker `schema_meta.verify_schema_version` (independent of Phase-1's `schema_meta.schema_version`). On any state.db that lacks the marker, it issues `DELETE FROM static_baseline`, stamps `verify_schema_version = '1'`, and emits a single-line WARNING-level log record on `logging.getLogger("trikon.verify.state_migrations")` when the delete affected one or more rows. On any state.db that already carries the marker, it is a no-op — a subsequent `trikon verify` invocation reads the marker, takes the fast path, and returns without touching `static_baseline`.

This is a **bugfix**. Public API shapes are unchanged. `Static_Baseline_Table` columns are unchanged. `ensure_verify_tables` remains additive-only. The migration operates on rows (DELETE + INSERT), not on schema (no ALTER, no DROP TABLE). The v0.3.6 release commit that lands this bugfix also bumps `pyproject.toml` and four sandbox image tag references from `:0.3.5` → `:0.3.6` — that is release plumbing, called out in §12 as a follow-up for the release engineer and NOT part of this spec's implementation tasks.

## 2. Root Cause Diagnosis

Four anchors in the source tree establish the trap and pin the fix.

**Anchor 1 — the poisoned-row write path (single, unambiguous)**, `trikon/verify/static_checks.py::_resolve_base_keys` (currently the only INSERT into `static_baseline`):

```python
try:
    conn.execute(
        "INSERT INTO static_baseline "
        "(base_sha, tool, tool_version, findings_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            base_sha,
            tool.name,
            tool_version,
            json.dumps(base_findings),
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
except sqlite3.Error as exc:
    raise StaticCheckError(...)
```

The `base_findings` list is produced by `_run_baseline_tool_on_host`. When that host-side subprocess returns empty stdout (missing binary, misconfigured PATH, cache-init failure on a sandbox-only path — any pre-v0.3.6 mechanism), `_parse_ruff_json("")` and `_parse_mypy_text("")` both return `[]`, and `json.dumps([])` produces the literal string `'[]'`. That literal reaches `Static_Baseline_Table` as a persisted row. There is exactly one INSERT site — the head-side sandbox never writes to this table — so the surface to close is small and well-scoped.

**Anchor 2 — the poisoned-row read path**, `trikon/verify/static_checks.py::_resolve_base_keys` (currently around the "Cache read" comment):

```python
try:
    cursor = conn.execute(
        "SELECT findings_json FROM static_baseline "
        "WHERE base_sha = ? AND tool = ? AND tool_version = ?",
        (base_sha, tool.name, tool_version),
    )
    row = cursor.fetchone()
except sqlite3.Error as exc:
    raise StaticCheckError(...)

if row is not None:
    # Cache hit: decode and reduce.
    findings_json_val = row[0]
    cached_payload_str = findings_json_val if isinstance(findings_json_val, str) else ""
    try:
        cached_payload = json.loads(cached_payload_str)
    except json.JSONDecodeError as exc:
        raise StaticCheckError(...)
    return _extract_finding_keys(cached_payload)
```

When the cache hit returns a poisoned row (`findings_json = '[]'`), `_extract_finding_keys([])` returns `frozenset()`. The subsequent Step 4 diff in `run_static_checks` (`key not in base_keys` for every parsed head finding, with `base_keys` empty) classifies every head finding as `is_new=True`. This is the observable regression.

**Anchor 3 — the state.db opener that must host the migration**, `trikon/verify/runner.py::_open_state_db` (currently around line 736):

```python
def _open_state_db(state_db: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(state_db))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
    except sqlite3.Error as exc:
        raise TestSelectionError(
            f"run_verification: failed to open state.db at {state_db}: {exc}"
        ) from exc

    try:
        ensure_verify_tables(conn)
    except VerificationRunnerError:
        conn.close()
        raise

    return conn
```

The migration's insertion point is between `ensure_verify_tables(conn)` and `return conn`. Every `run_verification` call — the sole entry point of the Verification-Runner subsystem — funnels through this function, so hooking the migration here is sufficient to guarantee it fires on every `trikon verify` invocation.

**Anchor 4 — Phase-1's existing schema-versioning discipline, which the Phase-2 migration mirrors**, `trikon/change_intel/dep_graph.py::DepGraph._init_schema` (currently around line 556):

```python
if has_meta:
    version_cursor = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    )
    version_row = version_cursor.fetchone()
    if version_row is not None:
        raw_version = version_row[0]
        try:
            stored_version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise DepGraphError(
                f"corrupt schema_meta: schema_version={raw_version!r}"
            ) from exc

        if stored_version > CURRENT_SCHEMA_VERSION:
            raise DepGraphError(
                f"state.db was written by a newer Trikon (schema_version={stored_version}, ...); "
                f"upgrade Trikon or delete .trikon/state.db"
            )
        if stored_version < CURRENT_SCHEMA_VERSION:
            self._run_migrations(stored_version)
```

The Phase-1 discipline reads `schema_meta.schema_version`, refuses future versions, dispatches to `_run_migrations` on older versions. Phase 2's migration reads `schema_meta.verify_schema_version` (a NEW key) with the identical control flow — future value refused, missing value triggers the drop-and-stamp, matching value fast-paths. The two version markers evolve on independent cadences: Phase 1's `schema_version` stays at `1` for this bugfix, Phase 2's `verify_schema_version` starts at `1` in v0.3.6.

### The exact upstream poisoning mechanism is not disambiguated

Multiple pre-v0.3.6 code paths could have persisted an empty `findings_json` payload:

- A pre-v0.3.5 `_run_baseline_tool_on_host` may have lacked the venv PATH augmentation now present, so `shutil.which("ruff")` returned `None` on a `.venv`-only install and the subsequent `subprocess.run(["ruff", …])` returned an empty stdout without raising (behavior depends on Python's exact `subprocess.run` semantics with an unresolvable argv[0]).
- A pre-v0.3.3 code path (before the `--cache-dir=` strip landed in `_run_baseline_tool_on_host`) would have passed the sandbox-only `--cache-dir=/workspace/tmp/.<tool>_cache` flag through to the host-side subprocess, and ruff/mypy would have failed cache init on the missing host path — producing empty stdout on some tool versions.
- An out-of-tree caller (a plugin, a hand-crafted `sdk.verify(...)` call) may have persisted an empty findings blob directly.

The bugfix is **invariant across all such mechanisms**. The observable evidence — a state.db with `static_baseline` rows carrying `findings_json = '[]'` and no `schema_meta.verify_schema_version` key — is the same regardless of which upstream path wrote the row, and the fix's contract ("drop every row that predates the v0.3.6 marker") is the same. The Clarify phase did not further pin the exact upstream cause because the fix's correctness does not depend on it.

## 3. Chosen Fix Strategy

**Schema-version bump + `DELETE FROM static_baseline` on migration, hosted in a new `trikon.verify.state_migrations` module and hooked into `trikon.verify.runner._open_state_db` after `ensure_verify_tables(conn)`.**

This is the fix strategy selected in Clarify Answer 1 (schema-version bump + drop static_baseline) combined with Clarify Answer 2 (new `trikon/verify/state_migrations.py` module + hook in `_open_state_db`). The version marker lives at `schema_meta.verify_schema_version`, independent of Phase-1's `schema_meta.schema_version` (which stays at `1`), so Phase-2 migration cadence does not couple to Phase-1 schema evolution.

### 3.1 Module surface

New file `trikon/verify/state_migrations.py`:

```python
"""Verification-Runner state.db row-level migrations.

This module owns the row-level migration discipline for the Phase-2
sibling tables in ``<repo>/.trikon/state.db`` (``coverage_map``,
``tests_seen``, ``static_baseline``). It is a peer to
:mod:`trikon.verify.db` — that module owns the additive-only DDL
(``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``);
this module owns the row-level state that must be dropped or
transformed on Trikon version bumps.

Phase-2 version marker
----------------------

The marker key is ``schema_meta.verify_schema_version`` (string
representation of an ``int``). It is independent of Phase-1's
``schema_meta.schema_version`` (owned by
:data:`trikon.change_intel.dep_graph.CURRENT_SCHEMA_VERSION`) — the
two versions evolve on separate cadences. A Phase-2 migration does
NOT bump Phase-1's marker, and Phase-1's schema-init does NOT read
Phase-2's marker.

Bugfix history
--------------

* v0.3.6 introduces ``CURRENT_VERIFY_SCHEMA_VERSION = 1``. The
  associated migration drops every row from ``static_baseline`` on
  any state.db that lacks the marker, closing the cache-poisoning
  trap where pre-v0.3.6 Trikon builds could persist an empty
  ``findings_json`` payload. See spec
  ``static-baseline-cache-poisoning-migration``.
"""

from __future__ import annotations

import logging
import sqlite3

from trikon import __version__ as _TRIKON_VERSION
from trikon.verify.errors import VerificationRunnerError

__all__ = [
    "CURRENT_VERIFY_SCHEMA_VERSION",
    "maybe_migrate_verify_state",
]

CURRENT_VERIFY_SCHEMA_VERSION: int = 1

_VERIFY_SCHEMA_VERSION_KEY: str = "verify_schema_version"

_logger = logging.getLogger(__name__)


def maybe_migrate_verify_state(conn: sqlite3.Connection) -> None:
    """Fire the Phase-2 row-level migration on the connection if needed.

    On any state.db whose ``schema_meta`` lacks the
    ``verify_schema_version`` key, this function issues
    ``DELETE FROM static_baseline``, stamps
    ``verify_schema_version = str(CURRENT_VERIFY_SCHEMA_VERSION)``,
    and commits the DELETE + INSERT as a single atomic transaction.
    When the delete affected one or more rows, a single WARNING-level
    log record is emitted on this module's logger naming the count
    dropped and the Trikon version that fired the migration.

    On any state.db whose ``schema_meta`` already carries the marker,
    this function executes exactly one ``SELECT`` (the marker probe)
    and returns without side effects — the fast path.

    Args:
        conn: An open :class:`sqlite3.Connection` to the repo's
            ``state.db``. The caller (``_open_state_db``) is
            expected to have already applied the Phase-1 pragmas
            (``journal_mode=WAL``, ``synchronous=NORMAL``,
            ``foreign_keys=ON``, ``temp_store=MEMORY``) and run
            :func:`trikon.verify.db.ensure_verify_tables` so
            ``static_baseline`` is guaranteed present.

    Raises:
        VerificationRunnerError: On any :class:`sqlite3.Error` from
            the version-marker probe, the DELETE, the INSERT, or on a
            corrupt ``verify_schema_version`` value that does not
            parse as an integer. The original exception is preserved
            on ``__cause__`` via ``raise ... from exc``. Also raised
            when the stored ``verify_schema_version`` parses to an
            integer strictly greater than
            :data:`CURRENT_VERIFY_SCHEMA_VERSION` — the state.db was
            written by a newer Trikon build and this build is not
            qualified to read from it.
    """
    # (implementation below)
```

The implementation follows Phase 1's discipline verbatim (Anchor 4 above): read the marker, refuse future versions, dispatch on older or missing values. The concrete algorithm:

```python
    # 1. Idempotently ensure schema_meta exists.
    #
    # In-tree, DepGraph._init_schema always creates schema_meta before
    # _open_state_db runs (compute_impact precedes run_verification in
    # every sdk.verify(...) call). Out-of-tree callers that invoke
    # run_verification directly may open a state.db with no schema_meta
    # table yet — the additive-only CREATE below unbreaks that path
    # without touching in-tree behavior.
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    except sqlite3.Error as exc:
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: failed to ensure schema_meta exists"
        ) from exc

    # 2. Version-marker probe.
    try:
        cursor = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_VERIFY_SCHEMA_VERSION_KEY,),
        )
        row = cursor.fetchone()
    except sqlite3.Error as exc:
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: verify_schema_version probe failed"
        ) from exc

    if row is not None:
        raw_value = row[0]
        try:
            stored_version = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise VerificationRunnerError(
                f"maybe_migrate_verify_state: corrupt verify_schema_version="
                f"{raw_value!r}"
            ) from exc

        if stored_version > CURRENT_VERIFY_SCHEMA_VERSION:
            raise VerificationRunnerError(
                f"maybe_migrate_verify_state: state.db was written by a newer "
                f"Trikon (verify_schema_version={stored_version}, this build "
                f"supports up to {CURRENT_VERIFY_SCHEMA_VERSION})"
            )
        # stored_version == CURRENT_VERIFY_SCHEMA_VERSION → fast path.
        # (A future stored_version < CURRENT_VERIFY_SCHEMA_VERSION branch
        # will land alongside a v1→v2 migration; today there is no such
        # branch — CURRENT_VERIFY_SCHEMA_VERSION == 1 is the initial
        # value, so ``stored_version < 1`` is unreachable via normal
        # writes and treated as an equality on the fast path.)
        return

    # 3. No marker present → migration fires.
    try:
        conn.execute("BEGIN IMMEDIATE")
        delete_cursor = conn.execute("DELETE FROM static_baseline")
        dropped_rows = delete_cursor.rowcount
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
            (_VERIFY_SCHEMA_VERSION_KEY, str(CURRENT_VERIFY_SCHEMA_VERSION)),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        # Best-effort rollback so the DELETE does not partially land
        # on a mid-transaction failure.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise VerificationRunnerError(
            "maybe_migrate_verify_state: static_baseline drop-and-stamp failed"
        ) from exc

    # 4. Notice.
    if dropped_rows > 0:
        _logger.warning(
            "Trikon v%s dropped %d stale row(s) from static_baseline "
            "(Trikon < 0.3.6 pre-migration cache; see spec "
            "static-baseline-cache-poisoning-migration).",
            _TRIKON_VERSION,
            dropped_rows,
        )
```

Design decisions:

- **Marker key name is `verify_schema_version`** — plain-english, parallel to Phase-1's `schema_version`. Not `verify_schema_v` or `phase2_version` or `state_migration_epoch`; the discipline is "one integer per Phase, stored in `schema_meta` under a keyed name that says what phase it tracks".
- **The marker is stored as a string** — matching the existing `schema_meta` schema (`value TEXT NOT NULL`). Parsed back with `int(...)` on read; parse failure is a wrapped `VerificationRunnerError`.
- **CREATE TABLE IF NOT EXISTS schema_meta at the top of the function**. Two-line safeguard against an out-of-tree caller that opens `_open_state_db` on a state.db never touched by Change-Intelligence. The DDL is byte-identical to Phase-1's DDL for `schema_meta`; running it on a `schema_meta`-present DB is a no-op.
- **The DELETE + INSERT run inside `BEGIN IMMEDIATE` / `COMMIT`**. If any statement raises, the whole transaction is rolled back — the DELETE does not partially land. `BEGIN IMMEDIATE` acquires a RESERVED lock immediately, so a concurrent racer either wins the lock (its migration commits, the loser's SELECT observes the marker on retry) or blocks briefly and then observes the winner's marker on its own retry.
- **`cursor.rowcount` reports the delete count.** SQLite's `sqlite3` module populates `rowcount` on `DELETE` (unlike some other DBAPI drivers). This is the same source of truth used for the notice condition (`if dropped_rows > 0`) and the log-message substitution.
- **`_logger.warning` uses the `%s`-style idiom** (`"Trikon v%s dropped %d …"`, `_TRIKON_VERSION`, `dropped_rows`), not an f-string. Handlers configured below WARNING pay no formatting cost — this matches Trikon's project-wide logging convention.
- **`_TRIKON_VERSION`** is imported at module-load time from `trikon.__init__` (which itself reads `importlib.metadata.version("trikon")` at import). No fallback probe inside the function.
- **`VerificationRunnerError`** is the exclusive raise class. No new bespoke exception subclass. This matches Requirement 6.10 (`State_Migrations_Module` public surface is limited to the two symbols).

### 3.2 Hook in `_open_state_db`

The insertion point in `trikon/verify/runner.py::_open_state_db` is between the existing `ensure_verify_tables(conn)` call and the `return conn` line. Exact diff shape:

```python
def _open_state_db(state_db: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(state_db))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
    except sqlite3.Error as exc:
        raise TestSelectionError(...) from exc

    try:
        ensure_verify_tables(conn)
    except VerificationRunnerError:
        conn.close()
        raise

    # v0.3.6: Phase-2 row-level migration. Drops any static_baseline
    # rows persisted by a pre-v0.3.6 Trikon build (verify_schema_version
    # absent from schema_meta) and stamps the marker. Idempotent on a
    # marker-present DB. See spec
    # static-baseline-cache-poisoning-migration.
    try:
        maybe_migrate_verify_state(conn)
    except VerificationRunnerError:
        conn.close()
        raise

    return conn
```

Two decisions worth calling out:

- **The migration is called AFTER `ensure_verify_tables`**, not before. `ensure_verify_tables` is idempotent and pays a negligible cost on an existing DB; running it first guarantees `static_baseline` exists before the DELETE, so a state.db opened by an old Trikon build (which by construction had `ensure_verify_tables` run) has both `static_baseline` and the missing `verify_schema_version` marker — the DELETE has something to target.
- **The migration's failure is treated identically to `ensure_verify_tables`'s failure** — `conn.close()` + re-raise. This matches Requirement 5.6 (`Open_State_DB_Site` closes the connection before re-raising) and preserves the existing exception-handling pattern.

The import wiring adds one line at the top of `runner.py`:

```python
from trikon.verify.state_migrations import maybe_migrate_verify_state
```

Alphabetized between `trikon.verify.plugins` and `trikon.verify.static_checks` in the existing block.

## 4. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property Reflection

Reviewing the properties identified in the prework analysis for redundancy:

- Prework 1.2 ("migration drops all pre-v0.3.6 rows atomically") and prework 1.3 ("on upgrade, the head-side finding-diff observes a cache miss") both express the same underlying invariant seen from two angles — the SQLite-level "static_baseline is empty after migration" and the diff-level "no cache hit against a poisoned key". They combine into one property whose body asserts both facets.
- Prework 2.1 ("poisoning identified by absence of marker"), prework 2.2 ("marker-present DB is a no-op"), and prework 2.3 ("v0.3.6-authored rows preserved") are three restatements of the same invariant: "a state.db carrying the marker retains every row in `static_baseline` across a `maybe_migrate_verify_state` call". They combine into one property.
- Prework 3.1 ("fast-path executes at most one SELECT and zero writes"), 3.2 ("second call in same process does not re-emit notice"), and 3.3 ("fast path is pure") all express fast-path purity. They combine into one property.
- Prework 4.1 ("emits one WARNING record when rows > 0") and 4.2 ("emits zero records when rows == 0") both describe the notice-firing condition. They combine into one property whose body covers both directions of the biconditional.
- Prework 4.4 ("notice names version, count, spec name") is a distinct property — a content invariant on the log message body, not the firing condition.

Consolidated property set below.

### Property 1: Migration drops every pre-v0.3.6 row and stamps the marker atomically

*For any* pre-v0.3.6 `State_DB` — modeled as an in-memory `sqlite3.Connection` post-`ensure_verify_tables` seeded with any finite collection of rows in `Static_Baseline_Table` (varied `base_sha`, `tool`, `tool_version`, and `findings_json` payloads including `'[]'` and non-empty JSON arrays) and NO `verify_schema_version` key in `Schema_Meta_Table` — after a single call to `maybe_migrate_verify_state(conn)`, `Static_Baseline_Table` SHALL be empty, `Schema_Meta_Table` SHALL contain the row `(key='verify_schema_version', value='1')`, and a subsequent `SELECT findings_json FROM static_baseline WHERE (base_sha, tool, tool_version) = ANY(<seeded triples>)` SHALL return no rows. On any simulated mid-transaction `sqlite3.Error` (via a connection wrapper that raises on the DELETE or the INSERT), the seeded rows SHALL be preserved (transaction rollback), no `verify_schema_version` row SHALL be inserted, and the outward exception SHALL be `VerificationRunnerError`.

**Validates: Requirements 1.2, 1.3, 5.2, 5.3, 5.5**

### Property 2: Marker-present `State_DB` is a fast-path no-op

*For any* `State_DB` whose `Schema_Meta_Table` already contains `verify_schema_version = '1'` — modeled as an in-memory `sqlite3.Connection` post-`ensure_verify_tables` with the marker seeded and any finite collection of rows in `Static_Baseline_Table` — after a call to `maybe_migrate_verify_state(conn)`, `Static_Baseline_Table` SHALL contain the same row set (byte-identical on `(base_sha, tool, tool_version, findings_json, computed_at)` for every row), the connection wrapper's recorded statement history SHALL contain exactly one executed `SELECT` (the marker probe) plus the idempotent `CREATE TABLE IF NOT EXISTS schema_meta` DDL and SHALL NOT contain any `DELETE`, `INSERT` (against `schema_meta` or `static_baseline`), `UPDATE`, `ALTER`, `DROP`, or new `CREATE INDEX` statement, and the `Migration_Notice_Logger` SHALL NOT have emitted any WARNING-or-higher record.

**Validates: Requirements 2.1, 2.2, 2.3, 3.1, 3.2, 3.3**

### Property 3: Notice fires if and only if the DELETE affected one or more rows

*For any* pre-v0.3.6 `State_DB` seeded with exactly N rows in `Static_Baseline_Table` (N ∈ ℕ, including N = 0) and no `verify_schema_version` marker, after a call to `maybe_migrate_verify_state(conn)` the count of WARNING-level log records emitted on `Migration_Notice_Logger` SHALL equal 1 if N > 0 and SHALL equal 0 if N == 0. When N > 0, the single record's rendered message SHALL contain the string form of N, the string form of `trikon.__version__`, and the literal substring `"static-baseline-cache-poisoning-migration"`.

**Validates: Requirements 4.1, 4.2, 4.4**

## 5. Threading Through Consumers

**No consumer of `_open_state_db` changes signature or behavior on the happy path.** The migration is entirely internal to the state-db-open sequence. `run_verification` continues to call `_open_state_db(resolved_state_db)`, gets back the same `sqlite3.Connection` shape, and hands it to `select_impacted_tests` and `run_static_checks` as before. The only observable behavior change is:

- On the first `run_verification` call against a pre-v0.3.6 state.db, `_open_state_db` returns a connection whose `static_baseline` table is empty (post-migration) and whose `schema_meta` table has one additional row. Downstream code — specifically `_resolve_base_keys` — takes the miss branch for every `(base_sha, tool, tool_version)` triple on this first call, materializes the base worktree, invokes `_run_baseline_tool_on_host`, and populates `static_baseline` with correct rows. The first call is measurably slower than a subsequent call (a full baseline capture instead of a cache hit), but the returned `Verdict` is correct.
- On every subsequent `run_verification` call against the same state.db, `_open_state_db` fast-paths through the migration (single SELECT, no writes, no notice), and `_resolve_base_keys` sees the correct v0.3.6-authored cache rows.

**No signature changes propagate.** `sdk.verify(...)`, `run_verification(...)`, `_open_state_db(...)`, `ensure_verify_tables(...)`, `_resolve_base_keys(...)`, `run_static_checks(...)` all keep their v0.3.5 signatures. The new symbol `maybe_migrate_verify_state(conn)` is added; nothing existing is renamed, removed, or widened.

**No changes to `Static_Baseline_Table` columns.** The version-marker discipline lives entirely in `Schema_Meta_Table`. Adding a column to `Static_Baseline_Table` was considered (Alternative B in §11) and rejected — `ensure_verify_tables`'s additive-only invariant would need to be relaxed, and the migration itself would need an `ALTER TABLE` which raises the schema-invariant surface area of the fix.

## 6. Never-Fail-Open Preservation

The migration introduces exactly four failure modes, all of them loud:

- **`sqlite3.Error` from the `CREATE TABLE IF NOT EXISTS schema_meta` DDL** → wrapped as `VerificationRunnerError`. The DDL is idempotent and cannot fail on any healthy connection; a failure here indicates a locked / read-only / corrupt state.db and MUST be surfaced.
- **`sqlite3.Error` from the marker-probe SELECT** → wrapped as `VerificationRunnerError`. Same rationale.
- **`sqlite3.Error` from the DELETE or INSERT inside the transaction** → wrapped as `VerificationRunnerError`, with a best-effort `ROLLBACK` first so the DELETE does not partially land.
- **`TypeError` / `ValueError` on `int(raw_value)`** for a corrupt `verify_schema_version` value → wrapped as `VerificationRunnerError` naming the offending value.
- **Stored `verify_schema_version` integer strictly greater than `CURRENT_VERIFY_SCHEMA_VERSION`** → raised directly as `VerificationRunnerError` naming both the stored value and the running-build's `CURRENT_VERIFY_SCHEMA_VERSION`. Matches Phase-1's discipline (Anchor 4) verbatim.

Every raise site uses `raise VerificationRunnerError(...) from exc` to preserve the originating exception on `__cause__`. `_open_state_db`'s existing exception-handling block (`except VerificationRunnerError: conn.close(); raise`) is widened to cover the new `maybe_migrate_verify_state` call with the identical shape — the connection is closed, the exception propagates to `run_verification`, which propagates to `sdk.verify`, which routes it into the `_fail_closed_verdict` path with `decision="require_human"`.

There is NO code path where a `sqlite3.Error` from the migration is caught and the migration silently proceeds. There is NO code path where the migration returns success while `static_baseline` still holds a poisoned row. The migration is atomic on success — DELETE + INSERT commit together, or neither commits.

## 7. Cache-Key and Determinism Discipline

The `static_baseline` cache key remains the `(base_sha, tool.name, tool_version)` triple defined by the `UNIQUE (base_sha, tool, tool_version)` constraint in `ensure_verify_tables`. This bugfix does NOT widen the cache key — the version discipline lives in `schema_meta`, not in the row.

Consequence: after migration, a v0.3.6 `_resolve_base_keys` call against a `(base_sha, tool, tool_version)` triple that previously hit a poisoned row will now miss (the row is deleted) and rebuild via the normal miss branch. Subsequent calls against the same triple will hit the newly-written row and reuse its findings.

The absence of a version column in the cache key is a deliberate choice. Adding one (Alternative B in §11) would:

- require an `ALTER TABLE` in the migration, breaking the additive-only invariant of `ensure_verify_tables` and adding schema surface area,
- require every read and write in `_resolve_base_keys` to thread the new column through the SELECT and INSERT statements,
- leave orphan pre-v0.3.6 rows on disk (never read, never GC'd),
- and it would not close the trap any better than the schema-marker discipline: a marker-present DB with a poisoned row (impossible under this design, but constructible by an out-of-tree caller) would still poison the diff. The schema-marker approach makes the poisoned-row set empty by construction.

Determinism: `maybe_migrate_verify_state` is deterministic in its inputs (the on-disk state) and its effects. Two identical starting states — same rows in `static_baseline`, same rows in `schema_meta`, same Trikon version — produce byte-identical post-migration states and byte-identical log-record streams.

## 8. Public-Boundary Discipline

The following symbols are the sole public API surface added by this bugfix:

- **`trikon.verify.state_migrations.CURRENT_VERIFY_SCHEMA_VERSION: int`** — the Phase-2 version marker. Value `1` in v0.3.6.
- **`trikon.verify.state_migrations.maybe_migrate_verify_state(conn: sqlite3.Connection) -> None`** — the migration entry point. Called from `_open_state_db` only. No new consumer is expected outside the Verification-Runner subsystem.

Public API invariants preserved by this bugfix (Requirement 6):

- **`sdk.verify(...)` signature** — unchanged (`repo`, `base_sha`, `head_sha`, `diff`, `policy_path`, `cache_db`, `no_sandbox`; returns `Verdict`).
- **`Verdict` field set** — unchanged (`decision`, `reason`, `matched_rule`, `warnings`, `evidence`, `audit_id`, `schema_version`).
- **CLI flag surface** — `trikon verify`, `trikon debug verify`, `trikon debug impact` all keep their v0.3.5 flag lists.
- **`Static_Baseline_Table` columns** — unchanged (`id`, `base_sha`, `tool`, `tool_version`, `findings_json`, `computed_at`).
- **`ensure_verify_tables`** — remains additive-only (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`).
- **`LocalSubprocessSandbox` / `LocalDockerSandbox` / `Sandbox` union / `SandboxExecResult`** — all unchanged.
- **No new exception class** — every migration raise uses `VerificationRunnerError` from `trikon.verify.errors`.

Type discipline: `maybe_migrate_verify_state` has the concrete signature `(conn: sqlite3.Connection) -> None`. No `dict[str, Any]` appears anywhere on the public surface. Internal typing uses concrete union types (`str | int` where a `schema_meta` value might arrive as either, narrowed at the parse site).

## 9. Error Handling

- **Every `sqlite3.Error` inside `maybe_migrate_verify_state` is wrapped as `VerificationRunnerError`** with `raise ... from exc`. Callers observe a single exception class — the underlying SQLite failure is preserved on `__cause__` for debugging.
- **Corrupt `verify_schema_version` values raise `VerificationRunnerError`**, not `ValueError` — the `ValueError` (or `TypeError`) that surfaced from `int(...)` is preserved on `__cause__`.
- **Future-version state.db raises `VerificationRunnerError`** immediately, before any DELETE runs. This matches Phase-1's `DepGraphError` refuse-to-open discipline for a Phase-1 schema_version greater than the running build's.
- **The best-effort ROLLBACK inside the transaction-failure branch** deliberately swallows its own `sqlite3.Error` (via `try / except sqlite3.Error: pass`). The primary failure has already been captured — a subsequent rollback failure is not itself worth raising, and letting it propagate would mask the primary cause.
- **`_open_state_db`'s exception-handling block** is extended verbatim from the `ensure_verify_tables` shape: on `VerificationRunnerError` from the migration, `conn.close()` runs before the re-raise. This is Requirement 5.6.

## 10. Testing Strategy

**Unit tests** (in a new file `tests/unit/verify/test_state_migrations.py`, following the style of `tests/unit/verify/` neighbors):

- **Property 1 — atomic drop-and-stamp on pre-v0.3.6 state.db**: hypothesis-generate lists of `(base_sha, tool, tool_version, findings_json)` tuples (base_sha as 40-hex-char text, tool ∈ `{"ruff", "mypy"}`, tool_version as arbitrary text, findings_json as either `'[]'` or `json.dumps` of a small dict list); seed each generated list into an in-memory DB post-`ensure_verify_tables` with NO marker; invoke `maybe_migrate_verify_state`; assert `SELECT COUNT(*) FROM static_baseline == 0` and `SELECT value FROM schema_meta WHERE key='verify_schema_version'` returns `'1'`. `@settings(max_examples=100, deadline=None)`. Docstring tag: `Feature: static-baseline-cache-poisoning-migration, Property 1: migration drops every pre-v0.3.6 row atomically`.
- **Property 2 — marker-present state.db fast-path**: hypothesis-generate lists of `(base_sha, tool, tool_version, findings_json)` tuples; seed the marker `('verify_schema_version', '1')` into `schema_meta` first; seed the generated rows into `static_baseline`; wrap the connection with a spy that records every executed statement; invoke `maybe_migrate_verify_state`; assert the row set in `static_baseline` is byte-identical to the seeded set; assert the recorded statements contain exactly one SELECT against `schema_meta` (the marker probe) and the idempotent `CREATE TABLE IF NOT EXISTS schema_meta` DDL, and zero DELETE / INSERT (against `schema_meta` or `static_baseline`) / UPDATE / ALTER / DROP / CREATE INDEX statements; assert `caplog.records` filtered on the migration-logger name is empty. `@settings(max_examples=100, deadline=None)`. Docstring tag: `Feature: static-baseline-cache-poisoning-migration, Property 2: marker-present state.db is a fast-path no-op`.
- **Property 3 — notice biconditional**: hypothesis-generate integer row counts N ∈ [0, 50]; seed N rows into `static_baseline` (with any valid triples) on a marker-absent DB; invoke `maybe_migrate_verify_state` with a caplog fixture; assert the count of WARNING records on the migration logger equals 1 if N > 0 and 0 if N == 0; when N > 0, assert the single record's message contains `str(N)`, `trikon.__version__`, and `"static-baseline-cache-poisoning-migration"`. `@settings(max_examples=100, deadline=None)`. Docstring tag: `Feature: static-baseline-cache-poisoning-migration, Property 3: notice fires iff DELETE dropped ≥1 row`.
- **Edge — schema_meta absent, migration idempotently creates it** (Requirement 1.6): open a bare `sqlite3.connect(":memory:")`, apply the four pragmas, call `ensure_verify_tables` (which creates `static_baseline` / `coverage_map` / `tests_seen` but NOT `schema_meta`), invoke `maybe_migrate_verify_state`; assert `SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'` returns a row and `SELECT value FROM schema_meta WHERE key='verify_schema_version'` returns `'1'`.
- **Edge — future-version state.db raises** (Requirement 2.5): seed `verify_schema_version = '2'` into a marker-present DB; assert `maybe_migrate_verify_state` raises `VerificationRunnerError` whose message contains `"2"` and `"1"` (stored and current values).
- **Edge — corrupt marker value raises** (Requirement 5.4): seed `verify_schema_version = 'not-an-int'`; assert `maybe_migrate_verify_state` raises `VerificationRunnerError` whose `__cause__` is a `ValueError`.
- **Edge — sqlite error on DELETE wraps + rows preserved** (Requirement 5.2): wrap the connection with a spy that raises `sqlite3.OperationalError("mocked")` on the `DELETE FROM static_baseline` statement only; seed rows; assert `maybe_migrate_verify_state` raises `VerificationRunnerError` with `__cause__` set to the injected error; assert the seeded rows are still present after the raise (transaction rolled back); assert no `verify_schema_version` row was inserted.
- **Edge — sqlite error on INSERT wraps + DELETE rolled back** (Requirement 5.3): wrap the connection with a spy that raises `sqlite3.OperationalError("mocked")` on the `INSERT INTO schema_meta` statement only; seed rows; assert `maybe_migrate_verify_state` raises `VerificationRunnerError`; assert seeded rows are still present (DELETE reverted by rollback).
- **Wiring — `_open_state_db` calls the migration after `ensure_verify_tables`** (Requirement 1.1): patch `trikon.verify.runner.maybe_migrate_verify_state` with a mock; call `_open_state_db(tmp_path / "state.db")`; assert the mock was called exactly once with a `sqlite3.Connection` argument; assert the mock's call comes after `ensure_verify_tables` was called on the same connection (via a call-order recorder).
- **Wiring — `_open_state_db` closes the connection when the migration raises** (Requirement 5.6): patch `maybe_migrate_verify_state` to raise `VerificationRunnerError`; call `_open_state_db(tmp_path / "state.db")`; assert `VerificationRunnerError` propagates; assert the connection returned by `sqlite3.connect` inside `_open_state_db` was closed (via a `sqlite3.connect` wrapper that records `close()` calls on the returned connection object).

**Integration checkpoint** — spelled out in Wave 2 of `tasks.md`. Reproduces the v0.3.5 checkpoint's poisoning-then-clean scenario end-to-end.

## 11. Alternatives Considered

- **Alternative A: `PRAGMA user_version` instead of `schema_meta.verify_schema_version`.** SQLite's built-in `PRAGMA user_version` is a per-file uint32 stored in the DB header; reads and writes are free. Rejected because Phase 1 already claims `schema_meta.schema_version` on the same file for its own versioning discipline, and `PRAGMA user_version` is a single per-file slot that cannot host two independent version markers. Extending Phase 1 to move to `PRAGMA user_version` would be a broader refactor than this bugfix should carry. The `schema_meta.verify_schema_version` approach is namespace-clean and mirrors the existing Phase-1 pattern.
- **Alternative B: Add a `trikon_version` column to `static_baseline` and widen the UNIQUE constraint.** `ALTER TABLE static_baseline ADD COLUMN trikon_version TEXT NOT NULL DEFAULT ''`; widen `UNIQUE(base_sha, tool, tool_version)` → `UNIQUE(base_sha, tool, tool_version, trikon_version)`; rewrite the SELECT and INSERT in `_resolve_base_keys` to thread the new column. Rejected because (a) it requires an `ALTER TABLE` in the migration, breaking `ensure_verify_tables`'s additive-only invariant; (b) it leaves orphan pre-v0.3.6 rows on disk (never read, never GC'd — the DB grows monotonically); (c) it widens the read/write surface of `_resolve_base_keys` for a bugfix; (d) it does not close the trap better than the marker approach — a marker-present DB with a poisoned row is impossible under this design, but a `trikon_version`-column DB with a poisoned row (findings_json = '[]' plus trikon_version = "0.3.6") would still poison the diff if a v0.3.6 code path ever wrote such a row. The marker approach is stronger because it makes the poisoned-row set empty by construction rather than tagged.
- **Alternative C: Empty-`findings_json` heuristic drop.** On cache hit, if the deserialized findings list is empty, refuse the cache hit and force a re-scan. Rejected because a genuinely-clean baseline (a small module with zero ruff/mypy findings) also produces `findings_json = '[]'`. The heuristic would false-positive on every clean-baseline case, forcing an unnecessary re-scan on every run. Behavior is harder to reason about than a marker-driven invariant.
- **Alternative D: `trikon cache clear` CLI subcommand.** Add a new CLI command that the user runs manually after upgrading. Rejected against Requirement 1.5 — the migration MUST be automatic on the first post-upgrade run. A manual CLI command shifts the burden onto the user, contradicts the "no manual state.db deletion required" contract, and does not close the trap for CI-only consumers who never run interactive commands.
- **Alternative E: Version-key inclusion via `tool_version` string mangling.** Pre-pend `trikon.__version__` to the `tool_version` string on the write side, so `(base_sha, tool, "0.3.6/1.11.2")` becomes the effective cache key. Rejected because it conflates two independent version identities (Trikon build vs tool build) in a single opaque string, complicates the `tool_version` cache-invalidation contract described in `static_checks.py` (which currently invalidates rows on tool-version bumps via the `pyproject.toml` pin), and does not remove the poisoned rows from disk.
- **Alternative F: Bump Phase-1's `schema_meta.schema_version` 1 → 2 and ship a Phase-1 migration.** Reuse the existing `DepGraph._run_migrations` scaffold. Rejected because the discipline note in `trikon/verify/db.py` explicitly says "Phase-2 code will not migrate Phase-1 schema", and `static_baseline` is a Phase-2 table — bumping Phase 1's version to migrate a Phase-2 row set couples the two cadences in a way this bugfix should not introduce. The independent-version-marker approach (this design) keeps them decoupled.
- **Alternative G: Hook the migration in every state.db open site (belt-and-suspenders).** Add the `maybe_migrate_verify_state(conn)` call to `DepGraph._get_conn`, `_open_state_db`, and `sdk.verify`'s audit-log open. Rejected during Clarify as Option C — the redundant calls are idempotent (fast-path no-op after the first fires) but add three surface areas to keep in sync. The single-site `_open_state_db` hook (Clarify Answer 2, Option B) is sufficient because every `trikon verify` invocation funnels through `run_verification`, which funnels through `_open_state_db`. A future new state.db opener that skips this hook would surface as a bug (missing migration on that path), not as silent poisoning — the missing-marker branch will fire the migration correctly when that path is fixed.

## 12. Release-Side Note (out of scope for this spec)

The v0.3.6 release commit that lands this bugfix will also:

- Bump `pyproject.toml` `version = "0.3.5"` → `version = "0.3.6"`.
- Update four sandbox image tag references from `:0.3.5` → `:0.3.6`:
  - `trikon/verify/sandbox.py` (image tag constant used by `LocalDockerSandbox`).
  - `trikon/verify/runner.py` (image tag default argument on `run_verification`).
  - `trikon/verify/_plugin_shim.py` (image tag used by the plugin sandbox variant).
  - `Dockerfile.sandbox` (the tag baked into the image build).
- Add a `[0.3.6]` entry to `CHANGELOG.md` under `### Fixed` naming this bugfix and the spec directory.

These edits are release plumbing and do NOT affect the migration logic. They are called out here so the release engineer knows to include them in the v0.3.6 commit; they are NOT part of this spec's implementation tasks (per the "NO git operations against the Trikon parent repo" constraint on this spec's task list).
