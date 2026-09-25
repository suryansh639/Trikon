# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing pydantic models
# — plus consuming boto3 / respx / PyJWT boundary values — surfaces as
# ``explicit-any`` errors that refer to library-generated code, not to
# hand-written signatures. Silence at file scope — every ``Any`` here is
# bounded to the moto / respx / pydantic / PyJWT fixture surface, never
# crossed into a production module.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.orchestrator.never_fail_open` (task 17.4).

Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

Three concern groups, matching the module under test's declared ``__all__``:

* :func:`build_synthetic_verdict_row` — the pure, IO-free builder for the
  synthetic ``require_human`` :class:`VerdictRow` (design.md §3.5). Tests
  exercise the eighteen-field table verbatim, the XOR invariant enforced
  by :class:`VerdictRow`'s ``model_validator(mode="after")``
  (``evidence_blob`` populated, ``evidence_s3_key`` :data:`None`), the
  ``sk`` and ``risk_bucket_sk`` composition rules, the gzip-canonical-JSON
  shape of ``evidence_blob``, and the purity contract (same inputs → same
  bytes across N invocations, no side effects on module state).
* :class:`OrchestratorGithubClient.create_neutral_check_run` — the neutral
  Check Run POST (design.md §7.3). Tests use :mod:`respx` to intercept
  the two GitHub REST endpoints and :mod:`moto` to back the App
  private-key Secrets Manager read. Coverage includes JWT ``iss`` claim
  shape (verified by RS256 signature decode against the public key
  derived from :func:`rsa_test_private_key_pem`), installation-token
  cache reuse (design.md §7.4), Check Run body shape (Invariant 7 —
  ``name == "Trikon"``), and the retry-budget contract (three attempts
  on 5xx/429; immediate raise on 4xx).
* :func:`write_orchestrator_failure_verdict` — the six-step body
  (design.md §7.2). Tests exercise the happy path (DDB write + Check
  Run POST), the idempotency win on ``ConditionalCheckFailedException``
  (Property 7 second-delivery contract), the partial-success path
  (DDB write succeeds, Check Run POST exhausts retries) where the
  function returns normally and the DDB row is still present, and the
  propagate-on-unknown-DDB-error contract.

Every test intercepts httpx via ``respx.mock(base_url="https://api.github.com")``
and AWS via ``@mock_aws()``. No live network, no real DynamoDB.

Load-bearing library-compat shims:

* :func:`_httpx_respx_method_compat` — httpx 0.28 kept the ``method``
  attribute as bytes when the underlying httpcore request carried
  bytes, breaking respx 0.21's string-typed ``Method eq 'POST'``
  matcher. The fixture patches :meth:`httpx.Request.__init__` to
  decode bytes methods to str so respx routes resolve. Mirrors the
  same shim used by
  :mod:`trikon_cloud.fargate_runner.tests.test_github_client`.
* :func:`_reset_module_caches` — :data:`_TOKEN_CACHE` and
  :data:`_APP_JWT_CACHE` live at module scope in
  :mod:`trikon_cloud.orchestrator.never_fail_open` to model the
  container-lifetime cache the production Lambda uses. Under test we
  need per-test isolation so the cache-reuse assertion does not see
  a warm hit from a prior test. This autouse fixture clears the dict
  and monkeypatches the JWT slot back to :data:`None` before every
  test.
* :func:`_aws_credentials` — moto's mocks reject boto3 calls that
  arrive without any AWS credential set. This autouse fixture
  populates the four canonical testing values so ``@mock_aws()``
  activates cleanly.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
import jwt
import pytest
import respx
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from cryptography.hazmat.primitives import serialization
from moto import mock_aws

from trikon_cloud.fargate_runner.models import VerdictRow
from trikon_cloud.orchestrator import never_fail_open
from trikon_cloud.orchestrator.models import OrchestratorEnvConfig
from trikon_cloud.orchestrator.never_fail_open import (
    OrchestratorGithubClient,
    OrchestratorGithubClientError,
    build_synthetic_verdict_row,
    write_orchestrator_failure_verdict,
)
from trikon_cloud.webhook_receiver.models import SqsJobMessage

