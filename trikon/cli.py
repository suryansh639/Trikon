"""Trikon command-line interface.

Entrypoint: ``trikon <command>``

Commands (v0.1):
    verify         Run the Trikon pipeline against a git change set and print a verdict.
    init           Scaffold a default .trikon/policy.yaml in the current repo.
    version        Print the installed Trikon version.
    debug impact   Run change-intelligence against a diff and print the ImpactSet JSON.

The ``verify`` / ``init`` commands are still stubs; only ``version`` and the
``debug impact`` command are wired to real code paths in Phase 1. See
ARCHITECTURE.md §MVP for scope and §5 for the intended flow.
"""

from __future__ import annotations

from pathlib import Path

import typer

from trikon import sdk

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
) -> None:
    """Verify a proposed change and emit a verdict."""
    # Wire into `trikon.sdk.verify` and render the verdict via the requested formatter.
    # Exit code: 0 if allow, 1 if block, 2 if require_human.
    raise NotImplementedError("Wire this up once trikon.sdk.verify is implemented.")


@app.command()
def init(
    repo: Path = typer.Option(Path.cwd(), "--repo", help="Path to the git repository."),
) -> None:
    """Scaffold a starter .trikon/policy.yaml in the target repo."""
    # Copy examples/policies/default.yaml into <repo>/.trikon/policy.yaml.
    raise NotImplementedError("Ship a starter policy from examples/policies/default.yaml.")


@app.command()
def version() -> None:
    """Print the installed Trikon version."""
    from trikon import __version__

    typer.echo(__version__)


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
    """Run change-intelligence against a diff and print the ImpactSet JSON.

    Exit codes:
      * 0 — success (the ``ImpactSet`` was emitted; Phase 1 always yields a
        ``require_human`` verdict, which is not an error here).
      * 1 — any uncaught exception; the class + message go to stderr.
      * 2 — CLI usage error (missing/incompatible options).
    """
    try:
        if diff_file is not None:
            diff_text = diff_file.read_text(encoding="utf-8")
            verdict = sdk.verify(repo, diff=diff_text, cache_db=cache_db)
        else:
            if not base or not head:
                typer.echo(
                    "Error: --base and --head are required (or use --diff-file).",
                    err=True,
                )
                raise typer.Exit(2)
            verdict = sdk.verify(repo, base_sha=base, head_sha=head, cache_db=cache_db)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(verdict.evidence.change.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
