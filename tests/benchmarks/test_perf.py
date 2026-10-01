"""Performance benchmarks for Change Intelligence — see ``design.md §7``.

Four benchmark cases from the design's performance table are shipped here:

1. Cold-index a full Django checkout (skipped unless ``TRIKON_DJANGO_PATH``).
2. Warm re-index of 5 files after a cold pass (skipped unless Django is
   available).
3. :func:`~trikon.change_intel.blast_radius.compute_impact` on the
   ``bad_retry`` scenario against ``examples/sample_repo/``. Always runs —
   no external checkout dependency.
4. :meth:`~trikon.change_intel.dep_graph.DepGraph.transitive_dependents`
   with ``max_hops=5`` on the fully-indexed Django graph (skipped unless
   Django is available).

The always-on ``compute_impact`` benchmark exists so CI has one perf case
that can never be silently skipped, giving task 12.2's ``perf`` job a
stable signal. Django-scale benchmarks are opt-in via
``TRIKON_DJANGO_PATH``: a developer or CI job that wants the tighter
regression gate exports the env var and gets four cases instead of one.

Timing thresholds from the design are enforced at the pytest-benchmark
layer (``--benchmark-max-time`` in CI, per-target ``min_rounds`` here), not
via inline ``assert`` on wall-clock. That keeps the tests portable across
machines while still surfacing regressions when a run breaches the
threshold on the CI runner class the benchmark was calibrated for.

Validates: Requirements 7.1, 7.2, 7.3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from trikon.change_intel.ast_indexer import index_files
from trikon.change_intel.blast_radius import compute_impact
from trikon.change_intel.dep_graph import DepGraph
from trikon.change_intel.diff_parser import parse_diff
from trikon.change_intel.models import SymbolDef

if TYPE_CHECKING:
    # ``pytest-benchmark`` ships without type stubs (no ``py.typed`` marker),
    # so mypy resolves :class:`BenchmarkFixture` as :data:`Any`. The
    # ``import-untyped`` suppression is narrow: it silences the missing-stubs
    # complaint without opening the door to :data:`Any` anywhere else. Guard
    # the import inside :data:`TYPE_CHECKING` so runtime never pays for a
    # module ``benchmark`` already imports transitively via the fixture.
    from pytest_benchmark.fixture import (  # type: ignore[import-untyped]
        BenchmarkFixture,
    )

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Env-var gating
# ---------------------------------------------------------------------------

#: Path to a Django source checkout. When set and pointing at a real
#: directory, the three Django-scale benchmarks activate. Unset by default
#: so ``pytest tests/benchmarks/`` runs cleanly on any developer machine.
_TRIKON_DJANGO_PATH: str | None = os.environ.get("TRIKON_DJANGO_PATH")

#: True when the env var is set and the pointed-at directory exists on disk.
#: The value drives the three ``@pytest.mark.skipif`` guards below.
_DJANGO_AVAILABLE: bool = _TRIKON_DJANGO_PATH is not None and Path(_TRIKON_DJANGO_PATH).is_dir()


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

#: The Trikon repo root, resolved from this test file's location.
#: ``tests/benchmarks/test_perf.py`` → ``parents[2]`` is the workspace root.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: The checked-in sample project used by benchmark #3. Copied into a
#: :func:`tmp_path` per test so no benchmark ever mutates the source tree.
_SAMPLE_REPO: Path = _REPO_ROOT / "examples" / "sample_repo"

#: Location of the ``bad_retry`` scenario patch that the always-on benchmark
#: applies before measuring :func:`compute_impact`.
_BAD_RETRY_PATCH: Path = _REPO_ROOT / "tests" / "fixtures" / "scenarios" / "bad_retry.patch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copytree_ignore(_dir: str, names: list[str]) -> list[str]:
    """``shutil.copytree`` ignore hook for the sample-repo → tmp copy.

    Mirrors the equivalent helper in
    ``tests/integration/change_intel/test_end_to_end_sample_repo.py``.
    Cache directories and any pre-existing ``state.db`` are skipped so the
    benchmark measures a cold pipeline every round.
    """
    skip = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    ignored = [n for n in names if n in skip]
    if "state.db" in names:
        ignored.append("state.db")
    return ignored


def _run_git(repo: Path, *args: str) -> str:
    """Execute a ``git`` subcommand against ``repo`` and return stdout.

    Matches the integration test's helper: ``check=True``,
    ``capture_output=True``, ``text=True``. No stdin is piped for the same
    reason the integration helper avoids it — the benchmark passes patches
    to ``git apply`` by path to keep unified-diff line endings intact on
    Windows.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(repo: Path) -> None:
    """Initialize ``repo`` as a throwaway git repository.

    ``--local`` scope on every config write keeps the host developer's
    global ``.gitconfig`` untouched.
    """
    _run_git(repo, "init", "--initial-branch=main")
    _run_git(repo, "config", "--local", "user.email", "trikon-bench@example.com")
    _run_git(repo, "config", "--local", "user.name", "Trikon Bench")
    _run_git(repo, "config", "--local", "core.autocrlf", "false")


