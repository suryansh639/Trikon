"""Plugin interface for repo-defined custom checks.

Custom checks live under ``.trikon/checks/*.py`` in the target repo. Each
file exposes a ``check(context: CheckContext) -> list[Finding]`` function.

Example custom check (in the target repo):

    # .trikon/checks/no_direct_sql.py
    from trikon.plugins import CheckContext, Finding

    def check(ctx: CheckContext) -> list[Finding]:
        findings = []
        for f in ctx.changed_files:
            if b"cursor.execute(" in ctx.read_bytes(f):
                findings.append(Finding(
                    tool="no_direct_sql",
                    rule_id="NDS001",
                    severity="warning",
                    file_path=str(f),
                    line=1, column=None,
                    message="Use the ORM, not raw SQL.",
                    is_new=True,
                ))
        return findings
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from trikon.verify.static_checks import Finding


@dataclass
class CheckContext:
    """State passed to each custom check."""

    repo_path: Path
    changed_files: list[Path]
    metadata: dict[str, str] = field(default_factory=dict)

    def read_bytes(self, file_path: Path) -> bytes:
        """Read a file relative to `repo_path`."""
        return (self.repo_path / file_path).read_bytes()


def discover_plugins(repo_path: Path) -> list[str]:
    """Return absolute paths to check-plugin files defined in the repo."""
    plugin_dir = repo_path / ".trikon" / "checks"
    if not plugin_dir.exists():
        return []
    return [str(p) for p in plugin_dir.glob("*.py") if not p.name.startswith("_")]


def run_plugin(plugin_path: str, ctx: CheckContext) -> list[Finding]:
    """Import and execute a single plugin file's `check` function."""
    # TODO: importlib.util.spec_from_file_location + module.check(ctx).
    raise NotImplementedError
