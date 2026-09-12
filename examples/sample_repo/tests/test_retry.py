"""Baseline tests for `payments.retry.with_backoff`."""
from __future__ import annotations

import pytest

from payments.retry import with_backoff


def test_returns_first_success_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: sleeps.append(s))

    def ok() -> str:
        return "ok"

    assert with_backoff(ok, max_attempts=3, base_delay_s=0.01) == "ok"
    assert sleeps == []


def test_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert with_backoff(flaky, max_attempts=5, base_delay_s=0.02) == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.02, 0.02]


def test_raises_last_exception_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("payments.retry.time.sleep", lambda s: None)
    calls = {"n": 0}

    def always_fails() -> str:
        calls["n"] += 1
        raise RuntimeError(f"attempt-{calls['n']}")

    with pytest.raises(RuntimeError, match="attempt-3"):
        with_backoff(always_fails, max_attempts=3, base_delay_s=0.01)
    assert calls["n"] == 3
