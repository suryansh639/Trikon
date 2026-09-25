# Helpers use ``dict[str, Any]`` for arbitrary GitHub webhook payload
# shapes. Under the repo's ``disallow_any_explicit = true`` mypy
# config, this surfaces as an ``explicit-any`` error. Suppress at file
# scope — this is a test module, not a production module.
# mypy: disable-error-code="explicit-any"
"""Pydantic model round-trip and edge-case tests for the webhook receiver models.

Covers three model classes exported from
:mod:`trikon_cloud.webhook_receiver.models`:

* :class:`GithubWebhookPayload` — canonical parse, ``extra="allow"``
  behavior, plus rejection of malformed head SHA / negative PR number
  / missing installation id.
* :class:`SqsJobMessage` — the on-the-wire field order guarantee from
  Requirement 5.3 (verified by parsing the serialized JSON with
  ``object_pairs_hook=list`` so we can inspect the actual key order)
  plus the extra-field disposition. The model does not declare
  ``model_config = ConfigDict(extra="forbid")``; pydantic v2's default
  ``extra="ignore"`` therefore governs, and this test suite asserts the
  observed drop-on-ignore semantics (see
  ``test_sqs_job_message_ignores_extra_fields``).
* :class:`ReceiverEnvConfig` — environment-variable loading and the
  ``ValidationError`` surface when a required alias is unset.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from trikon_cloud.webhook_receiver.models import (
    GithubWebhookPayload,
    ReceiverEnvConfig,
    SqsJobMessage,
)
from trikon_cloud.webhook_receiver.tests.conftest import (
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    make_pull_request_payload,
)

# ---------------------------------------------------------------------------
# GithubWebhookPayload.
# ---------------------------------------------------------------------------


def test_github_webhook_payload_parses_canonical_fixture() -> None:
    """The canonical conftest fixture round-trips through ``GithubWebhookPayload``."""
    fixture = make_pull_request_payload(action="opened")
    model = GithubWebhookPayload.model_validate(fixture)

    assert model.action == "opened"
    assert isinstance(model.installation.id, int)
    assert model.installation.id == CANONICAL_INSTALLATION_ID
    assert model.repository.full_name == CANONICAL_REPO_FULL_NAME
    assert model.repository.default_branch == "main"
    assert model.pull_request.number == CANONICAL_PR_NUMBER
    assert model.pull_request.head.sha == CANONICAL_HEAD_SHA
    assert model.pull_request.base.sha == CANONICAL_BASE_SHA
    assert model.sender.login == "octocat"


def test_github_webhook_payload_extra_allow() -> None:
    """Unknown top-level fields flow into ``__pydantic_extra__`` and don't reject."""
    fixture = make_pull_request_payload(action="opened")
    fixture["future_field"] = "future_value"

    model = GithubWebhookPayload.model_validate(fixture)

    assert model.__pydantic_extra__ is not None
    assert model.__pydantic_extra__.get("future_field") == "future_value"


def test_github_webhook_payload_rejects_wrong_sha_length() -> None:
    """A 39-char ``pull_request.head.sha`` fails validation."""
    fixture = make_pull_request_payload(action="opened")
    fixture["pull_request"]["head"]["sha"] = "a" * 39

    with pytest.raises(ValidationError):
        GithubWebhookPayload.model_validate(fixture)


def test_github_webhook_payload_rejects_non_hex_sha() -> None:
    """A 40-char but non-hex ``pull_request.head.sha`` fails validation."""
    fixture = make_pull_request_payload(action="opened")
    fixture["pull_request"]["head"]["sha"] = "z" * 40

    with pytest.raises(ValidationError):
        GithubWebhookPayload.model_validate(fixture)


def test_github_webhook_payload_rejects_negative_pr_number() -> None:
    """A negative ``pull_request.number`` fails the ``ge=1`` constraint."""
    fixture = make_pull_request_payload(action="opened")
    fixture["pull_request"]["number"] = -1

    with pytest.raises(ValidationError):
        GithubWebhookPayload.model_validate(fixture)


def test_github_webhook_payload_rejects_missing_installation_id() -> None:
    """Removing ``installation.id`` fails validation (required field)."""
    fixture = make_pull_request_payload(action="opened")
    del fixture["installation"]["id"]

    with pytest.raises(ValidationError):
        GithubWebhookPayload.model_validate(fixture)


