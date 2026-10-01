"""Unit tests for the Safety_Floor and fail-closed paths of :func:`trikon.sdk.verify`.

Covers Task 7.8 in ``.kiro/specs/trikon-engine-fail-safe/tasks.md``:

* the floor runs after the policy and before the audit write, so the single
  audit row holds the floored decision (Requirements 5.1, 5.11);
* an allow-all Custom_Policy is floored like any other policy
  (Requirement 5.10);
* :class:`ImportCheckError` and :class:`CollectionPassError` each fail closed
  to ``require_human`` with exactly one audit row (Requirement 7.3);
* every emitted Verdict, and its stored JSON, has ``schema_version == 3``
  (Requirement 7.8);
* the ImportReport survives the fail-closed path when ``run_verification``
  raises after ``check_imports`` finished (Requirement 4.13).

Each test builds a real git repository under ``tmp_path`` and runs the real
``parse_diff``, ``compute_impact``, ``check_imports``, policy loader,
evaluator, floor and audit writer. Only ``run_verification`` is replaced
(there is no Docker on the Windows gate), plus ``check_imports`` in the one
test that needs it to raise. Both are patched as attributes of the
:mod:`trikon.sdk` module, because :func:`~trikon.sdk.verify` calls the names
it imported at load time. The module is loaded with
:func:`importlib.import_module`: the ``trikon.verify`` package attribute is
the SDK function, which shadows the subpackage in dotted-string targets.

Git runs with identity, signing and line-ending settings passed per command,
so the tests never depend on (or touch) global git config. Files are written
as bytes with explicit ``\\n`` terminators.

_Validates: Requirements 5.1, 5.10, 5.11, 7.3, 7.8._
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from trikon.change_intel.errors import ImportCheckError
from trikon.change_intel.models import ChangeSet
from trikon.evidence import report
from trikon.evidence.report import (
    EMPTY_IMPACT_SET,
    EMPTY_VERIFICATION,
    BrokenImport,
    ImpactSet,
    ImportReport,
    StaticReport,
    Verdict,
    VerificationReport,
)
from trikon.exceptions import TrikonError
from trikon.policy.floor import FLOOR_BROKEN_IMPORTS, FLOOR_INSUFFICIENT_EVIDENCE
from trikon.verify.errors import CollectionPassError

_SDK: Final = importlib.import_module("trikon.sdk")
"""The :mod:`trikon.sdk` module object whose attributes the tests patch."""

_GIT_OPTIONS: Final = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "init.defaultBranch=main",
)
"""Per-command git settings, so global config can neither break nor leak in."""

_ALLOW_RULE: Final = "allow everything"
"""The only rule of the allow-all Custom_Policy."""

_ALLOW_ALL_POLICY: Final = (
    "version: 1\n"
    "rules:\n"
    f'  - name: "{_ALLOW_RULE}"\n'
    "    then: allow\n"
    '    reason: "Custom policy allows every change."\n'
).encode()
"""A Custom_Policy whose single unconditional rule allows every change."""

_CLEAN_STATIC: Final = StaticReport(
    tools_run=["ruff", "mypy"],
    new_errors=0,
    new_warnings=0,
    preexisting_errors=0,
)

_GREEN_TESTS: Final = report.TestReport(
    status="passed",
    total=2,
    passed=2,
    failed=0,
    skipped=0,
    duration_ms=5,
    collected=2,
    executed=2,
    strategy="full_suite",
    strategy_reasons=["coverage_map_missing"],
)
"""Complete test evidence: two executed tests, both passed."""

_NO_TESTS: Final = report.TestReport(
    status="passed",
    total=0,
    passed=0,
    failed=0,
    skipped=0,
    duration_ms=0,
)
"""The previous release's fail-open shape: ``passed`` with nothing executed."""

_HELPER_BROKEN_IMPORT: Final = BrokenImport(
    path="tests/test_helper.py",
    line=1,
    module="pkg.helper",
    name="helper",
    kind="removed_module",
)
"""What the real Import_Checker reports once ``pkg/helper.py`` is deleted."""


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Repo:
    """A two-commit tmp git repository plus the paths the SDK call needs."""

    path: Path
    base_sha: str
    head_sha: str
    state_db: Path
    policy: Path


