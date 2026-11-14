"""moto examples for the four Parameter_Readers (cost-cuts Req 5.5, 5.6, 6.3).

The readers under test:

* webhook secret: :func:`trikon_cloud.webhook_receiver.handler._get_webhook_secret`
  (module-level cache ``_webhook_secret``, once per warm container);
* Runner App key: :func:`trikon_cloud.fargate_runner.entrypoint._fetch_app_private_key`
  (no caching, one ``GetParameter`` per call);
* Orchestrator App key:
  :meth:`trikon_cloud.orchestrator.never_fail_open.OrchestratorGithubClient._get_app_private_key_pem`
  (no PEM cache; the App JWT minted from it is cached for 9 minutes);
* :class:`trikon_cloud.usage_client.secret.UsageSecretReader` (per-instance cache,
  failures not cached, blank value rejected).

Every test runs inside ``mock_aws()`` with fake credentials and the
``no_network`` guard, so nothing reaches AWS. SSM calls are counted with a
``provide-client-params.ssm.GetParameter`` hook on a fresh boto3 default
session, which records the real request parameters of every client the
readers create or receive.

Secret_Values are generated at run time. After each read, success or failure,
no captured log record (stdlib records at DEBUG for the Trikon loggers, INFO
elsewhere, plus everything written to the stdout/stderr file descriptors, which
is where the Powertools and structlog loggers write), no exception message and
nothing in the exception chain (``str``, ``repr``, ``args``, attributes and the
formatted traceback of the exception, its ``__cause__`` and its ``__context__``)
contains the value. Third-party DEBUG output (botocore logs raw response bodies
at DEBUG) is outside the readers and not enabled here; no reader configures it.

The Property 3 tests use a counting in-memory SSM fake instead of moto, so the
property runs stay fast. The Property 4 tests at the end of the file use a
scripted SSM fake that serves generated outcomes (success, blank value,
``ClientError``, a response without ``Parameter`` or ``Parameter.Value``, and
for the Usage_Secret a non-string value) to every reader, plus downstream
use of the value after the read: the Orchestrator's App-JWT mint and the
Runner's installation-token mint, each failing for a non-PEM value and, for
the Runner, on a GitHub 401. They also drive the
Spot_Retry_Handler with in-memory doubles and events whose Runner_Overrides
embed the generated value, and check its log records and raised exceptions.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import logging
import secrets
import string
import sys
import traceback
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import IO, Final, TypeVar

import boto3  # type: ignore[import-untyped]
import httpx
import jwt
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import event, given, settings
from hypothesis import strategies as st
from jwt.exceptions import InvalidKeyError
from moto import mock_aws

from trikon_cloud.fargate_runner import entrypoint
from trikon_cloud.fargate_runner import github_client as runner_github_client
from trikon_cloud.orchestrator import never_fail_open, spot_retry_handler
from trikon_cloud.orchestrator.models import SpotRetryEnvConfig
from trikon_cloud.orchestrator.never_fail_open import (
    CheckRunConclusion,
    OrchestratorGithubClient,
    OrchestratorGithubClientError,
)
from trikon_cloud.orchestrator.spot_retry import SpotEventError
from trikon_cloud.orchestrator.spot_retry_claims import CLAIM_STALE_MS, SpotRetryClaims
from trikon_cloud.orchestrator.spot_retry_handler import SpotRetryInFlightError
from trikon_cloud.usage_client.secret import UsageSecretError, UsageSecretReader
from trikon_cloud.webhook_receiver import handler as webhook_handler

_REGION = "us-east-1"
_WEBHOOK_PARAMETER = "/trikon-cloud/github-webhook-secret"
_APP_KEY_PARAMETER = "/trikon-cloud/github-app-private-key"
_USAGE_PARAMETER = "/trikon-cloud/usage-secret"
_APP_ID = 424242

# Loggers the readers' modules write through. Powertools loggers are named
# after their service; the rest live under the ``trikon_cloud`` package.
_TRIKON_LOGGERS = (
    "trikon_cloud",
    "trikon-cloud-webhook-receiver",
    "trikon-cloud-orchestrator",
)

SsmCalls = list[dict[str, object]]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ssm_calls(
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[SsmCalls]:
    """Activate moto with fake credentials and record every SSM ``GetParameter``.

    A fresh boto3 default session is installed (and dropped at teardown), and
    a ``provide-client-params`` hook on it appends each call's parameters to
    the yielded list. Clients copy the session's handlers when they are
    created, so every client built inside the test is counted.
    """
    del no_network  # dependency only
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)

    caplog.set_level(logging.INFO)
    for name in _TRIKON_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)

    calls: SsmCalls = []

    def _record(params: dict[str, object], **kwargs: object) -> None:
        del kwargs
        calls.append(dict(params))

    with mock_aws():
        boto3.setup_default_session(region_name=_REGION)
        boto3.DEFAULT_SESSION.events.register(
            "provide-client-params.ssm.GetParameter", _record
        )
        yield calls


@pytest.fixture
def webhook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the receiver at the test parameter and clear its module caches."""
    monkeypatch.setenv("TRIKON_WEBHOOK_SECRET_PARAMETER_NAME", _WEBHOOK_PARAMETER)
    monkeypatch.setenv(
        "TRIKON_VERIFY_JOBS_QUEUE_URL",
        f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-verify-jobs",
    )
    monkeypatch.setenv(
        "TRIKON_INSTALLATION_EVENTS_QUEUE_URL",
        f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-installation-events",
    )
    monkeypatch.setattr(webhook_handler, "_env_config", None)
    monkeypatch.setattr(webhook_handler, "_webhook_secret", None)
    monkeypatch.setattr(webhook_handler, "_ssm_client", None)


