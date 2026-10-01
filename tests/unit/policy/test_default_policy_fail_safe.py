"""Unit tests for the fail-safe rules in the packaged default policy.

The engine fail-safe work adds two rules to ``trikon/policy/default_policy.yaml``
(design §10):

- ``broken static imports`` (rule 1) blocks a change that leaves a static
  import of a removed module or name (Requirement 6.9);
- ``insufficient test evidence requires human`` (rule 6, before the allow
  rule) sends a Python change to a human when Executed_Test_Count is 0, the
  test evidence is incomplete or the import analysis is incomplete
  (Requirement 6.10).

The five previous-release rules and the ``require_human`` fall-through keep
their definitions. ``sensitive path requires human`` moves after the three
evidence block rules, so a sensitive change that fails its tests blocks
(Requirements 6.11, 8.3, 8.4). The last group pins the ``RuleMatchError``
messages the evaluator raises when the new rule's ``any_of`` is edited into a
malformed shape.

**Validates: Requirements 6.9, 6.10, 6.11**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

import pytest

# ``TestReport`` and ``TestStrategy`` are reached through the module so pytest
# does not try to collect them as test classes from this module's namespace.
from trikon.evidence import report
from trikon.evidence.report import (
    BlastBucket,
    BrokenImport,
    FileChangeInfo,
    ImpactSet,
    ImportReport,
    StaticReport,
    Verdict,
    VerificationReport,
)
from trikon.policy.dsl import Policy, Rule
from trikon.policy.errors import RuleMatchError
from trikon.policy.evaluator import evaluate_policy
from trikon.policy.loader import default_policy

# ---------------------------------------------------------------------------
# Rule names and definitions
# ---------------------------------------------------------------------------

_BROKEN_IMPORTS: Final = "broken static imports"
_TESTS_FAILED: Final = "impacted tests failed"
_STATIC_ERRORS: Final = "new static-analysis errors"
_SENSITIVE: Final = "sensitive path requires human"
_LARGE_BLAST: Final = "large blast radius requires human"
_INSUFFICIENT: Final = "insufficient test evidence requires human"
_AUTO_ALLOW: Final = "green, low-blast auto-allow"
_DEFAULT: Final = "default"

_RULE_ORDER: Final = (
    _BROKEN_IMPORTS,
    _TESTS_FAILED,
    _STATIC_ERRORS,
    _SENSITIVE,
    _LARGE_BLAST,
    _INSUFFICIENT,
    _AUTO_ALLOW,
    _DEFAULT,
)

# (name, when, then, reason) for each rule the previous release shipped,
# copied from its default policy.
_RuleShape = tuple[str, dict[str, object], str, str]
_PREVIOUS_RELEASE_RULES: Final[tuple[_RuleShape, ...]] = (
    (
        _SENSITIVE,
        {"any_path_matches": ["auth/**", "billing/**", "payments/**", "**/migrations/**"]},
        "require_human",
        "Change touches a sensitive subsystem.",
    ),
    (
        _TESTS_FAILED,
        {"verification.tests.status": "failed"},
        "block",
        "One or more impacted tests failed.",
    ),
    (
        _STATIC_ERRORS,
        {"verification.static.new_errors": {"gt": 0}},
        "block",
        "Change introduced new static-analysis errors.",
    ),
    (
        _LARGE_BLAST,
        {"change.blast_radius.score": "HIGH"},
        "require_human",
        "Blast radius is HIGH; a human should review scope.",
    ),
    (
        _AUTO_ALLOW,
        {
            "verification.tests.status": "passed",
            "verification.static.new_errors": {"eq": 0},
            "change.blast_radius.score": "LOW",
        },
        "allow",
        "Tests passed, no new static errors, small blast radius.",
    ),
    (_DEFAULT, {}, "require_human", "No prior rule matched; defaulting to human review."),
)

_BROKEN_IMPORTS_REASON: Final = "Change leaves static imports of removed modules or names."
_INSUFFICIENT_REASON: Final = "Python change without complete test or import evidence."

# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------

_BLAST_NUMERIC: Final[dict[BlastBucket, float]] = {"LOW": 4.5, "MEDIUM": 9.5, "HIGH": 30.0}

_WORKER_IMPORT: Final = BrokenImport(
    path="tests/test_worker.py",
    line=6,
    module="orders.worker",
    kind="removed_module",
)


def _change(
    changed_files: Sequence[str] = ("src/orders/service.py",),
    *,
    blast: BlastBucket = "LOW",
    file_changes: Sequence[FileChangeInfo] | None = None,
) -> ImpactSet:
    """Build an ImpactSet; by default one modified Python file, LOW blast radius."""
    if file_changes is None:
        file_changes = [FileChangeInfo(path=path, change_kind="modified") for path in changed_files]
    return ImpactSet(
        changed_files=list(changed_files),
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score=blast,
        blast_radius_numeric=_BLAST_NUMERIC[blast],
        file_changes=list(file_changes),
    )


def _verification(
    *,
    passed: int = 3,
    failed: int = 0,
    executed: int | None = None,
    status: Literal["passed", "failed", "skipped"] | None = None,
    strategy: report.TestStrategy = "full_suite",
    tests_incomplete: bool = False,
    broken: Sequence[BrokenImport] = (),
    imports_incomplete: bool = False,
) -> VerificationReport:
    """Build a VerificationReport; by default complete, green evidence.

    ``status`` defaults to what the runner derives from the outcomes.
    ``executed`` sets the stored field, which the DSL does not read.
    """
    if status is None:
        status = "failed" if failed else "passed" if passed else "skipped"
    tests = report.TestReport(
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        skipped=0,
        duration_ms=10,
        collected=passed + failed,
        executed=passed + failed if executed is None else executed,
        strategy=strategy,
        incomplete=tests_incomplete,
        incomplete_reasons=["execution_timeout"] if tests_incomplete else [],
    )
    return VerificationReport(
        tests=tests,
        static=StaticReport(
            tools_run=["ruff", "mypy"], new_errors=0, new_warnings=0, preexisting_errors=0
        ),
        sandbox_ms=5,
        total_ms=20,
        imports=ImportReport(
            broken=list(broken),
            incomplete=imports_incomplete,
            unparsed_files=["src/orders/legacy.py"] if imports_incomplete else [],
        ),
    )


def _evaluate(change: ImpactSet, verification: VerificationReport) -> Verdict:
    """Evaluate the packaged default policy against the evidence."""
    return evaluate_policy(default_policy(), change, verification)


def _matched(verdict: Verdict) -> dict[str, bool]:
    """Map each rule name to whether it matched. The evaluator records every rule."""
    return {result.rule_name: result.matched for result in verdict.evidence.policy_results}


def _rule(policy: Policy, name: str) -> Rule:
    """Return the rule called ``name``."""
    return next(rule for rule in policy.rules if rule.name == name)


# ---------------------------------------------------------------------------
# Rule list
# ---------------------------------------------------------------------------


def test_rules_are_in_the_documented_order() -> None:
    assert tuple(rule.name for rule in default_policy().rules) == _RULE_ORDER


def test_broken_import_rule_blocks_on_any_broken_import() -> None:
    rule = _rule(default_policy(), _BROKEN_IMPORTS)

    assert rule.when == {"verification.imports.broken": {"gt": 0}}
    assert rule.then == "block"
    assert rule.reason == _BROKEN_IMPORTS_REASON


def test_insufficient_evidence_rule_requires_human_on_any_gap() -> None:
    rule = _rule(default_policy(), _INSUFFICIENT)

    assert rule.when == {
        "change.python_change": True,
        "any_of": [
            {"verification.tests.executed": {"eq": 0}},
            {"verification.tests.incomplete": True},
            {"verification.imports.incomplete": True},
        ],
    }
    assert rule.then == "require_human"
    assert rule.reason == _INSUFFICIENT_REASON


@pytest.mark.parametrize(
    ("name", "when", "then", "reason"),
    _PREVIOUS_RELEASE_RULES,
    ids=[shape[0] for shape in _PREVIOUS_RELEASE_RULES],
)
def test_previous_release_rule_is_kept(
    name: str,
    when: dict[str, object],
    then: str,
    reason: str,
) -> None:
    rule = _rule(default_policy(), name)

    assert (rule.when, rule.then, rule.reason) == (when, then, reason)


def test_default_rule_is_last_and_requires_human() -> None:
    last = default_policy().rules[-1]

    assert last.name == _DEFAULT
    assert last.when == {}
    assert last.then == "require_human"


# ---------------------------------------------------------------------------
# Broken-import rule (Requirement 6.9)
# ---------------------------------------------------------------------------


def test_broken_import_blocks_a_green_low_blast_change() -> None:
    verdict = _evaluate(_change(), _verification(broken=(_WORKER_IMPORT,)))

    assert verdict.decision == "block"
    assert verdict.matched_rule == _BROKEN_IMPORTS
    assert verdict.reason == _BROKEN_IMPORTS_REASON
    # Without rule 1 the change would have been allowed.
    assert _matched(verdict)[_AUTO_ALLOW] is True


def test_broken_import_on_a_sensitive_path_blocks_before_human_review() -> None:
    verdict = _evaluate(
        _change(("src/payments/retry.py",)),
        _verification(broken=(_WORKER_IMPORT,)),
    )

    assert verdict.decision == "block"
    assert verdict.matched_rule == _BROKEN_IMPORTS
    assert _matched(verdict)[_SENSITIVE] is True


# ---------------------------------------------------------------------------
# Insufficient-evidence rule (Requirement 6.10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verification",
    [
        # Status ``passed`` keeps the allow rule matching, so only rule 6 stands
        # between the gap and ``allow``.
        pytest.param(_verification(passed=0, status="passed"), id="zero_executed"),
        # Executed_Test_Count is passed + failed, not the stored ``executed``.
        pytest.param(
            _verification(passed=0, executed=3, status="passed"),
            id="zero_outcomes_despite_stored_executed",
        ),
        pytest.param(_verification(tests_incomplete=True), id="tests_incomplete"),
        pytest.param(_verification(imports_incomplete=True), id="imports_incomplete"),
    ],
)
def test_each_evidence_gap_on_a_green_python_change_requires_human(
    verification: VerificationReport,
) -> None:
    verdict = _evaluate(_change(), verification)

    assert verdict.decision == "require_human"
    assert verdict.matched_rule == _INSUFFICIENT
    assert verdict.reason == _INSUFFICIENT_REASON
    matched = _matched(verdict)
    assert matched[_AUTO_ALLOW] is True
    assert not any(matched[name] for name in _RULE_ORDER[:5])


def test_python_file_renamed_away_still_counts_as_a_python_change() -> None:
    change = _change(
        ("src/orders/worker.txt",),
        file_changes=[
            FileChangeInfo(
                path="src/orders/worker.txt",
                change_kind="renamed",
                old_path="src/orders/worker.py",
            )
        ],
    )

    verdict = _evaluate(change, _verification(passed=0, status="passed"))

    assert verdict.decision == "require_human"
    assert verdict.matched_rule == _INSUFFICIENT


def test_complete_evidence_on_a_green_low_python_change_allows() -> None:
    verdict = _evaluate(_change(), _verification())

    assert verdict.decision == "allow"
    assert verdict.matched_rule == _AUTO_ALLOW
    matched = _matched(verdict)
    assert matched[_BROKEN_IMPORTS] is False
    assert matched[_INSUFFICIENT] is False


def test_non_python_change_without_tests_allows() -> None:
    # What the runner reports for strategy ``none``: status passed, every count 0.
    verdict = _evaluate(
        _change(("README.md", "docs/guide.md")),
        _verification(passed=0, status="passed", strategy="none"),
    )

    assert verdict.decision == "allow"
    assert verdict.matched_rule == _AUTO_ALLOW
    assert _matched(verdict)[_INSUFFICIENT] is False


# ---------------------------------------------------------------------------
# Previous-release routing with the new order (Requirements 6.11, 8.3, 8.4)
# ---------------------------------------------------------------------------


def test_failed_tests_on_a_sensitive_path_block() -> None:
    # The ``bad_retry`` shape: the only change is under ``src/payments/``.
    verdict = _evaluate(
        _change(("src/payments/retry.py",), blast="MEDIUM"),
        _verification(passed=2, failed=1),
    )

    assert verdict.decision == "block"
    assert verdict.matched_rule == _TESTS_FAILED
    assert _matched(verdict)[_SENSITIVE] is True


def test_green_sensitive_change_requires_human() -> None:
    verdict = _evaluate(_change(("src/auth/token.py",)), _verification())

    assert verdict.decision == "require_human"
    assert verdict.matched_rule == _SENSITIVE
    assert _matched(verdict)[_AUTO_ALLOW] is True


def test_green_medium_blast_change_falls_through_to_default() -> None:
    verdict = _evaluate(_change(blast="MEDIUM"), _verification())

    assert verdict.decision == "require_human"
    assert verdict.matched_rule == _DEFAULT


# ---------------------------------------------------------------------------
# ``any_of`` shape errors
# ---------------------------------------------------------------------------


def _policy_with(key: str, value: object) -> Policy:
    """The default policy with ``key`` in the insufficient-evidence rule set to ``value``."""
    policy = default_policy()
    _rule(policy, _INSUFFICIENT).when[key] = value
    return policy


_LIST_ERROR: Final = "any_of expects a non-empty list of condition mappings"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(
            {"verification.tests.incomplete": True}, f"{_LIST_ERROR}, got dict", id="mapping"
        ),
        pytest.param("verification.tests.incomplete", f"{_LIST_ERROR}, got str", id="string"),
        pytest.param(None, f"{_LIST_ERROR}, got NoneType", id="null"),
        pytest.param([], f"{_LIST_ERROR}, got an empty list", id="empty_list"),
        pytest.param(
            ["verification.tests.incomplete"],
            "any_of[0] expects a condition mapping, got str",
            id="string_entry",
        ),
        pytest.param(
            [{"verification.tests.incomplete": True}, [{"verification.imports.incomplete": True}]],
            "any_of[1] expects a condition mapping, got list",
            id="list_entry",
        ),
        pytest.param(
            [{}],
            "any_of[0] expects a non-empty condition mapping, got an empty mapping",
            id="empty_mapping_entry",
        ),
        pytest.param(
            [{1: True}],
            "any_of[0] expects string condition keys, got a int key",
            id="int_condition_key",
        ),
        pytest.param(
            [{"verification.tests.incomplete": 1}],
            "verification.tests.incomplete expects a boolean, got int",
            id="bool_key_given_1",
        ),
        pytest.param(
            [{"verification.imports.incomplete": 0}],
            "verification.imports.incomplete expects a boolean, got int",
            id="bool_key_given_0",
        ),
        pytest.param(
            [{"verification.tests.bogus": True}],
            f"unknown condition key: 'verification.tests.bogus' in rule {_INSUFFICIENT!r}",
            id="unknown_nested_key",
        ),
    ],
)
def test_malformed_any_of_raises_rule_match_error(value: object, message: str) -> None:
    policy = _policy_with("any_of", value)

    with pytest.raises(RuleMatchError) as excinfo:
        evaluate_policy(policy, _change(), _verification())

    assert str(excinfo.value) == message


def test_any_of_shape_is_checked_before_the_first_match_short_circuits() -> None:
    # Entry 0 matches this evidence, yet the empty entry 1 still raises.
    policy = _policy_with("any_of", [{"verification.tests.incomplete": True}, {}])

    with pytest.raises(RuleMatchError) as excinfo:
        evaluate_policy(policy, _change(), _verification(tests_incomplete=True))

    assert str(excinfo.value) == (
        "any_of[1] expects a non-empty condition mapping, got an empty mapping"
    )


@pytest.mark.parametrize("value", [1, 0])
def test_python_change_flag_rejects_integers(value: int) -> None:
    policy = _policy_with("change.python_change", value)

    with pytest.raises(RuleMatchError) as excinfo:
        evaluate_policy(policy, _change(), _verification())

    assert str(excinfo.value) == "change.python_change expects a boolean, got int"
