# respx captures request bodies as bytes / dicts at the mock boundary; moto's
# boto3 clients return ``dict[str, Any]`` for every attribute-value shape;
# pydantic's mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# :class:`BaseModel` subclass. Under this repo's
# ``disallow_any_explicit = true`` config, each of those surfaces as an
# ``explicit-any`` error referring to library-generated / boundary code, not
# to hand-written signatures. Suppress at file scope — every ``Any`` in this
# module is bounded to the respx / moto / pydantic fixture surface, never
# crossed into a production module.
# mypy: disable-error-code="explicit-any"
"""Entrypoint property tests + per-path §12 tests (Wave-6, task 6.9).

Encodes the two cross-cutting properties from design.md §11 plus
targeted per-path tests from the §12 fourteen-path enumeration.

* **Property 1 — Idempotency_On_Natural_Key** (Requirements 5.2, 5.3,
  11.1, 11.2, 11.3). A duplicate ``(installation_id, sk)`` PutItem
  short-circuits GitHub posts and returns exit 0. The load-bearing
  mechanism is DynamoDB's conditional PutItem raising
  ``ConditionalCheckFailedException`` — translated by
  :class:`~trikon_cloud.fargate_runner.dynamodb_writer.DynamoDBWriter`
  into :class:`~trikon_cloud.fargate_runner.dynamodb_writer.VerdictAlreadyCommittedError`,
  which the entrypoint catches and returns 0 without posting to
  GitHub (§12 path 7).

* **Property 2 — Never_Fail_Open_Closure** (Requirements 10.1-10.6,
  12.2). Every exception on the 12-step flow surfaces exit code 1.
  Parametrized over three representative injection points — SDK
  verify raise, git operation raise, GitHub client mint raise — so
  each pre-persist, mid-persist, and post-persist branch of §12 is
  exercised. There is NO code path from any exception to exit 0
  (the Property 2 closure), and NO synthetic ``allow`` verdict is
  ever emitted on the fail-closed surface.

* **§12 per-path spot checks**. Directly exercise §12 paths 1, 2, 3,
  4, and 11. Paths 7 and 8 (DynamoDB semantics) live in
  ``test_dynamodb_writer.py``; paths 3 and 10 (GitHub retry
  budgets) live in ``test_github_client.py``. This module
  concentrates on flow-composition assertions that only surface
  through :func:`main`.

* **Invariant spot checks**. Check Run ``name == "Trikon"``
  (Invariant 7); ``sdk.verify(no_sandbox=True)`` (Requirement 1.6,
  the load-bearing SDK-integration invariant); Property 3
  byte-identical summary parity at the entrypoint boundary
  (Property 3's rigorous encoding lives in
  ``test_summary_builder.py``; this module spot-checks that the
  entrypoint wires the single ``summary`` local to both GitHub
  writes without reformatting).

Every test intercepts httpx via ``respx.mock`` and AWS via
``@mock_aws``. No live network, no live git remote, no real Docker
daemon.

**Library-version compat notes.** One autouse fixture below bridges
a version-pin gap between the tests' assumptions and the installed
dependencies:

* :func:`_httpx_respx_method_compat` — httpx 0.28 kept the
  ``method`` attribute as bytes when the underlying httpcore
  request carried bytes, breaking respx 0.21's ``Method eq 'POST'``
  string matcher. The fixture patches
  :meth:`httpx.Request.__init__` to decode bytes methods to str
  so respx route patterns match. Same shim as in
  ``test_github_client.py``.
"""

from __future__ import annotations

import json
import signal
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
import respx
from moto import mock_aws

from trikon.evidence.report import Verdict
from trikon_cloud.fargate_runner import entrypoint, git_ops, github_client
from trikon_cloud.fargate_runner.dynamodb_writer import (
    DynamoDBWriter,
    VerdictAlreadyCommittedError,
)
from trikon_cloud.fargate_runner.git_ops import GitOpsError
from trikon_cloud.fargate_runner.github_client import GithubClientError

from .conftest import (
    APP_PRIVATE_KEY_PEM,
    INSTALLATION_ID,
    PR_NUMBER,
    REPO_FULL_NAME,
    make_check_run_response,
    make_comment_response,
    make_env_vars,
    make_installation_token_response,
    make_verdict,
)