@pytest.fixture
def orchestrator_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[OrchestratorGithubClient]:
    """An :class:`OrchestratorGithubClient` with an empty App JWT cache.

    Must be requested after :func:`ssm_calls` so its SSM client is built
    from the counting session inside the moto context.
    """
    monkeypatch.setattr(never_fail_open, "_APP_JWT_CACHE", None)
    with httpx.Client() as http_client:
        yield OrchestratorGithubClient(
            app_id=_APP_ID,
            app_private_key_parameter_name=_APP_KEY_PARAMETER,
            ssm_client=boto3.client("ssm"),
            http_client=http_client,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _put_secure_string(name: str, value: str) -> None:
    boto3.client("ssm").put_parameter(Name=name, Value=value, Type="SecureString")


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _new_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _public_pem(private_pem: str) -> bytes:
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _fragments(value: str) -> list[str]:
    """The value, its stripped form and every long line (a PEM body line)."""
    found = {value, value.strip()}
    found.update(
        line
        for line in value.splitlines()
        if len(line) >= 16 and not line.startswith("-----")
    )
    return sorted(found)


def _exception_texts(exc: BaseException) -> list[str]:
    """Render ``exc`` and every exception reachable through its chain."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        texts.extend(
            [
                str(current),
                repr(current),
                repr(current.args),
                repr(vars(current)),
                "".join(traceback.format_exception(current)),
            ]
        )
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
    return texts


def _log_texts(caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> list[str]:
    captured = capfd.readouterr()
    texts = [caplog.text, captured.out, captured.err]
    for record in caplog.records:
        texts.extend([record.getMessage(), repr(record.args), repr(vars(record))])
    return texts


def _assert_no_leak(
    value: str,
    *,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    exc: BaseException | None = None,
) -> None:
    texts = _log_texts(caplog, capfd)
    if exc is not None:
        texts.extend(_exception_texts(exc))
    for fragment in _fragments(value):
        for text in texts:
            assert fragment not in text, "Secret_Value leaked into a log record or exception"


def _assert_get_parameter_calls(calls: SsmCalls, *, name: str, count: int) -> None:
    assert calls == [{"Name": name, "WithDecryption": True}] * count


def _assert_parameter_not_found(exc: BaseException) -> None:
    assert isinstance(exc, ClientError)
    assert exc.response["Error"]["Code"] == "ParameterNotFound"


def test_leak_check_detects_a_planted_value(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """The checker itself must catch a value in a chained exception."""
    value = _new_token()
    try:
        try:
            raise KeyError(value)
        except KeyError as inner:
            raise RuntimeError("fixed message") from inner
    except RuntimeError as outer:
        with pytest.raises(AssertionError, match="Secret_Value leaked"):
            _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=outer)


# ---------------------------------------------------------------------------
# Webhook secret (webhook_receiver/handler.py)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("webhook_env")
def test_webhook_secret_read_once_per_warm_container(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    value = _new_token()
    _put_secure_string(_WEBHOOK_PARAMETER, value)

    assert webhook_handler._get_webhook_secret() == value.encode("utf-8")
    assert webhook_handler._get_webhook_secret() == value.encode("utf-8")

    _assert_get_parameter_calls(ssm_calls, name=_WEBHOOK_PARAMETER, count=1)
    _assert_no_leak(value, caplog=caplog, capfd=capfd)


@pytest.mark.usefixtures("webhook_env")
def test_webhook_secret_parameter_not_found_propagates_and_is_not_cached(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    value = _new_token()
    _put_secure_string(f"{_WEBHOOK_PARAMETER}-other", value)

    for _ in range(2):
        with pytest.raises(ClientError) as excinfo:
            webhook_handler._get_webhook_secret()
        _assert_parameter_not_found(excinfo.value)
        _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=excinfo.value)

    assert webhook_handler._webhook_secret is None
    _assert_get_parameter_calls(ssm_calls, name=_WEBHOOK_PARAMETER, count=2)


# ---------------------------------------------------------------------------
# Runner App key (fargate_runner/entrypoint.py)
# ---------------------------------------------------------------------------


def test_runner_app_key_is_read_on_every_call(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pem = _new_pem()
    _put_secure_string(_APP_KEY_PARAMETER, pem)

    assert entrypoint._fetch_app_private_key(_APP_KEY_PARAMETER) == pem.encode("utf-8")
    assert entrypoint._fetch_app_private_key(_APP_KEY_PARAMETER) == pem.encode("utf-8")

    _assert_get_parameter_calls(ssm_calls, name=_APP_KEY_PARAMETER, count=2)
    _assert_no_leak(pem, caplog=caplog, capfd=capfd)


def test_runner_app_key_parameter_not_found_propagates(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pem = _new_pem()
    _put_secure_string(f"{_APP_KEY_PARAMETER}-other", pem)

    with pytest.raises(ClientError) as excinfo:
        entrypoint._fetch_app_private_key(_APP_KEY_PARAMETER)

    _assert_parameter_not_found(excinfo.value)
    _assert_get_parameter_calls(ssm_calls, name=_APP_KEY_PARAMETER, count=1)
    _assert_no_leak(pem, caplog=caplog, capfd=capfd, exc=excinfo.value)


# ---------------------------------------------------------------------------
# Orchestrator App key (orchestrator/never_fail_open.py)
# ---------------------------------------------------------------------------


def test_orchestrator_app_key_mints_a_jwt_cached_across_reads(
    ssm_calls: SsmCalls,
    orchestrator_client: OrchestratorGithubClient,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pem = _new_pem()
    _put_secure_string(_APP_KEY_PARAMETER, pem)

    first = orchestrator_client._get_app_jwt()
    second = orchestrator_client._get_app_jwt()

    assert second == first
    claims = jwt.decode(first, _public_pem(pem), algorithms=["RS256"])
    assert claims["iss"] == str(_APP_ID)
    # The JWT cache spares the second SSM read.
    _assert_get_parameter_calls(ssm_calls, name=_APP_KEY_PARAMETER, count=1)
    # The PEM is held by neither the client nor the module cache.
    assert all(pem not in repr(attr) for attr in vars(orchestrator_client).values())
    assert pem not in repr(never_fail_open._APP_JWT_CACHE)
    _assert_no_leak(pem, caplog=caplog, capfd=capfd)


def test_orchestrator_app_key_pem_itself_is_not_cached(
    ssm_calls: SsmCalls,
    orchestrator_client: OrchestratorGithubClient,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pem = _new_pem()
    _put_secure_string(_APP_KEY_PARAMETER, pem)

    assert orchestrator_client._get_app_private_key_pem() == pem.encode("utf-8")
    assert orchestrator_client._get_app_private_key_pem() == pem.encode("utf-8")

    _assert_get_parameter_calls(ssm_calls, name=_APP_KEY_PARAMETER, count=2)
    _assert_no_leak(pem, caplog=caplog, capfd=capfd)


def test_orchestrator_app_key_parameter_not_found_propagates(
    ssm_calls: SsmCalls,
    orchestrator_client: OrchestratorGithubClient,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pem = _new_pem()
    _put_secure_string(f"{_APP_KEY_PARAMETER}-other", pem)

    with pytest.raises(ClientError) as excinfo:
        orchestrator_client._get_app_jwt()

    _assert_parameter_not_found(excinfo.value)
    assert never_fail_open._APP_JWT_CACHE is None
    _assert_get_parameter_calls(ssm_calls, name=_APP_KEY_PARAMETER, count=1)
    _assert_no_leak(pem, caplog=caplog, capfd=capfd, exc=excinfo.value)


# ---------------------------------------------------------------------------
# Usage_Secret (usage_client/secret.py)
# ---------------------------------------------------------------------------


def test_usage_secret_read_once_per_reader(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    value = _new_token()
    _put_secure_string(_USAGE_PARAMETER, f" {value}\n")
    reader = UsageSecretReader(parameter_name=_USAGE_PARAMETER, ssm_client=boto3.client("ssm"))

    assert reader.secret_bytes() == value.encode("utf-8")
    assert reader.secret_bytes() == value.encode("utf-8")

    _assert_get_parameter_calls(ssm_calls, name=_USAGE_PARAMETER, count=1)
    assert value not in repr(reader)
    _assert_no_leak(value, caplog=caplog, capfd=capfd)


def test_usage_secret_parameter_not_found_propagates_and_is_not_cached(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    value = _new_token()
    _put_secure_string(f"{_USAGE_PARAMETER}-other", value)
    reader = UsageSecretReader(parameter_name=_USAGE_PARAMETER, ssm_client=boto3.client("ssm"))

    for _ in range(2):
        with pytest.raises(ClientError) as excinfo:
            reader.secret_bytes()
        _assert_parameter_not_found(excinfo.value)
        _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=excinfo.value)

    _assert_get_parameter_calls(ssm_calls, name=_USAGE_PARAMETER, count=2)


def test_blank_usage_secret_raises_a_fixed_message(
    ssm_calls: SsmCalls,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Whitespace is the whole value, so a substring check would be vacuous:
    # assert the exact fixed message and an empty chain instead.
    _put_secure_string(_USAGE_PARAMETER, " \t\u00a0\u2028\n")
    reader = UsageSecretReader(parameter_name=_USAGE_PARAMETER, ssm_client=boto3.client("ssm"))

    for _ in range(2):
        with pytest.raises(UsageSecretError) as excinfo:
            reader.secret_bytes()
        assert excinfo.value.args == ("Usage_Secret value is blank",)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

    # A failed read is not cached.
    _assert_get_parameter_calls(ssm_calls, name=_USAGE_PARAMETER, count=2)
    # The reader logs nothing (botocore's own credential notice is not the reader's).
    assert [r for r in caplog.records if r.name.startswith(_TRIKON_LOGGERS)] == []
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


# ===========================================================================
# Property 3: Parameter_Reader round trip and caching (counting SSM fake)
# ===========================================================================
#
# These properties run against an in-memory SSM fake that records every
# ``get_parameter`` call, not against moto. Each example patches the reader's
# SSM seam and resets its module-level caches inside its own
# ``pytest.MonkeyPatch.context()``, so no state crosses examples.

# JavaScript's ``String.prototype.trim`` set, built here from the ECMAScript
# definition (WhiteSpace: TAB, VT, FF, SP, NBSP, ZWNBSP and every Unicode
# ``Zs`` character; LineTerminator: LF, CR, LS, PS) rather than imported from
# the reader, so the oracle is independent of the implementation.
_JS_TRIM_SET = "".join(
    sorted(
        set("\t\v\f \u00a0\ufeff\n\r\u2028\u2029")
        | {
            chr(code)
            for code in range(sys.maxunicode + 1)
            if unicodedata.category(chr(code)) == "Zs"
        }
    )
)


def _js_trim(value: str) -> str:
    return value.strip(_JS_TRIM_SET)


_PADDING = st.text(alphabet=st.sampled_from(_JS_TRIM_SET), max_size=8)

# Up to 4096 characters, non-blank under JS trim, sometimes padded with
# trim characters on either side. Hypothesis text excludes lone surrogates,
# so every value encodes to UTF-8.
_SECRET_VALUES = (
    st.tuples(_PADDING, st.text(min_size=1, max_size=4080), _PADDING)
    .map("".join)
    .filter(lambda value: _js_trim(value) != "")
)

_PARAMETER_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/",
    min_size=1,
    max_size=64,
).map(lambda tail: f"/{tail}")

_READ_COUNTS = st.integers(min_value=1, max_value=20)


class _CountingSsm:
    """An SSM client fake that serves one SecureString and records each call."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.calls: SsmCalls = []

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803 -- AWS boto3 API contract
    ) -> dict[str, object]:
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        return {
            "Parameter": {"Name": Name, "Type": "SecureString", "Value": self._value},
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


@functools.cache
def _property_pem() -> str:
    """One RSA key shared by the mint-count property (key generation is slow).

    It stays at 2048 bits: PyJWT emits ``InsecureKeyLengthWarning`` for
    smaller RSA keys.
    """
    return _new_pem()


@functools.cache
def _property_public_pem() -> bytes:
    return _public_pem(_property_pem())


# Feature: trikon-cloud-cost-cuts, Property 3: Parameter_Reader round trip and caching
@settings(max_examples=100, deadline=None)
@given(value=_SECRET_VALUES, name=_PARAMETER_NAMES, reads=_READ_COUNTS)
def test_property_3_webhook_secret_round_trip_reads_once(
    value: str, name: str, reads: int
) -> None:
    """**Validates: Requirements 5.1, 5.2**

    The webhook secret comes back as ``v`` (UTF-8) on every read and SSM is
    called once per warm container, with the configured name and decryption.
    """
    fake = _CountingSsm(value)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TRIKON_WEBHOOK_SECRET_PARAMETER_NAME", name)
        mp.setenv(
            "TRIKON_VERIFY_JOBS_QUEUE_URL",
            f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-verify-jobs",
        )
        mp.setenv(
            "TRIKON_INSTALLATION_EVENTS_QUEUE_URL",
            f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-installation-events",
        )
        mp.setattr(webhook_handler, "_env_config", None)
        mp.setattr(webhook_handler, "_webhook_secret", None)
        mp.setattr(webhook_handler, "_ssm_client", fake)

        results = [webhook_handler._get_webhook_secret() for _ in range(reads)]

    assert results == [value.encode("utf-8")] * reads
    _assert_get_parameter_calls(fake.calls, name=name, count=1)


# Feature: trikon-cloud-cost-cuts, Property 3: Parameter_Reader round trip and caching
@settings(max_examples=100, deadline=None)
@given(value=_SECRET_VALUES, name=_PARAMETER_NAMES, reads=_READ_COUNTS)
def test_property_3_runner_app_key_round_trip_reads_every_call(
    value: str, name: str, reads: int
) -> None:
    """**Validates: Requirements 5.1, 5.2**

    The Runner reads the App key on every ``_fetch_app_private_key`` call and
    returns ``v`` (UTF-8) each time.
    """
    fake = _CountingSsm(value)
    created: list[str] = []

    def _client(service_name: str) -> _CountingSsm:
        created.append(service_name)
        return fake

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(entrypoint, "boto3", SimpleNamespace(client=_client))

        results = [entrypoint._fetch_app_private_key(name) for _ in range(reads)]

    assert results == [value.encode("utf-8")] * reads
    assert created == ["ssm"] * reads
    _assert_get_parameter_calls(fake.calls, name=name, count=reads)


def test_property_3_orchestrator_app_key_round_trip_reads_every_call() -> None:
    """**Validates: Requirements 5.1, 5.2**

    The Orchestrator keeps no PEM cache: each ``_get_app_private_key_pem``
    call makes one SSM call and returns ``v`` (UTF-8). One ``httpx.Client``
    (unused by the reader, and slow to build) is shared by every example.
    """
    with httpx.Client() as http_client:

        # Feature: trikon-cloud-cost-cuts, Property 3: Parameter_Reader round trip and caching
        @settings(max_examples=100, deadline=None)
        @given(value=_SECRET_VALUES, name=_PARAMETER_NAMES, reads=_READ_COUNTS)
        def _prop(value: str, name: str, reads: int) -> None:
            fake = _CountingSsm(value)
            client = OrchestratorGithubClient(
                app_id=_APP_ID,
                app_private_key_parameter_name=name,
                ssm_client=fake,
                http_client=http_client,
            )

            results = [client._get_app_private_key_pem() for _ in range(reads)]

            assert results == [value.encode("utf-8")] * reads
            _assert_get_parameter_calls(fake.calls, name=name, count=reads)

        _prop()


def test_property_3_orchestrator_app_key_read_once_per_jwt_mint() -> None:
    """**Validates: Requirements 5.1, 5.2**

    Across ``k`` App-JWT requests, SSM is called once per mint: once for the
    first request and once more after each cache expiry. ``expire_before[i]``
    pushes the cached JWT inside its 1-minute safety margin before request
    ``i``, which forces a re-mint. The module JWT cache is reset per example.
    """
    pem = _property_pem()
    public_pem = _property_public_pem()
    with httpx.Client() as http_client:

        # Feature: trikon-cloud-cost-cuts, Property 3: Parameter_Reader round trip and caching
        @settings(max_examples=100, deadline=None)
        @given(
            name=_PARAMETER_NAMES,
            expire_before=st.lists(st.booleans(), min_size=1, max_size=20),
        )
        def _prop(name: str, expire_before: list[bool]) -> None:
            fake = _CountingSsm(pem)
            client = OrchestratorGithubClient(
                app_id=_APP_ID,
                app_private_key_parameter_name=name,
                ssm_client=fake,
                http_client=http_client,
            )
            mints = 0
            previous: str | None = None
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(never_fail_open, "_APP_JWT_CACHE", None)
                for expire in expire_before:
                    cached = never_fail_open._APP_JWT_CACHE
                    if expire and cached is not None:
                        never_fail_open._APP_JWT_CACHE = (cached[0], datetime.now(UTC))
                    mint_expected = cached is None or expire
                    token = client._get_app_jwt()
                    if mint_expected:
                        mints += 1
                    else:
                        assert token == previous
                    claims = jwt.decode(token, public_pem, algorithms=["RS256"])
                    assert claims["iss"] == str(_APP_ID)
                    previous = token

            assert mints == 1 + sum(expire_before[1:])
            _assert_get_parameter_calls(fake.calls, name=name, count=mints)

        _prop()


# Feature: trikon-cloud-cost-cuts, Property 3: Parameter_Reader round trip and caching
@settings(max_examples=100, deadline=None)
@given(value=_SECRET_VALUES, name=_PARAMETER_NAMES, reads=_READ_COUNTS)
def test_property_3_usage_secret_round_trip_reads_once(
    value: str, name: str, reads: int
) -> None:
    """**Validates: Requirements 5.1, 5.2**

    ``UsageSecretReader`` returns the JS-trimmed ``v`` as UTF-8 on every read
    and calls SSM once per reader.
    """
    fake = _CountingSsm(value)
    reader = UsageSecretReader(parameter_name=name, ssm_client=fake)

    results = [reader.secret_bytes() for _ in range(reads)]

    assert results == [_js_trim(value).encode("utf-8")] * reads
    _assert_get_parameter_calls(fake.calls, name=name, count=1)


# ===========================================================================
# Property 4: Secret values never leak
# ===========================================================================
#
# Generated Secret_Values carry a fixed prefix that no log text or library
# message contains, followed by at least 16 random ASCII alphanumerics, so a
# match can only come from the value itself. ASCII keeps the check honest for
# JSON log lines (no ``\uXXXX`` escaping hides a match). Some values carry
# whitespace padding, so ``_fragments`` also checks the stripped form.
#
# Each example resets its module caches inside ``pytest.MonkeyPatch.context()``
# and clears the captured logs and file descriptors before it runs; the
# function-scoped fixtures are requested by the outer test, as in Property 3.

_P4_PREFIX: Final = "TRIKONP4SECRET"
_P4_BLANK: Final = " \t\n"
_P4_ERROR_CODES: Final = (
    "ParameterNotFound",
    "AccessDeniedException",
    "ThrottlingException",
    "InternalServerError",
)

_P4_TOKENS = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=16, max_size=48
).map(lambda token: f"{_P4_PREFIX}{token}")
_P4_SECRETS = st.tuples(
    st.sampled_from(("", " ", "\n", "\t ")),
    _P4_TOKENS,
    st.sampled_from(("", "\n", " \r\n")),
).map("".join)

