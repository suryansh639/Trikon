"""Unit tests for :mod:`trikon_cloud.usage_client.secret` (Req 15.5; cost-cuts Req 5).

Every test runs against moto's SSM Parameter Store (``@mock_aws()``) with fake
credentials, so no real AWS call is made. The Usage_Secret is a
``SecureString`` parameter read with ``WithDecryption=True``. The values are
deliberately not secret-shaped.
"""

from __future__ import annotations

from collections.abc import Mapping

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.usage_client.secret import UsageSecretError, UsageSecretReader

_REGION = "us-east-1"
_PARAMETER_NAME = "/trikon/test/usage-secret"
_VALUE = "usage-test-value-1"
_OTHER_VALUE = "usage-test-value-2"


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate testing AWS credentials so ``@mock_aws()`` activates cleanly."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)


class _CountingClient:
    """Wraps a boto3 SSM client and records every ``get_parameter`` call."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[tuple[str, bool]] = []

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803
    ) -> Mapping[str, object]:
        self.calls.append((Name, WithDecryption))
        response: Mapping[str, object] = self._inner.get_parameter(  # type: ignore[attr-defined]
            Name=Name, WithDecryption=WithDecryption
        )
        return response


class _FixedResponseClient:
    """Returns one canned ``GetParameter`` response (shapes moto cannot produce)."""

    def __init__(self, response: Mapping[str, object]) -> None:
        self._response = response

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803
    ) -> Mapping[str, object]:
        del Name, WithDecryption
        return self._response


def _client() -> object:
    return boto3.client("ssm", region_name=_REGION)


def _put_secure_string(value: str, *, overwrite: bool = False) -> None:
    boto3.client("ssm", region_name=_REGION).put_parameter(
        Name=_PARAMETER_NAME, Value=value, Type="SecureString", Overwrite=overwrite
    )


def _reader(client: object | None = None) -> UsageSecretReader:
    return UsageSecretReader(
        parameter_name=_PARAMETER_NAME,
        ssm_client=client if client is not None else _client(),  # type: ignore[arg-type]
    )


@mock_aws()
def test_reads_secret_once_across_calls_with_decryption() -> None:
    _put_secure_string(_VALUE)
    counting = _CountingClient(_client())
    reader = UsageSecretReader(parameter_name=_PARAMETER_NAME, ssm_client=counting)

    assert counting.calls == []  # lazy: the constructor makes no call
    assert reader.secret_bytes() == _VALUE.encode("utf-8")
    assert reader.secret_bytes() == _VALUE.encode("utf-8")
    assert counting.calls == [(_PARAMETER_NAME, True)]


@mock_aws()
def test_value_is_cached_in_memory_after_first_read() -> None:
    _put_secure_string(_VALUE)
    reader = _reader()

    assert reader.secret_bytes() == _VALUE.encode("utf-8")
    _put_secure_string(_OTHER_VALUE, overwrite=True)

    assert reader.secret_bytes() == _VALUE.encode("utf-8")
    # A fresh reader sees the rotated value, so the cache is per instance.
    assert _reader().secret_bytes() == _OTHER_VALUE.encode("utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"  {_VALUE}\n", _VALUE),
        (f"\t\r\n{_VALUE}\v\f", _VALUE),
        # JS trim strips ZWNBSP, NBSP and Zs spaces; Python's str.strip keeps U+FEFF.
        (f"\ufeff{_VALUE}\ufeff", _VALUE),
        (f"\u00a0\u2003{_VALUE}\u3000\u2028", _VALUE),
        # JS trim keeps U+0085 and U+001C-U+001F; Python's str.strip removes them.
        (f"\u0085{_VALUE}\u0085", f"\u0085{_VALUE}\u0085"),
        (f"\u001c{_VALUE}\u001f", f"\u001c{_VALUE}\u001f"),
        # Interior whitespace is preserved.
        (" usage test\u00a0value ", "usage test\u00a0value"),
    ],
)
@mock_aws()
def test_strips_js_trim_whitespace(raw: str, expected: str) -> None:
    _put_secure_string(raw)
    reader = _reader()

    assert reader.secret_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize("blank", [" ", "\n\t \r\n", "\ufeff\u00a0\u2028"])
@mock_aws()
def test_blank_parameter_value_raises(blank: str) -> None:
    _put_secure_string(blank)
    reader = _reader()

    with pytest.raises(UsageSecretError) as excinfo:
        reader.secret_bytes()
    assert str(excinfo.value) == "Usage_Secret value is blank"
    assert "not loaded" in repr(reader)


@pytest.mark.parametrize(
    "response",
    [
        {"Parameter": {"Name": _PARAMETER_NAME, "Type": "SecureString"}},
        {"Parameter": {"Name": _PARAMETER_NAME, "Value": _VALUE.encode("utf-8")}},
        {"Parameter": None},
        {"ResponseMetadata": {"Note": _VALUE}},
    ],
    ids=["no-value", "bytes-value", "parameter-none", "no-parameter"],
)
def test_no_string_value_raises_without_leaking_value(
    response: Mapping[str, object],
) -> None:
    """SSM equivalent of the old ``SecretBinary``-only case.

    moto cannot store a parameter without a string value, so a canned
    response stands in for the malformed shapes.
    """
    reader = _reader(_FixedResponseClient(response))

    with pytest.raises(UsageSecretError) as excinfo:
        reader.secret_bytes()

    assert str(excinfo.value) == "Usage_Secret has no string value"
    assert _VALUE not in str(excinfo.value)
    assert _VALUE not in repr(excinfo.value)
    # ``from None``: no wrapped KeyError/TypeError (or response mapping) in the chain.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ or excinfo.value.__context__ is None
    assert "not loaded" in repr(reader)


@mock_aws()
def test_parameter_not_found_propagates_and_is_not_cached() -> None:
    counting = _CountingClient(_client())
    reader = UsageSecretReader(parameter_name=_PARAMETER_NAME, ssm_client=counting)

    with pytest.raises(ClientError) as excinfo:
        reader.secret_bytes()
    assert excinfo.value.response["Error"]["Code"] == "ParameterNotFound"
    assert "not loaded" in repr(reader)

    _put_secure_string(_VALUE)

    assert reader.secret_bytes() == _VALUE.encode("utf-8")
    assert len(counting.calls) == 2


@mock_aws()
def test_repr_never_shows_the_secret() -> None:
    _put_secure_string(_VALUE)
    reader = _reader()

    assert repr(reader) == "UsageSecretReader(<not loaded>)"
    reader.secret_bytes()
    assert repr(reader) == "UsageSecretReader(<loaded>)"
    assert _VALUE not in repr(reader)
    assert _VALUE not in str(reader)
