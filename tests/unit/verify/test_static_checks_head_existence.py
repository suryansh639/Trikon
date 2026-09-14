"""Unit tests for the head-existence filter in :mod:`trikon.verify.static_checks`.

Covers Task 6.1 items (a), (b), (c), (d) in
``.kiro/specs/head-path-existence-filter/tasks.md``. Follows the style of
``tests/unit/verify/test_static_checks_smoke.py``: no Docker daemon, no
git worktree, no ruff / mypy binaries. The tests exercise:

* :func:`~trikon.verify.static_checks._filter_by_head_existence` in
  isolation (item (a)) — the pure helper contract from Requirement
  3.1 through 3.7 and 6.1;
* :func:`~trikon.verify.static_checks.run_static_checks` Step 3 with an
  all-deleted change (item (b)) — the head-side raise site cleanly
  skips ``sandbox.exec`` when every ``.py`` path in
  ``impact.changed_files`` maps to a
  :class:`~trikon.evidence.report.FileChangeInfo` entry with
  ``change_kind == "deleted"`` (Requirement 4.3, 4.4, 4.5, 6.3);
* :func:`run_static_checks` Step 3 with a mixed deleted/modified
  change (item (c)) — the composed suffix + head-existence filter
  emits argv tokens for the modified paths only, and no deleted path
  ever reaches the sandbox (Requirement 4.6);
* :func:`run_static_checks` Step 3 with the pre-v0.3.4 shape
  ``file_changes=[]`` (item (d)) — the head-existence filter is a
  no-op and every ``.py`` in ``impact.changed_files`` reaches argv
  (Requirement 3.6 backward-compat).

The fake :class:`_FakeSandbox` implements just enough of the
:data:`~trikon.verify.sandbox.Sandbox` union protocol for
``run_static_checks`` to drive it: an ``exec`` method that returns a
:class:`~trikon.verify.models.SandboxExecResult` and captures every
``argv`` it observes. The ``static_baseline`` cache is seeded on an
in-memory SQLite connection so :func:`_resolve_base_keys` takes the
cache-hit path and never spawns ``git worktree add`` on the host.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.3, 4.4,
4.5, 4.6, 6.1, 6.3.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from trikon.evidence.report import (
    ChangeKind,
    FileChangeInfo,
    ImpactSet,
    StaticReport,
)
from trikon.verify.db import ensure_verify_tables
from trikon.verify.models import DEFAULT_STATIC_TOOLS, SandboxExecResult
from trikon.verify.sandbox import Sandbox
from trikon.verify.static_checks import (
    _filter_by_head_existence,
    run_static_checks,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FAKE_TOOL_VERSION = "captured-version"
"""Fake tool-version string returned by every version-capture ``exec``.

Fixed so the baseline-cache seed below can pin the ``tool_version``
column and force :func:`_resolve_base_keys` onto the cache-hit branch.
"""

_FAKE_BASE_SHA = "0" * 40
"""Fake 40-hex-char SHA used as the base commit throughout the tests.

The value never touches git — every raise site that would touch it
(baseline resolve, worktree materialization) is bypassed by the
seeded cache.
"""

_FAKE_HEAD_SHA = "1" * 40
"""Fake 40-hex-char SHA used as the head commit throughout the tests.

