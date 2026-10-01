"""Pure Collection_Pass handling and TestReport assembly for the runner.

The runner's test stage is a short pipeline. Everything here is pure: no
I/O, no Docker, no clock reads. The runner owns the sandbox execs and the
clock, and feeds their results through these pieces in order:

1. :class:`TestBudget` splits the test stage's share of the Verdict_Deadline
   between the Collection_Pass and test execution (Requirement 1.8). The
   collection pass gets at most ``collection_share`` of the budget; execution
   gets whatever is left. Both timeouts are clamped at 0, and because both
   are measured against the same ``deadline_at``, a collection pass that
   stays within its timeout plus an execution that stays within its timeout
   never run past the deadline.

2. :func:`parse_collection_report` reads the pytest-json-report payload of
   ``pytest --collect-only --json-report``. The fields it relies on, as
   pytest-json-report 1.5.0 writes them:

   * ``collectors``: one entry per collector node (session, directories,
     packages, modules, classes). The plugin leaves the key out when no
     collector reported, which parses as "nothing collected".
   * ``collectors[*].nodeid``: the collector's node ID (``""`` for the
     session).
   * ``collectors[*].outcome``: ``"passed"``, ``"failed"`` or ``"skipped"``.
   * ``collectors[*].result``: the collector's children, each a mapping
     with a ``nodeid`` (plus ``type`` and sometimes ``lineno``).
   * ``collectors[*].longrepr``: the error text of a failed collector.

   ``collected`` counts leaf items: distinct child node IDs that are not
   themselves a collector. That keeps the count independent of how the
   plugin's ``summary`` block behaves across versions (Requirement 1.2).
   Each failed collector becomes a :class:`RawCollectionError` with its test
   file path, a trimmed message and the repo-relative traceback frame paths.
   A payload that does not decode, or whose shape does not match the layout
   above, raises :class:`~trikon.verify.errors.CollectionPassError` so the
   SDK fails closed (Requirements 7.1, 7.2).

3. :func:`classify_collection_errors` decides which errors the change caused.
   An error is an Attributable_Collection_Error when its file is changed,
   any traceback frame points at a changed file, or its file holds a
   Broken_Import.

4. :func:`assemble_test_report` builds the final
   :class:`~trikon.evidence.report.TestReport`. Every count comes from the
   parsed pytest execution report (Requirement 2.1); collection errors never
   change a count. The status is decided by the ordered rules documented on
   the function, so a run that executed nothing is never reported as
   ``passed`` for a Python change (Requirements 2.2, 2.3).

Imports are limited to the standard library, :mod:`trikon.evidence.report`,
:mod:`trikon.verify.errors` and :mod:`trikon.verify.strategy`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from trikon.evidence.report import (
    CollectionError,
    IncompleteReason,
    TestReport,
    TestResult,
)
from trikon.verify.errors import CollectionPassError
from trikon.verify.strategy import StrategyDecision

__all__ = [
    "CollectionOutcome",
    "RawCollectionError",
    "TestBudget",
    "assemble_test_report",
    "classify_collection_errors",
    "parse_collection_report",
]

# Longest message kept for one collection error.
_MESSAGE_LIMIT: int = 1000

# Path recorded for a failed collector with an empty node ID (the session).
_SESSION_PATH: str = "<session>"

# Message used when a failed collector carries no error text at all.
_NO_MESSAGE: str = "collection failed without an error message"

# A pytest short-traceback frame line, e.g. ``tests/test_x.py:6: in <module>``.
# The optional drive letter lets a Windows host path (``C:\repo\x.py:3: in f``)
# match too; the rest is the design's ``^(\S[^:]*\.py):\d+: ``.
_FRAME_LINE_RE: re.Pattern[str] = re.compile(r"^((?:[A-Za-z]:)?\S[^:]*\.py):\d+: ")

# A CPython-style frame, e.g. ``File "/workspace/repo/x.py", line 3`` (this is
# how a SyntaxError in a test module names the file).
_FILE_FRAME_RE: re.Pattern[str] = re.compile(r'File "([^"]+\.py)", line \d+')

# A pytest error line: ``E`` followed by up to three marker spaces, or a bare
# ``E`` for a blank error line. The remainder is the original error text.
_ERROR_LINE_RE: re.Pattern[str] = re.compile(r"^E(?: {1,3}(.*)|)$")

# An absolute POSIX path or an absolute Windows path (after backslashes are
# normalised to ``/``).
_ABSOLUTE_PATH_RE: re.Pattern[str] = re.compile(r"^(?:/|[A-Za-z]:/)")

_Status = Literal["passed", "failed", "skipped"]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawCollectionError:
    """One failed collector, before attribution.

    ``path`` is the part of the collector's node ID before the first ``::``
    (``"<session>"`` for the session collector). ``message`` is the trimmed
    error text. ``frame_paths`` holds the repo-relative POSIX paths of the
    traceback frames inside the repository, deduplicated, in the order they
    first appear.
    """

    path: str
    message: str
    frame_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    """What the Collection_Pass observed.

    ``timed_out`` is true when the collection exec ran past its timeout; the
    runner then records ``collected=0`` and no errors, because no report was
    read.
    """

    timed_out: bool
    collected: int
    errors: tuple[RawCollectionError, ...]


@dataclass(frozen=True, slots=True)
class TestBudget:
    """The test stage's time budget, measured on the monotonic clock.

    ``deadline_at`` is the monotonic time by which collection and execution
    must both have finished, and ``total_seconds`` the length of the whole
    budget. The Collection_Pass may use at most ``collection_share`` of it
    (Requirement 1.8).
    """

    deadline_at: float
    total_seconds: float
    collection_share: float = 0.25

    def collection_timeout(self, now: float) -> float:
        """Return the Collection_Pass timeout when it starts at ``now``.

        The smaller of the collection share and the time left before
        ``deadline_at``, never below 0.
        """
        return max(0.0, min(self.total_seconds * self.collection_share, self.deadline_at - now))

    def execution_timeout(self, now: float) -> float:
        """Return the execution timeout when it starts at ``now``.

        Everything left before ``deadline_at``, never below 0. A result of 0
        means the runner must not start the execution at all.
        """
        return max(0.0, self.deadline_at - now)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_collection_report(text: str, *, repo_prefixes: tuple[str, ...]) -> CollectionOutcome:
    """Parse a ``pytest --collect-only`` pytest-json-report payload.

    Args:
        text: The raw JSON read back from the collection report file.
        repo_prefixes: Absolute repository roots as they may appear in
            traceback frames, e.g. ``"/workspace/repo/"`` inside the Docker
            sandbox and the host repo path for the local backend. Each is
            stripped from frame paths; a trailing ``/`` is implied, and
            backslashes are treated as ``/``.

    Returns:
        A :class:`CollectionOutcome` with ``timed_out=False``. ``collected``
        is the number of distinct leaf node IDs; ``errors`` holds one
        :class:`RawCollectionError` per collector whose outcome is
        ``"failed"``, in report order.

    Raises:
        CollectionPassError: When ``text`` is not valid JSON, or when the
            payload is not a mapping, ``collectors`` is not a list, or a
            collector or child entry does not have the expected keys and
            types.
    """
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectionPassError(
            f"run_verification: collection report decode failed: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise CollectionPassError(
            f"run_verification: collection report is not a mapping (got {type(payload).__name__})"
        )

    # pytest-json-report omits ``collectors`` when nothing reported.
    collectors_field: object = payload.get("collectors", [])
    if not isinstance(collectors_field, list):
        raise CollectionPassError(
            "run_verification: collection report 'collectors' field is not a list "
            f"(got {type(collectors_field).__name__})"
        )

    prefixes = _normalise_prefixes(repo_prefixes)
    collector_ids: set[str] = set()
    child_ids: set[str] = set()
    errors: list[RawCollectionError] = []

    for entry in collectors_field:
        if not isinstance(entry, dict):
            raise CollectionPassError(
                "run_verification: collection report collector is not a mapping "
                f"(got {type(entry).__name__})"
            )
        node_id = _require_str(entry, "nodeid", where="collector")
        outcome = _require_str(entry, "outcome", where="collector")
        collector_ids.add(node_id)
        child_ids.update(_child_node_ids(entry))

        if outcome == "failed":
            longrepr = _optional_str(entry, "longrepr", where="collector")
            errors.append(
                RawCollectionError(
                    path=node_id.split("::")[0] or _SESSION_PATH,
                    message=_error_message(longrepr),
                    frame_paths=_frame_paths(longrepr, prefixes),
                )
            )

    return CollectionOutcome(
        timed_out=False,
        collected=len(child_ids - collector_ids),
        errors=tuple(errors),
    )


def _require_str(entry: dict[str, object], key: str, *, where: str) -> str:
    """Return ``entry[key]`` as a ``str`` or raise :class:`CollectionPassError`."""
    value = entry.get(key)
    if not isinstance(value, str):
        raise CollectionPassError(
            f"run_verification: collection report {where} {key!r} is not a string "
            f"(got {type(value).__name__})"
        )
    return value


def _optional_str(entry: dict[str, object], key: str, *, where: str) -> str:
    """Return ``entry[key]`` as a ``str``, ``""`` when absent or null.

    Raises :class:`CollectionPassError` when the key holds any other type.
    """
    value = entry.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CollectionPassError(
            f"run_verification: collection report {where} {key!r} is not a string "
            f"(got {type(value).__name__})"
        )
    return value


def _child_node_ids(collector: dict[str, object]) -> list[str]:
    """Return the node IDs in a collector's ``result`` list."""
    result: object = collector.get("result")
    if not isinstance(result, list):
        raise CollectionPassError(
            "run_verification: collection report collector 'result' is not a list "
            f"(got {type(result).__name__})"
        )
    node_ids: list[str] = []
    for child in result:
        if not isinstance(child, dict):
            raise CollectionPassError(
                "run_verification: collection report result entry is not a mapping "
                f"(got {type(child).__name__})"
            )
        node_ids.append(_require_str(child, "nodeid", where="result entry"))
    return node_ids


