# Trikon — Architecture

> Working name. Last updated: 2026-09-12.
> Status: **design doc**. No implementation yet. This document is the source of truth for what we're building and, importantly, what we're *not* building.

---

## 1. Product summary

**Trikon is the verification layer for autonomous AI coding agents.** It runs between the agent and production and produces a machine-readable decision about whether a proposed change is safe to merge or deploy — backed by executed evidence, not LLM opinion.

**The core primitive**:

```
agent proposes change
        │
        ▼
guardrail.verify(change, policy) ──▶ Verdict {
                                        decision: ALLOW | BLOCK | REQUIRE_HUMAN,
                                        evidence: { change_intel, verification, policy_results },
                                        audit_id: uuid,
                                     }
```

**The buyer** is the platform / security / governance team defining "what AI is allowed to do unattended in our repo."

**The budget line** is governance and risk — not code review productivity. This matters because it means we are not competing with CodeRabbit / Greptile / Qodo for the same dollars.

---

## 2. What Trikon is not (non-goals)

Being explicit about non-goals keeps the design small and defensible.

1. **Not an AI PR reviewer.** We do not write natural-language comments on diffs. If a reviewer needs an LLM opinion, they use CodeRabbit. We produce evidence and a decision.
2. **Not an agent.** We do not generate code, propose fixes, or reason about intent. We verify what someone else's agent produced.
3. **Not a general test framework.** We select and run existing tests. We do not synthesize new tests in v0.
4. **Not a static analyzer.** Static checks are one of several signals we run. We are not competing with SonarQube, Semgrep, or Snyk on rule breadth.
5. **Not a codebase intelligence platform.** We index only what we need to compute blast radius. We are not competing with Sourcegraph or Greptile on repo-wide search.
6. **Not multi-language in v0.** Python only. Adding a language means adding an AST indexer + a test-runner adapter + coverage-map handling. That's 3–6 months per language. We ship Python first, prove the wedge, then expand deliberately.

---

## 3. System diagram

```
┌────────────────────────────────────────────────────────────────────┐
│  Trigger sources (any of these can call verify())                  │
│  ─────────────────────────────────────────────                     │
│  • Claude Code / Codex / Cursor Bugbot (via MCP tool)              │
│  • Unideploy autopilot (pre-deploy gate)                           │
│  • Nightly refactor / dependency-upgrade bot (via CLI)             │
│  • GitHub App on PR opened (via webhook)                           │
│  • GitHub Actions in CI (via `trikon/verify` action)           │
└────────────────────────────┬───────────────────────────────────────┘
                             │  ChangeSet { base_sha, head_sha, diff }
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Trikon Core                                                    │
│                                                                     │
│  ┌──────────────────────┐   ┌──────────────────────┐               │
│  │ Change Intelligence  │──▶│ Verification Runner  │               │
│  │                      │   │                      │               │
│  │ diff → AST → dep     │   │ • impacted pytest    │               │
│  │ graph → blast radius │   │ • ruff / mypy        │               │
│  │ → impacted symbols   │   │ • custom checks      │               │
│  │ → impacted tests     │   │                      │               │
│  │                      │   │ runs inside          │               │
│  │ (cached SQLite)      │   │ isolated sandbox     │               │
│  └──────────┬───────────┘   └──────────┬───────────┘               │
│             │                          │                            │
│             │        ┌─────────────────┘                            │
│             ▼        ▼                                              │
│  ┌────────────────────────────┐                                    │
│  │ Policy Engine              │                                    │
│  │                            │                                    │
│  │ YAML DSL:                  │                                    │
│  │ • path-based rules         │                                    │
│  │ • blast-radius thresholds  │                                    │
│  │ • test-status conditions   │                                    │
│  │ • time-of-day / actor      │                                    │
│  └──────────┬─────────────────┘                                    │
│             ▼                                                       │
│  ┌────────────────────────────┐                                    │
│  │ Verdict + Evidence Report  │                                    │
│  │                            │                                    │
│  │ ALLOW | BLOCK | REQUIRE_   │                                    │
│  │ HUMAN + structured JSON    │                                    │
│  └──────────┬─────────────────┘                                    │
└─────────────┼──────────────────────────────────────────────────────┘
              │
              ▼
┌────────────────────────────────────────────────────────────────────┐
│  Consumers of the verdict                                          │
│  ─────────────────────────────                                     │
│  • GitHub check-run status (green/red)                             │
│  • PR comment (markdown-formatted evidence)                        │
│  • MCP tool return value (agent proceeds or asks human)            │
│  • Autopilot decision gate (deploy or abort)                       │
│  • Audit log entry (hash-chained, compliance-exportable)           │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. Component detail

### 4.1 Change Intelligence (`trikon/change_intel/`)

Job: convert a git diff into a set of **impacted symbols**, **impacted modules**, and **impacted tests**, plus a **blast-radius score**.

| File | Responsibility |
| --- | --- |
| `diff_parser.py` | Parse `git diff` into a structured `ChangeSet` (files, hunks, byte ranges) |
| `ast_indexer.py` | Parse each Python file into a symbol table: functions, classes, methods, module-level assignments, with byte-offset ranges. Uses `libcst` for round-trippable AST |
| `symbol_resolver.py` | Resolve cross-file symbol references. Uses `jedi` for imports and call-site resolution |
| `dep_graph.py` | Build a directed graph: nodes = symbols, edges = "uses". Persist as SQLite for incremental updates. Cache key = file SHA-256 |
| `blast_radius.py` | Given changed symbols, compute N-hop transitive dependents. Emit `ImpactSet { direct, indirect, tests, external_touch_points }` |

**Design principle**: the dep graph is **incremental**. We hash each file and only re-index changed files. This matters because on a 500K-LOC repo, full re-indexing takes minutes; incremental updates take milliseconds. This is the same pattern the OSS `impact-radius` package uses.

**Blast-radius score** (v0 heuristic):

```
score = w1 * len(impacted_modules)
      + w2 * len(impacted_public_apis)      # anything not underscore-prefixed
      + w3 * len(impacted_test_files)
      + w4 * cross_package_boundary_hops
      + w5 * touches_sensitive_paths        # from policy config
