"""Baseline tests for `api.payments.charge_endpoint`."""
from __future__ import annotations

import pytest

from api.payments import charge_endpoint


def test_charge_endpoint_returns_ok_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)

    resp = charge_endpoint({"amount": 500, "card_id": "cd_1"})

    assert resp["status"] == "ok"
    assert isinstance(resp["charge_id"], str)
    assert resp["charge_id"].startswith("ch_")
    assert resp["amount_cents"] == 500


def test_charge_endpoint_coerces_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)

    resp = charge_endpoint({"amount": "750", "card_id": "cd_2"})

    assert resp["status"] == "ok"
    assert resp["amount_cents"] == 750
