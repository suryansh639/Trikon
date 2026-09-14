"""Task 7.1 property tests — the head-existence filter contract, argv safety, and purity.

Three ``hypothesis``-driven property tests covering the deliverable-named
invariants from ``.kiro/specs/head-path-existence-filter/design.md §11``:

* **Property 1 — head-existence filter contract.** For any sequence of paths
  ``P`` and any sequence of :class:`FileChangeInfo` entries ``F``, the tuple
  :func:`trikon.verify.static_checks._filter_by_head_existence` returns is a
  subsequence of ``P`` in original order, and no output path ``p`` matches an
  ``F`` entry with ``(path == p, change_kind == "deleted")`` or
  ``(change_kind == "renamed", old_path == p)``. When ``F`` is empty the
  output equals ``tuple(P)`` — the backward-compatibility branch (design.md
  §5). *Validates Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6.*

* **Property 2 — no argv token ever names a deleted-at-head path.** For every
  :class:`~trikon.verify.models.StaticTool` in
  :data:`~trikon.verify.models.DEFAULT_STATIC_TOOLS`, no argv token
  substituted for the ``{files}`` sentinel by
  :func:`~trikon.verify.static_checks._expand_argv` on
  ``_filter_by_head_existence(_filter_by_suffix(paths, t.accepted_suffixes),
  file_changes)`` names a path whose :class:`FileChangeInfo` entry has
  ``change_kind == "deleted"`` or (``change_kind == "renamed"`` and
  ``old_path == token``). This is the deliverable-named property from
  ``requirements.md §4`` — the E902 channel closed at the source. *Validates
  Requirements 4.1, 4.2, 4.6, 6.1, 6.2.*

* **Property 4 — ``_filter_by_head_existence`` is pure.** For any ``P`` and
  ``F``, calling ``_filter_by_head_existence(P, F)`` twice yields tuples
  that compare equal and does not mutate either argument. Pure-function
  contract from ``design.md §5``. *Validates Requirement 3.7.*

Property 3 (Docker sandbox and ``--no-sandbox`` paths agree on the verdict)
is the integration-scope property covered by Task 8.1 in
``tests/integration/verify/test_click_repro_head_existence.py`` and is
deliberately out of scope here — this file is unit-scope only, no sandbox
side effects.

All three tests are annotated ``@settings(max_examples=100)`` and use only
composed ``hypothesis`` strategies; the ``old_path`` invariant (non-``None``
iff ``change_kind == "renamed"``) is enforced by the ``file_change_info()``
composite strategy so every generated :class:`FileChangeInfo` is
semantically well-formed by construction.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from trikon.evidence.report import ChangeKind, FileChangeInfo
from trikon.verify.models import DEFAULT_STATIC_TOOLS
from trikon.verify.static_checks import (
    _expand_argv,
    _filter_by_head_existence,
    _filter_by_suffix,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Path-shaped strings. A leading lowercase ASCII letter, then up to 60
# characters drawn from a POSIX-safe alphabet (lowercase, digits, ``_``,
# ``/``, ``.``, ``-``), then a ``.py`` suffix. The regex is intentionally
# narrow so ``PurePosixPath(p).suffix`` is deterministically ``".py"``
# (Property 2 needs :func:`_filter_by_suffix` to keep the path so
# :func:`_filter_by_head_existence` has something to drop; without a ``.py``
# tail every path would be pruned by the suffix filter first and Property 2
# would degenerate to a trivial vacuous truth over an empty tuple).
_PY_PATH_STRATEGY: st.SearchStrategy[str] = st.from_regex(r"\A[a-z][a-z0-9_/.-]{0,60}\.py\Z")

# Arbitrary path-shaped strings without a fixed extension. Used for the
# generic ``paths: Sequence[str]`` argument to :func:`_filter_by_head_existence`
# where the suffix filter is not in the picture (Properties 1 and 4).
_ANY_PATH_STRATEGY: st.SearchStrategy[str] = st.from_regex(r"\A[a-z][a-z0-9_/.-]{0,60}\Z")

_CHANGE_KIND_STRATEGY: st.SearchStrategy[ChangeKind] = st.sampled_from(
    ["added", "modified", "deleted", "renamed"]
)


@st.composite
def _file_change_info(draw: st.DrawFn) -> FileChangeInfo:
    """Generate a well-formed :class:`FileChangeInfo` value.

    Enforces the rename invariant at the strategy level: ``old_path`` is a
    non-``None`` path string iff ``change_kind == "renamed"``. Pydantic
    would otherwise accept ``FileChangeInfo(path="a.py", change_kind="added",
    old_path="b.py")`` as legal at the type level even though the
    semantic contract from ``design.md §3`` says ``old_path`` is
    meaningful only on the rename branch. Constraining the strategy keeps
    the property tests focused on the filter contract instead of
    accidentally probing the model's laxity.
    """
    change_kind: ChangeKind = draw(_CHANGE_KIND_STRATEGY)
    path: str = draw(_PY_PATH_STRATEGY)
    old_path: str | None = (
        draw(_PY_PATH_STRATEGY) if change_kind == "renamed" else draw(st.just(None))
    )
    return FileChangeInfo(path=path, change_kind=change_kind, old_path=old_path)


_FILE_CHANGES_STRATEGY: st.SearchStrategy[list[FileChangeInfo]] = st.lists(
    _file_change_info(),
    min_size=0,
    max_size=20,
)

_PY_PATHS_STRATEGY: st.SearchStrategy[list[str]] = st.lists(
    _PY_PATH_STRATEGY,
    min_size=0,
    max_size=20,
)

_ANY_PATHS_STRATEGY: st.SearchStrategy[list[str]] = st.lists(
    _ANY_PATH_STRATEGY,
    min_size=0,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dropped_paths(file_changes: Sequence[FileChangeInfo]) -> set[str]:
    """Mirror of :func:`_filter_by_head_existence`'s internal drop-set build.

    Duplicating this reduction rather than importing a private module
    helper keeps the property test independent of the helper's internal
    control flow: we assert against the *contract* (a path is dropped iff
    it matches a deleted entry or a rename-source ``old_path``), not
    against the implementation's set construction.
    """
    dropped: set[str] = set()
    for info in file_changes:
        if info.change_kind == "deleted":
            dropped.add(info.path)
        elif info.change_kind == "renamed" and info.old_path is not None:
            dropped.add(info.old_path)
    return dropped


def _is_subsequence(sub: Sequence[str], parent: Sequence[str]) -> bool:
    """Return True iff ``sub`` is an in-order subsequence of ``parent``.

    Two-pointer walk over ``parent``; every element of ``sub`` must appear
    in ``parent`` in the same relative order, but not necessarily
    contiguously. Duplicates in ``parent`` are consumed left-to-right so
    ``sub`` can carry the same element multiple times as long as
    ``parent`` does too.
    """
    it = iter(parent)
    return all(elem in it for elem in sub)


# ---------------------------------------------------------------------------
# Property 1 — head-existence filter contract
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(paths=_ANY_PATHS_STRATEGY, file_changes=_FILE_CHANGES_STRATEGY)
def test_filter_by_head_existence_is_subsequence(
    paths: list[str],
    file_changes: list[FileChangeInfo],
) -> None:
    """Feature: head-path-existence-filter, Property 1: the filter output is a subsequence of the input paths containing only head-existent entries; when file_changes is empty the output equals tuple(paths)."""
    result = _filter_by_head_existence(paths, file_changes)

    # Subsequence in original order.
    assert _is_subsequence(result, paths)

    # No output path is classified as deleted-at-head or as a rename source.
    dropped = _dropped_paths(file_changes)
    for p in result:
        assert p not in dropped

    # Empty-``file_changes`` no-op branch (design.md §5).
    if not file_changes:
        assert result == tuple(paths)


# ---------------------------------------------------------------------------
# Property 2 — no argv token ever names a deleted-at-head path
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(paths=_PY_PATHS_STRATEGY, file_changes=_FILE_CHANGES_STRATEGY)
def test_no_argv_token_is_deleted_at_head(
    paths: list[str],
    file_changes: list[FileChangeInfo],
) -> None:
    """Feature: head-path-existence-filter, Property 2: for every StaticTool in DEFAULT_STATIC_TOOLS, no argv token substituted for the {files} sentinel names a path whose FileChangeInfo entry has change_kind=="deleted" or (change_kind=="renamed" and old_path==token)."""
    dropped = _dropped_paths(file_changes)

    for tool in DEFAULT_STATIC_TOOLS:
        filtered = _filter_by_head_existence(
            _filter_by_suffix(paths, tool.accepted_suffixes),
            file_changes,
        )
        argv = _expand_argv(tool.argv_template, filtered)

        # Every token that was substituted for the ``{files}`` sentinel
        # must be a member of ``filtered`` (:func:`_expand_argv` only
        # substitutes the sentinel — non-sentinel tokens are the tool
        # name, flags, etc.). Restrict the check to the substituted
        # tokens by intersecting argv against the filtered set.
        filtered_set = set(filtered)
        substituted = [t for t in argv if t in filtered_set]

        for token in substituted:
            assert token not in dropped, (
                f"tool={tool.name!r} substituted argv token {token!r} names a deleted-at-head path"
            )


# ---------------------------------------------------------------------------
# Property 4 — ``_filter_by_head_existence`` is pure
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(paths=_ANY_PATHS_STRATEGY, file_changes=_FILE_CHANGES_STRATEGY)
def test_filter_by_head_existence_is_pure(
    paths: list[str],
    file_changes: list[FileChangeInfo],
) -> None:
    """Feature: head-path-existence-filter, Property 4: calling _filter_by_head_existence twice yields tuples that compare equal and does not mutate either argument."""
    paths_before = copy.deepcopy(paths)
    file_changes_before = copy.deepcopy(file_changes)

    first = _filter_by_head_existence(paths, file_changes)
    second = _filter_by_head_existence(paths, file_changes)

    # Idempotent on repeat calls with the same inputs.
    assert first == second

    # Neither argument mutated.
    assert paths == paths_before
    assert file_changes == file_changes_before
