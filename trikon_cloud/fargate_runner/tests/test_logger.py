# The structlog processor signature accepts arbitrary ``event_dict`` values
# — the ``Any`` in the ``dict[str, Any]`` annotation is bounded to the
# structlog processor protocol and does not leak into production surfaces.
# Suppress the repo's ``disallow_any_explicit`` at file scope.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.fargate_runner.logger`.

Covers the ``_redact_pii_processor`` regex scrubbers (email, PEM
private-key block, ``x-access-token:...@`` URL fragment) and the
end-to-end ``configure_logging`` → ``get_logger`` → stdout JSON flow.
Invariant 6 in design.md §6 requires every secret pattern to be
scrubbed before serialization, so a stray ``logger.info(...,
email="alice@...")`` never surfaces the raw email.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import structlog

from trikon_cloud.fargate_runner.logger import (
    _redact_pii_processor,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Reset structlog's global configuration before every test.

    ``configure_logging`` mutates the process-wide structlog pipeline
    via :func:`structlog.configure`. Without a reset between tests,
    the first test that calls it wins and subsequent tests inherit
    the same configuration — including the cached logger factory,
    which is scoped to the process and does not honor a re-configure
    when ``cache_logger_on_first_use=True``. Reset before each test
    so every case starts clean.
    """
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# _redact_pii_processor — unit tests on the processor callable directly.
# ---------------------------------------------------------------------------


def test_redact_email_in_event_dict() -> None:
    """The email regex substitutes ``<email>`` in string values."""
    event_dict: dict[str, Any] = {"event": "user alice@example.com signed in"}
    result = _redact_pii_processor(None, "info", event_dict)
    assert result["event"] == "user <email> signed in"


def test_redact_multiple_emails() -> None:
    """Every email in every string value gets redacted, across keys."""
    event_dict: dict[str, Any] = {
        "event": "primary alice@example.com and cc bob@example.org",
        "actor": "carol@example.net",
        "reviewer": "dave@example.io opened the PR",
    }
    result = _redact_pii_processor(None, "info", event_dict)
    assert "alice@example.com" not in str(result)
    assert "bob@example.org" not in str(result)
    assert "carol@example.net" not in str(result)
    assert "dave@example.io" not in str(result)
    assert result["event"] == "primary <email> and cc <email>"
    assert result["actor"] == "<email>"
    assert result["reviewer"] == "<email> opened the PR"


def test_redact_pem_block() -> None:
    """A PEM private-key block is replaced with ``<private-key>`` wholesale."""
    pem_block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAtHFEXAMPLE0000000000000000000000000000000000000000\n"
        "extra fake key material for the test only\n"
        "-----END RSA PRIVATE KEY-----"
    )
    event_dict: dict[str, Any] = {"event": f"loaded key: {pem_block}"}
    result = _redact_pii_processor(None, "info", event_dict)
    assert "BEGIN" not in str(result)
    assert "END" not in str(result)
    assert "MIIEpAIBAA" not in str(result)
    assert "<private-key>" in result["event"]


def test_redact_access_token_url_fragment() -> None:
    """The installation-token portion of an authenticated clone URL is scrubbed.

    Invariant 6 requires the token value (``ghs_abc123def456``) to be
    absent from the rendered record. The processor runs the
    access-token substitution BEFORE the email substitution so a URL
    like ``x-access-token:ghs_abc123def456@github.com/foo/bar.git``
    gets its auth prefix rewritten to
    ``x-access-token:<redacted>@github.com/foo/bar.git`` — the
    ``<redacted>`` sentinel is deterministic (not ``<email>``), and
    the path suffix survives so operators can still tell which repo
    was targeted from the record.
    """
    event_dict: dict[str, Any] = {
        "event": "cloning from https://x-access-token:ghs_abc123def456@github.com/foo/bar.git"
    }
    result = _redact_pii_processor(None, "info", event_dict)

    # Load-bearing Invariant 6 assertion: the raw token never appears.
    assert "ghs_abc123def456" not in str(result)

    # The access-token substitution fires first, so the sentinel is
    # deterministic — always ``<redacted>``, never ``<email>``.
    scrubbed = result["event"]
    assert isinstance(scrubbed, str)
    assert "x-access-token:<redacted>@github.com" in scrubbed
    assert "<email>" not in scrubbed

    # Path suffix survives the substitution.
    assert "/foo/bar.git" in scrubbed


def test_non_string_values_untouched() -> None:
    """The processor leaves non-string values (int, etc.) untouched."""
    event_dict: dict[str, Any] = {
        "installation_id": 12345678,
        "count": 42,
        "event": "x",
    }
    result = _redact_pii_processor(None, "info", event_dict)
    assert result["installation_id"] == 12345678
    assert result["count"] == 42
    assert result["event"] == "x"
    assert isinstance(result["installation_id"], int)
    assert isinstance(result["count"], int)


# ---------------------------------------------------------------------------
# configure_logging + get_logger — end-to-end JSON emission.
# ---------------------------------------------------------------------------


_ISO_8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)


def test_configure_logging_emits_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full processor chain emits a single-line JSON record to stdout.

    Configures logging, emits one info record with an email kwarg, and
    asserts the captured stdout is one line of valid JSON carrying:

    * ``level == "info"`` (from :func:`structlog.processors.add_log_level`)
    * ``event == "test message"`` (the positional message)
    * ``email == "<email>"`` (the PII processor redacted the value)
    * ``timestamp`` matching ISO-8601-UTC (:class:`TimeStamper` with
      ``fmt="iso"``, ``utc=True``, ``key="timestamp"``)

    Together these confirm the processor chain is installed in the
    right order and the PII redactor runs *before* the JSON renderer.
    """
    configure_logging(log_level="INFO")
    logger = get_logger("test")

    logger.info("test message", email="alice@example.com")

    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one log line, got {len(lines)}: {lines!r}"

    payload: dict[str, Any] = json.loads(lines[0])

    assert payload["level"] == "info"
    assert payload["event"] == "test message"
    assert payload["email"] == "<email>"
    assert "alice@example.com" not in lines[0]

    timestamp = payload.get("timestamp")
    assert isinstance(timestamp, str)
    assert _ISO_8601_UTC_RE.match(timestamp), (
        f"timestamp {timestamp!r} does not match ISO-8601-UTC pattern"
    )
