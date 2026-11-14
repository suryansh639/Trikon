"""Checks for the shared ``no_network`` fixture in ``trikon_cloud/conftest.py`` (Req 1.3, 14.2)."""

from __future__ import annotations

import os
import socket

import pytest

from trikon_cloud.conftest import NetworkAccessError


@pytest.fixture
def aws_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    # Runs before no_network when listed first, so no_network must remove them.
    monkeypatch.setenv("AWS_PROFILE", "should-be-cleared")
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")


@pytest.mark.usefixtures("aws_hints", "no_network")
def test_no_network_blocks_connect_and_clears_aws_hints() -> None:
    assert "AWS_PROFILE" not in os.environ
    assert "CDK_DEFAULT_ACCOUNT" not in os.environ
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessError),
    ):
        sock.connect(("192.0.2.1", 443))  # TEST-NET-1; never actually dialled


def test_connect_is_untouched_without_the_fixture() -> None:
    assert socket.socket.connect.__name__ != "_refuse_connect"