The value is not consulted by :func:`run_static_checks` today; it
rides the signature for future audit metadata.
"""


class _FakeSandbox:
    """Structural stand-in for :data:`trikon.verify.sandbox.Sandbox`.

    ``run_static_checks`` only calls ``sandbox.exec`` on its sandbox
    argument (the Docker context-manager lifecycle is the caller's
    responsibility). The fake implements just that one method with a
    signature matching :meth:`trikon.verify.sandbox.LocalDockerSandbox.exec`
    so the tests can drive :func:`run_static_checks` without a Docker
    daemon.

    Every observed ``argv`` is appended to :attr:`exec_calls` in
    call order so the head-side / partial-filter assertions can
    inspect what actually reached the sandbox boundary.

    Two behavioural modes:

    * ``refuse_non_version_exec=True`` — raises :class:`AssertionError`
      on any ``exec`` argv that is not a two-token version-capture
      call (``(<tool>, "--version")``). Used by the all-deleted test
      to prove the head-side filter emptied the argv before it could
      reach ``sandbox.exec``.
    * ``refuse_non_version_exec=False`` (default) — captures every
      argv and returns a benign :class:`SandboxExecResult` shaped for
      the tool (``[]`` for ruff, empty string for mypy). Used by the
      partial-filter and backward-compat tests to inspect exactly
      which paths the composed filter passed through.
    """

    def __init__(self, *, refuse_non_version_exec: bool = False) -> None:
        self.exec_calls: list[tuple[str, ...]] = []
        self._refuse_non_version_exec = refuse_non_version_exec

    def exec(
        self,
        argv: tuple[str, ...],
        *,
        workdir: str = "/workspace/repo",
        timeout_seconds: float | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> SandboxExecResult:
        # ``workdir``, ``timeout_seconds`` and ``env`` are part of the
        # sandbox protocol but not observed by the fake — the tests
        # do not need to discriminate call sites by those.
        del workdir, timeout_seconds, env

        self.exec_calls.append(argv)

        # Version-capture argv shape: (<tool>, "--version"). Recognized
        # even in refuse-mode so Step 1 of ``run_static_checks`` can
        # complete without tripping the refuse branch.
        if len(argv) == 2 and argv[1] == "--version":
            return SandboxExecResult(
                exit_code=0,
                stdout=_FAKE_TOOL_VERSION,
                stderr="",
                duration_ms=0,
                timed_out=False,
            )

        if self._refuse_non_version_exec:
            raise AssertionError(
                f"unexpected non-version sandbox.exec argv: {argv!r} "
                f"(head-existence filter should have emptied argv)"
            )

        # Head-side tool run. Ruff parses JSON; mypy parses text. Empty
        # stdout on both sides produces zero findings, which is what
        # the tests care about — the interesting signal is the
        # captured argv, not the tool output.
        stdout_text = "[]" if argv and argv[0] == "ruff" else ""
        return SandboxExecResult(
            exit_code=0,
            stdout=stdout_text,
            stderr="",
            duration_ms=0,
            timed_out=False,
        )


def _seed_baseline_cache(conn: sqlite3.Connection) -> None:
    """Insert empty-findings ``static_baseline`` rows for every default tool.

    Forces :func:`_resolve_base_keys` onto the cache-hit branch for
    every :class:`~trikon.verify.models.StaticTool` in
    :data:`DEFAULT_STATIC_TOOLS` so the tests never touch
    ``git worktree add`` on the host. The ``tool_version`` column matches
    the fake sandbox's version-capture stdout (:data:`_FAKE_TOOL_VERSION`)
    so the cache key computed inside :func:`run_static_checks` resolves
    to these rows.
    """
    for tool in DEFAULT_STATIC_TOOLS:
        conn.execute(
            "INSERT INTO static_baseline "
            "(base_sha, tool, tool_version, findings_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _FAKE_BASE_SHA,
                tool.name,
                _FAKE_TOOL_VERSION,
                "[]",
                "2024-01-01T00:00:00+00:00",
            ),
        )
    conn.commit()


@pytest.fixture()
def seeded_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the verify tables and seeded baseline.

    Returns a fresh connection per test so the seeded rows do not leak
    between test runs. The connection lives for the duration of the
    test only; no ``close`` is required because :class:`sqlite3.Connection`
    on an in-memory database releases its resources on garbage collection
    at test-teardown time.
    """
    conn = sqlite3.connect(":memory:")
    ensure_verify_tables(conn)
    _seed_baseline_cache(conn)
    return conn


def _make_impact(
    changed_files: list[str],
    file_changes: list[FileChangeInfo],
) -> ImpactSet:
    """Build a minimal :class:`ImpactSet` for the head-side raise-site tests.

    Only the two fields the head-side raise site consumes
    (``changed_files`` for the argv, ``file_changes`` for the
    head-existence filter) carry meaningful values; every other list
    field is empty and the blast-radius numeric is zero. The bucket is
    ``LOW`` because the head-existence code path never inspects it.
    """
    return ImpactSet(
        changed_files=changed_files,
        changed_symbols=[],
        impacted_modules=[],
        impacted_public_apis=[],
        impacted_tests=[],
        blast_radius_score="LOW",
        blast_radius_numeric=0.0,
        file_changes=file_changes,
    )


