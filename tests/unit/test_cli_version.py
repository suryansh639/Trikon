"""CLI tests for the eager ``--version`` option and the ``version`` command.

``trikon --version`` must print the installed version and exit 0, because the
release checks run it in a fresh venv and in the published sandbox image. The
``version`` subcommand must keep working alongside it. Every assertion compares
against :data:`trikon.__version__` so the tests survive version bumps.

Validates: Requirements 10.9
"""

from __future__ import annotations

from typer.testing import CliRunner

import trikon
from trikon.cli import app

runner = CliRunner()


def test_version_option_prints_version_and_exits_zero() -> None:
    """``trikon --version`` prints only ``trikon.__version__`` and exits 0."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == trikon.__version__


def test_version_subcommand_still_prints_version() -> None:
    """The ``trikon version`` subcommand keeps its output next to the option."""
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == trikon.__version__


def test_version_option_is_eager_before_subcommand() -> None:
    """``--version`` wins over a following subcommand, which never runs."""
    result = runner.invoke(app, ["--version", "verify"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == trikon.__version__
