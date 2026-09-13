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


class SymbolRef(BaseModel):
    """A fully-qualified reference to a code symbol."""

    qualified_name: str
    file_path: str
    kind: Literal["function", "class", "method", "assignment"]


class ImpactSet(BaseModel):
    """The set of things a change affects."""

    changed_files: list[str]
    changed_symbols: list[SymbolRef]
    impacted_modules: list[str]
    impacted_public_apis: list[SymbolRef]
    impacted_tests: list[str]
    blast_radius_score: BlastBucket
    blast_radius_numeric: float


class TestResult(BaseModel):
    """A single test outcome."""

    node_id: str
    outcome: Literal["passed", "failed", "skipped", "errored"]
    duration_ms: int
    failure_summary: str | None = None


class TestReport(BaseModel):
    """Aggregated pytest results."""

    status: Literal["passed", "failed", "skipped"]
    total: int
    passed: int
    failed: int
    skipped: int
    duration_ms: int
    failures: list[TestResult] = Field(default_factory=list)
    coverage_map_stale: bool = False


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


class VerificationReport(BaseModel):
    """Everything the verification runner observed."""

    tests: TestReport
    static: StaticReport
    plugins: list[PluginResult] = Field(default_factory=list)
    sandbox_ms: int
    total_ms: int


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
    # Bumped 1 → 2 because the shape of the JSON grew: ``Decision`` widened
    # to four values and ``warnings`` was added (design.md §3.6). Downstream
    # consumers pinned to ``schema_version == 1`` will see the version bump
    # before they see an unfamiliar ``warnings`` key (Requirement 4.2). A
    # v1-shaped payload without a ``schema_version`` field deserializes with
    # ``schema_version == 2`` via this default.
    schema_version: int = 2


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
