"""Unit tests for :func:`trikon.change_intel.symbol_resolver.find_references`.

Covers Task 7.2 in ``.kiro/specs/change-intelligence/tasks.md``. Two named
Correctness Properties are pinned down here:

* **Property 5 — Reference-finder soundness and completeness (Requirements 3.1).**
  On a synthetic multi-file repo with a known reference graph, the resolver
  returns exactly the expected call sites — no missing refs, no spurious
  ones, and no refs from within the target symbol's own body.
* **Property 6 — No-crash invariant (Requirements 3.2).**
  A file with a syntactically valid but semantically broken import
  (``from nonexistent_pkg import foo``) does not crash the resolver; the
  resolvable references still come back.

The remaining tests exercise the module's contract: :class:`SymbolResolutionError`
surfaces only when :class:`jedi.Project` construction itself fails,
unreadable target files silently yield an empty list, the reference-kind
classifier tags ``import`` / ``attribute_access`` / ``call`` correctly,
results are sorted deterministically by ``(referring_file, referring_line)``,
and jedi's ``get_references`` returning ``None`` is treated as "nothing found"
rather than as an error.

_Validates: Requirements 3.1, 3.2._
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import jedi
import pytest

from trikon.change_intel.errors import SymbolResolutionError
from trikon.change_intel.models import SymbolDef, SymbolRefInternal
from trikon.change_intel.symbol_resolver import find_references

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """SHA-256 hex digest of ``text`` encoded as UTF-8.

    The resolver never reads ``file_sha`` (only :class:`DepGraph` does), but
    populating it with a real digest keeps the :class:`SymbolDef` values that
    show up in test output feel like the ones the AST indexer would emit.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_files(repo: Path, files: dict[str, str]) -> None:
    """Materialize ``files`` (POSIX-relative path → source text) under ``repo``.

    Parent directories are created on demand so a call like
    ``{"pkg/mod.py": "..."}`` handles the intermediate ``pkg/`` directory
    for the caller. Text is written UTF-8 so tests can assert on line-exact
    positions without worrying about newline translation.
    """
    for rel_path, text in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _make_symbol_def(
    *,
    qualified_name: str,
    file_path: str,
    source: str,
    start_line: int,
    end_line: int,
    is_public: bool = True,
) -> SymbolDef:
    """Build a :class:`SymbolDef` for ``qualified_name`` from a source string.

    Byte offsets span the whole ``source`` — the resolver only reads the file
    itself via :mod:`pathlib`; ``start_byte`` and ``end_byte`` are inert for
    this test suite but must still satisfy :class:`SymbolDef`'s frozen
    dataclass contract, so we ground them in real values.
    """
    source_bytes = source.encode("utf-8")
    return SymbolDef(
        qualified_name=qualified_name,
        kind="function",
        file_path=file_path,
        file_sha=_sha256(source),
        start_line=start_line,
        end_line=end_line,
        start_byte=0,
        end_byte=len(source_bytes),
        is_public=is_public,
    )


@pytest.fixture
def cache_db(tmp_path: Path) -> Path:
    """Return a cache-DB path inside ``tmp_path``.

    Phase 1's resolver reserves the ``cache_db`` argument for a follow-up
    SQLite cache; it does not touch the file. The fixture still returns a
    valid ``Path`` so tests exercise the signature exactly as production
    callers do.
    """
    return tmp_path / ".trikon" / "state.db"


# ---------------------------------------------------------------------------
# Property 5 — soundness and completeness (Requirements 3.1)
# ---------------------------------------------------------------------------


def test_property5_returns_all_external_call_sites_and_no_self_refs(
    tmp_path: Path, cache_db: Path
) -> None:
    """The resolver finds every external call site and drops in-body references.

    Set-up: ``pkg/mod.py`` defines ``target_func``. ``caller_a.py`` calls it
    once. ``caller_b.py`` also calls it once. ``mod.py`` contains a recursive
    self-call inside the body of ``target_func`` — that reference must not
    appear in the output because it originates inside the target symbol's
    own ``[start_line, end_line]`` window.

    **Property 5 / Validates: Requirements 3.1.**
    """
    mod_source = (
        "def target_func(n):\n"
        "    if n <= 0:\n"
        "        return 0\n"
        "    return target_func(n - 1) + 1\n"  # self-reference on line 4
    )
    caller_a = (
        "from pkg.mod import target_func\n"
        "\n"
        "def caller_a():\n"
        "    return target_func()\n"  # call site on line 4
    )
    caller_b = (
        "from pkg.mod import target_func\n"
        "\n"
        "def caller_b():\n"
        "    return target_func()\n"  # call site on line 4
    )
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller_a.py": caller_a,
            "caller_b.py": caller_b,
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=4,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db)

    # Exactly the two external call sites, no self-reference from mod.py.
    assert refs == [
        SymbolRefInternal(
            target_qualified_name="pkg.mod.target_func",
            target_file_path="pkg/mod.py",
            referring_file="caller_a.py",
            referring_line=4,
            kind="call",
        ),
        SymbolRefInternal(
            target_qualified_name="pkg.mod.target_func",
            target_file_path="pkg/mod.py",
            referring_file="caller_b.py",
            referring_line=4,
            kind="call",
        ),
    ]

    # Self-reference filter: no ref may originate inside mod.py's own body.
    assert not any(r.referring_file == "pkg/mod.py" for r in refs)