def _error_message(longrepr: str) -> str:
    """Return the trimmed message for one failed collector.

    The ``E`` lines of ``longrepr`` with their pytest marker removed, joined
    by newlines. When there are none, the first non-empty line. The result is
    cut to :data:`_MESSAGE_LIMIT` characters.
    """
    error_lines: list[str] = []
    for line in longrepr.splitlines():
        match = _ERROR_LINE_RE.match(line)
        if match is not None:
            error_lines.append((match.group(1) or "").rstrip())
    message = "\n".join(error_lines).strip()
    if not message:
        message = next((line.strip() for line in longrepr.splitlines() if line.strip()), "")
    if not message:
        message = _NO_MESSAGE
    return message[:_MESSAGE_LIMIT]


def _normalise_prefixes(repo_prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """Return the repo prefixes with ``/`` separators and a trailing ``/``."""
    normalised: list[str] = []
    for prefix in repo_prefixes:
        posix = prefix.replace("\\", "/")
        if not posix:
            continue
        normalised.append(posix if posix.endswith("/") else posix + "/")
    return tuple(normalised)


def _frame_paths(longrepr: str, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """Return the repo-relative traceback frame paths found in ``longrepr``.

    Both frame styles are read line by line, so the order follows the
    traceback. Frames outside the repository are dropped, and each path is
    kept once.
    """
    seen: dict[str, None] = {}
    for line in longrepr.splitlines():
        candidates: list[str] = []
        line_match = _FRAME_LINE_RE.match(line)
        if line_match is not None:
            candidates.append(line_match.group(1))
        candidates.extend(match.group(1) for match in _FILE_FRAME_RE.finditer(line))
        for candidate in candidates:
            relative = _repo_relative(candidate, prefixes)
            if relative is not None:
                seen.setdefault(relative, None)
    return tuple(seen)


def _repo_relative(raw_path: str, prefixes: tuple[str, ...]) -> str | None:
    """Map one frame path to a repo-relative POSIX path, or ``None``.

    An absolute path is kept only when it starts with one of ``prefixes``.
    A relative path is already relative to the pytest rootdir (the repo), so
    it is kept unless it climbs out with ``..``.
    """
    path = raw_path.replace("\\", "/")
    for prefix in prefixes:
        if path.startswith(prefix):
            return path[len(prefix) :] or None
    if _ABSOLUTE_PATH_RE.match(path):
        return None
    while path.startswith("./"):
        path = path[2:]
    if not path or path == ".." or path.startswith("../"):
        return None
    return path


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def classify_collection_errors(
    errors: Iterable[RawCollectionError],
    *,
    changed_paths: frozenset[str],
    broken_import_files: frozenset[str],
) -> tuple[CollectionError, ...]:
    """Turn raw errors into public :class:`CollectionError` records.

    ``attributable`` is true exactly when the error's path is in
    ``changed_paths``, any of its frame paths is in ``changed_paths``, or its
    path is in ``broken_import_files`` (the glossary's
    Attributable_Collection_Error). Order is preserved.
    """
    return tuple(
        CollectionError(
            path=error.path,
            message=error.message,
            attributable=(
                error.path in changed_paths
                or any(frame in changed_paths for frame in error.frame_paths)
                or error.path in broken_import_files
            ),
        )
        for error in errors
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_test_report(
    *,
    python_change: bool,
    decision: StrategyDecision,
    collection: CollectionOutcome,
    classified: tuple[CollectionError, ...],
    execution: TestReport | None,
    execution_timed_out: bool,
    coverage_map_stale: bool,
) -> TestReport:
    """Build the test stage's :class:`TestReport`.

    Counts (``total``, ``passed``, ``failed``, ``skipped``, ``duration_ms``)
    come only from ``execution``, the parsed pytest run, and are 0 when it is
    ``None`` (Requirement 2.1). ``executed`` is ``passed + failed``.
    Attributable errors are appended to ``failures`` as ``errored`` entries
    but never change a count. ``collection_errors`` lists every error with
    its ``attributable`` flag.

    ``incomplete_reasons`` follows the fixed order ``collection_timeout``,
    ``execution_timeout``, ``collection_error``; the last is added when any
    error is not attributable. ``incomplete`` is true exactly when the list
    is non-empty.

    The status is decided by the first rule that applies:

    1. Any attributable error: ``failed`` (Requirement 2.6).
    2. Strategy ``none`` for a change that is not a Python_Change: ``passed``
       with every count 0 (Requirement 2.4). No test run was started, so
       ``execution`` is ignored here. :func:`choose_strategy` never returns
       ``none`` for a Python change; if a caller passes that pair anyway it
       falls through to the later rules and ends ``skipped``.
    3. A collection or execution timeout: ``failed`` if any test failed
       before it, ``skipped`` otherwise (Requirement 2.10).
    4. ``failed > 0``: ``failed``.
    5. ``executed > 0``: ``passed``.
    6. Otherwise ``skipped`` (Requirements 2.2, 2.3).
    """
    attributable = [error for error in classified if error.attributable]
    incomplete_reasons = _incomplete_reasons(
        collection_timed_out=collection.timed_out,
        execution_timed_out=execution_timed_out,
        classified=classified,
    )
    no_run_passes = not attributable and decision.strategy == "none" and not python_change
    source = None if no_run_passes else execution

    total = source.total if source is not None else 0
    passed = source.passed if source is not None else 0
    failed = source.failed if source is not None else 0
    skipped = source.skipped if source is not None else 0
    duration_ms = source.duration_ms if source is not None else 0
    executed = passed + failed

    failures: list[TestResult] = list(source.failures) if source is not None else []
    failures.extend(
        TestResult(
            node_id=error.path,
            outcome="errored",
            duration_ms=0,
            failure_summary=error.message,
        )
        for error in attributable
    )

    status: _Status
    if attributable:
        status = "failed"
    elif no_run_passes:
        status = "passed"
    elif collection.timed_out or execution_timed_out:
        status = "failed" if failed > 0 else "skipped"
    elif failed > 0:
        status = "failed"
    elif executed > 0:
        status = "passed"
    else:
        status = "skipped"

    return TestReport(
        status=status,
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_ms=duration_ms,
        failures=failures,
        coverage_map_stale=coverage_map_stale,
        collected=collection.collected,
        executed=executed,
        strategy=decision.strategy,
        strategy_reasons=list(decision.reasons),
        incomplete=bool(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
        collection_errors=list(classified),
    )


def _incomplete_reasons(
    *,
    collection_timed_out: bool,
    execution_timed_out: bool,
    classified: tuple[CollectionError, ...],
) -> list[IncompleteReason]:
    """Return the incomplete-evidence reasons in their fixed order."""
    reasons: list[IncompleteReason] = []
    if collection_timed_out:
        reasons.append("collection_timeout")
    if execution_timed_out:
        reasons.append("execution_timeout")
    if any(not error.attributable for error in classified):
        reasons.append("collection_error")
    return reasons
