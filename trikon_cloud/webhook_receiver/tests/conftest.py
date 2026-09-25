# Fixture helper signatures use ``dict[str, Any]`` to model the arbitrary
# JSON shapes GitHub and API Gateway HTTP API v2 emit. Under the repo's
# ``disallow_any_explicit = true`` mypy config, each such helper surfaces as
# an ``explicit-any`` error. Suppress the check at file scope — this is a
# test conftest, not a production module, and the ``Any`` here is bounded
# to fixture data that flows into pytest tests.
# mypy: disable-error-code="explicit-any"
"""Shared pytest fixtures for the Trikon Cloud webhook receiver tests.

Provides:

* Canonical fixture data constants (installation id, repo full name,
  PR number, head/base SHAs, sender login, delivery id, webhook secret
  bytes) — inline Python per the design.md §8 rule that fixture data
  lives as constants in this file, NOT as top-level ``.json`` files
  under ``tests/fixtures/``.
* :func:`compute_signature` — builds the ``sha256=<hex>`` value GitHub
  would send in ``X-Hub-Signature-256`` for a given body / secret pair.
* :func:`make_pull_request_payload` — assembles the canonical
  ``pull_request.<action>`` payload dict (memo §5.1 shape).
* :func:`make_api_gateway_event` — wraps a body + headers in the
  API Gateway HTTP API v2 event envelope.
* :func:`mock_aws_env`, :func:`sqs_queue`,
  :func:`secrets_manager_secret` — pytest fixtures that stand up
  moto-backed AWS credentials + resources for tests that exercise the
  Wave-2 leaves against a mocked AWS control plane.

See ``.kiro/specs/trikon-cloud-webhook-receiver/design.md`` §5.1, §5.2,
§8 for the authoritative shapes referenced here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Canonical fixture data constants (design.md §5.1 shape).
# ---------------------------------------------------------------------------

WEBHOOK_SECRET_BYTES: bytes = b"trikon-test-secret-not-a-real-secret"

CANONICAL_INSTALLATION_ID: int = 12345678
CANONICAL_REPO_FULL_NAME: str = "octocat/hello-world"
CANONICAL_PR_NUMBER: int = 42
CANONICAL_HEAD_SHA: str = "6aabf09b" + "0" * 32  # 40-char hex
CANONICAL_BASE_SHA: str = "87f7a31b" + "0" * 32  # 40-char hex
CANONICAL_SENDER_LOGIN: str = "octocat"
CANONICAL_DELIVERY_ID: str = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
CANONICAL_DEFAULT_BRANCH: str = "main"


# ---------------------------------------------------------------------------
# Helper functions — deterministic constructors for test payloads.
# ---------------------------------------------------------------------------


def compute_signature(body: bytes, secret: bytes = WEBHOOK_SECRET_BYTES) -> str:
    """Return the ``X-Hub-Signature-256`` value GitHub would emit for ``body``.

    Mirrors the algorithm in :mod:`trikon_cloud.webhook_receiver.hmac_verifier`;
    tests use this to construct signature headers that either match (pass to
    ``verify_signature`` unchanged) or mismatch (mutate the returned string).
    """
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_pull_request_payload(*, action: str = "opened") -> dict[str, Any]:
    """Build a canonical GitHub ``pull_request`` webhook payload dict.

    The shape matches design.md §5.1 verbatim — five top-level keys, five
    nested references. ``action`` defaults to ``"opened"`` (the primary
    enqueue trigger); tests that exercise the ``synchronize`` or
    ``closed`` branches override it.
    """
    return {
        "action": action,
        "installation": {"id": CANONICAL_INSTALLATION_ID},
        "repository": {
            "full_name": CANONICAL_REPO_FULL_NAME,
            "default_branch": CANONICAL_DEFAULT_BRANCH,
        },
        "pull_request": {
            "number": CANONICAL_PR_NUMBER,
            "head": {"sha": CANONICAL_HEAD_SHA},
            "base": {"sha": CANONICAL_BASE_SHA},
        },
        "sender": {"login": CANONICAL_SENDER_LOGIN},
    }


def make_api_gateway_event(
    *,
    body: bytes,
    headers: dict[str, str],
    method: str = "POST",
    path: str = "/webhooks/github",
    is_base64_encoded: bool = False,
) -> dict[str, Any]:
    """Wrap ``body`` + ``headers`` in the API Gateway HTTP API v2 event shape.

    ``is_base64_encoded`` toggles the second body-transport path
    powertools' :class:`APIGatewayHttpResolver` supports; the default
    (``False``) matches GitHub's actual delivery shape.
    """
    body_str = (
        base64.b64encode(body).decode("ascii")
        if is_base64_encoded
        else body.decode("utf-8")
    )
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1"},
        },
        "body": body_str,
        "isBase64Encoded": is_base64_encoded,
    }


# ---------------------------------------------------------------------------
# Pytest fixtures — moto-backed AWS control-plane mocks.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set testing AWS credentials so moto's mocks activate cleanly.

    monkeypatch is function-scoped, so this fixture is function-scoped
    too — moto's ``mock_aws()`` context lives inside each dependent
    fixture (see :func:`sqs_queue`, :func:`secrets_manager_secret`) so
    per-test isolation is guaranteed.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def sqs_queue(mock_aws_env: None) -> Iterator[str]:
    """Create a moto-backed SQS queue and yield its URL.

    Depends on :func:`mock_aws_env` so the fake AWS credentials are in
    place before boto3 constructs its client.
    """
    del mock_aws_env  # dependency only; monkeypatch has fired by now
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        resp = client.create_queue(QueueName="trikon-verify-jobs-test")
        yield str(resp["QueueUrl"])


@pytest.fixture
def secrets_manager_secret(mock_aws_env: None) -> Iterator[str]:
    """Create a moto-backed Secrets Manager secret and yield the ARN.

    The stored ``SecretString`` is ``WEBHOOK_SECRET_BYTES`` decoded as
    UTF-8, matching the memo §3.5 rotation shape — the receiver reads
    the string, encodes it back to bytes, and passes those bytes to
    :func:`hmac_verifier.verify_signature`.
    """
    del mock_aws_env  # dependency only; monkeypatch has fired by now
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        resp = client.create_secret(
            Name="trikon-cloud/github-app-webhook-secret-test",
            SecretString=WEBHOOK_SECRET_BYTES.decode("utf-8"),
        )
        yield str(resp["ARN"])
