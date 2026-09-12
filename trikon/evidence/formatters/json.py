"""Canonical JSON serialization of a `Verdict`.

Pydantic already emits stable JSON via `.model_dump_json()`. This wrapper exists
so callers do not import Pydantic directly and so we can add pretty-printing,
field ordering, or version-specific transforms later without touching callers.
"""

from __future__ import annotations

from trikon.evidence.report import Verdict


def format_json(verdict: Verdict, *, indent: int | None = 2) -> str:
    """Return the canonical JSON for a Verdict."""
    return verdict.model_dump_json(indent=indent)
