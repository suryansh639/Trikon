# Feature: trikon-engine-fail-safe, Property 6: Collection parsing and attribution
"""Property test: Collection_Pass parsing and attribution.

*For any* generated ``pytest --collect-only`` pytest-json-report payload with
N leaf items and a set of failed collectors carrying tracebacks, and any sets
of changed paths and broken-import files:

- :func:`~trikon.verify.collection.parse_collection_report` yields
  ``collected == N``;
- it yields one error per failed collector, in report order, with the
  collector's file path, its ``E`` line message cut to 1000 characters and
  the repo-relative paths of the traceback frames inside the repository;
- :func:`~trikon.verify.collection.classify_collection_errors` keeps each
  error's path and message and marks it attributable exactly when its path is
  changed, any in-repo frame is changed, or its path holds a Broken_Import.

The expected values come from the generated model, not from the parser. The
generator builds a collector tree (session, ``.``, directories, modules,
classes, leaf functions) in which modules and classes pass, fail or skip,
then emits the collectors in a shuffled order. Every failed collector gets a
traceback whose frames the model already knows to be inside or outside the
repository:

- in-repo frames are rendered relative (``tests/x.py``, ``./tests/x.py``,
  ``tests\\x.py``), under the Docker prefix, or under the host prefix, in
  both pytest's short style and CPython's ``File "...", line N`` style;
- out-of-repo frames reuse in-repo relative paths under foreign roots,
  including near misses of a repo prefix (``/workspace/repository/``) and
  ``..`` climbs, so a parser that matched on suffixes would mark the wrong
  errors attributable.

The payload's ``summary`` block carries an arbitrary ``collected`` value, so
the leaf count cannot come from it.

**Validates: Requirements 1.2, 2.6, 2.7**
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from trikon.evidence.report import CollectionError
from trikon.verify.collection import (
    RawCollectionError,
    classify_collection_errors,
    parse_collection_report,
)

# ---------------------------------------------------------------------------
# Fixed vocabulary
# ---------------------------------------------------------------------------

# Longest message the parser keeps for one error (design §5).
_MESSAGE_LIMIT = 1000

_DOCKER_PREFIX = "/workspace/repo/"

# (prefix handed to the parser, repo root as it appears in traceback frames).
# The second entry passes a forward-slash prefix for a backslash-rendered
# root, so the parser's separator normalisation is exercised.
_HOST_PREFIXES: tuple[tuple[str, str], ...] = (
    ("C:\\work\\repo", "C:\\work\\repo\\"),
    ("C:/work/repo/", "C:\\work\\repo\\"),
    ("/home/dev/repo", "/home/dev/repo/"),
)

# Roots outside the repository under every host prefix above. Several are
# near misses of a repo prefix; the last two climb out of the rootdir.
_OUTSIDE_ROOTS: tuple[str, ...] = (
    "/usr/local/lib/python3.11/site-packages/",
    "/workspace/repository/",
    "/home/dev/repo-old/",
    "C:\\work\\repo2\\",
    "C:\\Python311\\Lib\\",
    "../outside/",
    "..\\sibling\\",
)

# How an in-repo frame path is rendered.
_IN_REPO_STYLES: tuple[str, ...] = ("relative", "dot", "backslash", "docker", "host")

_FUNCTIONS: tuple[str, ...] = ("<module>", "import_module", "_gcd_import", "helper")

# Indented source lines printed under a short-style frame.
_SOURCE_LINES: tuple[str, ...] = (
    "    from pkg.gone import x",
    "    import nonexistent_dep",
    "    return _bootstrap._gcd_import(name[level:], package, level)",
    "    ???",
)

_EXCEPTION_TYPES: tuple[str, ...] = (
    "ModuleNotFoundError",
    "ImportError",
    "SyntaxError",
    "NameError",
    "AttributeError",
)

# Message detail characters. No ``:`` and no ``"``, so an ``E`` text line can
# never look like a traceback frame of either style.
_DETAIL_ALPHABET = string.ascii_letters + string.digits + " _'(),=-."

_NAME: st.SearchStrategy[str] = st.from_regex(r"\A[a-z][a-z0-9_]{0,5}\Z")

_TEST_MODULE_PATH: st.SearchStrategy[str] = st.builds(
    lambda directory, name: f"tests/{directory}test_{name}.py",
    st.sampled_from(("", "unit/", "api/", "unit/deep/")),
    _NAME,
)

_SOURCE_PATH: st.SearchStrategy[str] = st.builds(
    lambda directory, name: f"{directory}{name}.py",
    st.sampled_from(("src/pkg/", "pkg/", "")),
    _NAME,
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Frame:
    """One traceback frame: its rendered path and, if in the repo, its path."""

    rendered: str
    repo_path: str | None
    lineno: int


@dataclass(frozen=True, slots=True)
class _Failure:
    """A failed collector and what the parser must report for it."""

    longrepr: str
    path: str
    message: str
    frame_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Scenario:
    """A rendered report, the model behind it and the attribution inputs."""

    text: str
    repo_prefixes: tuple[str, ...]
    leaf_count: int
    failures: tuple[_Failure, ...]  # in report order
    changed_paths: frozenset[str]
    broken_import_files: frozenset[str]


def _dedup(paths: list[str]) -> tuple[str, ...]:
    """Keep the first occurrence of each path, in order."""
    return tuple(dict.fromkeys(paths))


def _parent(node_id: str) -> str:
    """The directory collector that lists ``node_id`` (``"."`` at the top)."""
    return node_id.rsplit("/", 1)[0] if "/" in node_id else "."


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _frames(draw: st.DrawFn, universe: tuple[str, ...], host_root: str) -> _Frame:
    """A frame inside the repo (any rendering) or outside it."""
    lineno = draw(st.integers(min_value=1, max_value=999))
    rel = draw(st.sampled_from(universe))
    if draw(st.booleans()):
        style = draw(st.sampled_from(_IN_REPO_STYLES))
        host_sep = "\\" if "\\" in host_root else "/"
        rendered = {
            "relative": rel,
            "dot": "./" + rel,
            "backslash": rel.replace("/", "\\"),
            "docker": _DOCKER_PREFIX + rel,
            "host": host_root + rel.replace("/", host_sep),
        }[style]
        return _Frame(rendered=rendered, repo_path=rel, lineno=lineno)
    if draw(st.integers(min_value=0, max_value=7)) == 0:
        # Not a ``.py`` path, so neither frame pattern applies.
        return _Frame(rendered="<frozen importlib._bootstrap>", repo_path=None, lineno=lineno)
    root = draw(st.sampled_from(_OUTSIDE_ROOTS))
    sep = "\\" if "\\" in root else "/"
    return _Frame(rendered=root + rel.replace("/", sep), repo_path=None, lineno=lineno)


@st.composite
def _error_texts(draw: st.DrawFn) -> str:
    """The text of one ``E`` line: an exception type and a detail.

    Starts and ends with a non-space character, so the parser's stripping
    leaves it unchanged. The optional padding pushes the joined message past
    the 1000-character cut.
    """
    exception = draw(st.sampled_from(_EXCEPTION_TYPES))
    detail = draw(st.text(alphabet=_DETAIL_ALPHABET, min_size=1, max_size=30)).strip() or "boom"
    padding = draw(st.one_of(st.just(0), st.integers(min_value=300, max_value=1100)))
    return f"{exception}: {detail}{'x' * padding}"


@st.composite
def _failures(draw: st.DrawFn, path: str, universe: tuple[str, ...], host_root: str) -> _Failure:
    """A failed collector's ``longrepr`` and the error the parser must report.

    Layout: optional pytest header lines, short-style frames each followed by
    an indented source line, then 1 to 3 ``E`` lines. An ``E`` line holds
    either an exception text or a CPython-style ``File "...", line N`` frame,
    as pytest prints a SyntaxError.
    """
    lines: list[str] = []
    if draw(st.booleans()):
        lines.append(f"ImportError while importing test module '{_DOCKER_PREFIX}{path}'.")
        lines.append("Hint: make sure your test modules/packages have valid Python names.")
        lines.append("Traceback:")

    frame_order: list[_Frame] = []
    for frame in draw(st.lists(_frames(universe, host_root), max_size=4)):
        function = draw(st.sampled_from(_FUNCTIONS))
        lines.append(f"{frame.rendered}:{frame.lineno}: in {function}")
        lines.append(draw(st.sampled_from(_SOURCE_LINES)))
        frame_order.append(frame)

    error_texts: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        if draw(st.booleans()):
            frame = draw(_frames(universe, host_root))
            text = f'File "{frame.rendered}", line {frame.lineno}'
            frame_order.append(frame)
        else:
            text = draw(_error_texts())
        marker = "E" + " " * draw(st.integers(min_value=1, max_value=3))
        lines.append(marker + text)
        error_texts.append(text)

    return _Failure(
        longrepr="\n".join(lines),
        path=path,
        message="\n".join(error_texts)[:_MESSAGE_LIMIT],
        frame_paths=_dedup([f.repo_path for f in frame_order if f.repo_path is not None]),
    )


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    """A collector tree rendered as a shuffled pytest-json-report payload."""
    host_prefix, host_root = draw(st.sampled_from(_HOST_PREFIXES))
    repo_prefixes = tuple(draw(st.permutations((_DOCKER_PREFIX, host_prefix))))
    module_paths = draw(st.lists(_TEST_MODULE_PATH, max_size=6, unique=True))
    source_paths = draw(st.lists(_SOURCE_PATH, min_size=1, max_size=4, unique=True))
    universe = _dedup([*module_paths, *source_paths])

    collectors: list[dict[str, object]] = [
        {"nodeid": "", "outcome": "passed", "result": [{"nodeid": ".", "type": "Dir"}]}
    ]
    failures: dict[str, _Failure] = {}
    leaf_count = 0

    # Directory collectors, each listing its direct children.
    dir_children: dict[str, list[str]] = {".": []}
    for module in module_paths:
        child = module
        while True:
            parent = _parent(child)
            siblings = dir_children.setdefault(parent, [])
            if child not in siblings:
                siblings.append(child)
            if parent == ".":
                break
            child = parent
    for directory, children in dir_children.items():
        collectors.append(
            {
                "nodeid": directory,
                "outcome": "passed",
                "result": [
                    {"nodeid": c, "type": "Module" if c.endswith(".py") else "Dir"}
                    for c in children
                ],
            }
        )

    # Module collectors, their classes and leaf functions.
    for module in module_paths:
        outcome = draw(st.sampled_from(("passed", "failed", "skipped")))
        if outcome == "failed":
            failure = draw(_failures(module, universe, host_root))
            failures[module] = failure
            collectors.append(
                {"nodeid": module, "outcome": "failed", "result": [], "longrepr": failure.longrepr}
            )
            continue
        if outcome == "skipped":
            collectors.append(
                {
                    "nodeid": module,
                    "outcome": "skipped",
                    "result": [],
                    "longrepr": f"('{_DOCKER_PREFIX}{module}', 1, 'Skipped: not here')",
                }
            )
            continue

        children: list[dict[str, object]] = []
        for lineno, name in enumerate(draw(st.lists(_NAME, max_size=4, unique=True))):
            children.append(
                {"nodeid": f"{module}::test_{name}", "type": "Function", "lineno": lineno}
            )
            leaf_count += 1
        for name in draw(st.lists(_NAME, max_size=2, unique=True)):
            class_id = f"{module}::Test{name.capitalize()}"
            children.append({"nodeid": class_id, "type": "Class"})
            if draw(st.booleans()):
                failure = draw(_failures(module, universe, host_root))
                failures[class_id] = failure
                collectors.append(
                    {
                        "nodeid": class_id,
                        "outcome": "failed",
                        "result": [],
                        "longrepr": failure.longrepr,
                    }
                )
                continue
            methods = draw(st.lists(_NAME, max_size=3, unique=True))
            collectors.append(
                {
                    "nodeid": class_id,
                    "outcome": "passed",
                    "result": [
                        {"nodeid": f"{class_id}::test_{m}", "type": "Function"} for m in methods
                    ],
                }
            )
            leaf_count += len(methods)
        collectors.append({"nodeid": module, "outcome": "passed", "result": children})

    shuffled = draw(st.permutations(collectors))
    report_order = tuple(failures[str(c["nodeid"])] for c in shuffled if c["outcome"] == "failed")
    payload = {
        "exitcode": 2 if failures else 0,
        "root": _DOCKER_PREFIX.rstrip("/"),
        # Deliberately unrelated to the real count.
        "summary": {"total": 0, "collected": draw(st.integers(min_value=0, max_value=50))},
        "collectors": shuffled,
        "tests": [],
    }

    attribution_paths = st.sampled_from((*universe, "docs/notes.md"))
    return _Scenario(
        text=json.dumps(payload),
        repo_prefixes=repo_prefixes,
        leaf_count=leaf_count,
        failures=report_order,
        changed_paths=frozenset(draw(st.sets(attribution_paths, max_size=4))),
        broken_import_files=frozenset(draw(st.sets(attribution_paths, max_size=3))),
    )


def _attributable(failure: _Failure, changed: frozenset[str], broken: frozenset[str]) -> bool:
    """The glossary's Attributable_Collection_Error, over the model's frames."""
    if failure.path in changed or failure.path in broken:
        return True
    return any(path in changed for path in failure.frame_paths)


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(scenario=_scenarios())
def test_collection_parsing_and_attribution(scenario: _Scenario) -> None:
    """Feature: trikon-engine-fail-safe, Property 6: Collection parsing and attribution."""
    outcome = parse_collection_report(scenario.text, repo_prefixes=scenario.repo_prefixes)

    # Requirement 1.2: the leaf items, whatever ``summary`` says.
    assert outcome.timed_out is False
    assert outcome.collected == scenario.leaf_count

    # One error per failed collector, in report order.
    assert outcome.errors == tuple(
        RawCollectionError(path=f.path, message=f.message, frame_paths=f.frame_paths)
        for f in scenario.failures
    )
    for error in outcome.errors:
        assert len(error.message) <= _MESSAGE_LIMIT
        for frame_path in error.frame_paths:
            assert "\\" not in frame_path, frame_path
            assert not frame_path.startswith(("/", "./", "../")), frame_path
            assert ":" not in frame_path, frame_path

    # Requirements 2.6 and 2.7: path and message kept, attribution exact.
    classified = classify_collection_errors(
        outcome.errors,
        changed_paths=scenario.changed_paths,
        broken_import_files=scenario.broken_import_files,
    )
    assert classified == tuple(
        CollectionError(
            path=f.path,
            message=f.message,
            attributable=_attributable(f, scenario.changed_paths, scenario.broken_import_files),
        )
        for f in scenario.failures
    )