# ---------------------------------------------------------------------------
# SqsJobMessage.
# ---------------------------------------------------------------------------


_EXPECTED_KEY_ORDER: list[str] = [
    "installation_id",
    "repo_full_name",
    "pr_number",
    "head_sha",
    "base_sha",
    "event_type",
    "sent_at",
    "delivery_id",
]


def _canonical_sqs_message_dict() -> dict[str, Any]:
    """Return a full-shape dict for :class:`SqsJobMessage.model_validate` input."""
    return {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "repo_full_name": CANONICAL_REPO_FULL_NAME,
        "pr_number": CANONICAL_PR_NUMBER,
        "head_sha": CANONICAL_HEAD_SHA,
        "base_sha": CANONICAL_BASE_SHA,
        "event_type": "pull_request.opened",
        "sent_at": "2024-01-15T10:00:00.000Z",
        "delivery_id": CANONICAL_DELIVERY_ID,
    }


def test_sqs_job_message_field_order_via_model_dump_json() -> None:
    """``model_dump_json`` preserves the declared field order (Requirement 5.3).

    Parses the serialized JSON with ``object_pairs_hook=list`` so we
    see the on-the-wire key order verbatim rather than a Python-dict-
    normalized view. The expected order matches design.md §5.2.
    """
    msg = SqsJobMessage(**_canonical_sqs_message_dict())
    serialized = msg.model_dump_json()
    keys: list[str] = [k for k, _ in json.loads(serialized, object_pairs_hook=list)]

    assert keys == _EXPECTED_KEY_ORDER


def test_sqs_job_message_ignores_extra_fields() -> None:
    """Extra fields are ignored — no ``model_config = ConfigDict(extra="forbid")``.

    Task 2.1 specified ``SqsJobMessage`` must not carry ``extra="allow"``
    (the shape is our schema, not GitHub's) but did not mandate
    ``extra="forbid"``. Pydantic v2's default ``extra="ignore"``
    therefore governs — an unknown field is silently dropped rather
    than rejected. This test pins the observed behavior so a future
    change to ``extra="forbid"`` will surface as a deliberate design
    decision (accompanied by a test update), not an accident.
    """
    payload = _canonical_sqs_message_dict()
    payload["extra_field"] = "x"

    msg = SqsJobMessage.model_validate(payload)

    # Extra field must not appear on the model at any access path.
    assert not hasattr(msg, "extra_field")
    assert msg.__pydantic_extra__ is None or "extra_field" not in msg.__pydantic_extra__
    # And the serialized shape retains exactly the eight declared fields.
    assert list(msg.model_dump().keys()) == _EXPECTED_KEY_ORDER


# ---------------------------------------------------------------------------
# ReceiverEnvConfig.
# ---------------------------------------------------------------------------


def test_receiver_env_config_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the aliased env vars yields a fully populated config object."""
    monkeypatch.setenv("TRIKON_WEBHOOK_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
    monkeypatch.setenv(
        "TRIKON_VERIFY_JOBS_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123/trikon-verify-jobs",
    )
    monkeypatch.setenv(
        "TRIKON_INSTALLATION_EVENTS_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123/trikon-cloud-installation-events",
    )
    monkeypatch.setenv("TRIKON_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    config = ReceiverEnvConfig()

    assert config.webhook_secret_arn == "arn:aws:secretsmanager:us-east-1:123:secret:x"
    assert config.verify_jobs_queue_url == (
        "https://sqs.us-east-1.amazonaws.com/123/trikon-verify-jobs"
    )
    assert config.log_level == "DEBUG"
    assert config.aws_region == "us-east-1"


def test_receiver_env_config_raises_on_missing_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsetting ``TRIKON_WEBHOOK_SECRET_ARN`` raises ``ValidationError`` at construction."""
    monkeypatch.delenv("TRIKON_WEBHOOK_SECRET_ARN", raising=False)
    monkeypatch.setenv(
        "TRIKON_VERIFY_JOBS_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123/trikon-verify-jobs",
    )

    with pytest.raises(ValidationError):
        ReceiverEnvConfig()
