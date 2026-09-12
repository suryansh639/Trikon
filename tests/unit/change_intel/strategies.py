"""Hypothesis strategies for :mod:`trikon.change_intel` property tests.

The strategies here are kept deliberately narrow: they produce the smallest
values that still exercise the invariants a test cares about. Anything richer
(random Python source, real diffs, real symbol names) belongs in a strategy
file dedicated to the module under test.

Two composite strategies live here:

* :func:`dep_dag` — random DAGs of :class:`SymbolDef` values used by the
  ``DepGraph`` model-based test (Task 3.2).
* :func:`python_source` — syntactically-valid Python module source used by
  the ``fast_index_symbols`` property test (Task 4.2). Sources are built by
  templated string concatenation rather than AST construction so the shape
  Hypothesis shrinks toward is easy to read in a failure repro.

Later tasks (6.2, 8.3) will add more strategies alongside these.
"""

from __future__ import annotations

import keyword
import string
from typing import Final

from hypothesis import strategies as st

from trikon.change_intel.models import SymbolDef


def _make_symbol(name: str) -> SymbolDef:
    """Build a minimal :class:`SymbolDef` keyed on ``name``.

    The concrete field values do not matter for the graph-traversal tests —
    only ``qualified_name`` (uniqueness across the DAG) and ``file_sha``
    (stable across a single test run) participate in dep-graph identity.
    ``file_sha`` is fixed rather than random so every node in a generated
    DAG shares the same snapshot, matching the "one file, many symbols"
    invariant enforced by :meth:`DepGraph.upsert_symbols`.
    """
    return SymbolDef(
        qualified_name=f"pkg.mod.{name}",
        kind="function",
        file_path="src/pkg/mod.py",
        file_sha="0" * 64,
        start_line=1,
        end_line=1,
        start_byte=0,
        end_byte=1,
        is_public=True,
    )


@st.composite
def dep_dag(
    draw: st.DrawFn,
    *,
    min_nodes: int = 2,
    max_nodes: int = 30,
    max_edges: int = 100,
) -> tuple[list[SymbolDef], list[tuple[int, int]]]:
    """Generate a random DAG suitable for :class:`DepGraph` traversal tests.

    Returns ``(nodes, edges)`` where ``nodes`` is a list of ``SymbolDef``
    values with unique qualified names and ``edges`` is a list of
    ``(source_index, target_index)`` pairs. Every edge satisfies
    ``source_index < target_index`` so the graph is a DAG by construction;
    test callers decide whether to traverse edges forward or reversed.

    Bounds keep the generated cases small enough that :meth:`DepGraph`
    round-trip inserts, edge upserts, and reverse-BFS traversal stay well
    under Hypothesis' 2-second per-example deadline even on the slowest CI
    runner.
    """
    n_nodes = draw(st.integers(min_value=min_nodes, max_value=max_nodes))
    nodes = [_make_symbol(f"n{i}") for i in range(n_nodes)]

    # Only pairs with source < target are legal — that keeps the graph acyclic.
    max_possible_edges = n_nodes * (n_nodes - 1) // 2
    edge_ceiling = min(max_edges, max_possible_edges)
    n_edges = draw(st.integers(min_value=0, max_value=edge_ceiling))

    # De-duplicate as we go so the DB-side INSERT OR IGNORE isn't shouldering
    # dup filtering silently, and so the returned edge list matches what
    # the reference networkx graph will see.
    seen: set[tuple[int, int]] = set()
    edges: list[tuple[int, int]] = []
    for _ in range(n_edges):
        if len(seen) >= edge_ceiling:
            break
        src = draw(st.integers(min_value=0, max_value=n_nodes - 2))
        tgt = draw(st.integers(min_value=src + 1, max_value=n_nodes - 1))
        pair = (src, tgt)
        if pair in seen:
            continue
        seen.add(pair)
        edges.append(pair)

    return nodes, edges


# ---------------------------------------------------------------------------
# Python-source strategy — feeds the fast_index_symbols property test.
# ---------------------------------------------------------------------------


_KEYWORDS: Final[frozenset[str]] = frozenset(keyword.kwlist) | frozenset(
    getattr(keyword, "softkwlist", ())
)
"""Every reserved / soft-reserved word we must never emit as an identifier.

Includes hard keywords (``def``, ``class``, ``if`` …) and the soft keywords
Python added in 3.10+ (``match``, ``case``, ``type``, and the bare
underscore). Filtering both categories keeps generated sources unambiguous
regardless of interpreter version.
"""


_BASE_LETTERS: Final[st.SearchStrategy[str]] = st.text(
    alphabet=string.ascii_lowercase, min_size=1, max_size=6
)
"""Body of an identifier: 1-6 lowercase ASCII letters, no digits."""


