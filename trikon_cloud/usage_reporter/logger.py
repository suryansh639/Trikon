"""Structured logger for the Trikon Cloud Usage_Reporter Lambda.

Wraps :class:`aws_lambda_powertools.Logger` as a module-level singleton with
``service="trikon-cloud-usage-reporter"``, following the ``trikon-cloud``
service prefix the other Trikon Cloud Lambdas use.

Req 12.15: the Usage_Reporter logs fixed event names, Run_Ids, task ARNs, and
status codes only. It never logs the Usage_Secret, a Usage_Signature, or a
request or response body.

:data:`LOGGING_DENYLIST` names the fields that must never appear as structured
log keys: the orchestrator's ten keys (raw SQS bodies, the ``ecs.RunTask``
response body, and credential material) plus the four Usage_API keys
``usage_secret``, ``usage_signature``, ``request_body``, and ``response_body``.
It is defined here explicitly rather than imported, so this package's contract
does not change when the orchestrator's does.

As in :mod:`trikon_cloud.orchestrator.logger`, no Powertools filter mutates
records. The denylist is a convention enforced by call-site review and a static
grep guard in ``tests/test_logger.py``; a filter would give false confidence
that arbitrary log sites are safe.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

__all__ = ["get_logger", "LOGGING_DENYLIST"]  # noqa: RUF022


_LOGGER: Logger = Logger(
    service="trikon-cloud-usage-reporter",
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
        # Usage_API material (Req 12.15).
        "usage_secret",
        "usage_signature",
        "request_body",
        "response_body",
    }
)


def get_logger() -> Logger:
    """Return the module-level Powertools logger singleton.

    Constructed at import time with ``log_uncaught_exceptions=True`` so an
    unhandled exception in ``lambda_handler`` is captured as a structured ERROR
    record before the Lambda runtime terminates. Every call within the
    container lifetime returns the same instance.
    """
    return _LOGGER
