"""Run ruff, mypy, and repo-defined linters on changed files.

Each check produces a list of `Finding` records. We compute *new* findings
relative to the base commit — a change that inherits existing lint noise
should not be blocked for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """A single static-analysis finding."""

    tool: str  # "ruff" | "mypy" | plugin name
    rule_id: str
    severity: str  # "error" | "warning" | "info"
    file_path: str
    line: int
    column: int | None
    message: str
    is_new: bool  # True if not present at base_sha


def run_ruff(files: list[Path], repo_path: Path) -> list[Finding]:
    """Run ruff and parse its JSON output into `Finding` records."""
    raise NotImplementedError


def run_mypy(files: list[Path], repo_path: Path) -> list[Finding]:
    """Run mypy and parse its output into `Finding` records."""
    raise NotImplementedError


def diff_findings(
    at_head: list[Finding],
    at_base: list[Finding],
) -> list[Finding]:
    """Mark findings as new / preexisting by comparing head vs base runs."""
    # Match on (tool, rule_id, file, line, message) minus the line offset shift.
    raise NotImplementedError
