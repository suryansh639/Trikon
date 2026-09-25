# Fixture / test helpers use ``dict[str, Any]`` and ``Any`` for the
# captured send_message kwargs — the boto3 API surface is dynamically
# typed and the spy accumulates arbitrary keyword arguments. Under the
# repo's ``disallow_any_explicit = true`` mypy config, this surfaces as
# an ``explicit-any`` error. Suppress at file scope — this is a test
# module, not a production module.
# mypy: disable-error-code="explicit-any"
"""Moto-backed unit tests for :class:`trikon_cloud.webhook_receiver.sqs_writer.SqsWriter`.

Covers six assertions the sqs_writer's contract must uphold:

1. The message body deserialises to the expected ``model_dump(mode="json")`` dict.
2. Serialization uses :meth:`SqsJobMessage.model_dump_json` byte-identically —
   Requirement 5.3 pins field order on the wire, and the two-step
   ``json.dumps(model.model_dump())`` form does not preserve order under
   pydantic v2.
3. ``send_message`` is called without ``MessageDeduplicationId`` or
   ``MessageGroupId`` — Requirement 5.6 (the queue is a standard queue,
   FIFO-only keys must not appear on it).
4. A ``ClientError`` from boto3 is wrapped in :class:`SqsWriteError` with
   the original exception preserved on ``__cause__``.
5. A ``BotoCoreError`` (e.g., ``EndpointConnectionError``) is likewise
   wrapped in :class:`SqsWriteError` with the original on ``__cause__``.
6. A single :class:`SqsWriter` instance caches the underlying boto3
   client across ``send_job`` invocations — the writer must not
   construct a fresh client per send.
"""

from __future__ import annotations

import json
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.webhook_receiver import sqs_writer
from trikon_cloud.webhook_receiver.models import SqsJobMessage
from trikon_cloud.webhook_receiver.sqs_writer import SqsWriteError, SqsWriter
from trikon_cloud.webhook_receiver.tests.conftest import (
    CANONICAL_BASE_SHA,
    CANONICAL_DELIVERY_ID,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _canonical_message(**overrides: Any) -> SqsJobMessage:
    """Return a canonical :class:`SqsJobMessage` matching design.md §5.2."""
    fields: dict[str, Any] = {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "repo_full_name": CANONICAL_REPO_FULL_NAME,
        "pr_number": CANONICAL_PR_NUMBER,
        "head_sha": CANONICAL_HEAD_SHA,
        "base_sha": CANONICAL_BASE_SHA,
        "event_type": "pull_request.opened",
        "sent_at": "2024-01-15T10:00:00.000Z",
        "delivery_id": CANONICAL_DELIVERY_ID,
    }
    fields.update(overrides)
    return SqsJobMessage(**fields)


class _CapturingSqsClient:
    """Records every ``send_message`` call and optionally raises a configured exception."""

    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise: BaseException | None = raise_exc

    def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return {"MessageId": "spy-message-id"}


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@mock_aws
def test_send_job_writes_expected_body_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivered SQS message body deserialises to ``msg.model_dump(mode="json")``."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="trikon-verify-jobs-test")["QueueUrl"]

    msg = _canonical_message()
    writer = SqsWriter(queue_url=queue_url)
    writer.send_job(msg)

    received = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    messages = received.get("Messages", [])
    assert len(messages) == 1
    body_dict = json.loads(messages[0]["Body"])
    assert body_dict == msg.model_dump(mode="json")


def test_send_job_uses_model_dump_json_not_json_dumps_model_dump() -> None:
    """The MessageBody is byte-identical to ``msg.model_dump_json()`` (field order preserved)."""
    spy = _CapturingSqsClient()
    writer = SqsWriter(queue_url="https://sqs.example/q", boto3_client=spy)
    msg = _canonical_message()

    writer.send_job(msg)

    assert len(spy.calls) == 1
    assert spy.calls[0]["MessageBody"] == msg.model_dump_json()


def test_send_job_does_not_set_dedup_or_group_id() -> None:
    """The writer must not set ``MessageDeduplicationId`` / ``MessageGroupId`` (Requirement 5.6)."""
    spy = _CapturingSqsClient()
    writer = SqsWriter(queue_url="https://sqs.example/q", boto3_client=spy)

    writer.send_job(_canonical_message())

    assert len(spy.calls) == 1
    kwargs = spy.calls[0]
    assert "MessageDeduplicationId" not in kwargs
    assert "MessageGroupId" not in kwargs


def test_send_job_raises_sqs_write_error_on_client_error() -> None:
    """A ``ClientError`` from boto3 is wrapped in :class:`SqsWriteError` with cause preserved."""
    client_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "mocked"}},
        "SendMessage",
    )
    spy = _CapturingSqsClient(raise_exc=client_error)
    writer = SqsWriter(queue_url="https://sqs.example/q", boto3_client=spy)

    with pytest.raises(SqsWriteError) as exc_info:
        writer.send_job(_canonical_message())

    assert exc_info.value.__cause__ is client_error


def test_send_job_raises_sqs_write_error_on_boto_core_error() -> None:
    """A ``BotoCoreError`` (``EndpointConnectionError``) is wrapped identically."""
    boto_error = EndpointConnectionError(endpoint_url="mocked")
    spy = _CapturingSqsClient(raise_exc=boto_error)
    writer = SqsWriter(queue_url="https://sqs.example/q", boto3_client=spy)

    with pytest.raises(SqsWriteError) as exc_info:
        writer.send_job(_canonical_message())

    assert exc_info.value.__cause__ is boto_error


@mock_aws
def test_send_job_reuses_boto3_client_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single writer instance caches its boto3 client across ``send_job`` calls."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="trikon-verify-jobs-test")["QueueUrl"]

    # Count boto3.client("sqs", ...) invocations to guarantee the writer
    # constructs exactly one client instance over two send_job calls.
    # ``sqs_writer.boto3`` is the imported boto3 module. Its own
    # ``__all__`` doesn't enumerate ``boto3`` (only the two public
    # symbols the module exports), so mypy's ``attr-defined`` check
    # fires — silence it with a targeted ignore since the attribute
    # does exist at runtime.
    boto3_module: Any = sqs_writer.boto3  # type: ignore[attr-defined]
    real_client_factory = boto3_module.client
    invocation_count = {"n": 0}

    def _counting_client(*args: Any, **kwargs: Any) -> Any:
        invocation_count["n"] += 1
        return real_client_factory(*args, **kwargs)

    monkeypatch.setattr(boto3_module, "client", _counting_client)

    writer = SqsWriter(queue_url=queue_url)
    writer.send_job(_canonical_message())
    client_after_first = writer._client
    writer.send_job(_canonical_message(delivery_id="second-delivery-id"))
    client_after_second = writer._client

    assert client_after_first is client_after_second
    assert id(client_after_first) == id(client_after_second)
    assert invocation_count["n"] == 1
