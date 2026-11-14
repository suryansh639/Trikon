"""Usage_Secret reader for the Orchestrator and the Usage_Reporter (Req 11.2, 15.5).

The Usage_Secret is read from an SSM Parameter Store ``SecureString``
(``ssm:GetParameter`` with ``WithDecryption=True``) once per container and kept
only in memory, in an instance attribute (Req 15.5). Surrounding whitespace is
stripped with the same character set as JavaScript's ``String.prototype.trim``,
because Trikon_Web trims its ``TRIKON_CLOUD_USAGE_SECRET`` env value; both
sides then key the HMAC with identical bytes.

The secret is never logged, never put in an exception message, and never shown
by ``repr`` (Req 11.17, 15.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

__all__ = ["UsageSecretError", "UsageSecretReader"]

# The characters JavaScript's String.prototype.trim removes: WhiteSpace
# (TAB, VT, FF, SP, NBSP, ZWNBSP, and every Unicode "Zs" space) and
# LineTerminator (LF, CR, LS, PS). Python's str.strip() differs (it strips
# U+001C-U+001F and U+0085, and keeps U+FEFF), so the set is spelled out.
_JS_TRIM_CHARS = (
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class _SsmGetParameterProtocol(Protocol):
    """Structural type for the one boto3 SSM call we make.

    ``boto3`` ships without type stubs; this Protocol keeps ``mypy --strict``
    clean without ``boto3-stubs``. The real response also carries
    ``ResponseMetadata``, and ``Parameter`` carries ``Name``, ``Type``,
    ``Version``, ``ARN`` and more, hence the ``object`` values.
    """

    def get_parameter(
        self, *, Name: str, WithDecryption: bool  # noqa: N803 -- AWS boto3 API contract
    ) -> Mapping[str, object]: ...


class UsageSecretError(RuntimeError):
    """The Usage_Secret has no usable string value (missing, not text, or blank).

    The message is fixed and never includes the secret or any part of it.
    """


class UsageSecretReader:
    """Reads the Usage_Secret once and serves it from memory afterwards.

    Construct one per Lambda container (at cold start) and share it with the
    Usage_API client. The SSM call happens on the first :meth:`secret_bytes`
    call, not in the constructor, so a cold start that never reaches the
    Usage_API makes no call. A failed read is not cached, so the next call
    tries again. Errors from the SSM client (for example ``ParameterNotFound``)
    propagate unchanged.
    """

    def __init__(self, *, parameter_name: str, ssm_client: _SsmGetParameterProtocol) -> None:
        self._parameter_name: str = parameter_name
        self._ssm_client: _SsmGetParameterProtocol = ssm_client
        self._secret: bytes | None = None

    def __repr__(self) -> str:
        loaded = "loaded" if self._secret is not None else "not loaded"
        return f"UsageSecretReader(<{loaded}>)"

    def secret_bytes(self) -> bytes:
        """Return the stripped Usage_Secret as UTF-8 bytes, reading it on first use."""
        if self._secret is None:
            self._secret = self._read()
        return self._secret

    def _read(self) -> bytes:
        response = self._ssm_client.get_parameter(
            Name=self._parameter_name, WithDecryption=True
        )
        try:
            value = cast(Mapping[str, object], response["Parameter"])["Value"]
        except (KeyError, TypeError):
            # ``from None`` keeps the response mapping out of ``__context__``.
            raise UsageSecretError("Usage_Secret has no string value") from None
        if not isinstance(value, str):
            raise UsageSecretError("Usage_Secret has no string value")
        stripped = value.strip(_JS_TRIM_CHARS)
        if not stripped:
            raise UsageSecretError("Usage_Secret value is blank")
        return stripped.encode("utf-8")
