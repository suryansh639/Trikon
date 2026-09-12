# MCP Integration

Trikon exposes itself as an MCP tool so any MCP-aware agent (Claude Code, Cursor, Codex, custom agents) can call it before merging or deploying. This is the most important integration for the v0.1 wedge — "unattended agent execution" — because the agent needs a machine-readable signal, not a human-readable PR comment.

## Tool contract

```
name:        verify_change
description: Verify a proposed code change by computing its blast radius,
             running impacted tests and static checks in isolation, and
             returning a machine-readable verdict.
input:
    repo_path   (string, required)  — absolute path to the target repo
    base_sha    (string, optional)  — git SHA of the base commit
    head_sha    (string, optional)  — git SHA of the head commit
    diff        (string, optional)  — unified diff (alternative to SHAs)
    policy_path (string, default: ".trikon/policy.yaml")
output:
    A JSON-serialized `Verdict` (see trikon/evidence/report.py).
```

At least one of `(base_sha + head_sha)` or `diff` must be provided.

## Recommended agent behavior

```
1. Agent produces a proposed change.
2. Agent calls verify_change(repo_path, base_sha, head_sha).
3. Agent reads the returned Verdict:
     • decision == "allow"          → proceed (merge / deploy / push).
     • decision == "block"          → STOP. Do not retry blindly. Surface the
                                        failed tests to the human operator.
     • decision == "require_human"  → open a PR; do not merge. Include the
                                        markdown-formatted verdict in the PR body.
```

## Running the server

```bash
trikon mcp serve --port 4801
```

Point your MCP client at `http://127.0.0.1:4801` (or a TCP socket, depending on the client).

## Wiring into Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "trikon": {
      "command": "trikon",
      "args": ["mcp", "serve", "--stdio"]
    }
  }
}
```

Then in Claude Code, before merging, invoke:

```
verify this change with trikon against main
```

## Wiring into Unideploy autopilot

Trikon is the natural pre-deploy gate for Unideploy autopilot. Two integration options:

**Option A: subprocess (v0.1)**

Add to `~/.Unideploy/autopilot.toml`:

```toml
[schedules.nightly-refactor]
cron   = "0 3 * * *"
prompt = "Apply pending refactor PRs."
pre_apply_check = { command = "trikon verify --repo ${WORK_DIR} --base ${BASE_SHA} --head ${HEAD_SHA} -o json", fail_on = ["block", "require_human"] }
```

**Option B: MCP tool (v0.2)**

Register Trikon as an MCP server in Unideploy's `~/.Unideploy/config.toml` and the agent will call `verify_change` as a tool through the same MCP infrastructure Unideploy already uses.

## What the agent should NOT do

- **Do not** call the LLM again to interpret the Verdict when `decision != "allow"`. The verdict is structured for a reason.
- **Do not** silently retry on `block`. That is a policy circumvention.
- **Do not** treat `require_human` as a soft warning. It is a firm "open a PR; do not merge."
- **Do not** cache a stale verdict across changes. Every commit needs a fresh call.

## Error responses

If Trikon cannot produce a verdict at all (sandbox failed to start, repo missing, policy invalid), the tool returns:

```json
{
  "error": {
    "code": "sandbox_start_failed" | "repo_not_found" | "policy_invalid" | "internal",
    "message": "human-readable description",
    "recoverable": true
  }
}
```

**Agents must treat any error as `require_human`.** Never assume `allow` on error.
