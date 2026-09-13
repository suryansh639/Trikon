# Policy Engine — Design

> Phase 3 of the Trikon 12-week build. Turns an `ImpactSet` (Phase 1) and a
> `VerificationReport` (Phase 2) into the terminal `Verdict` the CLI, MCP
> tool, GitHub App, and GitHub Action all consume. Adds the append-only
> `audit_log` table and the GitHub-flavored Markdown formatter that ends up
> in the PR comment.
>
> Status: design. Phase-0/1/2 stubs at `trikon/policy/**` and
> `trikon/evidence/formatters/markdown.py` are extended, not replaced.
> Requirements are frozen against
> `.kiro/specs/policy-engine/requirements.md` and `EXECUTION_PLAN.md
> §Phase 3`.

---

## 1. Overview

### 1.1 What it does

Given a repo path and the two evidence models already produced upstream — `ImpactSet` from `trikon.change_intel.compute_impact` and `VerificationReport` from `trikon.verify.run_verification` — the Policy Engine emits the terminal `trikon.evidence.report.Verdict`:

```
ImpactSet + VerificationReport
        │
        ▼
load_policy(repo_path, policy_path)
        │  (missing → default_policy())
        ▼
       Policy
        │
        ▼
evaluate_policy(policy, change, verification)
        │
        │  first-match-terminal decision + warn accumulation
        ▼
      Verdict  (schema_version=2, warnings: list[str])
        │
        ├──▶ audit_log.record_verdict(conn, verdict)  ── append-only INSERT
        │
        └──▶ format_markdown(verdict)  ── GitHub-PR-comment string
                    │
                    ▼
              trikon verify  (exit 0/1/2 by decision)
```

Every stage has a single, well-typed entry point. Every stage can fail; every failure raises a subclass of `PolicyEvaluationError`. `PolicyEvaluationError` itself is a subclass of `TrikonError`, so the SDK boundary that already catches `ChangeIntelError` (Phase 1) and `VerificationRunnerError` (Phase 2) collapses to a single `except TrikonError` clause in Phase 3.

**Validates: Requirements 3.1, 5.1, 7.1, 7.2.**

### 1.2 Module boundary

Everything inside `trikon/policy/**` and `trikon/audit_log/**` raises only `PolicyEvaluationError` subclasses. Every foreign exception — `yaml.YAMLError`, `pydantic.ValidationError`, `sqlite3.Error`, `KeyError` from an unknown condition key, `OSError` from `importlib.resources`, `FileExistsError` from `trikon init` overwrite — is caught at the module boundary and re-raised as the appropriate subclass with the original attached to `__cause__`. That closure is what lets `sdk.verify` catch a single base class (`TrikonError`) and produce a well-formed `require_human` verdict without ambiguity.

**Validates: Requirement 7.1.**

### 1.3 Goals (v0.1)

| # | Goal | Measured by |
| - | ---- | ----------- |
| G1 | Deterministic `Verdict` for any (policy, impact, verification) | Same inputs → byte-identical `verdict.model_dump_json()` on 10 successive calls (excluding `audit_id` UUID + `created_at`) |
| G2 | 100 ms `evaluate_policy` on 6-rule default | `tests/benchmarks/test_policy_perf.py` — Requirement 8.1 |
| G3 | mypy `--strict` clean on `trikon/policy/**` and `trikon/audit_log/**` | `mypy --strict trikon/policy trikon/audit_log` exits 0 |
| G4 | ≥ 85 % branch coverage on both new subpackages | `coverage report --fail-under=85 --include='trikon/policy/*,trikon/audit_log/*'` |
| G5 | Never fail-open | Every uncaught error path in `sdk.verify` becomes `require_human` (§9, §13) |
| G6 | Zero `dict[str, Any]` on the public surface | `disallow_any_explicit=true` in `[tool.mypy]` — unchanged from Phase 1/2 |
| G7 | Audit log append-only by construction | AST scan of `trikon/audit_log/**` finds only `INSERT` verbs after the initial DDL (§9, Property 7) |

### 1.4 Non-goals (v0.1)

- **Hash-chained audit log.** `EXECUTION_PLAN.md §Phase 3` explicitly defers this to v0.2. The Phase-3 table has an `audit_id` PRIMARY KEY and an `INSERT`-only writer; tamper-evident chaining is future work.
- **Actor / time-of-day conditions.** The Phase-0 DSL enumerates `actor.agent_id` and `time_of_day` as example condition keys, but Requirement 1 fixes the v0.1 dispatch to exactly the five listed there. Unknown keys raise `RuleMatchError` (§5.3).
- **Policy schema migrations.** `Policy.version == 1` is the only value accepted; a `version` mismatch surfaces as `PolicyLoadError` via Pydantic validation.
- **Remote policy sources.** `load_policy` reads the local filesystem only. `s3://…` / `https://…` URLs are Phase 4 material.
- **Sub-rule tracing beyond `RuleResult`.** The `Evidence.policy_results` list already carries one entry per rule; per-condition trace ("this rule matched because `any_path_matches` fired against `src/foo.py`") is Phase 4 material.
- **JSON-schema export of `Policy`.** `pydantic.BaseModel.model_json_schema()` works out of the box, but publishing the schema to a versioned URL is not part of Phase 3.

---

## 2. Architecture

### 2.1 Component diagram

```
trikon/policy/
├── dsl.py                 Policy, Rule, Decision           — frozen (Phase 0)
├── evaluator.py           evaluate_policy, _rule_matches   — fill Phase 0 stubs
├── loader.py              load_policy, default_policy      — fill Phase 0 stubs
├── errors.py              PolicyEvaluationError hierarchy  — NEW
└── default_policy.yaml    packaged default policy          — NEW

trikon/audit_log/
├── __init__.py            record_verdict, ensure_audit_tables — NEW subpackage
├── db.py                  ensure_audit_tables + DDL           — NEW
└── writer.py              record_verdict INSERT               — NEW

trikon/evidence/
├── report.py              +Verdict.warnings, +schema_version=2, +Decision "warn"
└── formatters/markdown.py format_markdown (extend one-liner stub)

trikon/
├── sdk.py                 verify() ← extend to run evaluator + audit writer
└── cli.py                 trikon verify + trikon init      — extend Phase 2 stubs

External:
  trikon/exceptions.py     TrikonError                       (unchanged)
  trikon/change_intel/…    compute_impact → ImpactSet
  trikon/verify/…          run_verification → VerificationReport
  .trikon/state.db         Phase-1/2 tables + audit_log (new sibling table)
```

Two new subpackages, one new module (`errors.py` under `trikon/policy/`), one packaged data file (`default_policy.yaml`), and additive edits to three existing files (`report.py`, `sdk.py`, `cli.py`).

### 2.2 Data flow

```
                     ┌────────────────────────────────────────┐
   repo_path,        │  sdk.verify(repo, base, head)          │
   base_sha, head_sha│                                        │
        ─────────▶  │  ┌──────────────────────────────────┐  │
                     │  │ parse_diff → compute_impact       │  │  Phase 1
                     │  └──────────────────────────────────┘  │
                     │                    │                     │
                     │                    ▼                     │
                     │  ┌──────────────────────────────────┐  │
                     │  │ run_verification                    │  │  Phase 2
                     │  └──────────────────────────────────┘  │
                     │                    │                     │
                     │                    ▼                     │
                     │  ┌──────────────────────────────────┐  │
                     │  │ load_policy(repo, policy_path)      │  │  Phase 3
                     │  └──────────────────────────────────┘  │
                     │                    │                     │
                     │                    ▼                     │
                     │  ┌──────────────────────────────────┐  │
                     │  │ evaluate_policy(policy,             │  │
                     │  │                 impact,             │  │
                     │  │                 verification)       │  │
                     │  └──────────────────────────────────┘  │
                     │                    │                     │
                     │                    ▼                     │
                     │        ┌──────────────────────┐          │
                     │        │        Verdict       │          │
                     │        └──────────────────────┘          │
                     │                    │                     │
                     │           (outside try/except)           │
                     │                    ▼                     │
                     │  ┌──────────────────────────────────┐  │
                     │  │ audit_log.record_verdict(conn, v) │  │  append-only
                     │  └──────────────────────────────────┘  │
                     └────────────────────────────────────────┘
                                          │
                                          ▼
                              format_markdown(verdict)
                                          │
                                          ▼
                                 CLI stdout, exit 0/1/2
```

The stateful boundary is one SQLite file at `<repo>/.trikon/state.db` — the same file Phase 1 and Phase 2 use. Phase 3 adds exactly one sibling table (`audit_log`); the Phase-1 tables (`schema_meta`, `file_index`, `symbols`, `edges`) and the Phase-2 tables (`coverage_map`, `tests_seen`, `static_baseline`) are untouched. See §4 for the full DDL.

`ensure_audit_tables(conn)` is wired into `DepGraph._get_conn` alongside the existing `ensure_verify_tables(conn)` call so the state DB is Phase-3-ready from the first `sqlite3.connect`. That is the same discipline Phase 2 established.

**Validates: Requirements 4.1, 4.4, 5.1.**

### 2.3 Sequence of operations and wall-clock budget

Requirement 8 caps three per-stage times. The per-stage budget below composes to those numbers with headroom:

| # | Stage | Target | Expected (Ryzen-7 laptop) | Slack |
| - | ----- | ------ | ------------------------- | ----- |
| 1 | `load_policy` — resolve, `yaml.safe_load`, `Policy.model_validate` on ≤ 10 KB YAML | ≤ 50 ms (Requirement 8.2) | ~5 ms (PyYAML C loader, Pydantic v2 core) | ~45 ms |
| 2 | `evaluate_policy` — 6 rules × 5 conditions on `sample_repo` evidence | ≤ 100 ms (Requirement 8.1) | ~2 ms (pure Python, no I/O, no SQLite round-trip) | ~98 ms |
| 3 | `format_markdown` — fully-populated Verdict, 5 files + 5 failures + 3 warnings | ≤ 10 ms (Requirement 8.3) | ~1 ms (f-string join, no template engine, no I/O) | ~9 ms |
| 4 | `record_verdict` — one `INSERT` + `commit` on WAL-mode SQLite | *not gated by Requirement 8* | ~2 ms | — |
| **Total added to `sdk.verify`** | | | **≤ 10 ms** on cached policy path | — |

**Validates: Requirements 8.1, 8.2, 8.3.**

### 2.4 Concurrency model

Single-threaded. Everything Phase 3 adds is CPU-cheap and I/O-cheap: the policy YAML is at most a few KB, rule dispatch is a linear scan over ≤ 20 rules in practice, and the audit-log INSERT is a single SQLite round-trip. Parallelism has no cost floor to attack. The evaluator is a pure function of `(policy, impact, verification)`; the writer opens no long-lived resource. `sdk.verify` continues to run on the calling thread.

---

## 3. Public API surface

Every public function's signature is fixed here. Deviations require a design-doc update. Every signature is fully type-hinted; no `dict[str, Any]` appears on any public parameter or return type. Type aliases stay narrow — `Decision` is a four-value `Literal`, not `str`.

### 3.1 `trikon/policy/loader.py`