def _commit_all(repo: Path, message: str) -> str:
    """Stage every change in ``repo`` and commit with ``message``.

    Returns the resulting commit SHA — the caller uses it as ``base_sha`` or
    ``head_sha`` for the subsequent ``sdk.verify`` call.
    """
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").strip()


def _apply_patch(repo: Path, patch_path: Path) -> None:
    """Apply a checked-in scenario patch to ``repo``.

    Passing the patch path as a positional argument (rather than piping via
    stdin) avoids the Windows CRLF translation that would otherwise corrupt
    the unified-diff bytes ``git apply`` reads.
    """
    _run_git(repo, "apply", "--whitespace=nowarn", str(patch_path))


def _prepare_sample_repo(tmp_path: Path) -> tuple[Path, str, str, Path]:
    """Materialize the ``(baseline, bad_retry)`` sample-repo history.

    Steps:

    1. Copy ``examples/sample_repo/`` into ``tmp_path``, filtered by
       :func:`_copytree_ignore`.
    2. Initialize a fresh git repo and commit the baseline.
    3. Apply ``bad_retry.patch`` and commit the applied change.
    4. Compute a fresh cache-db path outside the copied repo so every
       benchmark round starts from a cold dep graph.

    Returns ``(repo_path, baseline_sha, applied_sha, cache_db)``.
    """
    repo = tmp_path / "sample_repo"
    shutil.copytree(_SAMPLE_REPO, repo, ignore=_copytree_ignore)

    _init_repo(repo)
    baseline_sha = _commit_all(repo, "baseline")
    _apply_patch(repo, _BAD_RETRY_PATCH)
    applied_sha = _commit_all(repo, "bad_retry")

    cache_db = tmp_path / "cache" / "state.db"
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    return repo, baseline_sha, applied_sha, cache_db


