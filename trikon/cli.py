"""Trikon command-line interface.

Entrypoint: ``trikon <command>``

Commands (Phase 2):
    verify           Run the Trikon pipeline against a git change set and print a verdict.
    init             Scaffold a default .trikon/policy.yaml in the current repo.
    version          Print the installed Trikon version.
    debug impact     Run change-intelligence against a diff and print the ImpactSet JSON.
    debug verify     Run the full change-intel + verification pipeline and print a verdict.
    coverage build   Build the coverage map used for test selection.
    mcp serve        Run the Trikon MCP server so AI agents can call ``trikon_verify``.

The ``verify`` command is still a stub; ``init``, ``version``, the two
``debug`` sub-commands, and ``coverage build`` are wired to real code
paths. See ``design.md §11`` for the CLI surface contract.
"""

from __future__ import annotations

import importlib.resources
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from trikon.change_intel.blast_radius import compute_impact
from trikon.change_intel.diff_parser import parse_diff
from trikon.change_intel.errors import ChangeIntelError
from trikon.evidence.formatters.markdown import format_markdown
from trikon.evidence.formatters.verify_text import format_verification_verdict
from trikon.sdk import verify as sdk_verify
from trikon.verify.coverage_builder import build_coverage_map
from trikon.verify.db import ensure_verify_tables
from trikon.verify.errors import CoverageBuildError

# Decision -> process exit code mapping for ``trikon verify``
# (``design.md §11.1``). ``warn`` is defensive: :func:`trikon.sdk.verify`
# never emits ``decision == "warn"`` at the SDK boundary, but a
# forward-compat Verdict with ``decision == "warn"`` still produces a
# defined, non-crashing exit. ``2`` matches ``require_human`` — a
# warn-only outcome still needs a human to look at it.
_EXIT_CODE_FOR: dict[str, int] = {
    "allow": 0,
    "block": 1,
    "require_human": 2,
    "warn": 2,
}

app = typer.Typer(
    name="trikon",
    help="Verification layer for autonomous AI coding agents.",
    no_args_is_help=True,
)

debug_app = typer.Typer(
    name="debug",
    help="Debug and introspection commands.",
    no_args_is_help=True,
)
app.add_typer(debug_app, name="debug")

coverage_app = typer.Typer(
    name="coverage",
    help="Manage the coverage map used for test selection.",
    no_args_is_help=True,
)
app.add_typer(coverage_app, name="coverage")