_PREFIX: Final[st.SearchStrategy[str]] = st.sampled_from(["", "", "_"])
"""Optional leading underscore.

Sampled with a 1-in-3 weight so roughly a third of generated identifiers
are underscore-prefixed and exercise the ``is_public`` heuristic in
:func:`trikon.change_intel.ast_indexer.fast_index_symbols`. A more precise
weight is not worth the strategy-construction cost — Hypothesis' shrinker
will still march toward the empty prefix on failure.
"""


def _identifier() -> st.SearchStrategy[str]:
    """Generate a valid, non-keyword Python identifier.

    Public names (``foo``) and private-looking names (``_foo``) are both
    produced. Digits are intentionally excluded — the body of an identifier
    can start with a digit only *after* the first character, and admitting
    that would just complicate the strategy without changing coverage.
    """
    return (
        st.tuples(_PREFIX, _BASE_LETTERS)
        .map(lambda pair: pair[0] + pair[1])
        .filter(lambda name: name not in _KEYWORDS)
    )


@st.composite
def _method_source(draw: st.DrawFn, indent: str) -> str:
    """Render a single class-body method as source text.

    ``indent`` is the leading whitespace shared by every line in the method
    definition (the class-body indent for a top-level method, +4 per class
    nesting level for methods on nested classes). The body is always a
    single ``pass`` so that byte ranges land on statements the indexer knows
    how to recognise.
    """
    name = draw(_identifier())
    is_async = draw(st.booleans())
    prefix = "async " if is_async else ""
    return f"{indent}{prefix}def {name}(self):\n{indent}    pass"


@st.composite
def _class_source(
    draw: st.DrawFn,
    depth: int,
    max_depth: int,
    indent: str,
) -> str:
    """Render a class definition, optionally nesting one more class inside.

    ``depth`` tracks how many enclosing classes we are inside; once
    ``depth + 1`` reaches ``max_depth`` the recursion stops. The body always
    contains at least one statement — a ``pass`` when nothing else was drawn
    — so :func:`ast.unparse`-like emitters and CPython's parser both accept
    the result.
    """
    name = draw(_identifier())
    body_indent = indent + "    "
    body_parts: list[str] = []

    n_methods = draw(st.integers(min_value=0, max_value=3))
    for _ in range(n_methods):
        body_parts.append(draw(_method_source(body_indent)))

    if depth + 1 < max_depth and draw(st.booleans()):
        body_parts.append(draw(_class_source(depth + 1, max_depth, body_indent)))

    if not body_parts:
        body_parts.append(f"{body_indent}pass")

    body = "\n".join(body_parts)
    return f"{indent}class {name}:\n{body}"


@st.composite
def _top_function_source(draw: st.DrawFn) -> str:
    """Render a top-level ``def`` / ``async def`` with a ``pass`` body."""
    name = draw(_identifier())
    is_async = draw(st.booleans())
    prefix = "async " if is_async else ""
    return f"{prefix}def {name}():\n    pass"


@st.composite
def _top_assignment_source(draw: st.DrawFn) -> str:
    """Render a module-level assignment, half the time with an annotation."""
    name = draw(_identifier())
    annotated = draw(st.booleans())
    value = draw(st.integers(min_value=0, max_value=100))
    if annotated:
        return f"{name}: int = {value}"
    return f"{name} = {value}"


@st.composite
def python_source(draw: st.DrawFn) -> str:
    """Generate a syntactically-valid Python module.

    The output always contains at least one top-level function; roughly half
    the time it also contains a class (with up to three methods and possibly
    one nested class); roughly a third of the time it contains a module-level
    assignment. Identifiers are lowercase ASCII with a ~33% chance of an
    underscore prefix, giving the ``is_public`` heuristic something to
    disagree with.

    Decorators, imports, comprehensions, and non-trivial expressions are all
    intentionally out of scope — Task 4.2 only needs to exercise the four
    symbol kinds the fast indexer captures (function, class, method,
    assignment).
    """
    parts: list[str] = []

    # Guaranteed at least one top-level function.
    n_functions = draw(st.integers(min_value=1, max_value=3))
    for _ in range(n_functions):
        parts.append(draw(_top_function_source()))

    # ~50% chance of a class (which may itself nest one more class inside).
    if draw(st.booleans()):
        parts.append(draw(_class_source(depth=0, max_depth=2, indent="")))

    # ~30% chance of a module-level assignment.
    if draw(st.sampled_from([True, True, True, False, False, False, False])):
        parts.append(draw(_top_assignment_source()))

    return "\n\n".join(parts) + "\n"


__all__ = ["dep_dag", "python_source"]
