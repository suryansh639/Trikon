"""Integration tests for the never-fail-open invariant at the SDK boundary.

Covers Task 10.2 in ``.kiro/specs/change-intelligence/tasks.md``. Validates:

* **Property 13 — Error-hierarchy closure (Requirements 6.1).** Every
  ``raise`` statement inside ``trikon/change_intel/**/*.py`` names either a
  subclass of :class:`ChangeIntelError`, ``NotImplementedError`` (grandfathered
  for stubs — the ``design.md §10`` DoD is zero on release), or
  ``AssertionError`` (allowed for unreachable-code guards). The scan is
  package-wide, which is what makes this stricter than task 1.3's file-level
  check.

* **Requirements 6.2 — Never fail-open at the SDK boundary.** For every
  :class:`ChangeIntelError` subclass raised anywhere on the change-intel path,
  :func:`trikon.sdk.verify` returns a :class:`Verdict` whose ``decision`` is
  ``"require_human"`` and whose ``evidence.change`` equals
  :data:`EMPTY_IMPACT_SET` (bucket ``HIGH``). "Unknown change" must never look
  like "safe change".

Monkeypatching strategy
-----------------------

:mod:`trikon.sdk` imports ``parse_diff`` and ``compute_impact`` with
``from ... import ...`` at module load, so the module-level names in
:mod:`trikon.sdk` are what :func:`verify` calls. Monkeypatching
``trikon.change_intel.blast_radius.compute_impact`` would leave
``trikon.sdk.compute_impact`` bound to the original callable and the test
would silently exercise the real pipeline. All patches therefore target
attributes on :mod:`trikon.sdk` directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trikon import sdk
from trikon.change_intel.errors import (
    AstParseError,
    BlastRadiusError,
    ChangeIntelError,
    DepGraphError,
    DiffInputError,
    DiffParseError,
    RepoNotFoundError,
    SymbolResolutionError,
)
from trikon.change_intel.models import ChangeSet
from trikon.evidence.report import EMPTY_IMPACT_SET

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

#: Every direct subclass of :class:`ChangeIntelError` that the SDK boundary
#: must translate into a ``require_human`` verdict. Kept as a hard-coded
#: tuple so adding a new subclass without also updating this test is a
#: deliberate act rather than an accidental gap.
ERROR_SUBCLASSES: tuple[type[ChangeIntelError], ...] = (
    AstParseError,
    BlastRadiusError,
    DepGraphError,
    DiffInputError,
    DiffParseError,
    RepoNotFoundError,
    SymbolResolutionError,
)

#: Root of the Change-Intelligence package on disk. Resolved from
#: ``tests/integration/change_intel/test_never_fail_open.py`` — ``parents[3]``
#: is the repo root, then ``trikon/change_intel``.
CHANGE_INTEL_ROOT: Path = Path(__file__).resolve().parents[3] / "trikon" / "change_intel"

#: Names allowed on the right-hand side of a ``raise`` inside
#: :data:`CHANGE_INTEL_ROOT`, beyond the :class:`ChangeIntelError` hierarchy.
#: See the module docstring for the rationale.
ALLOWED_STUB_NAMES: frozenset[str] = frozenset({"NotImplementedError", "AssertionError"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_change_intel_error_names() -> frozenset[str]:
    """Return every class name reachable from :class:`ChangeIntelError` via ``__subclasses__``.

    We introspect at runtime rather than hard-coding the seven subclass names
    so that a future task that adds an eighth subclass automatically widens
    the sanctioned vocabulary without editing this test.
    """
    names: set[str] = {ChangeIntelError.__name__}
    stack: list[type[ChangeIntelError]] = list(ChangeIntelError.__subclasses__())
    while stack:
        cls = stack.pop()
        names.add(cls.__name__)
        stack.extend(cls.__subclasses__())
    return frozenset(names)


def _raise_name(node: ast.Raise) -> str | None:
    """Return the name of the exception class raised by ``node``, or ``None``.

    Handles the shapes seen in this package:

    * bare ``raise`` (implicit re-raise) — returns ``None`` (always allowed).
    * ``raise SomeError`` — ``exc`` is an :class:`ast.Name`.
    * ``raise SomeError(...)`` / ``raise SomeError(...) from cause`` — ``exc``
      is an :class:`ast.Call` whose ``func`` is a :class:`ast.Name` or
      :class:`ast.Attribute`.
    """
    exc = node.exc
    if exc is None:
        return None
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Call):
        func = exc.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    if isinstance(exc, ast.Attribute):
        return exc.attr
    return None


def _make_error(cls: type[ChangeIntelError]) -> ChangeIntelError:
    """Construct an instance of ``cls`` honoring its constructor contract.

    :class:`AstParseError` requires ``file_path``; the rest take only a
    message. Centralising the branch keeps the parametrized test cases
    uniform.
    """
    if cls is AstParseError:
        return AstParseError("simulated parse failure", file_path="dummy.py", line=1)
    return cls("simulated change-intel failure")


def _empty_change_set(repo_path: Path) -> ChangeSet:
    """Return a well-formed, zero-file :class:`ChangeSet` for use by fake ``parse_diff``.

    :func:`sdk.verify` never inspects ``ChangeSet.files`` before handing the
    result to ``compute_impact``, so an empty tuple is sufficient — the test
    only cares that ``parse_diff`` succeeded on the way to ``compute_impact``.
    """
    return ChangeSet(
        repo_path=repo_path,
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=(),
    )


# ---------------------------------------------------------------------------
# Property 13 — Error-hierarchy closure
# ---------------------------------------------------------------------------


def test_error_hierarchy_closure_across_change_intel_package() -> None:
    """Every raise site under ``trikon/change_intel/**`` names a sanctioned exception.

    Walks every ``.py`` file under :data:`CHANGE_INTEL_ROOT`, scans its AST
    for :class:`ast.Raise` nodes, and asserts the raised class name is either
    a :class:`ChangeIntelError` subclass or one of :data:`ALLOWED_STUB_NAMES`.
    Bare re-raises (``raise`` with no argument inside an ``except`` block)
    are always allowed.

    Validates: Requirements 6.1.
    """
    allowed_names: frozenset[str] = _all_change_intel_error_names() | ALLOWED_STUB_NAMES

    py_files = sorted(p for p in CHANGE_INTEL_ROOT.rglob("*.py") if p.is_file())
    assert py_files, f"No Python files found under {CHANGE_INTEL_ROOT}; test is misconfigured."

    violations: list[str] = []
    for source_file in py_files:
        source = source_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            name = _raise_name(node)
            if name is None:
                # Bare re-raise inside an ``except`` block — always allowed.
                continue
            if name not in allowed_names:
                rel_path = source_file.relative_to(CHANGE_INTEL_ROOT.parent.parent)
                violations.append(
                    f"{rel_path}:{node.lineno}: raise {name}(...) — "
                    f"not a ChangeIntelError subclass or sanctioned stub"
                )

    assert not violations, (
        "Non-sanctioned raise sites detected in trikon/change_intel/**:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Requirements 6.2 — Never fail-open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_cls",
    ERROR_SUBCLASSES,
    ids=[cls.__name__ for cls in ERROR_SUBCLASSES],
)
def test_verify_returns_require_human_when_compute_impact_raises(
    error_cls: type[ChangeIntelError],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every :class:`ChangeIntelError` subclass surfaced by ``compute_impact`` fails closed.

    The scenario: ``parse_diff`` succeeds (fake returns an empty
    :class:`ChangeSet`) and ``compute_impact`` raises ``error_cls``. The SDK
    boundary must catch, translate, and return the never-fail-open verdict
    with ``blast_radius_score == "HIGH"``. Value equality against
    :data:`EMPTY_IMPACT_SET` — the singleton may or may not survive Pydantic
    validation as the same object, but its field values must.

    Validates: Requirements 6.2.
    """

    def fake_parse_diff(*args: object, **kwargs: object) -> ChangeSet:
        return _empty_change_set(tmp_path)

    err = _make_error(error_cls)

    def raising_compute_impact(*args: object, **kwargs: object) -> object:
        raise err

    monkeypatch.setattr("trikon.sdk.parse_diff", fake_parse_diff)
    monkeypatch.setattr("trikon.sdk.compute_impact", raising_compute_impact)

    verdict = sdk.verify(tmp_path, base_sha="deadbeef", head_sha="cafef00d")

    assert verdict.decision == "require_human"
    assert verdict.evidence.change == EMPTY_IMPACT_SET
    assert verdict.evidence.change.blast_radius_score == "HIGH"
    assert error_cls.__name__ in verdict.reason
    assert verdict.matched_rule is None
    assert verdict.evidence.policy_results == []


def test_verify_returns_require_human_when_parse_diff_raises_repo_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A :class:`RepoNotFoundError` from ``parse_diff`` still yields the fail-closed verdict.

    The failure occurs before ``compute_impact`` is reached, so this pins the
    other half of the SDK's ``try / except ChangeIntelError`` block: an early
    failure in the change-intel pipeline is treated identically to a late
    one.

    Validates: Requirements 6.2.
    """

    def raising_parse_diff(*args: object, **kwargs: object) -> ChangeSet:
        raise RepoNotFoundError(f"not a git repository: {tmp_path}")

    monkeypatch.setattr("trikon.sdk.parse_diff", raising_parse_diff)

    verdict = sdk.verify(tmp_path, base_sha="deadbeef", head_sha="cafef00d")

    assert verdict.decision == "require_human"
    assert verdict.evidence.change == EMPTY_IMPACT_SET
    assert verdict.evidence.change.blast_radius_score == "HIGH"
    assert "RepoNotFoundError" in verdict.reason


def test_verify_returns_require_human_when_parse_diff_raises_diff_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A :class:`DiffInputError` from ``parse_diff`` also yields the fail-closed verdict.

    :class:`DiffInputError` is the "caller violated the contract" flavor
    (e.g., neither SHA pair nor diff string supplied). It must fail closed
    just like any other pipeline failure — a malformed request must never
    quietly downgrade to ``allow`` in a later phase.

    Validates: Requirements 6.2.
    """

    def raising_parse_diff(*args: object, **kwargs: object) -> ChangeSet:
        raise DiffInputError("neither (base_sha, head_sha) nor diff was supplied")

    monkeypatch.setattr("trikon.sdk.parse_diff", raising_parse_diff)

    verdict = sdk.verify(tmp_path, base_sha=None, head_sha=None, diff=None)

    assert verdict.decision == "require_human"
    assert verdict.evidence.change == EMPTY_IMPACT_SET
    assert verdict.evidence.change.blast_radius_score == "HIGH"
    assert "DiffInputError" in verdict.reason
