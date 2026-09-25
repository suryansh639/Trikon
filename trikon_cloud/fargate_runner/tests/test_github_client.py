# respx' route and call recorders surface ``Any``-typed request /
# response objects at the mock boundary, and the pydantic mypy plugin
# synthesizes ``__init__(self, **data: Any)`` on every model. Under the
# repo's ``disallow_any_explicit = true`` config both surface as
# ``explicit-any`` errors that refer to library-generated / boundary
# code, not to hand-written signatures. Silence at file scope — every
# ``Any`` here is bounded to the respx / pydantic fixture surface.
# mypy: disable-error-code="explicit-any"
"""Respx-mocked tests for :class:`GithubClient` (Wave-6, task 6.7).

Covers the five GitHub REST endpoints the client wraps plus the
:func:`_retry_with_backoff` transient-failure policy. Every test
intercepts httpx via ``respx.mock(base_url="https://api.github.com")``
so no live GitHub is contacted.

Load-bearing invariants exercised:

* **Retry budget** — three attempts on 5xx / 429, immediate raise on
  non-retryable 4xx, ``GithubClientError`` on budget exhaustion.
* **JWT contents** — the mint call sends ``Authorization: Bearer <jwt>``
  with an RS256-signed JWT whose ``iss`` matches the caller-supplied
  ``app_id`` (checked via ``jwt.decode(..., options={"verify_signature":
  False})`` — the signing key is generated inside ``conftest`` and the
  server side does not verify at the test boundary).
* **Header set** — every request carries ``Accept:
  application/vnd.github+json`` and ``X-GitHub-Api-Version: 2022-11-28``
  (design.md §5.4 / Requirement 3.1).
* **Invariant 6 (secrets never logged)** — the distinctive installation
  token is absent from every log call recorded by the monkeypatched
  ``_LOGGER``.

**Library-version compat notes.** One autouse fixture below bridges a
version-pin gap between the tests' assumptions and the installed
dependencies:

* :func:`_httpx_respx_method_compat` — httpx 0.28 kept the ``method``
  attribute as bytes when the underlying httpcore request carried
  bytes, breaking respx 0.21's ``Method eq 'POST'`` string matcher.
  The fixture patches :meth:`httpx.Request.__init__` to decode bytes
  methods to str so respx route patterns match.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest
import respx

from trikon_cloud.fargate_runner import github_client
from trikon_cloud.fargate_runner.github_client import (
    GithubClient,
    GithubClientError,
)
from trikon_cloud.fargate_runner.models import (
    CheckRunCreatePayload,
    CheckRunOutput,
    CheckRunUpdatePayload,
    GithubCheckRunResponse,
    GithubCommentResponse,
    GithubInstallationTokenResponse,
)

from .conftest import (
    APP_ID,
    APP_PRIVATE_KEY_PEM,
    INSTALLATION_ID,
    PR_NUMBER,
    REPO_FULL_NAME,
    make_check_run_response,
    make_comment_response,
    make_installation_token_response,
)

# ---------------------------------------------------------------------------
# Autouse compat fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :func:`_sleep_with_jitter` so retry tests do not actually sleep.

    Autouse — every test benefits, and no test in this module wants
    the real jittered sleep. Preserves the retry accounting (attempt
    count, budget arithmetic) while eliminating wall-clock cost.
    """
    monkeypatch.setattr(github_client, "_sleep_with_jitter", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _httpx_respx_method_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28's bytes-vs-str method mismatch.

    httpx 0.28 stopped auto-decoding a bytes ``method`` argument on
    :class:`httpx.Request` construction. httpcore passes the method
    as bytes when respx intercepts at the connection-pool layer, so
    the resulting :class:`httpx.Request` carries ``method=b"POST"``.
    respx 0.21's :class:`Method` matcher compares against the str
    ``"POST"`` and the route never resolves — every test then fails
    with :class:`AllMockedAssertionError`.

    Patch :meth:`httpx.Request.__init__` to decode bytes methods to
    str before delegating to the real init. Test-only shim — nothing
    in production ever constructs a request from a bytes method.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)


