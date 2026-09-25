# Fixture / helper signatures use ``dict[str, Any]`` and ``Any`` because
# GitHub payload shapes and API Gateway HTTP API v2 events are arbitrary
# JSON. Under the repo's ``disallow_any_explicit = true`` mypy config,
# each such helper surfaces as an ``explicit-any`` error. Suppress at
# file scope — this is a test module, not a production module.
# mypy: disable-error-code="explicit-any"
"""Routing, mapping, and HTTP-status tests for the installation-events amendment.

Dedicated test file for
``.kiro/specs/trikon-cloud-webhook-receiver-installation-events`` (design.md
§9). Five test groups mirror the design's testing strategy:

* Group 1 — pure routing tests against ``_route_event``.
* Group 2 — pure payload-mapping tests against
  ``_build_installation_message``.
* Group 3 — end-to-end HTTP status tests through ``on_github_webhook``
  with moto-backed SQS + Secrets Manager.
* Group 4 — queue-picker isolation tests verifying installation traffic
  hits only ``_installation_events_writer`` and pull_request traffic
  hits only ``_sqs_writer``.
* Group 5 — a single ``sent_at`` capture test verifying the
  handler-entry timestamp is threaded into the SQS body verbatim
  (Requirement 2.10).

Shipping ``conftest.py`` fixtures (``compute_signature``,
``make_api_gateway_event``, ``make_pull_request_payload``,
``WEBHOOK_SECRET_BYTES``, ``CANONICAL_INSTALLATION_ID``,
``CANONICAL_DELIVERY_ID``) are reused unchanged; the two moto helpers
(``_install_moto_env`` and ``_swap_sqs_writer_boto3``) mirror the
patterns established in ``test_handler.py`` so the Group 3/4/5 tests
run against the same simulated AWS control plane as the Spec 1 suite.
"""

from __future__ import annotations

import json
import types
from typing import Any, cast
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from trikon_cloud.webhook_receiver import handler, sqs_writer
from trikon_cloud.webhook_receiver.handler import (
    _build_installation_message,
    _route_event,
)
from trikon_cloud.webhook_receiver.models import GithubInstallationPayload
from trikon_cloud.webhook_receiver.sqs_writer import SqsWriter
from trikon_cloud.webhook_receiver.tests.conftest import (
    CANONICAL_DELIVERY_ID,
    CANONICAL_INSTALLATION_ID,
    WEBHOOK_SECRET_BYTES,
    compute_signature,
    make_api_gateway_event,
    make_pull_request_payload,
)

# ---------------------------------------------------------------------------
# Module-scope fixture-data constants (design.md §9.6).
# ---------------------------------------------------------------------------

CANONICAL_APP_ID: int = 987654
CANONICAL_INSTALLATION_REPO_A: str = "octocat/repo-alpha"
CANONICAL_INSTALLATION_REPO_B: str = "octocat/repo-beta"
CANONICAL_INSTALLATION_REPO_C: str = "octocat/repo-gamma"


