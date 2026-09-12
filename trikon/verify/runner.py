"""Orchestrate the verification pass: sandbox spin-up, tests, static checks, plugins."""

from __future__ import annotations

from pathlib import Path

from trikon.evidence.report import ImpactSet, VerificationReport


def run_verification(
    repo_path: Path,
    impact: ImpactSet,
    sandbox_backend: str = "local_docker",
) -> VerificationReport:
    """Execute the impacted checks in isolation and return a structured report.

    Args:
        repo_path: Absolute path to the git repository.
        impact: The precomputed impact set (from change_intel.blast_radius).
        sandbox_backend: One of {"local_docker" (v0.1), "unideploy_warden" (v0.2)}.

    Returns:
        A `VerificationReport` capturing all check outcomes and timings.
    """
    # TODO:
    #   1. Build sandbox context from `sandbox_backend`.
    #   2. Inside sandbox:
    #        a. Run pytest with impact.impacted_tests as the node IDs.
    #        b. Run ruff on impact.changed_files.
    #        c. Run mypy on impact.changed_files.
    #        d. Run any repo-defined check plugins.
    #   3. Aggregate results into VerificationReport.
    raise NotImplementedError
