"""Canonical data models for Trikon verdicts.

These are the shapes serialized to JSON, embedded in GitHub PR comments, and
stored in the audit log. They are versioned via ``schema_version`` because
downstream consumers (dashboards, compliance exports) will depend on them.
"""

# mypy: disable-error-code=explicit-any
#
# Justification: every ``BaseModel`` subclass in this module inherits pydantic's
# synthetic ``def __init__(self, /, **data: Any) -> None``. Under
# ``--strict --disallow-any-explicit`` mypy flags each subclass declaration as
# ``explicit-any`` even though no ``Any`` appears in *our* source. The design
# permits scoped ``noqa``-style exemptions with justification (design.md §9.3
# "no ``Any`` unless justified by inline noqa"). ``disallow_any_explicit`` is
# still enforced everywhere else, including ``trikon/change_intel/``.

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Decision is widened to four values. `PolicyDecision` is defined as an alias
# for the same Literal so downstream code that wants to signal "this variable
# holds a rule outcome (may be warn)" as distinct from "this variable holds a
# terminal decision (never warn)" can annotate accordingly. Both aliases
# resolve to the same runtime type; the split is documentation, not
# enforcement (design.md §3.6).
#
# The widening is source-compatible: every existing consumer that asserts
# ``decision in ("allow", "block", "require_human")`` continues to work,
# because :func:`trikon.sdk.verify` never emits ``decision == "warn"`` on any
# path — see the invariant note on :attr:`Verdict.decision` below.
Decision = Literal["allow", "block", "require_human", "warn"]
PolicyDecision = Decision  # documented alias; identical Literal.
BlastBucket = Literal["LOW", "MEDIUM", "HIGH"]

# Mirror of :data:`trikon.change_intel.models.ChangeKind`. Deliberately
# duplicated here rather than imported: ``trikon.evidence.report`` is the
# SDK's public boundary and must not depend on ``trikon.change_intel.*``,
# which is an internal producer (design.md §9). The literal values are
# pinned in Requirement 1.4; if the internal ``ChangeKind`` ever grows a
# fifth value, the two literals will diverge and the plumbing site
# (:func:`trikon.change_intel.blast_radius._public_file_changes_sorted`)
# will fail mypy ``--strict`` — that divergence is the intended forcing
# function for the public-boundary contract.
ChangeKind = Literal["added", "modified", "deleted", "renamed"]

# How the runner chose which tests to execute (engine fail-safe design, Data
# Models). ``selected`` runs only the Test_Selector's picks, ``full_suite``
# runs every collected test, ``none`` starts no pytest test run at all (only
# legal for a change that touches no Python file).
TestStrategy = Literal["selected", "full_suite", "none"]
# Why the runner fell back to ``full_suite``. Recorded in a fixed order by
# :func:`trikon.verify.strategy.choose_strategy`.
StrategyReason = Literal[
    "empty_selection",
    "coverage_map_missing",
    "coverage_map_stale",
    "no_base_sha",
]
# Why the test evidence is incomplete. A non-empty list always comes with
# ``TestReport.incomplete is True``.
IncompleteReason = Literal["collection_timeout", "execution_timeout", "collection_error"]


class SymbolRef(BaseModel):
    """A fully-qualified reference to a code symbol."""

    qualified_name: str
    file_path: str
    kind: Literal["function", "class", "method", "assignment"]


class FileChangeInfo(BaseModel):
    """Per-file change metadata carried on :class:`ImpactSet`.

    Mirrors the internal :class:`trikon.change_intel.models.FileChange` on the
    fields the public boundary needs — ``path``, ``change_kind``, ``old_path``
    — and deliberately omits ``hunks`` (the unified-diff line-index sets),
    which are an internal-only detail of the change-intel pipeline and would
    inflate every ``ImpactSet`` JSON payload without carrying value at the SDK
    boundary (design.md §3).

    Field semantics
    ---------------
    ``path``: POSIX-relative path, same value as the internal
    ``FileChange.path``. For an added file this is the new path; for a
    modified file the unchanged path; for a deleted file the pre-deletion path
    (the file does not exist at HEAD); for a renamed file the post-rename
    path.

    ``change_kind``: One of the four :data:`ChangeKind` literals.

    ``old_path``: The pre-rename path when ``change_kind == "renamed"``,
    ``None`` for every other value of ``change_kind``. Callers that want to
    know "did this rename move a file from X to Y" read
    ``(old_path, path)`` when ``change_kind == "renamed"``.
    """

    path: str
    change_kind: ChangeKind
    old_path: str | None = None


class ImpactSet(BaseModel):
    """The set of things a change affects."""

    changed_files: list[str]
    changed_symbols: list[SymbolRef]
    impacted_modules: list[str]
    impacted_public_apis: list[SymbolRef]
    impacted_tests: list[str]
    blast_radius_score: BlastBucket
    blast_radius_numeric: float
    # Additive field (design.md §3). Defaults to ``[]`` so every existing
    # constructor and every existing JSON fixture keeps working; an empty
    # list is the "no metadata supplied" signal for consumers (backward-
    # compat with a pre-v0.3.4 producer, Requirement 1.7 / 1.8).
    file_changes: list[FileChangeInfo] = Field(default_factory=list)


