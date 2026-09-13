"""Verification Runner — execute impacted checks inside an isolated sandbox.

Pipeline:
    test_selector.select_tests()  → tests exercising the impacted symbols
    static_checks.run_static()    → ruff, mypy, custom linters on changed files
    runner.run_verification()     → orchestrates the above inside sandbox.execute()

The runner does not decide anything. It reports facts. The policy engine decides.

Public re-exports are limited to the frozen dataclasses in :mod:`trikon.verify.models`
and the :class:`VerificationRunnerError` hierarchy in :mod:`trikon.verify.errors`.
The runner, sandbox, and check orchestration modules stay private to the package;
the SDK boundary consumes them via :mod:`trikon.verify.runner` only.
"""

from trikon.verify.errors import (
    CoverageBuildError,
    PluginLoadError,
    SandboxExecError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    StaticCheckError,
    TestSelectionError,
    VerificationRunnerError,
)
from trikon.verify.models import (
    CoverageBuildReport,
    SandboxExecResult,
    SelectedTests,
    StaticTool,
)
from trikon.verify.sandbox import LocalDockerSandbox

__all__ = [
    "CoverageBuildError",
    "CoverageBuildReport",
    "LocalDockerSandbox",
    "PluginLoadError",
    "SandboxExecError",
    "SandboxExecResult",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "SelectedTests",
    "StaticCheckError",
    "StaticTool",
    "TestSelectionError",
    "VerificationRunnerError",
]
