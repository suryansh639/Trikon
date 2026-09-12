"""Property + unit tests for :func:`trikon.change_intel.ast_indexer.fast_index_symbols`.

Covers Task 4.2 in ``.kiro/specs/change-intelligence/tasks.md``. The headline
property is **Property 3 / Validates: Requirements 2.1**: for every
:class:`SymbolDef` the fast indexer returns, the UTF-8 byte slice
``source_bytes[d.start_byte : d.end_byte]`` must re-parse to an AST node whose
kind matches ``d.kind`` and whose name matches the last component of
``d.qualified_name``. That single invariant, exercised across a hundred
Hypothesis-generated Python modules, is what keeps the byte-range → symbol
map trustworthy for downstream diff-hunk enclosure lookups.

Hand-written examples then pin down the exact shape of the return value on
inputs the property test would rarely stumble into on its own: the four
tracked ``SymbolKind`` values, the ``is_public`` heuristic (underscore vs
dunder), the module-level filter that discards multi-target assignments and
tuple unpacking, nested-function suppression, async functions, annotated
assignments, and :class:`~trikon.change_intel.errors.AstParseError`
propagation for malformed sources.
"""

from __future__ import annotations

import ast
import textwrap

import pytest
from hypothesis import given, settings

from tests.unit.change_intel.strategies import python_source
from trikon.change_intel import AstParseError
from trikon.change_intel.ast_indexer import fast_index_symbols

# ---------------------------------------------------------------------------
# Property 3 — byte-slice re-parse invariant.
# ---------------------------------------------------------------------------

#: For every ``SymbolKind`` the fast indexer emits, the AST node types that a
#: correctly-sliced byte range must parse to. Function and method share the
#: same underlying ``FunctionDef`` / ``AsyncFunctionDef`` shapes; assignments
#: cover both plain and annotated forms.
_EXPECTED_NODE_TYPES: dict[str, tuple[type[ast.AST], ...]] = {
    "function": (ast.FunctionDef, ast.AsyncFunctionDef),
    "class": (ast.ClassDef,),
    "method": (ast.FunctionDef, ast.AsyncFunctionDef),
    "assignment": (ast.Assign, ast.AnnAssign),
}


def _parse_chunk(chunk: str) -> ast.Module:
    """Parse a byte-slice chunk, dedenting once if ``ast.parse`` rejects the indent.

    Slices for methods on a class body start at the ``def`` token itself, so
    their leading line has no indentation, but subsequent body lines keep the
    original file's larger indent. CPython accepts that shape without help,
    but a defensive ``dedent`` retry means we never blame the property test
    for what is really a Hypothesis-generated whitespace quirk.
    """
    try:
        return ast.parse(chunk)
    except IndentationError:
        return ast.parse(textwrap.dedent(chunk))


@given(python_source())
@settings(max_examples=100, deadline=2000)
def test_fast_index_byte_slice_reparses_to_same_symbol(source: str) -> None:
    """For every SymbolDef, its byte slice re-parses to a matching AST node.

    **Property 3 / Validates: Requirements 2.1.**

    The invariant is checked node-by-node inside the loop: (1) the slice
    parses at all, (2) it yields exactly one top-level statement, (3) that
    statement's AST type is one of the ones ``kind`` corresponds to, and
    (4) any name carried by the node (function, method, class, or single
    assignment target) matches the last dotted component of the recorded
    ``qualified_name``. If any of these fail we know the byte range is
    lying about what lives at that offset.
    """
    symbols = fast_index_symbols(source, module_path="testmod")
    source_bytes = source.encode("utf-8")

    for sym in symbols:
        chunk = source_bytes[sym.start_byte : sym.end_byte].decode("utf-8")
        parsed = _parse_chunk(chunk)

        assert len(parsed.body) == 1, (
            f"expected exactly one top-level statement in chunk for "
            f"{sym.qualified_name!r}, got {ast.dump(parsed)!r}"
        )
        node = parsed.body[0]

        expected_types = _EXPECTED_NODE_TYPES[sym.kind]
        assert isinstance(node, expected_types), (
            f"chunk for {sym.qualified_name!r} parsed to "
            f"{type(node).__name__}, expected one of "
            f"{[t.__name__ for t in expected_types]}"
        )

        last_name = sym.qualified_name.split(".")[-1]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            assert node.name == last_name, (
                f"parsed node.name={node.name!r} does not match "
                f"symbol tail {last_name!r} (qname={sym.qualified_name!r})"
            )
        elif isinstance(node, ast.Assign):
            # fast_index_symbols only emits assignments with a single Name target.
            target = node.targets[0]
            assert isinstance(target, ast.Name), (
                f"expected ast.Name target for assignment {sym.qualified_name!r}, "
                f"got {type(target).__name__}"
            )
            assert target.id == last_name
        elif isinstance(node, ast.AnnAssign):
            assert isinstance(node.target, ast.Name), (
                f"expected ast.Name target for annotated assignment "
                f"{sym.qualified_name!r}, got {type(node.target).__name__}"
            )
            assert node.target.id == last_name


