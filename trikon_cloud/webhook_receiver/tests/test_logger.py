# Helper signatures use ``Any`` on ``*args`` to model the arbitrary
# positional-argument tuple ``logging.LogRecord`` accepts. Under the
# repo's ``disallow_any_explicit = true`` mypy config, this surfaces as
# an ``explicit-any`` error. Suppress at file scope — this is a test
# module, not a production module.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.webhook_receiver.logger`.

Encodes the PII-redaction semantics of ``_PiiRedactionFilter`` and the
idempotent-cache contract of ``get_logger``. Together with the coverage
on ``logger.py`` already provided by the powertools-based Logger
instantiation exercised via the handler tests, these tests push the
module's branch coverage past the 80% floor required by design.md §13.2.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from trikon_cloud.webhook_receiver import logger as logger_module
from trikon_cloud.webhook_receiver.logger import _PiiRedactionFilter, get_logger


@pytest.fixture(autouse=True)
def _reset_logger_module_cache() -> None:
    """Reset the module-level ``_LOGGER`` cache so each test starts cold.

    ``get_logger()`` memoizes the constructed powertools Logger; without
    this reset the cache-hit test would pass trivially on any prior
    test's cache side effect.
    """
    logger_module._LOGGER = None


def _record(message: str, *args: Any) -> logging.LogRecord:
    """Build a :class:`logging.LogRecord` with the given message and positional args."""
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args if args else None,
        exc_info=None,
    )


def test_filter_redacts_email_in_message() -> None:
    """The email regex substitution on ``record.msg`` fires and returns ``True``."""
    filt = _PiiRedactionFilter()
    record = _record("user alice@example.com signed in")

    assert filt.filter(record) is True
    assert record.msg == "user <email> signed in"


def test_filter_redacts_pem_and_email_in_args_tuple() -> None:
    """The args-tuple iteration branch (lines 55-60) fires and redacts every str element.

    Non-str positional args pass through untouched — the ``isinstance``
    guard on line 57 excludes ints, dicts, and other non-str types
    from the substitution.
    """
    filt = _PiiRedactionFilter()
    pem_block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "AAAAB3NzaC1yc2EAAAADAQABAAABAQ==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    record = _record(
        "log with %s and %d and %s",
        "email@example.com",  # str — redacted to <email>
        42,  # int — passes through untouched
        pem_block,  # str containing PEM — redacted to <private-key>
    )

    assert filt.filter(record) is True
    assert record.args is not None
    assert isinstance(record.args, tuple)
    assert record.args[0] == "<email>"
    assert record.args[1] == 42  # int untouched by the redaction pass
    assert record.args[2] == "<private-key>"


def test_filter_leaves_non_tuple_args_untouched() -> None:
    """A record whose ``args`` is a dict exits the ``isinstance`` branch cleanly.

    Python's :class:`logging.Logger` supports ``%(name)s``-style
    formatting via a single mapping argument; the caller passes the
    dict wrapped in a 1-tuple, and :class:`~logging.LogRecord.__init__`
    unwraps it so ``record.args`` ends up as the raw mapping. The
    redaction filter only walks tuples — this dict path exercises the
    line 52 False branch and exits without mutation.
    """
    filt = _PiiRedactionFilter()
    # LogRecord unwraps a 1-tuple whose single element is a Mapping,
    # storing the mapping directly on ``self.args`` — this is the
    # standard stdlib idiom (see ``logging.LogRecord.__init__``).
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="dict-args log with %(email)s",
        args=({"email": "alice@example.com"},),
        exc_info=None,
    )
    # Sanity: LogRecord unwrapped the tuple, so ``record.args`` is now
    # the raw dict, exercising the ``isinstance(..., tuple)`` False path.
    assert isinstance(record.args, dict)

    assert filt.filter(record) is True
    # The msg text itself has no email — only the args dict does, which
    # the filter deliberately does not walk (matches the module's
    # declared contract of iterating tuples only).
    assert record.msg == "dict-args log with %(email)s"
    # Args dict passes through unchanged.
    assert record.args == {"email": "alice@example.com"}


def test_get_logger_returns_cached_instance_on_second_call() -> None:
    """The ``_LOGGER is None`` False path on line 74 fires on the second call.

    First call constructs the powertools Logger and caches it on the
    module. Second call short-circuits and returns the same instance —
    ``is`` identity, not merely ``==`` equality.
    """
    first = get_logger()
    second = get_logger()

    assert first is second
