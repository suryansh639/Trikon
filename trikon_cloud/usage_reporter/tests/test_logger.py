"""Unit tests for :mod:`trikon_cloud.usage_reporter.logger`.

Covers sandbox-minute-plans Req 12.15 and 15.1:

* :data:`LOGGING_DENYLIST` is a superset of the orchestrator's denylist and
  adds the four Usage_API keys (``usage_secret``, ``usage_signature``,
  ``request_body``, ``response_body``). The exact fourteen-key set is pinned.
* :func:`get_logger` returns a module-level singleton with service
  ``"trikon-cloud-usage-reporter"`` and ``log_uncaught_exceptions=True``.
* Static AST guard over every non-test module in
  ``trikon_cloud/usage_reporter/``: each ``logger.<level>(...)`` call passes a
  fixed string event name, no denylisted keyword, no ``**kwargs``, and no
  obviously sensitive value (the event, the Usage_Report, a body, a response,
  the Usage_Secret, a Usage_Signature, ``.content`` or ``model_dump``).
* ``trikon_cloud/usage_client/`` logs nothing: it contains no logger calls.

An AST walk is used instead of the orchestrator's regex so multi-line calls,
nested parentheses and attribute access are all handled.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeGuard

from trikon_cloud.orchestrator.logger import (
    LOGGING_DENYLIST as ORCHESTRATOR_DENYLIST,
)
from trikon_cloud.usage_reporter.logger import LOGGING_DENYLIST, get_logger

# Two-way contract with trikon_cloud/usage_reporter/logger.py: the equality
# assertion fails if a key is dropped or added on either side.
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
        "usage_secret",
        "usage_signature",
        "request_body",
        "response_body",
    }
)

_USAGE_KEYS: frozenset[str] = frozenset(
    {"usage_secret", "usage_signature", "request_body", "response_body"}
)

_LOG_LEVELS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# Identifiers that must never be passed to a log call. Substring match, so
# ``raw_body``, ``usage_secret``, ``response_body`` etc. are caught too.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("body", "secret", "signature", "response")

# Identifiers matched exactly: the raw EventBridge event and the Usage_Report.
_SENSITIVE_NAMES: frozenset[str] = frozenset({"event", "report", "usage_report"})

# Attribute or method names whose value is a payload dump.
_SENSITIVE_ATTRS: frozenset[str] = frozenset(
    {"content", "text", "json", "model_dump", "model_dump_json", "headers"}
)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_USAGE_CLIENT_ROOT = _PACKAGE_ROOT.parent / "usage_client"


def _source_files(root: Path) -> list[Path]:
    """Every ``.py`` file under ``root``, excluding the ``tests`` directory."""
    return sorted(
        path
        for path in root.rglob("*.py")
        if "tests" not in path.relative_to(root).parts and "__pycache__" not in path.parts
    )


def _is_log_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """True for ``<something log-ish>.<level>(...)``, e.g. ``logger.info(...)``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in _LOG_LEVELS:
        return False
    receiver = node.func.value
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    if isinstance(receiver, ast.Attribute):
        return "log" in receiver.attr.lower()
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
        return receiver.func.id == "get_logger"
    return False


def _log_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if _is_log_call(node)]


def _sensitive_identifiers(expr: ast.expr) -> list[str]:
    """Names and attributes inside ``expr`` that look like payloads or secrets."""
    found: list[str] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            ident = node.id.lower()
            if ident in _SENSITIVE_NAMES or any(s in ident for s in _SENSITIVE_SUBSTRINGS):
                found.append(node.id)
        elif isinstance(node, ast.Attribute):
            attr = node.attr.lower()
            if attr in _SENSITIVE_ATTRS or any(s in attr for s in _SENSITIVE_SUBSTRINGS):
                found.append(f".{node.attr}")
    return found


