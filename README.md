# Trikon

**The verification layer for autonomous AI coding agents.**

[![status](https://img.shields.io/badge/status-pre--alpha-orange)]() [![license](https://img.shields.io/badge/license-Apache--2.0-blue)]() [![python](https://img.shields.io/badge/python-3.11%2B-blue)]()

Autonomous AI agents (Claude Code, Codex, Cursor, Unideploy autopilot, custom nightly bots) are increasingly committing, merging, and deploying code without a human in the loop. Trikon sits between the agent and production. Before a change is merged or deployed, Trikon:

1. Analyzes the change's **blast radius** using an AST-based dependency graph.
2. Executes the **targeted subset** of tests, static checks, and policy rules that the change actually affects, and falls back to the full test suite when that subset can't be trusted.
3. Produces a machine-readable **Verdict** — `ALLOW`, `BLOCK`, or `REQUIRE_HUMAN` — with structured evidence.
4. Records the verdict in a **hash-chained audit log** for compliance.

## Install

    pip install trikon

Trikon requires Python 3.11+ and (optionally) Docker Desktop for the sandboxed verification backend. Verify your environment with `trikon doctor`.

## GitHub Action

Wire Trikon into any PR workflow with two lines. No app to install, no backend to deploy:

```yaml
- uses: suryansh639/Trikon/actions/verify@v0.5.0
  with:
    base: ${{ github.event.pull_request.base.sha }}
    head: ${{ github.event.pull_request.head.sha }}
```

The action pulls the pinned Docker image [`suryansh639/trikon:0.5.0`](https://hub.docker.com/r/suryansh639/trikon), runs `trikon verify` against the diff, emits a machine-readable Verdict (`decision`, `verdict-json`), and fails the job on `block`. See [`actions/verify/README.md`](./actions/verify/README.md) for inputs, outputs, and strict-gating examples.

## Why this exists

The bottleneck in software engineering has moved from writing code to verifying it. Human review does not scale with agent output. Human review is also the wrong tool for unattended agents that run at 3 a.m.

The market is full of tools that ask an LLM whether a diff *looks* correct. Trikon executes the code and produces evidence.

## Positioning

| | AI PR reviewers (CodeRabbit, Greptile, Qodo) | **Trikon** |
| --- | --- | --- |
| **Mechanism** | LLM opinion on a diff | Executed verification (tests, static checks, blast radius) |
| **Assumes** | A human reviewer will read the comments | An autonomous agent needs a machine-readable decision |
| **Output** | Free-text comments | Structured `Verdict` + hash-chained audit record |
| **Buyer** | Eng manager / individual dev | Platform, security, governance team |
| **Budget line** | Code review productivity | Governance / risk / compliance |

## Relationship to Unideploy

Trikon is a **separate product**, not a Unideploy feature. It reuses Unideploy's engine as internal infrastructure:

- Unideploy's **warden sandbox** provides isolated test execution.
- Unideploy's **AWS backend pattern** (Lambda + DynamoDB + API Gateway + Cognito) is copied for the Trikon control plane.
- Unideploy's **BYOM licensing** flow is mirrored for Trikon subscriptions.
- Unideploy's **autopilot** is the first customer of Trikon (verifies its own actions before touching prod).

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design.

## Status

Pre-alpha. Phase 1 (change intelligence), Phase 2 (verification runner) and Phase 3 (policy engine) are delivered end-to-end; the hosted control plane comes later. `sdk.verify()` and `trikon verify` return policy-driven verdicts (`allow`, `block` or `require_human`), and a non-overridable Safety_Floor runs after the policy. See [`EXECUTION_PLAN.md`](./EXECUTION_PLAN.md) for the phase-by-phase map.

## What ships today

- **Change-intelligence pipeline** — `parse_diff` → AST indexer → symbol resolver → dep graph → `compute_impact`, producing a structured `ImpactSet` (impacted files, symbols, modules, public APIs, tests, blast-radius score). Frozen contract in [`.kiro/specs/change-intelligence/design.md`](./.kiro/specs/change-intelligence/design.md).
- **CLI: `trikon debug impact`** — runs the whole pipeline against a git repo and prints the `ImpactSet` as JSON. The first end-to-end runnable checkpoint.
- **Never-fail-open guarantee** — every exception raised inside `trikon/change_intel/**` is a subclass of `ChangeIntelError`. The SDK boundary catches the base class and returns a `Verdict` with `decision="require_human"`; if change intelligence did not finish, the evidence carries the empty `ImpactSet` with `blast_radius_score="HIGH"`. `allow` is never emitted on an error path. See [`docs/change_intel.md`](./docs/change_intel.md#never-fail-open).
- **Verification (Phase 2)** — the same pipeline now *executes* the `ImpactSet`. A collection pass over the whole test suite runs first. The runner then runs only the selected tests when a fresh coverage map backs them, and the full suite otherwise, recording why in `strategy_reasons`, so a Python change never passes on zero tests. Static analysis (`ruff` + `mypy`) and custom `.trikon/checks/*.py` plugins run in the same pinned, network-isolated Docker sandbox (`suryansh639/trikon`, the image the GitHub Action uses). Every raise site under `trikon/verify/**` is a `VerificationRunnerError` subclass, so sandbox failures still land on `require_human` — the never-fail-open guarantee extends to Phase 2. Full walkthrough in [`docs/verification.md`](./docs/verification.md). One-line demo:

  ```bash
  trikon debug verify --repo examples/sample_repo --base HEAD~1 --head HEAD
  ```
- **Policy engine (Phase 3)** — `sdk.verify()` grades the evidence against `.trikon/policy.yaml` (or the packaged 8-rule default) and returns the first matching rule's decision with its `matched_rule` and `reason`. `trikon verify` maps the decision to exit code `0` / `1` / `2`, and every verdict is written to an append-only `audit_log` table before it is returned. Full walkthrough in [`docs/policy.md`](./docs/policy.md).
- **Broken-import detection and the Safety_Floor** — an import checker flags static imports of modules or names the change removed (`evidence.verification.imports`). After the policy, a Safety_Floor that no policy can disable turns `allow` into `block` when any import is broken, and into `require_human` when a Python change executed no tests or has incomplete test or import evidence. A floored verdict names `safety_floor.broken_imports` or `safety_floor.insufficient_evidence` as its `matched_rule`.

Try it locally:

```bash
pip install -e ".[dev]"
trikon debug impact --repo examples/sample_repo --base HEAD --head HEAD
```

For the Phase 2 verification runner, the warm path (CoverageMap-hit, sub-15 s verdicts) requires a one-time coverage-map build per repo before the first `trikon debug verify` invocation. Without a fresh map, a Python change runs the full test suite:

```bash
# 1. Build the coverage map (one-time setup per repo)
trikon coverage build --repo .

# 2. Run verification on a change
trikon debug verify --repo . --base HEAD~1 --head HEAD
```

See [`docs/verification.md`](./docs/verification.md) for the full verification pipeline (sandbox, test selection, static checks, plugins).

To gate CI on the verdict, run `trikon verify --repo . --base <sha> --head <sha>`. It exits `0` on `allow`, `1` on `block` and `2` on `require_human`. `trikon debug verify` prints the same policy-driven Verdict for a human to read and always exits `0`. See [`docs/policy.md`](./docs/policy.md) for the default rules and the Safety_Floor.

## MCP integration

Trikon exposes a `trikon_verify` MCP tool that any MCP-capable AI agent (Claude Code, Cursor, Kiro) can call:

    trikon mcp serve --transport stdio

See [docs/mcp-integration.md](docs/mcp-integration.md) for per-editor configs.

## Documentation

| Doc | Purpose |
| --- | --- |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Full system design. Start here. |
| [`EXECUTION_PLAN.md`](./EXECUTION_PLAN.md) | 12-week plan from empty repo to first paying customer. |
| [`PRICING.md`](./PRICING.md) | Tiers, unit economics, licensing decision. |
| [`OPERATIONS.md`](./OPERATIONS.md) | Production topology, SLA, runbook. |
| [`docs/quickstart.md`](./docs/quickstart.md) | Get running locally. |
| [`docs/worked_example.md`](./docs/worked_example.md) | End-to-end scenario with numbers. |
| [`docs/change_intel.md`](./docs/change_intel.md) | Engineer's walkthrough of the Phase 1 change-intelligence pipeline. |
| [`docs/verification.md`](./docs/verification.md) | Engineer's walkthrough of the Phase 2 verification runner (sandbox, test selection, static checks, plugins). |
| [`docs-site/`](./docs-site) | Mintlify docs site (end-user facing). |

## Immediate next steps

1. **Engine language: Python 3.11+** (locked, see `ARCHITECTURE.md` §7).
2. **MVP scope**: Python + pytest + Docker sandbox + CLI + MCP tool. No hosted backend in v0.1. See `EXECUTION_PLAN.md`.
3. **First design partner**: needed. One Python codebase with an active AI agent generating unattended PRs. This is the only truly blocking decision.

## License

TBD — likely Apache-2.0 for the core engine, commercial for the hosted backend.