# ---------------------------------------------------------------------------
# Property 6 — no-crash on broken imports (Requirements 3.2)
# ---------------------------------------------------------------------------


def test_property6_broken_import_does_not_raise_or_lose_resolvable_refs(
    tmp_path: Path, cache_db: Path
) -> None:
    """A file with a syntactically valid but semantically broken import is skipped.

    We add ``broken.py`` importing a non-existent package. The resolver must
    still return the legitimate reference from ``caller_a.py`` and must not
    raise :class:`SymbolResolutionError` — per ``design.md §2.3``, that
    exception only surfaces when :class:`jedi.Project` itself fails to
    construct.

    **Property 6 / Validates: Requirements 3.2.**
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller_a.py": (
                "from pkg.mod import target_func\n\ndef use():\n    return target_func()\n"
            ),
            # Broken: nonexistent_pkg has no distribution on the search path.
            "broken.py": (
                "from nonexistent_pkg import doesnt_matter\n"
                "\n"
                "def broken_use():\n"
                "    return doesnt_matter()\n"
            ),
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    # Must not raise. The broken import lives in a different file entirely,
    # so it cannot short-circuit the resolvable reference from caller_a.py.
    refs = find_references(sym, tmp_path, cache_db=cache_db)

    assert any(r.referring_file == "caller_a.py" and r.referring_line == 4 for r in refs)
    # broken.py must never contribute a spurious reference to target_func.
    assert not any(r.referring_file == "broken.py" for r in refs)


# ---------------------------------------------------------------------------
# SymbolResolutionError surfaces only at project construction
# ---------------------------------------------------------------------------


def test_symbol_resolution_error_when_project_construction_fails(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising :class:`jedi.Project` becomes :class:`SymbolResolutionError` at the entry point.

    We swap :class:`jedi.Project` for a class whose ``__init__`` always
    raises. The resolver must translate that failure into
    :class:`SymbolResolutionError` and chain the original exception via
    ``__cause__`` so the SDK boundary can surface the underlying reason
    without losing information.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(tmp_path, {"mod.py": mod_source})

    sym = _make_symbol_def(
        qualified_name="mod.target_func",
        file_path="mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    class _ExplodingProject:
        """Stand-in for :class:`jedi.Project` that fails on construction."""

        def __init__(self, path: str) -> None:
            raise RuntimeError(f"cannot construct project for {path}")

    monkeypatch.setattr(jedi, "Project", _ExplodingProject)

    with pytest.raises(SymbolResolutionError) as exc_info:
        find_references(sym, tmp_path, cache_db=cache_db)

    # The chained cause preserves the underlying failure verbatim.
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "cannot construct project" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# Silent empty-result contract
# ---------------------------------------------------------------------------


def test_returns_empty_list_when_target_file_is_missing(tmp_path: Path, cache_db: Path) -> None:
    """A :class:`SymbolDef` pointing at a non-existent file yields ``[]``.

    Per ``design.md §2.3`` and the docstring on :func:`find_references`, an
    unreadable target file is a soft failure (logged at ``DEBUG``, empty
    list returned) so a single missing symbol cannot poison the pipeline.
    """
    sym = SymbolDef(
        qualified_name="pkg.mod.ghost",
        kind="function",
        file_path="pkg/mod.py",  # file was never created on disk
        file_sha="deadbeef" * 8,
        start_line=1,
        end_line=2,
        start_byte=0,
        end_byte=10,
        is_public=True,
    )
    # ``pkg`` directory does not exist either; still must not raise.
    refs = find_references(sym, tmp_path, cache_db=cache_db)
    assert refs == []


def test_get_references_returning_none_is_treated_as_empty(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If jedi's ``get_references`` returns ``None``, the resolver returns ``[]``.

    Older jedi versions can surface ``None`` on unresolvable positions; the
    ``_get_references_safely`` helper normalizes that to an empty list. We
    force the behavior by monkeypatching :meth:`jedi.Script.get_references`.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    def _fake_get_references(self: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(jedi.Script, "get_references", _fake_get_references)

    refs = find_references(sym, tmp_path, cache_db=cache_db)
    assert refs == []


# ---------------------------------------------------------------------------
# Reference-kind classification
# ---------------------------------------------------------------------------


def test_reference_kind_classification_covers_import_attribute_and_call(
    tmp_path: Path, cache_db: Path
) -> None:
    """The three ``RefKind`` values all appear when their source pattern is used.

    * ``caller_call.py``  — plain ``target_func()`` → ``"call"``
    * ``caller_attr.py``  — ``mod.target_func()``   → ``"attribute_access"``
    * ``caller_alias.py`` — ``from ... import target_func as tf`` → ``"import"``

    The alias form is the only shape where jedi does *not* mark the
    reference site as a definition (in a plain ``from X import Y``, jedi
    treats ``Y`` as a new local definition and the resolver correctly
    filters it out). This is the sanctioned way to exercise the ``import``
    branch of the classifier heuristic.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller_call.py": (
                "from pkg.mod import target_func\n\ndef use():\n    return target_func()\n"
            ),
            "caller_attr.py": ("from pkg import mod\n\ndef use():\n    return mod.target_func()\n"),
            "caller_alias.py": (
                "from pkg.mod import target_func as tf\n\ndef use():\n    return tf()\n"
            ),
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db)

    by_file = {r.referring_file: r for r in refs}
    assert by_file["caller_call.py"].kind == "call"
    assert by_file["caller_attr.py"].kind == "attribute_access"
    assert by_file["caller_alias.py"].kind == "import"


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