# (kind, ClientError code). ``non_string_value`` is the Usage_Secret's own
# "no string value" branch; SSM itself always returns a string.
_P4Outcome = tuple[str, str]
_P4_FIXED_OUTCOMES: Final = (
    ("success", ""),
    ("blank", ""),
    ("missing_value", ""),
    ("missing_parameter", ""),
)
_P4_ERROR_OUTCOMES = st.tuples(st.just("client_error"), st.sampled_from(_P4_ERROR_CODES))
_P4_OUTCOMES = st.one_of(st.sampled_from(_P4_FIXED_OUTCOMES), _P4_ERROR_OUTCOMES)
_P4_USAGE_OUTCOMES = st.one_of(
    st.sampled_from((*_P4_FIXED_OUTCOMES, ("non_string_value", ""))), _P4_ERROR_OUTCOMES
)
_P4_OUTCOME_LISTS = st.lists(_P4_OUTCOMES, min_size=1, max_size=6)

_T = TypeVar("_T")


class _ScriptedSsm:
    """An SSM client fake whose next ``get_parameter`` serves ``outcome``."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.outcome: _P4Outcome = ("success", "")

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803 -- AWS boto3 API contract
    ) -> dict[str, object]:
        assert WithDecryption
        kind, code = self.outcome
        metadata = {"HTTPStatusCode": 200}
        if kind == "client_error":
            raise ClientError(
                {"Error": {"Code": code, "Message": f"Parameter {Name} not found."}},
                "GetParameter",
            )
        if kind == "missing_parameter":
            return {"ResponseMetadata": metadata}
        parameter: dict[str, object] = {"Name": Name, "Type": "SecureString", "Version": 1}
        if kind == "success":
            parameter["Value"] = self._value
        elif kind == "blank":
            parameter["Value"] = _P4_BLANK
        elif kind == "non_string_value":
            parameter["Value"] = [self._value]
        else:
            assert kind == "missing_value"
        return {"Parameter": parameter, "ResponseMetadata": metadata}


def _p4_call(read: Callable[[], _T]) -> tuple[_T | None, BaseException | None]:
    """Run one read; return its result or the exception a reader may raise."""
    try:
        return read(), None
    except (
        ClientError,
        KeyError,
        UsageSecretError,
        InvalidKeyError,
        runner_github_client.GithubClientError,
    ) as exc:
        return None, exc


def _p4_check_raw_read(
    outcome: _P4Outcome, value: str, result: bytes | None, exc: BaseException | None
) -> None:
    """The outcome a reader returning ``Parameter.Value`` as UTF-8 must give."""
    kind, code = outcome
    if kind == "client_error":
        assert isinstance(exc, ClientError)
        assert exc.response["Error"]["Code"] == code
    elif kind in ("missing_value", "missing_parameter"):
        assert isinstance(exc, KeyError)
    elif kind == "success":
        assert (result, exc) == (value.encode("utf-8"), None)
    else:
        assert (kind, result, exc) == ("blank", _P4_BLANK.encode("utf-8"), None)


def _p4_reset_capture(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.clear()
    capfd.readouterr()


def _p4_webhook_env(mp: pytest.MonkeyPatch, fake: _ScriptedSsm) -> None:
    mp.setenv("TRIKON_WEBHOOK_SECRET_PARAMETER_NAME", _WEBHOOK_PARAMETER)
    mp.setenv(
        "TRIKON_VERIFY_JOBS_QUEUE_URL",
        f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-verify-jobs",
    )
    mp.setenv(
        "TRIKON_INSTALLATION_EVENTS_QUEUE_URL",
        f"https://sqs.{_REGION}.amazonaws.com/123456789012/trikon-installation-events",
    )
    mp.setattr(webhook_handler, "_env_config", None)
    mp.setattr(webhook_handler, "_webhook_secret", None)
    mp.setattr(webhook_handler, "_ssm_client", fake)


@pytest.fixture
def p4_capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    for name in _TRIKON_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)


@pytest.mark.usefixtures("no_network", "p4_capture")
def test_property_4_webhook_secret_never_leaks(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """**Validates: Requirements 5.3, 5.4, 5.6**

    Across any sequence of read outcomes, the webhook secret reader keeps
    its contract (errors propagate uncached, the first value is cached) and
    no log record or exception chain contains the value.
    """

    # Feature: trikon-cloud-cost-cuts, Property 4: Secret values never leak
    @settings(max_examples=100, deadline=None)
    @given(value=_P4_SECRETS, outcomes=_P4_OUTCOME_LISTS)
    def _prop(value: str, outcomes: list[_P4Outcome]) -> None:
        _p4_reset_capture(caplog, capfd)
        fake = _ScriptedSsm(value)
        cached: bytes | None = None
        with pytest.MonkeyPatch.context() as mp:
            _p4_webhook_env(mp, fake)
            for outcome in outcomes:
                fake.outcome = outcome
                result, exc = _p4_call(webhook_handler._get_webhook_secret)
                if cached is not None:
                    assert (result, exc) == (cached, None)
                else:
                    _p4_check_raw_read(outcome, value, result, exc)
                    cached = result
                _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=exc)

    _prop()


def _p4_token_transport(status: int) -> httpx.MockTransport:
    """GitHub's installation-token endpoint, answering ``status`` in memory."""

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/access_tokens")
        if status == 201:
            return httpx.Response(
                201, json={"token": "ghs_p4installation", "expires_at": "2024-05-01T11:00:00Z"}
            )
        return httpx.Response(status, json={"message": "Bad credentials"})

    return httpx.MockTransport(_respond)


