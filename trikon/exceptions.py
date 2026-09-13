"""Shared exception root for every Trikon subsystem.

Phase 1 introduced :class:`trikon.change_intel.errors.ChangeIntelError` as a
direct subclass of :class:`Exception`; Phase 2 lifts the common root out so
:func:`trikon.sdk.verify` can catch a single class when Phase 3 fuses the
change-intel and verification ``try`` blocks. ``ChangeIntelError`` is
re-parented under :class:`TrikonError` here (see the docstring in
``trikon/change_intel/errors.py`` for the compatibility shim), and the
Phase-2 :class:`trikon.verify.errors.VerificationRunnerError` will inherit
from the same root when it lands.

See ``design.md §9`` and ``requirements.md §Requirement 6``.
"""

from __future__ import annotations


class TrikonError(Exception):
    """Root of the Trikon exception tree.

    Every subsystem-specific base class (``ChangeIntelError``,
    ``VerificationRunnerError``, and any Phase-3 additions) inherits from
    this class. The SDK boundary catches ``TrikonError`` exactly once and
    converts any internal failure into a ``require_human`` verdict — the
    never-fail-open contract laid out in ``design.md §9``.
    """
