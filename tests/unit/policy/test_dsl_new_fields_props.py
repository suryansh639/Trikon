# Feature: trikon-engine-fail-safe, Property 11: New DSL fields resolve to the evidence
"""Property test: the Policy DSL keys added by the fail-safe work read the evidence.

*For any* generated ImpactSet and VerificationReport, and any single-condition
rule on one of the new keys, ``_rule_matches`` equals the direct comparison
against the evidence:

- ``verification.tests.executed {eq|gt|lt: int}`` against
  ``tests.passed + tests.failed`` (Executed_Test_Count), never the stored
  ``tests.executed`` field;
- ``verification.tests.total {eq|gt|lt: int}`` against ``tests.total``;
- ``verification.tests.strategy: <str>`` against ``tests.strategy``;
- ``verification.tests.incomplete: <bool>`` against ``tests.incomplete``;
- ``verification.imports.broken {eq|gt|lt: int}`` against
  ``len(imports.broken)``;
- ``verification.imports.incomplete: <bool>`` against ``imports.incomplete``;
- ``change.python_change: <bool>`` against Python_Change, i.e. whether any
  ``file_changes`` ``path`` / ``old_path`` or ``changed_files`` entry ends in
  ``.py``.

An ``any_of`` over generated sub-conditions (which may themselves hold
``any_of``) equals the disjunction of the sub-conditions' individual results.
The default policy's insufficient-evidence rule shape (Requirement 6.10)
matches exactly when the change is a Python_Change and Executed_Test_Count is
0, the tests are incomplete, or the imports are incomplete.

The oracle below is written from the requirement definitions and does not
reuse the evaluator's operator table or helpers.

The generators keep counts small so ``eq`` thresholds hit often, and draw
the stored ``tests.executed`` independently of ``passed`` / ``failed`` so a
resolver that read the stored field would be caught. Paths include
look-alikes (``.pyi``, ``.py.txt``) that are not Python_Files, and Python
paths can appear only in ``old_path`` or only in ``changed_files``.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.10**
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

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
from trikon.policy.dsl import Rule
from trikon.policy.evaluator import _rule_matches

_RULE_NAME: Final[str] = "property 11 rule"

# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

_OPERATORS: Final[dict[str, Callable[[int, int], bool]]] = {
    "eq": lambda actual, threshold: actual == threshold,
    "gt": lambda actual, threshold: actual > threshold,
    "lt": lambda actual, threshold: actual < threshold,
}


def _compare(op_dict: object, actual: int) -> bool:
    """Apply a single-operator ``{eq|gt|lt: int}`` mapping to ``actual``."""
    assert isinstance(op_dict, dict)
    assert len(op_dict) == 1
    ((op_name, threshold),) = op_dict.items()
    assert isinstance(op_name, str)
    assert isinstance(threshold, int)
    return _OPERATORS[op_name](actual, threshold)


def _touches_python_file(change: ImpactSet) -> bool:
    """Python_Change: some changed path or pre-rename path ends in ``.py``."""
    paths = [file_change.path for file_change in change.file_changes]
    paths.extend(
        file_change.old_path
        for file_change in change.file_changes
        if file_change.old_path is not None
    )
    paths.extend(change.changed_files)
    return any(path.endswith(".py") for path in paths)


def _expected_condition(
    key: str,
    value: object,
    change: ImpactSet,
    verification: VerificationReport,
) -> bool:
    """What one condition should resolve to, read straight off the evidence."""
    tests = verification.tests
    imports = verification.imports
    if key == "verification.tests.executed":
        return _compare(value, tests.passed + tests.failed)
    if key == "verification.tests.total":
        return _compare(value, tests.total)
    if key == "verification.tests.strategy":
        return tests.strategy == value
    if key == "verification.tests.incomplete":
        return tests.incomplete is value
    if key == "verification.imports.broken":
        return _compare(value, len(imports.broken))
    if key == "verification.imports.incomplete":
        return imports.incomplete is value
    if key == "change.python_change":
        return _touches_python_file(change) is value
    assert key == "any_of", f"generator produced an unexpected key {key!r}"
    assert isinstance(value, list)
    return any(_expected_when(conditions, change, verification) for conditions in value)


def _expected_when(
    when: Mapping[str, object],
    change: ImpactSet,
    verification: VerificationReport,
) -> bool:
    """AND over every condition in ``when``."""
    return all(_expected_condition(key, value, change, verification) for key, value in when.items())


def _matches(when: dict[str, object], change: ImpactSet, verification: VerificationReport) -> bool:
    """Evaluate ``when`` through the real evaluator entry point."""
    rule = Rule(name=_RULE_NAME, when=when, then="block", reason="property 11")
    return _rule_matches(rule, change, verification)


# ---------------------------------------------------------------------------
# Evidence strategies
# ---------------------------------------------------------------------------

_PATH: st.SearchStrategy[str] = st.sampled_from(
    (
        # Python_Files.
        "src/app.py",
        "pkg/__init__.py",
        "tests/test_app.py",
        "setup.py",
        # Not Python_Files, including look-alikes.
        "README.md",
        "docs/guide.md",
        "src/app.pyi",
        "notes.py.txt",
        "data/py",
        "pyproject.toml",
    ),
)

# Small counts so ``eq`` thresholds hit often. ``passed`` / ``failed`` are 0
# half the time, so Executed_Test_Count 0 is common.
_SMALL: st.SearchStrategy[int] = st.integers(min_value=0, max_value=5)
_TEST_COUNT: st.SearchStrategy[int] = st.just(0) | st.integers(min_value=1, max_value=5)
_THRESHOLD: st.SearchStrategy[int] = st.integers(min_value=-1, max_value=11)

_FILE_CHANGE: st.SearchStrategy[FileChangeInfo] = st.builds(
    FileChangeInfo,
    path=_PATH,
    change_kind=st.just("renamed"),
    old_path=_PATH,
) | st.builds(
    FileChangeInfo,
    path=_PATH,
    change_kind=st.sampled_from(("added", "modified", "deleted")),
    old_path=st.none(),
)

# ``changed_files`` is drawn independently of ``file_changes`` so a Python
# path can sit in only one of them (a hand-built ImpactSet).
_IMPACT_SET: st.SearchStrategy[ImpactSet] = st.builds(
    ImpactSet,
    changed_files=st.lists(_PATH, max_size=2, unique=True),
    changed_symbols=st.builds(list),
    impacted_modules=st.builds(list),
    impacted_public_apis=st.builds(list),
    impacted_tests=st.builds(list),
    blast_radius_score=st.sampled_from(("LOW", "MEDIUM", "HIGH")),
    blast_radius_numeric=st.floats(min_value=0.0, max_value=100.0),
    file_changes=st.lists(_FILE_CHANGE, max_size=2),
)

_TEST_REPORT: st.SearchStrategy[report.TestReport] = st.builds(
    report.TestReport,
    status=st.sampled_from(("passed", "failed", "skipped")),
    total=st.integers(min_value=0, max_value=10),
    passed=_TEST_COUNT,
    failed=_TEST_COUNT,
    skipped=_SMALL,
    duration_ms=_SMALL,
    collected=_SMALL,
    # Drawn independently of ``passed`` / ``failed`` on purpose: the DSL key
    # must resolve to ``passed + failed``, not to this stored field.
    executed=st.integers(min_value=0, max_value=10),
    strategy=st.sampled_from(("selected", "full_suite", "none")),
    incomplete=st.booleans(),
)

_STATIC_REPORT: st.SearchStrategy[StaticReport] = st.builds(
    StaticReport,
    tools_run=st.lists(st.sampled_from(("ruff", "mypy")), max_size=2, unique=True),
    new_errors=_SMALL,
    new_warnings=_SMALL,
    preexisting_errors=_SMALL,
)

_BROKEN_IMPORT: st.SearchStrategy[BrokenImport] = st.builds(
    BrokenImport,
    path=_PATH,
    line=st.integers(min_value=1, max_value=500),
    module=st.sampled_from(("orders.worker", "pkg.util", "app")),
    name=st.none() | st.sampled_from(("run", "helper")),
    kind=st.sampled_from(("removed_module", "removed_name")),
)

_IMPORT_REPORT: st.SearchStrategy[ImportReport] = st.builds(
    ImportReport,
    broken=st.lists(_BROKEN_IMPORT, max_size=4),
    incomplete=st.booleans(),
    unparsed_files=st.lists(_PATH, max_size=2, unique=True),
)

_VERIFICATION_REPORT: st.SearchStrategy[VerificationReport] = st.builds(
    VerificationReport,
    tests=_TEST_REPORT,
    static=_STATIC_REPORT,
    sandbox_ms=_SMALL,
    total_ms=_SMALL,
    imports=_IMPORT_REPORT,
)

# ---------------------------------------------------------------------------
# Condition strategies
# ---------------------------------------------------------------------------

_INT_KEYS: Final[tuple[str, ...]] = (
    "verification.tests.executed",
    "verification.tests.total",
    "verification.imports.broken",
)
_BOOL_KEYS: Final[tuple[str, ...]] = (
    "verification.tests.incomplete",
    "verification.imports.incomplete",
    "change.python_change",
)

_OP_DICT: st.SearchStrategy[dict[str, int]] = st.dictionaries(
    st.sampled_from(("eq", "gt", "lt")),
    _THRESHOLD,
    min_size=1,
    max_size=1,
)
# The three Test_Strategy values plus strings that must never match.
_STRATEGY_VALUE: st.SearchStrategy[str] = st.sampled_from(
    ("selected", "full_suite", "none", "", "Selected", "full-suite"),
)

_LEAF_CONDITION: st.SearchStrategy[tuple[str, object]] = (
    st.tuples(st.sampled_from(_INT_KEYS), _OP_DICT)
    | st.tuples(st.sampled_from(_BOOL_KEYS), st.booleans())
    | st.tuples(st.just("verification.tests.strategy"), _STRATEGY_VALUE)
)

# A non-empty AND mapping of new-key leaf conditions.
_LEAF_WHEN: st.SearchStrategy[dict[str, object]] = st.lists(
    _LEAF_CONDITION,
    min_size=1,
    max_size=3,
    unique_by=lambda condition: condition[0],
).map(dict)


def _with_any_of(
    children: st.SearchStrategy[dict[str, object]],
) -> st.SearchStrategy[dict[str, object]]:
    """A mapping holding an ``any_of`` of child mappings, maybe next to leaf keys."""
    return st.tuples(
        st.just({}) | _LEAF_WHEN,
        st.lists(children, min_size=1, max_size=3),
    ).map(lambda parts: {**parts[0], "any_of": parts[1]})


# Non-empty mappings that may nest ``any_of`` a few levels deep.
_WHEN: st.SearchStrategy[dict[str, object]] = st.recursive(
    _LEAF_WHEN,
    _with_any_of,
    max_leaves=6,
)

# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(condition=_LEAF_CONDITION, change=_IMPACT_SET, verification=_VERIFICATION_REPORT)
def test_single_new_key_condition_resolves_to_the_evidence(
    condition: tuple[str, object],
    change: ImpactSet,
    verification: VerificationReport,
) -> None:
    """Feature: trikon-engine-fail-safe, Property 11: New DSL fields resolve to the evidence.

    A rule with one condition on a new key matches exactly when the direct
    comparison against the evidence holds.
    """
    key, value = condition

    actual = _matches({key: value}, change, verification)

    assert actual == _expected_condition(key, value, change, verification), (
        f"{key}: {value!r} resolved to {actual}"
    )


@settings(max_examples=100, deadline=None)
@given(
    sub_conditions=st.lists(_WHEN, min_size=1, max_size=4),
    change=_IMPACT_SET,
    verification=_VERIFICATION_REPORT,
)
def test_any_of_is_the_disjunction_of_its_sub_conditions(
    sub_conditions: list[dict[str, object]],
    change: ImpactSet,
    verification: VerificationReport,
) -> None:
    """Feature: trikon-engine-fail-safe, Property 11: New DSL fields resolve to the evidence.

    ``any_of`` matches exactly when at least one of its mappings matches on
    its own, and each mapping (including nested ``any_of``) matches exactly
    when the oracle says so.
    """
    individual = [_matches(conditions, change, verification) for conditions in sub_conditions]

    actual = _matches({"any_of": sub_conditions}, change, verification)

    assert actual == any(individual)
    assert individual == [
        _expected_when(conditions, change, verification) for conditions in sub_conditions
    ]


@settings(max_examples=100, deadline=None)
@given(change=_IMPACT_SET, verification=_VERIFICATION_REPORT)
def test_insufficient_evidence_rule_shape_matches_requirement_6_10(
    change: ImpactSet,
    verification: VerificationReport,
) -> None:
    """Feature: trikon-engine-fail-safe, Property 11: New DSL fields resolve to the evidence.

    The default policy's insufficient-evidence ``when`` (design §10) matches
    exactly when the change is a Python_Change and Executed_Test_Count is 0,
    the TestReport is incomplete, or the ImportReport is incomplete.
    """
    when: dict[str, object] = {
        "change.python_change": True,
        "any_of": [
            {"verification.tests.executed": {"eq": 0}},
            {"verification.tests.incomplete": True},
            {"verification.imports.incomplete": True},
        ],
    }
    tests = verification.tests
    expected = _touches_python_file(change) and (
        tests.passed + tests.failed == 0 or tests.incomplete or verification.imports.incomplete
    )

    assert _matches(when, change, verification) == expected
