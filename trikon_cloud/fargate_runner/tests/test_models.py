# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's ``disallow_any_explicit``
# config, constructing those models in test code surfaces as an
# ``explicit-any`` error the plugin generates, not code we write.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.fargate_runner.models`.

Round-trip / edge-case checks for every Pydantic model in the sibling
package: :class:`RunnerEnvConfig` (env-var loader), :class:`VerdictRow`
(with the ``evidence_blob`` XOR ``evidence_s3_key`` validator and the
``frozen=True`` enforcement), :class:`PrStateRow` (nullable-ids case),
:class:`CheckRunCreatePayload` (with the ``Literal["Trikon"]``
enforcement that carries Invariant 7 at the type level), and
:class:`GithubInstallationTokenResponse` (with the ``extra="allow"``
branch for future GitHub fields).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trikon_cloud.fargate_runner.models import (
    CheckRunCreatePayload,
    CheckRunOutput,
    GithubInstallationTokenResponse,
    PrStateRow,
    RunnerEnvConfig,
    VerdictRow,
)

from .conftest import (
    BASE_SHA,
    HEAD_SHA,
    INSTALLATION_ID,
    PR_NUMBER,
    REPO_FULL_NAME,
    make_env_vars,
)

# ---------------------------------------------------------------------------
# Fixture: monkeypatch every env var the loader reads.
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set every :class:`RunnerEnvConfig` env var to a valid default.

    Individual tests can then delete or override single keys via the
    returned :class:`MonkeyPatch` handle to exercise negative paths
    without leaking state into unrelated tests.
    """
    for key, value in make_env_vars().items():
        monkeypatch.setenv(key, value)
    # Clear any ambient overrides that might interfere with test defaults.
    for optional_key in (
        "TRIKON_VERDICTS_TABLE",
        "TRIKON_PR_STATE_TABLE",
        "TRIKON_EVIDENCE_BUCKET",
        "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE",
        "TRIKON_LOG_LEVEL",
    ):
        monkeypatch.delenv(optional_key, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# VerdictRow builder — a canonical row every test tweaks via overrides.
# ---------------------------------------------------------------------------


def _make_verdict_row(**overrides: Any) -> VerdictRow:
    """Build a canonical :class:`VerdictRow` with keyword overrides.

    Every field of :class:`VerdictRow` is populated with a valid
    default. Tests override single fields to exercise specific
    invariants (mutual-exclusivity of the evidence pointers, the
    ``frozen=True`` assignment guard, and so on).
    """
    defaults: dict[str, Any] = {
        "installation_id": INSTALLATION_ID,
        "sk": "2024-01-01T00:00:00.000Z#" + "a" * 32,
        "repo_full_name": REPO_FULL_NAME,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "decision": "block",
        "matched_rule": "test rule",
        "blast_radius_score": 42,
        "new_errors": 3,
        "new_warnings": 1,
        "preexisting_errors": 7,
        "duration_ms": 1234,
        "fargate_task_arn": "arn:aws:ecs:us-east-1:123456789012:task/trikon-verify-cluster/abcd",
        "schema_version": 2,
        "evidence_blob": b"gzipped-evidence",
        "evidence_s3_key": None,
        "risk_bucket_sk": "0042#2024-01-01T00:00:00.000Z",
    }
    defaults.update(overrides)
    return VerdictRow(**defaults)


# ---------------------------------------------------------------------------
# RunnerEnvConfig tests.
# ---------------------------------------------------------------------------


def test_runner_env_config_loads_from_environment(_fresh_env: pytest.MonkeyPatch) -> None:
    """Every alias in the env dict populates the corresponding field.

    Exercises the round-trip: monkeypatch each alias to its
    :func:`make_env_vars` default, construct
    :class:`RunnerEnvConfig`, and inspect the resulting object. All
    int-typed fields must land as ``int`` (pydantic-settings coerces
    the env string) and all str-typed fields as ``str``.
    """
    env = RunnerEnvConfig()

    assert env.installation_id == INSTALLATION_ID
    assert env.repo_full_name == REPO_FULL_NAME
    assert env.pr_number == PR_NUMBER
    assert env.head_sha == HEAD_SHA
    assert env.base_sha == BASE_SHA
    assert env.event_type == "pull_request.opened"
    assert env.delivery_id == "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
    assert env.aws_region == "us-east-1"
    assert env.app_id == 999999
    assert env.app_private_key_secret_arn.startswith(
        "arn:aws:secretsmanager:us-east-1:"
    )
    # Numeric fields land as int, not str.
    assert isinstance(env.installation_id, int)
    assert isinstance(env.pr_number, int)
    assert isinstance(env.app_id, int)
    # String fields land as str.
    assert isinstance(env.repo_full_name, str)
    assert isinstance(env.head_sha, str)


def test_runner_env_config_raises_on_missing_required_field(
    _fresh_env: pytest.MonkeyPatch,
) -> None:
    """A missing required alias raises :class:`ValidationError`."""
    _fresh_env.delenv("TRIKON_INSTALLATION_ID", raising=False)

    with pytest.raises(ValidationError):
        RunnerEnvConfig()


def test_runner_env_config_rejects_short_head_sha(
    _fresh_env: pytest.MonkeyPatch,
) -> None:
    """A 3-char head SHA violates the 40-char length constraint."""
    _fresh_env.setenv("TRIKON_HEAD_SHA", "abc")

    with pytest.raises(ValidationError):
        RunnerEnvConfig()


def test_runner_env_config_rejects_non_hex_base_sha(
    _fresh_env: pytest.MonkeyPatch,
) -> None:
    """A 40-char non-hex base SHA violates the hex pattern constraint."""
    _fresh_env.setenv("TRIKON_BASE_SHA", "z" * 40)

    with pytest.raises(ValidationError):
        RunnerEnvConfig()


def test_runner_env_config_repo_working_dir(_fresh_env: pytest.MonkeyPatch) -> None:
    """``repo_working_dir`` is the fixed ``/tmp/repo`` scratch mount."""
    env = RunnerEnvConfig()
    assert env.repo_working_dir == Path("/tmp/repo")


def test_runner_env_config_pr_state_key(_fresh_env: pytest.MonkeyPatch) -> None:
    """``pr_state_key`` composes the ``trikon_pr_state`` partition key."""
    env = RunnerEnvConfig()
    expected = f"{env.installation_id}#{env.repo_full_name}#{env.pr_number}"
    assert env.pr_state_key == expected


# ---------------------------------------------------------------------------
# VerdictRow tests.
# ---------------------------------------------------------------------------


def test_verdict_row_evidence_blob_xor_s3_key_via_validator() -> None:
    """The ``model_validator`` enforces exactly-one-non-None on the pair.

    Three cases:

    * both set → :class:`ValidationError`
    * both None → :class:`ValidationError`
    * exactly one set → constructs cleanly
    """
    # Both non-None: rejected.
    with pytest.raises(ValidationError):
        _make_verdict_row(evidence_blob=b"gzipped", evidence_s3_key="key")

    # Both None: rejected.
    with pytest.raises(ValidationError):
        _make_verdict_row(evidence_blob=None, evidence_s3_key=None)

    # Inline branch: only ``evidence_blob``.
    inline = _make_verdict_row(evidence_blob=b"gzipped", evidence_s3_key=None)
    assert inline.evidence_blob == b"gzipped"
    assert inline.evidence_s3_key is None

    # Spill branch: only ``evidence_s3_key``.
    spilled = _make_verdict_row(
        evidence_blob=None, evidence_s3_key="12345678/audit.json.gz"
    )
    assert spilled.evidence_blob is None
    assert spilled.evidence_s3_key == "12345678/audit.json.gz"


def test_verdict_row_frozen() -> None:
    """``frozen=True`` blocks post-construction attribute assignment.

    Pydantic v2 raises :class:`ValidationError` on frozen assignment,
    but some historical versions raised :class:`TypeError` — accept
    either so the test tracks the pydantic contract without pinning
    a specific version.
    """
    row = _make_verdict_row()
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        row.decision = "allow"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PrStateRow test.
# ---------------------------------------------------------------------------


def test_pr_state_row_last_ids_nullable() -> None:
    """First-run PR state carries ``None`` for both last-artifact ids."""
    row = PrStateRow(
        pr_key="12345678#octocat/hello-world#42",
        last_comment_id=None,
        last_check_run_id=None,
        last_head_sha="a" * 40,
        last_updated_at="2024-01-01T00:00:00.000Z",
    )
    assert row.last_comment_id is None
    assert row.last_check_run_id is None
    assert row.last_head_sha == "a" * 40


# ---------------------------------------------------------------------------
# CheckRunCreatePayload — the Literal["Trikon"] enforcement.
# ---------------------------------------------------------------------------


def test_check_run_create_payload_name_literal_trikon() -> None:
    """``name`` must be the literal ``"Trikon"`` — Invariant 7 at the type level.

    Constructing with ``name="Trikon"`` succeeds. Constructing with
    any other string (``"AgentGuard"``, the pre-rename product name)
    raises :class:`ValidationError` — the ``Literal["Trikon"]`` type
    fails pydantic's validation before the type checker even runs.
    """
    output = CheckRunOutput(
        title="**Trikon Cloud** verified this PR: ✅ **ALLOW**",
        summary="summary body",
        text=None,
    )

    # Happy path — accepted.
    payload = CheckRunCreatePayload(
        name="Trikon",
        head_sha=HEAD_SHA,
        status="completed",
        conclusion="failure",
        output=output,
        details_url="https://cloud.trikon.dev/audits/x",
    )
    assert payload.name == "Trikon"

    # Rename attempt — rejected.
    # The pydantic mypy plugin widens ``Literal["Trikon"]`` accepting
    # ``str`` at construction sites via its synthesized ``__init__``, so
    # this call passes mypy but fails pydantic's runtime Literal check.
    with pytest.raises(ValidationError):
        CheckRunCreatePayload(
            name="AgentGuard",
            head_sha=HEAD_SHA,
            status="completed",
            conclusion="failure",
            output=output,
            details_url="https://cloud.trikon.dev/audits/x",
        )


# ---------------------------------------------------------------------------
# GithubInstallationTokenResponse — extra="allow" future-compat.
# ---------------------------------------------------------------------------


def test_github_installation_token_response_extra_allow() -> None:
    """Unknown response fields pass through under ``extra="allow"``.

    GitHub may add fields to the token-mint response over time
    (``permissions``, ``repository_selection``, ...); the runner only
    reads ``token`` and ``expires_at`` today. Validation must accept
    payloads carrying additional keys so a future GitHub change does
    not brick the runner mid-release.
    """
    payload: dict[str, Any] = {
        "token": "x",
        "expires_at": "y",
        "permissions": {"metadata": "read"},
        "unknown_field": "future",
    }
    response = GithubInstallationTokenResponse.model_validate(payload)
    assert response.token == "x"
    assert response.expires_at == "y"
