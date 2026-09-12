"""Unit tests for :mod:`trikon.change_intel.blast_radius`.

Covers Task 8.3 in ``.kiro/specs/change-intelligence/tasks.md``. Exercises
the three public helpers introduced by Task 8.1 (:class:`BlastWeights`,
:func:`bucket`, :func:`enclosing_symbols`) plus light integration cases for
:func:`compute_impact` from Task 8.2. Full pipeline validation on the
seeded ``examples/sample_repo/`` scenarios is the responsibility of Task
10.1's end-to-end integration test.

Properties validated:

* **Property 11 / Requirements 5.2** — :func:`bucket` is monotone
  non-decreasing on the reals under the ranking ``LOW < MEDIUM < HIGH``.
* **Property 12 / Requirements 5.3** — for any change set ``C`` and every
  extra sensitive-path touch, the numeric score of
  ``compute_impact(C + sensitive_touch)`` grows by at least
  ``weights.sensitive_path_touch`` (measured before HIGH saturation).

Property 10 (impact soundness on directly changed symbols) is intentionally
deferred to Task 10.1: the tests there apply the frozen ``clean_refactor``,
``bad_retry``, ``sensitive_touch``, ``no_python_change``, and ``deleted_file``
scenarios against ``examples/sample_repo/`` and assert exact ``ImpactSet``
equality. That gives a stronger correctness guarantee than a hypothesis
strategy could synthesize at unit scope.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trikon.change_intel.blast_radius import (
    BlastWeights,
    bucket,
    compute_impact,
    enclosing_symbols,
)
from trikon.change_intel.models import (
    ChangeKind,
    ChangeSet,
    FileChange,
    Hunk,
    SymbolDef,
    SymbolKind,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUCKET_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
"""Numeric ranking for bucket comparisons. Higher means broader blast radius."""


def _symbol(
    qname: str,
    *,
    start_line: int,
    end_line: int,
    file_path: str = "src/pkg/mod.py",
    kind: SymbolKind = "function",
    is_public: bool = True,
) -> SymbolDef:
    """Compact :class:`SymbolDef` factory for the containment tests.

    Only the fields ``enclosing_symbols`` inspects are exposed as parameters;
    everything else gets a default that keeps the value construction cheap
    and readable at the call site.
    """
    return SymbolDef(
        qualified_name=qname,
        kind=kind,
        file_path=file_path,
        file_sha="0" * 64,
        start_line=start_line,
        end_line=end_line,
        start_byte=0,
        end_byte=1,
        is_public=is_public,
    )


def _file_change(
    path: str,
    *,
    change_kind: ChangeKind = "modified",
) -> FileChange:
    """Build a hunk-less :class:`FileChange` for short-circuit tests.

    The compute_impact short-circuit branches (empty change set, non-Python
    only) never consult hunks, so an empty tuple is enough to exercise them
    without materializing fake diff geometry.
    """
    return FileChange(path=path, change_kind=change_kind, old_path=None, hunks=())


def _change_set(repo_path: Path, *paths: str) -> ChangeSet:
    """Build a :class:`ChangeSet` from a list of file paths, no hunks."""
    return ChangeSet(
        repo_path=repo_path,
        base_sha=None,
        head_sha=None,
        files=tuple(_file_change(p) for p in paths),
    )


# ---------------------------------------------------------------------------
# Shared temp directory for hypothesis-driven compute_impact tests.
# ---------------------------------------------------------------------------

_SHARED_REPO: Path = Path(tempfile.mkdtemp(prefix="trikon-blast-radius-"))
"""Reusable writable directory for :func:`compute_impact` in property tests.