@pytest.mark.usefixtures("no_network", "p4_capture")
def test_property_4_runner_app_key_never_leaks(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """**Validates: Requirements 5.3, 5.4, 5.6**

    The Runner reads on every call, so each outcome shows through. A read
    that returns a value is then used downstream, as ``entrypoint`` does, to
    mint an installation token: any non-PEM value fails in ``jwt.encode``
    with ``InvalidKeyError``, and the real PEM gets either a token or a
    non-retryable 401 (``GithubClientError``). No log record or exception
    chain contains the value. The two GitHub clients are shared by every
    example.
    """
    with (
        httpx.Client(transport=_p4_token_transport(201)) as ok_http,
        httpx.Client(transport=_p4_token_transport(401)) as denied_http,
    ):
        github = {
            True: runner_github_client.GithubClient(http_client=ok_http),
            False: runner_github_client.GithubClient(http_client=denied_http),
        }

        # Feature: trikon-cloud-cost-cuts, Property 4: Secret values never leak
        @settings(max_examples=100, deadline=None)
        @given(
            value=st.one_of(_P4_SECRETS, st.builds(_property_pem)),
            steps=st.lists(st.tuples(_P4_OUTCOMES, st.booleans()), min_size=1, max_size=6),
        )
        def _prop(value: str, steps: list[tuple[_P4Outcome, bool]]) -> None:
            _p4_reset_capture(caplog, capfd)
            fake = _ScriptedSsm(value)
            is_pem = value == _property_pem()
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(entrypoint, "boto3", SimpleNamespace(client=lambda _name: fake))
                for outcome, github_accepts in steps:
                    fake.outcome = outcome
                    result, exc = _p4_call(
                        lambda: entrypoint._fetch_app_private_key(_APP_KEY_PARAMETER)
                    )
                    _p4_check_raw_read(outcome, value, result, exc)
                    _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=exc)
                    if result is None:
                        continue
                    minted, exc = _p4_call(
                        functools.partial(
                            github[github_accepts].mint_installation_token,
                            installation_id=int(_P4_INSTALLATION_ID),
                            app_id=_APP_ID,
                            private_key_pem=result,
                        )
                    )
                    if not (outcome[0] == "success" and is_pem):
                        assert isinstance(exc, InvalidKeyError)
                    elif github_accepts:
                        assert exc is None and minted is not None
                    else:
                        assert isinstance(exc, runner_github_client.GithubClientError)
                    _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=exc)

        _prop()