```

Weights are configurable per repo. Score buckets → LOW / MEDIUM / HIGH. The score is one input to the policy engine, not a decision by itself.

### 4.2 Verification Runner (`trikon/verify/`)

Job: execute the checks that the impact set says are relevant, inside an isolated environment, and return structured results.

| File | Responsibility |
| --- | --- |
| `test_selector.py` | Given impacted symbols, find pytest tests that exercise them. Uses coverage-map lookup (built once per test suite run, cached). Falls back to filename heuristics for tests without coverage data |
| `runner.py` | Orchestrates the run: test execution + static checks + custom check plugins. Collects results into a `VerificationReport` |
| `static_checks.py` | Runs `ruff`, `mypy`, and any repo-defined linters. Parses output into structured findings |
| `sandbox.py` | Isolation layer. Two backends: `local_docker` (v0.1) and `unideploy_warden` (v0.2) |
| `plugins.py` | Plugin interface for custom checks (e.g., "no direct SQL", "no secrets in strings"). Repo-defined |

**Test selection heuristic**:

1. Build a coverage map: `symbol → set(test_ids)` from a prior full run (cached in `.trikon/coverage.db`).
2. For each impacted symbol, look up covering tests.
3. Union all covering tests → candidate set.
4. Add explicit test files that were themselves modified.
5. Add tests in the same module as changed files (safety net for uncovered code paths).
6. Run pytest with `--pyargs <candidates>`.

**Coverage map staleness**: the map ages with each merge. Trikon rebuilds it opportunistically on green-verdict runs (when a full test run happens anyway) and warns when the map is >7 days behind HEAD.

**Sandbox** (v0.1): plain Docker container built from a pinned base image with repo mounted read-only, results written to a tmpfs mount, network disabled by default. Test process runs as a non-root uid.

**Sandbox** (v0.2): shell out to Unideploy's `warden` binary. See §6 for the reuse contract.

### 4.3 Policy Engine (`trikon/policy/`)

Job: given the impact set + verification report, produce a `Verdict`.

| File | Responsibility |
| --- | --- |
| `dsl.py` | Pydantic models for the YAML policy schema |
| `loader.py` | Load and validate `.trikon/policy.yaml` from the repo |
| `evaluator.py` | Apply rules in order, first-match wins for terminal decisions; accumulate warnings from non-terminal rules |

**Policy DSL** (example):

```yaml
version: 1

# Rules are evaluated top-to-bottom. First rule that matches AND emits a
# terminal decision (allow / block / require_human) wins.