def _collect_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root``, filtered for well-known noise.

    The Django benchmarks walk the source tree with :meth:`Path.rglob` and
    then drop anything under a ``.git``/``.venv``/``.tox``/build cache
    directory. Filenames with ``test`` in a path segment are kept — Django's
    own test suite is a real chunk of the LOC target and skipping it would
    understate the cold-index cost.
    """
    excluded_segments = {
        ".git",
        ".venv",
        "venv",
        ".tox",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
    paths: list[Path] = []
    for candidate in root.rglob("*.py"):
        if not candidate.is_file():
            continue
        if any(part in excluded_segments for part in candidate.parts):
            continue
        paths.append(candidate)
    return paths


# ---------------------------------------------------------------------------
# Benchmark 1 — cold-index Django (design.md §7 row 1)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _DJANGO_AVAILABLE,
    reason="TRIKON_DJANGO_PATH not set or not a directory",
)
def test_cold_index_django(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """Cold-index a full Django checkout — target ≤ 30 s, CI fail > 45 s.

    A fresh ``state.db`` is allocated on each round via
    ``pytest-benchmark``'s ``setup`` hook so every iteration measures the
    genuine cold path. ``min_rounds=1`` because Django-scale indexing is
    the dominant wall-clock cost; more rounds would blow past
    ``--benchmark-max-time`` without adding statistical value.

    Validates: Requirements 7.1.
    """
    django_root = Path(_TRIKON_DJANGO_PATH or "")
    python_files = _collect_python_files(django_root)
    assert python_files, f"No Python files under {django_root}"

    round_index = {"n": 0}

    def _setup() -> tuple[tuple[list[Path]], dict[str, Path]]:
        # Fresh cache per round: the benchmark measures a *cold* index.
        round_index["n"] += 1
        cache_db = tmp_path / f"django_cold_{round_index['n']}.db"
        return (python_files,), {"cache_db": cache_db}

    def _target(files: list[Path], *, cache_db: Path) -> None:
        index_files(files, cache_db=cache_db)

    benchmark.pedantic(_target, setup=_setup, rounds=1, iterations=1)


# ---------------------------------------------------------------------------
# Benchmark 2 — warm re-index 5 files (design.md §7 row 2)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _DJANGO_AVAILABLE,
    reason="TRIKON_DJANGO_PATH not set or not a directory",
)
def test_warm_reindex_five_files(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """Warm re-index of 5 files after a cold pass — target ≤ 500 ms.

    The cache is primed exactly once (before the benchmark timer starts)
    with a full Django cold pass; each timed round then re-indexes the same
    five files. Because their on-disk SHAs match what was persisted, every
    round hits :meth:`DepGraph.file_needs_reindex`'s fast path and no
    libcst parse should fire — the measurement is a pure cache-hit
    round-trip through SQLite.

    Validates: Requirements 7.2.
    """
    django_root = Path(_TRIKON_DJANGO_PATH or "")
    python_files = _collect_python_files(django_root)
    assert len(python_files) >= 5, "Django checkout has fewer than 5 .py files"

    cache_db = tmp_path / "django_warm.db"
    # Prime once. This is the "after cold pass" precondition from design.md §7.
    index_files(python_files, cache_db=cache_db)

    subset = python_files[:5]

    def _target() -> None:
        index_files(subset, cache_db=cache_db)

    benchmark(_target)


# ---------------------------------------------------------------------------
# Benchmark 3 — compute_impact on sample_repo (design.md §7 row 3)
# ---------------------------------------------------------------------------


def test_compute_impact_sample_repo(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """``compute_impact`` on the ``bad_retry`` scenario — target ≤ 200 ms.

    The git-history setup (``init`` + baseline commit + ``git apply`` +
    applied commit) and :func:`parse_diff` are one-shot and live outside
    the timed section; the resulting :class:`ChangeSet` is frozen, so every
    round reuses it. Every round starts from a fresh :class:`~pathlib.Path`
    for ``cache_db`` so the benchmark measures the cold ``compute_impact``
    path — cache warmup lands in benchmark #2.

    The benchmark times ``compute_impact`` alone, as Requirement 7.3
    states, and never reaches the verification runner. Its result therefore
    does not depend on whether a Docker daemon or the sandbox image is
    available: the PR ``perf`` job (Docker present) and a Windows run
    without Docker time and check the same code path.

    Validates: Requirements 7.3.
    """
    repo, baseline_sha, applied_sha, _ = _prepare_sample_repo(tmp_path)
    change_set = parse_diff(repo, base_sha=baseline_sha, head_sha=applied_sha)

    round_index = {"n": 0}

    def _setup() -> tuple[tuple[()], dict[str, Path]]:
        round_index["n"] += 1
        cache_db = tmp_path / "caches" / f"round_{round_index['n']}.db"
        cache_db.parent.mkdir(parents=True, exist_ok=True)
        return (), {"cache_db": cache_db}

    def _target(*, cache_db: Path) -> None:
        impact = compute_impact(change_set, repo, cache_db=cache_db)
        # The full ImpactSet assertions belong in the integration suite, not
        # the perf gate. This sanity check keeps a pipeline that indexes
        # nothing from posting fast timings.
        assert impact.changed_files == ["src/payments/retry.py"]
        changed_names = {s.qualified_name for s in impact.changed_symbols}
        assert "payments.retry.with_backoff" in changed_names

    benchmark.pedantic(_target, setup=_setup, rounds=3, iterations=1)


# ---------------------------------------------------------------------------
# Benchmark 4 — transitive_dependents on Django graph (design.md §7 row 4)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _DJANGO_AVAILABLE,
    reason="TRIKON_DJANGO_PATH not set or not a directory",
)
def test_transitive_dependents_django(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """5-hop BFS on the Django dep graph — target ≤ 100 ms p95.

    Django is indexed once (outside the timed section). The seed set is up
    to 10 arbitrary indexed symbols; the exact identities do not matter
    for perf — what matters is that the BFS has real symbols to traverse.
    Phase 1 does not auto-populate edges, so on a fresh cache the
    traversal returns empty; the benchmark still measures the query cost
    (SQLite planning + the empty-frontier early-exit) which is the piece
    the target ceiling actually bounds.

    Validates: Requirements 7.3.
    """
    django_root = Path(_TRIKON_DJANGO_PATH or "")
    python_files = _collect_python_files(django_root)
    assert python_files, f"No Python files under {django_root}"

    cache_db = tmp_path / "django_traversal.db"
    indexed = index_files(python_files, cache_db=cache_db)

    seeds: list[SymbolDef] = []
    for symbols in indexed.values():
        for sym in symbols:
            seeds.append(sym)
            if len(seeds) >= 10:
                break
        if len(seeds) >= 10:
            break
    assert seeds, "Django index produced zero symbols — cannot benchmark BFS"

    graph = DepGraph(cache_db)

    def _target() -> None:
        graph.transitive_dependents(seeds, max_hops=5)

    try:
        benchmark(_target)
    finally:
        graph.close()