@pytest.mark.usefixtures("no_network", "p4_capture")
def test_property_4_orchestrator_app_key_never_leaks(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """**Validates: Requirements 5.3, 5.4, 5.6**

    Steps alternate between the raw PEM read and an App-JWT request. A JWT
    request with no cached JWT reads the key and mints: a real PEM gives a
    JWT (then cached), any other value fails downstream in ``jwt.encode``
    with ``InvalidKeyError``. Neither the logs, nor any exception chain, nor
    the client or the module JWT cache contains the value.
    """
    with httpx.Client() as http_client:

        # Feature: trikon-cloud-cost-cuts, Property 4: Secret values never leak
        @settings(max_examples=100, deadline=None)
        @given(
            value=st.one_of(_P4_SECRETS, st.builds(_property_pem)),
            steps=st.lists(st.tuples(_P4_OUTCOMES, st.booleans()), min_size=1, max_size=6),
        )
        def _prop(value: str, steps: list[tuple[_P4Outcome, bool]]) -> None:
            _p4_reset_capture(caplog, capfd)
            fake = _ScriptedSsm(value)
            client = OrchestratorGithubClient(
                app_id=_APP_ID,
                app_private_key_parameter_name=_APP_KEY_PARAMETER,
                ssm_client=fake,
                http_client=http_client,
            )
            is_pem = value == _property_pem()
            cached_jwt: str | None = None
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(never_fail_open, "_APP_JWT_CACHE", None)
                for outcome, via_jwt in steps:
                    fake.outcome = outcome
                    exc: BaseException | None
                    if not via_jwt:
                        pem, exc = _p4_call(client._get_app_private_key_pem)
                        _p4_check_raw_read(outcome, value, pem, exc)
                    else:
                        token, exc = _p4_call(client._get_app_jwt)
                        kind = outcome[0]
                        if cached_jwt is not None:
                            assert (token, exc) == (cached_jwt, None)
                        elif kind in ("client_error", "missing_value", "missing_parameter"):
                            _p4_check_raw_read(outcome, value, None, exc)
                        elif kind == "success" and is_pem:
                            assert exc is None and isinstance(token, str)
                            cached_jwt = token
                        else:
                            assert isinstance(exc, InvalidKeyError)
                    _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=exc)
                    for fragment in _fragments(value):
                        assert fragment not in repr(vars(client))
                        assert fragment not in repr(never_fail_open._APP_JWT_CACHE)

    _prop()