rules:
  - name: "sensitive path always requires human"
    when:
      any_path_matches: ["payments/**", "auth/**", "billing/**"]
    then: require_human
    reason: "Change touches a sensitive subsystem."

  - name: "test failure blocks"
    when:
      verification.tests.status: failed
    then: block
    reason: "Impacted tests did not pass."

  - name: "static analysis regression blocks"
    when:
      verification.static.new_errors: { gt: 0 }
    then: block

  - name: "large blast radius requires human"
    when:
      change.blast_radius.score: HIGH
    then: require_human

  - name: "green + small blast auto-allow"
    when:
      verification.tests.status: passed
      verification.static.new_errors: { eq: 0 }
      change.blast_radius.score: LOW
    then: allow

  # default fall-through
  - name: "default"
    then: require_human
    reason: "No rule matched; defaulting to human review."
```

**Design principle**: policy is **explicit and versioned in the repo**, not a hidden SaaS setting. This is how you get platform teams to trust it.

### 4.4 Evidence Report (`trikon/evidence/`)

Job: package the verdict + all its inputs into structured outputs.

| File | Responsibility |
| --- | --- |
| `report.py` | Pydantic `Verdict`, `Evidence`, `ImpactSet`, `VerificationReport` models |
| `formatters/json.py` | Machine-readable JSON (canonical) |
| `formatters/markdown.py` | GitHub PR comment / check-run summary |
| `formatters/otel.py` | OpenTelemetry span export for observability |
| `formatters/sarif.py` | SARIF for security-tool interop (v0.3) |

**The `Verdict` shape** (canonical):

```python
class Verdict(BaseModel):
    decision: Literal["allow", "block", "require_human"]
    reason: str
    matched_rule: str | None
    evidence: Evidence
    audit_id: UUID
    created_at: datetime
    schema_version: int = 1

class Evidence(BaseModel):
    change: ImpactSet
    verification: VerificationReport
    policy_results: list[RuleResult]

class ImpactSet(BaseModel):
    changed_files: list[str]
    changed_symbols: list[SymbolRef]
    impacted_modules: list[str]
    impacted_public_apis: list[SymbolRef]
    impacted_tests: list[str]
    blast_radius_score: Literal["LOW", "MEDIUM", "HIGH"]
    blast_radius_numeric: float

class VerificationReport(BaseModel):
    tests: TestReport
    static: StaticReport
    plugins: list[PluginResult]
    sandbox_ms: int
    total_ms: int
```

### 4.5 Integrations (`trikon/integrations/`)

Job: expose `verify()` through the interfaces our users actually reach for.

| File | Interface | Consumer |
| --- | --- | --- |
| `cli.py` | `trikon verify <path>` | Local dev, CI scripts |
| `mcp_server.py` | MCP tool `verify_change` | Any MCP-aware agent (Claude Code, Cursor, custom) |
| `github_app.py` | GitHub App webhook handler | Hosted GitHub App |
| `gha_action.py` | GitHub Actions entrypoint | Users who prefer CI-based install |
| `sdk.py` | Python API `trikon.verify(change_set, policy)` | Embedding in autopilot / custom pipelines |

**Design principle**: the CLI, MCP tool, GitHub App, and CI action are all thin wrappers around the same `sdk.verify()`. There is one code path.

**MCP tool surface** (draft):

```json
{
  "name": "verify_change",
  "description": "Verify a proposed code change by computing its blast radius, running impacted tests and static checks in isolation, and returning a machine-readable verdict.",
  "input_schema": {
    "type": "object",
    "properties": {
      "repo_path": { "type": "string" },
      "base_sha":  { "type": "string" },
      "head_sha":  { "type": "string" },
      "diff":      { "type": "string", "description": "Unified diff; alternative to base/head" },
      "policy_path": { "type": "string", "default": ".trikon/policy.yaml" }
    },
    "required": ["repo_path"]
  }
}
```

### 4.6 Backend (`trikon/backend/`, deferred to v0.2)

The MVP runs entirely locally. Once we have a design partner, we add a thin control plane:

- **Audit service**: hash-chained verdict log. Same pattern as Unideploy's `unideploy-audit` lambda.
- **Policy service**: optional hosted policy management for orgs that want central control.
- **Metrics service**: aggregate agentability trends over time (verdict distribution, review-hours-saved, top blockers).
- **License service**: reuse Unideploy's `mcp-licenses` DynamoDB schema; different price tier.

Stack: AWS Lambda (Python 3.12) + DynamoDB + API Gateway + Cognito. Deployed via CDK. This is a copy-and-retarget of Unideploy's infra layout — see §6.

---

## 5. Data flow — worked example

Scenario: a nightly Claude Code bot opens PR #482 on `acme/payment-service`, titled "Add retry-with-backoff for failed Stripe charges."

```
1. GitHub sends `pull_request.opened` webhook to Trikon GitHub App.

