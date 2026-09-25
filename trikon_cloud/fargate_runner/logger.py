# Structlog's processor signature dictates ``event_dict: dict[str, Any]`` — the
# processor chain passes arbitrary user-supplied kwargs through the dict, and
# there is no upstream type discipline for their values. Under the repo's
# ``disallow_any_explicit = true`` mypy config the annotation surfaces as an
# ``explicit-any`` error. Silence it at the file level — the explicit ``Any``
# is required by the third-party protocol.
# mypy: disable-error-code="explicit-any"
"""Structlog-based JSON logger with PII redaction (design.md §6).

Every log record is a single-line JSON object emitted to stdout. The
processor chain is:

1. :func:`structlog.contextvars.merge_contextvars` — injects the
   request-scoped fields bound via
   :func:`structlog.contextvars.bind_contextvars` at the top of
   ``entrypoint.main()`` (delivery_id, installation_id, repo_full_name,
   pr_number, head_sha).
2. :func:`structlog.processors.add_log_level` — adds ``"level"``.
3. :func:`structlog.processors.TimeStamper` — adds ``"timestamp"`` in
   ISO-8601 UTC.
4. :func:`_redact_pii_processor` — the load-bearing filter. Runs three
   regex substitutions on every str value in the event-dict, enforcing
   Invariant 6 (secrets never enter observability planes).
5. :func:`structlog.processors.JSONRenderer` — final serialization.

Public surface:

* :func:`configure_logging` — configure the global structlog pipeline.
  Idempotent — safe to call multiple times (structlog itself dedupes).
* :func:`get_logger` — return a bound logger with the given name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

__all__ = ["configure_logging", "get_logger"]


# ---------------------------------------------------------------------------
# PII redaction patterns (design.md §6, processor 4).
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"
)
_ACCESS_TOKEN_RE = re.compile(r"x-access-token:[^@\s]+@")


def _redact_pii_processor(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor that scrubs PII patterns from event-dict values.

    Walks ``event_dict`` and applies three sequential regex
    substitutions to every ``str`` value:

    * :data:`_ACCESS_TOKEN_RE` → ``x-access-token:<redacted>@``
    * :data:`_PEM_RE` → ``<private-key>``
    * :data:`_EMAIL_RE` → ``<email>``

    Ordering matters. The access-token pattern is a strict superset of
    the email pattern's shape when applied to an auth URL like
    ``x-access-token:ghs_abc123def456@github.com/foo/bar.git`` — the
    email regex would otherwise fire on the ``def456@github.com``
    substring first and rewrite the token with the wrong sentinel
    (``<email>`` instead of ``<redacted>``). Redact the most specific
    pattern (access-token) first, then PEM blocks, then bare emails.
    Non-string values (``int``, ``bool``, ``list``, nested ``dict``)
    pass through untouched — nested structures are the caller's
    responsibility. The processor never drops records, only rewrites
    their content.

    The ``logger`` and ``method_name`` parameters are part of the
    structlog processor protocol; the concrete instance and log-level
    name are unused by this filter. ``logger`` is typed :data:`~typing.Any`
    to match structlog's ``Processor`` protocol (the bound-logger type
    is dynamic at the processor boundary).
    """
    del logger, method_name
    for key, value in event_dict.items():
        if isinstance(value, str):
            # Order matters: the access-token pattern is more specific
            # than the email pattern, so redact it first to prevent the
            # email regex from partial-matching inside an auth URL like
            # ``x-access-token:ghs_abc@github.com/...``.
            scrubbed = _ACCESS_TOKEN_RE.sub("x-access-token:<redacted>@", value)
            scrubbed = _PEM_RE.sub("<private-key>", scrubbed)
            scrubbed = _EMAIL_RE.sub("<email>", scrubbed)
            event_dict[key] = scrubbed
    return event_dict


def configure_logging(*, log_level: str = "INFO") -> None:
    """Configure the global structlog pipeline (design.md §6).

    Idempotent — structlog's :func:`~structlog.configure` replaces the
    prior configuration on repeat calls, so calling this multiple
    times in the same process is safe.

    ``log_level`` is matched case-insensitively against the stdlib
    :mod:`logging` module attribute names; an unknown level falls back
    to :data:`logging.INFO` rather than raising.
    """
    level_value = getattr(logging, log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            _redact_pii_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_value),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger tagged with ``name``.

    The returned logger inherits the processor chain configured by
    :func:`configure_logging`. Call sites conventionally use the
    module ``__name__`` as ``name`` so log records carry the source
    module in the ``logger`` field.

    :func:`structlog.get_logger` returns a lazy proxy typed
    :data:`~typing.Any` in the upstream stubs — cast to the concrete
    :class:`~structlog.stdlib.BoundLogger` so call sites get precise
    method typing.
    """
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
