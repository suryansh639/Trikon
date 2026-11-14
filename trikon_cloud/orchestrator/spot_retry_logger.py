"""Structured logger for the Trikon Cloud Spot_Retry_Handler Lambda.

Wraps :class:`aws_lambda_powertools.Logger` as a module-level singleton with
``service="trikon-cloud-spot-retry"``, following the ``trikon-cloud`` service
prefix the other Trikon Cloud Lambdas use.

Req 9.8: the Spot_Retry_Handler logs fixed event names with ``task_arn``,
``run_id``, ``reason`` and ``aws_error_code`` only. It never logs the
Task_Stopped_Event, its Runner_Overrides, the Retry_Job body, a pydantic
error, a GitHub response, the App private key, an App JWT or an
installation token.

:data:`LOGGING_DENYLIST` names the fields that must never appear as
structured log keys. It mirrors the orchestrator's fourteen keys and adds
the Spot_Retry_Handler's own payloads (``event``, ``detail``,
``overrides``, ``environment``, ``retry_job`` and ``message_body``). It is
defined here explicitly rather than imported, so this Lambda's contract does
not change when the orchestrator's does.

As in :mod:`trikon_cloud.orchestrator.logger`, no Powertools filter mutates
records. The denylist is a convention enforced by call-site review and a
static grep guard in the tests; a filter would give false confidence that
arbitrary log sites are safe.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

__all__ = ["get_logger", "LOGGING_DENYLIST"]  # noqa: RUF022


_LOGGER: Logger = Logger(
    service="trikon-cloud-spot-retry",
    log_uncaught_exceptions=True,
)

LOGGING_DENYLIST: frozenset[str] = frozenset(
    {
        # Mirrors trikon_cloud.orchestrator.logger.LOGGING_DENYLIST.
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
        "usage_secret",
        "usage_signature",
        "request_body",
        "response_body",
        # Spot_Retry_Handler payloads (cost-cuts Req 9.8).
        "event",
        "detail",
        "overrides",
        "environment",
        "retry_job",
        "message_body",
    }
)


def get_logger() -> Logger:
    """Return the module-level Powertools logger singleton.

    Constructed at import time with ``log_uncaught_exceptions=True`` so an
    unhandled exception in ``lambda_handler`` is captured as a structured
    ERROR record before the Lambda runtime terminates. Every call within the
    container lifetime returns the same instance.
    """
    return _LOGGER
