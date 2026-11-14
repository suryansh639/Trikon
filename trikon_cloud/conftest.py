"""Shared pytest fixtures for every Trikon Cloud test package.

Provides :func:`no_network`, an opt-in guard proving a test contacts
nothing over the network (Req 1.3, 14.2). It is deliberately not autouse:
existing moto-backed tests keep working unchanged, and tests that need the
guard (offline CDK synth, handler tests) request it by name or with
``@pytest.mark.usefixtures("no_network")``.
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessError(RuntimeError):
    """Raised when a test guarded by :func:`no_network` opens a connection."""


def _refuse_connect(self: socket.socket, address: object) -> None:
    raise NetworkAccessError(f"network access blocked by no_network fixture: {address!r}")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block outbound socket connections and drop AWS account/profile hints.

    * ``socket.socket.connect`` raises :class:`NetworkAccessError`, so any
      HTTP client (botocore, httpx, urllib) fails loudly instead of reaching
      AWS or GitHub. moto and respx patch above the socket layer, so tests
      that use them still work under this fixture.
    * ``AWS_PROFILE`` and ``CDK_DEFAULT_ACCOUNT`` are removed, so CDK
      synthesis cannot resolve a real account or credentials profile.

    monkeypatch restores everything at teardown.
    """
    monkeypatch.setattr(socket.socket, "connect", _refuse_connect)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