def _violations(source: str, filename: str) -> list[str]:
    """Return one message per unsafe log call in ``source``."""
    problems: list[str] = []
    for call in _log_calls(ast.parse(source, filename=filename)):
        where = f"{filename}:{call.lineno}"
        # Req 12.15: a fixed event name, with no %-style or positional payloads.
        if (
            not call.args
            or not isinstance(call.args[0], ast.Constant)
            or not isinstance(call.args[0].value, str)
        ):
            problems.append(f"{where}: first argument is not a fixed string event name")
        for extra in call.args[1:]:
            problems.append(f"{where}: extra positional argument {ast.unparse(extra)!r}")
            for ident in _sensitive_identifiers(extra):
                problems.append(f"{where}: positional argument passes {ident!r}")
        for keyword in call.keywords:
            if keyword.arg is None:
                problems.append(f"{where}: **{ast.unparse(keyword.value)} cannot be checked")
                continue
            if keyword.arg in LOGGING_DENYLIST:
                problems.append(f"{where}: denylisted keyword {keyword.arg!r}")
            for ident in _sensitive_identifiers(keyword.value):
                problems.append(
                    f"{where}: keyword {keyword.arg!r} passes sensitive value {ident!r}"
                )
    return problems


# ---------------------------------------------------------------------------
# Denylist contract.
# ---------------------------------------------------------------------------


def test_denylist_covers_orchestrator_keys_plus_usage_keys() -> None:
    """Req 15.1: every orchestrator key is denied here, plus the four Usage_API keys."""
    assert ORCHESTRATOR_DENYLIST <= LOGGING_DENYLIST
    assert _USAGE_KEYS <= LOGGING_DENYLIST


def test_denylist_is_exactly_fourteen_expected_keys() -> None:
    """Pin the exact set so an added or dropped key fails loudly."""
    assert LOGGING_DENYLIST == _EXPECTED_DENYLIST
    assert len(LOGGING_DENYLIST) == 14


# ---------------------------------------------------------------------------
# Logger singleton.
# ---------------------------------------------------------------------------


def test_get_logger_returns_configured_singleton() -> None:
    """One Logger per container, named for the Usage_Reporter service."""
    first = get_logger()
    second = get_logger()
    assert first is second
    assert first.service == "trikon-cloud-usage-reporter"
    assert first.log_uncaught_exceptions is True


# ---------------------------------------------------------------------------
# Static guards (Req 12.15).
# ---------------------------------------------------------------------------


def test_guard_flags_unsafe_log_calls() -> None:
    """The checker itself catches each class of leak, so a clean scan means something."""
    unsafe = "\n".join(
        [
            'logger.info("x", body=raw)',
            'logger.info("x", task_arn=report)',
            'logger.error("x", status=resp.content)',
            'logger.info("x", data=report.model_dump())',
            'logger.warning("x", secret=usage_secret)',
            'logger.info("x", sig=usage_signature)',
            'logger.info("x", payload=event)',
            'logger.info("x", r=response)',
            'logger.info("x", **fields)',
            'logger.info(f"x {run_id}")',
            'logger.info("x %s", event)',
            'get_logger().debug("x", request_body=b)',
        ]
    )
    problems = _violations(unsafe, "synthetic.py")
    flagged_lines = {int(p.split(":")[1]) for p in problems}
    assert flagged_lines == set(range(1, 13)), problems

    safe = 'logger.error("usage_report_rejected", task_arn=t, run_id=r, status=result.status)'
    assert _violations(safe, "synthetic.py") == []


def test_usage_reporter_log_calls_pass_no_body_secret_or_signature() -> None:
    """No log call in the Usage_Reporter package leaks a payload or credential."""
    files = _source_files(_PACKAGE_ROOT)
    assert {p.name for p in files} >= {"handler.py", "logger.py", "models.py"}

    problems: list[str] = []
    call_count = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        call_count += len(_log_calls(ast.parse(source, filename=path.name)))
        problems.extend(_violations(source, path.name))

    # handler.py emits usage_event_ignored, usage_report_skipped,
    # usage_report_delivered and usage_report_rejected; a zero count would
    # mean the guard stopped recognising the call sites.
    assert call_count >= 4
    assert problems == [], "unsafe log call(s):\n" + "\n".join(problems)


def test_usage_client_has_no_log_calls() -> None:
    """The shared Usage_API client logs nothing, so it cannot leak a body or secret."""
    files = _source_files(_USAGE_CLIENT_ROOT)
    assert {p.name for p in files} >= {"client.py", "signing.py", "secret.py"}

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        offenders.extend(f"{path.name}:{call.lineno}" for call in _log_calls(tree))
        offenders.extend(
            f"{path.name}:{node.lineno}: print()"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        )
    assert offenders == [], "usage_client must not log: " + ", ".join(offenders)
