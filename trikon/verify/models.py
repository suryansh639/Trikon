"""Internal data models for the Verification Runner pipeline.

Every type in this module is a frozen, slotted dataclass, mirroring the pattern
established in :mod:`trikon.change_intel.models`. That choice is deliberate:

* Frozen dataclasses are immutable, so a ``SelectedTests`` or
  ``SandboxExecResult`` cannot be mutated after leaving its producer. Bugs
  where a later stage rewrites an earlier stage's output cannot happen.
* Frozen dataclasses are hashable, which lets these value objects sit in
  ``set()`` / ``dict`` keys during report assembly without a bespoke
  ``__hash__``.
* ``slots=True`` eliminates the per-instance ``__dict__``. On a full-suite
  coverage build across a Django-sized codebase that is tens of thousands of
  instantiations; the memory and allocation savings matter on the hot path.

These types are the *internal* boundary of Verification Runner. The public
boundary — the shapes that land in a ``Verdict`` — is the Pydantic model set
in :mod:`trikon.evidence.report` (``VerificationReport``, ``TestReport``,
``StaticReport``, ``PluginResult``, and friends). The two shapes intentionally
do not overlap; the public surface has stricter validation and JSON-schema
generation costs we do not want to pay on every sandbox invocation, coverage
build, or per-plugin dispatch. See ``design.md §3.7`` for the full contract.

No field in this module is typed as ``dict[str, Any]``. Every payload that
would tempt that shape is either a tuple of typed primitives (``argv_template``,
``version_command``, ``fallback_reasons``) or a fully-typed scalar. This keeps
the module clean under ``[tool.mypy] disallow_any_explicit = true``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Test-selection output (see design.md §3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectedTests:
    """Pytest node IDs plus the provenance metadata the report needs.

    Produced by :func:`trikon.verify.test_selector.select_impacted_tests` and
    consumed by :func:`trikon.verify.runner.run_verification` to drive the
    in-sandbox pytest invocation.

    ``node_ids`` is deterministic-sorted so the same ``ImpactSet`` always
    produces byte-identical JSON when the report is serialized.
    ``coverage_map_stale`` propagates verbatim to
    :attr:`trikon.evidence.report.TestReport.coverage_map_stale` — the human
    formatter surfaces it as a "coverage map is stale" hint.
    ``fallback_reasons`` carries one entry per symbol that missed the coverage
    map (or was routed through the filename heuristic because the whole map
    was stale); the CLI human summary from Task 11 renders these to explain
    why a test was chosen.
    """

    node_ids: tuple[str, ...]
    coverage_map_stale: bool
    fallback_reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Sandbox execution result (see design.md §3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """Outcome of one command executed inside :class:`LocalDockerSandbox`.

    ``LocalDockerSandbox.exec`` returns this on every path — including
    non-zero exit codes and timeouts — because the sandbox contract from
    ``design.md §3.3`` and Requirement 2.2 forbids raising past the module
    boundary on timeouts. Callers inspect ``timed_out`` to distinguish a
    genuine tool failure from a deadline breach and synthesize the
    appropriate ``TestReport`` accordingly.

    ``duration_ms`` is captured via a ``time.monotonic()`` bracket around the
    ``exec_start`` call so it is monotonic across DST transitions and NTP
    adjustments. ``stdout`` and ``stderr`` are decoded as UTF-8 with the
    ``replace`` error handler so a rogue tool emitting invalid bytes cannot
    poison the report with a ``UnicodeDecodeError``.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