class TestResult(BaseModel):
    """A single test outcome."""

    node_id: str
    outcome: Literal["passed", "failed", "skipped", "errored"]
    duration_ms: int
    failure_summary: str | None = None


class CollectionError(BaseModel):
    """One collector that pytest's Collection_Pass could not import.

    ``path`` is the repo-relative POSIX path of the failing test file and
    ``message`` the trimmed error text. ``attributable`` is true when the
    error is caused by the change itself (the file is changed, or it holds a
    Broken_Import); an attributable error fails the TestReport, a
    non-attributable one only marks it incomplete (Requirements 2.6, 2.7).
    """

    path: str
    message: str
    attributable: bool = False


class TestReport(BaseModel):
    """Aggregated pytest results.

    The fields after ``coverage_map_stale`` were added by the engine
    fail-safe work. Each has a default, so a TestReport built with only the
    previous-release arguments, or parsed from a previous-release JSON
    document, still validates (Requirements 7.5, 7.6):

    - ``collected``: items the Collection_Pass found.
    - ``executed``: Executed_Test_Count, i.e. ``passed + failed``.
    - ``strategy`` / ``strategy_reasons``: which tests ran and why. The
      ``"selected"`` default is what the previous release always did.
    - ``incomplete`` / ``incomplete_reasons``: the evidence is partial
      (a timeout or a non-attributable collection error).
    - ``collection_errors``: every collector the Collection_Pass failed on.
    """

    status: Literal["passed", "failed", "skipped"]
    total: int
    passed: int
    failed: int
    skipped: int
    duration_ms: int
    failures: list[TestResult] = Field(default_factory=list)
    coverage_map_stale: bool = False
    collected: int = 0
    executed: int = 0
    strategy: TestStrategy = "selected"
    strategy_reasons: list[StrategyReason] = Field(default_factory=list)
    incomplete: bool = False
    incomplete_reasons: list[IncompleteReason] = Field(default_factory=list)
    collection_errors: list[CollectionError] = Field(default_factory=list)


class StaticReport(BaseModel):
    """Aggregated static-check results."""

    tools_run: list[str]
    new_errors: int
    new_warnings: int
    preexisting_errors: int
    # Findings are structured payloads emitted by ruff / mypy / repo plugins.
    # ``dict[str, object]`` preserves the "loose in v0.1" contract without
    # introducing ``Any`` at the public boundary; Phase 2 upgrades this to a
    # discriminated ``Finding`` model.
    findings: list[dict[str, object]] = Field(default_factory=list)


class PluginResult(BaseModel):
    """Output of a single repo-defined custom check.

    ``error`` is populated when the plugin failed to import, its
    ``check`` function was missing or non-callable, was declared with
    ``async def`` (Requirement 4.3), raised at runtime (Requirement 4.2),
    or exceeded its per-plugin timeout. When ``error`` is set,
    ``findings`` is empty; per-plugin failures never raise past the
    verification runner (design.md §8).
    """

    plugin: str
    findings: list[dict[str, object]] = Field(default_factory=list)
    error: str | None = None


class BrokenImport(BaseModel):
    """A static import in a Head_Tree file that names a removed module or name.

    ``path`` is the importing file (repo-relative POSIX) and ``line`` the
    1-based line of the import statement. ``module`` is the absolute dotted
    module the statement imports from; ``name`` is the imported name for a
    ``from M import n`` statement and ``None`` for ``import M``
    (Requirement 4.11).
    """

    path: str
    line: int
    module: str
    name: str | None = None
    kind: Literal["removed_module", "removed_name"]


class ImportReport(BaseModel):
    """Import_Checker results carried on every VerificationReport.

    ``broken`` is sorted by ``(path, line)``. ``incomplete`` is true when the
    checker could not see a changed file's base or head content;
    ``unparsed_files`` lists every file it could not parse (Requirements 3.7,
    4.10, 4.11). The all-default instance means "no broken imports found",
    which is also what a change with no Python file produces.
    """

    broken: list[BrokenImport] = Field(default_factory=list)
    incomplete: bool = False
    unparsed_files: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    """Everything the verification runner observed."""

    tests: TestReport
    static: StaticReport
    plugins: list[PluginResult] = Field(default_factory=list)
    sandbox_ms: int
    total_ms: int
    # Added by the engine fail-safe work (Requirement 4.13). Defaults to an
    # empty report so previous-release JSON and constructors keep working.
    imports: ImportReport = Field(default_factory=ImportReport)


class RuleResult(BaseModel):
    """Outcome of a single policy rule evaluation."""

    rule_name: str
    matched: bool
    # ``would_emit`` now covers all four decisions — a warn rule that fires
    # still reports ``would_emit="warn"`` in its trace entry (Requirement 3.4,
    # design.md §3.6). ``PolicyDecision`` is used as the annotation to signal
    # "this is a rule outcome (may be warn)" — it is identical at runtime to
    # ``Decision``.
    would_emit: PolicyDecision | None
    reason: str | None