mcp_app = typer.Typer(
    name="mcp",
    help="MCP (Model Context Protocol) server for AI-agent integration.",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")


@app.command()
def verify(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
    base: str | None = typer.Option(None, "--base", help="Base commit SHA."),
    head: str | None = typer.Option(None, "--head", help="Head commit SHA."),
    diff_file: Path | None = typer.Option(
        None, "--diff-file", help="Path to a unified diff file (alternative to --base/--head)."
    ),
    policy: Path = typer.Option(
        Path(".trikon/policy.yaml"),
        "--policy",
        help="Path to the policy YAML (relative to repo or absolute).",
    ),
    output: str = typer.Option(
        "markdown",
        "--output",
        "-o",
        help="Output format: markdown | json.",
    ),
    no_sandbox: bool = typer.Option(
        False,
        "--no-sandbox",
        help="Use the host-local subprocess backend instead of Docker (opt-in, no isolation).",
    ),
) -> None:
    """Verify a proposed change and emit a verdict.

    Exit codes (per ``design.md §11.1``, Requirements 5.2 / 5.3):

    * ``0`` — ``decision == "allow"``.
    * ``1`` — ``decision == "block"``.
    * ``2`` — ``decision == "require_human"`` (also emitted on any
      ``--output`` value outside ``{"markdown", "json"}`` as a Typer-style
      usage error).

    The ``--no-sandbox`` flag switches verification from the default
    Docker-backed sandbox to a host-local subprocess runner. This is an
    opt-in escape hatch for developer machines that do not have Docker
    installed; it runs pytest / ruff / mypy / plugins on the host with
    **no isolation** (no read-only mount, no network cap, no memory /
    cpu / pids cap). A security warning banner is printed to stderr
    every time the flag is active. Never use ``--no-sandbox`` against
    a repository whose contents you do not fully trust.
    """
    if output not in ("markdown", "json"):
        typer.echo(
            f"Error: --output must be markdown|json, got {output!r}",
            err=True,
        )
        raise typer.Exit(code=2)

    if no_sandbox:
        typer.echo("=" * 78, err=True)
        typer.echo("⚠  SECURITY WARNING: --no-sandbox is active.", err=True)
        typer.echo("   Verification will run on the HOST with NO isolation. Do NOT use", err=True)
        typer.echo("   with untrusted code. This mode is for local dev only.", err=True)
        typer.echo("=" * 78, err=True)

    diff_content = diff_file.read_text(encoding="utf-8") if diff_file is not None else None
    verdict = sdk_verify(
        repo,
        base_sha=base,
        head_sha=head,
        diff=diff_content,
        policy_path=policy,
        no_sandbox=no_sandbox,
    )

    if output == "json":
        typer.echo(verdict.model_dump_json(indent=2))
    else:
        typer.echo(format_markdown(verdict))

    raise typer.Exit(code=_EXIT_CODE_FOR[verdict.decision])


@app.command()
def init(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing .trikon/policy.yaml.",
    ),
) -> None:
    """Scaffold a starter .trikon/policy.yaml in the target repo."""
    target = repo / ".trikon" / "policy.yaml"
    if target.exists() and not force:
        typer.echo(
            f"Error: {target} already exists. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(code=1)

    target.parent.mkdir(parents=True, exist_ok=True)
    resource = importlib.resources.files("trikon.policy") / "default_policy.yaml"
    try:
        content = resource.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        typer.echo(f"Error: packaged default_policy.yaml missing: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    target.write_text(content, encoding="utf-8")
    typer.echo(f"Wrote {target}")


@app.command()
def version() -> None:
    """Print the installed Trikon version."""
    from trikon import __version__

    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Report environment readiness for running Trikon.

    Runs no pipeline — this command is pure environment introspection.
    It inspects Python, git, Docker, and the packaged default policy
    YAML and reports each check on its own line. Exit codes:

    * ``0`` — every check passed.
    * ``1`` — Docker is unavailable or the packaged default policy is
      missing (both are actionable; the operator can start Docker or
      reinstall Trikon).
    * ``2`` — Python is older than 3.11 (fatal; Trikon requires 3.11+
      and cannot run at all on an older interpreter).
    """
    import subprocess as _subprocess

    from trikon import __version__ as _trikon_version
    from trikon.verify.sandbox import _resolve_docker_socket_path

    exit_code = 0

    # -----------------------------------------------------------------
    # Python.
    # -----------------------------------------------------------------
    py_version = sys.version.splitlines()[0]
    python_ok = sys.version_info >= (3, 11)
    if python_ok:
        typer.echo(f"Python:         {py_version} (OK)")
    else:
        typer.echo(f"Python:         {py_version} (FATAL — Trikon requires 3.11+)")
        # Python-too-old is a fatal environment error — dominates the
        # other exit-code branches.
        exit_code = 2

    # -----------------------------------------------------------------
    # Trikon version.
    # -----------------------------------------------------------------
    typer.echo(f"Trikon:         {_trikon_version} (installed)")

    # -----------------------------------------------------------------
    # Git.
    # -----------------------------------------------------------------
    try:
        git_result = _subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (FileNotFoundError, OSError, _subprocess.TimeoutExpired) as git_exc:
        typer.echo(f"Git:            NOT AVAILABLE ({git_exc})")
    else:
        if git_result.returncode == 0:
            typer.echo(f"Git:            {git_result.stdout.strip()} (OK)")
        else:
            typer.echo(
                f"Git:            NOT AVAILABLE (git exited "
                f"{git_result.returncode}: {git_result.stderr.strip()[:200]})"
            )

    # -----------------------------------------------------------------
    # Docker.
    # -----------------------------------------------------------------
    # ``docker.from_env().ping()`` is the canonical liveness check.
    # Every foreign exception ``docker-py`` can raise is caught below
    # so ``doctor`` never surfaces an uncaught stacktrace.
    docker_socket = _resolve_docker_socket_path()
    docker_available = False
    docker_detail = ""
    try:
        import docker as _docker  # local import so a missing docker package still lets doctor run

        client = _docker.from_env()
        client.ping()
    except Exception as docker_exc:
        # Widest-scope catch is deliberate — docker-py may raise
        # docker.errors.DockerException, requests.ConnectionError, or
        # OSError depending on why the daemon is unreachable, and
        # ``doctor`` must never surface an uncaught stacktrace.
        docker_detail = f"{docker_socket} unreachable ({type(docker_exc).__name__}: {docker_exc})"
    else:
        docker_available = True
        try:
            info = client.version()
            server_version = info.get("Version") if isinstance(info, dict) else None
            if isinstance(server_version, str) and server_version:
                docker_detail = f"Docker {server_version} via {docker_socket}"
            else:
                docker_detail = f"Docker via {docker_socket}"
        except Exception as info_exc:
            # Ping succeeded but ``version()`` did not — still call
            # this OK because the daemon is reachable.
            docker_detail = f"Docker via {docker_socket} (version() failed: {info_exc})"

    if docker_available:
        typer.echo(f"Docker:         OK ({docker_detail})")
    else:
        typer.echo(f"Docker:         NOT AVAILABLE ({docker_detail})")
        typer.echo("                Start Docker Desktop or pass --no-sandbox to trikon verify.")
        # Missing Docker is actionable, not fatal; exit 1 unless a
        # more severe Python-version failure already set exit 2.
        if exit_code < 1:
            exit_code = 1

    # -----------------------------------------------------------------
    # Packaged default policy YAML.
    # -----------------------------------------------------------------
    policy_resource = importlib.resources.files("trikon.policy") / "default_policy.yaml"
    try:
        policy_ok = policy_resource.is_file()
    except (OSError, AttributeError):
        policy_ok = False

    if policy_ok:
        try:
            policy_bytes = policy_resource.read_bytes()
            typer.echo(
                f"Policy YAML:    trikon/policy/default_policy.yaml "
                f"(found, {len(policy_bytes)} bytes)"
            )
        except OSError as policy_exc:
            typer.echo(
                f"Policy YAML:    trikon/policy/default_policy.yaml "
                f"(found, read failed: {policy_exc})"
            )
            if exit_code < 1:
                exit_code = 1
    else:
        typer.echo(
            "Policy YAML:    trikon/policy/default_policy.yaml (NOT FOUND — reinstall Trikon)"
        )
        if exit_code < 1:
            exit_code = 1

    raise typer.Exit(code=exit_code)


@debug_app.command("impact")
def debug_impact(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
    base: str | None = typer.Option(None, "--base", help="Base commit SHA."),
    head: str | None = typer.Option(None, "--head", help="Head commit SHA."),
    diff_file: Path | None = typer.Option(
        None,
        "--diff-file",
        help="Path to a unified diff file (alternative to --base/--head).",
    ),
    cache_db: Path | None = typer.Option(
        None,
        "--cache-db",
        help="Path to the SQLite change-intel cache DB (defaults to <repo>/.trikon/state.db).",
    ),
) -> None:
    """Run change-intelligence ONLY (Phase 1) and print the ImpactSet JSON.

    This diagnostic runs :func:`parse_diff` and :func:`compute_impact`
    directly — it does NOT invoke the full :func:`sdk.verify` pipeline,
    so the Phase 2 sandbox (Docker) is not required. Use this to inspect
    the change-intel output even when the verification runner is
    unavailable.

    Exit codes:
      * 0 — success (the ``ImpactSet`` was emitted; Phase 1 always yields a
        ``require_human`` verdict, which is not an error here).
      * 1 — any uncaught exception; the class + message go to stderr.
      * 2 — CLI usage error (missing/incompatible options).
    """
    try:
        if diff_file is not None:
            diff_text = diff_file.read_text(encoding="utf-8")
            change_set = parse_diff(repo, diff=diff_text)
        else:
            if not base or not head:
                typer.echo(
                    "Error: --base and --head are required (or use --diff-file).",
                    err=True,
                )
                raise typer.Exit(2)
            change_set = parse_diff(repo, base_sha=base, head_sha=head)
        impact = compute_impact(change_set, repo, cache_db=cache_db)
    except typer.Exit:
        raise
    except ChangeIntelError as exc:
        typer.echo(f"Error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # unexpected
        typer.echo(f"Error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(impact.model_dump_json(indent=2))


@debug_app.command("verify")
def debug_verify(
    repo: Path = typer.Option(..., "--repo", help="Path to the git repository."),
    base: str | None = typer.Option(None, "--base", help="Base commit SHA."),
    head: str | None = typer.Option(None, "--head", help="Head commit SHA."),
    diff_file: Path | None = typer.Option(
        None,
        "--diff-file",
        help="Path to a unified diff file (alternative to --base/--head).",
    ),
    policy: Path | None = typer.Option(
        None,
        "--policy",
        help="Path to the policy YAML (reserved; unused in Phase 2).",
    ),
    cache_db: Path | None = typer.Option(
        None,
        "--cache-db",
        help="Path to the SQLite state DB (defaults to <repo>/.trikon/state.db).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the full Verdict as JSON instead of the human summary.",
    ),
    no_sandbox: bool = typer.Option(
        False,
        "--no-sandbox",
        help="Use the host-local subprocess backend instead of Docker (opt-in, no isolation).",
    ),
) -> None:
    """Run the full change-intel + verification pipeline and print a verdict.

    Exit codes (per ``design.md §11.1``):
      * 0 — any well-formed ``Verdict`` (including ``require_human``);
        a valid verdict is a successful CLI invocation.
      * 1 — uncaught Python exception (should be unreachable; the SDK
        boundary translates every :class:`VerificationRunnerError`
        into a ``require_human`` verdict).
      * 2 — Typer usage error (handled automatically by Typer for
        missing required options / bad flag combinations).

    The ``--no-sandbox`` flag switches verification from the default
    Docker-backed sandbox to a host-local subprocess runner. This is
    an opt-in escape hatch for developer machines without Docker; it
    runs on the host with **no isolation** and prints a security
    warning banner to stderr every time it is active. Never use
    ``--no-sandbox`` against a repository whose contents you do not
    fully trust.
    """
    del policy  # Phase 3 will consume this; accepted here for signature parity.

    if no_sandbox:
        typer.echo("=" * 78, err=True)
        typer.echo("⚠  SECURITY WARNING: --no-sandbox is active.", err=True)
        typer.echo("   Verification will run on the HOST with NO isolation. Do NOT use", err=True)
        typer.echo("   with untrusted code. This mode is for local dev only.", err=True)
        typer.echo("=" * 78, err=True)

    diff_content = diff_file.read_text(encoding="utf-8") if diff_file is not None else None
    verdict = sdk_verify(
        repo,
        base_sha=base,
        head_sha=head,
        diff=diff_content,
        cache_db=cache_db,
        no_sandbox=no_sandbox,
    )

    if json_output:
        typer.echo(verdict.model_dump_json(indent=2))
    else:
        typer.echo(
            format_verification_verdict(
                verdict,
                repo_path=repo,
                base_sha=base,
                head_sha=head,
            )
        )

    # A well-formed verdict — even ``require_human`` — is a successful
    # invocation. Any uncaught exception surfaces to Typer, which exits 1.
    raise typer.Exit(code=0)


@coverage_app.command("build")
def coverage_build(
    repo: Path = typer.Option(..., "--repo", help="Path to the git repository."),
    cache_db: Path | None = typer.Option(
        None,
        "--cache-db",
        help="Path to the SQLite state DB (defaults to <repo>/.trikon/state.db).",
    ),
) -> None:
    """Build the coverage map by running the full test suite once.

    Persists a ``symbol -> set(test_ids)`` mapping into the
    ``coverage_map`` and ``tests_seen`` tables of ``.trikon/state.db``.
    Subsequent ``trikon debug verify`` invocations use this map to select
    the smallest test slice covering the change (``design.md §6``).

    Exit codes (per ``design.md §11.2``):
      * 0 — success.
      * 1 — :class:`CoverageBuildError` raised during the build.
      * 2 — Typer usage error (missing ``--repo``).
    """
    state_db = cache_db if cache_db is not None else repo / ".trikon" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(state_db))
    try:
        ensure_verify_tables(conn)
        report = build_coverage_map(repo, conn)
    except CoverageBuildError as exc:
        typer.echo(f"Coverage build failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Built coverage map for {repo}")
    typer.echo(f"Indexed {report.symbols_indexed} symbols -> {report.test_nodes_seen} tests")
    typer.echo(f"Duration: {report.duration_ms / 1000:.1f}s")
    typer.echo(f"Coverage map is fresh at {datetime.now(UTC).isoformat()}")


@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="Transport: stdio (default, for Claude Code / Cursor / Kiro) or http.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for http transport."),
    port: int = typer.Option(4801, "--port", help="Bind port for http transport."),
) -> None:
    """Run the Trikon MCP server so AI agents can call ``trikon_verify``.

    Exposes a single MCP tool, ``trikon_verify``, which wraps
    :func:`trikon.sdk.verify` and returns a serialized
    :class:`~trikon.evidence.report.Verdict`. Register this command as
    an MCP server in Claude Code / Cursor / Kiro's ``mcp.json`` to give
    the agent a "verify before commit / merge / deploy" affordance.

    Exit codes:
      * 0 — server ran to graceful shutdown (client closed stdio).
      * 2 — invalid ``--transport`` value, or an HTTP/SSE transport was
        requested but is not yet enabled in this build.
    """
    from trikon.integrations.mcp_server import run_server

    if transport not in ("stdio", "http", "sse"):
        typer.echo(
            f"Error: --transport must be stdio|http|sse, got {transport!r}",
            err=True,
        )
        raise typer.Exit(code=2)

    run_server(transport=transport, host=host, port=port)


if __name__ == "__main__":
    app()