@pytest.mark.usefixtures("no_network", "p4_capture")
def test_property_4_usage_secret_never_leaks(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """**Validates: Requirements 5.3, 5.4, 5.6**

    ``UsageSecretReader`` raises its fixed messages for a blank, missing or
    non-string value, propagates ``ClientError`` and caches only a success.
    Nothing it logs, raises or shows in ``repr`` contains the value.
    """

    # Feature: trikon-cloud-cost-cuts, Property 4: Secret values never leak
    @settings(max_examples=100, deadline=None)
    @given(value=_P4_SECRETS, outcomes=st.lists(_P4_USAGE_OUTCOMES, min_size=1, max_size=6))
    def _prop(value: str, outcomes: list[_P4Outcome]) -> None:
        _p4_reset_capture(caplog, capfd)
        fake = _ScriptedSsm(value)
        reader = UsageSecretReader(parameter_name=_USAGE_PARAMETER, ssm_client=fake)
        cached: bytes | None = None
        for outcome in outcomes:
            fake.outcome = outcome
            result, exc = _p4_call(reader.secret_bytes)
            kind, code = outcome
            if cached is not None:
                assert (result, exc) == (cached, None)
            elif kind == "client_error":
                assert isinstance(exc, ClientError)
                assert exc.response["Error"]["Code"] == code
            elif kind == "success":
                assert (result, exc) == (_js_trim(value).encode("utf-8"), None)
                cached = result
            elif kind == "blank":
                assert isinstance(exc, UsageSecretError)
                assert exc.args == ("Usage_Secret value is blank",)
            else:
                assert isinstance(exc, UsageSecretError)
                assert exc.args == ("Usage_Secret has no string value",)
            _assert_no_leak(value, caplog=caplog, capfd=capfd, exc=exc)
            for fragment in _fragments(value):
                assert fragment not in repr(reader)

    _prop()


# ---------------------------------------------------------------------------
# Property 4, Spot_Retry_Handler half (Req 9.8)
# ---------------------------------------------------------------------------
#
# The handler logs ``task_arn`` and ``run_id`` (TRIKON_DELIVERY_ID) by
# design, so the generated value is embedded in every other place a
# Runner_Override can carry it: any other TRIKON_* field (replaced, or
# appended to or prepended onto the canonical value), extra TRIKON_* entries,
# a sidecar container override, an entry with the value as its name, and
# malformed entries (no name, or a non-string value) that make the event
# unparseable. The doubles below are local to this file.

_P4_TASK_ARN: Final = "arn:aws:ecs:us-east-1:000000000000:task/trikon-verify-cluster/0123abcd"
_P4_CLUSTER_ARN: Final = "arn:aws:ecs:us-east-1:000000000000:cluster/trikon-verify-cluster"
_P4_RUN_ID: Final = "e6e7a4d0-2b3c-4c5d-b6a7-1234567890ab"
_P4_INSTALLATION_ID: Final = "12345678"
_P4_REPO: Final = "octocat/hello-world"
_P4_PR_NUMBER: Final = "42"
_P4_HEAD_SHA: Final = "6aabf09b1abcdef0123456789abcdef012345678"
_P4_NOW_MS: Final = 1_714_557_751_000
_P4_ENV = SpotRetryEnvConfig.model_validate(
    {
        "TRIKON_APP_ID": 999999,
        "TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME": _APP_KEY_PARAMETER,
        "TRIKON_VERIFY_JOBS_QUEUE_URL": (
            "https://sqs.us-east-1.amazonaws.com/000000000000/trikon-verify-jobs"
        ),
    }
)
_P4_SPOT_LOGGER: Final = "trikon-cloud-spot-retry"
_P4_ALLOWED_LOG_KEYS: Final = frozenset(
    {"task_arn", "run_id", "reason", "aws_error_code"}
    | {"level", "location", "message", "timestamp", "service", "xray_trace_id"}
)
_P4_EVENT_NAMES: Final = frozenset(
    {
        "spot_event_unparseable",
        "spot_event_ignored",
        "spot_event_unroutable",
        "spot_retry_claim_failed",
        "spot_retry_duplicate",
        "spot_retry_in_flight",
        "spot_retry_send_failed",
        "spot_retry_requeued",
        "spot_retry_claim_update_failed",
        "spot_fail_closed_publish_failed",
        "spot_fail_closed_published",
    }
)
_P4_OVERRIDE_FIELDS: Final = (
    "TRIKON_INSTALLATION_ID",
    "TRIKON_REPO_FULL_NAME",
    "TRIKON_PR_NUMBER",
    "TRIKON_HEAD_SHA",
    "TRIKON_BASE_SHA",
    "TRIKON_EVENT_TYPE",
    "TRIKON_RESERVATION_ID",
    "TRIKON_ATTEMPT",
    "TRIKON_CAPACITY",
)
_P4_EXTRA_FIELDS: Final = (
    "TRIKON_APP_PRIVATE_KEY",
    "TRIKON_WEBHOOK_SECRET",
    "TRIKON_CLOUD_USAGE_SECRET",
)

# (placement kind, field name or "").
_P4Placement = tuple[str, str]
_P4_OVERRIDE_PLACEMENTS = st.tuples(
    st.sampled_from(("replace", "append", "prepend")), st.sampled_from(_P4_OVERRIDE_FIELDS)
)
# Weighted so that most events still parse and reach the claim, send and
# publish paths; the malformed placements make the whole event unparseable.
_P4_PLACEMENTS = st.one_of(
    _P4_OVERRIDE_PLACEMENTS,
    _P4_OVERRIDE_PLACEMENTS,
    st.tuples(st.just("extra"), st.sampled_from(_P4_EXTRA_FIELDS)),
    st.tuples(st.sampled_from(("sidecar", "as_name")), st.just("")),
    st.tuples(st.sampled_from(("nameless", "non_string")), st.just("")),
)
_P4_CLAIM_STATES: Final = ("none", "none", "none", "in_flight", "stale", "sent", "failed_closed")
_P4_RARE_FAILURE = st.sampled_from((False, False, False, True))


def _p4_client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "stubbed"}}, operation)