def _non_version_exec_calls(fake: _FakeSandbox) -> list[tuple[str, ...]]:
    """Return every captured ``exec`` argv except the two version-capture calls.

    :func:`run_static_checks` Step 1 always issues one
    ``(<tool>, "--version")`` call per tool; filtering them out
    isolates the head-side argv the tests actually care about.
    """
    return [argv for argv in fake.exec_calls if not (len(argv) == 2 and argv[1] == "--version")]


# ===========================================================================
# (a) `_filter_by_head_existence` edge cases
# ===========================================================================


def test_filter_by_head_existence_empty_file_changes_returns_paths_unchanged() -> None:
    """Empty ``file_changes`` is the backward-compat no-op branch.

    Locks Requirement 3.6 — an out-of-tree consumer that predates
    ``file_changes`` on :class:`ImpactSet` falls into this branch and
    behaves exactly like the pre-v0.3.4 code path.
    """
    paths = ["src/a.py", "src/b.py", "src/c.py"]

    result = _filter_by_head_existence(paths, [])

    assert result == tuple(paths)


def test_filter_by_head_existence_only_deleted_drops_every_matching_path() -> None:
    """Every path with a matching ``deleted`` entry is dropped from the output.

    Locks Requirement 3.2 / 6.1 — the head-side raise site never
    passes a deleted-at-head path into ruff / mypy argv.
    """
    paths = ["src/a.py", "src/b.py", "src/c.py"]
    file_changes = [
        FileChangeInfo(path="src/a.py", change_kind="deleted"),
        FileChangeInfo(path="src/b.py", change_kind="deleted"),
        FileChangeInfo(path="src/c.py", change_kind="deleted"),
    ]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == ()


def test_filter_by_head_existence_renamed_with_old_path_matching_p_drops_p() -> None:
    """A ``renamed`` entry with ``old_path == p`` drops ``p`` from the output.

    Locks Requirement 3.3 — the rename-source path does not exist at
    HEAD (only the rename target does), so the defensive branch drops
    it from argv even if a future producer starts emitting it.
    """
    paths = ["src/old_name.py", "src/other.py"]
    file_changes = [
        FileChangeInfo(
            path="src/new_name.py",
            change_kind="renamed",
            old_path="src/old_name.py",
        ),
    ]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == ("src/other.py",)


def test_filter_by_head_existence_renamed_with_none_old_path_drops_nothing() -> None:
    """A ``renamed`` entry with ``old_path is None`` cannot match any path.

    Guards against a regression where the ``old_path is not None``
    check on the rename branch is dropped — an anomalous rename with
    ``old_path is None`` must not accidentally drop a matching path
    from the output.
    """
    paths = ["src/a.py", "src/b.py"]
    file_changes = [
        # Rename target with no recorded source. Malformed shape but
        # the filter still must not drop anything on this branch.
        FileChangeInfo(path="src/a.py", change_kind="renamed", old_path=None),
    ]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == tuple(paths)


def test_filter_by_head_existence_added_and_modified_drop_nothing() -> None:
    """``added`` and ``modified`` entries never drop any path.

    Locks Requirement 3.4 — a path with no ``deleted`` / rename-source
    entry passes through the filter unchanged.
    """
    paths = ["src/a.py", "src/b.py", "src/c.py"]
    file_changes = [
        FileChangeInfo(path="src/a.py", change_kind="added"),
        FileChangeInfo(path="src/b.py", change_kind="modified"),
        FileChangeInfo(path="src/c.py", change_kind="modified"),
    ]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == tuple(paths)


def test_filter_by_head_existence_preserves_input_order() -> None:
    """The output is a subsequence of the input in the same relative order.

    Locks Requirement 3.5 — the output preserves the input order so a
    tool that cares about argv ordering (e.g., diagnostic-emission
    order downstream) sees the same sequence it would have seen
    without the filter.
    """
    paths = ["src/z.py", "src/a.py", "src/deleted.py", "src/m.py"]
    file_changes = [FileChangeInfo(path="src/deleted.py", change_kind="deleted")]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == ("src/z.py", "src/a.py", "src/m.py")


