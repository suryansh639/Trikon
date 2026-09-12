"""MCP server exposing Trikon as a tool any agent can call.

Tool contract:

    name: verify_change
    input:
        repo_path (str, required)
        base_sha  (str, optional)
        head_sha  (str, optional)
        diff      (str, optional; alternative to base/head)
        policy_path (str, default '.trikon/policy.yaml')
    output:
        A serialized Verdict (see trikon.evidence.report.Verdict).

Agents invoke this tool BEFORE committing / merging / deploying. When the tool
returns `decision != "allow"`, the agent should either surface the verdict to
its human operator (`require_human`) or stop entirely (`block`).
"""

from __future__ import annotations

from typing import Any


TOOL_DESCRIPTION = (
    "Verify a proposed code change by computing its blast radius, running "
    "impacted tests and static checks in isolation, and returning a "
    "machine-readable verdict (allow / block / require_human)."
)


TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo_path": {"type": "string"},
        "base_sha": {"type": "string"},
        "head_sha": {"type": "string"},
        "diff": {
            "type": "string",
            "description": "Unified diff. Alternative to base_sha + head_sha.",
        },
        "policy_path": {"type": "string", "default": ".trikon/policy.yaml"},
    },
    "required": ["repo_path"],
}


def handle_verify_change(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle a single MCP `verify_change` tool call.

    Wraps trikon.sdk.verify and returns the serialized Verdict.
    """
    # TODO: import trikon.sdk.verify at call time, run it, serialize the Verdict.
    raise NotImplementedError


def run_server(host: str = "127.0.0.1", port: int = 4801) -> None:
    """Start the MCP server. Wire up when the `mcp` Python package is a dependency."""
    # TODO: register the tool and start the MCP server loop.
    raise NotImplementedError
