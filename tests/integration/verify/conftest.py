"""Pytest hooks for ``tests/integration/verify/``.

Registers the ``--wheel-path`` command-line option so
:func:`tests.integration.verify.test_wheel_ships_shim.wheel_path`
can point at a specific ``.whl`` instead of auto-discovering the newest
one under ``dist/``. See ``.kiro/specs/plugin-shim-packaging/`` for the
regression this test file guards.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--wheel-path=<path>`` to the pytest CLI.

    The option is optional. When omitted, the integration test falls
    back to the newest ``dist/trikon-*.whl`` under the repo root, and
    skips (rather than fails) if nothing has been built yet — collection
    must still succeed on a fresh checkout.
    """
    parser.addoption(
        "--wheel-path",
        action="store",
        default=None,
        help=(
            "Path to the Trikon wheel to test against; when omitted, the "
            "newest dist/trikon-*.whl is used, and the test is skipped "
            "if no wheel is available."
        ),
    )