def _git(repo: Path, *args: str) -> str:
    """Run ``git *args`` in ``repo`` and return stdout; fail loudly on error."""
    result = subprocess.run(
        ["git", *_GIT_OPTIONS, *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def _write(repo: Path, rel: str, content: bytes) -> None:
    """Write ``content`` to ``repo / rel`` as raw bytes, creating parents."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _py(*lines: str) -> bytes:
    """Return ``lines`` joined with explicit ``\\n`` terminators, UTF-8 encoded."""
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _commit(repo: Path, message: str) -> str:
    """Stage everything in ``repo``, commit, and return the new HEAD SHA."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _make_repo(tmp_path: Path, *, delete_helper: bool) -> _Repo:
    """Build the base commit, then a head commit with one Python change.

    The base holds ``pkg/core.py``, ``pkg/helper.py`` and a test that does
    ``from pkg.helper import helper``. The head commit either deletes
    ``pkg/helper.py`` (leaving that import broken) or makes a clean one-line
    edit to ``pkg/core.py``. The state DB and the allow-all policy live
    outside the repository, so neither shows up in the diff.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "pkg/__init__.py", b"")
    _write(repo, "pkg/core.py", _py("VALUE = 1"))
    _write(repo, "pkg/helper.py", _py("def helper() -> int:", "    return 1"))
    _write(
        repo,
        "tests/test_helper.py",
        _py(
            "from pkg.helper import helper",
            "",
            "",
            "def test_helper() -> None:",
            "    assert helper() == 1",
        ),
    )
    base_sha = _commit(repo, "base")
    if delete_helper:
        (repo / "pkg" / "helper.py").unlink()
    else:
        _write(repo, "pkg/core.py", _py("VALUE = 2"))
    head_sha = _commit(repo, "head")

    state_db = tmp_path / "state" / "state.db"
    state_db.parent.mkdir()
    policy = tmp_path / "allow_all.yaml"
    policy.write_bytes(_ALLOW_ALL_POLICY)
    return _Repo(repo, base_sha, head_sha, state_db, policy)


def _verify(repo: _Repo) -> Verdict:
    """Run :func:`trikon.sdk.verify` on ``repo`` with the allow-all policy."""
    verdict: Verdict = _SDK.verify(
        repo.path,
        base_sha=repo.base_sha,
        head_sha=repo.head_sha,
        policy_path=repo.policy,
        cache_db=repo.state_db,
    )
    return verdict


# ---------------------------------------------------------------------------
# Fake verification runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunnerCall:
    """The SDK-supplied arguments one ``run_verification`` call received."""

    base_sha: str | None
    head_sha: str | None
    imports: ImportReport | None


@dataclass
class _FakeRunner:
    """Stand-in for ``run_verification``: returns ``tests`` or raises ``error``.

    The keyword-only signature mirrors the exact call ``sdk.verify`` makes, so
    a drift in that contract fails the test with a ``TypeError``. Like the
    real runner, it embeds ``imports or ImportReport()`` in its report.
    """

    tests: report.TestReport = field(default_factory=lambda: _GREEN_TESTS)
    error: TrikonError | None = None
    calls: list[_RunnerCall] = field(default_factory=list)

    def __call__(
        self,
        repo_path: Path,
        impact: ImpactSet,
        *,
        state_db: Path | None,
        no_sandbox: bool,
        base_sha: str | None,
        head_sha: str | None,
        imports: ImportReport | None,
    ) -> VerificationReport:
        self.calls.append(_RunnerCall(base_sha, head_sha, imports))
        if self.error is not None:
            raise self.error
        return VerificationReport(
            tests=self.tests,
            static=_CLEAN_STATIC,
            sandbox_ms=1,
            total_ms=1,
            imports=imports or ImportReport(),
        )


def _install_runner(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner) -> _FakeRunner:
    """Patch ``trikon.sdk.run_verification`` with ``runner`` and return it."""
    monkeypatch.setattr(_SDK, "run_verification", runner)
    return runner


# ---------------------------------------------------------------------------
# Audit-log helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AuditRow:
    """One ``audit_log`` row, typed."""

    audit_id: str
    decision: str
    matched_rule: str | None
    reason: str
    verdict_json: str


def _audit_rows(state_db: Path) -> list[_AuditRow]:
    """Return every ``audit_log`` row in ``state_db``, in insertion order."""
    conn = sqlite3.connect(str(state_db))
    try:
        raw_rows: list[tuple[object, ...]] = conn.execute(
            "SELECT audit_id, decision, matched_rule, reason, verdict_json "
            "FROM audit_log ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    rows: list[_AuditRow] = []
    for audit_id, decision, matched_rule, reason, verdict_json in raw_rows:
        assert isinstance(audit_id, str)
        assert isinstance(decision, str)
        assert matched_rule is None or isinstance(matched_rule, str)
        assert isinstance(reason, str)
        assert isinstance(verdict_json, str)
        rows.append(_AuditRow(audit_id, decision, matched_rule, reason, verdict_json))
    return rows


def _single_audit_row(state_db: Path, verdict: Verdict) -> _AuditRow:
    """Assert ``state_db`` holds exactly one row, recording ``verdict`` losslessly."""
    rows = _audit_rows(state_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.audit_id == str(verdict.audit_id)
    assert row.decision == verdict.decision
    assert row.matched_rule == verdict.matched_rule
    assert row.reason == verdict.reason
    assert Verdict.model_validate_json(row.verdict_json) == verdict
    return row


# ---------------------------------------------------------------------------
# Safety_Floor before the audit write, under an allow-all Custom_Policy
# ---------------------------------------------------------------------------


def test_allow_all_policy_allows_complete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with complete evidence the floor leaves the custom ``allow`` alone.

    Shows the Custom_Policy really is the one evaluated, and that the SDK
    threads the caller's SHAs and the ImportReport into the runner.
    """
    repo = _make_repo(tmp_path, delete_helper=False)
    runner = _install_runner(monkeypatch, _FakeRunner(tests=_GREEN_TESTS))

    verdict = _verify(repo)

    assert verdict.decision == "allow"
    assert verdict.matched_rule == _ALLOW_RULE
    assert [r.rule_name for r in verdict.evidence.policy_results] == [_ALLOW_RULE]
    assert runner.calls == [_RunnerCall(repo.base_sha, repo.head_sha, ImportReport())]
    row = _single_audit_row(repo.state_db, verdict)
    assert row.decision == "allow"


@pytest.mark.parametrize(
    ("delete_helper", "tests", "floor_decision", "floor_rule", "condition"),
    [
        pytest.param(
            True,
            _GREEN_TESTS,
            "block",
            FLOOR_BROKEN_IMPORTS,
            "1 broken import(s); first tests/test_helper.py:1 -> pkg.helper.helper",
            id="broken_imports",
        ),
        pytest.param(
            False,
            _NO_TESTS,
            "require_human",
            FLOOR_INSUFFICIENT_EVIDENCE,
            "Python change with 0 executed tests",
            id="zero_executed_tests",
        ),
    ],
)
def test_allow_all_policy_is_floored_before_audit_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_helper: bool,
    tests: report.TestReport,
    floor_decision: str,
    floor_rule: str,
    condition: str,
) -> None:
    """The policy says ``allow``; the floor overrides it, and the audit row agrees.

    The single audit row carries the floored decision, the Floor_Rule_Id and
    the floor reason, and its stored Verdict JSON equals the returned Verdict
    (floor RuleResult included), so the floor ran before the write
    (Requirements 5.1, 5.10, 5.11).
    """
    repo = _make_repo(tmp_path, delete_helper=delete_helper)
    _install_runner(monkeypatch, _FakeRunner(tests=tests))

    verdict = _verify(repo)

    assert verdict.decision == floor_decision
    assert verdict.matched_rule == floor_rule
    assert verdict.reason == (
        f"Safety floor {floor_rule}: {condition}. "
        f"Policy decided 'allow' via rule '{_ALLOW_RULE}' "
        "(Custom policy allows every change.)."
    )
    floor_result = verdict.evidence.policy_results[-1]
    assert floor_result.rule_name == floor_rule
    assert floor_result.matched is True
    assert floor_result.would_emit == floor_decision
    assert floor_result.reason == condition

    row = _single_audit_row(repo.state_db, verdict)
    assert row.decision == floor_decision
    assert row.matched_rule == floor_rule


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "failing_stage"),
    [
        pytest.param(
            ImportCheckError("simulated import check failure"),
            "check_imports",
            id="ImportCheckError",
        ),
        pytest.param(
            CollectionPassError("simulated collection report failure"),
            "run_verification",
            id="CollectionPassError",
        ),
    ],
)
def test_pipeline_error_fails_closed_with_one_audit_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: TrikonError,
    failing_stage: str,
) -> None:
    """Each new error class becomes ``require_human`` and exactly one audit row.

    The allow-all policy is configured but never reached, and the real
    ImpactSet computed before the failure is kept (Requirement 7.3).
    """
    repo = _make_repo(tmp_path, delete_helper=False)
    runner = _install_runner(
        monkeypatch,
        _FakeRunner(error=error if failing_stage == "run_verification" else None),
    )
    if failing_stage == "check_imports":

        def raising_check_imports(change_set: ChangeSet, repo_path: Path) -> ImportReport:
            raise error

        monkeypatch.setattr(_SDK, "check_imports", raising_check_imports)

    verdict = _verify(repo)

    assert verdict.decision == "require_human"
    assert verdict.matched_rule is None
    assert verdict.reason == f"{type(error).__name__}: {error}"
    assert verdict.evidence.policy_results == []
    assert verdict.evidence.change != EMPTY_IMPACT_SET
    assert verdict.evidence.change.changed_files == ["pkg/core.py"]
    assert verdict.evidence.verification == EMPTY_VERIFICATION
    assert len(runner.calls) == (1 if failing_stage == "run_verification" else 0)

    row = _single_audit_row(repo.state_db, verdict)
    assert row.decision == "require_human"
    assert row.matched_rule is None


