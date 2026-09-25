# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass, plus the moto / respx / boto3 /
# powertools boundary values that flow through this file are typed
# ``Any`` upstream. Under the repo's ``disallow_any_explicit = true``
# mypy config, every one of those touches surfaces as an
# ``explicit-any`` error the plugin generates, not code we write.
# Silence at file scope — this is a test module, not a production
# module, and the ``Any`` here is bounded to fixture data that flows
# into pytest tests.
# mypy: disable-error-code="explicit-any"
"""End-to-end unit tests for :mod:`trikon_cloud.orchestrator.handler` (task 17.5).

Feature: trikon-cloud-orchestrator, Properties 4, 5, 6, 7, 12.

Covers the eight-step per-record flow prescribed by design.md §4.1
verbatim:

* STEP 1 — SQS-level log context attached BEFORE any parse so
  payload-gate and validation failures still carry ``sqs_message_id``
  and ``approximate_receive_count``.
* STEP 2 — payload-size gate: bodies over 256 KiB are rejected as
  Terminal (Requirement 2.1) and the handler emits an
  ``orchestrator_payload_rejected`` ERROR record with
  ``reason="payload_exceeds_256kb"`` plus a best-effort
  ``delivery_id`` extraction.
* STEP 3 — malformed-body gate: bodies that fail
  :class:`~trikon_cloud.webhook_receiver.models.SqsJobMessage`
  validation emit ``orchestrator_payload_rejected`` with
  ``reason="malformed_sqs_body"`` and an ``errors`` list
  (Requirement 2.2).
* STEP 4 — natural-key log context propagation: the five fields
  ``installation_id``, ``repo_full_name``, ``pr_number``,
  ``delivery_id``, ``event_type`` land on every subsequent log
  record within the invocation (Requirement 9.2).
* STEP 5 — SSM revision resolution: the resolver-supplied
  ``trikon-verify-runner:<revision>`` flows verbatim into
  :func:`build_run_task_call` (Requirement 4.1).
* STEP 7 — Transient (Requirement 6.1) and Terminal
  (Requirement 7.1 Never-Fail-Open) branches.
* STEP 8 — success log carries ``dispatched_task_arn``
  (Requirement 9.4).

Plus the cross-cutting invariants:

* Requirement 1.1 — batch size 1 (defensive ``assert``).
* Requirement 1.4 — no ``audit_id`` correlation key at this layer.
* Requirement 9.3 / Invariant 6 — no raw SQS body, no ``ecs.RunTask``
  response body, and no credential material in any log record.

**Setup**

* ``@mock_aws()`` on every test that touches AWS (DynamoDB, SSM,
  Secrets Manager) — moto intercepts boto3 calls.
* :mod:`respx` intercepts the two GitHub REST endpoints exercised
  on the Terminal path.
* Module caches on both :mod:`trikon_cloud.orchestrator.handler`
  (``_ENV``, ``_RESOLVER``, ``_BOTO_SESSION``, ``_GITHUB_CLIENT``)
  and :mod:`trikon_cloud.orchestrator.never_fail_open`
  (``_TOKEN_CACHE``, ``_APP_JWT_CACHE``) are reset via autouse
  fixtures so every test cold-starts.
* :meth:`aws_lambda_powertools.Logger.clear_state` is invoked by
  the sibling ``conftest`` autouse fixture, so append-key context
  from a prior test cannot leak into this one.
* :func:`time.sleep` is no-op'd by a sibling ``conftest`` autouse
  fixture — the Never-Fail-Open partial-success path is not
  exercised here but the shim keeps the file resilient to future
  additions.

**Log capture**

The :func:`log_stream` fixture rebinds the powertools stdlib logger's
:class:`logging.StreamHandler` stream to a :class:`io.StringIO` for
the duration of a test so :func:`_log_records` can parse the JSON
lines back into a list of dicts for assertion. Mirrors the pattern in
:mod:`trikon_cloud.orchestrator.tests.test_logger`.

**Dispatch stubs**

``submit_run_task`` is monkeypatched on the handler module for both
the happy path (returns a canned :class:`RunTaskDispatchResult`) and
the failure paths (raises the configured
:class:`~botocore.exceptions.ClientError`). This keeps the test
surface focused on the handler's eight-step control flow rather than
on moto ECS' handling of the ``run_task`` request shape.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import IO, Any, cast
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
import respx
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.orchestrator import handler as handler_module
from trikon_cloud.orchestrator import never_fail_open as never_fail_open_module
from trikon_cloud.orchestrator.ecs_dispatcher import (
    RunTaskDispatchResult,
    TransientDispatchError,
)
from trikon_cloud.orchestrator.handler import lambda_handler
from trikon_cloud.orchestrator.logger import LOGGING_DENYLIST
from trikon_cloud.orchestrator.models import RunTaskCall

from .conftest import (
    CANONICAL_DELIVERY_ID,
    CANONICAL_EVENT_TYPE,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_PR_NUMBER,
    CANONICAL_REPO_FULL_NAME,
    make_sqs_job_message,
)

# ---------------------------------------------------------------------------
# Module-level constants used across tests.
# ---------------------------------------------------------------------------

# Canonical task ARN returned by the happy-path ``submit_run_task``
# stub — asserted verbatim on the ``run_task_dispatched`` INFO record
# (Requirement 9.4). A concrete ECS task ARN shape is used (not just
# "task-arn") so a hypothetical future regression that stripped a
# path segment would surface on the equality check.
_CANONICAL_TASK_ARN: str = (
    "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/abcdef0123456789"
)

# 256 KiB + a small padding — the payload-size gate rejects anything
# strictly greater than ``262_144`` bytes. This margin comfortably
# clears the boundary without becoming unwieldy in the test module.
_OVERSIZED_PADDING_LENGTH: int = 300_000


# ---------------------------------------------------------------------------
# Autouse fixtures — AWS creds, module cache resets, httpx compat shim.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate testing AWS credentials so ``@mock_aws()`` activates cleanly.

    moto rejects boto3 calls that arrive without any credential set,
    even in mock mode. The four aliases below are the canonical
    testing values used across the Spec-2/3 test suites; matching
    them keeps behavior consistent with the sibling test modules
    (:mod:`~trikon_cloud.orchestrator.tests.test_never_fail_open`).
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _reset_handler_module_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force cold-start bootstrap on every test by zeroing the four caches.

    :mod:`trikon_cloud.orchestrator.handler` keeps its cold-start
    caches (:data:`_ENV`, :data:`_RESOLVER`, :data:`_BOTO_SESSION`,
    :data:`_GITHUB_CLIENT`) at module scope so warm Lambda
    invocations reuse them. Without a per-test reset the first test's
    cached env / boto3 session / resolver would poison every
    subsequent test's environment. :meth:`pytest.MonkeyPatch.setattr`
    auto-reverts on teardown so the module returns to whatever cached
    state it accumulated after each test.
    """
    monkeypatch.setattr(handler_module, "_ENV", None)
    monkeypatch.setattr(handler_module, "_RESOLVER", None)
    monkeypatch.setattr(handler_module, "_BOTO_SESSION", None)
    monkeypatch.setattr(handler_module, "_GITHUB_CLIENT", None)


