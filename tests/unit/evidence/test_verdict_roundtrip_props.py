# Feature: trikon-engine-fail-safe, Property 13: Verdict JSON round trip
"""Property test: every Verdict survives a JSON round trip unchanged.

*For any* generated :class:`~trikon.evidence.report.Verdict`, including every
field added by the engine fail-safe work and JSON-compatible findings,
``Verdict.model_validate_json(v.model_dump_json()) == v``.

The strategies below build every model in the Verdict tree with every field
passed explicitly, so both the previous-release fields and the new ones are
exercised:

- ``TestReport``: ``collected``, ``executed``, ``strategy``,
  ``strategy_reasons``, ``incomplete``, ``incomplete_reasons`` and
  ``collection_errors``;
- ``VerificationReport.imports``: an ``ImportReport`` whose ``broken``
  entries carry ``name`` as either ``None`` or a string;
- ``Verdict.schema_version``: ``2`` (a stored previous-release document) or
  ``3`` (a fresh one).

Value domains are limited to what JSON can carry losslessly: text without
lone surrogates, finite floats, timezone-aware UTC datetimes with
microsecond precision, and findings built only from ``str``/``int``/``bool``/
``None`` scalars nested in lists and string-keyed dicts.

**Validates: Requirements 7.7**
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

# ``TestReport`` and ``TestResult`` are reached through the module so pytest
# does not try to collect them as test classes from this module's namespace.
from trikon.evidence import report
from trikon.evidence.report import (
    BlastBucket,
    BrokenImport,
    ChangeKind,
    CollectionError,
    Decision,
    Evidence,
    FileChangeInfo,
    ImpactSet,
    ImportReport,
    IncompleteReason,
    PluginResult,
    RuleResult,
    StaticReport,
    StrategyReason,
    SymbolRef,
    TestStrategy,
    Verdict,
    VerificationReport,
)

# ---------------------------------------------------------------------------
# Scalar strategies
# ---------------------------------------------------------------------------

# Any UTF-8-encodable character (no lone surrogates), so quotes, backslashes,
# control characters and non-ASCII text all exercise JSON string escaping.
_TEXT: st.SearchStrategy[str] = st.text(st.characters(codec="utf-8"), max_size=20)
_OPTIONAL_TEXT: st.SearchStrategy[str | None] = st.none() | _TEXT
_TEXTS: st.SearchStrategy[list[str]] = st.lists(_TEXT, max_size=3)

# Counts and durations are non-negative in practice; the upper bound keeps
# them inside a 32-bit range like every value the runner produces.
_COUNT: st.SearchStrategy[int] = st.integers(min_value=0, max_value=2**31 - 1)

_DECISIONS: tuple[Decision, ...] = ("allow", "block", "require_human", "warn")
_DECISION: st.SearchStrategy[Decision] = st.sampled_from(_DECISIONS)

_BUCKETS: tuple[BlastBucket, ...] = ("LOW", "MEDIUM", "HIGH")
_CHANGE_KINDS: tuple[ChangeKind, ...] = ("added", "modified", "deleted", "renamed")
_TEST_STRATEGIES: tuple[TestStrategy, ...] = ("selected", "full_suite", "none")
_STRATEGY_REASONS: tuple[StrategyReason, ...] = (
    "empty_selection",
    "coverage_map_missing",
    "coverage_map_stale",
    "no_base_sha",
)
_INCOMPLETE_REASONS: tuple[IncompleteReason, ...] = (
    "collection_timeout",
    "execution_timeout",
    "collection_error",
)

# Timezone-aware UTC datetimes with microsecond precision. Pydantic writes
# them as ISO 8601 with a ``Z`` suffix and parses them back to the same
# instant.
_CREATED_AT: st.SearchStrategy[datetime] = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2200, 12, 31, 23, 59, 59, 999999),
    timezones=st.just(UTC),
)

# ---------------------------------------------------------------------------
# JSON-compatible findings
# ---------------------------------------------------------------------------

_JSON_SCALAR: st.SearchStrategy[object] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    _TEXT,
)
_JSON_VALUE: st.SearchStrategy[object] = st.recursive(
    _JSON_SCALAR,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_TEXT, children, max_size=3),
    max_leaves=8,
)
_FINDINGS: st.SearchStrategy[list[dict[str, object]]] = st.lists(
    st.dictionaries(_TEXT, _JSON_VALUE, max_size=4),
    max_size=3,
)

# ---------------------------------------------------------------------------
# Model strategies
# ---------------------------------------------------------------------------

_SYMBOL_REF: st.SearchStrategy[SymbolRef] = st.builds(
    SymbolRef,
    qualified_name=_TEXT,
    file_path=_TEXT,
    kind=st.sampled_from(("function", "class", "method", "assignment")),
)

_FILE_CHANGE_INFO: st.SearchStrategy[FileChangeInfo] = st.builds(
    FileChangeInfo,
    path=_TEXT,
    change_kind=st.sampled_from(_CHANGE_KINDS),
    old_path=_OPTIONAL_TEXT,
)

_IMPACT_SET: st.SearchStrategy[ImpactSet] = st.builds(
    ImpactSet,
    changed_files=_TEXTS,
    changed_symbols=st.lists(_SYMBOL_REF, max_size=3),
    impacted_modules=_TEXTS,
    impacted_public_apis=st.lists(_SYMBOL_REF, max_size=3),
    impacted_tests=_TEXTS,
    blast_radius_score=st.sampled_from(_BUCKETS),
    blast_radius_numeric=st.floats(allow_nan=False, allow_infinity=False),
    file_changes=st.lists(_FILE_CHANGE_INFO, max_size=3),
)

_TEST_RESULT: st.SearchStrategy[report.TestResult] = st.builds(
    report.TestResult,
    node_id=_TEXT,
    outcome=st.sampled_from(("passed", "failed", "skipped", "errored")),
    duration_ms=_COUNT,
    failure_summary=_OPTIONAL_TEXT,
)

_COLLECTION_ERROR: st.SearchStrategy[CollectionError] = st.builds(
    CollectionError,
    path=_TEXT,
    message=_TEXT,
    attributable=st.booleans(),
)

_TEST_REPORT: st.SearchStrategy[report.TestReport] = st.builds(
    report.TestReport,
    status=st.sampled_from(("passed", "failed", "skipped")),
    total=_COUNT,
    passed=_COUNT,
    failed=_COUNT,
    skipped=_COUNT,
    duration_ms=_COUNT,
    failures=st.lists(_TEST_RESULT, max_size=3),
    coverage_map_stale=st.booleans(),
    collected=_COUNT,
    executed=_COUNT,
    strategy=st.sampled_from(_TEST_STRATEGIES),
    strategy_reasons=st.lists(st.sampled_from(_STRATEGY_REASONS), max_size=4),
    incomplete=st.booleans(),
    incomplete_reasons=st.lists(st.sampled_from(_INCOMPLETE_REASONS), max_size=3),
    collection_errors=st.lists(_COLLECTION_ERROR, max_size=3),
)

_STATIC_REPORT: st.SearchStrategy[StaticReport] = st.builds(
    StaticReport,
    tools_run=_TEXTS,
    new_errors=_COUNT,
    new_warnings=_COUNT,
    preexisting_errors=_COUNT,
    findings=_FINDINGS,
)

_PLUGIN_RESULT: st.SearchStrategy[PluginResult] = st.builds(
    PluginResult,
    plugin=_TEXT,
    findings=_FINDINGS,
    error=_OPTIONAL_TEXT,
)

_BROKEN_IMPORT: st.SearchStrategy[BrokenImport] = st.builds(
    BrokenImport,
    path=_TEXT,
    line=st.integers(min_value=1, max_value=2**31 - 1),
    module=_TEXT,
    name=_OPTIONAL_TEXT,
    kind=st.sampled_from(("removed_module", "removed_name")),
)

_IMPORT_REPORT: st.SearchStrategy[ImportReport] = st.builds(
    ImportReport,
    broken=st.lists(_BROKEN_IMPORT, max_size=3),
    incomplete=st.booleans(),
    unparsed_files=_TEXTS,
)

_VERIFICATION_REPORT: st.SearchStrategy[VerificationReport] = st.builds(
    VerificationReport,
    tests=_TEST_REPORT,
    static=_STATIC_REPORT,
    plugins=st.lists(_PLUGIN_RESULT, max_size=2),
    sandbox_ms=_COUNT,
    total_ms=_COUNT,
    imports=_IMPORT_REPORT,
)

_RULE_RESULT: st.SearchStrategy[RuleResult] = st.builds(
    RuleResult,
    rule_name=_TEXT,
    matched=st.booleans(),
    would_emit=st.none() | _DECISION,
    reason=_OPTIONAL_TEXT,
)

_EVIDENCE: st.SearchStrategy[Evidence] = st.builds(
    Evidence,
    change=_IMPACT_SET,
    verification=_VERIFICATION_REPORT,
    policy_results=st.lists(_RULE_RESULT, max_size=4),
)

_VERDICT: st.SearchStrategy[Verdict] = st.builds(
    Verdict,
    decision=_DECISION,
    reason=_TEXT,
    matched_rule=_OPTIONAL_TEXT,
    evidence=_EVIDENCE,
    audit_id=st.uuids(),
    created_at=_CREATED_AT,
    warnings=_TEXTS,
    schema_version=st.sampled_from((2, 3)),
)


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(verdict=_VERDICT)
def test_verdict_json_round_trip(verdict: Verdict) -> None:
    """Feature: trikon-engine-fail-safe, Property 13: Verdict JSON round trip.

    Serializing a Verdict with ``model_dump_json`` and parsing the text back
    with ``model_validate_json`` yields a Verdict equal to the original.
    """
    round_tripped = Verdict.model_validate_json(verdict.model_dump_json())

    assert round_tripped == verdict
