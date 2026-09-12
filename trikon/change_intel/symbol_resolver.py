"""Resolve cross-file symbol references.

Given a :class:`~trikon.change_intel.models.SymbolDef`, find every call site,
import, and attribute access that refers to it inside a repository. The
resolver is a thin, well-typed shell around :mod:`jedi`; the substantive work
lives inside jedi's project + script API.

The single public entry point is :func:`find_references`. Its contract is
narrow on purpose:

* Project construction is the only failure that surfaces as
  :class:`SymbolResolutionError`. Any other jedi hiccup during per-reference
  lookup is logged at ``DEBUG`` and skipped, so a single malformed import in a
  large monorepo cannot poison the whole result.
* Results are deterministic — sorted by ``(referring_file, referring_line)``
  so identical inputs yield byte-identical output.
* References inside the target symbol's own body are filtered out; recursion
  is uninteresting to blast-radius analysis.

The ``cache_db`` parameter is accepted for forward compatibility. A follow-up
task will cache jedi results in the same SQLite state store keyed by
``(symbol.qualified_name, symbol.file_sha)``. Phase 1 does not read or write
that table.
"""

from __future__ import annotations

import logging
from pathlib import Path

import jedi

from trikon.change_intel.errors import SymbolResolutionError
from trikon.change_intel.models import RefKind, SymbolDef, SymbolRefInternal

__all__ = ["find_references"]

_LOG = logging.getLogger(__name__)


def find_references(
    symbol: SymbolDef,
    repo_path: Path,
    *,
    cache_db: Path,  # reserved for the SQLite-backed cache landing in a follow-up
    project: jedi.Project | None = None,
) -> list[SymbolRefInternal]:
    """Return every reference to ``symbol`` inside ``repo_path``.

    Parameters
    ----------
    symbol:
        The definition whose call sites and imports we want to enumerate.
        ``symbol.file_path`` is treated as POSIX-relative to ``repo_path``
        when it is not already absolute.
    repo_path:
        Repository root passed to :class:`jedi.Project` as its ``path``.
        Missing or unreadable roots surface as :class:`SymbolResolutionError`.
    cache_db:
        Reserved. A future task will cache resolver output in this SQLite
        file keyed by ``(symbol.qualified_name, symbol.file_sha)``. Phase 1
        accepts the parameter for signature stability but does not use it.
    project:
        Optional pre-built :class:`jedi.Project` instance. When ``None`` we
        construct one from ``repo_path``.

    Returns
    -------
    list[SymbolRefInternal]
        References to ``symbol`` outside its own definition, sorted by
        ``(referring_file, referring_line)``. Empty when the target file
        cannot be read or jedi finds nothing.

    Raises
    ------
    SymbolResolutionError
        The :class:`jedi.Project` could not be constructed. Per-reference
        failures do not raise; they are logged at ``DEBUG`` and skipped.
    """
    if project is None:
        try:
            project = jedi.Project(path=str(repo_path))
        except Exception as exc:
            raise SymbolResolutionError(
                f"failed to construct jedi.Project for {repo_path}: {exc}"
            ) from exc

    file_abs = _resolve_target_path(symbol.file_path, repo_path)

    try:
        source = file_abs.read_text(encoding="utf-8")
    except OSError as exc:
        _LOG.debug("symbol_resolver: cannot read %s (%s); returning []", file_abs, exc)
        return []

    identifier = _short_name(symbol.qualified_name)
    column = _identifier_column(source, symbol.start_line, identifier)

    try:
        script = jedi.Script(source, path=str(file_abs), project=project)
    except Exception as exc:
        _LOG.debug("symbol_resolver: jedi.Script failed for %s: %s", file_abs, exc)
        return []

    candidates = _get_references_safely(script, symbol.start_line, column, file_abs)
    if not candidates:
        return []

    target_file_posix = symbol.file_path.replace("\\", "/")
    seen: set[tuple[str, int, RefKind]] = set()
    refs: list[SymbolRefInternal] = []

    for name in candidates:
        try:
            ref = _to_internal_ref(
                name,
                target_symbol=symbol,
                target_file_posix=target_file_posix,
                repo_path=repo_path,
                identifier=identifier,
            )
        except Exception as exc:
            _LOG.debug("symbol_resolver: skipping reference %r: %s", name, exc)
            continue

        if ref is None:
            continue

        key = (ref.referring_file, ref.referring_line, ref.kind)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)

    refs.sort(key=lambda r: (r.referring_file, r.referring_line))
    return refs


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_references_safely(
    script: jedi.Script,
    line: int,
    column: int,
    file_abs: Path,
) -> list[jedi.api.classes.Name]:
    """Call ``script.get_references`` while tolerating jedi API drift.

    The ``scope="project"`` keyword landed in a recent jedi release; on older
    installs we transparently fall back to the un-scoped call. Any other
    failure produces an empty list plus a ``DEBUG`` log — never a raise.
    """
    try:
        result = script.get_references(
            line=line,
            column=column,
            include_builtins=False,
            scope="project",
        )
    except TypeError:
        try:
            result = script.get_references(
                line=line,
                column=column,
                include_builtins=False,
            )
        except Exception as exc:
            _LOG.debug("symbol_resolver: get_references failed for %s: %s", file_abs, exc)
            return []
    except Exception as exc:
        _LOG.debug("symbol_resolver: get_references failed for %s: %s", file_abs, exc)
        return []

    return list(result) if result is not None else []


