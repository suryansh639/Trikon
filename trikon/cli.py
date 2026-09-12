"""Trikon command-line interface.

Entrypoint: ``trikon <command>``

Commands (v0.1):
    verify      Run the Trikon pipeline against a git change set and print a verdict.
    init        Scaffold a default .trikon/policy.yaml in the current repo.
    version     Print the installed Trikon version.

Not yet implemented — see ARCHITECTURE.md §MVP for scope and §5 for the intended flow.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="trikon",
    help="Verification layer for autonomous AI coding agents.",
    no_args_is_help=True,
)


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


if __name__ == "__main__":
    app()
