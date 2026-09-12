"""Canonical data models for Trikon verdicts.

These are the shapes serialized to JSON, embedded in GitHub PR comments, and
stored in the audit log. They are versioned via ``schema_version`` because
downstream consumers (dashboards, compliance exports) will depend on them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


Decision = Literal["allow", "block", "require_human"]
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
    findings: list[dict] = Field(default_factory=list)  # kept loose in v0.1


class PluginResult(BaseModel):
    """Output of a single repo-defined custom check."""

    plugin: str
    findings: list[dict] = Field(default_factory=list)


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
    would_emit: Decision | None
    reason: str | None


class Evidence(BaseModel):
    """Everything that led to the verdict."""

    change: ImpactSet
    verification: VerificationReport
    policy_results: list[RuleResult]


class Verdict(BaseModel):
    """The final decision, with full evidence attached."""

    decision: Decision
    reason: str
    matched_rule: str | None
    evidence: Evidence
    audit_id: UUID
    created_at: datetime
    schema_version: int = 1
