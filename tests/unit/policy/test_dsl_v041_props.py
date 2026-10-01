# Feature: trikon-engine-fail-safe, Property 12: v041 DSL fields are unchanged
"""Property test: the previous-release DSL keys evaluate exactly as before.

*For any* generated ImpactSet and VerificationReport, and any rule whose
``when`` uses only the keys the previous release (v041) understood, the
current :func:`trikon.policy.evaluator._rule_matches` returns the same
``bool`` as the frozen copy in
:mod:`tests.unit.policy._evaluator_v041_reference`, or both raise
:class:`~trikon.policy.errors.RuleMatchError` with the same message.

The previous-release keys are ``any_path_matches``, ``no_path_matches``,
``change.blast_radius.score``, ``verification.tests.status`` and
``verification.static.new_errors``. The generators cover:

- ``when`` mappings of 0 to 5 of those keys in any order, so the empty
  mapping, AND semantics and the short-circuit order are all exercised;
- for half the rules, malformed values for any key: a non-list or a list
  with a non-string entry for the path keys, a non-string for the two
  literal keys, and for ``new_errors`` a non-mapping, a non-string operator
  key, a non-int threshold, zero or several operators, or an unknown one;
- evidence that the new evaluator must ignore for these keys: the stored
  ``static.new_errors`` (the old code counts ``is_new`` findings instead),
  ``file_changes``, the new TestReport fields and the ImportReport.

The explicit ``@example`` cases pin each previous-release error message, so
every raise site is checked on every run, whatever hypothesis draws.

**Validates: Requirements 6.8**
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from hypothesis import example, given, settings
from hypothesis import strategies as st

from tests.unit.policy import _evaluator_v041_reference as v041_reference

# ``TestReport`` is reached through the module so pytest does not try to
# collect it as a test class from this module's namespace.
from trikon.evidence import report
from trikon.evidence.report import (
    BrokenImport,
    FileChangeInfo,
    ImpactSet,
    ImportReport,
    StaticReport,
    VerificationReport,
)
from trikon.policy import evaluator
from trikon.policy.dsl import Rule
from trikon.policy.errors import RuleMatchError

_RuleMatcher = Callable[[Rule, ImpactSet, VerificationReport], bool]
# ``("matched", bool)`` or ``("RuleMatchError", message)``.
_Outcome = tuple[str, bool | str]

_ANY_PATH: Final = "any_path_matches"
_NO_PATH: Final = "no_path_matches"
_BLAST: Final = "change.blast_radius.score"
_STATUS: Final = "verification.tests.status"
_NEW_ERRORS: Final = "verification.static.new_errors"

# ---------------------------------------------------------------------------
# Evidence strategies
# ---------------------------------------------------------------------------

_PATHS: Final[tuple[str, ...]] = (
    "src/auth/token.py",
    "src/app.py",
    "auth/login.py",
    "src/pkg/__init__.py",
    "tests/test_app.py",
    "docs/guide.md",
    "README.md",
    "migrations/0001_init.sql",
)
_PATH: st.SearchStrategy[str] = st.sampled_from(_PATHS)
_COUNT: st.SearchStrategy[int] = st.integers(min_value=0, max_value=20)

_FILE_CHANGE: st.SearchStrategy[FileChangeInfo] = st.builds(
    FileChangeInfo,
    path=_PATH,
    change_kind=st.sampled_from(("added", "modified", "deleted", "renamed")),
    old_path=st.none() | _PATH,
)

_IMPACT_SET: st.SearchStrategy[ImpactSet] = st.builds(
    ImpactSet,
    changed_files=st.lists(_PATH, max_size=4, unique=True),
    changed_symbols=st.builds(list),
    impacted_modules=st.builds(list),
    impacted_public_apis=st.builds(list),
    impacted_tests=st.builds(list),
    blast_radius_score=st.sampled_from(("LOW", "MEDIUM", "HIGH")),
    blast_radius_numeric=st.floats(min_value=0.0, max_value=100.0),
    # Independent of ``changed_files``: the path keys must read only
    # ``changed_files``, as the previous release did.
    file_changes=st.lists(_FILE_CHANGE, max_size=3),
)

_TEST_REPORT: st.SearchStrategy[report.TestReport] = st.builds(
    report.TestReport,
    status=st.sampled_from(("passed", "failed", "skipped")),
    total=_COUNT,
    passed=_COUNT,
    failed=_COUNT,
    skipped=_COUNT,
    duration_ms=_COUNT,
    coverage_map_stale=st.booleans(),
    collected=_COUNT,
    executed=_COUNT,
    strategy=st.sampled_from(("selected", "full_suite", "none")),
    incomplete=st.booleans(),
)

# ``is_new`` is read with ``bool(f.get("is_new"))``, so truthy and falsy
# non-bool values, ``None`` and a missing key all matter.
_FINDING: st.SearchStrategy[dict[str, object]] = st.fixed_dictionaries(
    {"code": st.sampled_from(("E501", "F401", "attr-defined"))},
    optional={"is_new": st.sampled_from((True, False, 1, 0, None, "yes", "", "false"))},
)

_STATIC_REPORT: st.SearchStrategy[StaticReport] = st.builds(
    StaticReport,
    tools_run=st.lists(st.sampled_from(("ruff", "mypy")), max_size=2, unique=True),
    # Drawn independently of ``findings``: ``new_errors`` counts ``is_new``
    # findings, not this stored field.
    new_errors=_COUNT,
    new_warnings=_COUNT,
    preexisting_errors=_COUNT,
    findings=st.lists(_FINDING, max_size=6),
)

_BROKEN_IMPORT: st.SearchStrategy[BrokenImport] = st.builds(
    BrokenImport,
    path=_PATH,
    line=st.integers(min_value=1, max_value=500),
    module=st.sampled_from(("orders.worker", "pkg.util")),
    name=st.none() | st.just("run"),
    kind=st.sampled_from(("removed_module", "removed_name")),
)

_VERIFICATION_REPORT: st.SearchStrategy[VerificationReport] = st.builds(
    VerificationReport,
    tests=_TEST_REPORT,
    static=_STATIC_REPORT,
    sandbox_ms=_COUNT,
    total_ms=_COUNT,
    imports=st.builds(
        ImportReport,
        broken=st.lists(_BROKEN_IMPORT, max_size=2),
        incomplete=st.booleans(),
    ),
)

# ---------------------------------------------------------------------------
# ``when`` value strategies, well-formed and malformed, per key
# ---------------------------------------------------------------------------

_PATTERNS: Final[tuple[str, ...]] = (
    "auth/**",
    "src/**",
    "**/*.py",
    "*.py",
    "*.md",
    "docs/*",
    "tests/test_*.py",
    "src/auth/*.py",
    "*",
    "migrations/**",
    "vendor/**",
    "README.md",
)
_PATTERN: st.SearchStrategy[str] = st.sampled_from(_PATTERNS)

# Values that are never a string, list or mapping.
_NON_CONTAINER: st.SearchStrategy[object] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-3, max_value=3),
    st.floats(allow_nan=False, allow_infinity=False),
)
_NON_STR: st.SearchStrategy[object] = st.one_of(
    _NON_CONTAINER,
    st.lists(_PATTERN, max_size=2),
    st.dictionaries(st.sampled_from(("eq", "x")), st.integers(0, 3), max_size=2),
)
_NON_LIST: st.SearchStrategy[object] = st.one_of(
    _NON_CONTAINER,
    _PATTERN,
    st.dictionaries(st.sampled_from(("eq", "x")), st.integers(0, 3), max_size=2),
)
_NON_DICT: st.SearchStrategy[object] = st.one_of(
    _NON_CONTAINER,
    st.sampled_from(("eq", "gt 0")),
    st.lists(st.integers(0, 3), max_size=2),
)

_PATHS_VALUE: st.SearchStrategy[object] = st.lists(_PATTERN, max_size=4)


@st.composite
def _paths_with_bad_entry(draw: st.DrawFn) -> list[object]:
    """A pattern list with one non-string entry at a random position."""
    patterns: list[object] = list(draw(st.lists(_PATTERN, max_size=3)))
    index = draw(st.integers(min_value=0, max_value=len(patterns)))
    patterns.insert(index, draw(_NON_CONTAINER | st.lists(_PATTERN, max_size=1)))
    return patterns


_BAD_PATHS_VALUE: st.SearchStrategy[object] = _NON_LIST | _paths_with_bad_entry()

_BLAST_VALUE: st.SearchStrategy[object] = st.sampled_from(("LOW", "MEDIUM", "HIGH", "low", ""))
_STATUS_VALUE: st.SearchStrategy[object] = st.sampled_from(
    ("passed", "failed", "skipped", "errored", "PASSED", "")
)

_OPERATORS: Final[tuple[str, ...]] = ("eq", "gt", "lt")
_UNKNOWN_OPERATORS: Final[tuple[str, ...]] = ("ge", "le", "ne", "EQ", "==", "")
# ``bool`` is an ``int`` subclass, so both evaluators accept it as a threshold.
_THRESHOLD: st.SearchStrategy[int] = st.integers(min_value=-1, max_value=8) | st.booleans()

_NEW_ERRORS_VALUE: st.SearchStrategy[object] = st.dictionaries(
    st.sampled_from(_OPERATORS), _THRESHOLD, min_size=1, max_size=1
)


@st.composite
def _bad_operator_dict(draw: st.DrawFn) -> dict[object, object]:
    """An operator mapping that the previous release rejected.

    Either the outer shape is wrong (a non-string key or a non-int threshold
    somewhere in the mapping), or the shape is right but the mapping has zero
    operators, several, or one unknown operator.
    """
    kind = draw(st.sampled_from(("bad_key", "bad_threshold", "count", "unknown")))
    if kind == "count":
        size = draw(st.sampled_from((0, 2, 3)))
        ops = draw(st.lists(st.sampled_from(_OPERATORS), min_size=size, max_size=size, unique=True))
        return {op: draw(_THRESHOLD) for op in ops}
    if kind == "unknown":
        return {draw(st.sampled_from(_UNKNOWN_OPERATORS)): draw(_THRESHOLD)}
    entries: list[tuple[object, object]] = [
        (op, draw(_THRESHOLD))
        for op in draw(st.lists(st.sampled_from(_OPERATORS), max_size=2, unique=True))
    ]
    bad: tuple[object, object]
    if kind == "bad_key":
        bad = (draw(st.integers(min_value=0, max_value=3) | st.none()), draw(_THRESHOLD))
    else:
        bad = (
            draw(st.sampled_from(_OPERATORS + _UNKNOWN_OPERATORS)),
            draw(st.none() | st.floats(allow_nan=False) | st.sampled_from(("1", "zero"))),
        )
    # A repeated operator key would overwrite an earlier one, so drop it first.
    entries = [entry for entry in entries if entry[0] != bad[0]]
    entries.insert(draw(st.integers(min_value=0, max_value=len(entries))), bad)
    return dict(entries)


_BAD_NEW_ERRORS_VALUE: st.SearchStrategy[object] = _NON_DICT | _bad_operator_dict()

# key -> (well-formed values, malformed values)
_VALUES: Final[dict[str, tuple[st.SearchStrategy[object], st.SearchStrategy[object]]]] = {
    _ANY_PATH: (_PATHS_VALUE, _BAD_PATHS_VALUE),
    _NO_PATH: (_PATHS_VALUE, _BAD_PATHS_VALUE),
    _BLAST: (_BLAST_VALUE, _NON_STR),
    _STATUS: (_STATUS_VALUE, _NON_STR),
    _NEW_ERRORS: (_NEW_ERRORS_VALUE, _BAD_NEW_ERRORS_VALUE),
}
_V041_KEYS: Final[tuple[str, ...]] = tuple(_VALUES)


@st.composite
def _when(draw: st.DrawFn) -> dict[str, object]:
    """A ``when`` mapping of previous-release keys in a random order.

    Half the mappings use only well-formed values, so whole rules match and
    fail on the evidence. In the other half each value is malformed half the
    time, so error paths come after matching and non-matching conditions.
    """
    keys = draw(st.lists(st.sampled_from(_V041_KEYS), max_size=len(_V041_KEYS), unique=True))
    allow_malformed = draw(st.booleans())
    when: dict[str, object] = {}
    for key in keys:
        well_formed, malformed = _VALUES[key]
        when[key] = draw(well_formed | malformed if allow_malformed else well_formed)
    return when


_RULE: st.SearchStrategy[Rule] = st.builds(
    Rule,
    name=st.sampled_from(("sensitive path requires human", "green, low-blast auto-allow", "r")),
    when=_when(),
    then=st.sampled_from(("allow", "block", "require_human", "warn")),
    reason=st.none() | st.just("because"),
)

# ---------------------------------------------------------------------------
# Fixed evidence for the explicit examples
# ---------------------------------------------------------------------------

_CHANGE: Final = ImpactSet(
    changed_files=["src/auth/token.py", "README.md"],
    changed_symbols=[],
    impacted_modules=[],
    impacted_public_apis=[],
    impacted_tests=[],
    blast_radius_score="MEDIUM",
    blast_radius_numeric=9.5,
)
_VERIFICATION: Final = VerificationReport(
    tests=report.TestReport(
        status="failed", total=3, passed=2, failed=1, skipped=0, duration_ms=10
    ),
    static=StaticReport(
        tools_run=["ruff"],
        new_errors=0,
        new_warnings=0,
        preexisting_errors=0,
        findings=[{"is_new": True}, {"is_new": 0}, {}],
    ),
    sandbox_ms=1,
    total_ms=2,
)


# One ``when`` per previous-release raise site and per match outcome, run
# against the fixed evidence above.
_EXAMPLE_WHENS: Final[tuple[Mapping[str, object], ...]] = (
    {},  # unconditional match
    {_ANY_PATH: ["auth/**"], _BLAST: "MEDIUM", _NEW_ERRORS: {"eq": 1}},  # all three hold
    {_NO_PATH: ["*.md"], _NEW_ERRORS: {}},  # False before the bad mapping is read
    {_STATUS: "passed", _BLAST: "LOW"},  # first condition fails
    {_ANY_PATH: "auth/**"},  # not a list
    {_NO_PATH: ["docs/*", 3]},  # non-string entry
    {_BLAST: 2},  # not a string
    {_STATUS: None},  # not a string
    {_NEW_ERRORS: [1]},  # not a mapping
    {_NEW_ERRORS: {1: 0}},  # non-string operator key
    {_NEW_ERRORS: {"gt": "0"}},  # non-int threshold
    {_NEW_ERRORS: {"gt": 0, "lt": 3}},  # more than one operator
    {_NEW_ERRORS: {"ge": 0}},  # unknown operator
)


def _rule(when: Mapping[str, object]) -> Rule:
    return Rule(name="example", when=dict(when), then="block")


_Test = Callable[[Rule, ImpactSet, VerificationReport], None]


def _with_fixed_examples(test: _Test) -> _Test:
    """Attach one hypothesis ``@example`` per entry of ``_EXAMPLE_WHENS``."""
    for when in _EXAMPLE_WHENS:
        test = example(rule=_rule(when), change=_CHANGE, verification=_VERIFICATION)(test)
    return test


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------


def _outcome(
    matcher: _RuleMatcher, rule: Rule, change: ImpactSet, verification: VerificationReport
) -> _Outcome:
    """Run ``matcher`` and reduce the result to something comparable.

    Any exception other than ``RuleMatchError`` propagates and fails the test.
    """
    try:
        matched = matcher(rule, change, verification)
    except RuleMatchError as exc:
        return ("RuleMatchError", str(exc))
    assert type(matched) is bool, f"{matcher.__module__} returned {matched!r}"
    return ("matched", matched)


@settings(max_examples=100, deadline=None)
@given(rule=_RULE, change=_IMPACT_SET, verification=_VERIFICATION_REPORT)
@_with_fixed_examples
def test_v041_keys_match_the_previous_release(
    rule: Rule, change: ImpactSet, verification: VerificationReport
) -> None:
    """Feature: trikon-engine-fail-safe, Property 12: v041 DSL fields are unchanged.

    The current ``_rule_matches`` gives the same ``bool``, or raises
    ``RuleMatchError`` with the same message, as the previous release's.
    """
    expected = _outcome(v041_reference._rule_matches, rule, change, verification)
    actual = _outcome(evaluator._rule_matches, rule, change, verification)

    assert actual == expected