```python
from __future__ import annotations

from pathlib import Path

from trikon.policy.dsl import Policy


def load_policy(repo_path: Path, policy_path: Path) -> Policy:
    """Load, parse, and validate a policy YAML from disk.

    Path resolution:
        If ``policy_path`` is absolute, use it as-is. Otherwise resolve it
        against ``repo_path`` (``repo_path / policy_path``). The resolved
        path is the sole filesystem read this function performs.

    Semantics:
        1. If the resolved file does not exist, return ``default_policy()``
           (Requirement 2.2). No exception on the missing-file path.
        2. Otherwise ``yaml.safe_load`` the file contents and pass the
           parsed mapping to ``Policy.model_validate``.
        3. Return the validated ``Policy`` object.

    Args:
        repo_path: Absolute path to the git repository. Used only for the
            relative-path resolution rule in step 1.
        policy_path: Absolute or repo-relative path to the policy YAML.

    Returns:
        A validated :class:`Policy`. On the missing-file path, the same
        object :func:`default_policy` returns.

    Raises:
        PolicyLoadError: The file exists but is malformed. Two sub-causes,
            both chained via ``__cause__``:

            * ``yaml.YAMLError`` — the file is not valid YAML.
            * ``pydantic.ValidationError`` — the parsed mapping does not
              conform to :class:`Policy` (unknown field, wrong type,
              missing required ``rules``, ``version != 1``).
    """


def default_policy() -> Policy:
    """Return the built-in default policy shipped inside the wheel.

    Loads ``trikon/policy/default_policy.yaml`` via
    ``importlib.resources.files("trikon.policy") / "default_policy.yaml"``,
    ``yaml.safe_load``s the contents, and returns the result of
    ``Policy.model_validate``.

    Raises:
        PolicyLoadError: The packaged file is missing or malformed
            (indicates a broken wheel; should be unreachable in a
            correctly-built installation). Test coverage in §13 pins the
            file's presence via an integration test.
    """
```

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5.**

### 3.2 `trikon/policy/evaluator.py`

```python
from __future__ import annotations

from trikon.evidence.report import ImpactSet, VerificationReport, Verdict
from trikon.policy.dsl import Policy, Rule


def evaluate_policy(
    policy: Policy,
    change: ImpactSet,
    verification: VerificationReport,
) -> Verdict:
    """Apply ``policy`` to the evidence and emit the terminal Verdict.

    Evaluation model (Requirements 3.1-3.4):
        1. Iterate ``policy.rules`` in declaration order.
        2. For each rule, evaluate ``_rule_matches(rule, change, verification)``.
        3. Record a ``RuleResult`` for every rule (matched or not) in
           declaration order — Requirement 3.4.
        4. The first rule whose ``then`` is a terminal decision
           (``allow`` | ``block`` | ``require_human``) AND that matched
           determines ``Verdict.decision``, ``Verdict.matched_rule``, and
           ``Verdict.reason`` — Requirement 3.1.
        5. Every rule whose ``then == "warn"`` AND that matched appends
           its reason (or a synthesized default) to ``Verdict.warnings``
           in declaration order — Requirement 3.3.
        6. If no terminal rule matches, ``decision = "require_human"``,
           ``matched_rule = None``, ``reason`` = fall-through default —
           Requirement 3.2.

    ``schema_version`` on the returned Verdict is 2 by default (§3.6 in
    the evidence-model changes).

    Raises:
        RuleMatchError: A rule's ``when`` clause contains an unknown
            condition key or a malformed operator dict for
            ``verification.static.new_errors``. Chained via ``__cause__``
            if it wraps a ``KeyError`` / ``TypeError``. This is a policy
            authoring error the caller should surface, not swallow.
        PolicyEvaluationError: Any other internal failure of the
            evaluator. Reserved for defensive raises; should be
            unreachable on valid inputs.
    """


def _rule_matches(
    rule: Rule,
    change: ImpactSet,
    verification: VerificationReport,
) -> bool:
    """Return True iff every condition in ``rule.when`` holds against the evidence.

    Dispatch table (Requirement 1.1-1.5):
        any_path_matches                     -> _match_any_path
        no_path_matches                      -> _match_no_path
        change.blast_radius.score            -> _match_blast_radius
        verification.tests.status            -> _match_tests_status
        verification.static.new_errors       -> _match_new_errors

    Semantics (Requirement 1.6-1.7):
        - Empty ``when`` (``{}``) — unconditional match, return True.
        - Multiple keys — AND across all conditions (every one must match).
        - Unknown key — raise ``RuleMatchError``.
    """
```

**Validates: Requirements 1.1-1.7, 3.1-3.4.**

### 3.3 `trikon/audit_log/db.py`

```python
from __future__ import annotations

import sqlite3


def ensure_audit_tables(conn: sqlite3.Connection) -> None:
    """Create the ``audit_log`` table and its indexes if absent.

    Executes only ``CREATE TABLE IF NOT EXISTS`` and
    ``CREATE INDEX IF NOT EXISTS`` — never ``ALTER``, ``DROP``, or
    ``DELETE`` (Requirement 4.3, Requirement 4.4). Idempotent by
    construction; safe to call on every connection open.

    Args:
        conn: An open :class:`sqlite3.Connection` to ``.trikon/state.db``.
            The caller is expected to have applied the Phase-1 pragmas.

    Raises:
        AuditLogError: Wraps any :class:`sqlite3.Error` raised while
            executing the DDL, with the original exception on ``__cause__``.
    """
```

**Validates: Requirements 4.3, 4.4.**

### 3.4 `trikon/audit_log/writer.py`

```python
from __future__ import annotations

import sqlite3

from trikon.evidence.report import Verdict


def record_verdict(conn: sqlite3.Connection, verdict: Verdict) -> None:
    """Append one row to ``audit_log`` for the given ``verdict``.

    Executes exactly one ``INSERT INTO audit_log (audit_id, created_at,
    decision, matched_rule, reason, verdict_json) VALUES (?,?,?,?,?,?)``
    followed by ``conn.commit()`` (Requirements 4.1, 4.2). The
    ``verdict_json`` column stores ``verdict.model_dump_json()`` —
    the full Verdict serialization at ``schema_version == 2``.

    Called from ``sdk.verify`` on both the happy-path return AND the
    fail-closed return, so no verdict is ever lost from the audit trail
    (Requirement 7.3).

    Raises:
        AuditLogError: Wraps any :class:`sqlite3.Error` from the INSERT
            or the commit. An audit failure is a hard failure — the SDK
            boundary re-raises rather than silently return a
            verdict-without-trail (Requirement 4.5).
    """
```

**Validates: Requirements 4.1, 4.2, 4.5, 7.3.**

### 3.5 `trikon/audit_log/__init__.py`

```python
"""Trikon audit log — append-only sink for every Verdict.

The subpackage is deliberately narrow: two re-exports and nothing else.
No ``update_*``, no ``delete_*``, no ``truncate_*``, no ``purge_*``. Any
future function that mutates existing rows is a v0.2+ decision that must
survive design review (Requirement 4.3).
"""
from __future__ import annotations

from trikon.audit_log.db import ensure_audit_tables
from trikon.audit_log.writer import record_verdict

__all__ = ["ensure_audit_tables", "record_verdict"]
```

**Validates: Requirement 4.3.**

### 3.6 `trikon/evidence/report.py` — three additive changes

Only three edits, all additive:

```python
# BEFORE (Phase 2)
Decision = Literal["allow", "block", "require_human"]

class RuleResult(BaseModel):
    rule_name: str
    matched: bool
    would_emit: Decision | None
    reason: str | None

class Verdict(BaseModel):
    decision: Decision
    reason: str
    matched_rule: str | None
    evidence: Evidence
    audit_id: UUID
    created_at: datetime
    schema_version: int = 1
```

```python
# AFTER (Phase 3)
# Decision is widened to four values. `PolicyDecision` is defined as an
# alias for the same Literal so downstream code that wants to signal
# "this variable holds a rule outcome (may be warn)" as distinct from
# "this variable holds a terminal decision (never warn)" can annotate
# accordingly. Both aliases resolve to the same runtime type; the split
# is documentation, not enforcement.
Decision = Literal["allow", "block", "require_human", "warn"]
PolicyDecision = Decision  # documented alias; identical Literal.

class RuleResult(BaseModel):
    rule_name: str
    matched: bool
    # `would_emit` now covers all four decisions — a warn rule that fires
    # still reports `would_emit="warn"` in its trace entry (Requirement 3.4).
    would_emit: PolicyDecision | None
    reason: str | None

class Verdict(BaseModel):
    decision: Decision            # ← still four-valued, but the SDK
                                  #    boundary invariant is that emitted
                                  #    Verdicts carry only terminal values
                                  #    (allow|block|require_human), never
                                  #    "warn" — warn is a rule outcome, not
                                  #    a verdict outcome.
    reason: str
    matched_rule: str | None
    evidence: Evidence
    audit_id: UUID
    created_at: datetime
    # New field. Rule-declaration order preserved; empty list is the
    # "no warn matched" case. Never None; a warn-empty Verdict has
    # `warnings == []` (Requirement 3.3).
    warnings: list[str] = Field(default_factory=list)
    # Bumped 1 → 2 because the shape of the JSON grew. Downstream
    # consumers pinned to schema_version=1 will see the version bump
    # before they see an unfamiliar `warnings` key (Requirement 4.2).
    schema_version: int = 2
```

Why widen `Decision` in the shared module instead of introducing a `PolicyDecision` type only in `trikon.policy.dsl`:

- Phase-0 `Decision` in `trikon/policy/dsl.py` is already four-valued (`allow | block | require_human | warn`) — that is what a rule's `then` field accepts. If `trikon/evidence/report.Decision` stays three-valued, `RuleResult.would_emit` would need a separate four-value type anyway. Unifying the alias on the four-value form and adding a `PolicyDecision` synonym keeps one runtime type live and the two names in sync.
- The four-value widening is source-compatible: every existing consumer that asserts `decision in ("allow", "block", "require_human")` continues to work, because `sdk.verify` never returns a Verdict with `decision == "warn"` on any path (§7). The invariant is documented, tested (Property 11), and enforced by `evaluate_policy`.
- `schema_version` bumps in the same edit so a single JSON payload signal covers both the widened `Decision` type and the new `warnings` list.

**Validates: Requirements 3.3, 3.4, 4.2, Glossary (Decision widened), Glossary (Verdict.warnings added), Glossary (schema_version 1→2).**

### 3.7 `trikon/evidence/formatters/markdown.py`

```python
from __future__ import annotations

from trikon.evidence.report import Verdict


def format_markdown(verdict: Verdict) -> str:
    """Render ``verdict`` as a GitHub-flavored Markdown summary.

    Output shape (§12 for the exact layout):
        1. Header line — decision icon + matched_rule + reason.
        2. "Focus your review on" — up to 5 file paths from
           ``verdict.evidence.change.changed_files`` in list order.
        3. "Impact" — file / module / API counts + blast-radius bucket
           and numeric score.
        4. "Verification" — pass / fail / skip counts + up to 5 failing
           test node IDs each rendered with their ``failure_summary``.
        5. "Warnings" — only when ``verdict.warnings`` is non-empty; every
           warning string verbatim in list order.
        6. Footer — ``audit_id`` and ``schema_version``.

    Pure function: no I/O, no template engine, no runtime dependency
    outside the standard library and the Pydantic models it consumes
    (Requirement 6.6).

    Args:
        verdict: The Verdict to render. Any decision is accepted,
            including ``"warn"`` (never actually emitted by
            :func:`sdk.verify`; supported for forward-compat).

    Returns:
        A single ``str`` — the full Markdown document.
    """
```

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6.**

### 3.8 Auxiliary types (recap)

| Type | Module | Notes |
| ---- | ------ | ----- |
| `PolicyEvaluationError` and subclasses | `trikon/policy/errors.py` | See §9 |
| `Decision`, `PolicyDecision` | `trikon/evidence/report.py` | Four-value `Literal`; alias identical |
| `Verdict.warnings: list[str]` | `trikon/evidence/report.py` | New field, `default_factory=list` |
| `Verdict.schema_version: int = 2` | `trikon/evidence/report.py` | Bumped default |

