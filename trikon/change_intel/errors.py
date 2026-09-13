"""Exception hierarchy for the Change Intelligence subsystem.

Every raise site under :mod:`trikon.change_intel` MUST use one of the classes
defined here. Nothing raises bare :class:`Exception`, :class:`ValueError`,
:class:`RuntimeError`, or :class:`sqlite3.Error` past the module boundary —
that closure is what lets :func:`trikon.sdk.verify` translate any internal
failure into a ``require_human`` verdict without ambiguity.

Phase 2 re-parents :class:`ChangeIntelError` under
:class:`trikon.exceptions.TrikonError` so the SDK boundary can catch a single
class when Phase 3 fuses the change-intel and verification ``try`` blocks.
The subclass hierarchy below is unchanged; only the base class shifts from
:class:`Exception` to :class:`~trikon.exceptions.TrikonError`, so every
existing ``except ChangeIntelError`` site continues to work.

See ``design.md §Error Handling`` and ``requirements.md §Requirement 6``.
"""

from __future__ import annotations

from trikon.exceptions import TrikonError


class ChangeIntelError(TrikonError):
    """Base class for every error raised by :mod:`trikon.change_intel`.

    Raised when a Change Intelligence stage cannot complete its work and needs
    the caller (usually :func:`trikon.sdk.verify`) to fail closed. Callers
    should catch this base class exactly once, at the SDK boundary, and
    convert the failure into a ``require_human`` verdict.
    """


class DiffInputError(ChangeIntelError):
    """The arguments to :func:`parse_diff` are invalid.

    Raised when the caller violates the "exactly one of ``(base_sha,
    head_sha)`` or ``diff``" contract, or when the diff payload exceeds the
    configured ``max_diff_bytes`` ceiling.
    """


class RepoNotFoundError(ChangeIntelError):
    """The supplied ``repo_path`` is not a git repository.

    Raised when :func:`parse_diff` is pointed at a directory that has no
    ``.git`` entry (or otherwise cannot be opened by ``gitpython``).
    """


class DiffParseError(ChangeIntelError):
    """A unified-diff payload is malformed and cannot be parsed.

    Raised when ``unidiff`` rejects the diff string, or when ``git diff``
    output cannot be interpreted as a valid patch set.
    """


class AstParseError(ChangeIntelError):
    """A Python source file failed to parse.

    Raised by the AST indexers (:func:`index_file`, :func:`fast_index_symbols`)
    when the source text is not valid Python. Callers get enough context on
    the exception to blocklist the offending file or surface a diagnostic —
    :attr:`file_path` identifies the file and :attr:`line` (when available)
    pinpoints the syntax error.
    """

    def __init__(
        self,
        message: str,
        *,
        file_path: str,
        line: int | None = None,
        cause: SyntaxError | None = None,
    ) -> None:
        super().__init__(message)
        self.file_path: str = file_path
        self.line: int | None = line
        self.cause: SyntaxError | None = cause
        if cause is not None:
            self.__cause__ = cause


class SymbolResolutionError(ChangeIntelError):
    """The reference resolver could not be initialized for the repository.

    Raised by :func:`find_references` only when the underlying ``jedi.Project``
    itself fails to construct (missing virtualenv metadata, corrupt
    ``sys.path``, etc.). Per-reference lookup failures are logged and skipped;
    they never surface as this exception.
    """


class DepGraphError(ChangeIntelError):
    """A dependency-graph operation failed.

    Raised when the SQLite state store rejects a query, when schema drift is
    detected (``schema_version`` newer than the running Trikon), or when a
    graph query receives an invalid argument (for example ``max_hops <= 0``).
    ``sqlite3.Error`` is caught inside :class:`DepGraph` and re-raised as this
    class so callers never need to know the persistence layer's exception
    vocabulary.
    """


class BlastRadiusError(ChangeIntelError):
    """The blast-radius orchestrator could not produce an :class:`ImpactSet`.

    Raised by :func:`compute_impact` when any downstream stage
    (:func:`parse_diff`, :func:`index_files`, :func:`find_references`,
    :class:`DepGraph`) fails. The underlying :class:`ChangeIntelError` is
    chained via ``__cause__`` so the SDK boundary can report the original
    reason without losing information.
    """
