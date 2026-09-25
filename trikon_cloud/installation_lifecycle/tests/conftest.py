"""Shared pytest fixtures for the Trikon Cloud installation-lifecycle tests.

Provides:

* Canonical fixture data constants — same set as the orchestrator's
  conftest so the two suites read from an identical vocabulary.
  Kept as module-level constants in this file (rather than imported
  from a shared helper) so this conftest is self-contained and does
  not couple the lifecycle tests to a private cross-package module.
* :func:`rsa_test_private_key_pem` — session-scoped fixture returning
  a freshly generated RSA-2048 PKCS8 PEM. Mirrors Spec 2's pattern at
  ``trikon_cloud/fargate_runner/tests/conftest.py``.
* :func:`make_sqs_job_message` — factory returning a fully-populated
  :class:`trikon_cloud.webhook_receiver.models.SqsJobMessage`. The
  lifecycle package does not consume this shape at runtime, but
  cross-suite Wave-5 tests that assert on both handlers' behavior on
  a single delivery need access to it.
* :func:`make_installation_event_message` — factory returning a
  :class:`trikon_cloud.installation_lifecycle.models.InstallationEventMessage`
  populated with canonical constants; ``event_type`` defaults to
  ``"installation.created"``.
* :func:`_env_setup` — autouse function-scoped fixture that populates
  the three :class:`LifecycleEnvConfig` aliases in the process env.
* :func:`_powertools_logger_state_clear` — autouse function-scoped
  fixture that calls :meth:`Logger.clear_state` on both package
  loggers so ``append_keys`` state does not leak across tests.

See ``.kiro/specs/trikon-cloud-orchestrator/design.md`` §2.3, §3.2,
§10 for the authoritative shapes referenced here.
"""

from __future__ import annotations

from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from trikon_cloud.installation_lifecycle.logger import (
    get_logger as get_lifecycle_logger,
)
from trikon_cloud.installation_lifecycle.models import InstallationEventMessage
from trikon_cloud.orchestrator.logger import get_logger as get_orchestrator_logger
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# ---------------------------------------------------------------------------
# Canonical fixture data constants (design.md §3.3 concrete payload).
# ---------------------------------------------------------------------------

CANONICAL_INSTALLATION_ID: int = 12345678
CANONICAL_REPO_FULL_NAME: str = "octocat/hello-world"
CANONICAL_PR_NUMBER: int = 42
# 40-char lowercase hex — matches ``SqsJobMessage.head_sha``'s
# ``pattern=r"^[0-9a-f]{40}$"`` validator.
CANONICAL_HEAD_SHA: str = "6aabf09b1abcdef0123456789abcdef012345678"
CANONICAL_BASE_SHA: str = "87f7a31b1abcdef0123456789abcdef012345678"
CANONICAL_DELIVERY_ID: str = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
CANONICAL_EVENT_TYPE: str = "pull_request.opened"
CANONICAL_SENT_AT: str = "2024-11-14T12:34:56.789+00:00"
CANONICAL_ACCOUNT_ID: str = "000000000000"
CANONICAL_APP_ID: int = 999999
CANONICAL_APP_PRIVATE_KEY_SECRET_ARN: str = (
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef"
)
# Alias for :data:`CANONICAL_APP_ID` under the name the
# ``installation_lifecycle`` package uses (``github_app_id`` on the
# SQS body per design.md §3.2). Kept identically valued so cross-suite
# tests can treat the two names as interchangeable.
CANONICAL_GITHUB_APP_ID: int = 999999


# ---------------------------------------------------------------------------
# Session-scoped RSA test keypair — mirrors Spec 2's conftest pattern.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_test_private_key_pem() -> bytes:
    """Generate a fresh RSA-2048 private key once per test session.

    Returns the PEM-encoded PKCS8 bytes suitable for any GitHub App
    JWT-signing path the lifecycle tests exercise. Session-scoped so
    the (relatively expensive) key generation runs once, not per
    test.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )


# ---------------------------------------------------------------------------
# Factories — functions, not fixtures.
# ---------------------------------------------------------------------------


def make_sqs_job_message(**overrides: object) -> SqsJobMessage:
    """Build a canonical :class:`SqsJobMessage` for tests.

    Populates every field with the module-level canonical constants.
    Callers override individual fields via keyword arguments —
    ``make_sqs_job_message(pr_number=99)`` — to exercise specific
    branches. Included here for cross-suite consistency with the
    orchestrator conftest.
    """
    base: dict[str, object] = {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "repo_full_name": CANONICAL_REPO_FULL_NAME,
        "pr_number": CANONICAL_PR_NUMBER,
        "head_sha": CANONICAL_HEAD_SHA,
        "base_sha": CANONICAL_BASE_SHA,
        "event_type": CANONICAL_EVENT_TYPE,
        "sent_at": CANONICAL_SENT_AT,
        "delivery_id": CANONICAL_DELIVERY_ID,
    }
    base.update(overrides)
    return SqsJobMessage.model_validate(base)


def make_installation_event_message(
    event_type: Literal[
        "installation.created",
        "installation.deleted",
        "installation_repositories.added",
        "installation_repositories.removed",
    ] = "installation.created",
    **overrides: object,
) -> InstallationEventMessage:
    """Build a canonical :class:`InstallationEventMessage` for tests.

    ``event_type`` is a positional-style keyword with a default of
    ``"installation.created"`` (the primary provisioning trigger); it
    is a :data:`typing.Literal` of the four values the Lifecycle_Handler
    dispatches on (design.md §5.1) so mypy rejects a typo at the call
    site. ``repositories`` defaults to a single-entry tuple; callers
    pass ``repositories=()`` or a different tuple to exercise the
    empty-repo-set and multi-repo branches.
    """
    base: dict[str, object] = {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "github_app_id": CANONICAL_GITHUB_APP_ID,
        "event_type": event_type,
        "repositories": (CANONICAL_REPO_FULL_NAME,),
        "sent_at": CANONICAL_SENT_AT,
        "delivery_id": CANONICAL_DELIVERY_ID,
    }
    base.update(overrides)
    return InstallationEventMessage.model_validate(base)


# ---------------------------------------------------------------------------
# Autouse fixtures — env, logger state.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the process env with the three Lifecycle aliases.

    Any test that constructs a :class:`LifecycleEnvConfig` (either
    directly or transitively via the handler's cold-start bootstrap)
    inherits these values without per-test boilerplate. Individual
    tests override with :meth:`pytest.MonkeyPatch.setenv` on the same
    ``monkeypatch`` instance to exercise malformed / missing-var
    branches.
    """
    monkeypatch.setenv("TRIKON_AWS_ACCOUNT_ID", CANONICAL_ACCOUNT_ID)
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", CANONICAL_APP_PRIVATE_KEY_SECRET_ARN
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _powertools_logger_state_clear() -> None:
    """Reset both package loggers' persistent context between tests.

    :meth:`aws_lambda_powertools.Logger.append_keys` mutates internal
    state that survives across tests because the ``Logger`` singleton
    lives at module scope in both
    :mod:`trikon_cloud.installation_lifecycle.logger` and
    :mod:`trikon_cloud.orchestrator.logger`. Without this reset, keys
    appended by :func:`append_lifecycle_context` in one test would
    leak into every subsequent test's log records.
    """
    get_lifecycle_logger().clear_state()
    get_orchestrator_logger().clear_state()
