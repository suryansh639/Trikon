"""Integration surfaces — how external systems reach `trikon.verify()`.

Each integration in this package is a thin adapter. They MUST NOT contain
verification logic. They translate their transport (MCP call, GitHub webhook,
GHA env vars) into a call to `trikon.sdk.verify()` and translate the
returned `Verdict` back into the transport's response format.

v0.1: mcp_server.py, cli (via `trikon.cli`).
v0.2: github_app.py, gha_action.py.
"""