@pytest.fixture(autouse=True)
def _reset_never_fail_open_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear :data:`_TOKEN_CACHE` and :data:`_APP_JWT_CACHE` between tests.

    Both caches live at module scope inside
    :mod:`trikon_cloud.orchestrator.never_fail_open` to model the
    container-lifetime cache the production Lambda uses. The
    Terminal-path test in this file mints a fresh token; without a
    reset a warm cache entry from an earlier test could suppress the
    mint round trip and invalidate ``call_count`` assertions.
    """
    never_fail_open_module._TOKEN_CACHE.clear()
    monkeypatch.setattr(never_fail_open_module, "_APP_JWT_CACHE", None)


@pytest.fixture(autouse=True)
def _httpx_respx_method_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge respx 0.21 + httpx 0.28's bytes-vs-str method mismatch.

    httpx 0.28 stopped auto-decoding a bytes ``method`` argument on
    :class:`httpx.Request` construction. httpcore passes the method
    as bytes when respx intercepts at the connection-pool layer, so
    the resulting :class:`httpx.Request` carries ``method=b"POST"``.
    respx 0.21's :class:`Method` matcher compares against the str
    ``"POST"`` and the route never resolves — every test then fails
    with :class:`AllMockedAssertionError`. Test-only shim mirroring
    the one in
    :mod:`~trikon_cloud.orchestrator.tests.test_never_fail_open`.
    """
    real_init = httpx.Request.__init__

    def _init(self: httpx.Request, method: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(method, bytes):
            method = method.decode("ascii")
        real_init(self, method, *args, **kwargs)

    monkeypatch.setattr(httpx.Request, "__init__", _init)


# ---------------------------------------------------------------------------
# Log capture — swap the powertools stream to a StringIO per test.
# ---------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's stream to a :class:`io.StringIO`.

    Powertools' :class:`~aws_lambda_powertools.Logger` wraps a stdlib
    :class:`logging.Logger` registered under the service name
    ``"trikon-cloud-orchestrator"``. A :class:`logging.StreamHandler`
    is attached at Logger construction time with a JSON formatter
    and ``stream=sys.stdout``. Swapping the handler's stream lets
    tests capture the exact JSON emitted for each log call
    independent of pytest's own stdout-capture mode. The original
    streams are restored on teardown so tests that follow this one
    in the same session see the handler configured as at import
    time. Same pattern as
    :mod:`trikon_cloud.orchestrator.tests.test_logger`.
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


# ---------------------------------------------------------------------------
# Helpers — LambdaContext stand-in, SQS envelope factory, AWS setup.
# ---------------------------------------------------------------------------


def _lambda_context() -> LambdaContext:
    """Return a :class:`unittest.mock.MagicMock` typed as :class:`LambdaContext`.

    The handler's body does ``del context`` — the value is never
    consumed. The mock only exists to satisfy the parameter type at
    the call site.
    """
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-orchestrator-test"
    return cast(LambdaContext, ctx)


def _make_sqs_event(
    *,
    body: str,
    message_id: str = "sqs-msg-1",
    receive_count: str = "1",
) -> dict[str, object]:
    """Assemble a one-record SQS Lambda event envelope.

    Matches the wire shape :class:`SqsEventEnvelope` validates
    against (design.md §3.4). ``ApproximateReceiveCount`` is a string
    per AWS' SQS contract; the handler parses it to :class:`int` at
    STEP 1.
    """
    return {
        "Records": [
            {
                "messageId": message_id,
                "receiptHandle": "handle-1",
                "body": body,
                "attributes": {"ApproximateReceiveCount": receive_count},
            }
        ]
    }


def _make_two_record_event() -> dict[str, object]:
    """Assemble a two-record SQS Lambda event envelope for the batch-size test.

    Requirement 1.1 pins ``BatchSize=1`` at the event-source-mapping
    layer; the handler defensively asserts on entry. This helper
    builds the event that trips that assertion.
    """
    body = make_sqs_job_message().model_dump_json()
    return {
        "Records": [
            {
                "messageId": f"sqs-msg-{i}",
                "receiptHandle": f"handle-{i}",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "1"},
            }
            for i in (1, 2)
        ]
    }