from .conftest import (
    CANONICAL_APP_ID,
    CANONICAL_DELIVERY_ID,
    CANONICAL_HEAD_SHA,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_SENT_AT,
    make_sqs_job_message,
)

# ---------------------------------------------------------------------------
# Autouse compat + isolation fixtures.
# ---------------------------------------------------------------------------


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

    Patches :meth:`httpx.Request.__init__` to decode bytes methods to
    str before delegating to the real init. Test-only shim — nothing
    in production ever constructs a request from a bytes method.
    Mirrors :mod:`trikon_cloud.fargate_runner.tests.test_github_client`.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)


@pytest.fixture(autouse=True)
def _reset_module_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear :data:`_TOKEN_CACHE` and :data:`_APP_JWT_CACHE` between tests.

    Both caches live at module scope inside
    :mod:`trikon_cloud.orchestrator.never_fail_open` to model the
    container-lifetime cache the production Lambda relies on. Without
    a per-test reset, the second test in this module would see a warm
    JWT hit from the first and the token-cache-reuse assertion would
    become vacuous. :meth:`pytest.MonkeyPatch.setattr` auto-reverts on
    teardown so the module returns to a clean baseline after each test.
    """
    never_fail_open._TOKEN_CACHE.clear()
    monkeypatch.setattr(never_fail_open, "_APP_JWT_CACHE", None)


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate testing AWS credentials so ``@mock_aws()`` activates cleanly.

    moto rejects boto3 calls that arrive without any credential set,
    even in mock mode. The four aliases below are the canonical
    testing values used across the Spec-2 test suite; matching them
    keeps behavior consistent across the repo.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Helpers — table + secret setup, public-key extraction, respx routes.
# ---------------------------------------------------------------------------


def _create_verdicts_table() -> None:
    """Create the ``trikon_verdicts`` DynamoDB table under moto.

    Key schema mirrors design.md §5.2 (Spec 2) and is byte-identical
    to the fargate_runner test suite's setup: partition key
    ``installation_id`` (``N``), sort key ``sk`` (``S``),
    ``PAY_PER_REQUEST`` billing. Called inside every test that
    exercises the DDB write branch of
    :func:`write_orchestrator_failure_verdict`.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
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


def _create_app_key_secret(pem: bytes) -> str:
    """Create a moto Secrets Manager secret carrying the App PEM.

    moto assigns its own randomized ARN suffix on ``create_secret``,
    so the caller stores whatever ARN moto returns and threads it
    into :class:`OrchestratorGithubClient`'s constructor — the
    ``CANONICAL_APP_PRIVATE_KEY_SECRET_ARN`` constant in ``conftest``
    is only used for the ``TRIKON_APP_PRIVATE_KEY_SECRET_ARN`` env
    alias and is never dereferenced by the tests here.

    Returns the moto-generated ARN so the caller can pass it as the
    client's ``app_private_key_secret_arn`` kwarg.
    """
    client = boto3.client("secretsmanager", region_name="us-east-1")
    resp = client.create_secret(
        Name="trikon/app-key-test",
        SecretString=pem.decode("utf-8"),
    )
    return str(resp["ARN"])


def _public_key_pem_from_private(private_pem: bytes) -> bytes:
    """Extract the RSA public key PEM from the given private-key PEM.

    :func:`jwt.decode` with ``algorithms=["RS256"]`` requires the
    public half of the RSA keypair for signature verification. The
    session-scoped :func:`rsa_test_private_key_pem` fixture yields
    the private PEM; this helper derives the corresponding public
    PEM in ``SubjectPublicKeyInfo`` format (the standard shape PyJWT
    accepts).
    """
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _mock_installation_token_route(
    respx_mock: respx.MockRouter,
    *,
    installation_id: int = CANONICAL_INSTALLATION_ID,
    token_value: str = "ghs_test_installation_token",
    expires_at: str = "2099-01-01T00:00:00Z",
) -> respx.Route:
    """Register the ``POST /app/installations/{id}/access_tokens`` route.

    Returns the :class:`respx.Route` so tests can inspect
    ``.call_count`` and ``.calls[-1].request`` for the JWT
    ``Authorization`` header. The far-future ``expires_at`` value
    keeps the cached entry inside the 5-minute safety-margin window
    so the token-cache-reuse test can rely on the cache honouring
    the entry across two consecutive :meth:`create_neutral_check_run`
    calls (design.md §7.4).
    """
    return respx_mock.post(
        f"/app/installations/{installation_id}/access_tokens"
    ).mock(
        return_value=httpx.Response(
            201,
            json={"token": token_value, "expires_at": expires_at},
        )
    )


