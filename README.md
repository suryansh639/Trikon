# Trikon

**The verification layer for autonomous AI coding agents.**

[![status](https://img.shields.io/badge/status-pre--alpha-orange)]() [![license](https://img.shields.io/badge/license-Apache--2.0-blue)]() [![python](https://img.shields.io/badge/python-3.11%2B-blue)]()

Autonomous AI agents (Claude Code, Codex, Cursor, Unideploy autopilot, custom nightly bots) are increasingly committing, merging, and deploying code without a human in the loop. Trikon sits between the agent and production. Before a change is merged or deployed, Trikon:

1. Analyzes the change's **blast radius** using an AST-based dependency graph.
2. Executes the **targeted subset** of tests, static checks, and policy rules that the change actually affects.
3. Produces a machine-readable **Verdict** — `ALLOW`, `BLOCK`, or `REQUIRE_HUMAN` — with structured evidence.
4. Records the verdict in a **hash-chained audit log** for compliance.

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

Pre-alpha. Phase 1 (change intelligence) is implemented end-to-end; the verification runner (Phase 2), policy engine (Phase 3), and hosted control plane come later. See [`EXECUTION_PLAN.md`](./EXECUTION_PLAN.md) for the phase-by-phase map.

## Phase 1: what ships today

- **Change-intelligence pipeline** — `parse_diff` → AST indexer → symbol resolver → dep graph → `compute_impact`, producing a structured `ImpactSet` (impacted files, symbols, modules, public APIs, tests, blast-radius score). Frozen contract in [`.kiro/specs/change-intelligence/design.md`](./.kiro/specs/change-intelligence/design.md).
- **CLI: `trikon debug impact`** — runs the whole pipeline against a git repo and prints the `ImpactSet` as JSON. The first end-to-end runnable checkpoint.
- **Never-fail-open guarantee** — every exception raised inside `trikon/change_intel/**` is a subclass of `ChangeIntelError`. The SDK boundary catches the base class and returns a `Verdict` with `decision="require_human"` and `blast_radius_score="HIGH"`. `allow` is never emitted on an error path. See [`docs/change_intel.md`](./docs/change_intel.md#never-fail-open).

Try it locally:

```bash
pip install -e ".[dev]"
trikon debug impact --repo examples/sample_repo --base HEAD --head HEAD
```

Phase 2 (targeted test execution + sandbox) and Phase 3 (policy engine + `trikon verify`) are next. Until they land, `sdk.verify()` returns `require_human` because there is no verification evidence to grade yet.

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
| [`docs-site/`](./docs-site) | Mintlify docs site (end-user facing). |

## Immediate next steps

1. **Engine language: Python 3.11+** (locked, see `ARCHITECTURE.md` §7).
2. **MVP scope**: Python + pytest + Docker sandbox + CLI + MCP tool. No hosted backend in v0.1. See `EXECUTION_PLAN.md`.
3. **First design partner**: needed. One Python codebase with an active AI agent generating unattended PRs. This is the only truly blocking decision.

## License

TBD — likely Apache-2.0 for the core engine, commercial for the hosted backend.
