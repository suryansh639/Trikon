"""Payment-processing worker. Wraps `payments.gateway.charge` with retries
and enforces a 2 s wall-clock deadline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from payments.gateway import ChargeResult, charge
from payments.retry import with_backoff


@dataclass(frozen=True)
class PaymentJob:
    """One unit of work handed to `PaymentWorker.process`."""

    order_id: str
    amount: int
    card_id: str


class PaymentWorker:
    """Processes payment jobs against the payments gateway."""

    DEADLINE_SECONDS: float = 2.0

    def process(self, job: PaymentJob) -> ChargeResult:
        """Process `job`, retrying transient failures within a 2 s deadline.

        The `bad_retry` scenario (Trikon test task 2.2) rewrites
        `payments.retry.with_backoff` timing so the total elapsed wall-clock
        exceeds `DEADLINE_SECONDS`, causing this method to raise
        `TimeoutError`.
        """
        started = time.monotonic()

        def _do_charge() -> ChargeResult:
            return charge(job.amount, job.card_id)

        result: ChargeResult = with_backoff(
            _do_charge,
            max_attempts=2,
            base_delay_s=0.01,
        )

        elapsed = time.monotonic() - started
        if elapsed > self.DEADLINE_SECONDS:
            raise TimeoutError(
                f"PaymentWorker.process exceeded {self.DEADLINE_SECONDS}s "
                f"deadline (took {elapsed:.3f}s)"
            )
        return result
