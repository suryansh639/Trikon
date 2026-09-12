"""Public Python SDK entry point.

The CLI, MCP tool, GitHub App, and GitHub Actions integration are all thin
wrappers around `verify()`. This is the one code path.

Usage:
    from trikon import verify

    verdict = verify(
        repo_path="/path/to/repo",
        base_sha="abc123",
        head_sha="def456",
        policy_path=".trikon/policy.yaml",
    )
    if verdict.decision == "block":
        raise SystemExit(1)
"""

from __future__ import annotations

from pathlib import Path

from trikon.change_intel.blast_radius import compute_impact
from trikon.change_intel.diff_parser import parse_diff
from trikon.evidence.report import Verdict
from trikon.policy.evaluator import evaluate_policy
from trikon.policy.loader import load_policy
from trikon.verify.runner import run_verification


def verify(
    repo_path: str | Path,
    base_sha: str | None = None,
    head_sha: str | None = None,
    diff: str | None = None,
    policy_path: str | Path = ".trikon/policy.yaml",
) -> Verdict:
    """Run the full Trikon pipeline against a change set.

    Args:
        repo_path: Absolute path to the git repository on disk.
        base_sha: Git SHA of the base commit. Mutually exclusive with ``diff``.
        head_sha: Git SHA of the head commit. Mutually exclusive with ``diff``.
        diff: Unified-diff string. Mutually exclusive with base/head.
        policy_path: Path to the policy YAML, relative to ``repo_path`` or absolute.

    Returns:
        A `Verdict` with decision, reason, and structured evidence.

    Raises:
        ValueError: if neither (base_sha + head_sha) nor ``diff`` is provided.
        FileNotFoundError: if the repo or policy file cannot be found.
    """
    # Step 1: parse the change into a structured ChangeSet.
    change_set = parse_diff(
        repo_path=Path(repo_path),
        base_sha=base_sha,
        head_sha=head_sha,
        diff=diff,
    )

    # Step 2: compute the blast radius — impacted modules, symbols, tests.
    impact = compute_impact(change_set=change_set, repo_path=Path(repo_path))

    # Step 3: execute the impacted tests + static checks in an isolated sandbox.
    verification = run_verification(
        repo_path=Path(repo_path),
        impact=impact,
    )

    # Step 4: apply the policy DSL to produce a verdict.
    policy = load_policy(Path(repo_path), Path(policy_path))
    verdict = evaluate_policy(
        policy=policy,
        change=impact,
        verification=verification,
    )

    return verdict
