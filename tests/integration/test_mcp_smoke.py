"""End-to-end smoke test for the ``trikon mcp serve`` stdio transport.

Wave 8. This test spawns the real MCP server as a subprocess (via the MCP
Python SDK's own :func:`mcp.stdio_client`), performs the full ``initialize``
handshake, calls ``tools/list`` to confirm the ``trikon_verify`` tool is
advertised with the expected input schema, then invokes
``trikon_verify`` against a real git repository — a shallow clone of
`click <https://github.com/pallets/click>`_ — and asserts that the returned
:class:`~trikon.evidence.report.Verdict` deserializes into the schema every
downstream consumer (audit log, PR comment, dashboard) depends on.

Why click? It is small, has no repo-local Trikon policy or state cache, and
its git history is stable enough that ``git log --oneline -n 5`` reliably
yields a five-commit window we can use as a base/head pair. The test does
not care what ``decision`` the pipeline reaches — the click repository does
not carry a Trikon policy or a coverage map, so any of ``allow``,
``block``, ``require_human``, or ``warn`` is a valid outcome — it only
asserts that the wire shape is well-formed and every field parses.

The test is marked ``pytest.mark.integration`` so the default ``pytest``
invocation (whose ``addopts`` filter deselects the ``integration`` marker)
skips it, and CI's dedicated ``integration`` job picks it up.

Cross-platform: everything uses :data:`sys.executable` and :mod:`pathlib`
so it runs identically on Windows and Ubuntu (CI is ``ubuntu-latest``).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Marker + budgets
# ---------------------------------------------------------------------------
#
# The default ``pytest`` invocation in ``pyproject.toml`` runs with
# ``-m 'not perf and not integration'`` (see ``[tool.pytest.ini_options]``),
# so this marker is what keeps the MCP smoke test out of the fast test loop
# and inside CI's dedicated ``integration`` job.

pytestmark = pytest.mark.integration

# Overall wall-clock budget for the MCP round-trip. The MCP server is spawned
# from ``sys.executable``, imports the Trikon package tree, and then runs the
# full change-intel + verify + policy pipeline against the click checkout.
# 180 s covers cold-start Python import overhead on CI + the pipeline itself
# with a generous margin; if any step hangs longer than that, the test aborts
# rather than blocking the CI job.
TEST_TIMEOUT_SECONDS = 180.0

# ---------------------------------------------------------------------------
# Session-scoped fixture: shallow click clone + base/head SHA extraction.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def click_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    """Clone ``pallets/click`` shallow into a session-scoped tmp dir.

    Uses ``tmp_path_factory`` so pytest owns the teardown — no manual
    ``shutil.rmtree`` in a finalizer, no leaked directories on Windows
    when a subprocess still has a handle open at the point of cleanup.

    Returns:
        A ``(repo_path, base_sha, head_sha)`` tuple where ``base_sha`` is
        the oldest of the last five commits (``git log -n 5`` last line)
        and ``head_sha`` is the newest (first line). The two SHAs are
        adjacent enough that the change set is small — a handful of files
        at most — but not necessarily identical from run to run because
        click's history keeps advancing. That is intentional: the test
        asserts on the wire shape of the Verdict, not on which files
        happened to change in this particular five-commit window.
    """
    clone_root = tmp_path_factory.mktemp("mcp_smoke_click")
    clone_dir = clone_root / "click"

    # Shallow clone (depth 100) is a compromise: it is small enough to be
    # fast on CI's cold cache but deep enough that the last-five-commits
    # log always returns five entries, even if the newest commit happens
    # to be a merge or revert that Trikon's diff parser would otherwise
    # find uninteresting.
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "100",
            "https://github.com/pallets/click.git",
            str(clone_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # ``git log --oneline -n 5`` yields exactly five ``<sha> <subject>`` lines,
    # newest first. Take the first line's SHA as head (newest) and the last
    # line's SHA as base (oldest) — the range ``base..head`` is then a walk
    # of four commits, which is small enough that the pipeline can traverse
    # it inside the 3-minute budget.
    log_result = subprocess.run(
        ["git", "log", "--oneline", "-n", "5"],
        cwd=str(clone_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    log_lines = [line for line in log_result.stdout.splitlines() if line.strip()]
    assert len(log_lines) >= 2, (
        f"expected at least 2 commits in the shallow clone, got {len(log_lines)}: "
        f"{log_result.stdout!r}"
    )
    head_sha = log_lines[0].split(maxsplit=1)[0]
    base_sha = log_lines[-1].split(maxsplit=1)[0]

    return clone_dir, base_sha, head_sha


# ---------------------------------------------------------------------------
# The MCP round-trip itself, async.
# ---------------------------------------------------------------------------


async def _run_mcp_round_trip(repo_path: Path, base_sha: str, head_sha: str) -> dict[str, object]:
    """Spawn the server, initialize, list tools, call ``trikon_verify``.

    Returns the ``structured_content`` payload of the tool call (the same
    dict shape as :meth:`Verdict.model_dump(mode="json")`). The two
    ``async with`` blocks handle clean shutdown of both the stdio streams
    and the child process — if either raises, the context managers still
    run their ``__aexit__`` and the subprocess is terminated cleanly.
    """
    # Spawn the server as ``<sys.executable> -m trikon.cli mcp serve
    # --transport stdio``. ``sys.executable`` is the interpreter running
    # this test — the same venv that has ``trikon`` installed — so the
    # child process inherits the correct package resolution without
    # relying on ``python`` / ``python3`` being on PATH.
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trikon.cli", "mcp", "serve", "--transport", "stdio"],
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        # 1. Handshake — negotiates protocol version, exchanges
        #    capability declarations. If the server crashes on import
        #    or the CLI errors out, this is where the failure surfaces.
        await session.initialize()

        # 2. list_tools — the server advertises exactly one tool.
        list_result = await session.list_tools()
        tool_names = [tool.name for tool in list_result.tools]
        assert tool_names == ["trikon_verify"], (
            f"expected exactly one tool named 'trikon_verify', got {tool_names!r}"
        )

        # Validate the tool descriptor's shape while we have it.
        tool = list_result.tools[0]
        assert isinstance(tool.description, str) and tool.description, (
            "trikon_verify must ship a non-empty description"
        )
        schema = tool.input_schema
        assert isinstance(schema, dict), f"input_schema must be a dict, got {type(schema)!r}"
        assert schema.get("type") == "object", f"input_schema.type must be 'object': {schema!r}"
        properties = schema.get("properties")
        assert isinstance(properties, dict), (
            f"input_schema.properties must be a dict, got {type(properties)!r}"
        )
        for expected in ("repo_path", "base_sha", "head_sha", "diff", "no_sandbox"):
            assert expected in properties, (
                f"input_schema.properties missing key {expected!r}; got {sorted(properties)!r}"
            )
        required = schema.get("required")
        assert isinstance(required, list) and "repo_path" in required, (
            f"input_schema.required must include 'repo_path': {required!r}"
        )

        # 3. call_tool — the real work. ``no_sandbox=True`` skips
        #    Docker (CI runners don't have it configured for
        #    privileged use) and runs verification on the host.
        call_result = await session.call_tool(
            "trikon_verify",
            {
                "repo_path": str(repo_path),
                "base_sha": base_sha,
                "head_sha": head_sha,
                "no_sandbox": True,
            },
        )

        # The server returns both structured_content (typed dict) and a
        # text-content payload holding the same JSON. Prefer the
        # structured payload; fall back to parsing the text content
        # only if the SDK ever drops the typed side.
        payload = call_result.structured_content
        if payload is None:
            assert call_result.content, "empty CallToolResult content"
            first = call_result.content[0]
            # TextContent.text is the JSON string
            text = getattr(first, "text", None)
            assert isinstance(text, str), f"expected TextContent with .text, got {type(first)!r}"
            parsed = json.loads(text)
            assert isinstance(parsed, dict), (
                f"tool payload must be a JSON object, got {type(parsed)!r}"
            )
            payload = parsed

        # The server wraps handler errors in ``{"error": "..."}`` and
        # sets ``is_error=True``. A well-formed Verdict has neither.
        assert not call_result.is_error, (
            f"tool call reported is_error=True with payload={payload!r}"
        )
        assert "error" not in payload, f"tool call returned an error payload: {payload!r}"

        return payload


# ---------------------------------------------------------------------------
# The synchronous test entry-point.
# ---------------------------------------------------------------------------


def test_mcp_stdio_round_trip_against_click(
    click_repo: tuple[Path, str, str],
) -> None:
    """Full ``initialize`` → ``list_tools`` → ``call_tool`` handshake.

    Wraps :func:`_run_mcp_round_trip` in :func:`asyncio.run` because
    ``pytest-asyncio`` is deliberately not in ``[project.optional-dependencies]
    dev``. :func:`asyncio.wait_for` enforces the wall-clock budget so a
    hung MCP server (bad transport handshake, deadlock, orphaned child
    process) fails fast instead of blocking the CI job.
    """
    repo_path, base_sha, head_sha = click_repo

    verdict_dict = asyncio.run(
        asyncio.wait_for(
            _run_mcp_round_trip(repo_path, base_sha, head_sha),
            timeout=TEST_TIMEOUT_SECONDS,
        )
    )

    # ------------------------------------------------------------------
    # Wire-shape assertions on the returned Verdict.
    # ------------------------------------------------------------------
    #
    # The tool payload is the same dict that ``Verdict.model_dump(mode="json")``
    # produces. Every field of the schema is checked here — this test is the
    # canary for accidental schema breakage on the MCP path (design.md §3.6).

    # decision is one of the four documented values.
    decision = verdict_dict.get("decision")
    assert decision in {"allow", "block", "require_human", "warn"}, (
        f"unexpected decision {decision!r}"
    )

    # reason is a non-empty string.
    reason = verdict_dict.get("reason")
    assert isinstance(reason, str) and reason, f"reason must be a non-empty string: {reason!r}"

    # matched_rule is a string or None.
    matched_rule = verdict_dict.get("matched_rule", "sentinel")
    assert matched_rule is None or isinstance(matched_rule, str), (
        f"matched_rule must be a string or None, got {type(matched_rule)!r}"
    )

    # evidence.change.changed_files is a list (contents depend on which
    # commits click happened to have at test time; only the shape matters).
    evidence = verdict_dict.get("evidence")
    assert isinstance(evidence, dict), f"evidence must be a dict, got {type(evidence)!r}"
    change = evidence.get("change")
    assert isinstance(change, dict), f"evidence.change must be a dict, got {type(change)!r}"
    changed_files = change.get("changed_files")
    assert isinstance(changed_files, list), (
        f"evidence.change.changed_files must be a list, got {type(changed_files)!r}"
    )

    # evidence.verification is present (its inner shape is validated by
    # the change_intel end-to-end test — this test only asserts existence).
    verification = evidence.get("verification")
    assert isinstance(verification, dict), (
        f"evidence.verification must be a dict, got {type(verification)!r}"
    )

    # evidence.policy_results is a list.
    policy_results = evidence.get("policy_results")
    assert isinstance(policy_results, list), (
        f"evidence.policy_results must be a list, got {type(policy_results)!r}"
    )

    # audit_id parses as a valid UUID.
    audit_id_raw = verdict_dict.get("audit_id")
    assert isinstance(audit_id_raw, str), f"audit_id must be a string, got {type(audit_id_raw)!r}"
    # UUID(...) raises ValueError on malformed input, which pytest surfaces
    # as a plain test failure — no need for an explicit try/except.
    UUID(audit_id_raw)

    # created_at is a valid ISO 8601 timestamp.
    created_at_raw = verdict_dict.get("created_at")
    assert isinstance(created_at_raw, str), (
        f"created_at must be a string, got {type(created_at_raw)!r}"
    )
    datetime.fromisoformat(created_at_raw)

    # schema_version == 3 (bumped by the engine fail-safe work, which added
    # the TestReport strategy/collection fields and verification.imports).
    assert verdict_dict.get("schema_version") == 3, (
        f"schema_version must be 3, got {verdict_dict.get('schema_version')!r}"
    )

    # warnings is a list (empty is fine — see EMPTY_VERIFICATION invariant).
    warnings = verdict_dict.get("warnings")
    assert isinstance(warnings, list), f"warnings must be a list, got {type(warnings)!r}"
