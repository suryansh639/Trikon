"""Shared pytest fixtures for every Trikon Cloud test package.

Provides :func:`no_network`, an opt-in guard proving a test contacts
nothing over the network (Req 1.3, 14.2). It is deliberately not autouse:
existing moto-backed tests keep working unchanged, and tests that need the
guard (offline CDK synth, handler tests) request it by name or with
``@pytest.mark.usefixtures("no_network")``.

Also provides :func:`lambda_asset_dir`, a session-scoped Lambda_Asset built
from the current sources with a fake installer, for tests that synthesize
the Lambda stacks.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence

import pytest

from trikon_cloud import lambda_asset


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


def _fake_installer(command: Sequence[str]) -> int:
    """Stand-in for uv/pip: installs nothing, contacts nothing, succeeds."""
    return 0


@pytest.fixture(scope="session")
def lambda_asset_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A real, non-stale Lambda_Asset directory built once per session.

    It holds the current Packaged_Sources and a valid Asset_Manifest, but no
    third-party packages: the fake installer writes nothing, so no network is
    used. Pass it as the ``lambda_asset_dir`` CDK context value.
    """
    out = tmp_path_factory.mktemp("lambda-asset")
    code = lambda_asset.build(out, run=_fake_installer)
    assert code == 0, f"lambda_asset.build returned {code}"
    return str(out)