# ---------------------------------------------------------------------------
# Static-analysis tool descriptor (see design.md §3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaticTool:
    """A pinned static-analysis tool the runner knows how to invoke.

    ``name`` is the human-facing tool name (``"ruff"``, ``"mypy"``); it is
    the discriminator on the ``static_baseline`` cache key alongside
    ``base_sha`` and ``tool_version`` (see ``design.md §4.1``).
    ``argv_template`` is the argv tuple used to invoke the tool inside the
    sandbox; the sentinel token ``"{files}"`` is expanded to the changed-file
    list at runtime.
    ``version_command`` is the argv tuple used to capture ``tool_version``
    for the cache key — bumping a pinned tool version in ``pyproject.toml``
    (or in the sandbox image) is what invalidates every cached row for that
    tool, per Requirement 3.3.
    ``parse_json`` picks the finding parser: ``ruff`` emits JSON with
    ``--output-format=json`` and is parsed by ``_parse_ruff_json``; ``mypy``
    emits line-per-diagnostic text and is parsed by ``_parse_mypy_text``.

    The type is a value object, not a strategy — the actual parser
    dispatch lives in :mod:`trikon.verify.static_checks` and picks between
    the two by inspecting this flag.
    """

    name: str
    argv_template: tuple[str, ...]
    version_command: tuple[str, ...]
    parse_json: bool


# ---------------------------------------------------------------------------
# Default static-tool registry (see design.md §3.4, Requirement 3.1)
# ---------------------------------------------------------------------------
#
# The canonical tuple of :class:`StaticTool` descriptors that
# :func:`trikon.verify.static_checks.run_static_checks` iterates by default.
# It lives here — not in ``static_checks.py`` — so tests, CLI code, and any
# future SDK entry point can import the descriptors without transitively
# importing the sandbox module (which pulls in the Docker client and its
# heavy dependencies at import time).
#
# The ``{files}`` sentinel in each ``argv_template`` is expanded to the
# head-side changed-file list by ``run_static_checks``; the ``version_command``
# tuple is what feeds the ``tool_version`` component of the
# ``static_baseline`` cache key (design.md §4.1). Bumping a pinned tool
# version in ``pyproject.toml`` (or in the sandbox image) is therefore what
# invalidates every cached row for that tool, per Requirement 3.3.
DEFAULT_STATIC_TOOLS: tuple[StaticTool, ...] = (
    StaticTool(
        name="ruff",
        argv_template=("ruff", "check", "--output-format=json", "{files}"),
        version_command=("ruff", "--version"),
        parse_json=True,
    ),
    StaticTool(
        name="mypy",
        argv_template=("mypy", "--no-color-output", "--show-column-numbers", "{files}"),
        version_command=("mypy", "--version"),
        parse_json=False,
    ),
)


# ---------------------------------------------------------------------------
# Coverage-map build report (see design.md §3.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageBuildReport:
    """Outcome of a ``trikon coverage build`` invocation.

    Returned by :func:`trikon.verify.coverage_builder.build_coverage_map`
    after a full-suite instrumented pytest run finishes and its results have
    been persisted atomically to the ``coverage_map`` and ``tests_seen``
    tables of ``.trikon/state.db`` (see ``design.md §10``).

    ``symbols_indexed`` is the count of distinct ``qualified_name`` entries
    written on this build; ``test_nodes_seen`` is the count of distinct
    pytest node IDs observed across all symbols. ``duration_ms`` covers the
    end-to-end build (sandbox startup, ``pytest --collect-only``, instrumented
    ``coverage run``, ``coverage json``, and SQLite persistence), captured
    the same way as :attr:`SandboxExecResult.duration_ms`.
    ``built_against_sha`` is the git SHA the build ran on and is what
    :func:`trikon.verify.test_selector.select_impacted_tests` compares against
    ``ImpactSet.base_sha`` when deciding whether the map is stale by SHA
    drift (Requirement 1.3). ``stale_rows_pruned`` is the count of
    ``tests_seen`` rows removed because the observed pytest node IDs no
    longer collect — feeds the future dead-test hygiene UI.
    """

    symbols_indexed: int
    test_nodes_seen: int
    duration_ms: int
    built_against_sha: str
    stale_rows_pruned: int


__all__ = [
    "DEFAULT_STATIC_TOOLS",
    "CoverageBuildReport",
    "SandboxExecResult",
    "SelectedTests",
    "StaticTool",
]