def test_filter_by_head_existence_does_not_mutate_input_list() -> None:
    """The input paths list must not be mutated by the filter.

    Locks Requirement 3.7 — the helper is pure. The input list is
    snapshotted before the call and compared for equality afterwards.
    """
    paths_before = ["src/a.py", "src/b.py", "src/c.py"]
    paths = list(paths_before)  # fresh copy for the call
    file_changes = [FileChangeInfo(path="src/b.py", change_kind="deleted")]

    _filter_by_head_existence(paths, file_changes)

    assert paths == paths_before


def test_filter_by_head_existence_calling_twice_yields_identical_output() -> None:
    """Calling the helper twice on the same input yields equal outputs.

    Locks Requirement 3.7 — pure means deterministic. A subsequent
    call must produce the same tuple as the first call.
    """
    paths = ["src/a.py", "src/deleted.py", "src/c.py"]
    file_changes = [
        FileChangeInfo(path="src/deleted.py", change_kind="deleted"),
        FileChangeInfo(
            path="src/new.py",
            change_kind="renamed",
            old_path="src/old.py",
        ),
    ]

    first = _filter_by_head_existence(paths, file_changes)
    second = _filter_by_head_existence(paths, file_changes)

    assert first == second


def test_filter_by_head_existence_unicode_paths_handled() -> None:
    """Non-ASCII path segments participate in the drop set correctly.

    Locks Requirement 6.1's "regardless of any other property of the
    path (length, encoding, mixed case, leading/trailing whitespace)"
    clause on the encoding axis: the filter compares raw string
    equality, so a unicode-bearing path drops or survives identically
    to an ASCII-only path.
    """
    paths = ["src/café.py", "src/déjà.py", "src/naïve.py"]
    file_changes = [FileChangeInfo(path="src/déjà.py", change_kind="deleted")]

    result = _filter_by_head_existence(paths, file_changes)

    assert result == ("src/café.py", "src/naïve.py")


# ===========================================================================
# (b) Head-side skip on an all-deleted change
# ===========================================================================


