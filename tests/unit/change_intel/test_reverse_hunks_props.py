# Feature: trikon-engine-fail-safe, Property 3: Base reconstruction round trip
"""Property test: ``reverse_hunks`` rebuilds the base text from a parsed diff.

*For any* base file (a list of lines) and any sequence of edits producing a
head file, ``reverse_hunks(head, parse_diff(diff=unified_diff(base, head,
n)).files[0].hunks)`` equals the base text for every context size ``n`` in
``0..3``. Reversing against a head that does not match the diff returns
``None``.

The diff goes through :func:`~trikon.change_intel.parse_diff`, so the test
also exercises the ``Hunk.source_lines`` / ``Hunk.target_lines`` fields that
``_build_hunk`` fills (task 2.2).

Generation
----------
* Lines come from a small pool, so repeated lines, blank lines and lines that
  look like diff syntax (``--- a/...``, ``@@ -1 +1 @@``, the ``\\ No newline``
  marker) are common. They are mixed with arbitrary text that holds no line
  break or other control character.
* Edits insert, delete or replace single lines at random positions.
* Diffs come from :func:`difflib.unified_diff` with git-style ``a/`` and
  ``b/`` headers and explicit ``\\n`` terminators. A line without a terminator
  gets git's ``\\ No newline at end of file`` marker after it.
* Either both files end with ``\\n`` or both lack the final newline. ``Hunk``
  does not keep the marker, so ``reverse_hunks`` gives a restored last line
  the head file's ending (design §1). Files that disagree on the final newline
  are outside the round trip by design and are not generated.
* The mismatch property changes one head line *inside* a hunk's target range.
  A hunk with ``new_lines == 0`` covers no head line, so drift outside every
  target range cannot be detected and is not asserted.

**Validates: Requirements 3.3, 3.7**
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hypothesis import assume, event, given, note, settings
from hypothesis import strategies as st

from trikon.change_intel import Hunk, parse_diff
from trikon.change_intel.import_check import reverse_hunks

Edit = tuple[str, int, str]
"""``(operation, raw position, line text)``; the position is reduced modulo the head length."""

_REPO: Final = Path("repo")
"""``parse_diff`` records this path but never reads it on the diff-string path."""

_PATH: Final = "pkg/mod.py"
_NO_NEWLINE_MARKER: Final = "\\ No newline at end of file"
_EOF_TEXT: Final = "eof"
"""Replaces an empty last line when a file must end without a newline."""

_LINE_POOL: Final = (
    "",
    " ",
    "a",
    "b",
    "x = 1",
    "    return x",
    "pass",
    "# note",
    "-",
    "+",
    f"--- a/{_PATH}",
    f"+++ b/{_PATH}",
    "@@ -1 +1 @@",
    _NO_NEWLINE_MARKER,
)

_line_text = st.one_of(
    st.sampled_from(_LINE_POOL),
    # No control characters (``\n``, ``\r``, ``\x0c`` ...) and no Unicode line
    # or paragraph separators, so a line never splits into two.
    st.text(
        st.characters(codec="utf-8", exclude_categories=("Cc", "Cs", "Zl", "Zp")),
        max_size=12,
    ),
)

_edit = st.tuples(
    st.sampled_from(("insert", "delete", "replace")),
    st.integers(min_value=0, max_value=63),
    _line_text,
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Case:
    """A base file, the head file the edits produced, and the diff settings."""

    base_lines: tuple[str, ...]
    head_lines: tuple[str, ...]
    final_newline: bool
    context: int

    @property
    def base_text(self) -> str:
        return "".join(_file_lines(self.base_lines, final_newline=self.final_newline))

    @property
    def head_text(self) -> str:
        return "".join(_file_lines(self.head_lines, final_newline=self.final_newline))


def _file_lines(lines: Sequence[str], *, final_newline: bool) -> list[str]:
    """Return ``lines`` with ``\\n`` terminators, leaving the last one bare unless ``final_newline``.

    This list is both the file text (joined) and the :mod:`difflib` input, so
    the diff and the text never disagree on where lines split.
    """
    terminated = [f"{line}\n" for line in lines]
    if terminated and not final_newline:
        terminated[-1] = terminated[-1].removesuffix("\n")
    return terminated


def _apply(base: Sequence[str], edits: Sequence[Edit]) -> list[str]:
    """Apply single-line ``edits`` to a copy of ``base`` and return the head lines."""
    head = list(base)
    for operation, raw_position, text in edits:
        if operation == "insert":
            head.insert(raw_position % (len(head) + 1), text)
        elif head:
            position = raw_position % len(head)
            if operation == "delete":
                del head[position]
            else:
                head[position] = text
    return head


def _with_text_last_line(lines: list[str]) -> list[str]:
    """Make the last line non-empty so a file without a final newline really ends in text.

    Otherwise ``"a\\n" + ""`` would render as ``"a\\n"``, which does end with a
    newline.
    """
    if lines and lines[-1] == "":
        lines[-1] = _EOF_TEXT
    return lines


@st.composite
def _cases(draw: st.DrawFn) -> _Case:
    # Long enough files, with edits spread over them, give multi-hunk diffs
    # at every context size.
    base = draw(st.lists(_line_text, max_size=30))
    head = _apply(base, draw(st.lists(_edit, min_size=1, max_size=8)))
    # An empty head has no last line to carry the missing newline, so both
    # files keep their terminators then.
    final_newline = draw(st.booleans()) or not head
    if not final_newline:
        base = _with_text_last_line(base)
        head = _with_text_last_line(head)
    return _Case(
        base_lines=tuple(base),
        head_lines=tuple(head),
        final_newline=final_newline,
        context=draw(st.integers(min_value=0, max_value=3)),
    )


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _unified_diff(case: _Case) -> str:
    """Render a git-style unified diff from the case's base to its head.

    Returns ``""`` when the files are equal, the same as ``git diff`` does.
    """
    body: list[str] = []
    for line in difflib.unified_diff(
        _file_lines(case.base_lines, final_newline=case.final_newline),
        _file_lines(case.head_lines, final_newline=case.final_newline),
        fromfile=f"a/{_PATH}",
        tofile=f"b/{_PATH}",
        n=case.context,
    ):
        body.append(line)
        if not line.endswith("\n"):
            body.append(f"\n{_NO_NEWLINE_MARKER}\n")
    if not body:
        return ""
    return f"diff --git a/{_PATH} b/{_PATH}\n" + "".join(body)


def _parsed_hunks(diff: str) -> tuple[Hunk, ...]:
    """Parse ``diff`` with :func:`parse_diff` and return the single file's hunks."""
    files = parse_diff(_REPO, diff=diff).files
    if not diff:
        assert files == ()
        return ()
    (file_change,) = files
    assert file_change.path == _PATH
    return file_change.hunks


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(case=_cases())
def test_reverse_hunks_rebuilds_the_base_from_a_parsed_diff(case: _Case) -> None:
    """Undoing the parsed hunks on the head text gives back the base text exactly."""
    diff = _unified_diff(case)
    note(f"diff:\n{diff}")
    hunks = _parsed_hunks(diff)
    event(f"context {case.context}")
    event("final newline" if case.final_newline else "no final newline")
    event(f"{min(len(hunks), 3)}{'+' if len(hunks) >= 3 else ''} hunks")

    assert reverse_hunks(case.head_text, hunks) == case.base_text


@settings(max_examples=100, deadline=None)
@given(case=_cases(), data=st.data())
def test_reverse_hunks_returns_none_when_a_target_line_drifted(
    case: _Case, data: st.DataObject
) -> None:
    """A head whose line inside a hunk's target range differs from the diff is rejected."""
    diff = _unified_diff(case)
    note(f"diff:\n{diff}")
    hunks = _parsed_hunks(diff)
    covered = sorted(
        {hunk.new_start - 1 + offset for hunk in hunks for offset in range(hunk.new_lines)}
    )
    assume(covered)

    index = data.draw(st.sampled_from(covered), label="drifted head line")
    original = case.head_lines[index]
    # The last line must stay non-empty when the file lacks a final newline.
    allow_empty = case.final_newline or index < len(case.head_lines) - 1
    drifted = data.draw(
        _line_text.filter(lambda text: text != original and (allow_empty or text != "")),
        label="drifted text",
    )
    drifted_lines = list(case.head_lines)
    drifted_lines[index] = drifted
    drifted_head = "".join(_file_lines(drifted_lines, final_newline=case.final_newline))

    assert reverse_hunks(drifted_head, hunks) is None
