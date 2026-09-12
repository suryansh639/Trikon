"""Public HTTP endpoint façade for payments. Framework-agnostic on purpose."""
from __future__ import annotations

from typing import Any

from payments.gateway import ChargeResult, charge


def charge_endpoint(request: dict[str, Any]) -> dict[str, Any]:
    """Handle a POST /charge request.

    Expects `{"amount": int, "card_id": str}`. Returns a JSON-shaped dict
    `{"status": "ok", "charge_id": str, "amount_cents": int}`.
    """
    amount = int(request["amount"])
    card_id = str(request["card_id"])
    result: ChargeResult = charge(amount, card_id)
    return {
        "status": result.status,
        "charge_id": result.charge_id,
        "amount_cents": result.amount_cents,
    }
