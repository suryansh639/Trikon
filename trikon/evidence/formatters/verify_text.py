"""Human-readable formatter for ``trikon debug verify``.

Renders a :class:`~trikon.evidence.report.Verdict` as the five-section text
block specified in ``.kiro/specs/verification-runner/design.md §11.1``:

    1. Header       -- repo label plus short base/head SHAs.
    2. Change       -- file/symbol counts and blast-radius bucket.
    3. Tests        -- pass/fail/skip counts, first 5 failing node IDs,
                       coverage-map freshness parenthetical.
    4. Static       -- one line per static-analysis tool (``new`` versus
                       ``preexisting``).
    5. Plugins      -- one line per repo-defined plugin, with a pluralized
                       finding count.
    6. Sandbox      -- wall-clock duration and lifecycle summary. When
                       the sandbox never started (fail-closed shape with
                       ``sandbox_ms == 0 and total_ms == 0``) the line
                       reads ``Sandbox: not started``; the defensive
                       middle branch ``Sandbox: not started (verification
                       total: <N>s)`` covers any future code path that
                       records ``total_ms`` without ``sandbox_ms``.
    7. Verdict      -- terminal decision line.

The formatter is pure: no I/O, no imports outside
:mod:`trikon.evidence.report`. ``base_sha`` and ``head_sha`` are not stored
on the :class:`Verdict` model, so the CLI (Task 11.2) passes them in
explicitly; ``repo_path`` is optional for the same reason.
"""

from __future__ import annotations

from pathlib import Path

from trikon.evidence.report import (
    PluginResult,
    StaticReport,
    TestReport,
    Verdict,
    VerificationReport,
)

_MAX_FAILING_TESTS_SHOWN = 5


def _short_sha(sha: str | None) -> str:
    """Return the first 8 characters of ``sha`` or ``"?"`` when unknown."""
    if sha is None:
        return "?"
    return sha[:8] if len(sha) >= 8 else sha


def _seconds(duration_ms: int) -> str:
    """Format a millisecond duration as ``<N>.<D>`` (one decimal)."""
    return f"{duration_ms / 1000:.1f}"


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``singular`` when ``count == 1``, else ``plural`` (default: +s)."""
    if count == 1:
        return singular
    return plural if plural is not None else singular + "s"


def _repo_label(repo_path: Path | None) -> str:
    """Return the display label for the repo header (basename or placeholder)."""
    if repo_path is None:
        return "<repo>"
    return repo_path.name or str(repo_path)


def _header_line(repo_path: Path | None, base_sha: str | None, head_sha: str | None) -> str:
    return (
        f"Trikon verification for {_repo_label(repo_path)} "
        f"(base: {_short_sha(base_sha)} \u2192 head: {_short_sha(head_sha)})"
    )


def _change_line(files: int, symbols: int, blast: str) -> str:
    return (
        f"Change: {files} {_pluralize(files, 'file')}, "
        f"{symbols} {_pluralize(symbols, 'symbol')}, "
        f"{blast} blast radius"
    )


def _tests_block(tests: TestReport) -> list[str]:
    """Render the ``Tests:`` section (header, failing-test list, freshness)."""
    header = (
        f"Tests:  passed={tests.passed}  failed={tests.failed}  "
        f"skipped={tests.skipped}  "
        f"({tests.total} total, {_seconds(tests.duration_ms)}s)"
    )
    lines: list[str] = [header]

    if tests.failures:
        lines.append("        First 5 failing tests:")
        shown = tests.failures[:_MAX_FAILING_TESTS_SHOWN]
        for failure in shown:
            lines.append(f"          {failure.node_id}")
        extra = len(tests.failures) - len(shown)
        if extra > 0:
            lines.append(f"          ... and {extra} more")

    if tests.coverage_map_stale:
        lines.append("        (coverage map stale \u2014 filename-heuristic fallback used)")
    else:
        lines.append("        (coverage map fresh)")
    return lines


def _tools_in_display_order(static: StaticReport) -> list[str]:
    """Preserve ``tools_run`` order, appending any extra tools seen in findings."""
    order: list[str] = list(static.tools_run)
    for finding in static.findings:
        tool_obj = finding.get("tool")
        if isinstance(tool_obj, str) and tool_obj not in order:
            order.append(tool_obj)
    return order


def _count_findings_by_tool(static: StaticReport, tool: str) -> tuple[int, int]:
    """Return ``(new_count, preexisting_count)`` for ``tool`` in ``static``."""
    new_count = 0
    pre_count = 0
    for finding in static.findings:
        if finding.get("tool") != tool:
            continue
        if finding.get("is_new") is True:
            new_count += 1
        else:
            pre_count += 1
    return new_count, pre_count


def _static_block(verification: VerificationReport) -> list[str]:
    """Render the ``Static:`` section, one line per tool."""
    static = verification.static
    tools = _tools_in_display_order(static)

    if not tools:
        return ["Static: (no tools run)"]

    name_width = max(len(t) for t in tools) + 2
    lines: list[str] = []
    prefix = "Static: "
    for tool in tools:
        new_count, pre_count = _count_findings_by_tool(static, tool)
        lines.append(f"{prefix}{tool.ljust(name_width)}new={new_count}  preexisting={pre_count}")
        prefix = "        "
    return lines


def _plugins_block(plugins: list[PluginResult]) -> list[str]:
    """Render the ``Plugins:`` section, one line per plugin."""
    if not plugins:
        return ["Plugins: (none)"]

    name_width = max(len(p.plugin) for p in plugins) + 2
    lines: list[str] = []
    prefix = "Plugins: "
    for plugin in plugins:
        n = len(plugin.findings)
        lines.append(f"{prefix}{plugin.plugin.ljust(name_width)}{n} {_pluralize(n, 'finding')}")
        prefix = "         "
    return lines


def _sandbox_line(verification: VerificationReport) -> str:
    if verification.sandbox_ms == 0 and verification.total_ms == 0:
        return "Sandbox: not started"
    if verification.sandbox_ms == 0:
        return f"Sandbox: not started (verification total: {_seconds(verification.total_ms)}s)"
    return (
        f"Sandbox: {_seconds(verification.sandbox_ms)}s wall clock "
        f"(started, ran, torn down cleanly)"
    )


def _verdict_line(verdict: Verdict) -> str:
    return f"Verdict: {verdict.decision} \u2014 {verdict.reason}"


def format_verification_verdict(
    verdict: Verdict,
    *,
    repo_path: Path | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> str:
    """Render ``verdict`` as the design §11.1 five-section human summary.

    The two SHA parameters and ``repo_path`` are optional because they are
    not stored on the :class:`Verdict` model itself. When omitted the
    header prints ``"<repo>"`` / ``"?"`` placeholders so callers can still
    exercise the formatter (unit tests, dry-runs) without fabricating SHAs.
    """
    change = verdict.evidence.change
    verification = verdict.evidence.verification

    lines: list[str] = []
    lines.append(_header_line(repo_path, base_sha, head_sha))
    lines.append("")

    lines.append(
        _change_line(
            files=len(change.changed_files),
            symbols=len(change.changed_symbols),
            blast=change.blast_radius_score,
        )
    )
    lines.append("")

    lines.extend(_tests_block(verification.tests))
    lines.append("")

    lines.extend(_static_block(verification))
    lines.append("")

    lines.extend(_plugins_block(verification.plugins))
    lines.append("")

    lines.append(_sandbox_line(verification))
    lines.append("")

    lines.append(_verdict_line(verdict))

    return "\n".join(lines)