def _create_ssm_param(value: str = "17") -> None:
    """Create ``/trikon/verify-runner/active-revision`` under moto.

    The resolver reads this parameter once per cold start and returns
    ``f"trikon-verify-runner:{value}"`` (Requirement 4.1). Default
    ``"17"`` matches the concrete task-definition string asserted on
    in the STEP 5 test.
    """
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/trikon/verify-runner/active-revision",
        Value=value,
        Type="String",
    )


def _create_verdicts_table() -> None:
    """Create the ``trikon_verdicts`` DynamoDB table under moto.

    Key schema mirrors design.md §5.2 (Spec 2) verbatim: partition
    key ``installation_id`` (``N``), sort key ``sk`` (``S``),
    ``PAY_PER_REQUEST`` billing. Required only for the Terminal-path
    test — the happy path and payload-gate paths never touch the
    verdicts table.
    """
    boto3.client("dynamodb", region_name="us-east-1").create_table(
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
    """Create a Secrets Manager secret carrying the RSA PEM, return its moto ARN.

    moto assigns its own randomized ARN suffix on ``create_secret``,
    so the caller pins the resulting ARN into
    ``TRIKON_APP_PRIVATE_KEY_SECRET_ARN`` via
    :meth:`pytest.MonkeyPatch.setenv` BEFORE the handler bootstraps
    (the sibling ``conftest`` autouse fixture sets a placeholder ARN
    that moto would otherwise reject).
    """
    resp = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name="trikon/app-key-test-handler",
        SecretString=pem.decode("utf-8"),
    )
    return str(resp["ARN"])


# ---------------------------------------------------------------------------
# Dispatch stubs — patch ``handler.submit_run_task`` on demand.
# ---------------------------------------------------------------------------


def _stub_submit_run_task_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_arn: str = _CANONICAL_TASK_ARN,
) -> list[RunTaskCall]:
    """Patch ``handler.submit_run_task`` to record the call and return success.

    Returns the ``list`` the stub appends to on each invocation —
    tests inspect ``captured[0].taskDefinition`` (STEP 5) and
    ``captured[0].overrides.containerOverrides[0].environment``
    (STEP 4 propagation).
    """
    captured: list[RunTaskCall] = []

    def _stub(call: RunTaskCall, *, ecs_client: object) -> RunTaskDispatchResult:
        del ecs_client
        captured.append(call)
        return RunTaskDispatchResult(task_arn=task_arn)

    monkeypatch.setattr(handler_module, "submit_run_task", _stub)
    return captured


def _stub_submit_run_task_raise(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_code: str,
) -> list[RunTaskCall]:
    """Patch ``handler.submit_run_task`` to raise a ClientError with ``error_code``.

    Drives the STEP 7 Transient / Terminal classification branches.
    Returns the same recording list so tests can assert that the
    stub was invoked exactly once before the exception surfaced.
    """
    captured: list[RunTaskCall] = []

    def _stub(call: RunTaskCall, *, ecs_client: object) -> RunTaskDispatchResult:
        del ecs_client
        captured.append(call)
        raise ClientError(
            {"Error": {"Code": error_code, "Message": "injected by test"}},
            "RunTask",
        )

    monkeypatch.setattr(handler_module, "submit_run_task", _stub)
    return captured


