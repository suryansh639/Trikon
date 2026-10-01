# Feature: trikon-engine-fail-safe, Property 14: v041-shaped Verdict JSON parses with defaults
"""Property test: previous-release-shaped Verdict JSON parses with defaults.

*For any* generated :class:`~trikon.evidence.report.Verdict`, deleting every
key added by the engine fail-safe work from its JSON (and setting
``schema_version`` to 2) produces a document that parses to a Verdict whose
new fields equal their defaults and whose previous-release fields are
unchanged. The checked-in fixture
``tests/fixtures/verdicts/verdict_0_4_1.json`` (the ``bad_retry`` Verdict
written by the published previous release) parses the same way.

Keys the engine fail-safe work added, and so deleted here:

- ``TestReport``: ``collected``, ``executed``, ``strategy``,
  ``strategy_reasons``, ``incomplete``, ``incomplete_reasons`` and
  ``collection_errors``;
- ``VerificationReport``: ``imports``.

"Unchanged" is checked two ways: the parsed Verdict equals the original with
only the new fields reset to their defaults and ``schema_version`` set to 2,
and dumping the parsed Verdict and deleting the new keys again gives back the
exact document that was parsed (same key names, same values).

**Validates: Requirements 7.4, 7.5, 7.6**
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

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

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "verdicts" / "verdict_0_4_1.json"

# ---------------------------------------------------------------------------
# Previous-release shape
# ---------------------------------------------------------------------------

# Keys each model wrote in the previous release.
_V041_VERDICT_KEYS = frozenset(
    {
        "decision",
        "reason",
        "matched_rule",
        "evidence",
        "audit_id",
        "created_at",
        "warnings",
        "schema_version",
    }
)
_V041_VERIFICATION_KEYS = frozenset({"tests", "static", "plugins", "sandbox_ms", "total_ms"})
_V041_TEST_REPORT_KEYS = frozenset(
    {
        "status",
        "total",
        "passed",
        "failed",
        "skipped",
        "duration_ms",
        "failures",
        "coverage_map_stale",
    }
)

# Every key the engine fail-safe work added, with the default a
# previous-release document must parse to (Requirement 7.5).
_NEW_TEST_REPORT_DEFAULTS: dict[str, object] = {
    "collected": 0,
    "executed": 0,
    "strategy": "selected",
    "strategy_reasons": [],
    "incomplete": False,
    "incomplete_reasons": [],
    "collection_errors": [],
}
_NEW_VERIFICATION_KEYS = frozenset({"imports"})
_V041_SCHEMA_VERSION = 2


def _child(mapping: dict[str, object], key: str) -> dict[str, object]:
    """Return ``mapping[key]`` narrowed to a JSON object."""
    value = mapping[key]
    assert isinstance(value, dict), key
    return {str(k): v for k, v in value.items()}


def _strip_new_keys(document: dict[str, object]) -> dict[str, object]:
    """Return a copy of a Verdict JSON object shaped like a previous-release one.

    Deletes every key the engine fail-safe work added and sets
    ``schema_version`` to 2. The input is left untouched.
    """
    stripped = copy.deepcopy(document)
    stripped["schema_version"] = _V041_SCHEMA_VERSION

    evidence = _child(stripped, "evidence")
    verification = _child(evidence, "verification")
    tests = _child(verification, "tests")
    for key in _NEW_TEST_REPORT_DEFAULTS:
        tests.pop(key, None)
    for key in _NEW_VERIFICATION_KEYS:
        verification.pop(key, None)

    verification["tests"] = tests
    evidence["verification"] = verification
    stripped["evidence"] = evidence
    return stripped


def _as_object(text: str) -> dict[str, object]:
    raw: object = json.loads(text)
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


def _assert_v041_shaped(document: dict[str, object]) -> None:
    """The document carries exactly the previous-release keys, at version 2."""
    evidence = _child(document, "evidence")
    verification = _child(evidence, "verification")
    tests = _child(verification, "tests")

    assert set(document) == _V041_VERDICT_KEYS
    assert set(verification) == _V041_VERIFICATION_KEYS
    assert set(tests) == _V041_TEST_REPORT_KEYS
    assert document["schema_version"] == _V041_SCHEMA_VERSION


def _assert_parses_with_defaults(document: dict[str, object]) -> Verdict:
    """Parse a previous-release-shaped document and check Requirements 7.4/7.5.

    Returns the parsed Verdict so callers can compare it further.
    """
    parsed = Verdict.model_validate_json(json.dumps(document))

    # The stored version is kept, not bumped to the current default.
    assert parsed.schema_version == _V041_SCHEMA_VERSION

    # Every new field takes its default.
    tests = parsed.evidence.verification.tests
    for name, default in _NEW_TEST_REPORT_DEFAULTS.items():
        assert getattr(tests, name) == default, name
    assert parsed.evidence.verification.imports == ImportReport()

    # Every previous-release key keeps its name and value: dumping the parsed
    # Verdict and deleting the new keys again gives back the input exactly.
    redumped = _strip_new_keys(_as_object(parsed.model_dump_json()))
    assert redumped == document

    return parsed


def _with_new_fields_defaulted(verdict: Verdict) -> Verdict:
    """The Verdict a previous-release document of ``verdict`` should parse to."""
    verification = verdict.evidence.verification
    tests = verification.tests.model_copy(update=_NEW_TEST_REPORT_DEFAULTS)
    verification = verification.model_copy(update={"tests": tests, "imports": ImportReport()})
    evidence = verdict.evidence.model_copy(update={"verification": verification})
    return verdict.model_copy(update={"evidence": evidence, "schema_version": _V041_SCHEMA_VERSION})


# ---------------------------------------------------------------------------
# Scalar strategies
# ---------------------------------------------------------------------------

# Any UTF-8-encodable character (no lone surrogates), so quotes, backslashes,
# control characters and non-ASCII text all exercise JSON string escaping.
_TEXT: st.SearchStrategy[str] = st.text(st.characters(codec="utf-8"), max_size=20)
_OPTIONAL_TEXT: st.SearchStrategy[str | None] = st.none() | _TEXT
_TEXTS: st.SearchStrategy[list[str]] = st.lists(_TEXT, max_size=3)

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

# Timezone-aware UTC datetimes with microsecond precision, which JSON carries
# losslessly as ISO 8601 with a ``Z`` suffix.
_CREATED_AT: st.SearchStrategy[datetime] = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2200, 12, 31, 23, 59, 59, 999999),
    timezones=st.just(UTC),
)

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
#
# Every field is passed explicitly, so the new fields usually hold
# non-default values that the stripping step must remove.

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
# Property 14
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(verdict=_VERDICT)
def test_v041_shaped_verdict_json_parses_with_defaults(verdict: Verdict) -> None:
    """Feature: trikon-engine-fail-safe, Property 14: v041-shaped Verdict JSON parses with defaults.

    Stripping the new keys from a generated Verdict's JSON and setting
    ``schema_version`` to 2 gives a document that parses without error, keeps
    version 2, fills every new field with its default and leaves every
    previous-release field as it was.
    """
    document = _strip_new_keys(_as_object(verdict.model_dump_json()))
    _assert_v041_shaped(document)

    parsed = _assert_parses_with_defaults(document)

    assert parsed == _with_new_fields_defaulted(verdict)


def test_v041_fixture_parses_with_defaults() -> None:
    """The checked-in previous-release Verdict passes the same checks as Property 14."""
    document = _as_object(_FIXTURE.read_text(encoding="utf-8"))
    _assert_v041_shaped(document)

    parsed = _assert_parses_with_defaults(document)

    # Spot-check that the stored evidence really came through.
    assert parsed.decision == "require_human"
    assert parsed.evidence.verification.tests.status == "failed"
    assert parsed.evidence.verification.tests.failed == 1
