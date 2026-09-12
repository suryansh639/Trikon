"""Retry helper used by the payments subsystem.

The `bad_retry` scenario (Trikon test task 2.2) rewrites the timing knob here
to drive `orders.worker.PaymentWorker.process` past its 2 s deadline.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int,
    base_delay_s: float,
) -> T:
    """Call `fn`, retrying up to `max_attempts` times.

    Between attempts, sleep for `base_delay_s` seconds. The last raised
    exception propagates when every attempt fails.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay_s < 0:
        raise ValueError("base_delay_s must be >= 0")

    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- broad by design for a retry helper
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(base_delay_s)
    assert last_exc is not None  # unreachable: loop always assigns before break
    raise last_exc