2. App downloads:
   - base_sha (main) and head_sha (PR branch) from GitHub API
   - .trikon/policy.yaml from the repo

3. Change Intelligence:
   - diff_parser.py    → 3 files changed, 47 lines added
                          [payments/retry.py, payments/gateway.py, tests/test_retry.py]
   - ast_indexer.py    → indexes the 3 files (incremental)
   - symbol_resolver.py → resolves imports and callers
   - dep_graph.py      → looks up transitive dependents of changed symbols
   - blast_radius.py   → impacted:
                          modules: [payments, orders, api]
                          public_apis: [payments.gateway.charge]
                          tests:   [tests/test_retry.py, tests/test_gateway.py,
                                    tests/test_worker.py, tests/api/test_payments.py]
                          score:   MEDIUM (5 modules, 1 public API, 12 tests)

4. Verification Runner:
   - Spin up sandbox container from pinned image.
   - Mount repo at HEAD, read-only.
   - Run pytest on the 12 impacted tests.
     → 10 passed, 2 failed:
        - tests/test_worker.py::test_worker_timeout    FAIL
          AssertionError: worker exceeded 2s deadline (retry loop sleeps 3s)
        - tests/api/test_payments.py::test_endpoint    FAIL
          Timeout waiting for response
   - Run ruff, mypy on changed files → clean.
   - Report:
        tests: { passed: 10, failed: 2, skipped: 0, ms: 8_400 }
        static: { new_errors: 0 }

