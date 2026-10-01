"""Pydantic models for the Trikon policy YAML.

Schema version 1 supports these `when` condition keys. Every key in one
`when` mapping must match (AND); `any_of` gives OR inside a rule.

  - path-glob matching       (`any_path_matches`, `no_path_matches`)
  - blast-radius thresholds  (`change.blast_radius.score`)
  - test-status conditions   (`verification.tests.status`)
  - static-check deltas      (`verification.static.new_errors: {eq|gt|lt: int}`)

Added by the engine fail-safe work (still version 1):

  - test evidence            (`verification.tests.executed: {eq|gt|lt: int}`,
                              which is `passed + failed`;
                              `verification.tests.total: {eq|gt|lt: int}`;
                              `verification.tests.strategy: <str>`;
                              `verification.tests.incomplete: <bool>`)
  - import evidence          (`verification.imports.broken: {eq|gt|lt: int}`,
                              the number of Broken_Imports;
                              `verification.imports.incomplete: <bool>`)
  - Python_Change            (`change.python_change: <bool>`)
  - disjunction              (`any_of: [<when mapping>, ...]`, a non-empty
                              list of non-empty mappings; true when any one
                              matches under AND semantics; may nest)

Boolean keys accept only YAML `true` / `false`, not `1` / `0`. Any unknown
key, or a value of the wrong shape, raises `RuleMatchError` at evaluation
time, and the SDK fails closed to `require_human`.

Why `version` stays 1: the new keys are purely additive and no existing key
changed its meaning, so every version-1 policy written for the previous
release evaluates exactly as before. A previous-release engine reading a
policy that uses a new key raises `RuleMatchError` on the unknown key and
fails closed, so it never silently ignores a condition. Changing the meaning
of an existing key would require bumping `version` and adding a migration in
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