class _P4Ddb:
    """Claims, verdict rows and ``trikon_pr_state``, with injectable failures."""

    def __init__(
        self,
        *,
        claim_state: str,
        pr_state_row: bool,
        fail_claim_put: bool,
        fail_verdict_put: bool,
        fail_mark: bool,
    ) -> None:
        self.claims: dict[str, dict[str, Mapping[str, object]]] = {}
        if claim_state != "none":
            status, claimed_at = {
                "in_flight": ("claimed", _P4_NOW_MS - 1_000),
                "stale": ("claimed", _P4_NOW_MS - CLAIM_STALE_MS),
                "sent": ("sent", _P4_NOW_MS - 1_000),
                "failed_closed": ("failed_closed", _P4_NOW_MS - 1_000),
            }[claim_state]
            self.claims[_P4_TASK_ARN] = {
                "task_arn": {"S": _P4_TASK_ARN},
                "status": {"S": status},
                "claimed_at_ms": {"N": str(claimed_at)},
            }
        self.pr_state: dict[str, dict[str, Mapping[str, object]]] = {}
        if pr_state_row:
            pr_key = f"{_P4_INSTALLATION_ID}#{_P4_REPO}#{_P4_PR_NUMBER}"
            self.pr_state[pr_key] = {
                "pr_key": {"S": pr_key},
                "last_head_sha": {"S": _P4_HEAD_SHA},
                "last_check_run_id": {"N": "777"},
            }
        self._fail_claim_put = fail_claim_put
        self._fail_verdict_put = fail_verdict_put
        self._fail_mark = fail_mark

    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803 -- AWS boto3 API contract
        Item: Mapping[str, Mapping[str, object]],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> object:
        if TableName == _P4_ENV.trikon_spot_retries_table:
            if self._fail_claim_put:
                raise _p4_client_error("ProvisionedThroughputExceededException", "PutItem")
            key = str(Item["task_arn"]["S"])
            if key in self.claims:
                raise _p4_client_error("ConditionalCheckFailedException", "PutItem")
            self.claims[key] = dict(Item)
            return {}
        assert TableName == _P4_ENV.trikon_verdicts_table
        if self._fail_verdict_put:
            raise _p4_client_error("InternalServerError", "PutItem")
        return {}

    def get_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        ConsistentRead: bool,  # noqa: N803
    ) -> dict[str, object]:
        assert ConsistentRead
        if TableName == _P4_ENV.trikon_spot_retries_table:
            item = self.claims.get(Key["task_arn"]["S"])
        else:
            assert TableName == _P4_ENV.trikon_pr_state_table
            item = self.pr_state.get(Key["pr_key"]["S"])
        return {} if item is None else {"Item": item}

    def update_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Key: Mapping[str, Mapping[str, str]],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ExpressionAttributeNames: Mapping[str, str],  # noqa: N803
        ExpressionAttributeValues: Mapping[str, Mapping[str, str]],  # noqa: N803
    ) -> object:
        if self._fail_mark:
            raise _p4_client_error("ProvisionedThroughputExceededException", "UpdateItem")
        assert TableName == _P4_ENV.trikon_spot_retries_table
        self.claims[Key["task_arn"]["S"]]["status"] = ExpressionAttributeValues[":s"]
        return {}


class _P4Sqs:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail
        self.sent: list[str] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        if self._fail:
            raise _p4_client_error("AccessDenied", "SendMessage")
        self.sent.append(MessageBody)
        return {"MessageId": "m-1"}


class _P4Github(OrchestratorGithubClient):
    """Check Run calls that succeed silently or raise a fixed error."""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    def create_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        head_sha: str,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        if self._fail:
            raise OrchestratorGithubClientError("github request returned 502 after 4 attempts")

    def update_check_run(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        check_run_id: int,
        title: str,
        summary: str,
        conclusion: CheckRunConclusion,
        details_url: str,
    ) -> None:
        if self._fail:
            raise OrchestratorGithubClientError("github request returned 502 after 4 attempts")


