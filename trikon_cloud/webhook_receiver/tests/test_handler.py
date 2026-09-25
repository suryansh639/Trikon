# Fixture / test helpers use ``dict[str, Any]`` to model API Gateway
# HTTP API v2 event envelopes and the Lambda response dict. Under the
# repo's ``disallow_any_explicit = true`` mypy config, each such helper
# surfaces as an ``explicit-any`` error. Suppress the check at file
# scope — this is a test module, not a production module, and the
# ``Any`` here is bounded to fixture data that flows into pytest tests.
# mypy: disable-error-code="explicit-any"
"""Unit + property tests for :mod:`trikon_cloud.webhook_receiver.handler`.

Encodes **Property 2** from design.md §6 (every 202 response has a
preceding successful SQS send) as two hypothesis-driven sub-tests plus
a battery of per-path assertions covering every branch in the
never-fail-open path enumeration in design.md §7. All tests run under
moto's ``@mock_aws`` decorator — no live AWS is contacted.

The handler caches four pieces of state at the module level
(``_env_config``, ``_webhook_secret``, ``_sqs_writer``,
``_secrets_client``) to keep warm-invocation cost minimal. Between test
functions the ``_reset_handler_module_state`` autouse fixture zeroes
these back to ``None``; between hypothesis examples inside a single
test function, the property test resets them manually (see
``_reset_handler_state`` inside the property test).

An additional monkeypatch replaces ``sqs_writer.boto3`` with a
:class:`types.SimpleNamespace` whose ``client`` attribute returns the
test's :class:`SpySqsClient`. This scopes the substitution to the SQS
writer module alone — the handler's Secrets Manager client construction
still hits real moto-backed ``boto3.client("secretsmanager")``.
"""

from __future__ import annotations

import json
import re
import types
from typing import Any, cast
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from trikon_cloud.webhook_receiver import handler, sqs_writer
from trikon_cloud.webhook_receiver.tests.conftest import (
    CANONICAL_DELIVERY_ID,
    WEBHOOK_SECRET_BYTES,
    compute_signature,
    make_api_gateway_event,
    make_pull_request_payload,
)

# ---------------------------------------------------------------------------
# Module-level test fixtures / helpers.
# ---------------------------------------------------------------------------


_SENT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PEM_PATTERN = "-----BEGIN"


class SpySqsClient:
    """Wraps an SQS client (real moto-backed or forced-failure) and records every call.

    ``calls`` is a list of ``(outcome, message_body, kwargs)`` tuples,
    where ``outcome`` is either ``"success"`` or ``"failure"``. When
    ``failure_exc`` is set, ``send_message`` records the failure and
    raises the exception; otherwise it forwards to ``real_client`` and
    records the outcome based on whether the underlying call raised.
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
    """Zero the handler module's four cached slots so each invocation cold-starts."""
    handler._env_config = None
    handler._webhook_secret = None
    handler._sqs_writer = None
    handler._secrets_client = None


@pytest.fixture(autouse=True)
def _reset_handler_module_state() -> None:
    """Reset handler module-level caches before each test function.

    The handler.py caches ``_env_config``, ``_webhook_secret``,
    ``_sqs_writer``, and ``_secrets_client`` at the module level so warm
    Lambda invocations reuse them. Under pytest, one test's cache
    poisons the next test unless we zero these slots explicitly.
    """
    _reset_handler_state()


