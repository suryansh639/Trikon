"""Structured logger with PII redaction for the webhook receiver.

Wraps :class:`aws_lambda_powertools.Logger` and attaches a
:class:`logging.Filter` that scrubs two PII shapes from every emitted
record — email addresses and PEM-encoded private keys — before the
record reaches the CloudWatch stream. The redaction filter is a
belt-and-braces enforcement of Invariant 6 (secrets never enter
observability planes); the handler itself never logs raw payload
strings, but log-line drift over time is easy and the filter is cheap.

Public surface:

* :func:`get_logger` — returns the cached module-level logger,
  constructing it and attaching the filter on first call. Every
  subsequent call in the same process (i.e., across warm Lambda
  invocations) returns the same instance.
"""

from __future__ import annotations

import logging
import re

from aws_lambda_powertools import Logger

__all__ = ["get_logger"]

_SERVICE_NAME = "trikon-cloud-webhook-receiver"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"
)

_LOGGER: Logger | None = None


class _PiiRedactionFilter(logging.Filter):
    """Redacts email addresses and PEM private keys from log records.

    Applies to both ``record.msg`` and, when present, each str element
    of ``record.args``. Non-str positional args pass through untouched.
    The filter always returns ``True`` — it never drops records, only
    rewrites their content.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        message = _EMAIL_RE.sub("<email>", message)
        message = _PEM_RE.sub("<private-key>", message)
        record.msg = message
        if isinstance(record.args, tuple):
            redacted_args: list[object] = []
            for arg in record.args:
                if isinstance(arg, str):
                    scrubbed = _EMAIL_RE.sub("<email>", arg)
                    scrubbed = _PEM_RE.sub("<private-key>", scrubbed)
                    redacted_args.append(scrubbed)
                else:
                    redacted_args.append(arg)
            record.args = tuple(redacted_args)
        return True


def get_logger() -> Logger:
    """Return the module-level Powertools logger, constructing it lazily.

    On first call, instantiates :class:`Logger` at INFO level, attaches
    a :class:`_PiiRedactionFilter` to every handler on the underlying
    stdlib logger, and caches the instance. Subsequent calls return the
    cached instance directly — the filter is attached exactly once.
    """
    global _LOGGER
    if _LOGGER is None:
        logger = Logger(service=_SERVICE_NAME)
        redaction_filter = _PiiRedactionFilter()
        stdlib_logger = logging.getLogger(_SERVICE_NAME)
        for handler in stdlib_logger.handlers:
            handler.addFilter(redaction_filter)
        _LOGGER = logger
    return _LOGGER