def test_references_sorted_by_referring_file_then_line(tmp_path: Path, cache_db: Path) -> None:
    """Refs come back sorted by ``(referring_file, referring_line)`` ascending.

    We deliberately create ``a_caller.py`` (three call sites, lines 4/7/10)
    and ``z_caller.py`` (one call site on line 3). Even though ``z_caller``'s
    line number is lower than any in ``a_caller``, file-name order wins the
    outer key, so ``a_caller`` refs appear first, ordered by line.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "a_caller.py": (
                "from pkg.mod import target_func\n"
                "\n"
                "def one():\n"
                "    return target_func()\n"  # line 4
                "\n"
                "def two():\n"
                "    return target_func()\n"  # line 7
                "\n"
                "def three():\n"
                "    return target_func()\n"  # line 10
            ),
            "z_caller.py": (
                "from pkg.mod import target_func\n"
                "\n"
                "x = target_func()\n"  # line 3 — earlier than any a_caller line
            ),
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db)
    keys = [(r.referring_file, r.referring_line) for r in refs]

    # Alphabetical file order wins over line number.
    assert keys == [
        ("a_caller.py", 4),
        ("a_caller.py", 7),
        ("a_caller.py", 10),
        ("z_caller.py", 3),
    ]

    # The invariant expressed as its own assertion so a failure message
    # points at "not sorted" rather than "unexpected element".
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Determinism across repeated invocations
# ---------------------------------------------------------------------------


def test_find_references_is_deterministic_across_calls(tmp_path: Path, cache_db: Path) -> None:
    """Two back-to-back calls on identical inputs produce byte-identical results.

    Same repo, same :class:`SymbolDef`, same ``cache_db`` → the resolver
    must return the exact same list of :class:`SymbolRefInternal` values.
    ``design.md §2.3`` promises deterministic ordering; this test pins the
    stronger claim of *full* determinism, not just sort stability.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller_a.py": (
                "from pkg.mod import target_func\n\ndef a():\n    return target_func()\n"
            ),
            "caller_b.py": (
                "from pkg.mod import target_func\n\ndef b():\n    return target_func()\n"
            ),
        },
    )

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    refs_first = find_references(sym, tmp_path, cache_db=cache_db)
    refs_second = find_references(sym, tmp_path, cache_db=cache_db)
    assert refs_first == refs_second
    assert len(refs_first) == 2


# ---------------------------------------------------------------------------
# Explicit-project pathway
# ---------------------------------------------------------------------------