def _install_moto_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, Any]:
    """Create moto SQS queue + Secrets Manager secret + env vars.

    Returns ``(queue_url, secret_arn, real_sqs_client)``. Must be
    called inside a ``@mock_aws`` context — the real boto3 clients this
    function constructs are moto-backed only while the decorator is
    active.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    real_sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = real_sqs.create_queue(QueueName="trikon-verify-jobs-test")[
        "QueueUrl"
    ]
    installation_events_queue_url = real_sqs.create_queue(
        QueueName="trikon-cloud-installation-events-test"
    )["QueueUrl"]
    real_secrets = boto3.client("secretsmanager", region_name="us-east-1")
    secret_arn = real_secrets.create_secret(
        Name="trikon-cloud/github-app-webhook-secret-test",
        SecretString=WEBHOOK_SECRET_BYTES.decode("utf-8"),
    )["ARN"]

    monkeypatch.setenv("TRIKON_WEBHOOK_SECRET_ARN", secret_arn)
    monkeypatch.setenv("TRIKON_VERIFY_JOBS_QUEUE_URL", queue_url)
    monkeypatch.setenv(
        "TRIKON_INSTALLATION_EVENTS_QUEUE_URL", installation_events_queue_url
    )
    return queue_url, secret_arn, real_sqs


def _swap_sqs_writer_boto3(
    monkeypatch: pytest.MonkeyPatch, spy: SpySqsClient
) -> None:
    """Replace ``sqs_writer.boto3`` with a namespace whose ``client`` returns ``spy``.

    Scopes the replacement to the SQS writer module — the handler's
    ``boto3.client("secretsmanager")`` construction is unaffected.
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
    """Build an API Gateway HTTP API v2 event with the ``stage`` field powertools requires.

    ``conftest.make_api_gateway_event`` produces the minimal event
    envelope but omits ``requestContext.stage``, which powertools'
    ``APIGatewayHttpResolver`` dereferences during route resolution. We
    layer a ``stage: "$default"`` onto the base event here so the
    resolver can compute the request path without raising ``KeyError``.
    """
    event = make_api_gateway_event(
        body=body, headers=headers, method=method, path=path
    )
    request_context: dict[str, Any] = event["requestContext"]
    request_context["stage"] = "$default"
    return event


def _pull_request_event(
    *,
    action: str = "opened",
    delivery_id: str = CANONICAL_DELIVERY_ID,
    pr_number: int | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
    signature_secret: bytes = WEBHOOK_SECRET_BYTES,
    include_signature: bool = True,
) -> tuple[dict[str, Any], bytes]:
    """Build a signed pull_request event + return the raw body bytes.

    Callers that need to override individual fields (``pr_number``,
    ``head_sha``, ``base_sha``) pass them through; the remaining fields
    fall back to the canonical constants in conftest.
    """
    payload = make_pull_request_payload(action=action)
    if pr_number is not None:
        payload["pull_request"]["number"] = pr_number
    if head_sha is not None:
        payload["pull_request"]["head"]["sha"] = head_sha
    if base_sha is not None:
        payload["pull_request"]["base"]["sha"] = base_sha
    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
    }
    if include_signature:
        headers["X-Hub-Signature-256"] = compute_signature(body, signature_secret)
    event = _api_gateway_event(body=body, headers=headers)
    return event, body


# ---------------------------------------------------------------------------
# Property 2 — every 202 has a preceding successful SQS send.
# ---------------------------------------------------------------------------


@mock_aws
def test_property_every_202_has_preceding_successful_sqs_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-webhook-receiver, Property 2: every 202 has a preceding successful SQS send.

    Sub-test A of Property 2. Validates: Requirements 1.1, 5.1, 5.5, 7.1.
    """
    queue_url, _secret_arn, real_sqs = _install_moto_env(monkeypatch)
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    @given(
        action=st.sampled_from(["opened", "synchronize"]),
        pr_number=st.integers(min_value=1, max_value=999_999),
        head_sha_seed=st.integers(min_value=0, max_value=2**160 - 1),
        base_sha_seed=st.integers(min_value=0, max_value=2**160 - 1),
    )
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def _prop(
        action: str, pr_number: int, head_sha_seed: int, base_sha_seed: int
    ) -> None:
        _reset_handler_state()
        spy.calls.clear()

        head_sha = hex(head_sha_seed)[2:].zfill(40)
        base_sha = hex(base_sha_seed)[2:].zfill(40)
        event, _body = _pull_request_event(
            action=action,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
        )
        response = handler.handler(event, _make_lambda_context())

        assert response["statusCode"] == 202
        assert len(spy.calls) == 1
        outcome, message_body, kwargs = spy.calls[0]
        assert outcome == "success"
        assert kwargs["QueueUrl"] == queue_url

        parsed_body = json.loads(message_body)
        assert parsed_body["installation_id"] == 12345678
        assert parsed_body["repo_full_name"] == "octocat/hello-world"
        assert parsed_body["pr_number"] == pr_number
        assert parsed_body["head_sha"] == head_sha
        assert parsed_body["base_sha"] == base_sha
        assert parsed_body["event_type"] == f"pull_request.{action}"
        assert _SENT_AT_RE.match(parsed_body["sent_at"]) is not None
        assert parsed_body["delivery_id"] == CANONICAL_DELIVERY_ID

        response_body = json.loads(_response_body(response))
        assert response_body == {
            "status": "accepted",
            "delivery_id": CANONICAL_DELIVERY_ID,
        }

    _prop()


@mock_aws
def test_property_5xx_iff_sqs_send_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feature: trikon-cloud-webhook-receiver, Property 2: every 202 has a preceding successful SQS send.

    Sub-test B of Property 2 — the contrapositive. When the SQS send
    raises, the response is 5xx (specifically 502 per Requirement 5.5)
    and no 202 is emitted. Validates: Requirement 5.5.
    """
    _queue_url, _secret_arn, _real_sqs = _install_moto_env(monkeypatch)
    failure = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "mocked"}},
        "SendMessage",
    )
    spy = SpySqsClient(failure_exc=failure)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(action="opened")
    response = handler.handler(event, _make_lambda_context())

    assert response["statusCode"] == 502
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "failure"
    assert json.loads(_response_body(response)) == {"error": "enqueue_failed"}