class Evidence(BaseModel):
    """Everything that led to the verdict."""

    change: ImpactSet
    verification: VerificationReport
    policy_results: list[RuleResult]


class Verdict(BaseModel):
    """The final decision, with full evidence attached."""

    # ``Decision`` is four-valued at the type level, but the SDK-boundary
    # invariant is that emitted Verdicts carry only terminal values
    # (``allow`` | ``block`` | ``require_human``), never ``"warn"`` — warn is
    # a rule outcome, not a verdict outcome (design.md §3.6). The Pydantic
    # type accepts ``"warn"`` for forward-compat and round-tripping, but
    # :func:`trikon.sdk.verify` never returns such a Verdict on any path.
    decision: Decision
    reason: str
    matched_rule: str | None
    evidence: Evidence
    audit_id: UUID
    created_at: datetime
    # New field (design.md §3.6). Rule-declaration order is preserved by
    # ``evaluate_policy``; an empty list is the "no warn matched" case.
    # Never ``None`` — a warn-empty Verdict carries ``warnings == []``
    # (Requirement 3.3). ``default_factory=list`` gives every fresh instance
    # its own list so mutations never leak across Verdicts, and a v1-shaped
    # JSON payload lacking the ``warnings`` key deserializes with ``[]``.
    warnings: list[str] = Field(default_factory=list)
    # History: 1 → 2 when ``Decision`` widened to four values and
    # ``warnings`` was added. 2 → 3 when the engine fail-safe work added the
    # TestReport strategy/collection fields and ``VerificationReport.imports``.
    # Consumers that pin a version, or validate with
    # ``additionalProperties: false``, see the bump before they see unfamiliar
    # keys. Nothing was renamed or retyped (Requirement 7.4).
    #
    # Documents written by the previous release carry an explicit
    # ``"schema_version": 2`` and keep that value when parsed; every new field
    # takes its default (Requirement 7.5). Only a payload with no
    # ``schema_version`` key at all (a pre-v2 document) picks up this default.
    schema_version: int = 3


# ---------------------------------------------------------------------------
# Python_Change helpers
# ---------------------------------------------------------------------------
#
# Shared by the runner (strategy choice), the Policy DSL
# (``change.python_change``) and the Safety_Floor, so all three agree on what
# counts as a Python change.


def is_python_path(path: str) -> bool:
    """Return ``True`` when ``path`` is a Python_File (ends in ``.py``)."""
    return path.endswith(".py")


def is_python_change(change: ImpactSet) -> bool:
    """Return ``True`` when the change touches at least one Python_File.

    Checks the union of every ``file_changes`` entry's ``path`` and
    ``old_path`` and every ``changed_files`` entry. For an ImpactSet built by
    ``compute_impact`` the two lists name the same paths; the union only adds
    conservatism for hand-built ImpactSets (e.g. a pure ``.py`` → ``.txt``
    rename still counts, via ``old_path``).
    """
    for file_change in change.file_changes:
        if is_python_path(file_change.path):
            return True
        if file_change.old_path is not None and is_python_path(file_change.old_path):
            return True
    return any(is_python_path(path) for path in change.changed_files)


# ---------------------------------------------------------------------------
# Never-fail-open sentinels
# ---------------------------------------------------------------------------
#
# When the change-intel pipeline fails (bad repo, corrupt diff, dep-graph
# schema mismatch, ...) the SDK must still return a well-formed ``Verdict``
# with ``decision="require_human"``. The two sentinels below are the shapes
# that verdict embeds: an :class:`ImpactSet` with zero symbols but a ``HIGH``
# blast-radius bucket ("we do not know what changed, so treat it as maximally
# risky") and an empty :class:`VerificationReport` marked ``skipped`` because
# the verification runner never got to observe anything.
#
# ``HIGH``, not ``LOW``, is deliberate: an empty impact set surfaced as
# ``LOW`` would let a downstream policy engine mistake "unknown change" for
# "safe change" and emit ``allow``. See ``design.md §5.1``.

EMPTY_IMPACT_SET: ImpactSet = ImpactSet(
    changed_files=[],
    changed_symbols=[],
    impacted_modules=[],
    impacted_public_apis=[],
    impacted_tests=[],
    blast_radius_score="HIGH",
    blast_radius_numeric=0.0,
    # Redundant with the field default, but written explicitly so the
    # sentinel documents the fail-closed shape by example — every field
    # is listed, every default is confirmed at the construction site
    # (Requirement 8.2, design.md §3).
    file_changes=[],
)


EMPTY_VERIFICATION: VerificationReport = VerificationReport(
    tests=TestReport(
        status="skipped",
        total=0,
        passed=0,
        failed=0,
        skipped=0,
        duration_ms=0,
        failures=[],
        # Nothing ran, so the strategy is ``none`` rather than the
        # ``selected`` field default. Every other new field (and
        # ``imports``) keeps its default.
        strategy="none",
    ),
    static=StaticReport(
        tools_run=[],
        new_errors=0,
        new_warnings=0,
        preexisting_errors=0,
        findings=[],
    ),
    plugins=[],
    sandbox_ms=0,
    total_ms=0,
)
