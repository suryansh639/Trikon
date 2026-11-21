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
  private-key SSM ``SecureString`` read. Coverage includes JWT ``iss`` claim
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
import io
import json
import logging
from collections.abc import Iterator
from typing import IO, Any

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
    CheckRunConclusion,
    FailClosedNotice,
    FailClosedPublication,
    FailClosedTarget,
    OrchestratorGithubClient,
    OrchestratorGithubClientError,
    build_synthetic_verdict_row,
    publish_fail_closed_result,
    read_interim_check_run_id,
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


_APP_KEY_PARAMETER_NAME = "/trikon/test/app-key"


def _create_app_key_parameter(pem: bytes) -> str:
    """Create a moto SSM ``SecureString`` parameter carrying the App PEM.

    Returns the parameter name so the caller can pass it as the
    client's ``app_private_key_parameter_name`` kwarg.
    """
    client = boto3.client("ssm", region_name="us-east-1")
    client.put_parameter(
        Name=_APP_KEY_PARAMETER_NAME,
        Value=pem.decode("utf-8"),
        Type="SecureString",
    )
    return _APP_KEY_PARAMETER_NAME


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
    parameter_name: str,
    http_client: httpx.Client,
    ssm_client: object | None = None,
) -> OrchestratorGithubClient:
    """Assemble a wired :class:`OrchestratorGithubClient` for a test.

    Constructs the boto3 SSM client inside the caller's
    ``@mock_aws()`` context so moto intercepts the
    :meth:`get_parameter` call. ``ssm_client`` overrides it (e.g. with
    a call-recording wrapper). ``app_id`` is
    :data:`CANONICAL_APP_ID` verbatim; the JWT-signed ``iss`` claim
    is asserted separately in
    :func:`test_create_neutral_check_run_mints_jwt_with_string_iss`.
    """
    if ssm_client is None:
        ssm_client = boto3.client("ssm", region_name="us-east-1")
    return OrchestratorGithubClient(
        app_id=CANONICAL_APP_ID,
        app_private_key_parameter_name=parameter_name,
        ssm_client=ssm_client,  # type: ignore[arg-type]
        http_client=http_client,
    )