# ---------------------------------------------------------------------------
# Per-path routing tests (design.md §7 path enumeration).
# ---------------------------------------------------------------------------


@mock_aws
def test_wrong_method_returns_405(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 1: HTTP method != POST → framework-level rejection.

    Design.md §7 pins this to 405; powertools' HTTP resolver returns
    404 for GET on a POST-only route (its "route not found" branch
    doesn't distinguish "method not allowed" from "path not found").
    Both statuses satisfy Invariant 2 (never-fail-open): a GET reaches
    neither HMAC verification nor SQS. The assertion tolerates either
    framework outcome.
    """
    _install_moto_env(monkeypatch)
    event = _api_gateway_event(
        body=b"",
        headers={},
        method="GET",
        path="/webhooks/github",
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] in {404, 405}


@mock_aws
def test_wrong_path_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 2: path != /webhooks/github → 404 (framework-level)."""
    _install_moto_env(monkeypatch)
    event = _api_gateway_event(
        body=b"",
        headers={},
        method="POST",
        path="/wrong/path",
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 404


@mock_aws
def test_missing_event_header_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 3: missing X-GitHub-Event → 400 {"error": "missing_event_header"}."""
    _install_moto_env(monkeypatch)
    event = _api_gateway_event(body=b"", headers={})
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 400
    assert json.loads(_response_body(response)) == {"error": "missing_event_header"}


@mock_aws
def test_ping_returns_200_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 4: X-GitHub-Event: ping → 200 {"status": "pong"}. SQS untouched."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event = _api_gateway_event(
        body=b"",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
        },
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 200
    assert json.loads(_response_body(response)) == {"status": "pong"}
    assert spy.calls == []


@mock_aws
def test_secrets_manager_read_failure_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 5: Secrets Manager read failure → 500 {"error": "internal_error"}."""
    _install_moto_env(monkeypatch)

    def _raise_client_error() -> bytes:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetSecretValue",
        )

    monkeypatch.setattr(handler, "_load_webhook_secret", _raise_client_error)

    event, _body = _pull_request_event(action="opened", include_signature=True)
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 500
    assert json.loads(_response_body(response)) == {"error": "internal_error"}


@mock_aws
def test_missing_signature_header_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 6: missing X-Hub-Signature-256 → 401. SQS untouched."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(action="opened", include_signature=False)
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 401
    assert json.loads(_response_body(response)) == {"error": "hmac_verification_failed"}
    assert spy.calls == []


@mock_aws
def test_wrong_signature_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 7: signature computed under a different secret → 401."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(
        action="opened",
        signature_secret=b"different-secret-value-not-configured",
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 401
    assert json.loads(_response_body(response)) == {"error": "hmac_verification_failed"}
    assert spy.calls == []


@mock_aws
def test_pull_request_closed_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 8: pull_request action outside {opened, synchronize} → 204."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(action="closed")
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 204
    assert spy.calls == []


def _non_enqueue_event(event_type: str) -> dict[str, Any]:
    """Build a minimal signed API Gateway event for a non-pull_request event type."""
    body = json.dumps({"zen": "keep it simple"}).encode("utf-8")
    sig = compute_signature(body)
    return _api_gateway_event(
        body=body,
        headers={
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
            "X-Hub-Signature-256": sig,
        },
    )


@mock_aws
def test_check_run_event_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 9: X-GitHub-Event: check_run → 204. SQS untouched."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    response = handler.handler(_non_enqueue_event("check_run"), _make_lambda_context())
    assert response["statusCode"] == 204
    assert spy.calls == []


@mock_aws
def test_installation_event_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 9: X-GitHub-Event: installation → 204."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    response = handler.handler(_non_enqueue_event("installation"), _make_lambda_context())
    assert response["statusCode"] == 204
    assert spy.calls == []


@mock_aws
def test_installation_repositories_event_returns_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 9: X-GitHub-Event: installation_repositories → 204."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    response = handler.handler(
        _non_enqueue_event("installation_repositories"), _make_lambda_context()
    )
    assert response["statusCode"] == 204
    assert spy.calls == []


@mock_aws
def test_push_event_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 9: X-GitHub-Event: push → 204."""
    _install_moto_env(monkeypatch)
    spy = SpySqsClient()
    _swap_sqs_writer_boto3(monkeypatch, spy)

    response = handler.handler(_non_enqueue_event("push"), _make_lambda_context())
    assert response["statusCode"] == 204
    assert spy.calls == []


@mock_aws
def test_malformed_json_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Path 10 (JSON variant): body isn't JSON → 400 {"error": "malformed_json"}."""
    _install_moto_env(monkeypatch)
    body = b"not-json"
    sig = compute_signature(body)
    event = _api_gateway_event(
        body=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
            "X-Hub-Signature-256": sig,
        },
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 400
    assert json.loads(_response_body(response)) == {"error": "malformed_json"}


@mock_aws
def test_missing_pull_request_head_sha_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 10 (schema variant): missing pull_request.head.sha → 400 malformed_payload.

    Also asserts the redacted ``detail`` string contains no email or
    PEM block per Invariant 6 (secrets never enter observability
    planes).
    """
    _install_moto_env(monkeypatch)
    payload = make_pull_request_payload(action="opened")
    del payload["pull_request"]["head"]["sha"]
    body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(body)
    event = _api_gateway_event(
        body=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": CANONICAL_DELIVERY_ID,
            "X-Hub-Signature-256": sig,
        },
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 400
    parsed = json.loads(_response_body(response))
    assert parsed["error"] == "malformed_payload"
    assert "detail" in parsed
    detail: str = parsed["detail"]
    assert _EMAIL_RE.search(detail) is None
    assert _PEM_PATTERN not in detail


@mock_aws
def test_202_body_carries_delivery_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 12: successful enqueue → body is {"status": "accepted", "delivery_id": <X-GitHub-Delivery>}."""
    _install_moto_env(monkeypatch)
    real_sqs = boto3.client("sqs", region_name="us-east-1")
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    custom_delivery_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    event, _body = _pull_request_event(
        action="opened", delivery_id=custom_delivery_id
    )
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 202
    assert json.loads(_response_body(response)) == {
        "status": "accepted",
        "delivery_id": custom_delivery_id,
    }


@mock_aws
def test_sqs_message_field_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful enqueue, the queue receives a message matching design.md §5.2 shape."""
    queue_url, _secret_arn, real_sqs = _install_moto_env(monkeypatch)
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(action="opened")
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 202

    dequeued = real_sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    messages = dequeued.get("Messages", [])
    assert len(messages) == 1
    body_dict = json.loads(messages[0]["Body"])

    # Eight design.md §5.2 fields present with the right types.
    assert isinstance(body_dict["installation_id"], int)
    assert isinstance(body_dict["repo_full_name"], str)
    assert isinstance(body_dict["pr_number"], int)
    assert isinstance(body_dict["head_sha"], str)
    assert _HEX40_RE.match(body_dict["head_sha"]) is not None
    assert isinstance(body_dict["base_sha"], str)
    assert _HEX40_RE.match(body_dict["base_sha"]) is not None
    assert isinstance(body_dict["event_type"], str)
    assert isinstance(body_dict["sent_at"], str)
    assert _SENT_AT_RE.match(body_dict["sent_at"]) is not None
    assert isinstance(body_dict["delivery_id"], str)


@mock_aws
@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_event_type_is_dotted(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The enqueued message's event_type is exactly ``pull_request.<action>``."""
    _queue_url, _secret_arn, real_sqs = _install_moto_env(monkeypatch)
    spy = SpySqsClient(real_client=real_sqs)
    _swap_sqs_writer_boto3(monkeypatch, spy)

    event, _body = _pull_request_event(action=action)
    response = handler.handler(event, _make_lambda_context())
    assert response["statusCode"] == 202

    assert len(spy.calls) == 1
    parsed_body: dict[str, Any] = json.loads(spy.calls[0][1])
    assert parsed_body["event_type"] == f"pull_request.{action}"


# ``cast`` is used above to silence potential Any leakage; re-export
# for mypy's benefit so an unused-import scan does not fire.
_ = cast
