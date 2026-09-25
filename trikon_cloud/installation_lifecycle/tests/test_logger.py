"""Unit tests for :mod:`trikon_cloud.installation_lifecycle.logger`.

Covers the four contracts task 18.2 pins on this module:

* :func:`get_logger` returns a module-level singleton — the same
  :class:`aws_lambda_powertools.Logger` instance on every call within
  a container's warm lifetime.
* :attr:`Logger.service` is exactly ``"trikon-cloud-installation-lifecycle"``
  (Requirement 18.2 fixes the ``trikon-cloud`` service-name prefix for
  every Spec-3 Lambda).
* :func:`append_lifecycle_context` attaches the three natural-key
  fields ``installation_id``, ``event_type``, ``delivery_id`` to the
  Powertools log context, so every subsequent log record within the
  same warm invocation carries them (Requirement 15.2).
* :data:`LOGGING_DENYLIST` is the exact five-entry frozenset the
  design pins (design.md §2.2 / Invariant 6): ``body``, ``raw_body``,
  ``sqs_body``, ``app_private_key``, ``aws_credentials``.

A fifth test — the static AST-scan guard — reads the three sibling
production modules (``handler.py``, ``iam_provisioner.py``,
``dynamodb_writer.py``) and asserts that no ``logger.*`` call site
passes any denylist key as a keyword argument. Design.md §2.2 notes
that the denylist is enforced by convention rather than by a
Powertools log filter; this guard catches a regression at test time
if a future edit accidentally writes ``logger.error("...", body=...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from trikon_cloud.installation_lifecycle.logger import (
    LOGGING_DENYLIST,
    append_lifecycle_context,
    get_logger,
)
from trikon_cloud.installation_lifecycle.tests.conftest import (
    make_installation_event_message,
)

# ---------------------------------------------------------------------------
# get_logger — singleton identity + service-name prefix.
# ---------------------------------------------------------------------------


def test_get_logger_returns_same_singleton_across_calls() -> None:
    """Two calls to :func:`get_logger` return the identical instance.

    The module constructs the :class:`Logger` once at import time and
    every subsequent :func:`get_logger` call returns that same object.
    ``is`` identity — not merely ``==`` equality — because AWS Lambda
    reuses the module across warm invocations and every invocation
    must share one logger.
    """
    first = get_logger()
    second = get_logger()

    assert first is second


def test_get_logger_service_name_matches_requirement_18_2() -> None:
    """The singleton's ``service`` attribute is the Requirement 18.2 name.

    Requirement 18.2 fixes the ``trikon-cloud`` service-name prefix
    across every Spec-3 Lambda; the installation-lifecycle handler's
    service name is the fully-qualified
    ``trikon-cloud-installation-lifecycle`` — byte-for-byte, no
    trailing whitespace, no case drift.
    """
    logger = get_logger()

    assert logger.service == "trikon-cloud-installation-lifecycle"


# ---------------------------------------------------------------------------
# append_lifecycle_context — the three natural-key fields.
# ---------------------------------------------------------------------------


def test_append_lifecycle_context_appends_three_natural_keys() -> None:
    """After calling the helper, the log context carries the three keys.

    Requirement 15.2 pins the Structured log field set for the
    lifecycle handler to exactly ``installation_id``, ``event_type``,
    and ``delivery_id``. This test constructs a canonical
    :class:`InstallationEventMessage`, appends it, and asserts each
    key appears on the logger's current persistent state with a value
    byte-identical to the message field.

    The conftest's autouse ``_powertools_logger_state_clear`` fixture
    guarantees a clean slate on entry, so no keys from a prior test
    leak into this assertion.
    """
    logger = get_logger()
    message = make_installation_event_message(
        event_type="installation_repositories.added",
    )

    append_lifecycle_context(logger, message=message)

    current_keys = logger.get_current_keys()
    assert current_keys["installation_id"] == message.installation_id
    assert current_keys["event_type"] == message.event_type
    assert current_keys["delivery_id"] == message.delivery_id


# ---------------------------------------------------------------------------
# LOGGING_DENYLIST — the five-entry frozenset.
# ---------------------------------------------------------------------------


def test_logging_denylist_matches_exact_five_entry_set() -> None:
    """:data:`LOGGING_DENYLIST` is the exact five-entry frozenset.

    Design.md §2.2 scopes the lifecycle-handler denylist to a smaller
    IO surface than the orchestrator's ten-entry set: only the raw
    SQS body forms and the two credential-material names. An exact
    equality check (rather than a superset check) locks the set so a
    future edit that adds or removes an entry must update this test
    deliberately.
    """
    expected = frozenset(
        {
            "body",
            "raw_body",
            "sqs_body",
            "app_private_key",
            "aws_credentials",
        }
    )

    assert expected == LOGGING_DENYLIST
    # Belt-and-braces: pin the size too, so a future edit that swaps
    # one member for another still fails the equality above but the
    # size assertion documents the "exactly five entries" invariant.
    assert len(LOGGING_DENYLIST) == 5


# ---------------------------------------------------------------------------
# Static AST-scan guard — no denylist key on any logger.* kwarg.
# ---------------------------------------------------------------------------


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_SCANNED_MODULES: tuple[str, ...] = (
    "handler.py",
    "iam_provisioner.py",
    "dynamodb_writer.py",
)


def _collect_logger_kwarg_names(source: str) -> frozenset[str]:
    """Return every keyword-argument name passed to any ``logger.*`` call.

    Walks the AST of ``source`` and, for every :class:`ast.Call` whose
    function is an attribute access on a name that ends in ``logger``
    (matching both bare ``logger.info(...)`` and
    ``get_logger().error(...)`` after the intermediate call), collects
    the ``keyword.arg`` string of each keyword argument. Star-args
    (``**kwargs``) have ``keyword.arg is None`` and are skipped —
    those cannot statically reveal a denylist key.

    Also matches call chains via :class:`ast.Attribute` where the value
    is itself a call to a name ending in ``get_logger`` — that covers
    the ``get_logger().info(...)`` pattern the modules use.
    """
    tree = ast.parse(source)
    kwarg_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        # ``logger.info(...)`` — the attribute is on a bare Name whose id
        # ends in ``logger``. Matches ``logger``, ``_LOG``-style aliases
        # are not caught by this suffix rule, but the three scanned modules
        # consistently use ``logger`` as the local binding.
        target = func.value
        if isinstance(target, ast.Name) and target.id.endswith("logger"):
            _absorb_kwargs(node, kwarg_names)
            continue
        # ``get_logger().info(...)`` — the attribute's value is itself a
        # Call whose func is a Name ending in ``get_logger``.
        if (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id.endswith("get_logger")
        ):
            _absorb_kwargs(node, kwarg_names)
    return frozenset(kwarg_names)


def _absorb_kwargs(call: ast.Call, sink: set[str]) -> None:
    """Copy every named keyword argument on ``call`` into ``sink``."""
    for kw in call.keywords:
        if kw.arg is not None:
            sink.add(kw.arg)


def test_no_denylist_key_appears_as_logger_kwarg() -> None:
    """No ``logger.*(...)`` call in the package passes a denylist key.

    Reads the three sibling production modules — ``handler.py``,
    ``iam_provisioner.py``, ``dynamodb_writer.py`` — parses each into
    an AST, walks every ``logger.<method>(...)`` and
    ``get_logger().<method>(...)`` call, and asserts that the
    intersection of collected keyword-argument names with
    :data:`LOGGING_DENYLIST` is empty.

    This is the design.md §2.2 "grep-scan guard" — a static check that
    prevents log-line drift from introducing an Invariant 6 violation
    (raw SQS body, App private key material, AWS credentials landing
    on a structured log key). Enforcing by AST rather than a naive
    substring grep avoids false positives on the design docstrings
    inside the same modules, which reference denylist names in prose.
    """
    for module_name in _SCANNED_MODULES:
        module_path = _PACKAGE_ROOT / module_name
        source = module_path.read_text(encoding="utf-8")
        found_kwargs = _collect_logger_kwarg_names(source)
        offending = found_kwargs & LOGGING_DENYLIST
        assert not offending, (
            f"{module_name} passes denylist key(s) {sorted(offending)!r} "
            f"as a logger.* keyword argument — Invariant 6 violation."
        )
