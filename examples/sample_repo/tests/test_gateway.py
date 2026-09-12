"""Baseline tests for `payments.gateway`."""
from __future__ import annotations

import pytest

from payments.gateway import ChargeResult, _normalize_amount, charge


def test_charge_returns_charge_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)
    result = charge(1500, "card_abc")
    assert isinstance(result, ChargeResult)
    assert result.amount_cents == 1500
    assert result.card_id == "card_abc"
    assert result.status == "ok"
    assert result.charge_id.startswith("ch_")


def test_normalize_amount_rounds_to_cents() -> None:
    assert _normalize_amount(100) == 100
    assert _normalize_amount(100.4) == 100
    assert _normalize_amount(100.6) == 101


def test_normalize_amount_rejects_negative() -> None:
    with pytest.raises(ValueError):
        _normalize_amount(-1)
