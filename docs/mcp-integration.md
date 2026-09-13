# Trikon MCP integration

Trikon ships an MCP (Model Context Protocol) server that exposes a single tool, `trikon_verify`, which any MCP-capable AI agent can call before committing, merging, or deploying agent-generated code. Register the server in your editor's MCP config once, and the agent can request a verdict on any git diff.

## Prerequisites

1. Trikon installed: `pip install trikon` (Python 3.11+).
2. Docker running (default sandbox backend), OR pass `no_sandbox: true` in the tool arguments to run without isolation on the host.

To confirm Trikon is discoverable:

    trikon --version    # 0.3.0 or newer
    trikon doctor       # reports Python / git / Docker / policy readiness
    trikon mcp serve --help

## The `trikon_verify` tool

Input schema:

- `repo_path` (string, required) — absolute path to the git repository on disk.
- `base_sha` (string, optional) — base commit SHA. Pair with `head_sha`.
- `head_sha` (string, optional) — head commit SHA. Pair with `base_sha`.
- `diff` (string, optional) — unified diff. Alternative to `base_sha` + `head_sha`.
- `policy_path` (string, default `.trikon/policy.yaml`) — policy YAML path.
- `no_sandbox` (boolean, default `false`) — opt in to the host-local subprocess backend (dev-only; no isolation).

Output: a serialized `Verdict` (see docs/policy.md and the `Verdict` Pydantic model). The `decision` field is one of `allow` / `block` / `require_human` / `warn`; the `reason` explains why; `evidence.change` carries the ImpactSet; `evidence.verification` carries the test + static-check + plugin results.

## Editor configurations

### Claude Code

Edit your Claude Code MCP config (usually `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "trikon": {
      "command": "trikon",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

Restart Claude Code. The `trikon_verify` tool should appear in the agent's tool list.

### Cursor

Cursor reads `.cursor/mcp.json` from your workspace root. Create or edit that file:

```json
{
  "mcpServers": {
    "trikon": {
      "command": "trikon",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

Reload Cursor's window. The tool becomes available to Cursor's agent.

### Kiro

Kiro reads its MCP config from `~/.kiro/settings/mcp.json`. Add Trikon under `mcpServers`:

```json
{
  "mcpServers": {
    "trikon": {
      "command": "trikon",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

## Windows notes

If `trikon` is not on your PATH after `pip install trikon`, use the interpreter form instead:

```json
{
  "mcpServers": {
    "trikon": {
      "command": "python",
      "args": ["-m", "trikon.cli", "mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

Or specify the venv's absolute path:

```json
{
  "mcpServers": {
    "trikon": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "trikon.cli", "mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

## Testing the connection

After registering, ask the agent to `verify the current change with trikon` in a git repo. The agent will call `trikon_verify` with `repo_path` set to the workspace and receive a serialized Verdict.

To verify from the command line without an editor:

    trikon mcp serve --transport stdio

Then send JSON-RPC over stdin. See the `_TESTING_LOCALLY` example in `tests/integration/test_mcp_smoke.py` (once we ship it) for the exact byte sequence.

## Security notes

- The `no_sandbox: true` flag is intended for local dev machines only. It runs verification on the host with no isolation. Never enable it against untrusted code.
- The stdio transport is inherently local — the MCP server communicates with the agent over the process's stdin/stdout, so it does not open a network port. HTTP / SSE transports are not enabled in v0.3.0.
