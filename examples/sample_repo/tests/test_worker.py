"""Baseline tests for `orders.worker.PaymentWorker`."""
from __future__ import annotations

import pytest

from orders.worker import PaymentJob, PaymentWorker
from payments.gateway import ChargeResult


def test_worker_processes_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)

    worker = PaymentWorker()
    result = worker.process(PaymentJob(order_id="o1", amount=2500, card_id="c1"))

    assert isinstance(result, ChargeResult)
    assert result.amount_cents == 2500
    assert result.card_id == "c1"
    assert result.status == "ok"


def test_worker_respects_2s_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A charge whose elapsed wall-clock exceeds 2 s must raise TimeoutError.

    Uses a fake monotonic clock so the test itself stays instantaneous. This is
    the invariant the `bad_retry` scenario (task 2.2) breaks by making
    `payments.retry.with_backoff` sleep long enough that `process` overshoots.
    """
    # First call is the "started" reading; second is the post-work reading. A
    # 5 s gap simulates a real 5 s call without actually sleeping.
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr("orders.worker.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)

    worker = PaymentWorker()
    with pytest.raises(TimeoutError, match="exceeded 2.0s"):
        worker.process(PaymentJob(order_id="o2", amount=10, card_id="c2"))
