"""Pydantic models for the Trikon policy YAML.

Schema version 1 supports:

  - path-glob matching       (`any_path_matches`, `no_path_matches`)
  - blast-radius thresholds  (`change.blast_radius.score`)
  - test-status conditions   (`verification.tests.status`)
  - static-check deltas      (`verification.static.new_errors`)
  - actor / time conditions  (`actor.agent_id`, `time_of_day`)

Extending the schema requires bumping `version` and adding a migration in
`loader.py`.
"""

# mypy: disable-error-code=explicit-any
#
# Justification: every ``BaseModel`` subclass in this module inherits pydantic's
# synthetic ``def __init__(self, /, **data: Any) -> None``. Under
# ``--strict --disallow-any-explicit`` mypy flags each subclass declaration as
# ``explicit-any`` even though no ``Any`` appears in *our* source. The design
# permits scoped ``noqa``-style exemptions with justification (design.md §9.3
# "no ``Any`` unless justified by inline noqa"). ``disallow_any_explicit`` is
# still enforced everywhere else, including ``trikon/change_intel/``.

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["allow", "block", "require_human", "warn"]


class Rule(BaseModel):
    """A single policy rule."""

    name: str
    when: dict[str, Any] = Field(default_factory=dict)
    then: Decision
    reason: str | None = None


class Policy(BaseModel):
    """Root policy document."""

    version: int = 1
    weights: dict[str, float] = Field(default_factory=dict)
    sensitive_paths: list[str] = Field(default_factory=list)
    rules: list[Rule]