def _p4_event(
    secret: str,
    placements: Sequence[_P4Placement],
    *,
    attempt: str | None,
    stop_code: str,
    exit_code: int | None,
    drop_run_id: bool,
) -> dict[str, object]:
    """A Task_Stopped_Event whose Runner_Overrides embed ``secret``."""
    env: dict[str, str] = {
        "TRIKON_INSTALLATION_ID": _P4_INSTALLATION_ID,
        "TRIKON_REPO_FULL_NAME": _P4_REPO,
        "TRIKON_PR_NUMBER": _P4_PR_NUMBER,
        "TRIKON_HEAD_SHA": _P4_HEAD_SHA,
        "TRIKON_BASE_SHA": "87f7a31b1abcdef0123456789abcdef012345678",
        "TRIKON_EVENT_TYPE": "pull_request.opened",
        "TRIKON_DELIVERY_ID": _P4_RUN_ID,
        "TRIKON_RESERVATION_ID": "3f1c2a9e-7b4d-4e8a-9c21-5d6e7f8a9b0c",
        "TRIKON_CAPACITY": "FARGATE_SPOT",
    }
    if attempt is not None:
        env["TRIKON_ATTEMPT"] = attempt
    if drop_run_id:
        del env["TRIKON_DELIVERY_ID"]
    extra: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    for kind, field in placements:
        if kind == "replace":
            env[field] = secret
        elif kind == "append":
            env[field] = env.get(field, "") + secret
        elif kind == "prepend":
            env[field] = secret + env.get(field, "")
        elif kind == "extra":
            extra.append({"name": field, "value": secret})
        elif kind == "sidecar":
            sidecars.append(
                {"name": "sidecar", "environment": [{"name": "TRIKON_HEAD_SHA", "value": secret}]}
            )
        elif kind == "as_name":
            extra.append({"name": secret, "value": "1"})
        elif kind == "nameless":
            extra.append({"value": secret})
        else:
            assert kind == "non_string"
            extra.append({"name": "TRIKON_BASE_SHA", "value": {"secret": secret}})
    environment: list[dict[str, object]] = [
        {"name": name, "value": value} for name, value in env.items()
    ]
    container: dict[str, object] = {"name": "runner", "lastStatus": "STOPPED"}
    if exit_code is not None:
        container["exitCode"] = exit_code
    return {
        "version": "0",
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "time": "2024-05-01T10:02:31Z",
        "detail": {
            "taskArn": _P4_TASK_ARN,
            "clusterArn": _P4_CLUSTER_ARN,
            "group": "family:trikon-verify-runner",
            "lastStatus": "STOPPED",
            "stopCode": stop_code,
            "stoppedAt": "2024-05-01T10:02:30.123456Z",
            "capacityProviderName": "FARGATE_SPOT",
            "containers": [container],
            "overrides": {
                "containerOverrides": [
                    {"name": "runner", "environment": environment + extra},
                    *sidecars,
                ]
            },
        },
    }


@contextlib.contextmanager
def _p4_spot_log_stream() -> Iterator[io.StringIO]:
    """Redirect the Spot_Retry_Handler's Powertools stream handlers to a buffer."""
    buf = io.StringIO()
    stdlib_logger = logging.getLogger(_P4_SPOT_LOGGER)
    original_level = stdlib_logger.level
    swapped: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for handler in stdlib_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            swapped.append((handler, handler.stream))
            handler.setStream(buf)
    assert swapped, "the Spot_Retry_Handler logger has no stream handler to capture"
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for handler, stream in swapped:
            handler.setStream(stream)
        stdlib_logger.setLevel(original_level)


@pytest.mark.usefixtures("no_network", "p4_capture")
def test_property_4_spot_retry_handler_never_logs_override_values(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """**Validates: Requirements 9.8**

    For any Task_Stopped_Event whose Runner_Overrides embed the value, on
    every handler path (ignored, unparseable, unroutable, claim failure,
    duplicate, in flight, re-queued, send failure, stale claim, published,
    publish failure, best-effort mark failure), every Spot_Retry_Handler log
    record has a fixed event name and only the ``task_arn``, ``run_id``,
    ``reason`` and ``aws_error_code`` keys, and no log record or raised
    exception chain contains the value.
    """

    # Feature: trikon-cloud-cost-cuts, Property 4: Secret values never leak
    @settings(max_examples=100, deadline=None)
    @given(
        secret=_P4_TOKENS,
        placements=st.lists(_P4_PLACEMENTS, min_size=1, max_size=2),
        attempt=st.sampled_from(("1", "1", "2", None)),
        stop_code=st.sampled_from(
            ("SpotInterruption", "SpotInterruption", "EssentialContainerExited", "TaskFailedToStart")
        ),
        exit_code=st.sampled_from((0, 1, 75, 75, None)),
        drop_run_id=_P4_RARE_FAILURE,
        claim_state=st.sampled_from(_P4_CLAIM_STATES),
        pr_state_row=st.booleans(),
        failures=st.lists(_P4_RARE_FAILURE, min_size=5, max_size=5),
    )
    def _prop(
        secret: str,
        placements: list[_P4Placement],
        attempt: str | None,
        stop_code: str,
        exit_code: int | None,
        drop_run_id: bool,
        claim_state: str,
        pr_state_row: bool,
        failures: list[bool],
    ) -> None:
        _p4_reset_capture(caplog, capfd)
        fail_claim_put, fail_send, fail_verdict_put, fail_github, fail_mark = failures
        stopped_event = _p4_event(
            secret,
            placements,
            attempt=attempt,
            stop_code=stop_code,
            exit_code=exit_code,
            drop_run_id=drop_run_id,
        )
        assert secret in json.dumps(stopped_event)  # the value is really in the overrides
        ddb = _P4Ddb(
            claim_state=claim_state,
            pr_state_row=pr_state_row,
            fail_claim_put=fail_claim_put,
            fail_verdict_put=fail_verdict_put,
            fail_mark=fail_mark,
        )
        exc: BaseException | None = None
        with pytest.MonkeyPatch.context() as mp, _p4_spot_log_stream() as stream:
            mp.setattr(spot_retry_handler, "_ENV", _P4_ENV)
            mp.setattr(spot_retry_handler, "_BOTO_SESSION", object())
            mp.setattr(
                spot_retry_handler,
                "_CLAIMS",
                SpotRetryClaims(table_name=_P4_ENV.trikon_spot_retries_table, ddb_client=ddb),
            )
            mp.setattr(spot_retry_handler, "_DDB_CLIENT", ddb)
            mp.setattr(spot_retry_handler, "_SQS_CLIENT", _P4Sqs(fail=fail_send))
            mp.setattr(spot_retry_handler, "_GITHUB_CLIENT", _P4Github(fail=fail_github))
            mp.setattr(spot_retry_handler, "_NOW_MS", lambda: _P4_NOW_MS)
            try:
                spot_retry_handler.lambda_handler(stopped_event, LambdaContext())
            except (
                SpotEventError,
                SpotRetryInFlightError,
                ClientError,
                OrchestratorGithubClientError,
            ) as caught:
                exc = caught

        raw = stream.getvalue()
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        assert records, "every handler path logs at least one record"
        event(f"last record: {records[-1]['message']}")
        for record in records:
            assert set(record) <= _P4_ALLOWED_LOG_KEYS, sorted(record)
            assert record["message"] in _P4_EVENT_NAMES
            assert record.get("task_arn", _P4_TASK_ARN) == _P4_TASK_ARN
            assert record.get("run_id") in (_P4_RUN_ID, None)
            assert secret not in json.dumps(record)
        assert secret not in raw
        _assert_no_leak(secret, caplog=caplog, capfd=capfd, exc=exc)

    _prop()