# ---------------------------------------------------------------------------
# Helpers — log-record parsing and respx routes for the Terminal path.
# ---------------------------------------------------------------------------


def _log_records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse the captured log stream into a list of JSON records.

    Powertools emits one JSON object per log call, each on its own
    line. This helper filters blank lines and JSON-decodes the rest
    into a list of dicts. Empty when no log records were emitted.
    """
    lines = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _install_token_route(respx_mock: respx.MockRouter) -> respx.Route:
    """Register the ``POST /app/installations/{id}/access_tokens`` route.

    Far-future ``expires_at`` keeps the minted token inside the
    5-minute safety-margin window so
    :meth:`OrchestratorGithubClient._get_installation_token` returns
    the cache entry on any follow-up call within the same test.
    """
    return respx_mock.post(
        f"/app/installations/{CANONICAL_INSTALLATION_ID}/access_tokens"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_test_installation_token",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
    )


def _check_run_route(respx_mock: respx.MockRouter) -> respx.Route:
    """Register the ``POST /repos/{repo}/check-runs`` route (Terminal path)."""
    return respx_mock.post(
        f"/repos/{CANONICAL_REPO_FULL_NAME}/check-runs"
    ).mock(return_value=httpx.Response(201, json={"id": 42}))


# ---------------------------------------------------------------------------
# §1 — Happy path (Property 4 + Property 12 + Property 5).
# ---------------------------------------------------------------------------


@mock_aws()
def test_happy_path_returns_empty_batch_and_dispatches_once(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 4: RunTaskCall shape invariant.

    Valid ``SqsJobMessage`` → STEP 5 SSM resolve (``"17"`` →
    ``"trikon-verify-runner:17"``) → STEP 6 pure builder → STEP 7
    dispatch (stubbed success) → STEP 8 success log → return
    ``{"batchItemFailures": []}``. Load-bearing end-to-end coverage —
    every subsequent test builds on this scaffold and asserts one
    additional invariant.
    """
    _create_ssm_param()
    captured = _stub_submit_run_task_success(monkeypatch)

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    result = lambda_handler(event, _lambda_context())

    assert result == {"batchItemFailures": []}
    # STEP 7 fired exactly once — Requirement 3.1.
    assert len(captured) == 1
    records = _log_records(log_stream)
    dispatched = [r for r in records if r.get("message") == "run_task_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["dispatched_task_arn"] == _CANONICAL_TASK_ARN


# ---------------------------------------------------------------------------
# §2 — STEP 1 log context (Requirement 9.2 pre-parse enrichment).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_1_log_context_attached_before_parse(
    log_stream: io.StringIO,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    STEP 1 attaches ``sqs_message_id`` and
    ``approximate_receive_count`` to the log context BEFORE STEP 2's
    payload gate runs, so a payload-gate rejection still carries the
    SQS-level correlation keys (design.md §4.1 STEP 1). Trigger the
    gate with an oversize body — the resulting ``ERROR`` record must
    carry both keys.
    """
    _create_ssm_param()
    # Oversize the body so STEP 2 rejects it. The body is not valid
    # JSON either, but STEP 2 checks byte-length BEFORE STEP 3's
    # parse — the gate fires first.
    body = "x" * _OVERSIZED_PADDING_LENGTH
    event = _make_sqs_event(
        body=body, message_id="sqs-msg-step1", receive_count="3"
    )

    result = lambda_handler(event, _lambda_context())

    assert result == {"batchItemFailures": []}
    records = _log_records(log_stream)
    rejected = [r for r in records if r.get("message") == "orchestrator_payload_rejected"]
    assert len(rejected) == 1
    # Both STEP-1 correlation keys land on the payload-gate ERROR
    # record — proof that STEP 1 ran before STEP 2's ``return``.
    assert rejected[0]["sqs_message_id"] == "sqs-msg-step1"
    assert rejected[0]["approximate_receive_count"] == 3


# ---------------------------------------------------------------------------
# §3 — STEP 2 payload-size gate (Requirement 2.1).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_2_payload_size_gate_rejects_oversized_body(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    Bodies over 256 KiB are rejected as Terminal (Requirement 2.1) —
    the handler logs ``orchestrator_payload_rejected`` with
    ``reason="payload_exceeds_256kb"``, extracts a best-effort
    ``delivery_id`` via :func:`_try_extract_delivery_id`, returns
    ``{"batchItemFailures": []}``, and MUST NOT invoke
    :func:`submit_run_task`. SSM resolution likewise never runs
    (the gate returns before STEP 5). Github respx routes are
    registered with ``assert_all_called=False`` to document — via
    ``call_count == 0`` — that no HTTP round-trip fired either.
    """
    _create_ssm_param()
    captured = _stub_submit_run_task_success(monkeypatch)
    # Valid JSON with a ``delivery_id`` field plus a large padding
    # payload — the padding pushes the body over the 256 KiB gate
    # while the JSON envelope keeps ``delivery_id`` extractable.
    body_payload: dict[str, object] = {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "delivery_id": CANONICAL_DELIVERY_ID,
        "padding": "x" * _OVERSIZED_PADDING_LENGTH,
    }
    body = json.dumps(body_payload)
    assert len(body.encode("utf-8")) > 262_144  # pre-condition sanity.
    event = _make_sqs_event(body=body)

    with respx.mock(
        base_url="https://api.github.com", assert_all_called=False
    ) as respx_mock:
        token_route = _install_token_route(respx_mock)
        check_run_route = _check_run_route(respx_mock)
        result = lambda_handler(event, _lambda_context())

    assert result == {"batchItemFailures": []}
    # No dispatch, no GitHub POST, no downstream side effects.
    assert captured == []
    assert token_route.call_count == 0
    assert check_run_route.call_count == 0

    records = _log_records(log_stream)
    rejected = [r for r in records if r.get("message") == "orchestrator_payload_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "payload_exceeds_256kb"
    assert rejected[0]["delivery_id"] == CANONICAL_DELIVERY_ID


# ---------------------------------------------------------------------------
# §4 — STEP 3 malformed-body gate (Requirement 2.2).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_3_malformed_body_rejected_with_errors_list(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    Bodies that fail :class:`SqsJobMessage` validation are rejected
    as Terminal (Requirement 2.2). Log record carries
    ``reason="malformed_sqs_body"`` and an ``errors`` list with
    ``{"loc", "type"}`` tuples per pydantic — enough for operators
    to diagnose the producer bug without the raw body ever entering
    the log surface (Invariant 6). ``submit_run_task`` MUST NOT
    fire.
    """
    _create_ssm_param()
    captured = _stub_submit_run_task_success(monkeypatch)
    # Not valid JSON — pydantic's ``model_validate_json`` raises
    # :class:`ValidationError` on the JSON decode failure before it
    # even inspects the schema.
    event = _make_sqs_event(body="not-valid-json{")

    result = lambda_handler(event, _lambda_context())

    assert result == {"batchItemFailures": []}
    assert captured == []

    records = _log_records(log_stream)
    rejected = [r for r in records if r.get("message") == "orchestrator_payload_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "malformed_sqs_body"
    # The ``errors`` field is a non-empty list of dicts each carrying
    # ``loc`` and ``type`` keys — pydantic's canonical error shape.
    errors = rejected[0]["errors"]
    assert isinstance(errors, list)
    assert len(errors) >= 1
    for entry in errors:
        assert "loc" in entry
        assert "type" in entry


# ---------------------------------------------------------------------------
# §5 — STEP 4 log context propagation (Requirement 9.2).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_4_log_context_propagation_after_parse(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    After STEP 3 parses the ``SqsJobMessage`` and STEP 4 invokes
    :func:`append_job_context`, every log record within the same
    invocation carries the five natural-key fields byte-identical to
    the source message (Requirement 9.2). The ``run_task_dispatched``
    INFO record at STEP 8 is the natural probe — asserting all five
    keys land there validates propagation across STEP 5 → 8.
    """
    _create_ssm_param()
    _stub_submit_run_task_success(monkeypatch)

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    lambda_handler(event, _lambda_context())

    records = _log_records(log_stream)
    dispatched = [r for r in records if r.get("message") == "run_task_dispatched"]
    assert len(dispatched) == 1
    record = dispatched[0]
    assert record["installation_id"] == CANONICAL_INSTALLATION_ID
    assert record["repo_full_name"] == CANONICAL_REPO_FULL_NAME
    assert record["pr_number"] == CANONICAL_PR_NUMBER
    assert record["delivery_id"] == CANONICAL_DELIVERY_ID
    assert record["event_type"] == CANONICAL_EVENT_TYPE


# ---------------------------------------------------------------------------
# §6 — STEP 5 SSM revision resolution (Property 5, Requirement 4.1).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_5_ssm_task_definition_flows_verbatim_to_build_run_task_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 5: taskDefinition pinned family:revision.

    The resolver reads ``/trikon/verify-runner/active-revision`` from
    SSM, parses the value ``"17"`` as an ``int``, and returns the
    fully qualified ``"trikon-verify-runner:17"`` string
    (Requirement 4.1). That exact string flows verbatim into
    :func:`build_run_task_call`'s ``task_definition`` parameter and
    surfaces on :attr:`RunTaskCall.taskDefinition`. Byte-identity
    matters because the ECS ``RunTask`` API rejects any suffix other
    than ``family:<int>``.
    """
    _create_ssm_param(value="17")
    captured = _stub_submit_run_task_success(monkeypatch)

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    lambda_handler(event, _lambda_context())

    assert len(captured) == 1
    call = captured[0]
    assert call.taskDefinition == "trikon-verify-runner:17"
    # Requirement 4.2 — no ``:LATEST`` suffix on the wire.
    assert ":LATEST" not in call.taskDefinition


# ---------------------------------------------------------------------------
# §7 — STEP 7 Transient failure (Requirement 6.1).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_7_transient_failure_raises_transient_dispatch_error(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 6: Transient vs Terminal classification.

    ``ecs.RunTask`` raising :class:`ClientError` with
    ``Code="ThrottlingException"`` classifies as ``"transient"``
    (design.md §4.2). The handler logs
    ``run_task_transient_failure`` at WARN level and raises
    :class:`TransientDispatchError` so the Lambda runtime marks the
    batch as a failure and SQS returns the message for redrive
    (Requirement 6.1). No ``trikon_verdicts`` row is written — the
    Terminal Never-Fail-Open branch does not run.
    """
    _create_ssm_param()
    captured = _stub_submit_run_task_raise(monkeypatch, error_code="ThrottlingException")

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)

    with pytest.raises(TransientDispatchError):
        lambda_handler(event, _lambda_context())

    assert len(captured) == 1
    records = _log_records(log_stream)
    warnings = [r for r in records if r.get("message") == "run_task_transient_failure"]
    assert len(warnings) == 1
    assert warnings[0]["error_class"] == "transient"
    assert warnings[0]["aws_error_code"] == "ThrottlingException"
    # No Terminal-branch log record — the Transient path exits before
    # writing a synthetic verdict.
    assert not [r for r in records if r.get("message") == "run_task_terminal_failure"]


# ---------------------------------------------------------------------------
# §8 — STEP 7 Terminal Never-Fail-Open (Requirement 7.1).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_7_terminal_failure_writes_verdict_and_posts_check_run(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    rsa_test_private_key_pem: bytes,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 7: Never-Fail-Open + idempotence.

    ``ecs.RunTask`` raising :class:`ClientError` with
    ``Code="TaskDefinitionNotFound"`` classifies as ``"terminal"``
    (design.md §4.2). The handler logs ``run_task_terminal_failure``
    at ERROR level with ``error_class="terminal"``, invokes
    :func:`write_orchestrator_failure_verdict` — which writes one
    row to ``trikon_verdicts`` and POSTs one neutral Check Run —
    and returns ``{"batchItemFailures": []}`` so SQS deletes the
    message (Requirement 7.1 Never-Fail-Open).
    """
    _create_ssm_param()
    _create_verdicts_table()
    secret_arn = _create_app_key_secret(rsa_test_private_key_pem)
    # Override the placeholder ARN set by the sibling conftest
    # ``_env_setup`` fixture with the moto-assigned ARN so
    # :meth:`get_secret_value` inside the App-JWT mint succeeds.
    monkeypatch.setenv("TRIKON_APP_PRIVATE_KEY_SECRET_ARN", secret_arn)
    _stub_submit_run_task_raise(monkeypatch, error_code="TaskDefinitionNotFound")

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)

    with respx.mock(base_url="https://api.github.com") as respx_mock:
        token_route = _install_token_route(respx_mock)
        check_run_route = _check_run_route(respx_mock)
        result = lambda_handler(event, _lambda_context())

    assert result == {"batchItemFailures": []}
    # Exactly one row in the verdicts table with the synthetic shape.
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    scan = ddb.scan(TableName="trikon_verdicts")
    assert scan["Count"] == 1
    row = scan["Items"][0]
    assert row["decision"]["S"] == "require_human"
    assert row["matched_rule"]["S"] == "orchestrator terminal failure"
    # Check Run POSTed exactly once with the Invariant-7-pinned
    # ``name`` and the ``neutral`` conclusion.
    assert token_route.call_count == 1
    assert check_run_route.call_count == 1
    check_run_body: dict[str, Any] = json.loads(check_run_route.calls[-1].request.content)
    assert check_run_body["name"] == "Trikon"
    assert check_run_body["conclusion"] == "neutral"
    # ERROR log carries the classification tokens the Terminal branch
    # pins per design.md §4.1 STEP 7.
    records = _log_records(log_stream)
    errors = [r for r in records if r.get("message") == "run_task_terminal_failure"]
    assert len(errors) == 1
    assert errors[0]["error_class"] == "terminal"
    assert errors[0]["aws_error_code"] == "TaskDefinitionNotFound"


# ---------------------------------------------------------------------------
# §9 — STEP 8 success log (Requirement 9.4).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step_8_success_log_carries_dispatched_task_arn(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 12: Structured log context propagation.

    Requirement 9.4 pins the ``run_task_dispatched`` INFO record's
    ``dispatched_task_arn`` field to the ARN returned by
    :func:`submit_run_task`. Use a non-canonical ARN so the equality
    check is observably keyed to the stub's return value rather than
    coincidentally matching the module-level canonical constant.
    """
    _create_ssm_param()
    custom_arn = (
        "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/deadbeef42"
    )
    _stub_submit_run_task_success(monkeypatch, task_arn=custom_arn)

    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    lambda_handler(event, _lambda_context())

    records = _log_records(log_stream)
    dispatched = [r for r in records if r.get("message") == "run_task_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["dispatched_task_arn"] == custom_arn
    # Log level is INFO — Requirement 9.4 pins the success record at
    # INFO, not WARN / ERROR.
    assert dispatched[0]["level"] == "INFO"


# ---------------------------------------------------------------------------
# §10 — Requirement 1.1 batch size 1 defensive assertion.
# ---------------------------------------------------------------------------


@mock_aws()
def test_req_1_1_batch_size_greater_than_one_asserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1.1: the handler defensively asserts ``BatchSize=1``.

    The event-source mapping in Task 13 sets ``BatchSize=1`` on the
    ``trikon-verify-jobs`` queue; the handler's ``assert
    len(envelope.Records) == 1`` catches a misconfigured event-source
    mapping that would otherwise silently drop the tail of a
    multi-record batch. Python raises :class:`AssertionError` when
    the condition is false. No dispatch fires.
    """
    _create_ssm_param()
    captured = _stub_submit_run_task_success(monkeypatch)
    event = _make_two_record_event()

    with pytest.raises(AssertionError):
        lambda_handler(event, _lambda_context())

    # STEP 7 never reached — the assertion trips at STEP 1.
    assert captured == []


# ---------------------------------------------------------------------------
# §11 — Requirement 1.4 no ``audit_id`` correlation key.
# ---------------------------------------------------------------------------


@mock_aws()
def test_req_1_4_no_audit_id_field_in_log_records(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1.4: ``delivery_id`` is the single correlation key.

    The handler MUST NOT introduce a separate ``audit_id`` at this
    layer — downstream tiers (e.g. Spec 2's Fargate runner) may
    generate one, but the orchestrator's job is to preserve
    ``delivery_id`` byte-for-byte from :class:`SqsJobMessage`
    through the ``TRIKON_DELIVERY_ID`` env override. Assertion: no
    log record emitted by the happy path carries a key literally
    named ``audit_id``.
    """
    _create_ssm_param()
    _stub_submit_run_task_success(monkeypatch)
    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    lambda_handler(event, _lambda_context())

    records = _log_records(log_stream)
    assert records, "expected at least one log record on the happy path"
    for record in records:
        assert "audit_id" not in record, (
            f"Requirement 1.4 violation: audit_id key present in log record {record!r}"
        )


# ---------------------------------------------------------------------------
# §12 — Invariant 6 / Requirement 9.3: no raw body / response / creds.
# ---------------------------------------------------------------------------


@mock_aws()
def test_req_9_3_no_denylist_fields_or_raw_body_in_log_records(
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 9.3 / Invariant 6: no payload or credential leakage.

    Emitted log records MUST NOT carry any :data:`LOGGING_DENYLIST`
    key AND MUST NOT contain the raw SQS record body as a substring
    of the serialized JSON. The dispatched task ARN is the only
    field the handler reads out of the ECS response, so the
    response body itself never enters the log surface. Fake
    credential markers embedded in the message would surface here
    if the handler inadvertently logged the response.
    """
    _create_ssm_param()
    _stub_submit_run_task_success(monkeypatch)
    body = make_sqs_job_message().model_dump_json()
    event = _make_sqs_event(body=body)
    lambda_handler(event, _lambda_context())

    records = _log_records(log_stream)
    assert records, "expected at least one log record"
    for record in records:
        # No denylist key at the top level.
        for banned in LOGGING_DENYLIST:
            assert banned not in record, (
                f"Requirement 9.3 violation: denylisted key {banned!r} "
                f"present in log record {record!r}"
            )
        # No raw SQS body embedded verbatim in the serialized record.
        serialized = json.dumps(record)
        assert body not in serialized, (
            "Requirement 9.3 violation: raw SQS body appears in serialized "
            "log record"
        )
        # The full ECS response body is not something the handler
        # reads back — but the canonical task ARN's structural form
        # only leaks if a caller emitted the wrong field. Verify the
        # ARN, when present, only surfaces on the ``dispatched_task_arn``
        # field — never smuggled into any other slot.
        for key, value in record.items():
            if key == "dispatched_task_arn":
                continue
            if isinstance(value, str):
                assert _CANONICAL_TASK_ARN not in value, (
                    f"ECS task ARN leaked into log field {key!r}: {value!r}"
                )


# ---------------------------------------------------------------------------
# §13 — _bootstrap warm-start branch coverage (design.md §4.1).
# ---------------------------------------------------------------------------


def test_bootstrap_returns_cached_values_without_reinit_on_warm_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, design.md §4.1 module-scope caches.

    :func:`_bootstrap` is idempotent — a warm invocation that finds
    every cache slot populated MUST return the cached four-tuple
    without touching any constructor. The autouse
    :func:`_reset_handler_module_caches` fixture zeros every slot at
    test-start; this test then pre-populates all four with concrete
    values and asserts :func:`_bootstrap` returns them verbatim,
    exercising the ``is None`` False branch on each of the four
    ``if`` guards (partial-branch gaps at ``138 → 140``, ``140 → 142``,
    ``142 → 144``, ``144 → 156``).
    """
    # Locally-imported to keep this test's dependencies obvious —
    # the happy-path tests above use only the ``handler_module``
    # namespace, but the warm-cache assertion needs concrete types.
    from trikon_cloud.orchestrator.ecs_dispatcher import TaskDefinitionResolver
    from trikon_cloud.orchestrator.models import OrchestratorEnvConfig

    env = OrchestratorEnvConfig()
    resolver = TaskDefinitionResolver(env=env)
    session = boto3.session.Session()
    # A bare :class:`~unittest.mock.MagicMock` stands in for the
    # :class:`OrchestratorGithubClient` slot — :func:`_bootstrap`
    # returns it verbatim without dereferencing any attribute, so
    # ``spec=`` is unnecessary.
    gh_client = MagicMock()

    monkeypatch.setattr(handler_module, "_ENV", env)
    monkeypatch.setattr(handler_module, "_RESOLVER", resolver)
    monkeypatch.setattr(handler_module, "_BOTO_SESSION", session)
    monkeypatch.setattr(handler_module, "_GITHUB_CLIENT", gh_client)

    (
        result_env,
        result_resolver,
        result_session,
        result_gh,
    ) = handler_module._bootstrap()

    # Identity comparison — the function returned the SAME objects
    # it found in the cache slots, proving no ``is None`` body ran.
    assert result_env is env
    assert result_resolver is resolver
    assert result_session is session
    assert result_gh is gh_client


# ---------------------------------------------------------------------------
# §14 — _try_extract_delivery_id defensive-parse fallbacks (Requirement 9.3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("[1, 2, 3]", "non-dict JSON root (list)"),
        ("42", "non-dict JSON root (scalar int)"),
        ("{}", "dict without delivery_id key"),
        ('{"delivery_id": 42}', "delivery_id present but not a str"),
    ],
)
def test_try_extract_delivery_id_returns_none_on_non_string_delivery_id(
    body: str,
    reason: str,
) -> None:
    """Feature: trikon-cloud-orchestrator, Requirement 9.3 defensive parse.

    :func:`_try_extract_delivery_id` MUST return :data:`None` — never
    raise — for every valid-JSON body whose top-level shape does not
    surface a string ``delivery_id``. Covers the two negative-guard
    lines (``line 195`` non-dict root and ``line 199`` missing /
    non-string field) that the payload-gate happy-path test does not
    exercise, since the gate test always sends a dict body carrying
    a string ``delivery_id``.
    """
    del reason  # parametrize-id surfaces on pytest failure output only.
    assert handler_module._try_extract_delivery_id(body) is None


# ---------------------------------------------------------------------------
# §15 — _extract_aws_error_code log-safe fallbacks (Requirement 9.4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed_response",
    [
        "not-a-dict",
        {"Error": "not-a-dict"},
        {"Error": {"Code": 42}},
    ],
)
def test_extract_aws_error_code_returns_unknown_on_malformed_response(
    malformed_response: object,
) -> None:
    """Feature: trikon-cloud-orchestrator, Requirement 9.4 log-safe error class.

    A Lambda that crashes while logging a :class:`ClientError` is
    worse than one that logs ``"unknown"`` — the helper walks the
    ``response → Error → Code`` chain with per-step
    :func:`isinstance` guards and returns the sentinel on any
    negative branch. Covers the three fallback returns at
    ``line 215`` (non-dict response), ``line 218`` (non-dict
    ``Error``), and ``line 221`` (non-string ``Code``) — all
    unreachable via a real ``botocore`` ``ClientError`` on the wire
    but exercisable by mutating the constructor-set attribute
    directly.
    """
    # Construct a well-formed ClientError first, then override
    # :attr:`response` with the malformed shape. ``botocore`` is
    # imported ``# type: ignore[import-untyped]`` so ``.response``
    # types as ``Any`` and the assignment stays mypy-strict clean.
    exc = ClientError({"Error": {"Code": "sentinel"}}, "RunTask")
    exc.response = malformed_response
    assert handler_module._extract_aws_error_code(exc) == "unknown"