# ---------------------------------------------------------------------------
# Payload builders.
# ---------------------------------------------------------------------------


def _make_check_run_create_payload() -> CheckRunCreatePayload:
    """Return a canonical Check Run create body used by several tests."""
    return CheckRunCreatePayload(
        name="Trikon",
        head_sha="a" * 40,
        status="completed",
        conclusion="failure",
        output=CheckRunOutput(title="Test", summary="Test summary"),
        details_url="https://cloud.trikon.dev/audits/x",
    )


def _make_check_run_update_payload() -> CheckRunUpdatePayload:
    """Return a canonical Check Run update body — patch path exercised."""
    return CheckRunUpdatePayload(
        status="completed",
        conclusion="success",
        output=CheckRunOutput(title="Updated", summary="Updated summary"),
        details_url="https://cloud.trikon.dev/audits/x",
    )


# ---------------------------------------------------------------------------
# mint_installation_token — happy path, retry policy, header shape.
# ---------------------------------------------------------------------------


def test_mint_installation_token_success() -> None:
    """The happy path returns a validated response and sends a Bearer JWT.

    Verifies three load-bearing surfaces at once: (a) the response body
    validates against :class:`GithubInstallationTokenResponse`; (b) the
    request carries ``Authorization: Bearer <jwt>``; (c) the JWT
    decodes (signature-verification disabled at the test boundary)
    with ``iss`` equal to ``str(app_id)`` — the production client
    stringifies the numeric App ID at JWT-construction time (PyJWT
    2.10+ rejects a non-string ``iss`` at :func:`jwt.encode`).
    """
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        ).mock(
            return_value=httpx.Response(200, json=make_installation_token_response())
        )

        client = GithubClient()
        response = client.mint_installation_token(
            installation_id=INSTALLATION_ID,
            app_id=APP_ID,
            private_key_pem=APP_PRIVATE_KEY_PEM,
        )

        assert isinstance(response, GithubInstallationTokenResponse)
        assert response.token == "ghs_test_token_not_real"
        assert route.call_count == 1

        request = route.calls[-1].request
        auth = request.headers["Authorization"]
        assert auth.startswith("Bearer ")
        signed = auth.removeprefix("Bearer ")
        decoded: dict[str, Any] = jwt.decode(
            signed, options={"verify_signature": False}
        )
        # The production client stringifies the int app_id before
        # signing (PyJWT 2.10+ constraint), so the JWT payload's
        # ``iss`` claim is the str form.
        assert decoded["iss"] == str(APP_ID)