# ---------------------------------------------------------------------------
# Autouse fixtures — every test in this module benefits.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the process-lifetime installation-token singleton.

    :mod:`trikon_cloud.fargate_runner.token_cache` maintains a
    module-level ``_CACHE`` slot. Once a test mints a token, the next
    test's mint call would take the fast-path warm hit and never
    contact the respx mock — the mock's ``call_count`` would then be
    off by one and route-match assertions would fail. Autouse
    :meth:`~pytest.MonkeyPatch.setattr` clears the slot before every
    test and restores the prior value on teardown.
    """
    monkeypatch.setattr(
        "trikon_cloud.fargate_runner.token_cache._CACHE", None
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :func:`~github_client._sleep_with_jitter` to a no-op.

    The retry-exhaustion tests below drive three 500 responses through
    :func:`_retry_with_backoff`. With the real jittered sleep, that
    burns ~7 seconds per retry cycle. Autouse — every test benefits
    and no test in this module wants the real sleep.
    """
    monkeypatch.setattr(
        "trikon_cloud.fargate_runner.github_client._sleep_with_jitter",
        lambda *_a, **_kw: None,
    )


@pytest.fixture(autouse=True)
def _httpx_respx_method_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28's bytes-vs-str method mismatch.

    httpx 0.28 stopped auto-decoding a bytes ``method`` argument on
    :class:`httpx.Request` construction. httpcore passes the method
    as bytes when respx intercepts at the connection-pool layer, so
    the resulting :class:`httpx.Request` carries ``method=b"POST"``,
    which never matches respx 0.21's string-typed ``Method eq 'POST'``
    matcher.

    The fix is a one-line pre-decode: wrap
    :meth:`httpx.Request.__init__` so a bytes ``method`` becomes an
    ASCII str before the parent constructor stores it. Identical to
    the shim in ``test_github_client.py``.
    """
    real_init = httpx.Request.__init__

    def _init(
        self: httpx.Request, method: Any, *args: Any, **kwargs: Any
    ) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)


@pytest.fixture(autouse=True)
def _neutralize_signal_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent :func:`signal.alarm` from actually scheduling SIGALRM.

    :func:`entrypoint.main` calls
    ``signal.signal(signal.SIGALRM, _on_alarm)`` and
    ``signal.alarm(600)`` at process start. On POSIX this schedules a
    real 10-minute timer — tests finish long before that, but the
    alarm state can leak between processes and disturb debuggers.
    On Windows :attr:`signal.SIGALRM` does not exist at all, so the
    entrypoint's ``signal.SIGALRM`` attribute access would raise
    :class:`AttributeError` before ``signal.signal`` is invoked.

    Autouse fixture that (a) provides a stub ``SIGALRM`` attribute on
    Windows so attribute lookup succeeds, and (b) patches
    :func:`signal.signal` and :func:`signal.alarm` to no-ops so the
    real handler is never installed.
    """
    if not hasattr(signal, "SIGALRM"):
        monkeypatch.setattr(signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(signal, "signal", lambda *_a, **_kw: None)
    monkeypatch.setattr(signal, "alarm", lambda *_a, **_kw: 0, raising=False)


@pytest.fixture(autouse=True)
def _clear_structlog_contextvars() -> None:
    """Clear structlog context between tests.

    :func:`entrypoint.main` calls
    :func:`structlog.contextvars.bind_contextvars` with the
    invocation-scoped fields (``installation_id``, ``pr_number``,
    etc.). Without a clear step, values leak into the next test's
    log records and complicate log-based debugging.
    """
    import structlog.contextvars

    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Test setup helpers — shared across the property + per-path tests.
# ---------------------------------------------------------------------------


def _setup_env(
    monkeypatch: pytest.MonkeyPatch, **overrides: str
) -> None:
    """Set the memo §5.4 env-var contract + AWS test credentials.

    ``make_env_vars()`` returns the six memo-§5.4 core fields plus
    ``AWS_REGION``. We augment with moto-friendly credential env vars
    and let callers override individual entries (used by ``test_path_1``
    to force a :class:`~pydantic.ValidationError`).

    ``ECS_CONTAINER_METADATA_URI_V4`` is deleted so
    :func:`~entrypoint._get_fargate_task_arn` short-circuits to
    ``"local"`` without attempting an HTTP GET.
    """
    env = make_env_vars()
    env["AWS_ACCESS_KEY_ID"] = "testing"
    env["AWS_SECRET_ACCESS_KEY"] = "testing"
    env["AWS_SESSION_TOKEN"] = "testing"
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)