# ---------------------------------------------------------------------------
# Hand-written examples — exact shape of the return value on known inputs.
# ---------------------------------------------------------------------------


def test_empty_source_returns_empty_list() -> None:
    """An empty source yields no symbols and does not raise.

    The indexer is called on every file the diff parser sees, including
    newly-added empty ``__init__.py`` files; a stray raise here would turn
    those into ``AstParseError`` at the SDK boundary.
    """
    assert fast_index_symbols("") == []


def test_single_line_assignment_returns_one_symbol() -> None:
    """A module of ``X = 42`` produces exactly one ``assignment`` symbol.

    Byte range is verified against the source directly — this is the
    smallest witness of the byte-slice invariant that does not depend on
    Hypothesis.
    """
    source = "X = 42\n"
    symbols = fast_index_symbols(source, module_path="pkg.mod")

    assert len(symbols) == 1
    sym = symbols[0]
    assert sym.qualified_name == "pkg.mod.X"
    assert sym.kind == "assignment"
    assert sym.start_line == 1
    assert sym.end_line == 1
    assert sym.is_public is True

    slice_bytes = source.encode("utf-8")[sym.start_byte : sym.end_byte]
    assert slice_bytes.decode("utf-8") == "X = 42"


def test_annotated_assignment_is_captured() -> None:
    """``NAME: T = value`` at module level is also an ``assignment`` symbol."""
    source = "COUNT: int = 5\n"
    symbols = fast_index_symbols(source, module_path="m")

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "m.COUNT"
    assert symbols[0].kind == "assignment"


def test_annotated_attribute_assignment_is_not_captured() -> None:
    """``obj.attr: int = 5`` has a non-Name target and must not surface.

    ``_module_level_assignment_target`` filters :class:`ast.AnnAssign` down
    to single-Name targets specifically because otherwise every stray
    module-level ``sys.path: list[str] = ...`` monkeypatch would create a
    ghost symbol whose byte range points at the outer attribute expression,
    not at the identifier being bound.
    """
    source = "import sys\nsys.path: list[str] = []\n"
    symbols = fast_index_symbols(source, module_path="pkg")

    assert symbols == []


def test_async_top_level_function_is_captured_as_function() -> None:
    """``async def`` at module level lands as ``function`` (not a new kind)."""
    source = "async def fetch():\n    pass\n"
    symbols = fast_index_symbols(source, module_path="pkg")

    assert len(symbols) == 1
    assert symbols[0].kind == "function"
    assert symbols[0].qualified_name == "pkg.fetch"


def test_all_symbol_kinds_from_hand_written_module() -> None:
    """A single fixture module exercises every ``SymbolKind`` at once.

    Matches the shape sketched in ``design.md §2.2``: module-level constants,
    top-level functions, classes, methods, and nested classes with methods of
    their own. Every symbol is looked up by its expected qualified name so
    an accidental rename shows up as a ``KeyError`` rather than a silent
    match against the wrong entry.
    """
    source = (
        textwrap.dedent(
            """
            CONSTANT = 42

            def top_level_function():
                pass

            class SomeClass:
                def method(self):
                    pass

                class Inner:
                    def inner_method(self):
                        pass
            """
        ).strip()
        + "\n"
    )

    by_name = {s.qualified_name: s for s in fast_index_symbols(source, module_path="pkg.mod")}

    assert by_name["pkg.mod.CONSTANT"].kind == "assignment"
    assert by_name["pkg.mod.top_level_function"].kind == "function"
    assert by_name["pkg.mod.SomeClass"].kind == "class"
    assert by_name["pkg.mod.SomeClass.method"].kind == "method"
    assert by_name["pkg.mod.SomeClass.Inner"].kind == "class"
    assert by_name["pkg.mod.SomeClass.Inner.inner_method"].kind == "method"
    # No stray entries — exactly six symbols in the fixture.
    assert len(by_name) == 6


def test_is_public_heuristic_handles_underscore_and_dunder() -> None:
    """Underscore-prefixed names are private; dunders stay public.

    The three rules encoded in :func:`_is_qualified_name_public`:

    * A leading ``_`` on any component ⇒ private.
    * A dunder identifier (``__init__``, length ≥ 4, brackets both ends)
      overrides that rule and stays public.
    * A private component anywhere in the dotted path taints the whole name.
    """
    source = (
        textwrap.dedent(
            """
            def public_func():
                pass

            def _private_func():
                pass

            class Cls:
                def __init__(self):
                    pass

                def _helper(self):
                    pass
            """
        ).strip()
        + "\n"
    )

    by_name = {s.qualified_name: s.is_public for s in fast_index_symbols(source, module_path="pkg")}

    assert by_name["pkg.public_func"] is True
    assert by_name["pkg._private_func"] is False
    assert by_name["pkg.Cls"] is True
    assert by_name["pkg.Cls.__init__"] is True
    assert by_name["pkg.Cls._helper"] is False