def test_mint_installation_token_retries_on_5xx() -> None:
    """500 → 500 → 200: the third attempt succeeds and the mock recorded 3 calls."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        ).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(200, json=make_installation_token_response()),
            ]
        )

        client = GithubClient()
        response = client.mint_installation_token(
            installation_id=INSTALLATION_ID,
            app_id=APP_ID,
            private_key_pem=APP_PRIVATE_KEY_PEM,
        )

        assert response.token == "ghs_test_token_not_real"
        assert route.call_count == 3


def test_mint_installation_token_raises_after_retry_budget() -> None:
    """Three 500s in a row exhaust the retry budget and raise the typed error."""
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        ).mock(return_value=httpx.Response(500))

        client = GithubClient()

        with pytest.raises(GithubClientError):
            client.mint_installation_token(
                installation_id=INSTALLATION_ID,
                app_id=APP_ID,
                private_key_pem=APP_PRIVATE_KEY_PEM,
            )

        assert route.call_count == 3


def test_mint_installation_token_raises_on_non_retryable_4xx() -> None:
    """401 is non-retryable — the client raises immediately without a retry.

    The retry policy in :func:`_retry_with_backoff` only retries on
    5xx / 429; every other 4xx surfaces immediately as
    :class:`GithubClientError`. Load-bearing because a 401 (revoked
    installation) must NOT waste the retry budget when the server has
    already declined.
    """
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        ).mock(return_value=httpx.Response(401))

        client = GithubClient()

        with pytest.raises(GithubClientError):
            client.mint_installation_token(
                installation_id=INSTALLATION_ID,
                app_id=APP_ID,
                private_key_pem=APP_PRIVATE_KEY_PEM,
            )

        assert route.call_count == 1


# ---------------------------------------------------------------------------
# create_check_run / patch_check_run — path + method + body shape.
# ---------------------------------------------------------------------------


def test_create_check_run_success() -> None:
    """The happy path posts to ``/repos/.../check-runs`` with the expected body.

    Verifies both response validation (``GithubCheckRunResponse.id ==
    111``) and request-body invariants — ``name == "Trikon"``
    (Invariant 7 at the wire level) and ``conclusion`` restricted to
    the three GitHub-accepted values.
    """
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(f"/repos/{REPO_FULL_NAME}/check-runs").mock(
            return_value=httpx.Response(200, json=make_check_run_response(111))
        )

        client = GithubClient()
        response = client.create_check_run(
            installation_token="ghs_test",
            repo_full_name=REPO_FULL_NAME,
            body=_make_check_run_create_payload(),
        )

        assert isinstance(response, GithubCheckRunResponse)
        assert response.id == 111
        assert route.call_count == 1

        body_bytes = route.calls[-1].request.content
        body_json: dict[str, Any] = json.loads(body_bytes)
        assert body_json["name"] == "Trikon"
        assert body_json["conclusion"] in {"success", "failure", "neutral"}


def test_patch_check_run_uses_patch_method_and_correct_path() -> None:
    """``patch_check_run`` targets ``PATCH /repos/.../check-runs/<id>``.

    Verifies the HTTP method (PATCH, not POST) and the exact path
    template — the check-run ID is appended as a path segment, not a
    query parameter or a request-body field.
    """
    check_run_id = 111
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.patch(
            f"/repos/{REPO_FULL_NAME}/check-runs/{check_run_id}"
        ).mock(return_value=httpx.Response(200, json=make_check_run_response(check_run_id)))

        client = GithubClient()
        response = client.patch_check_run(
            installation_token="ghs_test",
            repo_full_name=REPO_FULL_NAME,
            check_run_id=check_run_id,
            body=_make_check_run_update_payload(),
        )

        assert response.id == check_run_id
        assert route.call_count == 1

        request = route.calls[-1].request
        assert request.method == "PATCH"
        assert request.url.path == f"/repos/{REPO_FULL_NAME}/check-runs/{check_run_id}"


# ---------------------------------------------------------------------------
# create_pr_comment / patch_pr_comment.
# ---------------------------------------------------------------------------


def test_create_pr_comment_body_is_string() -> None:
    """The PR comment body is serialized as ``{"body": "..."}`` (JSON string).

    Regression guard: the model of a PR comment on the wire is a JSON
    object with a single ``body`` string key. If the client ever
    starts double-encoding (nested JSON) or omits the wrapper, this
    test catches it.
    """
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(
            f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments"
        ).mock(return_value=httpx.Response(200, json=make_comment_response(222)))

        client = GithubClient()
        response = client.create_pr_comment(
            installation_token="ghs_test",
            repo_full_name=REPO_FULL_NAME,
            pr_number=PR_NUMBER,
            body="test comment",
        )

        assert isinstance(response, GithubCommentResponse)
        assert response.id == 222

        body_bytes = route.calls[-1].request.content
        body_json: dict[str, Any] = json.loads(body_bytes)
        assert body_json == {"body": "test comment"}


def test_patch_pr_comment_path_uses_comment_id() -> None:
    """``patch_pr_comment`` targets ``PATCH /repos/.../issues/comments/<id>``.

    Note the shape of the PR-comment update URL: the comment ID is
    appended to ``/issues/comments/`` — NOT ``/issues/<pr_number>/
    comments/<id>`` (the update path does not need the PR number
    because comment IDs are globally unique per repo).
    """
    comment_id = 222
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.patch(
            f"/repos/{REPO_FULL_NAME}/issues/comments/{comment_id}"
        ).mock(return_value=httpx.Response(200, json=make_comment_response(comment_id)))

        client = GithubClient()
        response = client.patch_pr_comment(
            installation_token="ghs_test",
            repo_full_name=REPO_FULL_NAME,
            comment_id=comment_id,
            body="updated",
        )

        assert response.id == comment_id
        assert route.call_count == 1

        request = route.calls[-1].request
        assert request.method == "PATCH"
        assert request.url.path == f"/repos/{REPO_FULL_NAME}/issues/comments/{comment_id}"


# ---------------------------------------------------------------------------
# Header set — Accept + X-GitHub-Api-Version.
# ---------------------------------------------------------------------------


def test_client_headers_include_accept_and_api_version() -> None:
    """The default client wire header set is design.md §5.4 verbatim.

    Both headers matter operationally: ``Accept:
    application/vnd.github+json`` is GitHub's recommended MIME type
    for machine-readable responses; ``X-GitHub-Api-Version:
    2022-11-28`` pins the stable REST vintage so a future default-
    version shift on GitHub's end cannot silently change response
    shapes.
    """
    with respx.mock(base_url="https://api.github.com") as respx_mock:
        route = respx_mock.post(f"/repos/{REPO_FULL_NAME}/check-runs").mock(
            return_value=httpx.Response(200, json=make_check_run_response(111))
        )

        client = GithubClient()
        client.create_check_run(
            installation_token="ghs_test",
            repo_full_name=REPO_FULL_NAME,
            body=_make_check_run_create_payload(),
        )

        request = route.calls[-1].request
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# Invariant 6 — installation token never enters the observability plane.
# ---------------------------------------------------------------------------


def test_installation_token_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No log call at any level contains the distinctive token marker.

    Monkeypatches every level of ``github_client._LOGGER`` to record
    every ``(msg, args, kwargs)`` tuple. Drives a request through the
    retry path (500 → 200) so at least one debug log fires inside
    :func:`_retry_with_backoff`. Iterates every recorded call and
    asserts the marker string does not appear in ``msg`` or in any
    positional / keyword argument value — the load-bearing Invariant 6
    assertion.
    """
    marker = "ghs_secret_marker_do_not_leak"
    records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _capture(method_name: str) -> Any:
        def _record(msg: str, *args: Any, **kwargs: Any) -> None:
            records.append((method_name, (msg, *args), kwargs))

        return _record

    monkeypatch.setattr(github_client._LOGGER, "debug", _capture("debug"))
    monkeypatch.setattr(github_client._LOGGER, "info", _capture("info"))
    monkeypatch.setattr(github_client._LOGGER, "warning", _capture("warning"))
    monkeypatch.setattr(github_client._LOGGER, "error", _capture("error"))

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.post(f"/repos/{REPO_FULL_NAME}/check-runs").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, json=make_check_run_response(111)),
            ]
        )

        client = GithubClient()
        client.create_check_run(
            installation_token=marker,
            repo_full_name=REPO_FULL_NAME,
            body=_make_check_run_create_payload(),
        )

    # The retry path must have fired at least one debug log — otherwise
    # the assertion below is vacuously true and the test is not actually
    # exercising Invariant 6.
    assert any(level == "debug" for level, _args, _kwargs in records), (
        "expected at least one debug log call on the 500 → 200 retry path"
    )

    for _level, args, kwargs in records:
        for arg in args:
            assert marker not in repr(arg), f"marker leaked into log arg: {arg!r}"
        for value in kwargs.values():
            assert marker not in repr(value), (
                f"marker leaked into log kwarg: {value!r}"
            )