Hypothesis reruns the test body many times per invocation. Rather than pay
for a fresh :func:`tempfile.mkdtemp` per example (or fight the
``function_scoped_fixture`` health check to inject :data:`tmp_path`), we
allocate the directory once at import time. :func:`compute_impact` on a
non-Python change set only creates ``<repo>/.trikon/`` — no file writes,
no SQLite open — so reuse is safe.
"""


# ---------------------------------------------------------------------------
# 1. bucket — boundary and monotonicity behaviour.
# ---------------------------------------------------------------------------


class TestBucketBoundaries:
    """Fixed-input tests around the default bucket thresholds."""

    def test_zero_score_is_low(self) -> None:
        """A ``0.0`` score maps to ``LOW`` — the resting state of the function."""
        assert bucket(0.0) == "LOW"

    def test_low_boundary_is_inclusive(self) -> None:
        """``score == low_bucket_max`` (5.0 default) stays in ``LOW`` per design."""
        assert bucket(5.0) == "LOW"

    def test_just_above_low_boundary_is_medium(self) -> None:
        """A hair above ``low_bucket_max`` crosses into ``MEDIUM``."""
        assert bucket(5.001) == "MEDIUM"

    def test_medium_boundary_is_inclusive(self) -> None:
        """``score == medium_bucket_max`` (15.0 default) stays in ``MEDIUM``."""
        assert bucket(15.0) == "MEDIUM"

    def test_just_above_medium_boundary_is_high(self) -> None:
        """A hair above ``medium_bucket_max`` crosses into ``HIGH``."""
        assert bucket(15.001) == "HIGH"

    def test_large_negative_score_is_low(self) -> None:
        """Negative scores are still bounded by ``LOW`` — the function is total."""
        assert bucket(-100.0) == "LOW"

    def test_positive_infinity_is_high(self) -> None:
        """Unbounded positive scores land in ``HIGH``."""
        assert bucket(math.inf) == "HIGH"

    def test_nan_is_high(self) -> None:
        """NaN means "we don't know" — the safe answer is HIGH, never LOW.

        See :func:`bucket`'s docstring: fail closed when the score itself is
        ill-defined so a bad computation cannot masquerade as a low-risk
        change.
        """
        assert bucket(math.nan) == "HIGH"

    def test_custom_weights_shift_thresholds(self) -> None:
        """Callers widen the LOW / MEDIUM bands by supplying custom weights.

        This is what a policy tuned for a large monorepo does: raise the
        thresholds so an ordinary change does not immediately land in HIGH.
        """
        custom = BlastWeights(low_bucket_max=100.0, medium_bucket_max=200.0)
        assert bucket(50.0, weights=custom) == "LOW"
        assert bucket(150.0, weights=custom) == "MEDIUM"
        assert bucket(250.0, weights=custom) == "HIGH"


# ---------------------------------------------------------------------------
# 2. Property 11 — bucket monotonicity.
# ---------------------------------------------------------------------------


@given(
    a=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    b=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200, deadline=1000)
def test_property11_bucket_monotone_default_weights(a: float, b: float) -> None:
    """For any ``x <= y``, ``bucket(x) <= bucket(y)`` under ``LOW < MEDIUM < HIGH``.

    Restricting the domain to a bounded finite range keeps the shrinker
    honest — Hypothesis will drive both sides toward the LOW / MEDIUM and
    MEDIUM / HIGH boundaries if a regression re-orders them.

    Validates: Requirements 5.2 (Property 11).
    """
    lo, hi = sorted([a, b])
    assert _BUCKET_ORDER[bucket(lo)] <= _BUCKET_ORDER[bucket(hi)]


@given(
    a=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    b=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    low_max=st.floats(min_value=0.1, max_value=50.0, allow_nan=False, allow_infinity=False),
    gap=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, deadline=1000)
def test_property11_bucket_monotone_custom_weights(
    a: float,
    b: float,
    low_max: float,
    gap: float,
) -> None:
    """Monotonicity holds for every admissible :class:`BlastWeights` configuration.

    The custom weights parametrisation matters because policy files
    override the defaults on a per-repo basis; the invariant must not
    depend on the default numbers being unchanged.
    """
    weights = BlastWeights(low_bucket_max=low_max, medium_bucket_max=low_max + gap)
    lo, hi = sorted([a, b])
    assert _BUCKET_ORDER[bucket(lo, weights=weights)] <= _BUCKET_ORDER[bucket(hi, weights=weights)]


# ---------------------------------------------------------------------------
# 3. enclosing_symbols — line-range containment.
# ---------------------------------------------------------------------------


class TestEnclosingSymbols:
    """Behaviour of :func:`enclosing_symbols` across the boundary cases."""

    _FILE_PATH = Path("src/pkg/mod.py")

    def _fn(self, name: str, start: int, end: int) -> SymbolDef:
        """Shortcut for a function symbol in the fixed test file."""
        return _symbol(name, start_line=start, end_line=end)

    def test_empty_changed_lines_returns_empty(self) -> None:
        """No changed lines ⇒ nothing to attribute, regardless of population."""
        result = enclosing_symbols(
            self._FILE_PATH,
            [],
            [self._fn("pkg.mod.a", 1, 10)],
        )
        assert result == []

    def test_empty_symbols_returns_empty(self) -> None:
        """Empty symbol list ⇒ no matches even when lines are provided."""
        assert enclosing_symbols(self._FILE_PATH, [1, 2, 3], []) == []

    def test_empty_lines_and_symbols_returns_empty(self) -> None:
        """Doubly-empty inputs are a stable no-op."""
        assert enclosing_symbols(self._FILE_PATH, [], []) == []

    def test_line_inside_symbol_returns_it(self) -> None:
        """A line strictly inside ``[start_line, end_line]`` matches the symbol."""
        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, [15], [sym]) == [sym]

    def test_start_line_boundary_is_inclusive(self) -> None:
        """``start_line`` itself is inside the range."""
        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, [10], [sym]) == [sym]

    def test_end_line_boundary_is_inclusive(self) -> None:
        """``end_line`` itself is inside the range."""
        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, [20], [sym]) == [sym]

    def test_line_just_before_start_returns_empty(self) -> None:
        """``start_line - 1`` is outside the range."""
        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, [9], [sym]) == []

    def test_line_just_after_end_returns_empty(self) -> None:
        """``end_line + 1`` is outside the range."""
        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, [21], [sym]) == []

    def test_nested_class_and_method_both_returned(self) -> None:
        """Overlapping symbols (class + method) both match, sorted by ``(start_line, qname)``.

        A line inside the method's body is also inside the enclosing class's
        body. Callers of :func:`enclosing_symbols` see both layers so they
        can attribute a change to any level of the containment hierarchy;
        :func:`compute_impact` internally then picks the innermost.
        """
        klass = _symbol(
            "pkg.mod.C",
            start_line=1,
            end_line=30,
            kind="class",
        )
        method = _symbol(
            "pkg.mod.C.f",
            start_line=5,
            end_line=15,
            kind="method",
        )
        result = enclosing_symbols(self._FILE_PATH, [10], [method, klass])
        # Class starts at line 1 (< method's line 5), so class sorts first.
        assert result == [klass, method]

    def test_same_start_line_tie_break_on_qname(self) -> None:
        """Symbols sharing ``start_line`` are ordered by ascending qualified name."""
        zebra = self._fn("pkg.mod.zebra", 5, 20)
        alpha = self._fn("pkg.mod.alpha", 5, 20)
        result = enclosing_symbols(self._FILE_PATH, [10], [zebra, alpha])
        assert result == [alpha, zebra]

    def test_multiple_lines_returned_once(self) -> None:
        """One symbol enclosing many changed lines is not duplicated in the result."""
        sym = self._fn("pkg.mod.f", 10, 20)
        result = enclosing_symbols(self._FILE_PATH, [11, 12, 13, 14], [sym])
        assert result == [sym]

    def test_duplicate_lines_in_iterable_do_not_duplicate_matches(self) -> None:
        """Repeated line numbers in the iterable are collapsed by the internal set."""
        sym = self._fn("pkg.mod.f", 10, 20)
        result = enclosing_symbols(self._FILE_PATH, [10, 10, 15, 15, 20], [sym])
        assert result == [sym]

    def test_accepts_generator_as_changed_lines(self) -> None:
        """``changed_lines`` is documented as ``Iterable[int]`` — a generator must work.

        Guards against a regression that iterates ``changed_lines`` twice
        (which would silently break for one-shot iterables).
        """

        def _lines() -> Iterable[int]:
            yield 10
            yield 15

        sym = self._fn("pkg.mod.f", 10, 20)
        assert enclosing_symbols(self._FILE_PATH, _lines(), [sym]) == [sym]

    def test_disjoint_symbols_only_matching_one_are_returned(self) -> None:
        """Symbols whose ranges do not intersect the changed lines are dropped."""
        inside = self._fn("pkg.mod.hit", 10, 20)
        outside = self._fn("pkg.mod.miss", 100, 200)
        assert enclosing_symbols(self._FILE_PATH, [15], [inside, outside]) == [inside]


# ---------------------------------------------------------------------------
# 4. compute_impact — light integration on the short-circuit branches.
# ---------------------------------------------------------------------------


class TestComputeImpactLight:
    """Cover the short-circuit paths through :func:`compute_impact`.

    Full pipeline validation (indexer + dep-graph + score arithmetic on
    realistic changes) is the responsibility of Task 10.1's end-to-end
    tests against ``examples/sample_repo/``.
    """

    def test_empty_change_set_is_zero_and_low(self, tmp_path: Path) -> None:
        """An empty change set yields an empty :class:`ImpactSet` with score ``0``."""
        impact = compute_impact(_change_set(tmp_path), tmp_path)

        assert impact.changed_files == []
        assert impact.changed_symbols == []
        assert impact.impacted_modules == []
        assert impact.impacted_public_apis == []
        assert impact.impacted_tests == []
        assert impact.blast_radius_score == "LOW"
        assert impact.blast_radius_numeric == 0.0

    def test_non_python_only_change_short_circuits_to_low(self, tmp_path: Path) -> None:
        """A README-only change never touches the indexer or the dep graph.

        The short-circuit is what keeps ``no_python_change`` in the LOW
        bucket in the fixture set — validating it here means a regression
        that starts to touch SQLite for pure-markdown edits fails a unit
        test before it fails the integration suite.
        """
        cs = _change_set(tmp_path, "README.md", "docs/guide.md")
        impact = compute_impact(cs, tmp_path)

        # sorted() puts "README.md" before "docs/guide.md" because 'R' < 'd'
        # in ASCII — that determinism is Goal G1 in ``design.md``.
        assert impact.changed_files == ["README.md", "docs/guide.md"]
        assert impact.changed_symbols == []
        assert impact.impacted_modules == []
        assert impact.impacted_public_apis == []
        assert impact.impacted_tests == []
        assert impact.blast_radius_score == "LOW"
        assert impact.blast_radius_numeric == 0.0

    def test_sensitive_path_touch_contributes_score(self, tmp_path: Path) -> None:
        """A non-Python edit under ``payments/**`` still adds the sensitive-path weight.

        Sensitive-path scoring must not depend on the file being Python:
        an edit to ``src/payments/schema.yaml`` is still a payments-scoped
        change and must move the score off zero.
        """
        cs = _change_set(tmp_path, "src/payments/schema.yaml")
        impact = compute_impact(cs, tmp_path)

        # One sensitive touch * 5.0 default = 5.0. That equals the LOW /
        # MEDIUM boundary, which is inclusive on the LOW side by design.
        assert impact.blast_radius_numeric == pytest.approx(5.0)
        assert impact.blast_radius_score == "LOW"

    def test_multiple_sensitive_touches_accumulate(self, tmp_path: Path) -> None:
        """Two sensitive-path touches double the sensitive-weight contribution.

        Two * 5.0 = 10.0, which is still ``<= medium_bucket_max`` (15.0),
        landing the verdict in the MEDIUM bucket without saturating.
        """
        cs = _change_set(
            tmp_path,
            "src/payments/schema.yaml",
            "src/auth/roles.yaml",
        )
        impact = compute_impact(cs, tmp_path)

        assert impact.blast_radius_numeric == pytest.approx(10.0)
        assert impact.blast_radius_score == "MEDIUM"

    def test_non_sensitive_non_python_stays_at_zero(self, tmp_path: Path) -> None:
        """A file that is neither Python nor under a sensitive glob contributes nothing."""
        cs = _change_set(tmp_path, "docs/guide.md", "assets/logo.png")
        impact = compute_impact(cs, tmp_path)

        assert impact.blast_radius_numeric == 0.0
        assert impact.blast_radius_score == "LOW"

    def test_custom_sensitive_paths_override_defaults(self, tmp_path: Path) -> None:
        """A custom :class:`BlastWeights` swaps out the sensitive-path glob set.

        Repos in domains where ``payments/**`` is not a hotspot (say, a
        game engine) should be able to redefine the sensitivity set without
        editing the code. This exercises the ``weights`` argument on
        ``compute_impact``.
        """
        weights = BlastWeights(sensitive_paths=("compliance/**",))
        cs = _change_set(
            tmp_path,
            "src/payments/schema.yaml",  # sensitive under defaults, not this set
            "compliance/rules.txt",  # sensitive under this custom set
        )
        impact = compute_impact(cs, tmp_path, weights=weights)

        # Only the compliance file counts. 1 * 5.0 = 5.0.
        assert impact.blast_radius_numeric == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 5. Property 12 — sensitive-path weight monotonicity.
# ---------------------------------------------------------------------------


@given(
    n_base=st.integers(min_value=0, max_value=4),
    n_added_sensitive=st.integers(min_value=1, max_value=3),
)
@settings(
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property12_sensitive_path_weight_monotone(
    n_base: int,
    n_added_sensitive: int,
) -> None:
    """Adding ``k`` sensitive-path touches lifts the score by at least ``k * weight``.

    For any base change set ``C`` composed of non-sensitive non-Python
    files, the augmented change set ``C + S`` (with ``S`` = ``k`` extra
    files under a sensitive glob) satisfies
    ``score(C + S) >= score(C) + k * weights.sensitive_path_touch``,
    measured before HIGH saturation.

    Non-Python inputs are used deliberately so the property depends only
    on the sensitive-path arithmetic in :func:`_count_sensitive_touches`
    and the closed-form score in :func:`_compute_score`; there is no
    indirection through the AST indexer, dep graph, or test-heuristic
    fallout, which are covered elsewhere.

    Validates: Requirements 5.3 (Property 12).
    """
    weights = BlastWeights()

    # Base change set: N non-sensitive, non-Python files. Each contributes 0.
    base_paths = tuple(f"docs/note_{i}.md" for i in range(n_base))
    base_cs = _change_set(_SHARED_REPO, *base_paths)
    base_score = compute_impact(base_cs, _SHARED_REPO, weights=weights).blast_radius_numeric

    # Augment: same base plus K non-Python sensitive-path touches.
    added_paths = tuple(f"src/payments/patch_{i}.yaml" for i in range(n_added_sensitive))
    augmented_cs = _change_set(_SHARED_REPO, *base_paths, *added_paths)
    augmented_score = compute_impact(
        augmented_cs,
        _SHARED_REPO,
        weights=weights,
    ).blast_radius_numeric

    expected_delta = weights.sensitive_path_touch * n_added_sensitive
    # Small epsilon tolerance against floating-point rounding on the sum.
    assert augmented_score >= base_score + expected_delta - 1e-9
    # Base score is trivially zero here — no sensitive files, no Python files.
    assert base_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. compute_impact — orchestration through real Python fixtures.
# ---------------------------------------------------------------------------


class TestComputeImpactOrchestration:
    """Drive :func:`compute_impact` through the full pipeline with tmp-path fixtures.

    These tests build minimal Python layouts on disk to exercise the
    indexer + dep-graph query + score arithmetic paths without depending
    on ``examples/sample_repo/``. They are deliberately narrower than the
    Task 10.1 end-to-end tests: each one trips a specific branch (public
    API surface, private symbol filter, test-file heuristic, deleted-file
    skip, sensitive-Python-file scoring, rename bookkeeping) rather than
    asserting exact ``ImpactSet`` equality against a fixture JSON.
    """

    def _ensure_package(self, repo: Path, rel_dir: str) -> None:
        """Create ``rel_dir`` with an empty ``__init__.py`` for module inference."""
        pkg = repo / rel_dir
        pkg.mkdir(parents=True, exist_ok=True)
        init = pkg / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")

    def _write_module(self, repo: Path, rel_path: str, source: str) -> None:
        """Write a Python source file at ``repo / rel_path``."""
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def _hunk(
        self,
        *,
        added: tuple[int, ...] = (),
        removed: tuple[int, ...] = (),
    ) -> Hunk:
        """Build a :class:`Hunk` whose geometry mirrors ``added`` / ``removed``.

        The exact ``old_start`` / ``new_start`` values do not matter for
        :func:`compute_impact` — only ``added_lines`` and ``removed_lines``
        drive the enclosing-symbol lookup — but they must remain
        internally consistent so the value is well-formed.
        """
        # Point the hunk header at the first affected line if any exists,
        # falling back to line 1 for a well-formed but empty payload.
        anchor = min(added or removed or (1,))
        return Hunk(
            old_start=anchor,
            old_lines=len(removed),
            new_start=anchor,
            new_lines=len(added),
            added_lines=added,
            removed_lines=removed,
        )

    def _change_set_with_hunks(
        self,
        repo: Path,
        *entries: tuple[str, tuple[int, ...], tuple[int, ...]],
    ) -> ChangeSet:
        """Build a :class:`ChangeSet` from ``(rel_path, added, removed)`` triples."""
        files = tuple(
            FileChange(
                path=path,
                change_kind="modified",
                old_path=None,
                hunks=(self._hunk(added=added, removed=removed),),
            )
            for path, added, removed in entries
        )
        return ChangeSet(
            repo_path=repo,
            base_sha=None,
            head_sha=None,
            files=files,
        )

    def test_public_function_edit_shows_in_changed_symbols(self, tmp_path: Path) -> None:
        """A hunk landing inside a public function surfaces that function.

        Also exercises: :func:`_top_package` (impacted_modules picks up the
        top-level package), :func:`_public_refs_sorted` (translation to
        :class:`SymbolRef` with a repo-relative POSIX ``file_path``), and
        :func:`_impacted_tests` when no matching test file exists.
        """
        self._ensure_package(tmp_path, "src/pkg")
        source = (
            "def foo():\n"  # line 1
            "    return 42\n"  # line 2
            "\n"  # line 3
            "\n"  # line 4
            "def bar():\n"  # line 5
            "    return 7\n"  # line 6
        )
        self._write_module(tmp_path, "src/pkg/mod.py", source)

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (2,), ()),  # touch foo's body only
        )
        impact = compute_impact(change_set, tmp_path)

        qnames = {ref.qualified_name for ref in impact.changed_symbols}
        assert "pkg.mod.foo" in qnames
        assert "pkg.mod.bar" not in qnames
        assert impact.impacted_modules == ["pkg"]
        # foo is public → it lands in impacted_public_apis too.
        api_qnames = {ref.qualified_name for ref in impact.impacted_public_apis}
        assert "pkg.mod.foo" in api_qnames
        # file_path in the public ref is repo-relative POSIX, not absolute.
        for ref in impact.changed_symbols:
            assert not ref.file_path.startswith("/")
            assert not ref.file_path.startswith(str(tmp_path))
            assert "\\" not in ref.file_path  # POSIX separators only

    def test_private_symbol_excluded_from_public_apis(self, tmp_path: Path) -> None:
        """A hunk on a leading-underscore function stays out of impacted_public_apis."""
        self._ensure_package(tmp_path, "src/pkg")
        source = (
            "def _private():\n"  # line 1
            "    return 0\n"  # line 2
        )
        self._write_module(tmp_path, "src/pkg/mod.py", source)

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (2,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        qnames = {ref.qualified_name for ref in impact.changed_symbols}
        assert "pkg.mod._private" in qnames
        # is_public=False on the SymbolDef ⇒ empty impacted_public_apis.
        assert impact.impacted_public_apis == []

    def test_test_file_heuristic_matches_existing_test(self, tmp_path: Path) -> None:
        """A module ``pkg.mod`` with a ``tests/test_mod.py`` on disk sees the test.

        Exercises :func:`_impacted_tests`'s filename heuristic — the first
        candidate is ``tests/test_{leaf}.py``, and any candidate that
        actually exists is added to the result.
        """
        self._ensure_package(tmp_path, "src/pkg")
        self._write_module(
            tmp_path,
            "src/pkg/mod.py",
            "def foo():\n    return 1\n",
        )
        # Materialize a matching test file. Any content is fine — the
        # heuristic only checks existence.
        self._write_module(
            tmp_path,
            "tests/test_mod.py",
            "def test_foo():\n    assert True\n",
        )

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (2,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        assert "tests/test_mod.py" in impact.impacted_tests

    def test_test_file_in_change_set_is_impacted(self, tmp_path: Path) -> None:
        """A modified test file is trivially in ``impacted_tests``.

        The union branch in :func:`_impacted_tests` picks up any file whose
        POSIX path contains a ``tests`` component AND whose name starts
        with ``test_``. That covers scenarios where the change itself is a
        test edit with no matching production module.
        """
        self._ensure_package(tmp_path, "src/pkg")
        self._write_module(
            tmp_path,
            "src/pkg/mod.py",
            "def foo():\n    return 1\n",
        )
        # Create the test file so index_files can read it.
        self._write_module(
            tmp_path,
            "tests/test_mod.py",
            "def test_foo():\n    assert True\n",
        )

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (2,), ()),
            ("tests/test_mod.py", (2,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        assert "tests/test_mod.py" in impact.impacted_tests

    def test_deleted_python_file_skipped_by_indexer(self, tmp_path: Path) -> None:
        """A ``change_kind="deleted"`` entry is not read from disk.

        :func:`_collect_python_paths` filters deleted files out before
        :func:`index_files` runs; otherwise the missing file would raise
        an OS error. The change still contributes to ``changed_files``.
        """
        # Note: we deliberately never write mod.py to tmp_path — it is
        # "already deleted" from the working tree.
        change_set = ChangeSet(
            repo_path=tmp_path,
            base_sha=None,
            head_sha=None,
            files=(
                FileChange(
                    path="src/pkg/mod.py",
                    change_kind="deleted",
                    old_path=None,
                    hunks=(),
                ),
            ),
        )
        impact = compute_impact(change_set, tmp_path)

        assert impact.changed_files == ["src/pkg/mod.py"]
        # Deleted files contribute zero symbols — Phase 1 does not chase
        # them through git-show. See ``_collect_python_paths`` docstring.
        assert impact.changed_symbols == []

    def test_missing_python_file_silently_skipped(self, tmp_path: Path) -> None:
        """A ``modified`` entry whose path does not exist is skipped, not raised.

        Guards :func:`_collect_python_paths`'s ``if not abs_path.exists()``
        branch: the caller may hand us a stale ChangeSet where a file was
        deleted between the diff being produced and the impact being
        computed. Skipping is safer than crashing.
        """
        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/vanished.py", (1,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        assert impact.changed_files == ["src/pkg/vanished.py"]
        assert impact.changed_symbols == []
        # No sensitive glob matches, no python files indexed, LOW bucket.
        assert impact.blast_radius_score == "LOW"

    def test_sensitive_python_file_adds_sensitive_weight(self, tmp_path: Path) -> None:
        """A real Python edit under ``payments/**`` gets the sensitive weight bump.

        This is the "hot" path — indexer runs, dep-graph opens, sensitive
        counter increments. Score contributions: 1 impacted module
        (``payments``) * 1.0 + 1 impacted public API * 3.0 + 1 sensitive
        touch * 5.0 = 9.0 → MEDIUM.
        """
        self._ensure_package(tmp_path, "src/payments")
        source = (
            "def charge(amount: int) -> int:\n"  # line 1
            "    return amount + 1\n"  # line 2
        )
        self._write_module(tmp_path, "src/payments/gateway.py", source)

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/payments/gateway.py", (2,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        qnames = {ref.qualified_name for ref in impact.changed_symbols}
        assert "payments.gateway.charge" in qnames
        assert impact.impacted_modules == ["payments"]
        # 1 module + 1 public API + 1 sensitive path.
        assert impact.blast_radius_numeric == pytest.approx(9.0)
        assert impact.blast_radius_score == "MEDIUM"

    def test_multiple_hunks_same_function_dedup(self, tmp_path: Path) -> None:
        """Multiple hunks touching the same function only surface it once.

        The dedup key in :func:`_collect_changed_symbols` is
        ``(qualified_name, file_sha)``; two hunks on the same function
        share both.
        """
        self._ensure_package(tmp_path, "src/pkg")
        source = (
            "def foo():\n"  # line 1
            "    a = 1\n"  # line 2
            "    b = 2\n"  # line 3
            "    c = 3\n"  # line 4
            "    return a + b + c\n"  # line 5
        )
        self._write_module(tmp_path, "src/pkg/mod.py", source)

        change_set = ChangeSet(
            repo_path=tmp_path,
            base_sha=None,
            head_sha=None,
            files=(
                FileChange(
                    path="src/pkg/mod.py",
                    change_kind="modified",
                    old_path=None,
                    hunks=(
                        self._hunk(added=(2,)),
                        self._hunk(added=(4,)),
                    ),
                ),
            ),
        )
        impact = compute_impact(change_set, tmp_path)

        qnames = [ref.qualified_name for ref in impact.changed_symbols]
        assert qnames.count("pkg.mod.foo") == 1

    def test_init_module_symbol_normalizes_qualified_name(self, tmp_path: Path) -> None:
        """A symbol defined in ``pkg/__init__.py`` has its ``.__init__.`` collapsed.

        The AST indexer's :func:`_infer_module_path` includes ``__init__``
        as a component (``pkg.__init__``), so a top-level symbol there has
        ``qualified_name == "pkg.__init__.__all__"``. The public boundary
        translation collapses that mid-path segment so callers see
        ``pkg.__all__``.
        """
        self._ensure_package(tmp_path, "src/pkg")
        # Rewrite __init__.py with a real assignment we can target.
        self._write_module(
            tmp_path,
            "src/pkg/__init__.py",
            '__all__ = ["foo"]\n',
        )

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/__init__.py", (1,), ()),
        )
        impact = compute_impact(change_set, tmp_path)

        qnames = {ref.qualified_name for ref in impact.changed_symbols}
        # The `.__init__.` segment was collapsed at the boundary.
        assert "pkg.__all__" in qnames
        assert not any(".__init__." in q for q in qnames)
        # Top package derivation still lands on "pkg".
        assert impact.impacted_modules == ["pkg"]

    def test_hunk_between_symbols_yields_no_changed_symbol(self, tmp_path: Path) -> None:
        """An edit at module scope between definitions attributes to no symbol.

        Nothing in the file has a line range that contains a blank line
        between two functions, so :func:`_innermost_enclosing` returns
        ``None`` for that line and ``changed_symbols`` stays empty.
        Impact score is still zero — LOW bucket.
        """
        self._ensure_package(tmp_path, "src/pkg")
        source = (
            "def foo():\n"  # line 1
            "    return 1\n"  # line 2
            "\n"  # line 3  — the blank gap we will target
            "\n"  # line 4
            "def bar():\n"  # line 5
            "    return 2\n"  # line 6
        )
        self._write_module(tmp_path, "src/pkg/mod.py", source)

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (3,), ()),  # blank line between foo and bar
        )
        impact = compute_impact(change_set, tmp_path)

        assert impact.changed_symbols == []
        assert impact.blast_radius_score == "LOW"

    def test_renamed_file_uses_new_path_for_indexing(self, tmp_path: Path) -> None:
        """A rename surfaces the new path in ``changed_files`` and indexes it.

        The ``old_path`` is metadata for downstream consumers; the
        indexer works exclusively against ``path`` (the post-image
        location).
        """
        self._ensure_package(tmp_path, "src/pkg")
        self._write_module(
            tmp_path,
            "src/pkg/renamed.py",
            "def foo():\n    return 1\n",
        )

        change_set = ChangeSet(
            repo_path=tmp_path,
            base_sha=None,
            head_sha=None,
            files=(
                FileChange(
                    path="src/pkg/renamed.py",
                    change_kind="renamed",
                    old_path="src/pkg/original.py",
                    hunks=(self._hunk(added=(2,)),),
                ),
            ),
        )
        impact = compute_impact(change_set, tmp_path)

        assert "src/pkg/renamed.py" in impact.changed_files
        qnames = {ref.qualified_name for ref in impact.changed_symbols}
        assert "pkg.renamed.foo" in qnames

    def test_explicit_cache_db_path_is_honored(self, tmp_path: Path) -> None:
        """Passing ``cache_db=`` uses the explicit location, not ``.trikon/state.db``."""
        self._ensure_package(tmp_path, "src/pkg")
        self._write_module(
            tmp_path,
            "src/pkg/mod.py",
            "def foo():\n    return 1\n",
        )
        cache_db = tmp_path / "custom" / "cache.db"

        change_set = self._change_set_with_hunks(
            tmp_path,
            ("src/pkg/mod.py", (2,), ()),
        )
        compute_impact(change_set, tmp_path, cache_db=cache_db)

        # Cache file lands where the caller asked, not at .trikon/.
        assert cache_db.exists()
        assert not (tmp_path / ".trikon" / "state.db").exists()
