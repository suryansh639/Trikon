"""Exception hierarchy for the Verification Runner subsystem.

Every raise site under ``trikon.verify`` MUST use one of the classes
defined here. Nothing raises bare ``Exception``, ``ValueError``,
``docker.errors.*``, ``subprocess.CalledProcessError``, or
``sqlite3.Error`` past the module boundary — that closure is what lets
``trikon.sdk.verify`` translate any internal failure into a
``require_human`` verdict without ambiguity.

See requirements.md §Requirement 6 and design.md §12.
"""

from __future__ import annotations

from trikon.exceptions import TrikonError

__all__ = [
    "CoverageBuildError",
    "PluginLoadError",
    "SandboxExecError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "StaticCheckError",
    "TestSelectionError",
    "VerificationRunnerError",
]


class VerificationRunnerError(TrikonError):
    """Base class for every error raised by :mod:`trikon.verify`."""


class SandboxUnavailableError(VerificationRunnerError):
    """The Docker daemon cannot be reached.

    Raised on ``docker.from_env`` failure (socket missing, daemon down,
    permission denied). The diagnostic message MUST include the socket
    path that was attempted (Requirement 6.3).
    """


class SandboxExecError(VerificationRunnerError):
    """A non-timeout container operation failed.

    Raised for ``docker.errors.APIError``, image-pull failures, mount
    validation failures, OOM kills detected via
    ``container.attrs['State']['OOMKilled']``, and any ``exec_create`` /
    ``exec_start`` failure.
    """


class SandboxTimeoutError(VerificationRunnerError):
    """Container exceeded its wall-clock deadline.

    Defined for completeness but never raised past the module boundary
    (Requirement 2.2). The runner internally converts a timed-out
    ``SandboxExecResult`` into a synthesized failed ``TestReport`` and
    returns normally. Present in the hierarchy so unit tests can
    parametrize by exception class without a special case.
    """


class TestSelectionError(VerificationRunnerError):
    """``select_impacted_tests`` could not query the coverage map.

    Raised on ``sqlite3.Error`` from the ``coverage_map`` lookup, and on
    malformed ``test_ids_json`` payloads (JSON parse failure or shape
    mismatch).
    """


class StaticCheckError(VerificationRunnerError):
    """``run_static_checks`` failed to produce a report.

    Raised on git-worktree materialization failure, tool-version capture
    failure, JSON parse failure of ruff's ``--output-format=json`` output,
    and any ``sqlite3.Error`` writing the baseline row.
    """


class PluginLoadError(VerificationRunnerError):
    """Plugin discovery or shim invocation failed at the sandbox level.

    Note this is NOT raised for per-plugin failures — those are recorded
    on the returned ``PluginResult``. This class is reserved for
    infrastructure problems the runner cannot recover from (shim missing,
    ``.trikon/checks/`` unreadable, JSON output file missing).
    """


class CoverageBuildError(VerificationRunnerError):
    """``build_coverage_map`` failed during test collection or execution.

    On any raise, the caller's previously-persisted ``coverage_map`` /
    ``tests_seen`` rows are left untouched (Requirement 5.3).
    """