def _setup_aws_resources() -> tuple[str, str, str, str]:
    """Provision moto-backed DynamoDB tables + S3 bucket + Secret.

    MUST be called inside an active ``@mock_aws()`` decorator scope.
    Returns ``(verdicts_table, pr_state_table, evidence_bucket,
    secret_arn)``. The Secrets Manager secret stores the fixture
    RSA PEM so :func:`~entrypoint._fetch_app_private_key` retrieves
    a valid key for :func:`~github_client.GithubClient.mint_installation_token`.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="trikon_verdicts",
        KeySchema=[
            {"AttributeName": "installation_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "installation_id", "AttributeType": "N"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName="trikon_pr_state",
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pr_key", "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="trikon-cloud-evidence")

    sm = boto3.client("secretsmanager", region_name="us-east-1")
    resp = sm.create_secret(
        Name="trikon-cloud/github-app-private-key",
        SecretString=APP_PRIVATE_KEY_PEM.decode("utf-8"),
    )
    secret_arn: str = resp["ARN"]
    return (
        "trikon_verdicts",
        "trikon_pr_state",
        "trikon-cloud-evidence",
        secret_arn,
    )


def _mock_github_endpoints(
    respx_mock: respx.MockRouter,
    *,
    check_run_id: int = 111,
    comment_id: int = 222,
    installation_id: int = INSTALLATION_ID,
    repo: str = REPO_FULL_NAME,
    pr: int = PR_NUMBER,
) -> dict[str, respx.Route]:
    """Mock the five GitHub REST endpoints with canonical responses.

    Returns a name-keyed dict of :class:`respx.Route` handles so tests
    can inspect ``.called`` / ``.call_count`` / ``.calls[0].request``
    for individual endpoints without hardcoding URL paths.
    """
    routes: dict[str, respx.Route] = {}
    routes["mint"] = respx_mock.post(
        f"/app/installations/{installation_id}/access_tokens"
    ).mock(
        return_value=httpx.Response(
            200, json=make_installation_token_response()
        )
    )
    routes["create_check_run"] = respx_mock.post(
        f"/repos/{repo}/check-runs"
    ).mock(
        return_value=httpx.Response(
            200, json=make_check_run_response(check_run_id)
        )
    )
    routes["patch_check_run"] = respx_mock.patch(
        f"/repos/{repo}/check-runs/{check_run_id}"
    ).mock(
        return_value=httpx.Response(
            200, json=make_check_run_response(check_run_id)
        )
    )
    routes["create_comment"] = respx_mock.post(
        f"/repos/{repo}/issues/{pr}/comments"
    ).mock(
        return_value=httpx.Response(
            200, json=make_comment_response(comment_id)
        )
    )
    routes["patch_comment"] = respx_mock.patch(
        f"/repos/{repo}/issues/comments/{comment_id}"
    ).mock(
        return_value=httpx.Response(
            200, json=make_comment_response(comment_id)
        )
    )
    return routes


def _patch_git_ops(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> None:
    """Patch :func:`~git_ops.shallow_fetch_and_checkout` to a no-op or raise.

    When ``fail=False`` the stub returns ``None`` without touching
    subprocess or the filesystem. When ``fail=True`` the stub raises
    :class:`GitOpsError` — used by §12 path 4's test to force the
    fail-closed handler.
    """

    def _stub(**_kwargs: Any) -> None:
        if fail:
            raise GitOpsError("git fetch head failed: exit 1")

    monkeypatch.setattr(git_ops, "shallow_fetch_and_checkout", _stub)


def _patch_sdk_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: Verdict | None = None,
    exc: BaseException | None = None,
    capture_kwargs: dict[str, Any] | None = None,
) -> None:
    """Patch :func:`trikon.sdk.verify` to return ``verdict`` or raise ``exc``.

    Called by every test that goes through step 5. ``capture_kwargs``
    is an optional out-dict that receives the kwargs the entrypoint
    passed to :func:`~trikon.sdk.verify` — used by the
    ``no_sandbox=True`` invariant test (Requirement 1.6).
    """
    if exc is not None:
        def _stub_raise(**_kwargs: Any) -> Verdict:
            raise exc

        monkeypatch.setattr("trikon.sdk.verify", _stub_raise)
        return

    v = verdict if verdict is not None else make_verdict()

    def _stub_return(**kwargs: Any) -> Verdict:
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
        return v

    monkeypatch.setattr("trikon.sdk.verify", _stub_return)


def _scan_verdicts_table() -> list[dict[str, Any]]:
    """Return every item in the moto ``trikon_verdicts`` table.

    Convenience wrapper around a full-table ``scan`` — the test
    corpus never has enough rows for pagination to matter.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    return list(dynamodb.scan(TableName="trikon_verdicts").get("Items", []))