def test_module_level_multi_target_and_unpacking_are_skipped() -> None:
    """``a = b = 1``, tuple unpacking, and attribute assign never surface.

    ``design.md §2.2`` restricts assignments to single ``ast.Name`` targets.
    Anything more elaborate is ignored, so downstream reference resolution
    is not misled by a symbol that has no unambiguous binding site.
    """
    source = (
        textwrap.dedent(
            """
            a = b = 1
            x, y = (2, 3)
            obj.attr = 5

            def real_symbol():
                pass
            """
        ).strip()
        + "\n"
    )

    qnames = [s.qualified_name for s in fast_index_symbols(source, module_path="")]
    assert qnames == ["real_symbol"]


def test_nested_function_body_is_not_traversed() -> None:
    """Functions defined inside a function body do not become symbols in v0.1.

    We walk class bodies (to find methods and nested classes) but never
    function bodies — anything nested inside a ``def`` is opaque to the
    indexer. If this changes we will want new ``SymbolKind`` values, not a
    silent widening of ``function``.
    """
    source = (
        textwrap.dedent(
            """
            def outer():
                def inner():
                    pass
            """
        ).strip()
        + "\n"
    )

    qnames = [s.qualified_name for s in fast_index_symbols(source, module_path="")]
    assert qnames == ["outer"]


@pytest.mark.parametrize(
    ("line_ending", "label"),
    [("\r\n", "CRLF"), ("\r", "lone-CR")],
    ids=["crlf", "cr"],
)
def test_byte_ranges_survive_non_lf_line_endings(line_ending: str, label: str) -> None:
    """CR and CRLF line endings still produce correct byte ranges.

    The line-start table used by :func:`_byte_range` recognises all three
    line terminators Python's tokenizer accepts (LF, CRLF, lone CR).
    Regressing on CRLF would silently offset every symbol's byte range on
    Windows-authored source, which is exactly the class of Heisenbug we
    want caught here.
    """
    source = f"def foo():{line_ending}    pass{line_ending}"
    symbols = fast_index_symbols(source, module_path="pkg")

    assert len(symbols) == 1, f"{label} source should yield one symbol"
    sym = symbols[0]
    assert sym.qualified_name == "pkg.foo"

    chunk = source.encode("utf-8")[sym.start_byte : sym.end_byte].decode("utf-8")
    # The chunk always starts with `def foo(` regardless of line-ending flavour.
    assert chunk.startswith("def foo("), f"{label} chunk was {chunk!r}"
    assert chunk.rstrip().endswith("pass"), f"{label} chunk was {chunk!r}"


def test_qualified_name_prefixes_with_module_path() -> None:
    """The ``module_path`` argument is prepended, class-nesting path threaded through."""
    source = "class Outer:\n    class Inner:\n        def method(self):\n            pass\n"
    symbols = fast_index_symbols(source, module_path="a.b")
    qnames = {s.qualified_name for s in symbols}

    assert qnames == {"a.b.Outer", "a.b.Outer.Inner", "a.b.Outer.Inner.method"}


def test_empty_module_path_leaves_names_unqualified() -> None:
    """When ``module_path`` is blank, symbol names stand alone (no leading dot)."""
    source = "def foo():\n    pass\n"
    symbols = fast_index_symbols(source)

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "foo"


def test_file_sha_and_file_path_are_threaded_verbatim() -> None:
    """Callers own the caching boundary — the indexer never touches ``file_sha``."""
    source = "def foo():\n    pass\n"
    symbols = fast_index_symbols(
        source,
        module_path="pkg",
        file_path="src/pkg/mod.py",
        file_sha="deadbeef" * 8,
    )

    assert len(symbols) == 1
    assert symbols[0].file_path == "src/pkg/mod.py"
    assert symbols[0].file_sha == "deadbeef" * 8


# ---------------------------------------------------------------------------
# AstParseError propagation.
# ---------------------------------------------------------------------------


def test_ast_parse_error_carries_file_path_and_line() -> None:
    """Broken Python raises :class:`AstParseError` with the file and line.

    ``fast_index_symbols`` is on the SDK's never-fail-open path — anything
    it raises must be a :class:`~trikon.change_intel.errors.ChangeIntelError`
    subclass, and the raise must carry enough context for the operator to
    identify the offending file.
    """
    broken = "def foo(:\n    pass\n"

    with pytest.raises(AstParseError) as excinfo:
        fast_index_symbols(broken, file_path="broken.py")

    err = excinfo.value
    assert err.file_path == "broken.py"
    assert err.line is not None
    assert err.__cause__ is not None
    assert isinstance(err.__cause__, SyntaxError)


def test_ast_parse_error_defaults_file_path_to_empty_string() -> None:
    """Callers who omit ``file_path`` still get a well-formed error.

    Nothing on the change-intel path currently omits ``file_path``, but the
    kwarg default is part of the signature and downstream code (tests,
    debug utilities, REPL sessions) may exercise it.
    """
    with pytest.raises(AstParseError) as excinfo:
        fast_index_symbols("def broken(\n")

    assert excinfo.value.file_path == ""
