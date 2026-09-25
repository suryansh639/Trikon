"""httpx-based GitHub REST client with hand-rolled retry (design.md §3.3, §3.4).

Wraps the five GitHub REST endpoints the Fargate runner touches:

1. ``POST /app/installations/{id}/access_tokens`` — mint a JIT
   installation token (via an App JWT signed with ``PyJWT[crypto]``).
2. ``POST /repos/{repo}/check-runs`` — create a Check Run.
3. ``PATCH /repos/{repo}/check-runs/{id}`` — update an existing Check
   Run.
4. ``POST /repos/{repo}/issues/{pr_number}/comments`` — create a PR
   comment.
5. ``PATCH /repos/{repo}/issues/comments/{id}`` — update an existing PR
   comment.

Every call is wrapped in :func:`_retry_with_backoff` — exponential
backoff (base 1 s, factor 2, jitter ±20 %), three attempts, 30-second
total budget, retrying on ``httpx.HTTPError`` / HTTP 5xx / HTTP 429
and surfacing non-retryable 4xx immediately. Rejected ``tenacity``
because the retry surface is exactly five endpoints (design.md §3.9
clarify answer 4).

Public surface:

* :class:`GithubClient` — typed wrapper. Constructor accepts an
  injected :class:`httpx.Client` for tests; production callers pass
  ``None`` and get a lazily-constructed client with the standard
  Accept / API-version headers.
* :class:`GithubClientError` — raised on retry-budget exhaustion or
  non-retryable 4xx.

**Security invariants** (Invariant 6):

* The App private key, the signed JWT, and the installation token
  are never logged, never persisted, and never embedded in raised
  exception messages.
* The class does not maintain any per-installation token state —
  callers pass the installation token per-call so the same client
  instance can serve multiple installations if ever reused (the
  runner never does, but the design keeps that door open).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx
import jwt

from trikon_cloud.fargate_runner.logger import get_logger
from trikon_cloud.fargate_runner.models import (
    CheckRunCreatePayload,
    CheckRunUpdatePayload,
    GithubCheckRunResponse,
    GithubCommentResponse,
    GithubInstallationTokenResponse,
)

__all__ = ["GithubClient", "GithubClientError"]


_LOGGER = get_logger("trikon_cloud.fargate_runner.github_client")


class GithubClientError(Exception):
    """Raised when a GitHub API call fails after retries or returns non-retryable 4xx."""


# ---------------------------------------------------------------------------
# Retry helper (module-level; not part of the class so tests can exercise it
# without materializing a ``GithubClient`` instance).
# ---------------------------------------------------------------------------


def _retry_with_backoff(
    callable_: Callable[[], httpx.Response],
    *,
    max_attempts: int = 3,
    budget_seconds: float = 30.0,
) -> httpx.Response:
    """Retry ``callable_`` on transient failures (5xx or 429).

    Exponential backoff: base 1 s, factor 2, jitter ±20 % (design.md
    §3.4). Total cumulative sleep capped at ``budget_seconds`` — if
    the next planned sleep would exceed the remaining budget, the
    helper aborts and raises :class:`GithubClientError`.

    Retry policy (design.md §3.9 clarify answer 4):

    * ``httpx.HTTPError`` (network failure, timeout) → retry.
    * HTTP 5xx → retry.
    * HTTP 429 → retry.
    * Any other 4xx → surface immediately as
      :class:`GithubClientError`.
    * 2xx → return response.
    """
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            response = callable_()
        except httpx.HTTPError as exc:
            elapsed = time.monotonic() - start
            if attempt >= max_attempts or elapsed >= budget_seconds:
                raise GithubClientError(
                    f"GitHub API request failed after {attempt} attempts: {exc}"
                ) from exc
            _LOGGER.debug(
                "github request transient failure; retrying",
                attempt=attempt,
                error_type=type(exc).__name__,
            )
            _sleep_with_jitter(attempt, budget_seconds - elapsed)
            continue

        if 200 <= response.status_code < 300:
            return response
        if response.status_code == 429 or 500 <= response.status_code < 600:
            elapsed = time.monotonic() - start
            if attempt >= max_attempts or elapsed >= budget_seconds:
                raise GithubClientError(
                    f"GitHub API returned {response.status_code} "
                    f"after {attempt} attempts"
                )
            _LOGGER.debug(
                "github request transient status; retrying",
                attempt=attempt,
                status_code=response.status_code,
            )
            _sleep_with_jitter(attempt, budget_seconds - elapsed)
            continue
        # Non-retryable 4xx — surface immediately. The response body
        # is deliberately NOT included in the exception message; the
        # caller's ``except`` in ``entrypoint.main()`` handles the
        # Never_Fail_Open path without needing the body.
        raise GithubClientError(
            f"GitHub API returned non-retryable {response.status_code}"
        )


def _sleep_with_jitter(attempt: int, budget_remaining: float) -> None:
    """Sleep for a backoff duration with ±20 % jitter.

    Backoff is ``2 ** (attempt - 1)`` seconds (1, 2, 4, ...) multiplied
    by a jitter factor uniformly sampled from ``[0.8, 1.2]``. The
    sleep is capped by ``budget_remaining`` so the helper never sleeps
    past the retry budget.
    """
    base = 2 ** (attempt - 1)
    jitter = random.uniform(0.8, 1.2)
    duration = min(base * jitter, budget_remaining)
    if duration > 0:
        time.sleep(duration)


class GithubClient:
    """Typed httpx wrapper for the five GitHub REST endpoints the runner touches.

    Every method wraps its underlying ``self._client.<verb>(...)``
    call in :func:`_retry_with_backoff`. The class does not maintain
    the installation token — callers pass it per-call so a single
    client instance can serve multiple installations if ever reused
    (the runner never does, but the design keeps that door open).

    ``http_client`` is a dependency-injection seam for tests
    (typically a ``respx.MockRouter``-backed :class:`httpx.Client`).
    When ``None`` (production), the class constructs one with the
    standard Accept / API-version headers and a 30-second read /
    10-second connect timeout.
    """

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        if http_client is not None:
            self._client = http_client
        else:
            self._client = httpx.Client(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

    def mint_installation_token(
        self,
        *,
        installation_id: int,
        app_id: int,
        private_key_pem: bytes,
    ) -> GithubInstallationTokenResponse:
        """POST ``/app/installations/{id}/access_tokens``.

        Builds a short-lived App JWT (``iat = now - 60`` for clock-skew
        tolerance, ``exp = now + 600`` — the maximum GitHub accepts)
        and exchanges it for an installation-scoped token. The JWT
        and the response body are never logged.
        """
        now = int(time.time())
        # PyJWT >= 2.10 rejects a non-string ``iss`` claim with
        # ``TypeError("Issuer (iss) must be a string.")`` at
        # :func:`jwt.encode`. GitHub accepts either form on the wire, so
        # stringify the numeric App ID at construction to keep the encoder
        # happy across PyJWT versions.
        jwt_payload = {"iss": str(app_id), "iat": now - 60, "exp": now + 600}
        signed_jwt = jwt.encode(jwt_payload, private_key_pem, algorithm="RS256")

        response = _retry_with_backoff(
            lambda: self._client.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {signed_jwt}"},
            )
        )
        return GithubInstallationTokenResponse.model_validate_json(response.content)

    def create_check_run(
        self,
        *,
        installation_token: str,
        repo_full_name: str,
        body: CheckRunCreatePayload,
    ) -> GithubCheckRunResponse:
        """POST ``/repos/{repo}/check-runs`` — create a Check Run."""
        response = _retry_with_backoff(
            lambda: self._client.post(
                f"https://api.github.com/repos/{repo_full_name}/check-runs",
                headers={"Authorization": f"Bearer {installation_token}"},
                content=body.model_dump_json(),
            )
        )
        return GithubCheckRunResponse.model_validate_json(response.content)

    def patch_check_run(
        self,
        *,
        installation_token: str,
        repo_full_name: str,
        check_run_id: int,
        body: CheckRunUpdatePayload,
    ) -> GithubCheckRunResponse:
        """PATCH ``/repos/{repo}/check-runs/{id}`` — update an existing Check Run."""
        response = _retry_with_backoff(
            lambda: self._client.patch(
                f"https://api.github.com/repos/{repo_full_name}/check-runs/{check_run_id}",
                headers={"Authorization": f"Bearer {installation_token}"},
                content=body.model_dump_json(),
            )
        )
        return GithubCheckRunResponse.model_validate_json(response.content)

    def create_pr_comment(
        self,
        *,
        installation_token: str,
        repo_full_name: str,
        pr_number: int,
        body: str,
    ) -> GithubCommentResponse:
        """POST ``/repos/{repo}/issues/{pr_number}/comments`` — create a PR comment."""
        response = _retry_with_backoff(
            lambda: self._client.post(
                f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments",
                headers={"Authorization": f"Bearer {installation_token}"},
                json={"body": body},
            )
        )
        return GithubCommentResponse.model_validate_json(response.content)

    def patch_pr_comment(
        self,
        *,
        installation_token: str,
        repo_full_name: str,
        comment_id: int,
        body: str,
    ) -> GithubCommentResponse:
        """PATCH ``/repos/{repo}/issues/comments/{id}`` — update an existing PR comment."""
        response = _retry_with_backoff(
            lambda: self._client.patch(
                f"https://api.github.com/repos/{repo_full_name}/issues/comments/{comment_id}",
                headers={"Authorization": f"Bearer {installation_token}"},
                json={"body": body},
            )
        )
        return GithubCommentResponse.model_validate_json(response.content)
