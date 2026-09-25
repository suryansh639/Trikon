"""SQS writer for the GitHub webhook receiver.

Wraps :func:`boto3.client("sqs").send_message` in a small, typed shim so
the handler can express its single side effect — an ``SqsJobMessage``
lands on the ``trikon-verify-jobs`` queue — without leaking boto3 types
into the composition layer. Two public symbols:

* :class:`SqsWriter` — instantiated once per Lambda cold start; caches
  the underlying boto3 client on ``self`` so warm invocations reuse it.
* :class:`SqsWriteError` — raised when
  :meth:`boto3.client.send_message` fails after adaptive-mode retries.
  Wraps the underlying ``botocore`` exception via ``raise ... from``.

Serialization uses :meth:`pydantic.BaseModel.model_dump_json` — never
``json.dumps(msg.model_dump())`` — because Requirement 5.3 pins the SQS
message body's field order to the ``SqsJobMessage`` field declaration
order verbatim. ``model_dump_json`` preserves declaration order in
pydantic v2; the two-step form does not.

The queue is a standard queue (memo §3.3), so the writer intentionally
does not set ``MessageDeduplicationId`` or ``MessageGroupId``
(Requirement 5.6) — idempotency is delegated to downstream consumers.
"""

from __future__ import annotations

from typing import Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from trikon_cloud.installation_lifecycle.models import InstallationEventMessage
from trikon_cloud.webhook_receiver.models import SqsJobMessage

# Ordering places the class before its exception — deliberate, not alphabetical.
__all__ = ["SqsWriter", "SqsWriteError"]  # noqa: RUF022


class SqsWriteError(Exception):
    """Raised when sqs.SendMessage fails after all retries."""


class _SqsClient(Protocol):
    """Structural type for the subset of the boto3 SQS client we call.

    ``boto3`` ships without type stubs; this protocol lets ``mypy
    --strict`` type-check the single ``send_message`` call site without
    a runtime dependency on ``boto3-stubs``.
    """

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        ...


class SqsWriter:
    """Writes ``SqsJobMessage`` values to a single SQS queue.

    The class caches its boto3 client on ``self`` — construct once per
    Lambda cold start and reuse across warm invocations to avoid the
    per-call TLS handshake cost.

    Args:
        queue_url: Target queue URL (typically supplied via
            ``ReceiverEnvConfig.verify_jobs_queue_url``).
        boto3_client: Optional dependency-injection seam for tests. When
            ``None`` (the production path), the class lazily constructs
            a boto3 SQS client configured with adaptive-mode retries
            capped at three attempts.
    """

    def __init__(self, *, queue_url: str, boto3_client: object | None = None) -> None:
        self._queue_url: str = queue_url
        self._client: _SqsClient | None = (
            cast(_SqsClient, boto3_client) if boto3_client is not None else None
        )

    def _get_client(self) -> _SqsClient:
        """Return the cached SQS client, constructing it on first use."""
        if self._client is None:
            retry_config = Config(retries={"mode": "adaptive", "max_attempts": 3})
            self._client = cast(_SqsClient, boto3.client("sqs", config=retry_config))
        return self._client

    def _send_body(self, body: str) -> None:
        """Dispatch a pre-serialized ``body`` to the configured SQS queue.

        Shared low-level send path for :meth:`send_job` and
        :meth:`send_installation_event`. Raises :class:`SqsWriteError`
        on any ``botocore`` failure surface, preserving the original
        exception via ``__cause__``.
        """
        client = self._get_client()
        try:
            client.send_message(QueueUrl=self._queue_url, MessageBody=body)
        except (ClientError, BotoCoreError) as exc:
            raise SqsWriteError(
                f"sqs.SendMessage failed for queue {self._queue_url}: {exc}"
            ) from exc

    def send_job(self, message: SqsJobMessage) -> None:
        """Write ``message`` to the configured SQS queue.

        Serializes via :meth:`SqsJobMessage.model_dump_json` (Requirement
        5.3 — field order is significant on the wire). Raises
        :class:`SqsWriteError` on any ``botocore`` failure surface,
        preserving the original exception via ``__cause__``.
        """
        self._send_body(message.model_dump_json())

    def send_installation_event(self, message: InstallationEventMessage) -> None:
        """Write an ``InstallationEventMessage`` to the configured SQS queue.

        Serializes via :meth:`InstallationEventMessage.model_dump_json`
        so the wire body matches the model's declared field order — the
        Lifecycle_Handler consumer parses with ``extra="forbid"``, so a
        shape drift here would fail loudly downstream. Raises
        :class:`SqsWriteError` on any ``botocore`` failure surface,
        preserving the original exception via ``__cause__``.
        """
        self._send_body(message.model_dump_json())