# ---------------------------------------------------------------------------
# Property 1 — Idempotency_On_Natural_Key.
# ---------------------------------------------------------------------------


@mock_aws
def test_property_idempotency_on_natural_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-fargate-runner, Property 1: Idempotency_On_Natural_Key.

    Validates: Requirements 5.2, 5.3, 11.1, 11.2, 11.3.

    The natural-key idempotency guard sits at DynamoDB's conditional
    :meth:`~DynamoDBWriter.put_verdict` — a duplicate
    ``(installation_id, sk)`` PutItem raises
    ``ConditionalCheckFailedException`` which the writer translates
    into :class:`VerdictAlreadyCommittedError`. The entrypoint catches
    that exception, logs INFO, and returns exit 0 without posting to
    GitHub.

    A literal re-invocation of :func:`main` is not directly testable
    because ``sk`` embeds ``pr_ts`` (a wall-clock timestamp captured
    fresh at each container start) — two runs of :func:`main` in the
    same process produce different ``sk`` values. The natural-key
    dedup semantics therefore surface via a monkeypatched
    :meth:`~DynamoDBWriter.put_verdict` that raises
    :class:`VerdictAlreadyCommittedError` on the first call, standing
    in for a prior invocation's committed row. The assertions verify
    the invariant surface visible to a customer: exit 0, and zero
    Check Run / PR comment posts.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    # Simulate a prior invocation's committed row: the conditional
    # PutItem fires ``ConditionalCheckFailedException``, translated
    # by the writer into :class:`VerdictAlreadyCommittedError`.
    def _put_raises_committed(
        _self: DynamoDBWriter, *, row: Any
    ) -> None:
        del row  # unused — exception is unconditional
        raise VerdictAlreadyCommittedError(
            "simulated prior invocation's committed row"
        )

    monkeypatch.setattr(
        DynamoDBWriter, "put_verdict", _put_raises_committed
    )

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    # Invariant a: exit 0 (idempotency win short-circuits).
    assert exit_code == 0
    # Invariant b: NO Check Run post (the runner returned before
    # step 9). ``mint`` may or may not fire depending on whether
    # step 3 executed before step 7 — we don't assert on it here.
    assert routes["create_check_run"].call_count == 0
    assert routes["patch_check_run"].call_count == 0
    # Invariant c: NO PR comment post.
    assert routes["create_comment"].call_count == 0
    assert routes["patch_comment"].call_count == 0


# ---------------------------------------------------------------------------
# Property 2 — Never_Fail_Open_Closure (parametrized).
# ---------------------------------------------------------------------------


# Injection descriptors: (name, target_kind, exception).
# ``target_kind`` names the surface being patched — the test body
# routes to the right :meth:`~pytest.MonkeyPatch.setattr` call site.
_INJECTIONS: list[tuple[str, str, BaseException]] = [
    (
        "sdk_verify_raises",
        "sdk_verify",
        RuntimeError("simulated SDK failure at step 5"),
    ),
    (
        "git_ops_raises",
        "git_ops",
        GitOpsError("simulated git failure at step 4"),
    ),
    (
        "github_mint_raises",
        "mint",
        GithubClientError("simulated GitHub mint failure at step 3"),
    ),
]