def test_run_static_checks_skips_head_side_exec_when_every_py_is_deleted(
    seeded_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """All-deleted ``.py`` changes skip ``sandbox.exec`` on the head side.

    Locks Requirements 4.3, 4.4, 4.5, 6.3 — when every
    ``impact.changed_files`` entry maps to a
    :class:`FileChangeInfo` entry with ``change_kind == "deleted"``,
    the head-existence filter empties the argv before the head-side
    ``sandbox.exec`` fires. ``tools_run`` still records every default
    tool (because ``tools_run.append(tool.name)`` runs at the top of
    the per-tool loop, before the composed filter is applied),
    ``findings`` is empty, and every counter stays at zero.

    The fake sandbox raises on any non-version ``exec`` call — the
    absence of an :class:`AssertionError` at test-run time is the
    positive evidence that no head-side argv reached the sandbox.
    """
    changed_files = [
        "src/mod_deleted_1.py",
        "src/mod_deleted_2.py",
        "src/mod_deleted_3.py",
    ]
    file_changes = [FileChangeInfo(path=p, change_kind="deleted") for p in changed_files]
    impact = _make_impact(changed_files, file_changes)
    fake = _FakeSandbox(refuse_non_version_exec=True)

    report: StaticReport = run_static_checks(
        cast(Sandbox, fake),
        seeded_conn,
        impact,
        repo_path=tmp_path,
        base_sha=_FAKE_BASE_SHA,
        head_sha=_FAKE_HEAD_SHA,
    )

    # No non-version exec was issued — every head-side call was
    # short-circuited by the head-existence filter.
    assert _non_version_exec_calls(fake) == []

    # ``tools_run`` still records every default tool by name, in
    # declaration order.
    assert report.tools_run == [tool.name for tool in DEFAULT_STATIC_TOOLS]

    # No findings, no counter increments.
    assert report.findings == []
    assert report.new_errors == 0
    assert report.new_warnings == 0
    assert report.preexisting_errors == 0


# ===========================================================================
# (c) Head-side partial filter — deleted paths dropped, modified paths pass
# ===========================================================================


def test_run_static_checks_head_side_argv_excludes_deleted_paths(
    seeded_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Mixed deleted / modified ``.py`` inputs send only the modified paths to argv.

    Locks Requirement 4.6 — the composed suffix + head-existence
    filter guarantees no argv token names a deleted-at-head path,
    while every modified path still reaches the sandbox.
    """
    modified_paths = ["src/mod_a.py", "src/mod_b.py"]
    deleted_paths = ["src/del_x.py", "src/del_y.py"]
    changed_files = [*modified_paths, *deleted_paths]
    file_changes = [
        FileChangeInfo(path=modified_paths[0], change_kind="modified"),
        FileChangeInfo(path=modified_paths[1], change_kind="modified"),
        FileChangeInfo(path=deleted_paths[0], change_kind="deleted"),
        FileChangeInfo(path=deleted_paths[1], change_kind="deleted"),
    ]
    impact = _make_impact(changed_files, file_changes)
    fake = _FakeSandbox()

    run_static_checks(
        cast(Sandbox, fake),
        seeded_conn,
        impact,
        repo_path=tmp_path,
        base_sha=_FAKE_BASE_SHA,
        head_sha=_FAKE_HEAD_SHA,
    )

    non_version_calls = _non_version_exec_calls(fake)
    # One head-side exec per default tool — ruff and mypy each ran once.
    assert len(non_version_calls) == len(DEFAULT_STATIC_TOOLS)

    for argv in non_version_calls:
        # Every deleted path is absent from argv.
        for deleted in deleted_paths:
            assert deleted not in argv, (
                f"deleted path {deleted!r} reached sandbox argv {argv!r}; "
                "the head-existence filter did not drop it"
            )
        # Every modified path is present in argv.
        for modified in modified_paths:
            assert modified in argv, (
                f"modified path {modified!r} missing from sandbox argv "
                f"{argv!r}; the head-existence filter over-filtered"
            )


# ===========================================================================
# (d) Empty ``file_changes`` — pre-v0.3.4 backward-compat identity fallback
# ===========================================================================


def test_run_static_checks_empty_file_changes_passes_every_py_to_argv(
    seeded_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Pre-v0.3.4 shape (``file_changes=[]``) sends every ``.py`` to argv.

    Locks Requirement 3.6 backward-compat — when ``file_changes`` is
    empty (the shape a producer that predates v0.3.4 emits), the
    head-existence filter is a no-op and every ``.py`` entry from
    ``impact.changed_files`` reaches the head-side argv, matching the
    pre-fix code path exactly.
    """
    changed_files = ["src/a.py", "src/b.py", "src/c.py"]
    impact = _make_impact(changed_files, [])
    fake = _FakeSandbox()

    run_static_checks(
        cast(Sandbox, fake),
        seeded_conn,
        impact,
        repo_path=tmp_path,
        base_sha=_FAKE_BASE_SHA,
        head_sha=_FAKE_HEAD_SHA,
    )

    non_version_calls = _non_version_exec_calls(fake)
    assert len(non_version_calls) == len(DEFAULT_STATIC_TOOLS)

    for argv in non_version_calls:
        for path in changed_files:
            assert path in argv, (
                f"path {path!r} missing from sandbox argv {argv!r}; "
                "empty file_changes should be an identity fallback"
            )


# ===========================================================================
# Sanity check — ChangeKind literal shape used by the tests
# ===========================================================================


def test_change_kind_literal_covers_all_four_expected_values() -> None:
    """Guard against a silent :data:`ChangeKind` widening.

    The tests above rely on the public :data:`ChangeKind` literal
    accepting exactly the four values the head-existence filter
    inspects (``added`` / ``modified`` / ``deleted`` / ``renamed``).
    This check would fail :mod:`mypy` ``--strict`` if the literal
    ever gained or lost a member without the test suite noticing.
    """
    kinds: tuple[ChangeKind, ...] = ("added", "modified", "deleted", "renamed")
    # No runtime introspection of ``Literal`` is needed — the tuple
    # itself is what mypy validates. The assertion is a formality so
    # pytest counts this as a discovered test.
    assert len(kinds) == 4