5. Policy Engine:
   - "sensitive path always requires human": matches (payments/**).
   - Terminal → decision = require_human.
   - Reason: "Change touches a sensitive subsystem."
   - But: the tests-failed rule would also have blocked. We record both in
     `policy_results` for transparency; the *matched_rule* is the first
     terminal one.

6. Evidence Report → Verdict:
   {
     "decision": "require_human",
     "reason": "Change touches a sensitive subsystem.",
     "matched_rule": "sensitive path always requires human",
     "evidence": { ...full ImpactSet, VerificationReport, all rule results... },
     "audit_id": "01JAG...",
     "created_at": "2026-09-13T03:14:07Z"
   }

7. Consumers:
   - GitHub check-run: status = "action_required", summary = markdown report.
   - PR comment: "Trikon requires human review. 2 impacted tests failed
     (worker_timeout, endpoint_timeout). Focus review on payments/retry.py.
     Estimated review time: ~8 min."
   - Audit log: hash-chained entry stored.
```

**What the reviewer sees**: not 47 lines of AI-generated diff to eyeball. Instead, "here are the exact 2 failing tests and the 1 file that most likely caused them." A 30-minute manual trace becomes a 5-minute focused review.

**What the *agent* sees** (if called via MCP): a JSON verdict saying `require_human`. The agent stops. It does not merge. It does not retry blindly.

---

## 6. Reuse from Unideploy

The strategic recommendation was: **new product, but reuse the engine**. Here is the concrete map.

| Unideploy component | Trikon use | Integration mechanism |
| --- | --- | --- |
| `libs/server/src/sandbox.rs` (warden) | Isolated test execution | v0.1: not used (plain Docker). v0.2: shell out to `unideploy warden run --image=... --mount=<repo>:<ro> -- <cmd>` |
| `.stakpak/warden/ca` mTLS bootstrap | Sandbox trust bootstrap in v0.2 | Reuse the same CA-generation and trust flow |
| `infra/` CDK skeleton (Lambda + DynamoDB + API Gateway + Cognito) | Copied and retargeted for `trikon-api` | Copy `infra/stacks/api_stack.py` structure, replace resource names |
| `unideploy-authorizer` lambda | Trikon auth pattern | Same custom-authorizer pattern; new `trikon-api-keys` DynamoDB table |
| `unideploy-audit` lambda | Trikon audit log | Same hash-chained schema; new `trikon-verdicts` table |
| `mcp-licenses` DynamoDB table | Subscription tracking | Same table shape, new plan tier `trikon-team` |
| `libs/mcp/server` | MCP tool server pattern | We build a separate MCP server, but follow the same tool-registration pattern |
| Autopilot | **First internal customer**. Autopilot calls Trikon before applying any change to prod infra | Autopilot invokes `trikon verify` via subprocess or MCP tool |
| Semantic compression | Not reused; different data shape | N/A |

**Explicit non-reuse**: the Rust CLI shell, ratatui TUI, session/checkpoint pipeline, LLM provider abstraction (`libs/ai`), and Anthropic-specific message conversion are all Unideploy-internal and not touched.

**Cross-repo boundary**: Trikon is a Python codebase in its own git repo. It does not import from Unideploy source. Where it needs Unideploy functionality (warden), it shells out. This keeps the two products cleanly separable — critical for pricing, packaging, and potential later divestiture.

---

## 7. Tech stack + rationale

| Layer | Choice | Why |
| --- | --- | --- |
| Engine language | **Python 3.11+** | Matches target ecosystem (Python is the top language for both AI codegen and modern test suites). Matches pytest as oracle. Rich AST tooling (`libcst`, `jedi`). Faster to ship than Rust |
| AST library | `libcst` (concrete syntax) + `ast` stdlib (fast pass) | libcst preserves formatting for potential future patch generation; stdlib `ast` is faster for pure indexing |
| Cross-file resolution | `jedi` | Battle-tested, understands imports, virtualenvs, stubs |
| Dep-graph store | SQLite | Zero-ops, incremental-friendly, portable. Same pattern as `impact-radius` |
| Coverage | `coverage.py` in branch mode | Standard, well-supported, JSON output |
| CLI | `typer` | Type-annotated, generates `--help` from signatures |
| Config / DSL | `pydantic` v2 + `pyyaml` | Runtime validation with good error messages |
| Sandbox v0.1 | Plain Docker via subprocess | Simple; the sandbox is not our differentiator |
| Sandbox v0.2 | Unideploy warden | Better isolation, reuses existing tech |
| Backend | AWS Lambda + DynamoDB + API Gateway (Python 3.12) via CDK | Mirrors Unideploy; skips a whole architecture debate |
| Auth | API keys with SHA-256 hashing + Cognito for SSO | Copy Unideploy's `unideploy-authorizer` pattern |

**Why Python not Rust for the engine**: Rust would be faster on very large repos, but the differentiator is not raw indexing speed — it's the *product*: policy DSL, verdict quality, agent-facing UX. Python halves time-to-first-customer. If perf becomes the bottleneck on 5M-LOC monorepos, we rewrite the hot path (dep graph resolution) in Rust as a native extension. Not before.

---

## 8. MVP scope (v0.1)

The v0.1 target is small enough to ship in **~8 weeks** with a design partner.

**In scope**:

- Python 3.11+ codebases only.
- Git diff parsing via `gitpython`.
- AST indexing (`libcst`) + incremental dep graph in SQLite.
- Blast-radius computation with default weights.
- Test selection using pre-built coverage map.
- pytest execution in a plain Docker container.
- `ruff` and `mypy` static checks.
- YAML policy DSL with 5 rule types (path match, blast-radius threshold, test status, static-check delta, default).
- Structured `Verdict` output (JSON + Markdown).
- CLI: `trikon verify` (against local repo).
- MCP server exposing `verify_change` as a single tool.
- Local SQLite audit log.

**Deliberately out of scope for v0.1** (deferred to later versions):

| Feature | Deferred to |
| --- | --- |
| GitHub App + hosted webhooks | v0.2 |
| AWS backend / hosted control plane | v0.2 |
| Unideploy warden sandbox integration | v0.2 |
| GitHub Actions action | v0.2 |
| Web dashboard | v0.3 |
| Other languages (Java, TypeScript, Go) | v1 |
| Test synthesis for gaps in coverage | v1 |
| Behavioral verification (traffic replay against a shadow env) | v1+ |
| SOC2 audit-export bundle | v1 |
| SARIF output for security-tool interop | v0.3 |
| Automated remediation (proposing fixes) | v2+ |

**MVP definition-of-done**: one design partner running Trikon against their real repo, with a real AI agent generating PRs, and their platform lead saying: *"we would notice if you turned this off."*

---

## 9. Deferred but planned

- **v0.2** — Hosted GitHub App, hosted backend on AWS, warden sandbox, GitHub Actions integration.
- **v0.3** — Web dashboard (verdict trends, top blockers, review-hours-saved), SARIF export, first non-Python language (Node/TS is the obvious pick).
- **v1** — Test-gap synthesis (generate a test for the exact behavior the change modified but which no existing test covers), Java support, enterprise SSO + RBAC.
- **v2** — Behavioral verification via shadow-environment traffic replay. This is where the moat compounds because it requires integrating with the customer's staging/prod. Highest lock-in.

---

## 10. Open design questions

These are the decisions we deliberately have not made yet. Any of them could change the shape of the MVP.

1. **Test-selection accuracy for fixture-heavy pytest suites.** Fixtures create dependencies that plain AST analysis misses. Do we build our own fixture-aware selector, or fork `pytest-impacted` and credit upstream?
2. **Coverage map staleness policy.** How stale is too stale? 7 days? 100 commits? Do we hard-block on stale maps or soft-warn?
3. **Handling AI-generated tests.** If the agent's PR includes new tests, do we trust them as verification signal? Or run them separately and treat them as claims, not evidence? *Recommendation: treat AI-written tests as claims. Run them, but weight them lower in the verdict than pre-existing tests.*
4. **Policy DSL vs Rego.** YAML is easy for a design partner. Rego is more expressive and battle-tested (OPA). Ship YAML in v0.1, evaluate migration to Rego if we hit expressiveness ceilings.
5. **Multi-tenancy in the audit log.** For the hosted backend, is the audit log per-repo, per-org, or per-verdict-signer? *Recommendation: per-org, with repo and signer as attributes.*
6. **Pricing model.** Per-verdict? Per-repo? Per-seat? Per-org unlimited with an SLA? To be shaped by the design-partner conversations.
7. **Naming.** "Trikon" is the working name. Public-launch name TBD.

---

## 11. File layout

```
trikon/
├── README.md                     # product overview
├── ARCHITECTURE.md               # this file
├── pyproject.toml                # Python package definition
├── .gitignore
├── trikon/                   # the Python package
│   ├── __init__.py
│   ├── cli.py                    # `trikon verify` entrypoint
│   ├── sdk.py                    # `trikon.verify()` public API
│   ├── change_intel/
│   │   ├── __init__.py
│   │   ├── diff_parser.py
│   │   ├── ast_indexer.py
│   │   ├── symbol_resolver.py
│   │   ├── dep_graph.py
│   │   └── blast_radius.py
│   ├── verify/
│   │   ├── __init__.py
│   │   ├── test_selector.py
│   │   ├── runner.py
│   │   ├── static_checks.py
│   │   ├── sandbox.py
│   │   └── plugins.py
│   ├── policy/
│   │   ├── __init__.py
│   │   ├── dsl.py
│   │   ├── loader.py
│   │   └── evaluator.py
│   ├── evidence/
│   │   ├── __init__.py
│   │   ├── report.py
│   │   └── formatters/
│   │       ├── __init__.py
│   │       ├── json.py
│   │       └── markdown.py
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── mcp_server.py
│   │   ├── github_app.py         # v0.2
│   │   └── gha_action.py         # v0.2
│   └── backend/                  # v0.2 — hosted control plane client
│       ├── __init__.py
│       └── api_client.py
├── examples/
│   ├── policies/
│   │   └── default.yaml
│   └── sample_repo/              # tiny Python project used in demos
├── infra/                        # v0.2 — AWS CDK, mirrors Unideploy
│   ├── app.py
│   └── stacks/
│       ├── __init__.py
│       └── api_stack.py
├── docs/
│   ├── quickstart.md
│   ├── policy_dsl.md
│   ├── worked_example.md
│   └── mcp_integration.md
└── tests/
    ├── unit/
    └── integration/
```

---

## 12. TL;DR for a future contributor

- Trikon sits between an autonomous AI agent and production. It answers one question: **is this specific AI-generated change safe to merge or deploy?**
- The answer is a machine-readable `Verdict` — `ALLOW`, `BLOCK`, or `REQUIRE_HUMAN` — backed by executed evidence (impacted tests, static checks, blast radius), not by an LLM's opinion.
- The wedge is **unattended agent execution**, not PR review. Buyer is platform / governance, not eng manager.
- v0.1 is Python + pytest + local Docker + CLI + MCP tool. 8 weeks with a design partner. No hosted backend yet.
- Reuse from Unideploy is deliberate but bounded: warden sandbox (v0.2), AWS backend patterns (v0.2), MCP server pattern. No shared source tree.
- The first customer of Trikon is **Unideploy autopilot** — it verifies its own actions before touching prod. This gives us a working reference deployment before any external design partner sees it.
