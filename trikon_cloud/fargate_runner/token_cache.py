"""Task-lifetime installation-token cache (design.md §3.9, clarify answer 3).

The Fargate task wall time is capped at 10 minutes by Invariant 5, and
GitHub installation tokens are valid for ~1 hour — so a single mint at
task start covers the whole run with no refresh loop needed. This
module holds that single token behind a mutex-guarded slot so a
future refactor that spins helper threads (e.g., posting the Check
Run and PR comment concurrently) cannot mint the token twice under
contention. The lock is held across the ``minter`` call so
cache-miss callers serialize on the network round-trip and the
minter fires exactly once per task lifetime.

Public surface:

* :class:`TokenCache` — one-slot cache for a minted installation
  token. Instances are created lazily; the token is stored on first
  ``get_or_mint`` call and never refreshed.
* :func:`get_cache` — return the module-level singleton
  :class:`TokenCache`, constructing it lazily under its own lock.

**Security invariants** (Invariant 6 in the design):

* The cached token is stored on an underscore-prefixed attribute so
  attribute-walking diagnostic tools skip it by convention.
* :meth:`TokenCache.__repr__` deliberately omits the token value and
  renders only whether the slot is populated — guards against a
  future ``print(cache)`` inadvertently leaking the secret.
* The cache never logs the token, never returns it via a public
  attribute accessor, and never persists it to disk or any external
  store.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from trikon_cloud.fargate_runner.models import GithubInstallationTokenResponse

__all__ = ["TokenCache", "get_cache"]


class TokenCache:
    """Task-lifetime cache for a single GitHub App installation token.

    The cache holds at most one token for the lifetime of the Fargate
    task. Populated on first :meth:`get_or_mint` call; never
    refreshed (task wall time ≤ 10 min per Invariant 5; token TTL
    ≥ 1 hr per GitHub's App installation-token spec).

    Thread-safe via a mutex-guarded slot. The runner is
    single-threaded on the happy path, but the cache is designed
    defensively so a later refactor that spins helper threads (e.g.,
    concurrent Check Run + PR comment posts) cannot mint the token
    twice under contention.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._lock = threading.Lock()

    def get_or_mint(
        self, *, minter: Callable[[], GithubInstallationTokenResponse]
    ) -> str:
        """Return the cached token, minting one via ``minter`` on miss.

        Single-lock section around both the slot check and the
        ``minter`` invocation. On a warm hit the lock is acquired once,
        the slot is returned, and no network IO fires. On a cold miss
        the lock is held across the mint so concurrent cache-miss
        callers see the ``minter`` invoked EXACTLY ONCE — the runner's
        design (§3.9 clarify answer 3) accepts the serialization
        tradeoff because every task's mint fires once at task start
        and the wall-time cap (10 minutes per Invariant 5) is orders
        of magnitude larger than the ~200ms round-trip.
        """
        with self._lock:
            if self._token is not None:
                return self._token
            # Mint inside the lock so the minter fires exactly once even
            # under contention. The task-lifetime cache design (design.md
            # §3.9 clarify answer 3) accepts the serialization tradeoff:
            # every task's mint fires at task start and the wall-time
            # cap of 10 minutes is orders of magnitude larger than the
            # ~200ms mint latency.
            response = minter()
            self._token = response.token
            return self._token

    def clear(self) -> None:
        """Zero the cache.

        Used by tests to reset state between cases, and reserved for a
        future explicit-refresh path if the task-lifetime assumption
        ever gets relaxed.
        """
        with self._lock:
            self._token = None

    def __repr__(self) -> str:
        """Redacted repr — never leaks the token value.

        Guards against Invariant 6 violations if a diagnostic printer
        (traceback frame renderer, REPL echo, ad-hoc ``print(cache)``)
        happens to render the cache instance. Reports only whether
        the slot is populated.
        """
        populated = self._token is not None
        return f"TokenCache(populated={populated})"


_CACHE: TokenCache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> TokenCache:
    """Return the module-level singleton :class:`TokenCache`.

    Constructs the singleton lazily under its own lock. The
    singleton-construction lock is separate from the instance's own
    :attr:`TokenCache._lock` — that way ``minter`` calls in
    :meth:`TokenCache.get_or_mint` do not hold this outer lock during
    network IO, and callers do not risk deadlocking against it.

    The double-checked pattern (check outside the lock, then again
    inside) avoids the fast-path lock cost on every call after the
    first, since the singleton lives for the process lifetime.
    """
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            if _CACHE is None:
                _CACHE = TokenCache()
    return _CACHE