def _details_url() -> str:
    """Render the canonical ``details_url`` used across the write-verdict tests.

    Matches :attr:`OrchestratorEnvConfig.trikon_check_run_details_url_template`'s
    default (``https://api.trikon.unideploy.com/audits/{delivery_id}``) with
    :data:`CANONICAL_DELIVERY_ID` substituted.
    """
    return f"https://api.trikon.unideploy.com/audits/{CANONICAL_DELIVERY_ID}"


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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(500)
        )

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(non_retryable_status)
        )

        with httpx.Client() as http_client:
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            github_client = _build_client(
                parameter_name=parameter_name, http_client=http_client
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            github_client = _build_client(
                parameter_name=parameter_name, http_client=http_client
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(500)
        )

        with httpx.Client() as http_client:
            github_client = _build_client(
                parameter_name=parameter_name, http_client=http_client
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
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

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
                parameter_name=parameter_name, http_client=http_client
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
    cached JWT — SSM is never read, :func:`jwt.encode`
    is never called, and the token-mint request carries the
    pre-seeded JWT verbatim on its ``Authorization: Bearer …`` header.
    Covers the App-JWT warm-hit path (design.md §7.4) that the
    always-cold-start ``_reset_module_caches`` autouse fixture would
    otherwise mask in the load-bearing §2 tests.
    """
    from datetime import UTC, datetime, timedelta

    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)
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
            client = _build_client(parameter_name=parameter_name, http_client=http_client)
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
# §4b — App private key read from SSM Parameter Store (cost-cuts Req 5, 6).
# ---------------------------------------------------------------------------


class _RecordingSsmClient:
    """Wraps a moto-backed boto3 SSM client and records ``get_parameter`` calls."""

    def __init__(self) -> None:
        self._inner = boto3.client("ssm", region_name="us-east-1")
        self.calls: list[tuple[str, bool]] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Any:  # noqa: N803
        self.calls.append((Name, WithDecryption))
        return self._inner.get_parameter(Name=Name, WithDecryption=WithDecryption)


@mock_aws()
def test_app_key_read_uses_ssm_with_decryption_once_per_jwt(
    rsa_test_private_key_pem: bytes,
) -> None:
    """The PEM is read by ``GetParameter(WithDecryption=True)``; the JWT cache holds.

    Two installations mint two installation tokens but only one App JWT
    (9-minute cache, unchanged), so SSM is read exactly once.
    """
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)
    recording = _RecordingSsmClient()
    other_installation = CANONICAL_INSTALLATION_ID + 1

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        _mock_installation_token_route(respx_mock, installation_id=other_installation)
        _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(
                parameter_name=parameter_name,
                http_client=http_client,
                ssm_client=recording,
            )
            for installation_id in (CANONICAL_INSTALLATION_ID, other_installation):
                client.create_neutral_check_run(
                    installation_id=installation_id,
                    repo_full_name=CANONICAL_REPO_FULL_NAME,
                    head_sha=CANONICAL_HEAD_SHA,
                    details_url=_details_url(),
                )

    assert recording.calls == [(parameter_name, True)]


@mock_aws()
def test_app_key_parameter_not_found_propagates_without_github_call() -> None:
    """A missing parameter raises ``ClientError(ParameterNotFound)`` unchanged.

    No GitHub call is made and no App JWT is cached.
    """
    recording = _RecordingSsmClient()

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        token_route = _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)

        with httpx.Client() as http_client:
            client = _build_client(
                parameter_name=_APP_KEY_PARAMETER_NAME,
                http_client=http_client,
                ssm_client=recording,
            )
            with pytest.raises(ClientError) as excinfo:
                client.create_neutral_check_run(
                    installation_id=CANONICAL_INSTALLATION_ID,
                    repo_full_name=CANONICAL_REPO_FULL_NAME,
                    head_sha=CANONICAL_HEAD_SHA,
                    details_url=_details_url(),
                )

        assert excinfo.value.response["Error"]["Code"] == "ParameterNotFound"
        assert token_route.call_count == 0
        assert check_run_route.call_count == 0
        assert never_fail_open._APP_JWT_CACHE is None


def test_app_key_response_without_value_raises_key_error() -> None:
    """A ``GetParameter`` response with no ``Parameter.Value`` raises ``KeyError``.

    The message names only the missing key, never a credential.
    """

    class _NoValueClient:
        def get_parameter(self, *, Name: str, WithDecryption: bool) -> Any:  # noqa: N803
            del WithDecryption
            return {"Parameter": {"Name": Name, "Type": "SecureString"}}

    with httpx.Client() as http_client:
        client = _build_client(
            parameter_name=_APP_KEY_PARAMETER_NAME,
            http_client=http_client,
            ssm_client=_NoValueClient(),
        )
        with pytest.raises(KeyError) as excinfo:
            client._get_app_private_key_pem()

    assert excinfo.value.args == ("Value",)


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
    canonical ``https://api.trikon.unideploy.com/audits/{delivery_id}``
    template used by every other test in this module never reaches.
    """
    assert (
        never_fail_open._extract_delivery_id_hint("delivery-only-no-slash")
        == "delivery-only-no-slash"
    )


# ---------------------------------------------------------------------------
# §7 — Fail_Closed_Result notices (sandbox-minute-plans, Req 11.9, 11.10).
# ---------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's stream to a :class:`io.StringIO`.

    Same pattern as :mod:`trikon_cloud.orchestrator.tests.test_logger`
    and :mod:`trikon_cloud.orchestrator.tests.test_orchestrator_handler`:
    the Powertools :class:`Logger` attaches a :class:`logging.StreamHandler`
    bound to ``sys.stdout`` at import time, so the handler's stream is
    swapped for the duration of the test and restored on teardown.
    """
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-orchestrator")
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            original_streams.append((handler, handler.stream))
            handler.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in original_streams:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


def _log_records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse the captured Powertools JSON lines into a list of dicts."""
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


#: Body key order of the historical neutral Check Run POST. Pinned so a
#: refactor of :meth:`OrchestratorGithubClient.create_check_run` cannot
#: silently reorder the wire JSON.
_CHECK_RUN_BODY_KEYS: list[str] = [
    "name",
    "head_sha",
    "status",
    "conclusion",
    "output",
    "details_url",
]
_CHECK_RUN_OUTPUT_KEYS: list[str] = ["title", "summary", "text"]


def _make_notice(
    *,
    conclusion: CheckRunConclusion = "action_required",
) -> FailClosedNotice:
    """Build a quota-style :class:`FailClosedNotice` by hand.

    Deliberately independent of ``quota_notices.py``: these tests pin
    the never-fail-open plumbing, not the copy table. Every field is
    distinct from the RunTask-terminal defaults so an assertion can
    only pass if the notice's value reached the row or the wire.
    """
    return FailClosedNotice(
        matched_rule="sandbox minutes exhausted",
        title="Monthly sandbox minutes used up",
        summary=(
            "This Trikon account has used all of its sandbox minutes for the "
            "current period, so Trikon Cloud did not run the hosted check."
        ),
        details_url="https://trikon.unideploy.com/account",
        conclusion=conclusion,
        error_class="quota",
        error_code="minutes_exhausted",
    )


def _expected_item(
    sqs_message: SqsJobMessage,
    *,
    matched_rule: str,
) -> dict[str, dict[str, str]]:
    """Every scanned ``trikon_verdicts`` attribute except ``evidence_blob``.

    The synthetic row's shape is the same on both paths; only
    ``matched_rule`` (and the evidence payload, checked separately)
    differs when a notice is passed.
    """
    return {
        "installation_id": {"N": str(sqs_message.installation_id)},
        "sk": {"S": f"{sqs_message.sent_at}#{sqs_message.delivery_id}"},
        "repo_full_name": {"S": sqs_message.repo_full_name},
        "pr_number": {"N": str(sqs_message.pr_number)},
        "head_sha": {"S": sqs_message.head_sha},
        "base_sha": {"S": sqs_message.base_sha},
        "decision": {"S": "require_human"},
        "matched_rule": {"S": matched_rule},
        "blast_radius_score": {"N": "0"},
        "new_errors": {"N": "0"},
        "new_warnings": {"N": "0"},
        "preexisting_errors": {"N": "0"},
        "duration_ms": {"N": "0"},
        "fargate_task_arn": {"S": "n/a-orchestrator-terminal"},
        "schema_version": {"N": "2"},
        "risk_bucket_sk": {"S": f"0000#{sqs_message.sent_at}"},
    }


def _split_evidence(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(row_without_evidence_blob, decoded_evidence_payload)``."""
    rest = {key: value for key, value in row.items() if key != "evidence_blob"}
    blob = row["evidence_blob"]["B"]
    assert isinstance(blob, bytes)
    payload: dict[str, Any] = json.loads(gzip.decompress(blob).decode("utf-8"))
    return rest, payload


def _write_once(
    *,
    sqs_message: SqsJobMessage,
    parameter_name: str,
    http_client: httpx.Client,
    notice: FailClosedNotice | None,
    calls: int = 1,
) -> None:
    """Call :func:`write_orchestrator_failure_verdict` ``calls`` times.

    The caller's ``error_class`` / ``error_code`` are the terminal
    values so the notice tests can prove the notice's evidence fields
    win over them.
    """
    github_client = _build_client(parameter_name=parameter_name, http_client=http_client)
    env = OrchestratorEnvConfig()
    session = boto3.session.Session(region_name="us-east-1")
    for _ in range(calls):
        write_orchestrator_failure_verdict(
            sqs_message=sqs_message,
            error_class="terminal",
            error_code="TaskDefinitionNotFound",
            boto3_session=session,
            github_client=github_client,
            env=env,
            notice=notice,
        )


@mock_aws()
def test_write_failure_verdict_without_notice_keeps_terminal_row_and_check_run(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: sandbox-minute-plans, Requirements 11.9, 11.10 (``notice=None``).

    With no notice, the RunTask-terminal path is unchanged: the row
    carries ``matched_rule="orchestrator terminal failure"`` and the
    caller's ``error_class`` / ``error_code`` in ``evidence_blob``,
    and the Check Run body is the historical neutral POST, key order
    included, with the details URL rendered from the env template and
    the delivery id in the summary.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client:
            _write_once(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=None,
            )

        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        rest, evidence = _split_evidence(rows[0])
        assert rest == _expected_item(
            sqs_message, matched_rule="orchestrator terminal failure"
        )
        assert evidence == {
            "error_class": "terminal",
            "error_code": "TaskDefinitionNotFound",
            "delivery_id": sqs_message.delivery_id,
        }

        assert check_run_route.call_count == 1
        body: dict[str, Any] = json.loads(check_run_route.calls[-1].request.content)
        assert list(body) == _CHECK_RUN_BODY_KEYS
        assert list(body["output"]) == _CHECK_RUN_OUTPUT_KEYS
        assert body == {
            "name": "Trikon",
            "head_sha": sqs_message.head_sha,
            "status": "completed",
            "conclusion": "neutral",
            "output": {
                "title": "Trikon Cloud verification unavailable",
                "summary": (
                    "The Trikon Cloud verification runner could not be "
                    "dispatched for this pull request. A human reviewer will "
                    f"follow up. Delivery id: {sqs_message.delivery_id}."
                ),
                "text": None,
            },
            "details_url": (
                "https://api.trikon.unideploy.com/audits/"
                f"{sqs_message.delivery_id}"
            ),
        }


@mock_aws()
@pytest.mark.parametrize("conclusion", ["action_required", "neutral"])
def test_write_failure_verdict_with_notice_uses_notice_rule_copy_and_conclusion(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
    conclusion: CheckRunConclusion,
) -> None:
    """Feature: sandbox-minute-plans, Requirements 11.9, 11.10 (with a notice).

    A notice overrides the row's ``matched_rule`` and the evidence
    ``error_class`` / ``error_code`` (the caller's terminal values are
    ignored), and the Check Run carries the notice's title, summary,
    details URL, and conclusion with ``name="Trikon"``,
    ``status="completed"``, ``output.text`` null, and the historical
    key order. Every other row field matches the terminal row.
    """
    notice = _make_notice(conclusion=conclusion)
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client:
            _write_once(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=notice,
            )

        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        rest, evidence = _split_evidence(rows[0])
        assert rest == _expected_item(sqs_message, matched_rule=notice.matched_rule)
        assert evidence == {
            "error_class": "quota",
            "error_code": "minutes_exhausted",
            "delivery_id": sqs_message.delivery_id,
        }

        assert check_run_route.call_count == 1
        body: dict[str, Any] = json.loads(check_run_route.calls[-1].request.content)
        assert list(body) == _CHECK_RUN_BODY_KEYS
        assert list(body["output"]) == _CHECK_RUN_OUTPUT_KEYS
        assert body == {
            "name": "Trikon",
            "head_sha": sqs_message.head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": notice.title,
                "summary": notice.summary,
                "text": None,
            },
            "details_url": notice.details_url,
        }


@mock_aws()
def test_write_failure_verdict_with_notice_is_idempotent_on_redelivery(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
    log_stream: io.StringIO,
) -> None:
    """Feature: sandbox-minute-plans, Requirement 11.9 (idempotency unchanged).

    The natural-key ``ConditionExpression`` guards the notice path the
    same way: a second call for the same message leaves one row,
    logs INFO ``verdict_already_exists``, and posts no second Check Run.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client:
            _write_once(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=_make_notice(),
                calls=2,
            )

        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        assert rows[0]["matched_rule"]["S"] == "sandbox minutes exhausted"
        assert check_run_route.call_count == 1

    records = _log_records(log_stream)
    already = [r for r in records if r["message"] == "verdict_already_exists"]
    assert len(already) == 1
    assert already[0]["level"] == "INFO"
    assert already[0]["sk"] == f"{sqs_message.sent_at}#{sqs_message.delivery_id}"


@mock_aws()
def test_write_failure_verdict_with_notice_logs_check_run_failure_without_raising(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
    log_stream: io.StringIO,
) -> None:
    """Feature: sandbox-minute-plans, Requirement 11.10 (partial success unchanged).

    When the notice's Check Run POST exhausts its retries, the row is
    still written, the function returns normally, and the failure is
    logged at ERROR as ``check_run_post_failed`` followed by
    ``orchestrator_failure_resolved``.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        check_run_route = _mock_check_run_route(
            respx_mock, response=httpx.Response(500)
        )
        with httpx.Client() as http_client:
            _write_once(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=_make_notice(),
            )

        rows = _scan_verdicts_rows()
        assert len(rows) == 1
        assert rows[0]["matched_rule"]["S"] == "sandbox minutes exhausted"
        assert check_run_route.call_count == 3
        body: dict[str, Any] = json.loads(check_run_route.calls[-1].request.content)
        assert body["conclusion"] == "action_required"

    records = _log_records(log_stream)
    messages = [r["message"] for r in records]
    failed = [r for r in records if r["message"] == "check_run_post_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "ERROR"
    assert failed[0]["error_type"] == "OrchestratorGithubClientError"
    assert "500" in failed[0]["error_message"]
    assert messages.index("check_run_post_failed") < messages.index(
        "orchestrator_failure_resolved"
    )


# ---------------------------------------------------------------------------
# trikon-cloud-cost-cuts — Spot-related Fail_Closed_Results (Requirement 11).
# ---------------------------------------------------------------------------

_INTERIM_CHECK_RUN_ID = 777

#: Key order of the PATCH body: no ``name`` / ``head_sha`` on update.
_CHECK_RUN_PATCH_BODY_KEYS: list[str] = ["status", "conclusion", "output", "details_url"]


def _target(sqs_message: SqsJobMessage) -> FailClosedTarget:
    return FailClosedTarget(
        installation_id=sqs_message.installation_id,
        repo_full_name=sqs_message.repo_full_name,
        head_sha=sqs_message.head_sha,
        pr_number=sqs_message.pr_number,
    )


def _mock_patch_route(
    respx_mock: respx.MockRouter,
    *,
    check_run_id: int = _INTERIM_CHECK_RUN_ID,
    response: httpx.Response | None = None,
) -> respx.Route:
    """Register ``PATCH /repos/{repo}/check-runs/{id}``."""
    if response is None:
        response = httpx.Response(200, json={"id": check_run_id})
    return respx_mock.patch(
        f"/repos/{CANONICAL_REPO_FULL_NAME}/check-runs/{check_run_id}"
    ).mock(return_value=response)


def _publish(
    *,
    sqs_message: SqsJobMessage,
    row_message: SqsJobMessage | None,
    parameter_name: str,
    http_client: httpx.Client,
    complete_check_run_id: int | None,
) -> FailClosedPublication:
    return publish_fail_closed_result(
        target=_target(sqs_message),
        row_message=row_message,
        notice=_make_notice(),
        verdicts_table="trikon_verdicts",
        ddb_client=boto3.client("dynamodb", region_name="us-east-1"),
        github_client=_build_client(parameter_name=parameter_name, http_client=http_client),
        complete_check_run_id=complete_check_run_id,
    )


@mock_aws()
def test_publish_fail_closed_result_patches_interim_check_run(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirements 11.1-11.4, 11.7 (PATCH).

    With a ``complete_check_run_id`` the Interim_Check_Run is PATCHed to
    ``completed`` with the notice's conclusion, copy and details URL; no
    new Check Run is created. The row carries the notice's rule.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)
    notice = _make_notice()

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        patch_route = _mock_patch_route(respx_mock)
        with httpx.Client() as http_client:
            result = _publish(
                sqs_message=sqs_message,
                row_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                complete_check_run_id=_INTERIM_CHECK_RUN_ID,
            )

        assert result == FailClosedPublication(row="written", check_run="completed")
        assert post_route.call_count == 0
        assert patch_route.call_count == 1
        body: dict[str, Any] = json.loads(patch_route.calls[-1].request.content)
        assert list(body) == _CHECK_RUN_PATCH_BODY_KEYS
        assert body == {
            "status": "completed",
            "conclusion": "action_required",
            "output": {"title": notice.title, "summary": notice.summary, "text": None},
            "details_url": notice.details_url,
        }

    rows = _scan_verdicts_rows()
    assert len(rows) == 1
    rest, evidence = _split_evidence(rows[0])
    assert rest == _expected_item(sqs_message, matched_rule=notice.matched_rule)
    assert evidence["error_class"] == "quota"


@mock_aws()
def test_publish_fail_closed_result_falls_back_to_post_when_patch_fails(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11 (PATCH -> POST fallback).

    A 404 on the PATCH (the Interim_Check_Run is gone) falls back to a
    POST of a completed Check Run on the target head.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        patch_route = _mock_patch_route(respx_mock, response=httpx.Response(404))
        with httpx.Client() as http_client:
            result = _publish(
                sqs_message=sqs_message,
                row_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                complete_check_run_id=_INTERIM_CHECK_RUN_ID,
            )

        assert result == FailClosedPublication(row="written", check_run="created")
        assert patch_route.call_count == 1
        assert post_route.call_count == 1
        body: dict[str, Any] = json.loads(post_route.calls[-1].request.content)
        assert list(body) == _CHECK_RUN_BODY_KEYS
        assert body["head_sha"] == sqs_message.head_sha
        assert body["status"] == "completed"
        assert body["conclusion"] == "action_required"


@mock_aws()
def test_publish_fail_closed_result_existed_row_still_posts_check_run(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.6 (re-delivery).

    A re-delivered event finds the row (``existed``) and still posts the
    Check Run, so an event whose first POST failed publishes on retry.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client:
            results = [
                _publish(
                    sqs_message=sqs_message,
                    row_message=sqs_message,
                    parameter_name=parameter_name,
                    http_client=http_client,
                    complete_check_run_id=None,
                )
                for _ in range(2)
            ]

        assert results == [
            FailClosedPublication(row="written", check_run="created"),
            FailClosedPublication(row="existed", check_run="created"),
        ]
        assert post_route.call_count == 2
    assert len(_scan_verdicts_rows()) == 1


@mock_aws()
def test_publish_fail_closed_result_without_row_message_skips_row(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.4 (no row message)."""
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client:
            result = _publish(
                sqs_message=sqs_message,
                row_message=None,
                parameter_name=parameter_name,
                http_client=http_client,
                complete_check_run_id=None,
            )

        assert result == FailClosedPublication(row="skipped", check_run="created")
        assert post_route.call_count == 1
    assert _scan_verdicts_rows() == []


@mock_aws()
def test_publish_fail_closed_result_propagates_dynamodb_error(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.6 (DynamoDB error).

    A non-conditional ``ClientError`` (missing table) propagates and no
    Check Run is posted.
    """
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        post_route = _mock_check_run_route(respx_mock)
        with httpx.Client() as http_client, pytest.raises(ClientError) as exc_info:
            _publish(
                sqs_message=sqs_message,
                row_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                complete_check_run_id=None,
            )
        assert post_route.call_count == 0
    assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"


@mock_aws()
def test_publish_fail_closed_result_propagates_github_error(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.6 (GitHub error).

    When both the PATCH and the fallback POST fail, the
    :class:`OrchestratorGithubClientError` propagates (strict, unlike
    the partial-success orchestrator path). The row was written.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock, response=httpx.Response(422))
        patch_route = _mock_patch_route(respx_mock, response=httpx.Response(404))
        with httpx.Client() as http_client, pytest.raises(OrchestratorGithubClientError):
            _publish(
                sqs_message=sqs_message,
                row_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                complete_check_run_id=_INTERIM_CHECK_RUN_ID,
            )
        assert patch_route.call_count == 1
        assert post_route.call_count == 1
    assert len(_scan_verdicts_rows()) == 1


def _create_pr_state_table() -> None:
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="trikon_pr_state",
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_pr_state(*, pr_key: str, head_sha: str, check_run_id: dict[str, Any]) -> None:
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName="trikon_pr_state",
        Item={
            "pr_key": {"S": pr_key},
            "last_head_sha": {"S": head_sha},
            "last_comment_id": {"NULL": True},
            "last_check_run_id": check_run_id,
            "last_updated_at": {"S": "2025-01-01T00:00:00.000+00:00"},
        },
    )


_PR_KEY = f"{CANONICAL_INSTALLATION_ID}#{CANONICAL_REPO_FULL_NAME}#42"


@mock_aws()
@pytest.mark.parametrize(
    ("stored_head", "stored_id", "expected"),
    [
        (CANONICAL_HEAD_SHA, {"N": str(_INTERIM_CHECK_RUN_ID)}, _INTERIM_CHECK_RUN_ID),
        ("0" * 40, {"N": str(_INTERIM_CHECK_RUN_ID)}, None),
        (CANONICAL_HEAD_SHA, {"NULL": True}, None),
    ],
)
def test_read_interim_check_run_id_matches_head_only(
    stored_head: str, stored_id: dict[str, Any], expected: int | None
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.5 (pr_state read)."""
    _create_pr_state_table()
    _put_pr_state(pr_key=_PR_KEY, head_sha=stored_head, check_run_id=stored_id)
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    assert (
        read_interim_check_run_id(
            ddb, table="trikon_pr_state", pr_key=_PR_KEY, head_sha=CANONICAL_HEAD_SHA
        )
        == expected
    )


@mock_aws()
def test_read_interim_check_run_id_missing_row_or_client_error_returns_none() -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.5 (read failures)."""
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    # No table yet: ResourceNotFoundException -> None.
    assert (
        read_interim_check_run_id(
            ddb, table="trikon_pr_state", pr_key=_PR_KEY, head_sha=CANONICAL_HEAD_SHA
        )
        is None
    )
    _create_pr_state_table()
    assert (
        read_interim_check_run_id(
            ddb, table="trikon_pr_state", pr_key=_PR_KEY, head_sha=CANONICAL_HEAD_SHA
        )
        is None
    )


def _write_with_complete_id(
    *,
    sqs_message: SqsJobMessage,
    parameter_name: str,
    http_client: httpx.Client,
    notice: FailClosedNotice | None,
    complete_check_run_id: int | None,
) -> None:
    write_orchestrator_failure_verdict(
        sqs_message=sqs_message,
        error_class="terminal",
        error_code="AccessDeniedException",
        boto3_session=boto3.session.Session(region_name="us-east-1"),
        github_client=_build_client(parameter_name=parameter_name, http_client=http_client),
        env=OrchestratorEnvConfig(),
        notice=notice,
        complete_check_run_id=complete_check_run_id,
    )


@mock_aws()
def test_write_failure_verdict_with_complete_check_run_id_patches(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.5 (Retry_Job terminal path).

    The Interim_Check_Run is PATCHed with the notice's copy; no POST.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)
    notice = _make_notice()

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        patch_route = _mock_patch_route(respx_mock)
        with httpx.Client() as http_client:
            _write_with_complete_id(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=notice,
                complete_check_run_id=_INTERIM_CHECK_RUN_ID,
            )
        assert post_route.call_count == 0
        assert patch_route.call_count == 1
        body: dict[str, Any] = json.loads(patch_route.calls[-1].request.content)
        assert body["conclusion"] == "action_required"
        assert body["output"]["summary"] == notice.summary
    assert len(_scan_verdicts_rows()) == 1


@mock_aws()
def test_write_failure_verdict_with_complete_check_run_id_keeps_partial_success(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
    log_stream: io.StringIO,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.5 (fallback, partial success).

    A failed PATCH falls back to the POST; when the POST also fails,
    the error is logged ``check_run_post_failed`` and not raised.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock, response=httpx.Response(422))
        patch_route = _mock_patch_route(respx_mock, response=httpx.Response(404))
        with httpx.Client() as http_client:
            _write_with_complete_id(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=_make_notice(),
                complete_check_run_id=_INTERIM_CHECK_RUN_ID,
            )
        assert patch_route.call_count == 1
        assert post_route.call_count == 1

    messages = [r["message"] for r in _log_records(log_stream)]
    assert "check_run_post_failed" in messages
    assert messages[-1] == "orchestrator_failure_resolved"


@mock_aws()
def test_write_failure_verdict_default_complete_id_never_patches(
    rsa_test_private_key_pem: bytes,
    sqs_message: SqsJobMessage,
) -> None:
    """Feature: trikon-cloud-cost-cuts, Requirement 11.5 (default path unchanged).

    ``complete_check_run_id=None`` posts the historical neutral body and
    sends no PATCH.
    """
    _create_verdicts_table()
    parameter_name = _create_app_key_parameter(rsa_test_private_key_pem)

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        _mock_installation_token_route(respx_mock)
        post_route = _mock_check_run_route(respx_mock)
        patch_route = _mock_patch_route(respx_mock)
        with httpx.Client() as http_client:
            _write_with_complete_id(
                sqs_message=sqs_message,
                parameter_name=parameter_name,
                http_client=http_client,
                notice=None,
                complete_check_run_id=None,
            )
        assert patch_route.call_count == 0
        assert post_route.call_count == 1
        body: dict[str, Any] = json.loads(post_route.calls[-1].request.content)
        assert list(body) == _CHECK_RUN_BODY_KEYS
        assert body["conclusion"] == "neutral"
        assert body["output"]["title"] == "Trikon Cloud verification unavailable"
