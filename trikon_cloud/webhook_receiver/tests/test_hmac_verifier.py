"""Unit + property tests for :mod:`trikon_cloud.webhook_receiver.hmac_verifier`.

Encodes two of the three correctness properties from
``.kiro/specs/trikon-cloud-webhook-receiver/design.md`` §6:

* **Property 1** — HMAC verification is total and correct. Encoded via
  a hypothesis strategy that draws ``(body, secret)`` pairs and a
  match-or-corrupt coin flip; the assertion is that the function's
  return value agrees with the coin flip across every drawn input.
* **Property 3** — HMAC verification runs in constant time. Encoded as
  a source-level AST check that (a) the module imports ``hmac``, (b)
  ``verify_signature`` contains exactly one call to
  ``hmac.compare_digest``, and (c) no ``==`` comparison inside the
  function body operates between two digest-shaped identifiers
  (``parsed``, ``expected``, ``signature``). Property 3 is a structural
  invariant Property 1 cannot detect by sampling — a timing side-channel
  is not observable from function-return values.

Also covers the deterministic edge cases enumerated in tasks.md §3.2 —
``None`` header, empty header, wrong prefix, wrong length, non-hex
characters, exact matching signature, and the ``caplog`` assertion that
the verifier never emits log records.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import logging
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.webhook_receiver import hmac_verifier
from trikon_cloud.webhook_receiver.hmac_verifier import verify_signature

# ---------------------------------------------------------------------------
# Property 1 — HMAC verification is total and correct.
# ---------------------------------------------------------------------------

# Five deterministic mutations of a valid ``sha256=<hex>`` signature. Each
# maps the correct signature to a distinct wrong-signature shape the
# verifier must reject; hypothesis draws one uniformly per example.
_MUTATION_STRATEGIES = st.sampled_from(
    ["drop_prefix", "swap_prefix_sha1", "flip_hex_char", "truncate_last", "none_header"]
)


@given(
    body=st.binary(min_size=0, max_size=10_000),
    secret=st.binary(min_size=1, max_size=256),
    should_match=st.booleans(),
    mutation=_MUTATION_STRATEGIES,
    flip_index=st.integers(min_value=0, max_value=63),
    hex_swap_char=st.sampled_from("0123456789abcdef"),
)
@settings(max_examples=200, deadline=None)
def test_property_hmac_verifier_is_total(
    body: bytes,
    secret: bytes,
    should_match: bool,
    mutation: str,
    flip_index: int,
    hex_swap_char: str,
) -> None:
    """Feature: trikon-cloud-webhook-receiver, Property 1: HMAC verification is total and correct.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4.
    """
    correct_digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    correct_sig = f"sha256={correct_digest}"

    if should_match:
        assert verify_signature(body, correct_sig, secret) is True
        return

    # Corrupt branch — apply one of five deterministic mutations and
    # assert the verifier returns False for the mutated header value.
    mutated: str | None
    if mutation == "drop_prefix":
        mutated = correct_digest  # missing ``sha256=`` prefix
    elif mutation == "swap_prefix_sha1":
        mutated = f"sha1={correct_digest}"
    elif mutation == "flip_hex_char":
        # Flip one hex char at ``flip_index`` to a different-but-valid hex char.
        chars = list(correct_digest)
        original = chars[flip_index]
        # Guarantee the swap produces a different character (hypothesis may
        # otherwise draw the same char, which would leave the signature valid).
        replacement = hex_swap_char if hex_swap_char != original else (
            "0" if original != "0" else "1"
        )
        chars[flip_index] = replacement
        mutated = "sha256=" + "".join(chars)
    elif mutation == "truncate_last":
        mutated = correct_sig[:-1]
    else:  # "none_header"
        mutated = None

    assert verify_signature(body, mutated, secret) is False


# ---------------------------------------------------------------------------
# Property 3 — HMAC verification runs in constant time (AST check).
# ---------------------------------------------------------------------------


_DIGEST_IDENTS = frozenset({"parsed", "expected", "signature"})


def _find_verify_signature_def(tree: ast.Module) -> ast.FunctionDef:
    """Locate the ``verify_signature`` FunctionDef within the parsed module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "verify_signature":
            return node
    raise AssertionError("verify_signature FunctionDef not found in hmac_verifier.py")


def _is_compare_digest_call(node: ast.Call) -> bool:
    """Return True iff ``node`` is a call to ``hmac.compare_digest`` or ``compare_digest``."""
    func = node.func
    # ``hmac.compare_digest(...)`` — Attribute access on the ``hmac`` module.
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "compare_digest"
        and isinstance(func.value, ast.Name)
        and func.value.id == "hmac"
    ):
        return True
    # ``compare_digest(...)`` — direct name after ``from hmac import compare_digest``.
    return isinstance(func, ast.Name) and func.id == "compare_digest"


