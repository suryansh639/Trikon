# Feature: trikon-engine-fail-safe, Property 2: Relative-import resolution round trip
"""Property test: rendering a relative import and resolving it gives the target back.

*For any* file path F under a Module_Root with a non-empty package P, and any
absolute target T under the same root that shares a prefix of length k >= 1
with P, the relative form of T seen from F is ``level = len(P) - k + 1`` and
``module = ".".join(T[k:])`` (``None`` when T is exactly ``P[:k]``).
:func:`resolve_relative` maps that form back to ``".".join(T)``. Any level with
``level - 1 > len(P)`` resolves to ``None``.

The generator places F under one of the roots ``("",)`` or ``("", "src")``.
When ``src`` is a root, F sits either under ``src/`` (the innermost root, so
``src`` is not part of P) or elsewhere under the repo root (P never starts with
``src`` there, or ``src`` would be the innermost root). With only the repo
root, P may start with ``src``. F is either ``P/__init__.py`` or ``P/<mod>.py``;
both have package P.

``level - 1 == len(P)`` (k = 0, a climb to a top-level module) lies outside
this property; the example tests in ``test_import_check.py`` cover it.

**Validates: Requirements 4.6**
"""

from __future__ import annotations

import keyword
import string
from dataclasses import dataclass
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from trikon.change_intel.import_check import resolve_relative

_SRC: Final = "src"
_RESERVED: Final = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)

_SEGMENT: Final = st.one_of(
    st.sampled_from((_SRC, "pkg", "app", "tests", "_private", "core2")),
    st.builds(
        lambda head, tail: head + tail,
        st.sampled_from(string.ascii_lowercase + "_"),
        st.text(alphabet=string.ascii_lowercase + string.digits + "_", max_size=6),
    ),
).filter(lambda segment: segment not in _RESERVED)
"""One importable dotted-name segment: an ASCII, non-keyword identifier."""

_DOTTED: Final = st.lists(_SEGMENT, min_size=1, max_size=4).map(".".join)
"""A non-empty dotted module name."""


@dataclass(frozen=True)
class Placement:
    """A Python file F and the facts the property needs about it.

    ``package`` is F's package P under the innermost root containing it,
    written out by the generator rather than derived from ``path``.
    """

    roots: tuple[str, ...]
    package: tuple[str, ...]
    path: str


@st.composite
def _placements(draw: st.DrawFn) -> Placement:
    """Draw roots, a root containing F, a non-empty package P and F's file name."""
    roots = draw(st.sampled_from((("",), ("", _SRC))))
    root = draw(st.sampled_from(roots))
    # Under the repo root with ``src`` also a root, a leading ``src`` directory
    # would make ``src`` the innermost root and shorten the package.
    shadowed = root == "" and _SRC in roots
    first = draw(_SEGMENT.filter(lambda segment: segment != _SRC) if shadowed else _SEGMENT)
    rest = draw(st.lists(_SEGMENT, max_size=4))
    package = (first, *rest)
    filename = draw(st.one_of(st.just("__init__.py"), _SEGMENT.map(lambda stem: stem + ".py")))
    parts = [root] if root else []
    path = "/".join([*parts, *package, filename])
    return Placement(roots=roots, package=package, path=path)


@settings(max_examples=100, deadline=None)
@given(placement=_placements(), data=st.data())
def test_relative_form_resolves_back_to_target(placement: Placement, data: st.DataObject) -> None:
    package = placement.package
    shared = data.draw(st.integers(min_value=1, max_value=len(package)), label="k")
    suffix = data.draw(st.lists(_SEGMENT, max_size=3), label="suffix")
    target = (*package[:shared], *suffix)

    level = len(package) - shared + 1
    module = ".".join(target[shared:]) or None

    assert resolve_relative(placement.path, level, module, placement.roots) == ".".join(target)


@settings(max_examples=100, deadline=None)
@given(
    placement=_placements(),
    excess=st.integers(min_value=1, max_value=6),
    module=st.none() | _DOTTED,
)
def test_level_beyond_package_depth_resolves_to_none(
    placement: Placement, excess: int, module: str | None
) -> None:
    level = len(placement.package) + 1 + excess  # level - 1 > len(P)

    assert resolve_relative(placement.path, level, module, placement.roots) is None
