"""Usage_Signature construction for Usage_API requests (sandbox-minute-plans §9).

Pure: no I/O, no clock, no AWS. The caller supplies the Usage_Secret, the
Request_Timestamp, and the body bytes.

The signature must match Trikon_Web's ``src/usage/signature.ts``
(``computeUsageSignature``) byte for byte: the lowercase hex HMAC-SHA256,
keyed with the Usage_Secret, of the Request_Timestamp text (whole Unix
seconds, ASCII), the character ``"."``, and the raw body bytes (Req 11.2).
Trikon_Web keys the HMAC with its ``TRIKON_CLOUD_USAGE_SECRET`` string, which
Node encodes as UTF-8, so callers pass the secret as its UTF-8 bytes.

:func:`encode_body` produces the exact bytes that are both signed and sent.
The HTTP client must post them with ``content=`` (never ``json=``) so the bytes
on the wire are the bytes that were signed.

The Usage_Secret and every signature are secrets: never log them (Req 11.17,
12.15).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import BaseModel

__all__ = [
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "encode_body",
    "sign",
    "signed_headers",
]

#: Header carrying the Request_Timestamp (whole Unix seconds). HTTP header
#: names are case-insensitive; Trikon_Web reads the lowercased form.
TIMESTAMP_HEADER = "X-Trikon-Timestamp"

#: Header carrying the Usage_Signature (64 lowercase hex characters).
SIGNATURE_HEADER = "X-Trikon-Signature"


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 of ``f"{timestamp}." + body``.

    Args:
        secret: The Usage_Secret as UTF-8 bytes.
        timestamp: The Request_Timestamp in whole Unix seconds. Its decimal
            text (no sign, no leading zeros) is what Trikon_Web signs.
        body: The exact request body bytes that will be sent.
    """
    message = f"{timestamp}.".encode("ascii") + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signed_headers(secret: bytes, body: bytes, now_seconds: int) -> dict[str, str]:
    """Return the request headers for one signed Usage_API attempt.

    Each attempt calls this again with a fresh ``now_seconds`` so a retry
    never reuses a timestamp that may have left the Signature_Window.
    """
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(now_seconds),
        SIGNATURE_HEADER: sign(secret, now_seconds, body),
    }


def encode_body(payload: BaseModel) -> bytes:
    """Return compact, key-sorted JSON for ``payload``.

    These exact bytes are signed and sent. Non-ASCII text is ``\\uXXXX``
    escaped, so the output is pure ASCII; non-finite floats are rejected
    rather than emitted as invalid JSON.
    """
    return json.dumps(
        payload.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
