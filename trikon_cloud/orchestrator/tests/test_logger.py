"""Unit tests for :mod:`trikon_cloud.orchestrator.logger`.

Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

Covers:

* :func:`get_logger` returns a module-level singleton — Requirement 9.1.
* :func:`append_job_context` propagates the five natural-key fields
  (``installation_id``, ``repo_full_name``, ``pr_number``,
  ``delivery_id``, ``event_type``) onto every subsequent log record —
  Requirement 9.2, design.md §10.4 Property 12.
* :data:`LOGGING_DENYLIST` contains the ten expected payload / credential
  field names — Requirement 9.3, design.md §7.4.
* Static grep-scan guard: no call site under
  ``trikon_cloud/orchestrator/`` uses a denylist name as a
  logger-call keyword argument — catches accidental
  ``logger.error("...", body=raw_body)`` regressions at test time
  (Requirement 9.3).

The stream-swap fixture (:func:`log_stream`) rebinds the Powertools
logger's :class:`logging.StreamHandler` to a fresh :class:`io.StringIO`
for the duration of a test so log records are captured deterministically
regardless of pytest's own stdout capture mode. The original stream is
restored after the test.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

from trikon_cloud.orchestrator.logger import (
    LOGGING_DENYLIST,
    append_job_context,
    get_logger,
)
from trikon_cloud.orchestrator.tests.conftest import make_sqs_job_message

# The ten field names Requirement 9.3 forbids as structured log keys.
# Held here as a plain frozenset so the equality assertion in
# :func:`test_logging_denylist_contains_ten_expected_keys` fails loudly
# if either side drifts — the test is a two-way contract between this
# file and :mod:`trikon_cloud.orchestrator.logger`.
_EXPECTED_DENYLIST: frozenset[str] = frozenset(
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


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's stream to a :class:`io.StringIO`.

    Powertools' :class:`Logger` wraps a stdlib :class:`logging.Logger`
    registered under the service name ``"trikon-cloud-orchestrator"``
    (Requirement 9.1). A :class:`logging.StreamHandler` is attached at
    Logger construction time with a JSON formatter and
    ``stream=sys.stdout``. Swapping the handler's stream lets tests
    capture the exact JSON emitted for a log call independent of
    pytest's own stdout-capture mode.

    The original streams are restored on teardown so tests that follow
    this one in the same session see the handler configured as at
    import time.
    """
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-orchestrator")
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            original_streams.append((handler, handler.stream))
            handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in original_streams:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


def test_get_logger_returns_singleton() -> None:
    """:func:`get_logger` returns the same :class:`Logger` instance on every call.

    Requirement 9.1 pins the module-level singleton — the handler,
    context stack, and JSON formatter must survive across warm Lambda
    invocations. Two calls returning distinct instances would double
    every log record and drop appended context on each cold-start
    re-import.
    """
    first = get_logger()
    second = get_logger()
    assert first is second


def test_append_job_context_propagates_five_natural_keys(
    log_stream: io.StringIO,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    After :func:`append_job_context`, every log record carries the five
    natural-key fields from the source :class:`SqsJobMessage`
    byte-identical to the input — Requirement 9.2.
    """
    logger = get_logger()
    message = make_sqs_job_message()

    append_job_context(logger, message=message)
    logger.info("run_task_dispatched")

    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one log record, got {len(lines)}"
    record = json.loads(lines[0])
    assert record["installation_id"] == message.installation_id
    assert record["repo_full_name"] == message.repo_full_name
    assert record["pr_number"] == message.pr_number
    assert record["delivery_id"] == message.delivery_id
    assert record["event_type"] == message.event_type


def test_logging_denylist_contains_ten_expected_keys() -> None:
    """:data:`LOGGING_DENYLIST` names all ten forbidden log-key strings.

    Requirement 9.3 enumerates the ten payload / credential names that
    must never appear as structured log keys. The exact-set-equality
    assertion here (rather than a subset check) fails loudly if either
    a key is dropped from the denylist or a new one is added without
    updating this contract test.
    """
    assert LOGGING_DENYLIST == _EXPECTED_DENYLIST
    assert len(LOGGING_DENYLIST) == 10


def test_no_orchestrator_log_site_uses_denylisted_kwarg() -> None:
    """Grep-scan guard: no ``logger.<level>(..., <denylisted>=…)`` in-package.

    Design.md §7.4 makes the denylist a *convention* rather than a
    Powertools filter — this test is the enforcement mechanism.
    Walks the three modules that actually emit log records
    (``handler.py``, ``ecs_dispatcher.py``, ``never_fail_open.py``)
    and asserts no log-call keyword-argument name matches a denylist
    entry. Catches accidental ``logger.error("...", body=raw_body)``
    regressions at test time (Requirement 9.3).
    """
    denylist_alternation = "|".join(sorted(LOGGING_DENYLIST))
    pattern = re.compile(
        r"logger\.(debug|info|warning|error|critical)\("
        rf"[^)]*\b({denylist_alternation})=",
    )
    source_root = Path(__file__).resolve().parent.parent
    targets = (
        source_root / "handler.py",
        source_root / "ecs_dispatcher.py",
        source_root / "never_fail_open.py",
    )
    offenders: list[str] = []
    for path in targets:
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            offenders.append(f"{path.name}: {match.group(0)!r}")
    assert offenders == [], (
        "log-call kwarg name(s) in LOGGING_DENYLIST found: "
        + "; ".join(offenders)
    )


def test_emitted_log_records_carry_no_denylist_keys(
    log_stream: io.StringIO,
) -> None:
    """Runtime guard: a record produced by an ``append_job_context``ed
    logger carries no denylist key.

    Complements the static grep scan by validating the actual JSON on
    the wire — Powertools reserved keys ``level``, ``location``,
    ``message``, ``timestamp``, ``service`` plus the five natural keys
    are the only entries that should surface.
    """
    logger = get_logger()
    message = make_sqs_job_message()

    append_job_context(logger, message=message)
    logger.info("run_task_dispatched")

    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one log record"
    for line in lines:
        record = json.loads(line)
        for banned in LOGGING_DENYLIST:
            assert banned not in record, (
                f"denylisted key {banned!r} present in log record: {record!r}"
            )
