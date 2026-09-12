"""Load and validate a policy YAML from disk."""

from __future__ import annotations

from pathlib import Path

from trikon.policy.dsl import Policy


def load_policy(repo_path: Path, policy_path: Path) -> Policy:
    """Load `<repo_path>/<policy_path>` (or absolute `policy_path`) and validate it.

    If the file does not exist, returns the built-in default policy.
    Raises `pydantic.ValidationError` on schema violations.
    """
    # TODO:
    #   1. Resolve `policy_path` against `repo_path` when relative.
    #   2. If missing, return `default_policy()`.
    #   3. yaml.safe_load + Policy.model_validate.
    raise NotImplementedError


def default_policy() -> Policy:
    """The built-in fallback policy — conservative: default to require_human."""
    # TODO: return a Policy with the same rules as examples/policies/default.yaml.
    raise NotImplementedError