def test_property_hmac_verifier_uses_compare_digest_not_eq() -> None:
    """Feature: trikon-cloud-webhook-receiver, Property 3: HMAC verification runs in constant time.

    Validates: Requirement 2.2, Invariant 6.
    """
    module_path = Path(hmac_verifier.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    # (a) Module imports ``hmac`` — either via ``import hmac`` or
    # ``from hmac import compare_digest``.
    imports_hmac = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "hmac":
                    imports_hmac = True
                    break
        elif isinstance(node, ast.ImportFrom) and node.module == "hmac":
            imports_hmac = True
        if imports_hmac:
            break
    assert imports_hmac, "hmac_verifier.py must import the ``hmac`` module"

    verify_def = _find_verify_signature_def(tree)

    # (b) Exactly one ``hmac.compare_digest`` / ``compare_digest`` call
    # inside the function body.
    compare_digest_calls = [
        node
        for node in ast.walk(verify_def)
        if isinstance(node, ast.Call) and _is_compare_digest_call(node)
    ]
    assert len(compare_digest_calls) == 1, (
        "verify_signature must contain exactly one hmac.compare_digest call; "
        f"found {len(compare_digest_calls)}"
    )

    # (c) No ``ast.Compare`` node in the function body operates with
    # ``ast.Eq`` between two identifiers both in {parsed, expected,
    # signature}. This is the anti-pattern Property 3 forbids — a byte-
    # by-byte ``==`` on two hex digests leaks timing information.
    for node in ast.walk(verify_def):
        if not isinstance(node, ast.Compare):
            continue
        for op_index, op in enumerate(node.ops):
            if not isinstance(op, ast.Eq):
                continue
            left_operand = node.left if op_index == 0 else node.comparators[op_index - 1]
            right_operand = node.comparators[op_index]
            if not (isinstance(left_operand, ast.Name) and isinstance(right_operand, ast.Name)):
                continue
            if left_operand.id in _DIGEST_IDENTS and right_operand.id in _DIGEST_IDENTS:
                raise AssertionError(
                    "verify_signature contains an ``==`` comparison between two "
                    f"digest-shaped identifiers ({left_operand.id!r}, "
                    f"{right_operand.id!r}); use hmac.compare_digest instead."
                )


# ---------------------------------------------------------------------------
# Deterministic edge cases (tasks.md §3.2).
# ---------------------------------------------------------------------------


def test_signature_header_is_none_returns_false() -> None:
    """A missing signature header returns False (never raises)."""
    assert verify_signature(b"", None, b"secret") is False


def test_signature_header_empty_string_returns_false() -> None:
    """An empty-string signature header returns False."""
    assert verify_signature(b"", "", b"secret") is False


def test_signature_header_missing_sha256_prefix_returns_false() -> None:
    """Headers without the ``sha256=`` prefix (including ``sha1=``) return False."""
    valid_length_hex = "a" * 64
    assert verify_signature(b"", "not-a-prefix", b"secret") is False
    assert verify_signature(b"", f"sha1={valid_length_hex}", b"secret") is False


def test_signature_header_wrong_length_returns_false() -> None:
    """Headers whose digest is shorter or longer than 64 hex chars return False."""
    assert verify_signature(b"", "sha256=abc", b"secret") is False
    assert verify_signature(b"", "sha256=" + "0" * 63, b"secret") is False


def test_signature_header_non_hex_returns_false() -> None:
    """A 64-char digest containing non-hex characters returns False."""
    assert verify_signature(b"", "sha256=" + "z" * 64, b"secret") is False


def test_matching_signature_returns_true_deterministic() -> None:
    """The verifier returns True for the exact HMAC-SHA256 signature of a fixed body / secret."""
    body = b"deterministic-body"
    secret = b"deterministic-secret"
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    sig = f"sha256={digest}"
    assert verify_signature(body, sig, secret) is True


def test_verifier_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    """The verifier emits no log records on either matching or mismatching inputs.

    Invariant 6 (secrets never enter observability planes) rules out
    logging inside the verifier — the digest and the parsed header both
    carry information an attacker with log-plane access could combine
    with request timing to recover signature bits.
    """
    body = b"logged-body"
    secret = b"logged-secret"
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    good_sig = f"sha256={digest}"
    bad_sig = f"sha256={'0' * 64}"

    with caplog.at_level(logging.DEBUG):
        assert verify_signature(body, good_sig, secret) is True
        assert verify_signature(body, bad_sig, secret) is False

    assert caplog.records == []