def _mock_check_run_route(
    respx_mock: respx.MockRouter,
    *,
    repo_full_name: str = CANONICAL_REPO_FULL_NAME,
    response: httpx.Response | None = None,
) -> respx.Route:
    """Register the ``POST /repos/{repo}/check-runs`` route.

    Default response is ``201`` with a minimal Check Run body; tests
    exercising the retry / 4xx branches override ``response`` with
    an :class:`httpx.Response` carrying the desired status code.
    """
    if response is None:
        response = httpx.Response(201, json={"id": 42})
    return respx_mock.post(f"/repos/{repo_full_name}/check-runs").mock(
        return_value=response
    )


def _build_client(
    *,
    secret_arn: str,
    http_client: httpx.Client,
) -> OrchestratorGithubClient:
    """Assemble a wired :class:`OrchestratorGithubClient` for a test.

    Constructs the boto3 Secrets Manager client inside the caller's
    ``@mock_aws()`` context so moto intercepts the
    :meth:`get_secret_value` call. ``app_id`` is
    :data:`CANONICAL_APP_ID` verbatim; the JWT-signed ``iss`` claim
    is asserted separately in
    :func:`test_create_neutral_check_run_mints_jwt_with_string_iss`.
    """
    secrets_client = boto3.client("secretsmanager", region_name="us-east-1")
    return OrchestratorGithubClient(
        app_id=CANONICAL_APP_ID,
        app_private_key_secret_arn=secret_arn,
        secrets_client=secrets_client,
        http_client=http_client,
    )


def _details_url() -> str:
    """Render the canonical ``details_url`` used across the write-verdict tests.

    Matches :attr:`OrchestratorEnvConfig.trikon_check_run_details_url_template`'s
    default (``https://cloud.trikon.dev/audits/{delivery_id}``) with
    :data:`CANONICAL_DELIVERY_ID` substituted.
    """
    return f"https://cloud.trikon.dev/audits/{CANONICAL_DELIVERY_ID}"


@pytest.fixture
def sqs_message() -> SqsJobMessage:
    """Return the canonical :class:`SqsJobMessage` used across the tests."""
    return make_sqs_job_message()


# ---------------------------------------------------------------------------
# §1 — build_synthetic_verdict_row (design.md §3.5).
# ---------------------------------------------------------------------------


