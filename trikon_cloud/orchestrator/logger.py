"""Structured logger for the Trikon Cloud orchestrator Lambda.

Wraps :class:`aws_lambda_powertools.Logger` as a module-level singleton
configured with ``service="trikon-cloud-orchestrator"``. Requirement 9.1
mandates every log emission from the orchestrator carry that ``service``
field, and Requirement 18.2 fixes the ``trikon-cloud`` prefix on the
Powertools service name across all Spec-3 Lambdas.

Requirement 9.2 pins the natural-key context to a fixed five-field set —
``installation_id``, ``repo_full_name``, ``pr_number``, ``delivery_id``,
``event_type`` — appended via :func:`append_job_context` once the
inbound :class:`SqsJobMessage` has validated. Every subsequent log
record within the same warm invocation carries those keys.

:data:`LOGGING_DENYLIST` (design.md §7.4) enumerates the ten field names
that must never appear as structured log keys — raw SQS bodies, the
``ecs.RunTask`` response body, and any credential material. Requirement
9.3 / Invariant 6 forbid emitting those payloads at any level.

Note that we deliberately do NOT install a Powertools log filter that
mutates records: the denylist is a *convention* enforced by call-site
review plus a static grep-scan guard in ``test_logger.py`` (design.md
§2.2). A filter would give false confidence that arbitrary log sites
are safe — the whole point of the denylist is that these payload names
never enter a log-call keyword-argument slot in the first place.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

from trikon_cloud.webhook_receiver.models import SqsJobMessage

__all__ = ["get_logger", "append_job_context", "LOGGING_DENYLIST"]  # noqa: RUF022


_LOGGER: Logger = Logger(
    service="trikon-cloud-orchestrator",
    log_uncaught_exceptions=True,
)

LOGGING_DENYLIST: frozenset[str] = frozenset(
    {
        "body",
        "raw_body",
        "sqs_body",
        "response",
        "ecs_response",
        "secret_value",
        "app_private_key",
        "installation_token",
        "app_jwt",
        "aws_credentials",
    }
)


def get_logger() -> Logger:
    """Return the module-level Powertools logger singleton.

    The singleton is constructed at import time with
    ``service="trikon-cloud-orchestrator"`` and
    ``log_uncaught_exceptions=True`` so that unhandled exceptions in
    ``lambda_handler`` are captured as structured ERROR records before
    the Lambda runtime terminates. Every call within the container
    lifetime returns the same instance — Requirement 9.1.
    """
    return _LOGGER


def append_job_context(logger: Logger, *, message: SqsJobMessage) -> None:
    """Attach the five natural-key fields to the logger for this invocation.

    Called once by ``lambda_handler`` immediately after the inbound
    :class:`SqsJobMessage` has validated. Every log record emitted for
    the remainder of the invocation carries these keys, satisfying
    Requirement 9.2's Structured log field set.

    Parameters
    ----------
    logger:
        The Powertools :class:`Logger` returned by :func:`get_logger`.
    message:
        The validated inbound job message. Field values are copied
        byte-for-byte into the log context — no normalization.
    """
    logger.append_keys(
        installation_id=message.installation_id,
        repo_full_name=message.repo_full_name,
        pr_number=message.pr_number,
        delivery_id=message.delivery_id,
        event_type=message.event_type,
    )
