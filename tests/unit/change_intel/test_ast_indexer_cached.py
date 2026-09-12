"""Cache-hit and cache-miss tests for :func:`index_files`.

Covers Task 5.3 in ``.kiro/specs/change-intelligence/tasks.md``. The headline
invariant is **Property 4 / Validates: Requirements 2.2**: once a
``(file_path, file_sha)`` pair has landed in the SQLite cache, a subsequent
``index_files`` call for the same pair must hydrate symbols from the cache
without invoking the libcst parser at all. Regressing on that promise turns
the incremental-index cost model into "cold every time" and quietly blows
the 500 ms warm-reindex ceiling from ``design.md §7``.

The property is exercised by monkey-patching ``libcst.parse_module`` in the
indexer's own namespace with a sentinel that either raises on entry (proving
non-invocation) or counts invocations via ``wraps=`` (proving the exact
parse count on mixed cache hit / cache miss batches). Alongside that, we
pin down insertion-order preservation, mutation-driven cache invalidation,
and the :class:`AstParseError` propagation contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import libcst
import pytest

from trikon.change_intel import AstParseError
from trikon.change_intel.ast_indexer import index_files

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_SAMPLE_SOURCE_ONE: str = "def foo():\n    return 1\n"
_SAMPLE_SOURCE_TWO: str = (
    "def foo():\n    return 2\n\n\nclass Bar:\n    def m(self):\n        pass\n"
)


def _write(path: Path, source: str) -> Path:
    """Write ``source`` to ``path`` and return the path unchanged."""
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Property 4 — cache hit does not re-parse. Validates: Requirements 2.2.
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_parser(tmp_path: Path) -> None:
    """A second call for the same (path, sha) must not touch ``libcst.parse_module``.

    **Property 4 / Validates: Requirements 2.2.**

    First call primes the SQLite cache. Second call runs under a monkey-patch
    whose ``side_effect`` raises — if the indexer ever reaches the parser,
    the ``RuntimeError`` propagates out and this test fails. Byte-for-byte
    equality of the returned :class:`SymbolDef` lists then confirms that
    the hydration path reconstructs the exact same rows the parser emitted.
    """
    file = _write(tmp_path / "sample.py", _SAMPLE_SOURCE_ONE)
    cache = tmp_path / "state.db"

    result1 = index_files([file], cache_db=cache)

    with patch(
        "trikon.change_intel.ast_indexer.libcst.parse_module",
        side_effect=RuntimeError("libcst.parse_module should not be called on cache hit"),
    ) as mock_parse:
        result2 = index_files([file], cache_db=cache)

    mock_parse.assert_not_called()
    assert result2 == result1
    # Sanity: the primed row is not empty, so the "no parse" claim is meaningful.
    assert len(result2[file]) == 1
    assert result2[file][0].qualified_name.endswith(".foo")


def test_cache_miss_after_mutation_reinvokes_parser(tmp_path: Path) -> None:
    """Mutating the file bumps its SHA, which must force a re-parse on the next call.

    ``index_files`` consults :meth:`DepGraph.file_needs_reindex` before it
    hydrates from cache. When the on-disk SHA no longer matches the stored
    ``file_sha``, the routine must fall back to :func:`index_file` and let
    libcst re-parse. This test spies on the parser with ``wraps=`` so we
    can assert exactly one re-invocation, and then verifies that the new
    symbol set reflects the mutated contents (a fresh ``Bar`` class the
    original file did not contain).
    """
    file = _write(tmp_path / "sample.py", _SAMPLE_SOURCE_ONE)
    cache = tmp_path / "state.db"

    result1 = index_files([file], cache_db=cache)
    assert [s.qualified_name.split(".")[-1] for s in result1[file]] == ["foo"]

    # Now mutate — new content, new SHA, cache must miss.
    _write(file, _SAMPLE_SOURCE_TWO)

    spy = MagicMock(wraps=libcst.parse_module)
    with patch("trikon.change_intel.ast_indexer.libcst.parse_module", spy):
        result2 = index_files([file], cache_db=cache)

    assert spy.call_count == 1
    qnames = {s.qualified_name.split(".")[-1] for s in result2[file]}
    assert qnames == {"foo", "Bar", "m"}


# ---------------------------------------------------------------------------
# Batch behaviour — one call, three files.
# ---------------------------------------------------------------------------


def test_multiple_files_parse_once_each_then_zero_on_second_call(tmp_path: Path) -> None:
    """First batch triggers one parse per file; the second batch triggers none.

    Three distinct files exercise the per-file loop inside
    :func:`index_files`; the cold pass must call the parser exactly three
    times, and the warm pass exactly zero. This is the concrete evidence
    for the warm-reindex target in ``design.md §7`` — if the loop were
    re-parsing on cache hits, this count would be six on the warm pass,
    not zero.
    """
    files = [_write(tmp_path / f"m{i}.py", f"def fn_{i}():\n    return {i}\n") for i in range(3)]
    cache = tmp_path / "state.db"

    cold_spy = MagicMock(wraps=libcst.parse_module)
    with patch("trikon.change_intel.ast_indexer.libcst.parse_module", cold_spy):
        cold_result = index_files(files, cache_db=cache)
    assert cold_spy.call_count == 3
    assert set(cold_result.keys()) == set(files)

    warm_spy = MagicMock(wraps=libcst.parse_module)
    with patch("trikon.change_intel.ast_indexer.libcst.parse_module", warm_spy):
        warm_result = index_files(files, cache_db=cache)
    assert warm_spy.call_count == 0
    assert warm_result == cold_result


def test_batch_with_one_mutated_file_reparses_only_that_file(tmp_path: Path) -> None:
    """Mixing cache hits with a single cache miss triggers exactly one parse.

    Only the mutated file's SHA drifts; the other two match their stored
    values and must be hydrated from SQLite. Anything greater than one
    parse invocation means the cache check is spuriously invalidating
    unchanged files, which would blow the warm-reindex budget as the
    working set grows.
    """
    files = [_write(tmp_path / f"m{i}.py", f"def fn_{i}():\n    return {i}\n") for i in range(3)]
    cache = tmp_path / "state.db"

    # Prime.
    index_files(files, cache_db=cache)

    # Mutate exactly one file.
    _write(files[1], "def fn_1_renamed():\n    return 99\n")

    spy = MagicMock(wraps=libcst.parse_module)
    with patch("trikon.change_intel.ast_indexer.libcst.parse_module", spy):
        result = index_files(files, cache_db=cache)

    assert spy.call_count == 1
    # The mutated file's symbol set reflects the rename.
    mutated_qnames = {s.qualified_name.split(".")[-1] for s in result[files[1]]}
    assert mutated_qnames == {"fn_1_renamed"}
    # The untouched files still resolve to their original single symbol.
    for i in (0, 2):
        assert [s.qualified_name.split(".")[-1] for s in result[files[i]]] == [f"fn_{i}"]


# ---------------------------------------------------------------------------
# Insertion-order preservation.
# ---------------------------------------------------------------------------


def test_result_dict_preserves_input_ordering(tmp_path: Path) -> None:
    """``dict(result).keys()`` iterates in the caller-supplied order.

    ``design.md §2.2`` and the :func:`index_files` docstring both promise
    that dict iteration matches ``file_paths``. This test uses a non-
    alphabetical input order so a naive ``sorted()`` regression would flip
    the assertion. Python 3.7+ dicts preserve insertion order, so all we
    have to do is refuse to mangle it on the way through.
    """
    f_z = _write(tmp_path / "z.py", "def z():\n    pass\n")
    f_a = _write(tmp_path / "a.py", "def a():\n    pass\n")
    f_m = _write(tmp_path / "m.py", "def m():\n    pass\n")
    cache = tmp_path / "state.db"

    result = index_files([f_z, f_a, f_m], cache_db=cache)

    assert list(result.keys()) == [f_z, f_a, f_m]


# ---------------------------------------------------------------------------
# Error propagation — libcst parse failures surface as AstParseError.
# ---------------------------------------------------------------------------


def test_ast_parse_error_propagates_and_aborts_batch(tmp_path: Path) -> None:
    """A syntactically invalid file in the batch raises :class:`AstParseError`.

    :func:`index_files` deliberately does not swallow parse errors — the
    caller decides whether to retry with a filtered list. This test both
    confirms that the exception surfaces (rather than being wrapped in a
    generic error or, worse, silently skipped) and pins down the shape of
    the error so callers can rely on ``file_path`` to identify the bad
    file.
    """
    good = _write(tmp_path / "good.py", _SAMPLE_SOURCE_ONE)
    broken = _write(tmp_path / "broken.py", "def broken(:\n    pass\n")
    cache = tmp_path / "state.db"

    with pytest.raises(AstParseError) as excinfo:
        index_files([good, broken], cache_db=cache)

    # The offending file is identified in POSIX form on the exception.
    assert Path(excinfo.value.file_path) == broken


def test_repeated_cache_hit_calls_stay_stable(tmp_path: Path) -> None:
    """Three consecutive calls on unchanged files all hit the cache.

    Belt-and-braces check for Property 4: even after two prior calls, a
    third call still hydrates from the cache instead of falling back to
    the parser. That matters because the SQLite connection is reopened
    on every :func:`index_files` invocation (via the :class:`DepGraph`
    context manager) — the freshness check has to survive that lifecycle
    to hold at scale.
    """
    file = _write(tmp_path / "sample.py", _SAMPLE_SOURCE_ONE)
    cache = tmp_path / "state.db"

    first = index_files([file], cache_db=cache)

    with patch(
        "trikon.change_intel.ast_indexer.libcst.parse_module",
        side_effect=RuntimeError("cache hit expected"),
    ) as mock_parse:
        second = index_files([file], cache_db=cache)
        third = index_files([file], cache_db=cache)

    mock_parse.assert_not_called()
    assert second == first
    assert third == first