No new dataclasses or Pydantic models. Everything else re-uses shapes Phase 1/2 already defined.

---

## 4. Data model & storage

### 4.1 SQLite DDL — one new sibling table

Added idempotently at the first `sdk.verify` call via `ensure_audit_tables`, which is wired into `DepGraph._get_conn` alongside `ensure_verify_tables` (§4.3). The Phase-1 pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY`) remain the connection defaults. No Alembic — additive-only schema evolution, same rule Phase 1 and Phase 2 established.

```sql
-- =========================================================================
-- audit_log. Append-only record of every Verdict sdk.verify has emitted,
-- successful or fail-closed. One row per Verdict.
-- =========================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id      TEXT NOT NULL PRIMARY KEY,       -- UUID4 in canonical form
    created_at    TEXT NOT NULL,                    -- ISO-8601 UTC
    decision      TEXT NOT NULL
        CHECK(decision IN ('allow', 'block', 'require_human', 'warn')),
    matched_rule  TEXT,                             -- NULL on fall-through
    reason        TEXT NOT NULL,
    verdict_json  TEXT NOT NULL                     -- verdict.model_dump_json()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON audit_log(created_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_decision
    ON audit_log(decision);
```

Column-by-column rationale:

- `audit_id TEXT PRIMARY KEY` — the same UUID emitted on `Verdict.audit_id`. Using the Verdict UUID as the primary key means every verdict has a unique row and no duplicate writes can occur; a second `INSERT` with the same `audit_id` fails with `sqlite3.IntegrityError` and surfaces as `AuditLogError`. Cheap tamper detector.
- `created_at TEXT NOT NULL` — ISO-8601 UTC, populated from `verdict.created_at`. Indexed for time-range scans (compliance audits typically ask "verdicts between date X and date Y").
- `decision TEXT NOT NULL CHECK(decision IN (...))` — the four-value `Decision` literal. The `CHECK` constraint enforces the widened alphabet at the storage layer (Requirement 4.1). `warn` is included in the alphabet even though `sdk.verify` never emits it; the storage layer is agnostic to the SDK-boundary invariant.
- `matched_rule TEXT NULL` — `NULL` on fall-through and on the fail-closed path (Requirement 3.2, Requirement 7.2). Not indexed; queried rarely.
- `reason TEXT NOT NULL` — always populated. On the fail-closed path this carries the failing exception class name + message (§10) so audit reviewers can see the cause of `require_human` verdicts.
- `verdict_json TEXT NOT NULL` — full `verdict.model_dump_json()` output at `schema_version == 2` (Requirement 4.2). This is the lossless record; every other column is a projection maintained for query performance.

The two indexes cover the two query patterns the compliance surface actually needs: "recent verdicts" and "verdicts of type `block`".

**Validates: Requirements 4.1, 4.2, 4.3, 4.4.**

### 4.2 Additive-only enforcement mechanism

Two layers, mirroring `trikon/verify/db.py`:

1. **Module-level constant list.** `trikon/audit_log/db.py` defines `_DDL_STATEMENTS: tuple[str, ...]` with exactly three entries — the `CREATE TABLE IF NOT EXISTS`, the two `CREATE INDEX IF NOT EXISTS`. `ensure_audit_tables` iterates that tuple and calls `conn.execute` on each. No other DDL surface exists in the module.
2. **Public API closure.** `trikon/audit_log/__init__.py` re-exports exactly two names — `ensure_audit_tables` and `record_verdict`. No `update_*`, `delete_*`, `truncate_*`, or `purge_*` function is defined anywhere in `trikon/audit_log/**` (Requirement 4.3). Property 7 (§17) is a static AST scan enforcing this.

`record_verdict` executes exactly one SQL statement — the `INSERT` — followed by `conn.commit()`. That statement is a module-level constant string; the writer never composes SQL dynamically.

**Validates: Requirement 4.3.**

### 4.3 Wiring `ensure_audit_tables` into the state-DB open path

`trikon/change_intel/dep_graph.py::DepGraph._get_conn` already runs `ensure_verify_tables(conn)` on the first connection open (§4.3 of `verification-runner/design.md`). Phase 3 adds one line:

```python
# In trikon/change_intel/dep_graph.py::_get_conn, after the existing
# ensure_verify_tables(conn) call:
try:
    ensure_verify_tables(conn)
    # Phase-3 (Policy Engine) sibling table. `ensure_audit_tables` is
    # additive-only (design.md §4.2) — CREATE TABLE IF NOT EXISTS +
    # CREATE INDEX IF NOT EXISTS only, no ALTER/DROP on Phase-1 or
    # Phase-2 tables — and idempotent by construction. Running this
    # after Phase 2 means the state DB is Phase-3-ready from the first
    # sqlite3.connect.
    ensure_audit_tables(conn)
except Exception:
    self.close()
    raise
```

Same shape as the Phase-2 wire-up. The exception discipline is delegated: `ensure_audit_tables` wraps `sqlite3.Error` as `AuditLogError`, which is a `TrikonError` subclass, so the SDK boundary catches it uniformly.

**Validates: Requirements 4.4, 7.1.**

### 4.4 Row-count expectations

On any real repository:

| Table | Rows per verdict | Cumulative after 10 000 verdicts |
| ----- | ---------------- | -------------------------------- |
| `audit_log` | +1 | ~10 000 rows |

`verdict_json` is the largest per-row payload; on `examples/sample_repo` it clocks in around 8 KB. 10 000 verdicts is ~80 MB uncompressed. SQLite's page-level compression is not enabled by default, but the WAL journal and the 4 KB page size keep the file readable at that size.

No pruning policy in Phase 3. `EXECUTION_PLAN.md §Phase 4` names retention policy and hash-chained export as v0.2 material.

### 4.5 Migration policy

Additive only. Phase-3 code will not `ALTER` or `DROP` any table Phase 1 or Phase 2 created. If Phase 4 needs to change `audit_log`, it bumps `schema_meta.schema_version` and ships a numbered migration file — the same rule the two prior phases established.

---

## 5. Rule dispatch design

### 5.1 Condition-key catalog

Five keys, one dispatcher per key. Each row below is the intended raise-site and match-logic contract.

| Key | Args (from `rule.when[key]`) | Semantics | Matcher signature | Requirement |
| --- | ---------------------------- | --------- | ----------------- | ----------- |
| `any_path_matches` | `list[str]` of glob patterns | Rule matches iff at least one pattern matches at least one entry in `change.changed_files` under `PurePath.match` semantics (which handles `**` recursion). | `_match_any_path(patterns, change) -> bool` | 1.1 |
| `no_path_matches` | `list[str]` of glob patterns | Dual of `any_path_matches` — rule matches iff **no** pattern matches **any** entry in `change.changed_files`. | `_match_no_path(patterns, change) -> bool` | 1.2 |
| `change.blast_radius.score` | `str` — one of `"LOW" \| "MEDIUM" \| "HIGH"` | Literal equality against `change.blast_radius_score`. | `_match_blast_radius(expected, change) -> bool` | 1.3 |
| `verification.tests.status` | `str` — one of `"passed" \| "failed" \| "skipped"` | Literal equality against `verification.tests.status`. | `_match_tests_status(expected, verification) -> bool` | 1.4 |
| `verification.static.new_errors` | `dict[str, int]` — exactly one of `{"eq": N} \| {"gt": N} \| {"lt": N}` | Count `f` in `verification.static.findings` with `f["is_new"] == True`; compare to `N` under the named operator. | `_match_new_errors(op_dict, verification) -> bool` | 1.5 |

### 5.2 Registry vs inline dispatch

Chosen: **inline `if`/`elif` chain on the condition key**, no registry.

Rationale:

1. **Five conditions is under the "table wins" threshold.** A registry (`_MATCHERS: dict[str, Callable]`) adds a layer of indirection that pays off past ~10 entries. At five, the `if key == "any_path_matches": …` chain is more readable, more type-checkable (each branch has a specialized signature), and takes fewer lines.
2. **Argument types are heterogeneous.** `any_path_matches` takes `list[str]`, `verification.static.new_errors` takes `dict[str, int]`, the blast-radius key takes a bare string. A registry would need to erase the argument type to `object` (or `Any`, which the project explicitly forbids); the inline chain lets each branch declare its concrete argument shape via `isinstance` guards and mypy narrowing.
3. **Adding a new condition is a two-line diff.** A new `elif` plus a `_match_*` helper. The lack of a registry does not slow extension; it removes the "where do I register this?" foot-fault.

The inline chain lives in `_rule_matches`. Roughly:

```python
def _rule_matches(rule: Rule, change: ImpactSet, verification: VerificationReport) -> bool:
    if not rule.when:
        return True  # empty when — unconditional match (Requirement 1.7)

    for key, value in rule.when.items():
        if key == "any_path_matches":
            matched = _match_any_path(_expect_list_str(value, key), change)
        elif key == "no_path_matches":
            matched = _match_no_path(_expect_list_str(value, key), change)
        elif key == "change.blast_radius.score":
            matched = _match_blast_radius(_expect_str(value, key), change)
        elif key == "verification.tests.status":
            matched = _match_tests_status(_expect_str(value, key), verification)
        elif key == "verification.static.new_errors":
            matched = _match_new_errors(_expect_operator_dict(value, key), verification)
        else:
            raise RuleMatchError(f"unknown condition key: {key!r} in rule {rule.name!r}")

        if not matched:
            return False  # AND semantics — one false condition kills the rule (Requirement 1.6)

    return True
```

The `_expect_*` helpers wrap Pydantic-style shape checks and raise `RuleMatchError` on shape mismatch (§5.5). They are what let the matcher functions have concrete signatures without `dict[str, Any]` leaking in.

**Validates: Requirements 1.1-1.7.**

### 5.3 Empty-when and unknown-key handling

- **Empty when** (`rule.when == {}`): `not rule.when` is `True`, the function returns `True` on the first branch (Requirement 1.7). No dispatch happens; no `_expect_*` guard runs. Property 4 pins this behavior universally.
- **Unknown key**: the trailing `else` branch raises `RuleMatchError` naming the offending key and rule name. This is a policy authoring error, not a data error — the fault lies in the policy YAML the operator wrote. It surfaces as a `PolicyEvaluationError` subclass, which propagates to `sdk.verify`, which fail-closes to `require_human` (§10). Property 5 pins this behavior.

### 5.4 Glob library choice

Requirement 1.1 fixes the semantics to `fnmatch.fnmatchcase`. The implementation uses `pathlib.PurePath.match` for the `**` recursive-glob case and falls back to `fnmatch.fnmatchcase` for flat patterns. Rationale:

- `fnmatch.fnmatchcase` does not honor `**` — `fnmatch.fnmatchcase("a/b/c.py", "**/c.py")` returns `False` in stdlib Python. That would break the default policy's `"auth/**"`, `"billing/**"`, `"payments/**"`, `"**/migrations/**"` patterns.
- `PurePath.match` handles `**` and `*` correctly on POSIX-style paths, and it is stdlib. The changed-file paths come out of `ImpactSet.changed_files` as POSIX-style strings (Phase 1 normalizes to `PurePosixPath`), so `PurePath.match` gives the intended semantics without a third-party dep.
- The Glossary's citation of "`fnmatch.fnmatchcase` semantics" is the shorthand policy-authors expect; the implementation upgrades to `PurePath` for the `**` case. Property 1 in §17 pins the observable behavior — the exact function called is a design detail.

```python
from pathlib import PurePosixPath

def _match_any_path(patterns: list[str], change: ImpactSet) -> bool:
    for f in change.changed_files:
        p = PurePosixPath(f)
        for pat in patterns:
            if p.match(pat):
                return True
    return False


def _match_no_path(patterns: list[str], change: ImpactSet) -> bool:
    return not _match_any_path(patterns, change)
```

**Validates: Requirements 1.1, 1.2.**

### 5.5 Operator table for `verification.static.new_errors`

Requirement 1.5 fixes the three-operator alphabet. Implementation:

```python
import operator
from typing import Callable

_NEW_ERRORS_OPERATORS: dict[str, Callable[[int, int], bool]] = {
    "eq": operator.eq,
    "gt": operator.gt,
    "lt": operator.lt,
}

def _match_new_errors(op_dict: dict[str, int], verification: VerificationReport) -> bool:
    if len(op_dict) != 1:
        raise RuleMatchError(
            f"verification.static.new_errors expects exactly one operator, got {sorted(op_dict)!r}"
        )
    (op_name, threshold), = op_dict.items()
    op = _NEW_ERRORS_OPERATORS.get(op_name)
    if op is None:
        raise RuleMatchError(
            f"verification.static.new_errors: unknown operator {op_name!r}; "
            f"expected one of {sorted(_NEW_ERRORS_OPERATORS)}"
        )
    if not isinstance(threshold, int):
        raise RuleMatchError(
            f"verification.static.new_errors[{op_name}]: expected int, got {type(threshold).__name__}"
        )
    count = sum(1 for f in verification.static.findings if bool(f.get("is_new")))
    return op(count, threshold)
```

Table-driven rather than a chain of `if op == "eq": …` branches — three entries meets the "table wins" threshold that rejected the same shape for the top-level dispatcher, because every value here has an identical signature `(int, int) -> bool`. When the argument shapes are uniform, a `dict` is denser than an `if` chain.

**Validates: Requirement 1.5.**

### 5.6 Multiple-condition AND semantics

Requirement 1.6: every condition in a `when` clause must match for the rule to match. The inline dispatcher (§5.2) short-circuits — the first `matched == False` returns `False`, skipping the remaining keys. Rule order among keys is irrelevant because AND is commutative; the short-circuit is a performance optimization, not a semantics choice. Property 1 pins the semantics; the short-circuit is an internal detail.

**Validates: Requirement 1.6.**


---

## 6. Policy loading + default resolution

### 6.1 `load_policy` pseudocode

```
INPUT:  repo_path (Path)
        policy_path (Path)

STEP 1: Resolve the path
  if policy_path.is_absolute():
      resolved ← policy_path
  else:
      resolved ← repo_path / policy_path

STEP 2: Missing-file fallback (Requirement 2.2)
  if not resolved.exists():
      return default_policy()

STEP 3: Read + parse YAML (Requirement 2.1, 2.4)
  try:
      raw ← resolved.read_text(encoding="utf-8")
  except OSError as exc:
      raise PolicyLoadError(f"failed to read {resolved}") from exc

  try:
      parsed ← yaml.safe_load(raw)
  except yaml.YAMLError as exc:
      raise PolicyLoadError(f"{resolved} is not valid YAML") from exc

STEP 4: Pydantic validation (Requirement 2.1, 2.3)
  if parsed is None:
      # Empty file — treat as missing/invalid; explicit rules field required.
      raise PolicyLoadError(f"{resolved} is empty")

  try:
      policy ← Policy.model_validate(parsed)
  except pydantic.ValidationError as exc:
      raise PolicyLoadError(f"{resolved} failed schema validation") from exc

STEP 5: Return
  return policy
```

Every foreign exception is caught and re-raised as `PolicyLoadError` with the original on `__cause__`. `yaml.YAMLError` and `pydantic.ValidationError` are the two explicit conversions Requirement 2.3 and 2.4 name; `OSError` is included for defense in depth (read-permission errors, half-written files) even though the requirements do not name it.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4.**

### 6.2 `default_policy` pseudocode

```
STEP 1: Locate the packaged YAML
  try:
      resource ← importlib.resources.files("trikon.policy") / "default_policy.yaml"
  except (ModuleNotFoundError, FileNotFoundError) as exc:
      raise PolicyLoadError("packaged default_policy.yaml not found") from exc

STEP 2: Read the resource
  try:
      raw ← resource.read_text(encoding="utf-8")
  except (OSError, FileNotFoundError) as exc:
      raise PolicyLoadError("failed to read packaged default_policy.yaml") from exc

STEP 3: Parse + validate (same discipline as load_policy)
  try:
      parsed ← yaml.safe_load(raw)
      return Policy.model_validate(parsed)
  except (yaml.YAMLError, pydantic.ValidationError) as exc:
      raise PolicyLoadError("packaged default_policy.yaml is malformed") from exc
```

A `PolicyLoadError` from `default_policy()` indicates a broken installation — the wheel does not contain the file it claims to. Test coverage (§13) includes a smoke test that imports `trikon` and calls `default_policy()`; if the wheel drops the resource, the test fails at package build time before it ships.

**Validates: Requirement 2.5.**

### 6.3 Shipping `default_policy.yaml` in the wheel

The YAML file lives at `trikon/policy/default_policy.yaml`. Content is copied verbatim from `examples/policies/default.yaml` — same 6-rule policy, same `sensitive_paths`, same weights. The `examples/` copy is retained for documentation and for `trikon init` diagnostic messages; the runtime path is the packaged copy.

`pyproject.toml` fragment to include the YAML in the wheel:

```toml
[tool.hatch.build.targets.wheel]
packages = ["trikon"]

# Ship the packaged default policy inside the wheel. Without this,
# `importlib.resources.files("trikon.policy") / "default_policy.yaml"`
# raises FileNotFoundError on `pip install trikon` even though it works
# in a source checkout.
[tool.hatch.build.targets.wheel.force-include]
"trikon/policy/default_policy.yaml" = "trikon/policy/default_policy.yaml"
```

Rationale for `force-include` over a `package_data` entry: hatchling by default excludes non-Python files from `packages = ["trikon"]`; `force-include` is the documented escape hatch for that behavior and keeps the include list explicit. A single-line rule that names both source and destination path is the smallest surface that captures the intent.

**Validates: Requirement 2.5, Requirement 5.4.**

### 6.4 Round-trip equivalence between the two loaders

Property 6 (§17): for any repo state, `default_policy()` and `load_policy(repo_path, Path("/tmp/nonexistent.yaml"))` return byte-identical Policy objects. This is what makes the missing-file fallback path safe — the same conservative 6-rule policy runs whether the operator forgot to create `.trikon/policy.yaml` or the file was deleted after install.

The `trikon init` command (§11.2) copies the packaged YAML into `<repo>/.trikon/policy.yaml`, so a fresh `trikon init` followed by `trikon verify` runs the same policy the missing-file fallback would have. This is the round-trip: `init` writes bytes; `load_policy` on those bytes returns a Policy; `default_policy()` returns a Policy equivalent to that one.

**Validates: Requirements 2.5, 5.4.**

---

## 7. Verdict emission

### 7.1 Delta from the existing `evaluate_policy` implementation

The Phase-0 stub in `trikon/policy/evaluator.py` already implements the terminal-first loop and the `RuleResult` trace. Phase 3 adds two branches to the same loop:

```python
# BEFORE (Phase 0 stub)
for rule in policy.rules:
    matched = _rule_matches(rule, change, verification)  # raises NotImplementedError
    rule_results.append(RuleResult(
        rule_name=rule.name,
        matched=matched,
        would_emit=rule.then if matched else None,
        reason=rule.reason,
    ))
    if matched and rule.then in TERMINAL and decision is None:
        decision = rule.then
        matched_rule_name = rule.name
        reason = rule.reason or f"Matched rule '{rule.name}'."

if decision is None:
    decision = "require_human"

return Verdict(
    decision=decision,
    reason=reason,
    matched_rule=matched_rule_name,
    evidence=Evidence(change=change, verification=verification, policy_results=rule_results),
    audit_id=uuid4(),
    created_at=datetime.now(UTC),
)
```

```python
# AFTER (Phase 3)
warnings: list[str] = []

for rule in policy.rules:
    matched = _rule_matches(rule, change, verification)
    rule_results.append(RuleResult(
        rule_name=rule.name,
        matched=matched,
        # Widened: `would_emit` now reports the rule's `then` on both
        # terminal AND warn matches — Requirement 3.4.
        would_emit=rule.then if matched else None,
        reason=rule.reason,
    ))

    if not matched:
        continue

    if rule.then == "warn":
        # New Phase-3 branch. Warn rules accumulate reasons without
        # terminating (Requirement 3.3).
        warnings.append(rule.reason or f"Rule '{rule.name}' warned.")
        continue

    # Terminal branch — first-match-terminal wins (Requirement 3.1).
    if rule.then in TERMINAL and decision is None:
        decision = rule.then
        matched_rule_name = rule.name
        reason = rule.reason or f"Matched rule '{rule.name}'."

if decision is None:
    decision = "require_human"

return Verdict(
    decision=decision,
    reason=reason,
    matched_rule=matched_rule_name,
    evidence=Evidence(change=change, verification=verification, policy_results=rule_results),
    audit_id=uuid4(),
    created_at=datetime.now(UTC),
    warnings=warnings,
    # schema_version=2 by default (§3.6); no explicit set here.
)
```

Three deltas:

1. **`warnings: list[str]`** initialized at the top of the function.
2. **`rule.then == "warn"` branch** appends the rule's reason (or a synthesized default) and `continue`s. Non-terminal — control falls through to the next rule.
3. **Verdict construction** passes `warnings=warnings` to the Pydantic model. `schema_version` defaults to `2` via the model default (§3.6), so no explicit assignment appears here.

The terminal-branch condition is unchanged. `matched and rule.then in TERMINAL and decision is None` still gates the assignment — subsequent matching terminal rules do not overwrite the first winner. That is what Requirement 3.1's "first matching terminal rule in declaration order" fixes.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4.**

### 7.2 Warn accumulation preserves declaration order

`warnings.append` inside the same declaration-order loop guarantees the property. Property 3 in §17 pins it. A warn rule matched at position 4 lands in `warnings[k]` for the correct `k` given prior warn matches; a warn rule that does not match contributes nothing. There is no reordering, no dedup, and no filtering — the reason strings appear in `warnings` verbatim.

`rule.reason or f"Rule '{rule.name}' warned."` is the synthesized default. Requirement 3.3 permits the synthesized default; Phase 3 keeps the format uniform ("Rule '<name>' warned.") so audit-log consumers can parse the synthesized cases if they need to.

**Validates: Requirement 3.3.**

### 7.3 Fall-through default

When no terminal rule matches, `decision is None` at the end of the loop and the fall-through sets `decision = "require_human"`. `matched_rule_name` stays `None` (its initial value); `reason` stays at its initial `"No rule matched; defaulting to require_human."`. The default policy shipped in §6.3 always ends with a `"default"` rule whose `then: require_human` matches unconditionally, so this branch is only reached against custom policies that omit a fall-through — which is a policy authoring anti-pattern the Markdown output can surface via the `matched_rule = None` signal.

**Validates: Requirement 3.2.**

### 7.4 `Verdict.warnings` on the fail-closed path

The fail-closed helper `_fail_closed_verdict` (§10) constructs its own Verdict without going through `evaluate_policy`. Its warnings list is initialized to `[]` because the policy engine did not run — there are no warn-rule matches to accumulate. That is consistent with the requirement: `Verdict.warnings` is `list[str] = Field(default_factory=list)` and defaults to empty on any code path that does not populate it.

**Validates: Requirement 3.3.**

---

## 8. Audit log writer

### 8.1 `record_verdict` pseudocode

```
INPUT:  conn (sqlite3.Connection)
        verdict (Verdict)

STEP 1: Serialize the Verdict to JSON
  verdict_json ← verdict.model_dump_json()  # schema_version=2 embedded

STEP 2: INSERT the row
  try:
      conn.execute(
          "INSERT INTO audit_log "
          "(audit_id, created_at, decision, matched_rule, reason, verdict_json) "
          "VALUES (?, ?, ?, ?, ?, ?)",
          (
              str(verdict.audit_id),
              verdict.created_at.isoformat(),
              verdict.decision,
              verdict.matched_rule,           # None → SQLite NULL
              verdict.reason,
              verdict_json,
          ),
      )
      conn.commit()
  except sqlite3.Error as exc:
      raise AuditLogError(f"failed to record verdict {verdict.audit_id}") from exc

STEP 3: Return (no return value; success is the absence of raise)
```

`conn.commit()` is inside the same `try` block so an integrity-constraint failure at commit time (extremely unlikely on a single-row INSERT, but possible under corrupt WAL) still translates to `AuditLogError`. The commit is unconditional — the writer does not participate in a caller-owned transaction.

The SQL statement is a module-level constant string (`_INSERT_AUDIT_LOG`) rather than an inline literal, so the AST scan for `UPDATE|DELETE|DROP` (§4.2, Property 7) has a single well-known target to grep.

**Validates: Requirements 4.1, 4.2, 4.5.**

### 8.2 Call sites

`sdk.verify` calls `record_verdict` on both return paths:

- **Happy path**: after `evaluate_policy` returns the Verdict, outside the `try`/`except` (so an `AuditLogError` propagates rather than being caught and swallowed).
- **Fail-closed path**: `_fail_closed_verdict` builds the Verdict, then the caller writes it to the audit log before returning.

An audit-write failure on the fail-closed path is a compound failure — the policy engine already fail-closed, and now the audit trail is broken too. The SDK re-raises the `AuditLogError` in that case; there is no "double fail-close" recovery. The caller (CLI, MCP tool, GitHub App) sees a `TrikonError` and is responsible for surfacing it. This matches Requirement 4.5's "an audit failure is a hard failure" contract.

The exact call pattern in `sdk.verify` is shown in §10.

**Validates: Requirements 4.5, 7.3.**

### 8.3 Module surface — the append-only contract

`trikon/audit_log/__init__.py` re-exports two names:

```python
from trikon.audit_log.db import ensure_audit_tables
from trikon.audit_log.writer import record_verdict

__all__ = ["ensure_audit_tables", "record_verdict"]
```

There is no `update_verdict`, no `delete_verdict`, no `truncate_audit_log`, no `purge_older_than`. Retention policy, hash chaining, and structured export are Phase 4 material — the point of the narrow surface is that no code path inside `trikon/audit_log/**` can mutate a row that has already been written. Property 7 in §17 encodes this as a static AST-scan invariant.

**Validates: Requirement 4.3.**

---

## 9. `PolicyEvaluationError` hierarchy

One new file, four classes:

```python
# trikon/policy/errors.py  (NEW)
"""Exception hierarchy for the Policy Engine + Audit Log subsystems.

Every raise site under ``trikon.policy`` and ``trikon.audit_log`` MUST
use one of the classes defined here. Nothing raises bare ``Exception``,
``ValueError``, ``KeyError``, ``yaml.YAMLError``, ``pydantic.ValidationError``,
or ``sqlite3.Error`` past either module boundary — that closure is what
lets ``trikon.sdk.verify`` translate any internal failure into a
``require_human`` verdict without ambiguity.

See requirements.md §Requirement 7 and design.md §13."""

from __future__ import annotations

from trikon.exceptions import TrikonError


class PolicyEvaluationError(TrikonError):
    """Base class for every error raised by :mod:`trikon.policy` and
    :mod:`trikon.audit_log`.

    Raisable directly for evaluator-internal failures that do not fit
    a more specific subclass. Callers should catch this base class (or
    :class:`TrikonError`) exactly once, at the SDK boundary, and convert
    the failure into a ``require_human`` verdict."""


class PolicyLoadError(PolicyEvaluationError):
    """The policy YAML could not be loaded or validated.

    Raised by :func:`trikon.policy.loader.load_policy` and
    :func:`trikon.policy.loader.default_policy`. Wraps the underlying
    :class:`yaml.YAMLError` or :class:`pydantic.ValidationError` on
    ``__cause__`` (Requirements 2.3, 2.4). Also raised for read-permission
    failures on the resolved path and for missing packaged
    ``default_policy.yaml`` (indicates a broken wheel)."""


class RuleMatchError(PolicyEvaluationError):
    """A rule's ``when`` clause references an unknown condition key or
    a malformed operator dict.

    Raised inside :func:`trikon.policy.evaluator._rule_matches` when the
    dispatcher receives a key it does not recognize (§5.3), or when
    ``verification.static.new_errors`` receives an operator dict that is
    not a single-key mapping of ``eq|gt|lt`` to an int (§5.5). This is a
    policy-authoring error, not a data error — the fix is to correct
    ``.trikon/policy.yaml`` (Requirement 7.1)."""


class AuditLogError(PolicyEvaluationError):
    """The audit log could not accept a write.

    Raised by :func:`trikon.audit_log.writer.record_verdict` and
    :func:`trikon.audit_log.db.ensure_audit_tables` when the underlying
    SQLite operation fails (:class:`sqlite3.Error` on execute or commit,
    :class:`sqlite3.IntegrityError` on a duplicate ``audit_id``, disk
    full, schema mismatch). The original exception is chained via
    ``__cause__``. An audit-write failure is a hard failure at the SDK
    boundary (Requirement 4.5)."""
```

### 9.1 Raise sites and boundary behavior

| Class | Raise site | Boundary behavior |
| ----- | ---------- | ----------------- |
| `PolicyEvaluationError` | `evaluate_policy` defensive raises (should be unreachable on valid inputs) | Bubbles to `sdk.verify`; verdict is `require_human` + `EMPTY_IMPACT_SET` + `EMPTY_VERIFICATION` |
| `PolicyLoadError` | `load_policy`, `default_policy` (missing packaged YAML, YAML parse error, Pydantic validation error, `OSError` on read) | Bubbles; same treatment |
| `RuleMatchError` | `_rule_matches` (unknown condition key, malformed operator dict, shape-mismatch on value type) | Bubbles; same treatment |
| `AuditLogError` | `record_verdict` (`sqlite3.Error`, `IntegrityError`), `ensure_audit_tables` (`sqlite3.Error` on DDL) | Bubbles as a **hard failure** — Requirement 4.5. The SDK does NOT convert this to `require_human`; it re-raises so the caller sees a broken audit trail. |

There is one boundary-behavior wrinkle worth pinning: three of the four subclasses fail-close to `require_human` at the SDK boundary. `AuditLogError` does not, because a `require_human` verdict without an audit-trail row is functionally indistinguishable from no verdict at all — the compliance surface loses its evidence. Requirement 4.5 is explicit that audit failure is a hard failure; the SDK re-raises rather than degrade silently.

**Validates: Requirements 7.1, 4.5.**

---

## 10. SDK integration

Before/after of `trikon/sdk.py::verify`:

```python
# BEFORE (Phase 2)
def verify(repo_path, base_sha=None, head_sha=None, diff=None, *,
           policy_path=".trikon/policy.yaml", cache_db=None) -> Verdict:
    repo = Path(repo_path)
    try:
        change_set = parse_diff(repo, base_sha=base_sha,
                                head_sha=head_sha, diff=diff)
        impact = compute_impact(change_set, repo, cache_db=cache_db)
        verification = run_verification(repo, impact, state_db=cache_db)
    except (ChangeIntelError, VerificationRunnerError) as exc:
        return _fail_closed_verdict(reason=f"{type(exc).__name__}: {exc}")

    return Verdict(
        decision="require_human",   # hardcoded — Phase 3 will fix this
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

```python
# AFTER (Phase 3)
def verify(repo_path, base_sha=None, head_sha=None, diff=None, *,
           policy_path=".trikon/policy.yaml", cache_db=None) -> Verdict:
    repo = Path(repo_path)

    # Try the full pipeline. Any TrikonError (change-intel, verification,
    # policy load, policy eval) fail-closes to require_human. AuditLogError
    # is NOT caught here — it re-raises so a broken audit trail surfaces
    # as a hard failure (Requirement 4.5).
    try:
        change_set = parse_diff(repo, base_sha=base_sha,
                                head_sha=head_sha, diff=diff)
        impact = compute_impact(change_set, repo, cache_db=cache_db)
        verification = run_verification(repo, impact, state_db=cache_db)
        policy = load_policy(repo, Path(policy_path))
        verdict = evaluate_policy(policy, impact, verification)
    except AuditLogError:
        # Shouldn't be raised inside this try (record_verdict is outside),
        # but if a future refactor introduces one, propagate hard.
        raise
    except TrikonError as exc:
        verdict = _fail_closed_verdict(reason=f"{type(exc).__name__}: {exc}")

    # Audit write is OUTSIDE the try/except (Requirement 4.5). A failure
    # here re-raises AuditLogError to the caller — a verdict without an
    # audit trail is a hard failure, not a silent degrade.
    state_db = cache_db if cache_db is not None else repo / ".trikon" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_db))
    try:
        ensure_audit_tables(conn)     # additive-only DDL; idempotent
        record_verdict(conn, verdict)
    finally:
        conn.close()

    return verdict
```

Key deltas from Phase 2:

1. **`except (ChangeIntelError, VerificationRunnerError)` → `except TrikonError`.** All three subsystem base classes inherit from `TrikonError` (Phase 1 re-parenting, Phase 2 addition, Phase 3 addition), so a single catch clause suffices. This is the collapse the Phase-2 doc anticipated.
2. **`_PHASE_2_REASON` is retired.** The Verdict returned on the happy path is now the one `evaluate_policy` produced — real decision, real matched_rule, real reason, real `warnings`. The hardcoded `"require_human"` is gone.
3. **`_fail_closed_verdict` is unchanged in shape** but now also gets called for `PolicyLoadError` and `RuleMatchError` (both `TrikonError` subclasses). Its output still has `warnings=[]` by default (§7.4).
4. **`record_verdict` is called outside the `try`/`except`.** An audit-write failure is a hard failure per Requirement 4.5. The `conn.close()` runs in a `finally` so the connection is not leaked on either return path.
5. **`policy_path` is no longer discarded.** Phase 2 did `del policy_path`; Phase 3 wires it to `load_policy`.

The pipeline order — `parse_diff → compute_impact → run_verification → load_policy → evaluate_policy → record_verdict` — matches Requirement 5.1 verbatim.

**Validates: Requirements 5.1, 7.1, 7.2, 7.3, 4.5.**

### 10.1 `_fail_closed_verdict` — no changes needed

The Phase-2 helper already returns a Verdict with `decision="require_human"`, `matched_rule=None`, and the two empty sentinels. Phase 3 changes only the `Verdict` model default for `schema_version` (1 → 2); the helper picks that up automatically. `warnings` defaults to `[]` (also model default), so the fail-closed shape gains a `warnings` field without any code edit to the helper. See `trikon/sdk.py::_fail_closed_verdict`.

**Validates: Requirement 7.2.**


---

## 11. CLI surfaces

### 11.1 `trikon verify`

The Phase-2 stub becomes the top-level command every CI job calls:

```
trikon verify --repo <path> --base <sha> --head <sha>
              [--diff-file <path>] [--policy <path>]
              [--output markdown|json]
```

Wire-up:

```python
@app.command()
def verify(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
    base: str | None = typer.Option(None, "--base", help="Base commit SHA."),
    head: str | None = typer.Option(None, "--head", help="Head commit SHA."),
    diff_file: Path | None = typer.Option(None, "--diff-file"),
    policy: Path = typer.Option(
        Path(".trikon/policy.yaml"),
        "--policy",
        help="Path to the policy YAML (relative to repo or absolute).",
    ),
    output: str = typer.Option(
        "markdown", "--output", "-o",
        help="Output format: markdown | json.",
    ),
) -> None:
    """Verify a proposed change and emit a verdict."""
    if output not in ("markdown", "json"):
        typer.echo(f"Error: --output must be markdown|json, got {output!r}", err=True)
        raise typer.Exit(code=2)

    diff_content = diff_file.read_text(encoding="utf-8") if diff_file is not None else None
    verdict = sdk_verify(
        repo,
        base_sha=base, head_sha=head, diff=diff_content,
        policy_path=policy,
    )

    if output == "json":
        typer.echo(verdict.model_dump_json(indent=2))
    else:
        typer.echo(format_markdown(verdict))

    raise typer.Exit(code=_EXIT_CODE_FOR[verdict.decision])


_EXIT_CODE_FOR: dict[str, int] = {
    "allow": 0,
    "block": 1,
    "require_human": 2,
    # `warn` is never emitted by sdk.verify (§7). Included here so a
    # forward-compat Verdict with decision=="warn" produces a defined,
    # non-crashing exit. `2` matches require_human — a warn-only outcome
    # still needs a human to look at it.
    "warn": 2,
}
```

Exit-code contract (Requirements 5.2, 5.3):

| Decision | Exit code | Rationale |
| -------- | --------- | --------- |
| `allow` | 0 | Success — CI can merge or continue. |
| `block` | 1 | Explicit rejection — CI job fails. |
| `require_human` | 2 | Needs review — CI job passes but the merge queue holds. |
| `warn` (defensive) | 2 | Never emitted; mapped to `2` for forward compat. |

`--output json` produces the same JSON `trikon debug verify --json` produces (`verdict.model_dump_json(indent=2)`) but with the decision-based exit codes rather than always-0. Requirement 5.3 explicitly names this: JSON output preserves the exit-code mapping from Requirement 5.2.

**Validates: Requirements 5.1, 5.2, 5.3.**

### 11.2 `trikon init`

Copies the packaged `default_policy.yaml` into the repo:

```
trikon init [--repo <path>] [--force]
```

Wire-up:

```python
@app.command()
def init(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing .trikon/policy.yaml.",
    ),
) -> None:
    """Scaffold a starter .trikon/policy.yaml in the target repo."""
    target = repo / ".trikon" / "policy.yaml"
    if target.exists() and not force:
        typer.echo(
            f"Error: {target} already exists. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(code=1)

    target.parent.mkdir(parents=True, exist_ok=True)
    resource = importlib.resources.files("trikon.policy") / "default_policy.yaml"
    try:
        content = resource.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        typer.echo(f"Error: packaged default_policy.yaml missing: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    target.write_text(content, encoding="utf-8")
    typer.echo(f"Wrote {target}")
```

Exit codes:

| Condition | Exit code |
| --------- | --------- |
| Success (new file, or `--force` overwrote) | 0 |
| Existing file, no `--force` | 1 |
| Missing packaged resource | 1 |
| Typer usage error | 2 |

The packaged YAML is read via `importlib.resources.files` so a wheel install works identically to a source checkout. `target.parent.mkdir(parents=True, exist_ok=True)` creates `.trikon/` if needed. `force` guards the overwrite — without it, `target.exists()` fails loudly.

**Validates: Requirement 5.4.**

### 11.3 `trikon debug verify`

Preserved from Phase 2. Continues to render the Phase-2 human-readable summary via `format_verification_verdict` and always exits `0`. Requirement 5.5 pins this — `trikon debug verify` stays the developer diagnostic surface distinct from `trikon verify`.

The Phase-2 implementation in `trikon/cli.py::debug_verify` already:

- Runs `sdk.verify(...)` (which after Phase 3 returns a real evaluated Verdict, not the hardcoded `require_human`).
- Prints either the human-readable summary or the JSON dump depending on `--json`.
- Exits `0` unconditionally.

No code change in Phase 3 — the command's behavior is orthogonal to the policy-engine wiring. Its output does get richer (the Verdict now has a real `decision`, `matched_rule`, `warnings`), but the exit-code contract is unchanged.

**Validates: Requirement 5.5.**

---

## 12. Markdown formatter design

### 12.1 7-block layout

`format_markdown(verdict)` returns a single string composed of the following blocks in order. Every block is a Markdown section; blocks are joined by `"\n\n"`. Empty conditional blocks (Warnings when `verdict.warnings == []`) are simply omitted.

```
Block 1 — Header
    {ICON[decision]} **Trikon: {DECISION_UPPER}** — {matched_rule_display}
    > {reason}

Block 2 — Focus your review on
    ## Focus your review on
    - `<path 1>`
    - `<path 2>`
    - ... (up to 5, from evidence.change.changed_files in list order)

Block 3 — Impact
    ## Impact
    - **Changed files:** 3
    - **Impacted modules:** 5
    - **Impacted public APIs:** 2
    - **Blast radius:** MEDIUM (0.42)

Block 4 — Verification
    ## Verification
    - **Tests:** 27 passed · 2 failed · 1 skipped
    - **First 5 failing tests:**
      - `tests/test_worker.py::test_backoff_shape` — assertion failed
      - `tests/test_worker.py::test_backoff_max_retries` — timeout after 5s

Block 5 — Warnings (conditional — omitted when verdict.warnings is empty)
    ## Warnings
    - Change touches deprecated module `foo.legacy`.
    - Test coverage below 60% threshold.

Block 6 — Footer
    ---
    _Audit id_: `9c1a...`  ·  _Schema version_: `2`
```

### 12.2 Header details

- `ICON`: `{"allow": "✅", "block": "🛑", "require_human": "🔍", "warn": "⚠️"}`. Requirement 6.1 names three; `"warn"` is included for forward compat but never emitted at the SDK boundary.
- `DECISION_UPPER`: `verdict.decision.upper()`.
- `matched_rule_display`:
  - When `verdict.matched_rule is not None`: `` `<name>` `` (backtick-wrapped rule name).
  - When `None` (fall-through or fail-closed): the literal string `` `<no rule matched>` ``.
- The `reason` line is prefixed with `> ` to render as a Markdown blockquote, so long reason strings wrap without breaking the section flow.

**Validates: Requirement 6.1.**

### 12.3 "Focus your review on" details

- Draws from `verdict.evidence.change.changed_files` in list order (Requirement 6.2). No sort, no dedup — the change-intel layer already produces a stable list.
- Slices to `min(5, len(changed_files))`. If fewer than 5 files are present, only that many bullets appear.
- Each path is backtick-wrapped so paths containing punctuation render as inline code.
- On an empty `changed_files` list (fail-closed path with `EMPTY_IMPACT_SET`), the section renders with a single italic bullet: `- _no files_`. This keeps the section shape stable across happy-path and fail-closed rendering.

**Validates: Requirement 6.2.**

### 12.4 "Impact" details

Four bullet points, all sourced from `verdict.evidence.change`:

| Bullet | Source |
| ------ | ------ |
| Changed files | `len(evidence.change.changed_files)` |
| Impacted modules | `len(evidence.change.impacted_modules)` |
| Impacted public APIs | `len(evidence.change.impacted_public_apis)` |
| Blast radius | `f"{evidence.change.blast_radius_score} ({evidence.change.blast_radius_numeric:.2f})"` |

The blast-radius line combines the bucket string (`LOW` / `MEDIUM` / `HIGH`) and the numeric score to two decimal places — Requirement 6.3 names both. The two-decimal precision is a formatting choice; the underlying float is preserved on the Verdict.

**Validates: Requirement 6.3.**

### 12.5 "Verification" details

- Top line: `"{passed} passed · {failed} failed · {skipped} skipped"` — Requirement 6.4.
- Failing-tests sub-list:
  - Slices `verdict.evidence.verification.tests.failures` to `min(5, len(failures))`.
  - Each entry renders as `` - `<node_id>` — <failure_summary> `` (dash-separator). When `failure_summary is None`, the entry omits the trailing `—` and text.
- When `failures` is empty (all tests passed or all were skipped), the sub-list header is omitted; the top line stands alone.

**Validates: Requirement 6.4.**

### 12.6 "Warnings" details

- **Conditional presence**: block emitted iff `verdict.warnings` is non-empty (Requirement 6.5). An empty list produces no `## Warnings` header.
- Each warning renders as `- {warning_string}` verbatim, in list order.
- No truncation — warn accumulation is bounded by the number of warn rules in the policy (typically < 10).

**Validates: Requirement 6.5.**

### 12.7 Footer

Two comma-separated fields on a single line under a horizontal rule:

```
---
_Audit id_: `<uuid>`  ·  _Schema version_: `<int>`
```

`<uuid>` is `str(verdict.audit_id)`; `<int>` is `verdict.schema_version` (always `2` for Phase-3-emitted Verdicts). The footer is what lets a reviewer paste an audit ID back into a compliance query and pull the exact stored `verdict_json`. Property 12 in §17 pins that every emitted Verdict carries `schema_version == 2`.

**Validates: Requirement 6.6 (contains `audit_id`), and cross-cutting Glossary invariant on schema_version.**

### 12.8 Purity and dependency budget

`format_markdown` is a pure function of `Verdict`:

- No I/O (`open`, `read_text`, `write_text`, `socket`, HTTP).
- No template engine (`jinja2`, `mako`, `chevron`, `string.Template`).
- Only stdlib and the already-imported Pydantic model. `str.format`, f-strings, `"\n".join`, and list slicing carry the whole thing.
- Runs in a few milliseconds on any Verdict (§2.3, Requirement 8.3).

Property 11 in §17 asserts every named section is present with the correct projection. A follow-on static test (§13) grep-scans the formatter module for import strings that would violate the stdlib-only rule.

**Validates: Requirement 6.6.**


---

## 13. Error handling & fail-closed matrix

Every failure mode the Policy Engine + Audit Log stack can produce, where it is detected, what class it becomes, and what the SDK boundary emits.

| # | Failure mode | Detected at | Raised as | SDK-boundary outcome |
| - | ------------ | ----------- | --------- | -------------------- |
| 1 | Missing `.trikon/policy.yaml` | `load_policy` — `resolved.exists()` returns False | *Not raised* — fall through to `default_policy()` (Requirement 2.2) | Happy path with default policy |
| 2 | Malformed YAML (unbalanced quotes, tabs mixed) | `yaml.safe_load` in `load_policy` | `PolicyLoadError` with `yaml.YAMLError` on `__cause__` | `require_human` + `EMPTY_IMPACT_SET` + `EMPTY_VERIFICATION`; audit row written on fail-closed path |
| 3 | `pydantic.ValidationError` (missing `rules`, wrong types, `version != 1`) | `Policy.model_validate` in `load_policy` | `PolicyLoadError` with `ValidationError` on `__cause__` | Same as #2 |
| 4 | Unknown condition key in a rule's `when` | `_rule_matches` inline dispatcher (§5.2) | `RuleMatchError` naming the key and rule | Same as #2 |
| 5 | Malformed operator dict on `verification.static.new_errors` (wrong operator name, non-int threshold, multi-key mapping) | `_match_new_errors` (§5.5) | `RuleMatchError` naming the specific violation | Same as #2 |
| 6 | `sqlite3.Error` on `record_verdict` INSERT (disk full, WAL corruption) | `record_verdict` `try/except` | `AuditLogError` with `sqlite3.Error` on `__cause__` | **Hard failure** — re-raised past `sdk.verify` (Requirement 4.5). No fail-close. |
| 7 | `importlib.resources` failure loading packaged `default_policy.yaml` (broken wheel, missing resource) | `default_policy` — `resource.read_text()` | `PolicyLoadError` with the underlying `OSError`/`FileNotFoundError` on `__cause__` | Same as #2 |
| 8 | `PurePath.match` fails (should be unreachable — patterns are validated as strings; kept for defense) | `_match_any_path` inner loop | `RuleMatchError` with the offending pattern | Same as #2 |
| 9 | Empty `when` clause (`{}`) | `_rule_matches` first branch | *Not raised* — returns True unconditionally (Requirement 1.7) | Rule matches; downstream logic proceeds |
| 10 | `sqlite3.Error` on `ensure_audit_tables` DDL | `ensure_audit_tables` `try/except` | `AuditLogError` with `sqlite3.Error` on `__cause__` | Bubbles out of `DepGraph._get_conn`; treated as a Change-Intel init failure by the SDK boundary (same fail-close as #2) since it happens before `evaluate_policy` runs |
| 11 | `os.error` on `trikon init` overwrite without `--force` | CLI wire-up (§11.2) | *Not raised* — CLI prints "already exists" and exits 1 (Requirement 5.4) | CLI exit 1, no verdict emitted |
| 12 | Missing packaged `default_policy.yaml` at CLI `trikon init` time | `importlib.resources.files(...).read_text()` in `init` command | *Not raised past CLI* — prints an error and exits 1 | CLI exit 1, no verdict emitted |
| 13 | Duplicate `audit_id` INSERT (`sqlite3.IntegrityError` on the PRIMARY KEY constraint) | `record_verdict` INSERT | `AuditLogError` with `IntegrityError` on `__cause__` | Hard failure — same as #6. Indicates a UUID collision or a caller trying to re-record a verdict, both of which are bugs the audit trail should surface. |

The invariant, encoded as a closure test: for every subclass of `PolicyEvaluationError` **except `AuditLogError`**, `sdk.verify` returns a `Verdict` with `decision == "require_human"` and `evidence.change == EMPTY_IMPACT_SET` and `evidence.verification == EMPTY_VERIFICATION`. `AuditLogError` re-raises. `allow` is never emitted on any of these paths (Requirement 7.4) — the `EMPTY_IMPACT_SET`'s `blast_radius_score == "HIGH"` guarantees the default policy's fall-through resolves to `require_human`, and the fail-closed helper hardcodes `decision="require_human"` regardless.

**Validates: Requirement 7 in full.**

---

## 14. Testing strategy

### 14.1 Layout

```
tests/
├── unit/
│   ├── policy/
│   │   ├── test_evaluator_conditions.py    # per-condition dispatch
│   │   ├── test_evaluator_terminal.py      # first-match-terminal, warn accumulation
│   │   ├── test_loader.py                  # load_policy + default_policy
│   │   ├── test_errors.py                  # exception hierarchy invariants
│   │   └── strategies.py                   # hypothesis generators
│   ├── audit_log/
│   │   ├── test_db.py                      # ensure_audit_tables idempotence
│   │   ├── test_writer.py                  # record_verdict INSERT semantics
│   │   └── test_append_only_closure.py     # AST scan (Property 7)
│   └── evidence/
│       └── formatters/
│           ├── test_markdown.py            # per-section content
│           └── test_markdown_purity.py     # no I/O, no template engine
├── integration/
│   └── policy/
│       ├── test_sample_repo_bad_retry.py       # decision=block, matched_rule="impacted tests failed"
│       ├── test_sample_repo_clean_refactor.py  # decision=allow, matched_rule="green, low-blast auto-allow"
│       ├── test_sample_repo_sensitive.py       # decision=require_human, matched_rule="sensitive path requires human"
│       ├── test_never_fail_open.py             # PolicyEvaluationError → require_human + audit still written
│       └── test_trikon_init.py                 # init + verify round-trip
└── benchmarks/
    └── test_policy_perf.py                    # Requirement 8 targets
```

### 14.2 Unit tests per module

- **`test_evaluator_conditions.py`** — one test per condition kind (Requirements 1.1-1.5). Each parametrizes over positive and negative cases, plus one shape-mismatch case that asserts `RuleMatchError`. Hypothesis strategy in `strategies.py` generates `(rule, change, verification)` triples for property-based coverage.
  - `any_path_matches`: hits, misses, empty pattern list, `**` recursion.
  - `no_path_matches`: dual of `any_path_matches` — same inputs, negated result.
  - `change.blast_radius.score`: parametrized over the three bucket values and the three observed values (nine combinations).
  - `verification.tests.status`: same shape as blast_radius.
  - `verification.static.new_errors`: parametrized over `(eq, gt, lt) × N ∈ {0, 1, 5} × observed count ∈ {0, 1, 5}`.
- **`test_evaluator_terminal.py`** — the terminal-decision loop.
  - First matching terminal rule wins (Property 2). Two terminal rules both match; the earlier `then` becomes `decision`.
  - Fall-through defaults to `require_human` (Requirement 3.2). No rule matches; `matched_rule is None`.
  - Warn accumulation (Property 3). Three warn rules match; `warnings` has three entries in declaration order.
  - Every rule leaves a trace (Requirement 3.4). `len(evidence.policy_results) == len(policy.rules)`.
  - Empty `when` unconditional (Property 4). A rule with `when={}` and terminal `then` matches every input.
- **`test_loader.py`**
  - Happy path: on-disk `.trikon/policy.yaml` with a valid policy → Pydantic-equal to the parsed YAML.
  - Missing file → `default_policy()` (Requirement 2.2). Assert byte-identical to `Policy.model_validate(yaml.safe_load(importlib.resources.read_text("trikon.policy", "default_policy.yaml")))`.
  - Malformed YAML → `PolicyLoadError` with `yaml.YAMLError` on `__cause__` (Requirement 2.4).
  - Malformed Policy (missing `rules`, wrong type, `version=99`) → `PolicyLoadError` with `pydantic.ValidationError` on `__cause__` (Requirement 2.3).
  - Round-trip property: for any Hypothesis-generated `Policy`, dump to YAML, load, assert Pydantic-equal (Property 6 in §17 anchors this).
- **`test_errors.py`**
  - AST-scan `trikon/policy/**` and `trikon/audit_log/**` for `raise` statements whose target class is not a `PolicyEvaluationError` subclass — Property 7 (§17). Uses `ast.walk` on each module's source.
  - Instantiability: every subclass constructs cleanly from a single string arg.
- **`test_db.py`**
  - `ensure_audit_tables` twice on the same connection → no error, no row loss (idempotence).
  - `ensure_audit_tables` on a `sqlite3.Error`-injected connection → `AuditLogError` with `sqlite3.Error` on `__cause__`.
  - Schema shape: `PRAGMA table_info(audit_log)` returns the six columns in the design.
- **`test_writer.py`**
  - Happy path: one Verdict in, one row out. Assert every column value matches the corresponding `Verdict` field.
  - `verdict_json` round-trip: `Verdict.model_validate_json(row.verdict_json).model_dump() == verdict.model_dump()`.
  - Duplicate `audit_id` → `sqlite3.IntegrityError` wrapped as `AuditLogError`.
  - Fault injection: monkeypatch `conn.execute` to raise `sqlite3.OperationalError` → `AuditLogError` chained.
- **`test_append_only_closure.py`** — Property 7 (§17). Static AST scan of `trikon/audit_log/**` for `str` literals containing SQL verbs `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE` (case-insensitive). Only `INSERT`, `CREATE`, and `SELECT` (for smoke-test count queries in tests) are permitted. Fails if any forbidden verb appears in production source.
- **`test_markdown.py`** — per-section coverage, mostly example-based.
  - Header for each of `allow`, `block`, `require_human` — icon matches, rule name appears, reason appears.
  - "Focus your review on" — parametrize over `changed_files` lists of length 0, 1, 5, 10 (assert slice to 5, in list order).
  - "Impact" — assert every field appears; blast radius line contains bucket + numeric with two-decimal precision.
  - "Verification" — pass/fail/skip counts; failure sub-list appears iff `failures` is non-empty; slice to 5.
  - "Warnings" — section absent iff `warnings == []`; section present with all warnings in list order otherwise.
  - Footer — `audit_id` and `schema_version` both present.
- **`test_markdown_purity.py`** — grep the source of `trikon/evidence/formatters/markdown.py` for forbidden imports (`jinja2`, `mako`, `chevron`, `string.Template`, `open`, `Path(...).read_text()`, `socket`, `httpx`, `requests`). Property 11 in §17 (partial).

### 14.3 Integration tests (`tests/integration/policy/`)

- **`test_sample_repo_bad_retry.py`** — apply the bad_retry patch to a fresh clone of `examples/sample_repo/`, run `sdk.verify(repo, base_sha, head_sha)`, assert:
  - `verdict.decision == "block"`.
  - `verdict.matched_rule == "impacted tests failed"`.
  - `verdict.reason.startswith("One or more impacted tests failed.")`.
  - The audit table contains exactly one new row with `verdict.audit_id`.
- **`test_sample_repo_clean_refactor.py`** — apply the clean_refactor patch, assert:
  - `verdict.decision == "allow"`.
  - `verdict.matched_rule == "green, low-blast auto-allow"`.
  - Warnings list may be empty or contain rule-driven notices; do not over-constrain.
- **`test_sample_repo_sensitive.py`** — patch a file under `payments/`, assert:
  - `verdict.decision == "require_human"`.
  - `verdict.matched_rule == "sensitive path requires human"`.
- **`test_never_fail_open.py`** — Property 9 (§17). Fault-inject at each raise site by monkeypatching:
  1. `parse_diff` to raise `ChangeIntelError` → assert `decision=="require_human"` and audit row present.
  2. `load_policy` to raise `PolicyLoadError` → same.
  3. `evaluate_policy` internal to raise `RuleMatchError` → same.
  4. `record_verdict` to raise `AuditLogError` → assert `AuditLogError` propagates past `sdk.verify` (hard failure per Requirement 4.5).
- **`test_trikon_init.py`** — end-to-end `trikon init` in a temp repo:
  1. First `init` → `.trikon/policy.yaml` created, byte-identical to packaged `default_policy.yaml`.
  2. Second `init` without `--force` → exit code 1, file unchanged.
  3. Second `init` with `--force` → exit code 0, file overwritten with packaged content.
  4. Subsequent `trikon verify` uses the freshly-written policy.

### 14.4 Perf smoke test (`@pytest.mark.perf`, opt-in)

- `evaluate_policy` on 6-rule default × `sample_repo` evidence ≤ 100 ms (Requirement 8.1).
- `load_policy` on 10 KB YAML ≤ 50 ms (Requirement 8.2).
- `format_markdown` on fully-populated Verdict ≤ 10 ms (Requirement 8.3).

Runs in the nightly CI job — matches the Phase-1/2 `perf` job structure in `.github/workflows/ci.yml`.

### 14.5 Coverage target

`coverage report --fail-under=85 --include='trikon/policy/*,trikon/audit_log/*'`.

Phase 3 sets the same 85 % floor as Phase 2's `trikon/verify/**` floor. Phase 1's 90 % floor on `trikon/change_intel/**` is preserved. The 5 % gap in Phase 3 is reserved for:

- The `except OSError` defensive branches in `load_policy` (rarely reachable without a filesystem fault).
- The `defensive PolicyEvaluationError` raise in `evaluate_policy` (unreachable on valid inputs; documented in §9).

---

## 15. Performance analysis

Per-target expected cost against Requirement 8:

| Requirement | Target | Expected (Ryzen-7 dev laptop) | Slack | Design lever if slipping |
| ----------- | ------ | ----------------------------- | ----- | ------------------------ |
| 8.1 `evaluate_policy` on 6-rule default | ≤ 100 ms | ~2 ms (pure Python, no I/O, 6 rules × ≤ 3 conditions each) | ~98 ms | None needed; slack is 50×. |
| 8.2 `load_policy` on ≤ 10 KB YAML | ≤ 50 ms | ~5 ms (PyYAML C loader + Pydantic v2 core parser) | ~45 ms | Cache the parsed Policy per (path, mtime) pair — trivially recovers another ~4 ms if the same policy loads twice. Deferred; not needed. |
| 8.3 `format_markdown` on fully-populated Verdict | ≤ 10 ms | ~1 ms (f-string + list join, no template engine) | ~9 ms | None needed. |
| — `record_verdict` INSERT | *ungated* | ~2 ms (single-row INSERT + commit on WAL SQLite) | — | Batch commits across multiple verdicts — not needed at v0.1 volumes. |
| — `ensure_audit_tables` DDL (idempotent) | *ungated* | ~0.3 ms on cached path, ~2 ms on first-open | — | Cache the "already ran" flag on the connection — hatchling-style; not needed. |

**Total Phase-3 addition to `sdk.verify` wall clock**: ~10 ms on the cached policy path. That is well within the Phase-1/2 60 s cold / 15 s warm ceilings for `sdk.verify` — Phase 3 does not perturb the verification-runner budget.

**Validates: Requirement 8 in full.**

---

## 16. Alternatives considered

- **Registry-based rule dispatch (`_MATCHERS: dict[str, Callable]`).** Rejected: five conditions is under the "table wins" threshold; a registry would need to erase argument types to `object` (or `Any`, forbidden by project rule G6), losing mypy narrowing for each branch. Inline `if`/`elif` chain retains per-branch typing and adds a new condition in a two-line diff.
- **Encoding warn accumulation as a separate pass after the terminal-first loop.** Rejected: two passes would need to re-run `_rule_matches` twice on non-terminal rules OR would need to cache match results on `RuleResult` — either doubles the dispatch cost or leaks a mutable state field. The single-pass loop in §7.1 is trivially correct and preserves declaration-order semantics for free.
- **Separate `audit.db` file (not `state.db`).** Rejected: consistency argues for a single SQLite file the whole `.trikon/` toolchain agrees on. Two files means two locking regimes, two backup surfaces, and split query patterns for compliance dashboards that want to correlate audit rows with the dep-graph state that produced them.
- **`glob.fnmatch.fnmatchcase` for path patterns.** Rejected: `fnmatch.fnmatchcase` does not honor `**` — a `"auth/**"` pattern would not match `"auth/tokens/rsa.py"`. `pathlib.PurePath.match` on POSIX paths handles `**` correctly and is stdlib (§5.4).
- **Making `AuditLogError` fail-close to `require_human` like the other three subclasses.** Rejected: an audit failure means the compliance record is broken; returning a `require_human` verdict without persisting it silently degrades the invariant `EXECUTION_PLAN.md` sets ("every verdict is auditable"). Requirement 4.5 is explicit; the design honors it.
- **Emitting the four-value `Decision` widening as a breaking Pydantic v3-style discriminated union.** Rejected: `Literal["allow","block","require_human","warn"]` covers the type surface without introducing a discriminator field, keeps the JSON shape flat, and lets `schema_version=2` be the single compat signal. A discriminated union would double the JSON size and force every consumer to update their unmarshalling code.

---

## 17. Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Property numbering aligns to the prework analysis stored in context. Every property below maps to one or more acceptance criteria in `requirements.md`.

### Property 1: Condition-dispatch completeness

*For any* rule `r` whose `when` clause contains exactly one of the five recognized condition keys and a valid argument shape, `_rule_matches(r, change, verification)` returns the value of the reference matcher for that key: `any` glob-hit against `change.changed_files` for `any_path_matches`; its negation for `no_path_matches`; literal-string equality for `change.blast_radius.score` and `verification.tests.status`; and `operator.{eq,gt,lt}(count_new_findings, N)` for `verification.static.new_errors`.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**

### Property 2: First-match-terminal wins

*For any* `Policy` and any evidence `(change, verification)`, if at least one rule with `then ∈ {"allow","block","require_human"}` matches, then `evaluate_policy(policy, change, verification).decision` equals the `then` value of the first such rule in `policy.rules` declaration order, and `matched_rule` equals that rule's `name`.

**Validates: Requirements 3.1**

### Property 3: Warn accumulation preserves declaration order

*For any* `Policy` and any evidence, `Verdict.warnings` equals the list `[r.reason or synthesized(r.name) for r in policy.rules if r.then == "warn" and _rule_matches(r, change, verification)]` — in the same order the rules appear in `policy.rules`. A warn-only match sets no terminal decision.

**Validates: Requirements 3.3**

### Property 4: Empty-when unconditional match

*For any* rule `r` with `r.when == {}` and any evidence `(change, verification)`, `_rule_matches(r, change, verification)` returns `True`; when such a rule is the first terminal rule in `policy.rules`, its `then` decides the verdict.

**Validates: Requirements 1.7**

### Property 5: Unknown condition key raises `RuleMatchError`

*For any* rule `r` whose `when` clause contains a key outside the five recognized names (`any_path_matches`, `no_path_matches`, `change.blast_radius.score`, `verification.tests.status`, `verification.static.new_errors`), evaluating `_rule_matches(r, change, verification)` raises `RuleMatchError` (a `PolicyEvaluationError` subclass) whose message names the offending key and rule; no other exception type escapes the function.

**Validates: Requirements 7.1**

### Property 6: Default-policy round-trip equivalence

*For any* invocation of `default_policy()`, the returned `Policy` equals `Policy.model_validate(yaml.safe_load(importlib.resources.files("trikon.policy").joinpath("default_policy.yaml").read_text()))`; further, `load_policy(repo, Path("does_not_exist.yaml"))` returns a `Policy` byte-identical (under `model_dump()`) to that returned by `default_policy()`.

**Validates: Requirements 2.2, 2.5, 5.4**

### Property 7: Audit-log append-only closure (static)

*For any* Python source file under `trikon/audit_log/**`, no string literal contains the SQL verbs `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `TRUNCATE` (case-insensitive, matched as whole SQL tokens); and `trikon.audit_log.__init__.__all__` names only `ensure_audit_tables` and `record_verdict`. Enforced by an AST scan test at CI time.

**Validates: Requirements 4.3**

### Property 8: Audit row lands on every SDK return path (dynamic)

*For any* invocation of `sdk.verify(repo, base, head)` that returns a `Verdict` (happy path or fail-closed), executing `SELECT COUNT(*) FROM audit_log WHERE audit_id = ?` with the returned Verdict's `audit_id` yields `1`; further, `Verdict.model_validate_json(SELECT verdict_json FROM audit_log WHERE audit_id = ?)` reproduces a `Verdict` whose `model_dump()` equals the returned Verdict's `model_dump()`.

**Validates: Requirements 4.1, 4.2, 7.3**

### Property 9: Fail-closed at the SDK boundary — never `allow`

*For any* `TrikonError` subclass **except** `AuditLogError` raised at any point during `sdk.verify`, the returned `Verdict` has `decision == "require_human"`, `evidence.change == EMPTY_IMPACT_SET`, and `evidence.verification == EMPTY_VERIFICATION`; `decision == "allow"` is never emitted on any error path. `AuditLogError` re-raises rather than fail-closing (Requirement 4.5).

**Validates: Requirements 7.1, 7.2, 7.4**

### Property 10: Exit-code semantics parametrized by decision and output format

*For any* decision `d ∈ {"allow", "block", "require_human"}` emitted by `sdk.verify` and any `--output` value in `{"markdown", "json"}`, the process exit code of `trikon verify` equals `{"allow": 0, "block": 1, "require_human": 2}[d]`; the mapping is independent of `--output`. `trikon debug verify` always exits `0` regardless of decision.

**Validates: Requirements 5.2, 5.3, 5.5**

### Property 11: Markdown-section completeness

*For any* well-formed `Verdict`, `format_markdown(verdict)` returns a `str` that contains, in order: the header line (icon + `matched_rule` display + reason), the "Focus your review on" section (up to 5 file paths in list order), the "Impact" section (four counts + blast-radius bucket + numeric score), the "Verification" section (pass/fail/skip counts + up to 5 failing node IDs with `failure_summary`), a "Warnings" section iff `verdict.warnings` is non-empty (every warning verbatim in list order), and a footer containing `verdict.audit_id` and `verdict.schema_version`. No import of any template engine (`jinja2`, `mako`, `chevron`, `string.Template`) and no I/O call (`open`, `Path.read_text`, `Path.write_text`, `socket`, `httpx`, `requests`) appears in the formatter's source.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6**

### Property 12: `schema_version == 2` on every emitted Verdict

*For any* `Verdict` returned by `sdk.verify` (happy path or fail-closed) or produced by `evaluate_policy` directly, `verdict.schema_version == 2`; further, `Verdict.model_validate_json(verdict.model_dump_json()).schema_version == 2`, so the JSON serialization carries the version signal.

**Validates: Glossary (schema_version 1→2), Requirements 3, 4.2**

---

## 18. Open questions for Phase 4+ (out of scope)

Written down here so we do not have to remember them. None block starting Phase 3.

1. **Hash-chained audit log.** `EXECUTION_PLAN.md §Phase 3` explicitly punts on this to v0.2. Each row would carry a `prev_hash TEXT NOT NULL` column linking to the previous row's SHA-256 of `(audit_id || created_at || decision || verdict_json)`. Ships alongside a `trikon audit verify` command that walks the chain. Deferred.
2. **Retention policy on `audit_log`.** No pruning in Phase 3; the table grows monotonically. A `trikon audit prune --older-than 90d` command would need policy-aware safeguards (never prune `block` rows without operator confirmation). Deferred to Phase 4.
3. **Actor and time-of-day condition keys.** The Phase-0 DSL docstring lists `actor.agent_id` and `time_of_day` as example keys. Requirement 1 does not include them; adding them is a v0.2 DSL extension gated on `Policy.version = 2` and a migration path.
4. **Policy diff explainer.** When a policy is updated, `trikon policy diff <old.yaml> <new.yaml>` would surface which rules changed and which verdicts flip on a sample corpus. Deferred to Phase 4.
5. **Per-condition `RuleResult` trace.** Today `RuleResult.matched` is a single boolean per rule. A future extension records which specific condition inside `when` fired (or failed to fire) so audit reviewers can see "rule matched because `any_path_matches` hit `src/auth/token.py`, but `verification.tests.status` was `passed`". Deferred to Phase 4.
6. **Remote policy sources.** `load_policy` reads local files only. `s3://…` and `https://…` URLs, signed with the deploying team's key, would enable centrally-managed policy for multi-repo orgs. Deferred to Phase 5.