@mock_aws
@pytest.mark.parametrize(
    "injection",
    _INJECTIONS,
    ids=[t[0] for t in _INJECTIONS],
)
def test_property_never_fail_open_closure(
    injection: tuple[str, str, BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-fargate-runner, Property 2: Never_Fail_Open_Closure.

    Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 12.2.

    Parametrized over three representative injection points that
    cover the pre-persist (step 3 mint, step 4 git) and mid-flow
    (step 5 SDK verify) branches of §12. For every injection the
    invariant is exit code 1 — no code path from any exception on
    the 12-step flow returns 0. The fail-closed handler's
    best-effort verdict-persist + best-effort Check Run post are
    tolerated as best-effort per Requirement 10.6; only the exit
    code is Property-2-bearing.
    """
    _name, kind, exc = injection

    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    # Defaults for the two non-injected surfaces.
    _patch_git_ops(monkeypatch, fail=(kind == "git_ops"))
    if kind == "sdk_verify":
        _patch_sdk_verify(monkeypatch, exc=exc)
    else:
        _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    if kind == "mint":
        # Patch on the class so both the main-path mint AND the
        # fail-closed best-effort mint hit the exception. The
        # fail-closed handler wraps its own mint in
        # ``try / except Exception`` so this still routes to
        # exit 1 rather than bubbling out of :func:`main`.
        def _stub_mint(_self: Any, **_kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr(
            github_client.GithubClient,
            "mint_installation_token",
            _stub_mint,
        )

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    # Property 2's load-bearing assertion: exit 1 on every
    # exception surface. The three injections exercise steps 3, 4,
    # and 5 — a representative cover of the §12 path tree.
    assert exit_code == 1

    # Property 2 supplemental: NO row in ``trikon_verdicts`` carries
    # ``decision == "allow"``. The fail-closed synth is always
    # ``require_human`` (mapped to Check Run ``conclusion="neutral"``);
    # for pre-persist failures the row may or may not exist, but the
    # ``allow`` value is never emitted by the fail-closed handler.
    rows = _scan_verdicts_table()
    for row in rows:
        assert row["decision"]["S"] != "allow"


# ---------------------------------------------------------------------------
# §12 per-path spot checks.
# ---------------------------------------------------------------------------


def test_path_1_validation_error_on_missing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12 path 1: missing env → :class:`ValidationError` → exit 1.

    ``TRIKON_INSTALLATION_ID`` is a required (no-default) field on
    :class:`RunnerEnvConfig`. Deleting it forces the pydantic-settings
    loader to raise :class:`pydantic.ValidationError`, which the
    entrypoint's :func:`_fail_closed_no_env` catches and logs to
    stderr before returning 1. No AWS or GitHub calls fire on this
    path — structlog is not even configured yet.
    """
    _setup_env(monkeypatch)
    monkeypatch.delenv("TRIKON_INSTALLATION_ID")

    exit_code = entrypoint.main()

    assert exit_code == 1


@mock_aws
def test_path_2_secrets_manager_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12 path 2: Secrets Manager :meth:`get_secret_value` fails → exit 1.

    We provision the DynamoDB tables + S3 bucket but deliberately do
    NOT create the referenced secret. moto returns
    ``ResourceNotFoundException`` on the :meth:`get_secret_value`
    call. The exception propagates from
    :func:`~entrypoint._fetch_app_private_key`, is caught by
    :func:`main`'s top-level handler, and :func:`_fail_closed`
    returns 1. The fail-closed handler's own re-fetch attempt is
    isolated in a nested try/except so a second Secrets Manager
    failure does not crash the handler.
    """
    _setup_env(monkeypatch)
    _setup_aws_resources()
    # Override with a non-existent secret ARN so moto raises
    # ``ResourceNotFoundException``.
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:123:secret:does-not-exist",
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 1


@mock_aws
def test_path_3_installation_token_mint_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12 path 3: mint POST returns 401 → :class:`GithubClientError` → exit 1.

    :func:`~github_client._retry_with_backoff` treats 401 as a
    non-retryable 4xx and surfaces :class:`GithubClientError`
    immediately (no retry). The exception bubbles up through the
    token-cache minter callable and out of the main try / except
    into :func:`_fail_closed`, which returns 1.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        # Override the mint route with a 401 response. The other
        # routes are populated for the fail-closed best-effort post,
        # which may or may not fire depending on ordering.
        _mock_github_endpoints(respx_mock)
        respx_mock.post(
            f"/app/installations/{INSTALLATION_ID}/access_tokens"
        ).mock(return_value=httpx.Response(401, json={"message": "bad jwt"}))

        exit_code = entrypoint.main()

    assert exit_code == 1


@mock_aws
def test_path_4_git_fetch_fails_returns_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12 path 4: :func:`shallow_fetch_and_checkout` raises → exit 1.

    :func:`_patch_git_ops(fail=True)` stubs the git surface to raise
    :class:`GitOpsError` on invocation. The main-path try / except
    routes to :func:`_fail_closed` which best-effort posts a
    ``neutral`` Check Run and returns 1.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch, fail=True)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 1


@mock_aws
def test_path_11_pr_comment_fails_but_check_run_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12 path 11: PR comment retries exhausted → exit 0 (best-effort degrade).

    Validates: Requirement 8.5.

    The Check Run is the primary customer-visible surface. When the
    PR comment POST fails after retry exhaustion but the Check Run
    was successfully posted, the entrypoint logs ERROR and continues
    to the ``pr_state`` upsert, then returns 0. A subsequent
    invocation on the same head_sha will retry the comment on
    the fresh POST path (``prior_pr_state.last_comment_id is None``).
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        # Override the PR-comment POST route with a 500 responder.
        # With ``_sleep_with_jitter`` patched to no-op, the three
        # retry attempts fire back-to-back; the third exhausts the
        # budget and :class:`GithubClientError` is raised, caught by
        # the entrypoint's comment-scoped ``try/except``.
        comment_500 = respx_mock.post(
            f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments"
        ).mock(return_value=httpx.Response(500, json={"message": "boom"}))

        exit_code = entrypoint.main()

    # Best-effort degrade: exit 0 because the Check Run posted
    # successfully.
    assert exit_code == 0
    # The Check Run POST fired exactly once.
    assert routes["create_check_run"].call_count == 1
    # The PR-comment POST fired three times (retry-budget
    # exhaustion) — one for the initial + two retries.
    assert comment_500.call_count == 3


# ---------------------------------------------------------------------------
# Happy path + invariant spot checks.
# ---------------------------------------------------------------------------


@mock_aws
def test_happy_path_writes_verdict_and_posts_both_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: exit 0, 1 verdict row, 1 Check Run POST, 1 PR comment POST.

    Exercises the 12-step flow end-to-end against moto + respx with
    the canonical fixture data. Verifies the four observable
    invariants of a successful run: (a) exit 0, (b) exactly one row
    in ``trikon_verdicts``, (c) exactly one Check Run POST, and
    (d) exactly one PR comment POST. Serves as the flow-composition
    smoke test that the per-module tests cannot cover.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0
    assert routes["mint"].call_count == 1
    assert routes["create_check_run"].call_count == 1
    assert routes["create_comment"].call_count == 1

    # Exactly one verdict row committed.
    rows = _scan_verdicts_table()
    assert len(rows) == 1
    assert rows[0]["decision"]["S"] == "block"
    assert rows[0]["installation_id"]["N"] == str(INSTALLATION_ID)


@mock_aws
def test_happy_path_check_run_name_is_trikon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 7 spot check: Check Run ``name`` == ``"Trikon"``.

    Enforced at the type level by
    :class:`~trikon_cloud.fargate_runner.models.CheckRunCreatePayload`'s
    ``name: Literal["Trikon"]`` — this test verifies the value
    survives the wire round-trip. The payload is captured from
    respx and JSON-decoded.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0
    assert routes["create_check_run"].call_count == 1
    check_run_body = json.loads(
        routes["create_check_run"].calls[0].request.content
    )
    assert check_run_body["name"] == "Trikon"


@mock_aws
def test_no_sandbox_true_passed_to_sdk_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1.6: the entrypoint calls ``sdk.verify(no_sandbox=True)``.

    The load-bearing SDK-integration invariant: the Fargate task IS
    the sandbox (design.md §3.9 clarify answer 1), so the SDK's
    ``LocalDockerSandbox`` must NOT fire. A refactor that flips
    ``no_sandbox`` to ``False`` or drops the kwarg entirely would
    silently reintroduce Docker-in-Docker inside the Fargate task —
    a Docker-daemon dependency the container image does not carry.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_sdk_verify(
        monkeypatch,
        verdict=make_verdict(),
        capture_kwargs=captured,
    )

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0
    # Load-bearing invariant: ``no_sandbox=True`` was passed.
    assert captured.get("no_sandbox") is True
    # And the natural-key context matches the env:
    assert captured.get("base_sha") == make_env_vars()["TRIKON_BASE_SHA"]
    assert captured.get("head_sha") == make_env_vars()["TRIKON_HEAD_SHA"]


@mock_aws
def test_summary_content_parity_at_entrypoint_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 3 spot check: Check Run summary == PR comment body byte-identical.

    Validates: Requirements 7.1, 8.1, 8.3.

    Property 3's rigorous encoding (hypothesis-driven over a
    generated :class:`Verdict`) lives in ``test_summary_builder.py``.
    This test spot-checks the entrypoint-level wiring: the single
    ``summary`` local from :func:`render_summary` reaches both the
    Check Run's ``output.summary`` and the PR comment's ``body``
    without mutation. A future refactor that accidentally introduces
    two :func:`render_summary` calls with different arguments — or
    reformats one leg — is caught here.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0
    check_run_body = json.loads(
        routes["create_check_run"].calls[0].request.content
    )
    comment_body = json.loads(
        routes["create_comment"].calls[0].request.content
    )
    # Byte-identical parity: the Check Run's ``output.summary`` is
    # the same string the PR comment's ``body`` receives.
    assert check_run_body["output"]["summary"] == comment_body["body"]
    # And the summary is not the empty string (guard against a
    # future null-render regression).
    assert len(check_run_body["output"]["summary"]) > 0


@mock_aws
def test_happy_path_conclusion_maps_from_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check Run ``conclusion`` follows the decision → conclusion mapping.

    ``allow`` → ``success``; ``block`` → ``failure``; ``require_human``
    → ``neutral``. The mapping is a single dict at module scope in
    :mod:`~trikon_cloud.fargate_runner.entrypoint`. Verifying it
    here catches an accidental rebinding of ``_CONCLUSION_BY_DECISION``
    that would otherwise only surface in production.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    # A ``block`` decision → ``failure`` on the wire.
    _patch_sdk_verify(monkeypatch, verdict=make_verdict(decision="block"))

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        routes = _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0
    check_run_body = json.loads(
        routes["create_check_run"].calls[0].request.content
    )
    assert check_run_body["conclusion"] == "failure"


@mock_aws
def test_happy_path_pr_state_upsert_populates_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a happy path, ``trikon_pr_state`` carries both artifact IDs.

    Verifies Requirement 9.3 at the entrypoint level: after a
    successful run, ``last_check_run_id`` and ``last_comment_id``
    are both populated (not ``NULL``) and ``last_head_sha`` matches
    the run's ``head_sha``. The next invocation on the same
    head_sha will therefore take the PATCH-in-place branches for
    both artifacts.
    """
    _setup_env(monkeypatch)
    _, _, _, secret_arn = _setup_aws_resources()
    monkeypatch.setenv(
        "TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn
    )

    _patch_git_ops(monkeypatch)
    _patch_sdk_verify(monkeypatch, verdict=make_verdict())

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_github_endpoints(respx_mock)
        exit_code = entrypoint.main()

    assert exit_code == 0

    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    pr_key = f"{INSTALLATION_ID}#{REPO_FULL_NAME}#{PR_NUMBER}"
    resp = dynamodb.get_item(
        TableName="trikon_pr_state", Key={"pr_key": {"S": pr_key}}
    )
    item = resp.get("Item")
    assert item is not None
    # Both artifact IDs populated (the canonical response fixtures
    # use 111 and 222).
    assert item["last_check_run_id"]["N"] == "111"
    assert item["last_comment_id"]["N"] == "222"
    assert (
        item["last_head_sha"]["S"] == make_env_vars()["TRIKON_HEAD_SHA"]
    )
