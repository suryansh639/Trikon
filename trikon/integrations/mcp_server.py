"""MCP server exposing Trikon as a tool any agent can call.

Tool contract:

    name: trikon_verify
    input:
        repo_path   (str, required)
        base_sha    (str, optional)
        head_sha    (str, optional)
        diff        (str, optional; alternative to base/head)
        policy_path (str, default '.trikon/policy.yaml')
        no_sandbox  (bool, default False; opt-in host-local backend, no isolation)
    output:
        A serialized :class:`~trikon.evidence.report.Verdict`.

Agents invoke this tool BEFORE committing / merging / deploying. When the tool
returns ``decision != "allow"``, the agent should either surface the verdict
to its human operator (``require_human``) or stop entirely (``block``).

The implementation is a thin adapter around :func:`trikon.sdk.verify` — this
module contains no verification logic. The SDK already fail-closes internally
so a ``TrikonError`` escaping to this layer is highly unusual, but the
``AuditLogError`` path (see ``trikon.sdk`` module docstring) can still
propagate. Both classes are caught at the tool boundary and translated into
a JSON ``{"error": ...}`` payload rather than re-raised past the MCP tool
handler — a raise here would kill the stdio transport and cost the agent its
session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from trikon import sdk
from trikon.exceptions import TrikonError

TOOL_NAME = "trikon_verify"

TOOL_DESCRIPTION = (
    "Verify a proposed code change by computing its blast radius, running "
    "impacted tests and static checks in an isolated sandbox, and returning "
    "a machine-readable verdict (allow / block / require_human). Call this "
    "tool before committing, merging, or deploying agent-generated code."
)


TOOL_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "repo_path": {
            "type": "string",
            "description": "Absolute path to the git repository on disk.",
        },
        "base_sha": {
            "type": "string",
            "description": "Base commit SHA. Pair with head_sha.",
        },
        "head_sha": {
            "type": "string",
            "description": "Head commit SHA. Pair with base_sha.",
        },
        "diff": {
            "type": "string",
            "description": "Unified diff. Alternative to base_sha + head_sha.",
        },
        "policy_path": {
            "type": "string",
            "description": "Path to policy YAML, relative to repo_path or absolute.",
            "default": ".trikon/policy.yaml",
        },
        "no_sandbox": {
            "type": "boolean",
            "description": (
                "Opt in to the host-local subprocess backend (dev-only; "
                "no isolation). Docker is used by default."
            ),
            "default": False,
        },
    },
    "required": ["repo_path"],
}


def handle_verify_change(arguments: dict[str, object]) -> dict[str, object]:
    """Handle a single MCP ``trikon_verify`` tool call.

    Wraps :func:`trikon.sdk.verify` and returns the serialized
    :class:`~trikon.evidence.report.Verdict` (via ``model_dump(mode="json")``).

    Bad input never raises across the MCP boundary — the function returns an
    ``{"error": "..."}`` payload so the agent can read a structured failure
    and recover. Exceptions raised by the SDK (rare — the SDK already
    fail-closes internally, but ``AuditLogError`` can still escape) are
    captured the same way.
    """
    # ------------------------------------------------------------------
    # Required argument: repo_path.
    # ------------------------------------------------------------------
    repo_path_raw = arguments.get("repo_path")
    if repo_path_raw is None:
        return {"error": "repo_path is required"}
    if not isinstance(repo_path_raw, str):
        return {"error": "repo_path must be a string"}

    # ------------------------------------------------------------------
    # Optional string arguments — each may be omitted, but if present
    # must be a string. Anything else is a structured error, not a
    # crash across the JSON-RPC boundary.
    # ------------------------------------------------------------------
    base_sha_raw = arguments.get("base_sha")
    if base_sha_raw is not None and not isinstance(base_sha_raw, str):
        return {"error": "base_sha must be a string"}

    head_sha_raw = arguments.get("head_sha")
    if head_sha_raw is not None and not isinstance(head_sha_raw, str):
        return {"error": "head_sha must be a string"}

    diff_raw = arguments.get("diff")
    if diff_raw is not None and not isinstance(diff_raw, str):
        return {"error": "diff must be a string"}

    policy_path_raw = arguments.get("policy_path", ".trikon/policy.yaml")
    if not isinstance(policy_path_raw, str):
        return {"error": "policy_path must be a string"}

    no_sandbox_raw = arguments.get("no_sandbox", False)
    if not isinstance(no_sandbox_raw, bool):
        return {"error": "no_sandbox must be a boolean"}

    # ------------------------------------------------------------------
    # Dispatch. The SDK fail-closes internally into a require_human
    # Verdict for every TrikonError subclass; the only exception that
    # can still escape is AuditLogError, which is deliberately caught
    # here — the MCP tool handler must never raise into the transport.
    # ------------------------------------------------------------------
    try:
        verdict = sdk.verify(
            repo_path=Path(repo_path_raw),
            base_sha=base_sha_raw,
            head_sha=head_sha_raw,
            diff=diff_raw,
            policy_path=Path(policy_path_raw),
            no_sandbox=no_sandbox_raw,
        )
    except TrikonError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive; AuditLogError etc.
        return {"error": f"{type(exc).__name__}: {exc}"}

    return verdict.model_dump(mode="json")


async def _list_tools_handler(
    ctx: ServerRequestContext[None, None],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    """Return the single ``trikon_verify`` tool descriptor."""
    del ctx, params
    tool = types.Tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        inputSchema=TOOL_INPUT_SCHEMA,
    )
    return types.ListToolsResult(tools=[tool])


async def _call_tool_handler(
    ctx: ServerRequestContext[None, None],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    """Dispatch a ``tools/call`` request to :func:`handle_verify_change`."""
    del ctx
    if params.name != TOOL_NAME:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"Unknown tool: {params.name!r}",
                )
            ],
            isError=True,
        )
    arguments = params.arguments or {}
    result = handle_verify_change(arguments)

    # A payload of the shape {"error": "..."} — and only that — is a
    # tool-level error (bad input, SDK propagation). Anything else is a
    # serialized Verdict and must be reported with isError=False so the
    # agent parses it as a real result.
    is_error = list(result.keys()) == ["error"]

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result))],
        structuredContent=result,
        isError=is_error,
    )


def run_server(*, transport: str = "stdio", host: str = "127.0.0.1", port: int = 4801) -> None:
    """Start the MCP server.

    Args:
        transport: ``"stdio"`` (default, used by Claude Code / Cursor / Kiro),
            ``"http"``, or ``"sse"``. Only ``stdio`` is wired up in this build
            — ``http`` / ``sse`` print a clear error and exit rather than
            invent an untested transport.
        host: Bind host for HTTP transports. Reserved (unused for stdio).
        port: Bind port for HTTP transports. Reserved (unused for stdio).
    """
    from trikon import __version__

    if transport not in ("stdio", "http", "sse"):
        raise ValueError(f"Unknown transport: {transport!r}")

    if transport in ("http", "sse"):
        # The MCP SDK's streamable-http / sse transports exist but the
        # end-to-end handshake against Kiro / Cursor / Claude Code is
        # not yet part of Trikon's regression matrix. Rather than ship
        # an untested transport, we exit with a clear operator-facing
        # error. Wave 8+ will wire this up once we have a test rig.
        print(
            f"Error: {transport!r} transport is not enabled in this build. Use --transport stdio.",
            file=sys.stderr,
        )
        # host/port are reserved for future HTTP/SSE wiring — reference
        # them to keep mypy --strict / ruff --unused-argument happy.
        del host, port
        raise SystemExit(2)

    # stdio: log to stderr only. stdout is the JSON-RPC channel; any
    # text written to it corrupts the protocol frame.
    print(
        f"trikon MCP server starting on stdio (trikon {__version__})",
        file=sys.stderr,
        flush=True,
    )

    server: Server[None] = Server(
        "trikon",
        version=__version__,
        description="Verification layer for autonomous AI coding agents.",
        on_list_tools=_list_tools_handler,
        on_call_tool=_call_tool_handler,
    )

    asyncio.run(_run_stdio(server))


async def _run_stdio(server: Server[None]) -> None:
    """Bind the server to stdin/stdout and pump until the client closes."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