def test_build_synthetic_verdict_row_returns_valid_verdict_row_xor_evidence(
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    The synthetic row satisfies :class:`VerdictRow`'s
    ``model_validator(mode="after")`` XOR invariant: ``evidence_blob``
    is populated (inline gzipped canonical JSON, always small on
    this path) and ``evidence_s3_key`` is :data:`None` (never spills
    to S3 — design.md §3.5 ``evidence_s3_key`` row). Construction
    itself would raise :class:`pydantic.ValidationError` if the
    invariant were broken, so ``isinstance`` alone plus the two
    field checks constitute the assertion.
    """
    row = build_synthetic_verdict_row(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="TaskDefinitionNotFound",
    )

    assert isinstance(row, VerdictRow)
    assert row.evidence_blob is not None
    assert isinstance(row.evidence_blob, bytes)
    assert row.evidence_s3_key is None


def test_build_synthetic_verdict_row_every_field_matches_design_35(
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Every field on the returned :class:`VerdictRow` matches the
    design.md §3.5 table verbatim. This is the load-bearing shape
    check — every subsequent test assumes this contract holds and
    only exercises specific compositional details
    (``sk``, ``risk_bucket_sk``, ``evidence_blob``).
    """
    row = build_synthetic_verdict_row(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="AccessDeniedException",
        duration_ms=1234,
    )

    # Identity fields — copied byte-for-byte from the SQS message.
    assert row.installation_id == sqs_message.installation_id
    assert row.repo_full_name == sqs_message.repo_full_name
    assert row.pr_number == sqs_message.pr_number
    assert row.head_sha == sqs_message.head_sha
    assert row.base_sha == sqs_message.base_sha

    # Decision + rule — fixed strings per Requirement 7.1.
    assert row.decision == "require_human"
    assert row.matched_rule == "orchestrator terminal failure"

    # Zero counters — the runner never observed the diff.
    assert row.blast_radius_score == 0
    assert row.new_errors == 0
    assert row.new_warnings == 0
    assert row.preexisting_errors == 0

    # Caller-supplied wall-clock ms.
    assert row.duration_ms == 1234

    # Documented placeholders + fixed schema version.
    assert row.fargate_task_arn == "n/a-orchestrator-terminal"
    assert row.schema_version == 2


def test_build_synthetic_verdict_row_sk_composition(
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    ``sk == f"{sqs_message.sent_at}#{sqs_message.delivery_id}"`` — the
    natural-key ``sk`` matches Spec 2's ``trikon_verdicts`` writer
    byte-for-byte (Requirement 7.4) so the ``ConditionExpression``
    idempotency guard works across a redelivery from either code
    path.
    """
    row = build_synthetic_verdict_row(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="TaskDefinitionNotFound",
    )

    assert row.sk == f"{sqs_message.sent_at}#{sqs_message.delivery_id}"
    # Guard against accidental byte-shape drift on the ``sent_at`` copy.
    assert row.sk == f"{CANONICAL_SENT_AT}#{CANONICAL_DELIVERY_ID}"


def test_build_synthetic_verdict_row_evidence_blob_gunzips_to_canonical_json(
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    ``evidence_blob`` is the gzip compression of a canonical JSON
    object carrying exactly three keys — ``error_class``,
    ``error_code``, ``delivery_id`` — encoded with ``sort_keys=True``
    and ``separators=(",", ":")`` (design.md §3.5 ``evidence_blob``
    row). Gunzip + JSON decode round-trips the payload verbatim.
    """
    row = build_synthetic_verdict_row(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="TaskDefinitionNotFound",
    )
    assert row.evidence_blob is not None

    decompressed = gzip.decompress(row.evidence_blob)
    payload: dict[str, Any] = json.loads(decompressed.decode("utf-8"))

    assert payload == {
        "error_class": "terminal",
        "error_code": "TaskDefinitionNotFound",
        "delivery_id": sqs_message.delivery_id,
    }
    # Canonical form: sorted keys, no whitespace. Re-encoding the
    # decoded dict must yield the same bytes we started with (modulo
    # gzip framing, which we strip via re-compression).
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert decompressed == canonical.encode("utf-8")


def test_build_synthetic_verdict_row_risk_bucket_sk_composition(
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    ``risk_bucket_sk == f"0000#{sqs_message.sent_at}"`` — bucket
    ``0000`` is reserved for ``require_human`` verdicts with no risk
    score (design.md §3.5). Dashboards querying the GSI2 sort key
    for zero-risk verdicts pick up the synthetic row on the same
    scan as Spec 2's ``require_human`` rows.
    """
    row = build_synthetic_verdict_row(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="TaskDefinitionNotFound",
    )

    assert row.risk_bucket_sk == f"0000#{sqs_message.sent_at}"
    assert row.risk_bucket_sk == f"0000#{CANONICAL_SENT_AT}"


def test_build_synthetic_verdict_row_is_pure(sqs_message: SqsJobMessage) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Purity contract (design.md §3.5): same inputs produce byte-
    identical output across N invocations, no module-scope state
    mutations, no IO. Called five times with the same kwargs; the
    resulting :class:`VerdictRow` instances compare equal (Pydantic's
    ``__eq__`` compares field values, so ``evidence_blob`` bytes
    equality is the operative check for gzip determinism).
    """
    kwargs: dict[str, Any] = {
        "sqs_message": sqs_message,
        "error_class": "terminal",
        "error_code": "TaskDefinitionNotFound",
        "duration_ms": 0,
    }
    rows = [build_synthetic_verdict_row(**kwargs) for _ in range(5)]
    # Every produced row equals the first, and — more strongly — the
    # gzipped ``evidence_blob`` bytes are byte-identical (gzip's
    # ``mtime`` header defaults to a repeatable ``0`` when Python
    # writes to ``BytesIO``, so this equality is well-defined).
    first = rows[0]
    for other in rows[1:]:
        assert other == first
        assert other.evidence_blob == first.evidence_blob

    # Purity — module-scope caches are untouched.
    assert never_fail_open._TOKEN_CACHE == {}
    assert never_fail_open._APP_JWT_CACHE is None


# ---------------------------------------------------------------------------
# §2 — OrchestratorGithubClient.create_neutral_check_run (design.md §7.3).
# ---------------------------------------------------------------------------


@mock_aws()
def test_create_neutral_check_run_mints_jwt_with_string_iss(
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    The signed App JWT sent as ``Authorization: Bearer <jwt>`` on
    the token-mint request carries ``iss == str(app_id)`` — PyJWT
    ≥ 2.10 rejects a non-string ``iss`` at :func:`jwt.encode`
    (established defect fix in Spec 2's ``github_client.py``).
    Verified by RS256 signature-verifying decode against the public
    key derived from :func:`rsa_test_private_key_pem`.
    """
    public_pem = _public_key_pem_from_private(rsa_test_private_key_pem)
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )

        request = token_route.calls[-1].request
        auth_header = request.headers["Authorization"]
        assert auth_header.startswith("Bearer ")
        signed_jwt = auth_header.removeprefix("Bearer ")

        decoded: dict[str, Any] = jwt.decode(
            signed_jwt, public_pem, algorithms=["RS256"]
        )
        # PyJWT ≥ 2.10 constraint: ``iss`` must be a string. The
        # production client stringifies :attr:`_app_id` via
        # ``str(...)`` at JWT-construction time — the decoded payload
        # carries that same str form on the wire.
        assert isinstance(decoded["iss"], str)
        assert decoded["iss"] == str(CANONICAL_APP_ID)


@mock_aws()
def test_create_neutral_check_run_fetches_installation_token(
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    A first-ever :meth:`create_neutral_check_run` call fetches the
    installation-scoped access token via
    ``POST /app/installations/{id}/access_tokens`` (design.md §7.3).
    ``call_count == 1`` on the token route captures the single mint;
    ``call_count == 1`` on the check-run route captures the single
    downstream POST.
    """
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )

        assert token_route.call_count == 1
        assert check_run_route.call_count == 1
        # The mint hit the ``/app/installations/{id}/access_tokens``
        # path template with the caller-supplied installation id.
        token_request = token_route.calls[-1].request
        assert token_request.url.path == (
            f"/app/installations/{CANONICAL_INSTALLATION_ID}/access_tokens"
        )


@mock_aws()
def test_create_neutral_check_run_posts_body_with_trikon_name_and_neutral(
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    The Check Run POST body carries exactly ``name == "Trikon"``
    (Invariant 7 — product-name-in-user-copy pinned at the wire
    level), ``status == "completed"``, and
    ``conclusion == "neutral"`` (design.md §7.2 step 5 body). The
    ``head_sha`` and ``details_url`` echo the caller's kwargs.
    """
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )

        request = check_run_route.calls[-1].request
        body: dict[str, Any] = json.loads(request.content)
        assert body["name"] == "Trikon"
        assert body["status"] == "completed"
        assert body["conclusion"] == "neutral"
        assert body["head_sha"] == CANONICAL_HEAD_SHA
        assert body["details_url"] == _details_url()
        # The output object matches the design.md §7.2 template shape.
        assert body["output"]["title"] == "Trikon Cloud verification unavailable"
        assert body["output"]["text"] is None


@mock_aws()
def test_create_neutral_check_run_reuses_cached_installation_token(
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Second call for the same ``installation_id`` reuses the cached
    token — no second POST hits ``/access_tokens`` (design.md §7.4
    container-lifetime cache). The far-future ``expires_at`` on the
    token response keeps the cached entry inside the 5-minute safety
    margin so the second call takes the warm-hit branch. Check Run
    POSTs still fire twice because the check-run API is not
    deduplicating.
    """
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )

        # Exactly ONE token mint across two Check Run POSTs — the
        # load-bearing cache-reuse assertion.
        assert token_route.call_count == 1
        assert check_run_route.call_count == 2


@mock_aws()
def test_create_neutral_check_run_retries_on_5xx_up_to_three_attempts(
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Three consecutive 500s on the Check Run POST exhaust the
    retry-budget (three attempts, exponential backoff, 30-second
    total budget per design.md §7.3) and raise
    :class:`OrchestratorGithubClientError`. ``call_count == 3`` on
    the check-run route captures every attempt including retries.
    The mint route fires exactly once — the mint's response is 201,
    so its retry loop does not engage.
    """
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(500)
        )

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            with pytest.raises(OrchestratorGithubClientError):
                client.create_neutral_check_run(
                    installation_id=CANONICAL_INSTALLATION_ID,
                    repo_full_name=CANONICAL_REPO_FULL_NAME,
                    head_sha=CANONICAL_HEAD_SHA,
                    details_url=_details_url(),
                )

        assert check_run_route.call_count == 3
        assert token_route.call_count == 1


@mock_aws()
@pytest.mark.parametrize("non_retryable_status", [401, 403])
def test_create_neutral_check_run_raises_immediately_on_401_403(
    rsa_test_private_key_pem: bytes,
    non_retryable_status: int,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    The retry policy retries only on 5xx / 429 (design.md §7.3).
    Every other 4xx surfaces immediately as
    :class:`OrchestratorGithubClientError` on the first attempt —
    a 401 (revoked installation, missing scope) or 403 (repository
    permissions blocked) is not going to become a 201 on retry, so
    the client MUST NOT waste the retry budget waiting.
    """
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(non_retryable_status)
        )

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            with pytest.raises(OrchestratorGithubClientError):
                client.create_neutral_check_run(
                    installation_id=CANONICAL_INSTALLATION_ID,
                    repo_full_name=CANONICAL_REPO_FULL_NAME,
                    head_sha=CANONICAL_HEAD_SHA,
                    details_url=_details_url(),
                )

        # Exactly ONE attempt — no retry on non-retryable 4xx.
        assert check_run_route.call_count == 1


# ---------------------------------------------------------------------------
# §3 — write_orchestrator_failure_verdict (design.md §7.2).
# ---------------------------------------------------------------------------


def _scan_verdicts_rows() -> list[dict[str, Any]]:
    """Return every row in the moto ``trikon_verdicts`` table.

    Used by the write-verdict tests to assert row presence / count
    after :func:`write_orchestrator_failure_verdict` returns. Called
    inside the caller's ``@mock_aws()`` context.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    resp = client.scan(TableName="trikon_verdicts")
    return list(resp.get("Items", []))


@mock_aws()
def test_write_orchestrator_failure_verdict_happy_path(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Happy path (design.md §7.2 six-step body): DDB ``put_item`` on
    ``trikon_verdicts`` writes the synthetic row, then the neutral
    Check Run POST fires exactly once. Post-conditions: exactly one
    row present in ``trikon_verdicts`` with ``decision ==
    "require_human"``, exactly one Check Run POST recorded by respx.
    """
    _create_verdicts_table()
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            github_client = _build_client(
                secret_arn=secret_arn, http_client=http_client
            )
            write_orchestrator_failure_verdict(
                sqs_message=sqs_message,
                error_class="terminal",
                error_code="TaskDefinitionNotFound",
                boto3_session=boto3.session.Session(region_name="us-east-1"),
                github_client=github_client,
                env=OrchestratorEnvConfig(),
            )

        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        assert rows[0]["decision"]["S"] == "require_human"
        assert rows[0]["matched_rule"]["S"] == "orchestrator terminal failure"
        assert rows[0]["sk"]["S"] == (
            f"{sqs_message.sent_at}#{sqs_message.delivery_id}"
        )
        assert check_run_route.call_count == 1


@mock_aws()
def test_write_orchestrator_failure_verdict_idempotency_on_second_call(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Two consecutive calls for the same :class:`SqsJobMessage`
    produce exactly one ``trikon_verdicts`` row. The second call
    trips the ``ConditionExpression`` guard —
    ``ConditionalCheckFailedException`` is caught, logged INFO as
    ``verdict_already_exists``, and the function returns cleanly
    without posting a second Check Run (design.md §7.2 steps 2 → 3;
    the ``return`` after the idempotency win happens BEFORE step 5's
    Check Run POST).
    """
    _create_verdicts_table()
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            github_client = _build_client(
                secret_arn=secret_arn, http_client=http_client
            )
            env = OrchestratorEnvConfig()
            session = boto3.session.Session(region_name="us-east-1")
            # First call — writes row and posts Check Run.
            write_orchestrator_failure_verdict(
                sqs_message=sqs_message,
                error_class="terminal",
                error_code="TaskDefinitionNotFound",
                boto3_session=session,
                github_client=github_client,
                env=env,
            )
            # Second call — hits ConditionalCheckFailedException and
            # returns cleanly without raising.
            write_orchestrator_failure_verdict(
                sqs_message=sqs_message,
                error_class="terminal",
                error_code="TaskDefinitionNotFound",
                boto3_session=session,
                github_client=github_client,
                env=env,
            )

        # Idempotency: still exactly one row.
        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        # No second Check Run POST — the second call returned before
        # step 5 fired.
        assert check_run_route.call_count == 1


@mock_aws()
def test_write_orchestrator_failure_verdict_partial_success_check_run_fails(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Partial-success semantics (design.md §4.5 / §7.1): the DDB write
    succeeds, the Check Run POST exhausts its retry budget on three
    consecutive 500s, and :func:`write_orchestrator_failure_verdict`
    returns normally (no re-raise). Invariant 2 is already satisfied
    by the DDB row — the dashboard picks up the ``require_human``
    verdict on the next poll even without the Check Run.
    """
    _create_verdicts_table()
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(500)
        )

        with httpx.Client() as http_client:
            github_client = _build_client(
                secret_arn=secret_arn, http_client=http_client
            )
            # No exception escapes — the OrchestratorGithubClientError
            # from the exhausted retry budget is caught and logged
            # ERROR without re-raising.
            write_orchestrator_failure_verdict(
                sqs_message=sqs_message,
                error_class="terminal",
                error_code="TaskDefinitionNotFound",
                boto3_session=boto3.session.Session(region_name="us-east-1"),
                github_client=github_client,
                env=OrchestratorEnvConfig(),
            )

        # DDB row IS present — the write happened before the Check
        # Run POST was even attempted.
        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        assert rows[0]["decision"]["S"] == "require_human"
        # Three attempts on the 500 — the retry budget was exhausted,
        # confirming the failure path was exercised.
        assert check_run_route.call_count == 3


@mock_aws()
def test_write_orchestrator_failure_verdict_non_conditional_ddb_error_propagates(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Any :class:`ClientError` other than
    ``ConditionalCheckFailedException`` propagates unhandled to the
    caller (design.md §7.2 step 3 else-branch). Here we withhold
    the ``trikon_verdicts`` table so moto raises
    ``ResourceNotFoundException`` on ``put_item`` — the exception
    surfaces to the caller so SQS returns the message for one more
    attempt before DLQ. No Check Run POST fires — the write failure
    short-circuits before step 5.
    """
    # Deliberately omit :func:`_create_verdicts_table` — moto's
    # DynamoDB will reject ``put_item`` with
    # ``ResourceNotFoundException``, exercising the non-idempotency
    # ClientError branch.
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)

    # ``assert_all_called=False`` — the DDB write short-circuits
    # before either the mint or the Check Run POST fires. Registering
    # the routes lets the ``call_count == 0`` assertion below document
    # the short-circuit; default respx behavior would fail the test
    # for unused routes.
    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            github_client = _build_client(
                secret_arn=secret_arn, http_client=http_client
            )
            with pytest.raises(ClientError) as exc_info:
                write_orchestrator_failure_verdict(
                    sqs_message=sqs_message,
                    error_class="terminal",
                    error_code="TaskDefinitionNotFound",
                    boto3_session=boto3.session.Session(region_name="us-east-1"),
                    github_client=github_client,
                    env=OrchestratorEnvConfig(),
                )
            # Sanity check — the propagated error is the expected
            # missing-table condition, not a coincidental other error.
            assert exc_info.value.response["Error"]["Code"] == (
                "ResourceNotFoundException"
            )

        # Check Run POST never fires when the DDB write short-circuits.
        assert check_run_route.call_count == 0


# ---------------------------------------------------------------------------
# §4 — Warm-cache branch coverage on `_get_app_jwt` (design.md §7.3-§7.4).
# ---------------------------------------------------------------------------


@mock_aws()
def test_create_neutral_check_run_reuses_cached_app_jwt(
    rsa_test_private_key_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    Pre-populate :data:`_APP_JWT_CACHE` with a still-valid entry
    whose expiry is well past the 1-minute safety margin. A
    subsequent :meth:`create_neutral_check_run` MUST reuse the
    cached JWT — Secrets Manager is never read, :func:`jwt.encode`
    is never called, and the token-mint request carries the
    pre-seeded JWT verbatim on its ``Authorization: Bearer …`` header.
    Covers the App-JWT warm-hit path (design.md §7.4) that the
    always-cold-start ``_reset_module_caches`` autouse fixture would
    otherwise mask in the load-bearing §2 tests.
    """
    from datetime import UTC, datetime, timedelta

    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)
    # Pre-seed the App JWT cache with a dummy value that stays
    # valid for the next hour — well past the 1-minute safety
    # margin the cache honours (design.md §7.3).
    warm_expiry = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(
        never_fail_open, "_APP_JWT_CACHE", ("dummy-cached-jwt", warm_expiry)
    )

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(secret_arn=secret_arn, http_client=http_client)
            client.create_neutral_check_run(
                installation_id=CANONICAL_INSTALLATION_ID,
                repo_full_name=CANONICAL_REPO_FULL_NAME,
                head_sha=CANONICAL_HEAD_SHA,
                details_url=_details_url(),
            )

        # The token-mint POST fires exactly once (the token cache
        # started empty), authenticated with the CACHED JWT — the
        # load-bearing warm-hit assertion.
        assert token_route.call_count == 1
        assert check_run_route.call_count == 1
        auth_header = token_route.calls[-1].request.headers["Authorization"]
        assert auth_header == "Bearer dummy-cached-jwt"


# ---------------------------------------------------------------------------
# §5 — `_extract_client_error_code` defensive-parse fallbacks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed_response",
    [
        "not-a-dict",
        {"Error": "not-a-dict"},
        {"Error": {"Code": 42}},
    ],
)
def test_extract_client_error_code_returns_empty_string_on_malformed_response(
    malformed_response: object,
) -> None:
    """Feature: trikon-cloud-orchestrator, Requirement 7.4 idempotency dispatch.

    :func:`_extract_client_error_code` is the caller-side classifier
    for the ``ConditionalCheckFailedException`` idempotency-win
    branch in :func:`write_orchestrator_failure_verdict`. On any
    malformed response shape the helper returns ``""`` — the
    equality check against ``"ConditionalCheckFailedException"``
    then falls through to the re-raise arm, which is the safe
    default (unknown errors propagate to SQS for one more attempt).
    Covers the three fallback returns at ``line 790`` (non-dict
    response), ``line 793`` (non-dict ``Error``), and ``line 796``
    (non-string ``Code``).
    """
    exc = ClientError({"Error": {"Code": "sentinel"}}, "PutItem")
    # ``botocore`` is imported ``# type: ignore[import-untyped]``
    # so :attr:`ClientError.response` types as ``Any`` and the
    # override assignment stays mypy-strict clean.
    exc.response = malformed_response
    assert never_fail_open._extract_client_error_code(exc) == ""


# ---------------------------------------------------------------------------
# §6 — `_extract_delivery_id_hint` empty-slash fallback (design.md §7.3).
# ---------------------------------------------------------------------------


def test_extract_delivery_id_hint_returns_full_string_when_no_slash() -> None:
    """Feature: trikon-cloud-orchestrator, design.md §7.3 summary rendering.

    :func:`_extract_delivery_id_hint` is a best-effort parse that
    normally returns the trailing path segment of ``details_url``.
    When the input carries no ``/`` at all, the function returns
    the (right-stripped) input verbatim rather than raising — the
    Check Run summary carries the caller's raw string as its
    correlation hint. Covers ``line 643`` in
    :mod:`trikon_cloud.orchestrator.never_fail_open`, which the
    canonical ``https://cloud.trikon.dev/audits/{delivery_id}``
    template used by every other test in this module never reaches.
    """
    assert (
        never_fail_open._extract_delivery_id_hint("delivery-only-no-slash")
        == "delivery-only-no-slash"
    )
