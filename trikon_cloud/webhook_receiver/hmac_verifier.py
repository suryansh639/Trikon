"""Constant-time HMAC-SHA256 verifier for the GitHub webhook receiver.

The single public function :func:`verify_signature` implements the
verification algorithm GitHub documents for ``X-Hub-Signature-256``. Two
correctness properties from design.md §6 govern this module:

* **Property 1 — HMAC verification is total and correct.** For any
  triple ``(body, signature_header, secret)`` in the domain
  ``(bytes, str | None, bytes)`` the function returns a ``bool`` — it
  never raises, never blocks, and never touches the network. The return
  value is ``True`` if and only if ``signature_header`` equals
  ``"sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()``,
  and ``False`` in every other case (missing header, empty header,
  missing ``sha256=`` prefix, wrong-length digest, non-hex digest,
  mismatched digest).

* **Property 3 — HMAC verification runs in constant time.** The
  comparison between the parsed header digest and the computed digest
  uses :func:`hmac.compare_digest`, never the ``==`` operator. This is
  a source-level invariant enforced by an AST-walking test — a future
  refactor that reintroduces ``==`` between two hex digests would leak
  byte-by-byte match information to a network-timing attacker and must
  break the build.

The function must not log — a leaked digest byte in a log line would
undermine Invariant 6 (secrets never enter observability planes).
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["verify_signature"]


def verify_signature(body: bytes, signature_header: str | None, secret: bytes) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` header against ``body``.

    Args:
        body: Raw HTTP request body bytes (post base64-decode).
        signature_header: Verbatim ``X-Hub-Signature-256`` header value,
            or ``None`` if the request did not carry the header.
        secret: The shared webhook secret bytes fetched from Secrets
            Manager.

    Returns:
        ``True`` iff ``signature_header`` matches the SHA-256 HMAC of
        ``body`` computed under ``secret``; ``False`` in every other
        case (see module docstring for the full enumeration).
    """
    if signature_header is None or signature_header == "":
        return False
    if not signature_header.startswith("sha256="):
        return False
    parsed = signature_header.removeprefix("sha256=")
    if len(parsed) != 64:
        return False
    try:
        int(parsed, 16)
    except ValueError:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # Property 3: constant-time compare — never replace with ``==``.
    return hmac.compare_digest(parsed, expected)
