"""Structured logger for the Trikon Cloud installation-lifecycle Lambda.

Wraps :class:`aws_lambda_powertools.Logger` as a module-level singleton
configured with ``service="trikon-cloud-installation-lifecycle"``.
Requirement 18.2 fixes the ``trikon-cloud`` service-name prefix across
every Spec-3 Lambda; Requirement 15.2 pins the natural-key context to
the three-field set ``installation_id``, ``event_type``, ``delivery_id``
— appended via :func:`append_lifecycle_context` once the inbound
:class:`InstallationEventMessage` has validated. Every subsequent log
record within the same warm invocation then carries those keys.

:data:`LOGGING_DENYLIST` mirrors the orchestrator's denylist (design.md
§7.4) but is scoped to the lifecycle handler's smaller IO surface: no
``ecs.RunTask`` response, no App JWT / installation token (the
lifecycle Lambda never mints one). Requirement 15.2 / Invariant 6
forbid these payloads from ever appearing as structured log keys.

As with the orchestrator's logger, we deliberately do NOT install a
Powertools log filter that mutates records — the denylist is a
convention enforced by call-site review plus a static grep-scan guard
in ``test_logger.py`` (design.md §2.2). A filter would give false
confidence that arbitrary log sites are safe; the point of the
denylist is that these payload names never enter a log-call
keyword-argument slot in the first place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger

if TYPE_CHECKING:
    from trikon_cloud.installation_lifecycle.models import InstallationEventMessage

__all__ = ["get_logger", "append_lifecycle_context", "LOGGING_DENYLIST"]  # noqa: RUF022


_LOGGER: Logger = Logger(
    service="trikon-cloud-installation-lifecycle",
    log_uncaught_exceptions=True,
)

LOGGING_DENYLIST: frozenset[str] = frozenset(
    {
        "body",
        "raw_body",
        "sqs_body",
        "app_private_key",
        "aws_credentials",
    }
)


def get_logger() -> Logger:
    """Return the module-level Powertools logger singleton.

    The singleton is constructed at import time with
    ``service="trikon-cloud-installation-lifecycle"`` and
    ``log_uncaught_exceptions=True`` so that unhandled exceptions in
    ``lambda_handler`` are captured as structured ERROR records before
    the Lambda runtime terminates. Every call within the container
    lifetime returns the same instance.
    """
    return _LOGGER


def append_lifecycle_context(
    logger: Logger, *, message: InstallationEventMessage
) -> None:
    """Attach the three natural-key fields to the logger for this invocation.

    Called once by ``lambda_handler`` immediately after the inbound
    :class:`InstallationEventMessage` has validated. Every log record
    emitted for the remainder of the invocation carries these keys,
    satisfying Requirement 15.2's Structured log field set for the
    installation-lifecycle handler.

    Parameters
    ----------
    logger:
        The Powertools :class:`Logger` returned by :func:`get_logger`.
    message:
        The validated inbound lifecycle event message. Field values are
        copied byte-for-byte into the log context — no normalization.
    """
    logger.append_keys(
        installation_id=message.installation_id,
        event_type=message.event_type,
        delivery_id=message.delivery_id,
    )