def make_installation_payload(
    *,
    action: str,
    installation_id: int = CANONICAL_INSTALLATION_ID,
    app_id: int = CANONICAL_APP_ID,
    repositories: tuple[str, ...] | None = None,
    repositories_added: tuple[str, ...] | None = None,
    repositories_removed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a canonical GitHub installation payload dict (design.md §9.6).

    The three list fields are declared **individually** — a caller
    selects the correct one for the (event_type, action) it is
    exercising and leaves the others as ``None`` (in which case the
    key is omitted from the returned dict entirely, matching GitHub's
    actual delivery shape).
    """
    payload: dict[str, Any] = {
        "action": action,
        "installation": {"id": installation_id, "app_id": app_id},
    }
    if repositories is not None:
        payload["repositories"] = [{"full_name": name} for name in repositories]
    if repositories_added is not None:
        payload["repositories_added"] = [
            {"full_name": name} for name in repositories_added
        ]
    if repositories_removed is not None:
        payload["repositories_removed"] = [
            {"full_name": name} for name in repositories_removed
        ]
    return payload


# ---------------------------------------------------------------------------
# Local helpers — mirror the patterns in ``test_handler.py`` so this file
# is self-contained and does not depend on private helpers there.
# ---------------------------------------------------------------------------


class SpySqsClient:
    """Records every ``send_message`` call and either raises or forwards.

    When ``failure_exc`` is set, ``send_message`` records the call as
    ``"failure"`` and raises. Otherwise it forwards to ``real_client``
    (typically the moto-backed boto3 SQS client) and records the
    outcome based on whether the underlying call raised.
    """

    def __init__(
        self,
        *,
        real_client: Any = None,
        failure_exc: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._real: Any = real_client
        self._failure_exc: BaseException | None = failure_exc

    def send_message(
        self, *, QueueUrl: str, MessageBody: str, **kwargs: Any  # noqa: N803
    ) -> Any:
        call_kwargs: dict[str, Any] = {"QueueUrl": QueueUrl, **kwargs}
        if self._failure_exc is not None:
            self.calls.append(("failure", MessageBody, call_kwargs))
            raise self._failure_exc
        try:
            result = self._real.send_message(
                QueueUrl=QueueUrl, MessageBody=MessageBody, **kwargs
            )
        except BaseException:
            self.calls.append(("failure", MessageBody, call_kwargs))
            raise
        self.calls.append(("success", MessageBody, call_kwargs))
        return result


def _make_lambda_context() -> Any:
    """Return a stand-in ``LambdaContext`` sufficient for the powertools resolver."""
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-webhook-receiver-test"
    ctx.invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:test"
    )
    return ctx


def _response_body(response: dict[str, Any]) -> str:
    """Return ``response["body"]`` narrowed to ``str`` for JSON parsing."""
    return cast(str, response["body"])


def _reset_handler_state() -> None:
    """Zero the handler module's five cached slots so each invocation cold-starts.

    Includes the amendment's new ``_installation_events_writer`` slot
    alongside the four shipping slots so the second writer's cache is
    not carried across tests.
    """
    handler._env_config = None
    handler._webhook_secret = None
    handler._sqs_writer = None
    handler._installation_events_writer = None
    handler._secrets_client = None


@pytest.fixture(autouse=True)
def _reset_handler_module_state() -> None:
    """Reset handler module-level caches before each test function."""
    _reset_handler_state()


def _install_moto_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, str, Any]:
    """Create moto SQS queues (verify + installation) + Secrets Manager secret + env vars.

    Returns ``(verify_queue_url, installation_queue_url, secret_arn,
    real_sqs_client)``. Must be called inside a ``@mock_aws`` context —
    the real boto3 clients this function constructs are moto-backed
    only while the decorator is active.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    real_sqs = boto3.client("sqs", region_name="us-east-1")
    verify_queue_url = real_sqs.create_queue(
        QueueName="trikon-verify-jobs-test"
    )["QueueUrl"]
    installation_queue_url = real_sqs.create_queue(
        QueueName="trikon-cloud-installation-events-test"
    )["QueueUrl"]
    real_secrets = boto3.client("secretsmanager", region_name="us-east-1")
    secret_arn = real_secrets.create_secret(
        Name="trikon-cloud/github-app-webhook-secret-test",
        SecretString=WEBHOOK_SECRET_BYTES.decode("utf-8"),
    )["ARN"]

    monkeypatch.setenv("TRIKON_WEBHOOK_SECRET_ARN", secret_arn)
    monkeypatch.setenv("TRIKON_VERIFY_JOBS_QUEUE_URL", verify_queue_url)
    monkeypatch.setenv(
        "TRIKON_INSTALLATION_EVENTS_QUEUE_URL", installation_queue_url
    )
    return verify_queue_url, installation_queue_url, secret_arn, real_sqs


def _swap_sqs_writer_boto3(
    monkeypatch: pytest.MonkeyPatch, spy: SpySqsClient
) -> None:
    """Replace ``sqs_writer.boto3`` with a namespace whose ``client`` returns ``spy``.

    Scopes the replacement to the SQS writer module — the handler's
    ``boto3.client("secretsmanager")`` construction is unaffected, so
    the moto-backed Secrets Manager continues to serve the webhook
    secret.
    """
    mock_boto3 = types.SimpleNamespace(client=lambda *_a, **_kw: spy)
    monkeypatch.setattr(sqs_writer, "boto3", mock_boto3)


def _api_gateway_event(
    *,
    body: bytes,
    headers: dict[str, str],
    method: str = "POST",
    path: str = "/webhooks/github",
) -> dict[str, Any]:
    """Build an API Gateway HTTP API v2 event with the ``stage`` field powertools requires."""
    event = make_api_gateway_event(
        body=body, headers=headers, method=method, path=path
    )
    request_context: dict[str, Any] = event["requestContext"]
    request_context["stage"] = "$default"
    return event


def _installation_event(
    *,
    event_type: str = "installation",
    action: str = "created",
    delivery_id: str = CANONICAL_DELIVERY_ID,
    repositories: tuple[str, ...] | None = None,
    repositories_added: tuple[str, ...] | None = None,
    repositories_removed: tuple[str, ...] | None = None,
    signature_secret: bytes = WEBHOOK_SECRET_BYTES,
    include_signature: bool = True,
    include_delivery: bool = True,
    installation_id: int = CANONICAL_INSTALLATION_ID,
    app_id: int = CANONICAL_APP_ID,
) -> tuple[dict[str, Any], bytes]:
    """Build a signed API-Gateway-shaped installation event + return the raw body bytes."""
    payload = make_installation_payload(
        action=action,
        installation_id=installation_id,
        app_id=app_id,
        repositories=repositories,
        repositories_added=repositories_added,
        repositories_removed=repositories_removed,
    )
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {"X-GitHub-Event": event_type}
    if include_delivery:
        headers["X-GitHub-Delivery"] = delivery_id
    if include_signature:
        headers["X-Hub-Signature-256"] = compute_signature(body, signature_secret)
    event = _api_gateway_event(body=body, headers=headers)
    return event, body


# ===========================================================================
# Group 1 — Routing tests (design.md §9.1).
# ===========================================================================


def test_route_installation_created_returns_enqueue_installation() -> None:
    """``installation.created`` routes to ``enqueue_installation`` (Requirement 1.1)."""
    assert _route_event("installation", "created") == "enqueue_installation"


def test_route_installation_deleted_returns_enqueue_installation() -> None:
    """``installation.deleted`` routes to ``enqueue_installation`` (Requirement 1.2)."""
    assert _route_event("installation", "deleted") == "enqueue_installation"


def test_route_installation_repositories_added_returns_enqueue_installation() -> None:
    """``installation_repositories.added`` routes to ``enqueue_installation`` (Requirement 1.3)."""
    assert (
        _route_event("installation_repositories", "added") == "enqueue_installation"
    )


def test_route_installation_repositories_removed_returns_enqueue_installation() -> None:
    """``installation_repositories.removed`` routes to ``enqueue_installation`` (Requirement 1.4)."""
    assert (
        _route_event("installation_repositories", "removed")
        == "enqueue_installation"
    )


def test_route_installation_unknown_action_returns_non_enqueue() -> None:
    """``installation`` with an unrecognised action falls through to ``non_enqueue`` (Requirement 1.5)."""
    assert _route_event("installation", "suspended") == "non_enqueue"


def test_route_installation_repositories_unknown_action_returns_non_enqueue() -> None:
    """``installation_repositories`` with an unrecognised action falls through (Requirement 1.5)."""
    assert _route_event("installation_repositories", "created") == "non_enqueue"


def test_route_preserves_pull_request_opened() -> None:
    """``pull_request.opened`` continues to route to ``enqueue_verify`` (Requirement 1.7)."""
    assert _route_event("pull_request", "opened") == "enqueue_verify"


def test_route_preserves_pull_request_synchronize() -> None:
    """``pull_request.synchronize`` continues to route to ``enqueue_verify`` (Requirement 1.7)."""
    assert _route_event("pull_request", "synchronize") == "enqueue_verify"


# ===========================================================================
# Group 2 — Payload mapping tests (design.md §9.2).
# ===========================================================================


_FIXED_SENT_AT: str = "2025-01-15T12:34:56.789Z"


def test_build_message_installation_created_maps_repositories_full_names() -> None:
    """``installation.created`` maps ``payload.repositories[*].full_name`` order-preservingly.

    Validates Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.9, 2.10.
    """
    repos = (
        CANONICAL_INSTALLATION_REPO_A,
        CANONICAL_INSTALLATION_REPO_B,
        CANONICAL_INSTALLATION_REPO_C,
    )
    payload_dict = make_installation_payload(action="created", repositories=repos)
    payload = GithubInstallationPayload.model_validate(payload_dict)

    message = _build_installation_message(
        payload=payload,
        event_type="installation",
        delivery_id=CANONICAL_DELIVERY_ID,
        sent_at=_FIXED_SENT_AT,
    )

    assert message.installation_id == CANONICAL_INSTALLATION_ID
    assert message.github_app_id == CANONICAL_APP_ID
    assert message.event_type == "installation.created"
    assert message.repositories == repos
    assert message.delivery_id == CANONICAL_DELIVERY_ID
    assert message.sent_at == _FIXED_SENT_AT


def test_build_message_installation_deleted_yields_empty_repositories() -> None:
    """``installation.deleted`` forces ``repositories == ()`` regardless of payload contents.

    Requirement 2.6. Even a payload that carries a ``repositories`` list
    is normalised to the empty tuple — the deleted signal is a terminal
    state, no repository set applies.
    """
    payload_dict = make_installation_payload(
        action="deleted",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
    )
    payload = GithubInstallationPayload.model_validate(payload_dict)

    message = _build_installation_message(
        payload=payload,
        event_type="installation",
        delivery_id=CANONICAL_DELIVERY_ID,
        sent_at=_FIXED_SENT_AT,
    )

    assert message.event_type == "installation.deleted"
    assert message.repositories == ()


def test_build_message_installation_repositories_added_maps_repositories_added() -> None:
    """``installation_repositories.added`` reads ``payload.repositories_added`` (Requirement 2.7)."""
    added = (CANONICAL_INSTALLATION_REPO_A, CANONICAL_INSTALLATION_REPO_B)
    payload_dict = make_installation_payload(
        action="added", repositories_added=added
    )
    payload = GithubInstallationPayload.model_validate(payload_dict)

    message = _build_installation_message(
        payload=payload,
        event_type="installation_repositories",
        delivery_id=CANONICAL_DELIVERY_ID,
        sent_at=_FIXED_SENT_AT,
    )

    assert message.event_type == "installation_repositories.added"
    assert message.repositories == added


def test_build_message_installation_repositories_removed_maps_repositories_removed() -> None:
    """``installation_repositories.removed`` reads ``payload.repositories_removed`` (Requirement 2.8)."""
    removed = (CANONICAL_INSTALLATION_REPO_A,)
    payload_dict = make_installation_payload(
        action="removed", repositories_removed=removed
    )
    payload = GithubInstallationPayload.model_validate(payload_dict)

    message = _build_installation_message(
        payload=payload,
        event_type="installation_repositories",
        delivery_id=CANONICAL_DELIVERY_ID,
        sent_at=_FIXED_SENT_AT,
    )

    assert message.event_type == "installation_repositories.removed"
    assert message.repositories == removed


# ===========================================================================
# Group 3 — HTTP status tests (design.md §9.3).
# ===========================================================================


@mock_aws
def test_installation_created_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path — signed ``installation.created`` returns 202 with the delivery id echoed (Requirement 4.6)."""
    _verify_url, _installation_url, _secret_arn, real_sqs = _install_moto_env(
        monkeypatch
    )
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _installation_event(
        event_type="installation",
        action="created",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 202
    body = json.loads(_response_body(response))
    assert body == {"status": "accepted", "delivery_id": CANONICAL_DELIVERY_ID}


@mock_aws
def test_installation_repositories_added_returns_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path variant — signed ``installation_repositories.added`` returns 202."""
    _verify_url, _installation_url, _secret_arn, real_sqs = _install_moto_env(
        monkeypatch
    )
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _installation_event(
        event_type="installation_repositories",
        action="added",
        repositories_added=(CANONICAL_INSTALLATION_REPO_A,),
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 202
    body = json.loads(_response_body(response))
    assert body == {"status": "accepted", "delivery_id": CANONICAL_DELIVERY_ID}


@mock_aws
def test_installation_unknown_action_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``installation`` with an unrecognised action returns 204 and no SQS send (Requirement 6.5, 1.5)."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _installation_event(
        event_type="installation",
        action="suspended",
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 204
    assert spy.calls == []


@mock_aws
def test_malformed_installation_payload_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema violation (``installation.id`` non-int) returns a 400 with ``error`` + ``detail`` (Requirement 6.4, 3.1)."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    payload = make_installation_payload(action="created", repositories=())
    payload["installation"]["id"] = "not-an-int"
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
        "X-GitHub-Event": "installation",
        "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
        "X-Hub-Signature-256": compute_signature(body),
    }
    event = _api_gateway_event(body=body, headers=headers)

    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 400
    parsed = json.loads(_response_body(response))
    assert parsed["error"] == "malformed_installation_payload"
    assert "detail" in parsed
    assert spy.calls == []


@mock_aws
def test_non_json_installation_body_returns_400_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON body for a known installation event returns 400 ``malformed_json`` (Requirement 3.2)."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    body = b"not json"
    headers: dict[str, str] = {
        "X-GitHub-Event": "installation",
        "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
        "X-Hub-Signature-256": compute_signature(body),
    }
    event = _api_gateway_event(body=body, headers=headers)

    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 400
    parsed = json.loads(_response_body(response))
    assert parsed["error"] == "malformed_json"
    assert "detail" in parsed
    assert spy.calls == []


@mock_aws
def test_installation_with_missing_delivery_header_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid installation payload with no ``X-GitHub-Delivery`` returns 400 ``missing_delivery_id`` (Requirement 3.3)."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _installation_event(
        event_type="installation",
        action="created",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
        include_delivery=False,
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 400
    parsed = json.loads(_response_body(response))
    assert parsed["error"] == "missing_delivery_id"
    assert spy.calls == []


@mock_aws
def test_installation_with_invalid_hmac_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid installation payload with a byte-flipped HMAC returns 401 and no SQS send.

    Demonstrates that HMAC verification runs before the Event-Type
    Router evaluates installation events (Requirement 1.6, 6.6).
    """
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _installation_event(
        event_type="installation",
        action="created",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
    )
    # Byte-flip the last hex nibble of the signature so the digest
    # mismatches. The header shape (``sha256=<64 hex>``) stays valid,
    # forcing the verifier down the mismatch branch rather than the
    # missing-header / malformed-header branches.
    sig: str = event["headers"]["X-Hub-Signature-256"]
    last_char = sig[-1]
    flipped_last = "0" if last_char != "0" else "1"
    event["headers"]["X-Hub-Signature-256"] = sig[:-1] + flipped_last

    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 401
    assert spy.calls == []


# ===========================================================================
# Group 4 — Queue-picker isolation tests (design.md §9.4).
# ===========================================================================


@mock_aws
def test_installation_event_invokes_installation_writer_not_verify_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed installation event drives the installation writer, never the verify writer (Requirement 6.7)."""
    _install_moto_env(monkeypatch)

    # Pre-populate both writer singletons with spec-bound MagicMocks so
    # the handler's lazy accessors see ``is not None`` and reuse the
    # mocks instead of constructing real ``SqsWriter`` instances.
    verify_writer_mock = MagicMock(spec=SqsWriter)
    installation_writer_mock = MagicMock(spec=SqsWriter)
    monkeypatch.setattr(handler, "_sqs_writer", verify_writer_mock)
    monkeypatch.setattr(
        handler, "_installation_events_writer", installation_writer_mock
    )

    event, _body = _installation_event(
        event_type="installation",
        action="created",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 202
    installation_writer_mock.send_installation_event.assert_called_once()
    verify_writer_mock.send_job.assert_not_called()


@mock_aws
def test_pull_request_event_invokes_verify_writer_not_installation_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed pull_request event drives the verify writer, never the installation writer (Requirement 6.7 inverse)."""
    _install_moto_env(monkeypatch)

    verify_writer_mock = MagicMock(spec=SqsWriter)
    installation_writer_mock = MagicMock(spec=SqsWriter)
    monkeypatch.setattr(handler, "_sqs_writer", verify_writer_mock)
    monkeypatch.setattr(
        handler, "_installation_events_writer", installation_writer_mock
    )

    payload = make_pull_request_payload(action="opened")
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
        "X-Hub-Signature-256": compute_signature(body),
    }
    event = _api_gateway_event(body=body, headers=headers)

    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 202
    verify_writer_mock.send_job.assert_called_once()
    installation_writer_mock.send_installation_event.assert_not_called()


# ===========================================================================
# Group 5 — ``sent_at`` capture test (design.md §9.5).
# ===========================================================================


@mock_aws
def test_sent_at_captured_at_handler_entry_uses_isoformat_milliseconds_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sent_at`` is captured once at handler entry, ISO-8601 UTC ms, ``Z``-suffixed (Requirement 2.10).

    Monkeypatches ``handler.datetime`` so ``datetime.now(UTC).isoformat(
    timespec="milliseconds")`` yields the fixed string
    ``"2025-01-15T12:34:56.789+00:00"``. The handler's
    ``.replace("+00:00", "Z")`` step must then finish it as
    ``"2025-01-15T12:34:56.789Z"`` on the SQS body — the ``Z`` suffix
    proves the replacement ran and the ``.789`` proves millisecond
    precision. Deliberately uses ``monkeypatch`` rather than
    ``freezegun`` to honour Requirement 7.5 (no new dev deps).
    """
    _install_moto_env(monkeypatch)
    real_sqs = boto3.client("sqs", region_name="us-east-1")
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    fake_datetime = MagicMock()
    fake_datetime.now.return_value.isoformat.return_value = (
        "2025-01-15T12:34:56.789+00:00"
    )
    monkeypatch.setattr(handler, "datetime", fake_datetime)

    event, _body = _installation_event(
        event_type="installation",
        action="created",
        repositories=(CANONICAL_INSTALLATION_REPO_A,),
    )
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 202
    assert len(spy.calls) == 1
    parsed_body: dict[str, Any] = json.loads(spy.calls[0][1])
    assert parsed_body["sent_at"] == "2025-01-15T12:34:56.789Z"
    assert parsed_body["sent_at"].endswith("Z")
    assert ".789" in parsed_body["sent_at"]
