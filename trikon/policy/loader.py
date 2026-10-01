"""Load and validate a policy YAML from disk.

Implements the two entry points named in ``design.md §3.1``:

* :func:`load_policy` — read ``<repo>/<policy_path>`` (or an absolute
  ``policy_path``), parse it as YAML, validate it against
  :class:`trikon.policy.dsl.Policy`, and return the resulting model.
  Missing file → :func:`default_policy` (Requirement 2.2).

* :func:`default_policy` — load the packaged
  ``trikon/policy/default_policy.yaml`` shipped inside the wheel and
  return the same conservative 8-rule policy the ``trikon init``
  scaffold writes into ``.trikon/policy.yaml`` (Requirement 2.5).

Every foreign exception raised inside these functions
(``OSError``, ``yaml.YAMLError``, ``pydantic.ValidationError``,
``ModuleNotFoundError``, ``FileNotFoundError``) is caught at the module
boundary and re-raised as
:class:`trikon.policy.errors.PolicyLoadError` with the original attached
on ``__cause__`` (Requirement 7.1). That closure lets the SDK boundary
catch a single ``TrikonError`` subclass and translate any policy-load
failure into a well-formed ``require_human`` verdict without ambiguity.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pydantic
import yaml

from trikon.policy.dsl import Policy
from trikon.policy.errors import PolicyLoadError


def load_policy(repo_path: Path, policy_path: Path) -> Policy:
    """Load ``<repo_path>/<policy_path>`` (or absolute ``policy_path``) and validate it.

    See ``design.md §6.1`` for the 5-step algorithm.

    Args:
        repo_path: Absolute path to the git repository. Used only for the
            relative-path resolution rule in step 1.
        policy_path: Absolute or repo-relative path to the policy YAML.

    Returns:
        A validated :class:`Policy`. On the missing-file path, the same
        object :func:`default_policy` returns (Requirement 2.2).

    Raises:
        PolicyLoadError: The file exists but could not be read, parsed,
            or validated. The original ``OSError``, ``yaml.YAMLError``,
            or ``pydantic.ValidationError`` is chained via ``__cause__``
            (Requirements 2.3, 2.4, 7.1).
    """
    # Step 1: resolve the policy path against the repo root when relative.
    resolved = policy_path if policy_path.is_absolute() else repo_path / policy_path

    # Step 2: missing-file fallback — return the built-in default policy
    # rather than raise (Requirement 2.2).
    if not resolved.exists():
        return default_policy()

    # Step 3: read the file bytes. Wrap OSError for defense in depth
    # (read-permission errors, half-written files, mid-flight deletion).
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyLoadError(f"failed to read {resolved}") from exc

    # Step 4: parse the YAML and reject empty files. An empty file has
    # no `rules` key, which would fail Pydantic validation anyway; the
    # explicit check produces a clearer error message (Requirement 2.4).
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"{resolved} is not valid YAML") from exc

    if parsed is None:
        raise PolicyLoadError(f"{resolved} is empty")

    # Step 5: validate the parsed mapping against the Policy schema
    # (Requirement 2.3).
    try:
        return Policy.model_validate(parsed)
    except pydantic.ValidationError as exc:
        raise PolicyLoadError(f"{resolved} failed schema validation") from exc


def default_policy() -> Policy:
    """Return the built-in default policy shipped inside the wheel.

    Loads ``trikon/policy/default_policy.yaml`` via
    :func:`importlib.resources.files`, parses it with ``yaml.safe_load``,
    and returns the result of :meth:`Policy.model_validate`. See
    ``design.md §6.2``.

    Raises:
        PolicyLoadError: The packaged file is missing or malformed
            (indicates a broken wheel; should be unreachable in a
            correctly-built installation). The original
            ``ModuleNotFoundError``, ``FileNotFoundError``, ``OSError``,
            ``yaml.YAMLError``, or ``pydantic.ValidationError`` is
            chained via ``__cause__`` (Requirement 7.1).
    """
    # Step 1: locate the packaged YAML resource.
    try:
        resource = resources.files("trikon.policy") / "default_policy.yaml"
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        raise PolicyLoadError("packaged default_policy.yaml not found") from exc

    # Step 2: read the resource bytes as UTF-8 text.
    try:
        raw = resource.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        raise PolicyLoadError("failed to read packaged default_policy.yaml") from exc

    # Step 3: parse + validate under the same exception-wrapping
    # discipline as load_policy (Requirements 2.3, 2.4, 2.5).
    try:
        parsed = yaml.safe_load(raw)
        return Policy.model_validate(parsed)
    except (yaml.YAMLError, pydantic.ValidationError) as exc:
        raise PolicyLoadError("packaged default_policy.yaml is malformed") from exc
