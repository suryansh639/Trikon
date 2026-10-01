"""Mirror test: every shipped copy of the default policy defines the same rules.

The default policy lives in three places:

- ``trikon/policy/default_policy.yaml``: the packaged runtime default that
  :func:`trikon.policy.loader.default_policy` loads;
- ``examples/policies/default.yaml``: the documented copy users start from;
- ``examples/sample_repo/.trikon/policy.yaml``: the policy the sample-repo
  integration suite verifies against.

Each file is parsed with ``Policy.model_validate(yaml.safe_load(...))`` and
compared field by field against the packaged copy: ``version``, ``weights``,
``sensitive_paths`` and the ordered ``rules`` (name, when, then, reason).
Comments and line endings may differ; the parsed policy may not.

**Validates: Requirements 6.12**
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from trikon.policy.dsl import Policy, Rule
from trikon.policy.loader import default_policy

# ``tests/unit/policy/test_default_policy_mirror.py`` -> ``parents[3]`` is the
# Trikon repo root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

_PACKAGED: Final[Path] = _REPO_ROOT / "trikon" / "policy" / "default_policy.yaml"

#: The copies that must mirror the packaged default, as repo-relative posix paths.
_MIRRORS: Final[tuple[str, ...]] = (
    "examples/policies/default.yaml",
    "examples/sample_repo/.trikon/policy.yaml",
)


def _load(path: Path) -> Policy:
    """Parse ``path`` exactly as the mirror requirement specifies."""
    parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Policy.model_validate(parsed)


def _rule_fields(rules: list[Rule]) -> list[tuple[str, object, str, str | None]]:
    """Reduce each rule to the compared fields, keeping evaluation order."""
    return [(rule.name, rule.when, rule.then, rule.reason) for rule in rules]


def test_packaged_default_policy_is_the_loader_default() -> None:
    """``default_policy()`` returns exactly the parsed packaged file."""
    assert default_policy() == _load(_PACKAGED)


@pytest.mark.parametrize("mirror", _MIRRORS)
def test_mirror_matches_packaged_default(mirror: str) -> None:
    """Each mirror has the packaged default's version, weights, paths and rules."""
    expected = _load(_PACKAGED)
    actual = _load(_REPO_ROOT / mirror)

    assert actual.version == expected.version
    assert actual.weights == expected.weights
    assert actual.sensitive_paths == expected.sensitive_paths
    assert _rule_fields(actual.rules) == _rule_fields(expected.rules)
