# Trikon Quickstart

> Pre-alpha. The commands below describe the intended v0.1 UX. They will work once implementation lands.

Trikon runs between your AI coding agent and production. Before the agent merges or deploys, Trikon computes the change's blast radius, runs the tests that actually matter, and emits a machine-readable verdict.

## Install

```bash
pip install trikon        # not yet published — install from source for now:
# pip install -e path/to/trikon
```

## Initialize a repo

Inside the target Python repo:

```bash
trikon init
```

Creates `.trikon/policy.yaml` from the default template and (once v0.2 lands) sets up the coverage-map database.

## Verify a change locally

```bash
trikon verify --base main --head HEAD
```

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | `allow` — safe to merge/deploy |
| `1` | `block` — do not merge |
| `2` | `require_human` — needs human review |

Add `-o json` for machine output.

## Wire it into an AI agent (via MCP)

Start the MCP server:

```bash
trikon mcp serve --port 4801
```

Any MCP-aware agent (Claude Code, Cursor, Codex, a custom agent) can now call:

```json
{
  "tool": "verify_change",
  "arguments": {
    "repo_path": "/workspace/acme-api",
    "base_sha": "abc123",
    "head_sha": "def456"
  }
}
```

The response is a full `Verdict` JSON. Configure your agent to *stop* on `block` and *escalate* on `require_human`.

## Wire it into CI (GitHub Actions, v0.2)

```yaml
- uses: trikon/verify@v1
  with:
    policy: .trikon/policy.yaml
    fail-on: block,require_human   # set the check-run to red for either
```

## Wire it into Unideploy autopilot

In `~/.Unideploy/autopilot.toml`:

```toml
[schedules.nightly-refactor.pre_apply_check]
command = "trikon verify --base $BASE_SHA --head $HEAD_SHA -o json"
fail_on = ["block", "require_human"]
```

Autopilot will not apply an unattended change unless Trikon emits `allow`.

## Where Trikon writes to disk

| Path | Purpose |
| --- | --- |
| `.trikon/policy.yaml` | Policy DSL (committed to the repo) |
| `.trikon/checks/*.py` | Custom check plugins (committed to the repo) |
| `.trikon/state.db` | SQLite: symbol index + dep graph + coverage map. `.gitignore`-worthy |
| `.trikon/audit.log` | Hash-chained local audit log (v0.1) |

## What Trikon does NOT do

- Write PR comments in natural language. Use CodeRabbit if you need that.
- Generate patches or fix things. Trikon verifies; it doesn't propose.
- Depend on any single LLM. It doesn't call an LLM at all in v0.1.