def _resolve_target_path(file_path: str, repo_path: Path) -> Path:
    """Return the absolute path to ``file_path`` given ``repo_path`` as root.

    ``SymbolDef.file_path`` is documented as POSIX-relative to the repo root,
    but we accept absolute paths as well for callers building
    :class:`SymbolDef` instances from an on-disk index.
    """
    candidate = Path(file_path)
    if candidate.is_absolute():
        return candidate
    return (repo_path / candidate).resolve()


def _short_name(qualified_name: str) -> str:
    """Return the last dotted component of ``qualified_name``.

    ``"payments.gateway.charge"`` becomes ``"charge"``; a bare
    ``"charge"`` is returned unchanged.
    """
    if not qualified_name:
        return ""
    return qualified_name.rsplit(".", 1)[-1]


def _identifier_column(source: str, line_no: int, identifier: str) -> int:
    """Locate the 0-based column of ``identifier`` on ``line_no`` of ``source``.

    Jedi needs the position of the *identifier*, not the ``def`` / ``class``
    keyword that precedes it. We slice the requested source line and look for
    the identifier; if it isn't found (unusual — the symbol's ``start_line``
    should point at its own header) we fall back to column 0, which still
    resolves to *some* name in scope so ``get_references`` returns useful data.
    """
    if not identifier or line_no < 1:
        return 0
    lines = source.splitlines()
    idx = line_no - 1
    if idx >= len(lines):
        return 0
    line = lines[idx]
    col = line.find(identifier)
    return col if col >= 0 else 0


def _to_internal_ref(
    name: jedi.api.classes.Name,
    *,
    target_symbol: SymbolDef,
    target_file_posix: str,
    repo_path: Path,
    identifier: str,
) -> SymbolRefInternal | None:
    """Translate a jedi ``Name`` into a :class:`SymbolRefInternal`.

    Returns ``None`` when the reference should be dropped: definitions of the
    target itself, sites inside the target's own body, or references that
    lack the metadata we need to build a fully populated internal ref.
    """
    module_path = getattr(name, "module_path", None)
    line = getattr(name, "line", None)
    if line is None:
        return None

    is_definition_fn = getattr(name, "is_definition", None)
    if callable(is_definition_fn):
        try:
            if is_definition_fn():
                return None
        except Exception:
            # Some jedi versions raise from is_definition() on unresolved names;
            # treat as a non-definition and keep going.
            pass

    referring_file = _relative_posix(module_path, repo_path)
    if referring_file is None:
        return None

    # Drop references originating inside the target symbol's own definition.
    if referring_file == target_file_posix and (
        target_symbol.start_line <= int(line) <= target_symbol.end_line
    ):
        return None

    line_text = _read_reference_line(module_path, int(line))
    kind = _classify_reference(line_text, identifier)

    return SymbolRefInternal(
        target_qualified_name=target_symbol.qualified_name,
        target_file_path=target_file_posix,
        referring_file=referring_file,
        referring_line=int(line),
        kind=kind,
    )


def _relative_posix(module_path: Path | None, repo_path: Path) -> str | None:
    """Return ``module_path`` as a POSIX-relative path under ``repo_path``.

    Jedi returns :class:`pathlib.Path` for in-project modules and ``None`` for
    references that live inside a builtin or a compiled extension; the latter
    are irrelevant to blast-radius analysis and we drop them.
    """
    if module_path is None:
        return None
    try:
        p = Path(module_path)
    except TypeError:
        return None
    try:
        rel = p.resolve().relative_to(repo_path.resolve())
    except ValueError:
        # Reference lives outside the repo (site-packages, stdlib). Not interesting.
        return None
    return rel.as_posix()


def _read_reference_line(module_path: Path | None, line_no: int) -> str:
    """Return the source line at ``line_no`` inside ``module_path``, or ``""``.

    Used only for the reference-kind heuristic. Failures fall through to an
    empty string so :func:`_classify_reference` still produces a valid
    :data:`RefKind` (``"call"``, the sanest default).
    """
    if module_path is None or line_no < 1:
        return ""
    try:
        text = Path(module_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    idx = line_no - 1
    if idx >= len(lines):
        return ""
    return lines[idx]


def _classify_reference(line_text: str, identifier: str) -> RefKind:
    """Classify a reference site as ``import``, ``attribute_access``, or ``call``.

    Heuristic — cheap and deterministic:

    * Line contains ``import`` and the identifier → ``import``.
    * Line contains ``.<identifier>`` → ``attribute_access``.
    * Anything else → ``call``, which is the dominant case in practice and
      the sanest default for downstream traversal.
    """
    if not line_text:
        return "call"
    stripped = line_text.lstrip()
    if (
        identifier
        and identifier in line_text
        and (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or " import " in line_text
        )
    ):
        return "import"
    if identifier and f".{identifier}" in line_text:
        return "attribute_access"
    return "call"
