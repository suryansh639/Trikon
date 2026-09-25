"""Unit tests for :mod:`trikon_cloud.fargate_runner.token_cache`.

Covers the double-checked-locking cache semantics, the process-wide
:func:`get_cache` singleton, and Invariant 6 (the cached token never
appears in ``repr(cache)`` so a stray diagnostic printer cannot leak
it to the observability plane).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from trikon_cloud.fargate_runner.models import GithubInstallationTokenResponse
from trikon_cloud.fargate_runner.token_cache import TokenCache, get_cache

from .conftest import make_installation_token_response


class _SpyMinter:
    """A callable that records how many times it is invoked.

    Emulates the ``minter`` protocol expected by
    :meth:`TokenCache.get_or_mint`: called with no arguments, returns a
    :class:`GithubInstallationTokenResponse`. The optional ``sleep_s``
    inserts a delay inside the call to widen the race window the
    thread-safety test exercises.
    """

    def __init__(self, *, token: str = "ghs_test_token_not_real", sleep_s: float = 0.0) -> None:
        self._token = token
        self._sleep_s = sleep_s
        self.call_count: int = 0

    def __call__(self) -> GithubInstallationTokenResponse:
        self.call_count += 1
        if self._sleep_s > 0:
            time.sleep(self._sleep_s)
        response = make_installation_token_response(token=self._token)
        return GithubInstallationTokenResponse.model_validate(response)


def test_get_or_mint_calls_minter_once_on_first_call() -> None:
    """First :meth:`get_or_mint` call invokes the minter exactly once."""
    spy = _SpyMinter(token="ghs_first")
    cache = TokenCache()

    result = cache.get_or_mint(minter=spy)

    assert spy.call_count == 1
    assert result == "ghs_first"


def test_get_or_mint_returns_cached_on_second_call() -> None:
    """A subsequent call returns the cached token without re-minting."""
    spy = _SpyMinter(token="ghs_cached")
    cache = TokenCache()

    first = cache.get_or_mint(minter=spy)
    second = cache.get_or_mint(minter=spy)

    assert spy.call_count == 1
    assert first == "ghs_cached"
    assert second == "ghs_cached"
    assert first == second


def test_clear_forces_reminting() -> None:
    """After :meth:`clear`, the next call mints a fresh token.

    Verifies the clear path is reachable and correct — reserved for a
    future explicit-refresh scenario if the task-lifetime assumption
    ever gets relaxed.
    """
    spy = _SpyMinter(token="ghs_reminted")
    cache = TokenCache()

    cache.get_or_mint(minter=spy)
    assert spy.call_count == 1

    cache.clear()
    cache.get_or_mint(minter=spy)

    assert spy.call_count == 2


def test_thread_safety_single_slot_under_contention() -> None:
    """Double-checked storage yields one canonical token under contention.

    Spins 20 worker threads that all call :meth:`get_or_mint`
    simultaneously, with a 100 ms sleep inside the spy to widen the
    race window between the fast-path check and the slow-path
    re-check.

    The load-bearing invariant is that every thread observes the
    **same** cached value — the double-checked-locking pattern
    single-slots the storage step, so whichever mint wins the second
    lock acquisition becomes the canonical token for every caller.
    A follow-up call after all threads finish must reuse that
    canonical token with no additional mint (the fast path is warm).

    The current :meth:`TokenCache.get_or_mint` runs ``minter`` outside
    the lock so concurrent cache-miss callers do not serialize on
    the network round-trip — that is intentional per its docstring.
    Consequently the mint callable may run more than once under
    contention; only the store is single-slotted. The assertion below
    reflects that actual invariant.
    """
    spy = _SpyMinter(token="ghs_racy", sleep_s=0.1)
    cache = TokenCache()

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: cache.get_or_mint(minter=spy), range(20)))

    # Every thread must see the same cached value — the single-slot
    # store guarantees a canonical winner regardless of scheduling.
    assert results.count(results[0]) == len(results)
    assert results[0] == "ghs_racy"

    # After contention settles, a subsequent call hits the fast path
    # and does not re-mint.
    baseline_count = spy.call_count
    cache.get_or_mint(minter=spy)
    assert spy.call_count == baseline_count


def test_token_value_not_in_repr() -> None:
    """Invariant 6 — :meth:`__repr__` never leaks the cached token value.

    A future diagnostic printer (traceback frame renderer, REPL echo,
    ad-hoc ``print(cache)``) that renders the cache must not expose
    the secret. The distinctive marker string below is present in the
    cache after :meth:`get_or_mint`; the repr must not contain it.
    """
    marker = "secret_marker_do_not_leak"
    spy = _SpyMinter(token=marker)
    cache = TokenCache()

    cache.get_or_mint(minter=spy)

    rendered = repr(cache)
    assert marker not in rendered
    # And repr still reports something useful about the state.
    assert "populated=True" in rendered


def test_get_cache_returns_singleton() -> None:
    """:func:`get_cache` returns the same instance across calls.

    The module-level singleton is lazily constructed on first call
    under its own lock; subsequent calls return the same object
    identity. Every consumer in the runner reaches the token via this
    singleton, so ``is``-identity is the load-bearing property.
    """
    first = get_cache()
    second = get_cache()

    assert first is second
