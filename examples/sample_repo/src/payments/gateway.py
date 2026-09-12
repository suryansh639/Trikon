"""Payment gateway — public API surface of the payments subsystem."""
from __future__ import annotations

from dataclasses import dataclass

from payments.retry import with_backoff


@dataclass(frozen=True)
class ChargeResult:
    """Result of a successful `charge()` call."""

    charge_id: str
    amount_cents: int
    card_id: str
    status: str


def _normalize_amount(amount: int | float) -> int:
    """Convert `amount` to non-negative integer cents (banker's rounding is fine)."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return int(round(float(amount)))


def _external_charge_call(amount_cents: int, card_id: str) -> str:
    """Placeholder for the real gateway call. Tests monkeypatch around this."""
    return f"ch_{card_id}_{amount_cents}"


def charge(amount: int, card_id: str) -> ChargeResult:
    """Charge `card_id` for `amount` cents via the gateway with retries.

    This is the public entry point of the payments subsystem: `orders.worker`
    and `api.payments` both call it.
    """
    amount_cents = _normalize_amount(amount)

    def _do_charge() -> str:
        return _external_charge_call(amount_cents, card_id)

    charge_id = with_backoff(_do_charge, max_attempts=3, base_delay_s=0.05)
    return ChargeResult(
        charge_id=charge_id,
        amount_cents=amount_cents,
        card_id=card_id,
        status="ok",
    )