def test_import_report_survives_collection_pass_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check_imports`` finished, ``run_verification`` raised: the findings stay.

    The fail-closed Verdict carries the real ImportReport on top of the empty
    verification sentinel, in the returned Verdict and in the audit row, and
    the shared sentinel itself is not mutated (Requirement 4.13).
    """
    repo = _make_repo(tmp_path, delete_helper=True)
    runner = _install_runner(
        monkeypatch, _FakeRunner(error=CollectionPassError("collect.json is not JSON"))
    )

    verdict = _verify(repo)

    expected_imports = ImportReport(broken=[_HELPER_BROKEN_IMPORT])
    assert runner.calls == [_RunnerCall(repo.base_sha, repo.head_sha, expected_imports)]
    assert verdict.decision == "require_human"
    assert verdict.reason.startswith("CollectionPassError: ")
    verification = verdict.evidence.verification
    assert verification.imports == expected_imports
    assert verification.tests == EMPTY_VERIFICATION.tests
    assert verification.tests.strategy == "none"
    assert verification.static == EMPTY_VERIFICATION.static
    assert EMPTY_VERIFICATION.imports == ImportReport()

    row = _single_audit_row(repo.state_db, verdict)
    stored = Verdict.model_validate_json(row.verdict_json)
    assert stored.evidence.verification.imports == expected_imports


# ---------------------------------------------------------------------------
# schema_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delete_helper", "runner_error", "expected_decision"),
    [
        pytest.param(False, None, "allow", id="policy_decision"),
        pytest.param(True, None, "block", id="floored"),
        pytest.param(False, CollectionPassError("boom"), "require_human", id="fail_closed"),
    ],
)
def test_every_verdict_has_schema_version_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_helper: bool,
    runner_error: TrikonError | None,
    expected_decision: str,
) -> None:
    """The returned Verdict and its stored JSON both say ``schema_version`` 3 (Req 7.8)."""
    repo = _make_repo(tmp_path, delete_helper=delete_helper)
    _install_runner(monkeypatch, _FakeRunner(error=runner_error))

    verdict = _verify(repo)

    assert verdict.decision == expected_decision
    assert verdict.schema_version == 3
    row = _single_audit_row(repo.state_db, verdict)
    stored: object = json.loads(row.verdict_json)
    assert isinstance(stored, dict)
    assert stored["schema_version"] == 3