def test_prebuilt_project_argument_is_used_without_reconstructing(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``project=`` is supplied, the resolver reuses it rather than building anew.

    We monkeypatch :class:`jedi.Project` to a sentinel that raises on
    construction; if the resolver ignored the caller-supplied ``project``
    and tried to build its own, this test would surface the failure as a
    :class:`SymbolResolutionError`. Instead we expect the resolver to route
    around the sentinel entirely and return valid references.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller.py": (
                "from pkg.mod import target_func\n\ndef use():\n    return target_func()\n"
            ),
        },
    )

    real_project = jedi.Project(path=str(tmp_path))

    class _ForbiddenProject:
        """Explodes to prove the resolver never touched :class:`jedi.Project`."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("jedi.Project should not have been called")

    monkeypatch.setattr(jedi, "Project", _ForbiddenProject)

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db, project=real_project)
    assert any(r.referring_file == "caller.py" for r in refs)


# ---------------------------------------------------------------------------
# Defensive fall-through paths
# ---------------------------------------------------------------------------


def test_jedi_script_construction_failure_returns_empty(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing :class:`jedi.Script` is treated as "no references" — not a raise.

    Per ``design.md §2.3``: :class:`SymbolResolutionError` surfaces only from
    :class:`jedi.Project` construction. A per-file :class:`jedi.Script`
    failure must be logged and translated into an empty result so a single
    broken source file cannot poison the pipeline.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": mod_source})

    def _boom_script(*args: object, **kwargs: object) -> object:
        raise RuntimeError("script construction exploded")

    monkeypatch.setattr(jedi, "Script", _boom_script)

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    # Must not raise — this is not the :class:`jedi.Project` construction path.
    assert find_references(sym, tmp_path, cache_db=cache_db) == []


def test_get_references_typeerror_falls_back_to_older_signature(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A :class:`TypeError` on the ``scope=`` kwarg triggers the fallback call.

    The resolver's ``_get_references_safely`` helper handles jedi API drift:
    modern jedi accepts ``scope="project"``; older builds do not. We simulate
    the older signature by raising :class:`TypeError` on any keyword-scoped
    call and passing through cleanly on the un-scoped one. The observable
    outcome is that references still come back — the fallback path took over.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller.py": (
                "from pkg.mod import target_func\n\ndef use():\n    return target_func()\n"
            ),
        },
    )

    real_get_references = jedi.Script.get_references

    def _typeerror_on_scope(self: object, *args: object, **kwargs: object) -> object:
        if "scope" in kwargs:
            # Simulate an older jedi that does not know about the ``scope`` keyword.
            raise TypeError("get_references() got an unexpected keyword argument 'scope'")
        return real_get_references(self, *args, **kwargs)

    monkeypatch.setattr(jedi.Script, "get_references", _typeerror_on_scope)

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db)
    assert any(r.referring_file == "caller.py" for r in refs)


def test_get_references_typeerror_then_error_returns_empty(
    tmp_path: Path, cache_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both the scoped and the fallback ``get_references`` fail, result is ``[]``.

    First call raises :class:`TypeError` (route into the fallback branch);
    the fallback call raises :class:`RuntimeError` (route into the outer
    "log and return empty" branch). The resolver must not propagate either
    exception.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": mod_source})

    def _always_fail(self: object, *args: object, **kwargs: object) -> object:
        if "scope" in kwargs:
            raise TypeError("no scope kwarg")
        raise RuntimeError("fallback also fails")

    monkeypatch.setattr(jedi.Script, "get_references", _always_fail)

    sym = _make_symbol_def(
        qualified_name="pkg.mod.target_func",
        file_path="pkg/mod.py",
        source=mod_source,
        start_line=1,
        end_line=2,
    )

    assert find_references(sym, tmp_path, cache_db=cache_db) == []


def test_absolute_file_path_in_symbol_def_is_handled(tmp_path: Path, cache_db: Path) -> None:
    """A :class:`SymbolDef` with an absolute ``file_path`` still resolves cleanly.

    ``SymbolDef.file_path`` is documented as POSIX-relative, but the
    resolver accepts absolute paths as well (see ``_resolve_target_path``).
    The absolute-path branch is the only way callers building
    :class:`SymbolDef` from an already-on-disk index can pipe results into
    the resolver.
    """
    mod_source = "def target_func():\n    return 1\n"
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": mod_source,
            "caller.py": (
                "from pkg.mod import target_func\n\ndef use():\n    return target_func()\n"
            ),
        },
    )
    absolute_mod = (tmp_path / "pkg" / "mod.py").resolve()

    sym = SymbolDef(
        qualified_name="pkg.mod.target_func",
        kind="function",
        file_path=str(absolute_mod),  # absolute — exercises the branch
        file_sha=_sha256(mod_source),
        start_line=1,
        end_line=2,
        start_byte=0,
        end_byte=len(mod_source.encode("utf-8")),
        is_public=True,
    )

    refs = find_references(sym, tmp_path, cache_db=cache_db)
    # We do not assert on ``target_file_path`` (the absolute form flows
    # through unchanged) — the important observable is that resolution
    # succeeds against caller.py.
    assert any(r.referring_file == "caller.py" for r in refs)
