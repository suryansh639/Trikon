"""Tests for :mod:`trikon_cloud.secure_params` (Req 3.1, 3.2, 3.3).

Example tests come first, then the ``parameter_arn`` half of Property 2,
then Property 1 (CDK context validation, Req 3.2, 3.3, 8.2), which covers
:func:`~trikon_cloud.secure_params.parameter_name_from_context` and the
orchestrator app's ``use_fargate_spot`` parser.
"""

from __future__ import annotations

import string
from pathlib import Path

import aws_cdk as cdk
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud import secure_params
from trikon_cloud.orchestrator.infra.app import (
    USE_FARGATE_SPOT_CONTEXT,
    _optional_bool_context,
)
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_CONTEXT,
    APP_KEY_PARAMETER_DEFAULT,
    USAGE_SECRET_PARAMETER_CONTEXT,
    USAGE_SECRET_PARAMETER_DEFAULT,
    WEBHOOK_SECRET_PARAMETER_CONTEXT,
    WEBHOOK_SECRET_PARAMETER_DEFAULT,
    parameter_arn,
    parameter_name_from_context,
)

_CONTEXT_DEFAULTS = [
    (APP_KEY_PARAMETER_CONTEXT, APP_KEY_PARAMETER_DEFAULT),
    (WEBHOOK_SECRET_PARAMETER_CONTEXT, WEBHOOK_SECRET_PARAMETER_DEFAULT),
    (USAGE_SECRET_PARAMETER_CONTEXT, USAGE_SECRET_PARAMETER_DEFAULT),
]


def test_constants_match_the_requirements() -> None:
    assert APP_KEY_PARAMETER_CONTEXT == "app_private_key_parameter_name"
    assert WEBHOOK_SECRET_PARAMETER_CONTEXT == "webhook_secret_parameter_name"
    assert USAGE_SECRET_PARAMETER_CONTEXT == "usage_secret_parameter_name"
    assert APP_KEY_PARAMETER_DEFAULT == "/trikon-cloud/github-app-private-key"
    assert WEBHOOK_SECRET_PARAMETER_DEFAULT == "/trikon-cloud/github-webhook-secret"
    assert USAGE_SECRET_PARAMETER_DEFAULT == "/trikon-cloud/usage-secret"
    assert secure_params.APP_KEY_PARAMETER_ENV == "TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME"
    assert secure_params.WEBHOOK_SECRET_PARAMETER_ENV == "TRIKON_WEBHOOK_SECRET_PARAMETER_NAME"
    assert (
        secure_params.USAGE_SECRET_PARAMETER_ENV == "TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME"
    )


@pytest.mark.parametrize(("key", "default"), _CONTEXT_DEFAULTS)
def test_absent_context_returns_the_default(key: str, default: str) -> None:
    assert parameter_name_from_context(None, key=key, default=default) == default


@pytest.mark.parametrize("name", ["/a", "/team/trikon_cloud/app-key.v2", "/x" * 505])
def test_valid_name_is_returned_unchanged(name: str) -> None:
    assert (
        parameter_name_from_context(
            name, key=APP_KEY_PARAMETER_CONTEXT, default=APP_KEY_PARAMETER_DEFAULT
        )
        == name
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty (Req 3.3)
        "trikon-cloud/usage-secret",  # no leading "/" (Req 3.3)
        "/",  # nothing after the root
        "/has space",
        "/bad:colon",
        "/trailing-newline\n",
        "/" + "a" * 1011,  # longer than SSM allows
        42,
        ["/a"],
    ],
)
@pytest.mark.parametrize(("key", "default"), _CONTEXT_DEFAULTS)
def test_invalid_context_raises_naming_the_key(raw: object, key: str, default: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        parameter_name_from_context(raw, key=key, default=default)
    assert str(excinfo.value).startswith(key)


def test_parameter_arn_concatenates_the_name_after_parameter() -> None:
    assert (
        parameter_arn(region="us-east-1", account="123456789012", name=USAGE_SECRET_PARAMETER_DEFAULT)
        == "arn:aws:ssm:us-east-1:123456789012:parameter/trikon-cloud/usage-secret"
    )


def test_parameter_arn_accepts_a_partition_and_cdk_tokens() -> None:
    assert (
        parameter_arn(
            region="${Token[AWS.Region.1]}",
            account="${Token[AWS.AccountId.2]}",
            name="/a",
            partition="aws-us-gov",
        )
        == "arn:aws-us-gov:ssm:${Token[AWS.Region.1]}:${Token[AWS.AccountId.2]}:parameter/a"
    )


def test_parameter_arn_rejects_a_name_without_leading_slash() -> None:
    with pytest.raises(AssertionError):
        parameter_arn(region="us-east-1", account="123456789012", name="no-slash")


# ---------------------------------------------------------------------------
# Property 2 (parameter_arn half): the Parameter_ARN template.
# The renderer half lives in installation_lifecycle/tests/test_iam_template.py.
# ---------------------------------------------------------------------------

# Valid SSM parameter names: "/" followed by 1-1010 characters of the SSM
# name character set, so every draw passes parameter_name_from_context.
_PARAMETER_NAMES = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/",
    min_size=1,
    max_size=1010,
).map(lambda tail: "/" + tail)
_ACCOUNTS = st.text(alphabet="0123456789", min_size=12, max_size=12)
_REGIONS = st.from_regex(r"[a-z]{2}(-gov)?-[a-z]{4,9}-[1-9]", fullmatch=True)
_PARTITIONS = st.sampled_from(["aws", "aws-us-gov", "aws-cn"])


# Feature: trikon-cloud-cost-cuts, Property 2: App-key statement uses the Parameter_ARN
@given(name=_PARAMETER_NAMES, account=_ACCOUNTS, region=_REGIONS, partition=_PARTITIONS)
@settings(max_examples=200, deadline=None)
def test_property_parameter_arn_template(
    name: str, account: str, region: str, partition: str
) -> None:
    """**Validates: Requirements 4.4, 6.1**

    For any valid name, 12-digit account, region and partition,
    ``parameter_arn`` is exactly ``arn:<partition>:ssm:<region>:<account>:parameter<name>``,
    and the name is recoverable unchanged from the resource part. The
    default partition is ``aws``.
    """
    # The generator only draws names the CDK context validator accepts.
    assert (
        parameter_name_from_context(
            name, key=APP_KEY_PARAMETER_CONTEXT, default=APP_KEY_PARAMETER_DEFAULT
        )
        == name
    )

    arn = parameter_arn(region=region, account=account, name=name, partition=partition)

    assert arn == f"arn:{partition}:ssm:{region}:{account}:parameter{name}"
    # SSM names cannot contain ":", so the six ARN fields split cleanly and
    # the resource part is "parameter" followed by the name itself (no
    # extra or missing "/").
    assert arn.split(":") == ["arn", partition, "ssm", region, account, f"parameter{name}"]
    assert parameter_arn(region=region, account=account, name=name) == (
        f"arn:aws:ssm:{region}:{account}:parameter{name}"
    )


# ---------------------------------------------------------------------------
# Property 1: CDK context validation (Req 3.2, 3.3, 8.2).
# ---------------------------------------------------------------------------

# Oracle for parameter names, restated from the rules rather than reusing
# secure_params._NAME_PATTERN: a "/" followed by 1-1010 more characters,
# every character an ASCII letter, digit, "_", ".", "-" or "/".
_SSM_NAME_CHARS = string.ascii_letters + string.digits + "_.-/"
_SSM_NAME_MIN_LEN = 2
_SSM_NAME_MAX_LEN = 1011


def _is_valid_parameter_name(raw: object) -> bool:
    return (
        isinstance(raw, str)
        and raw[:1] == "/"
        and _SSM_NAME_MIN_LEN <= len(raw) <= _SSM_NAME_MAX_LEN
        and all(ch in _SSM_NAME_CHARS for ch in raw)
    )


_VALID_NAMES = st.text(alphabet=_SSM_NAME_CHARS, min_size=1, max_size=80).map(
    lambda tail: "/" + tail
)
# Names around the length limit: 1010 and 1011 characters are valid,
# 1012 and 1013 are too long.
_BOUNDARY_NAMES = st.integers(min_value=1009, max_value=1012).map(lambda n: "/" + "a" * n)
# Characters SSM (or this validator) rejects, including whitespace, ":",
# NUL, non-ASCII letters and digits, and a full-width lookalike.
_BAD_NAME_CHARS = st.sampled_from(
    [" ", ":", "\n", "\t", "\x00", "*", "\\", "@", "é", "ß", "\u0661", "\uff41", "\u200b"]
)
_NEAR_MISS_KINDS = st.sampled_from(
    ["no_slash", "bare_slash", "empty", "bad_char", "trailing_newline", "too_long"]
)


@st.composite
def _near_miss_names(draw: st.DrawFn) -> str:
    """A valid name with one rule broken (the oracle still decides)."""
    name = draw(_VALID_NAMES)
    kind = draw(_NEAR_MISS_KINDS)
    if kind == "no_slash":
        return name.lstrip("/")
    if kind == "bare_slash":
        return "/"
    if kind == "empty":
        return ""
    if kind == "bad_char":
        index = draw(st.integers(min_value=0, max_value=len(name)))
        return name[:index] + draw(_BAD_NAME_CHARS) + name[index:]
    if kind == "trailing_newline":
        return name + "\n"
    extra = draw(st.integers(min_value=1, max_value=50))
    return name + "a" * (_SSM_NAME_MAX_LEN - len(name) + extra)


_NON_STRING_VALUES = st.one_of(
    st.booleans(),
    st.integers(),
    st.floats(),
    st.binary(max_size=8),
    st.lists(st.text(alphabet=_SSM_NAME_CHARS, max_size=8), max_size=3),
    st.dictionaries(st.text(max_size=4), st.text(max_size=4), max_size=2),
)

# ``None`` stands for an absent context key.
_PARAMETER_NAME_CONTEXT_VALUES = st.one_of(
    st.none(),
    _VALID_NAMES,
    _BOUNDARY_NAMES,
    _near_miss_names(),
    st.text(max_size=40),
    _NON_STRING_VALUES,
)


# Feature: trikon-cloud-cost-cuts, Property 1: CDK context validation
@given(raw=_PARAMETER_NAME_CONTEXT_VALUES, key_default=st.sampled_from(_CONTEXT_DEFAULTS))
@settings(max_examples=200, deadline=None)
def test_property_parameter_name_context_validation(
    raw: object, key_default: tuple[str, str]
) -> None:
    """**Validates: Requirements 3.2, 3.3**

    For any context value and any Parameter_Name_Context key,
    ``parameter_name_from_context`` returns the key's default when the value
    is absent, returns a valid name unchanged, and otherwise raises
    ``ValueError`` whose message starts with the key.
    """
    key, default = key_default

    if raw is None:
        assert parameter_name_from_context(raw, key=key, default=default) == default
    elif _is_valid_parameter_name(raw):
        assert parameter_name_from_context(raw, key=key, default=default) == raw
    else:
        with pytest.raises(ValueError) as excinfo:
            parameter_name_from_context(raw, key=key, default=default)
        assert str(excinfo.value).startswith(key)


# Oracle for the Spot_Flag, restated from Req 8.2: absent means true, a JSON
# bool is itself, "true"/"false" in any ASCII letter case is the bool, and
# everything else (including padded strings and ints) is rejected.
_SPOT_WORDS = (("true", True), ("false", False))


def _expected_spot_flag(raw: object) -> bool | None:
    """The parsed flag, or ``None`` when the parser must raise."""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        for word, value in _SPOT_WORDS:
            if len(raw) == len(word) and all(
                ch in (lower, lower.upper()) for ch, lower in zip(raw, word, strict=True)
            ):
                return value
    return None


@st.composite
def _cased_spot_words(draw: st.DrawFn) -> str:
    """``true`` or ``false`` with each letter independently upper- or lower-case."""
    word = draw(st.sampled_from([word for word, _ in _SPOT_WORDS]))
    return "".join(draw(st.sampled_from([ch, ch.upper()])) for ch in word)


_SPOT_PADDING = st.sampled_from([" ", "\t", "\n", "\x00", "\u00a0"])


@st.composite
def _padded_spot_words(draw: st.DrawFn) -> str:
    word = draw(_cased_spot_words())
    before = draw(st.booleans())
    pad = draw(_SPOT_PADDING)
    return pad + word if before else word + pad


# Strings must survive the jsii round trip into a real ``cdk.App``, so lone
# surrogates are excluded. NaN is excluded because jsii serializes JSON.
_SPOT_TEXT = st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=8)
_SPOT_NEAR_MISSES = st.sampled_from(
    ["", "yes", "no", "on", "off", "1", "0", "t", "f", "tru", "truee", "fals", "falsey",
     "null", "None", "\uff54rue", "tr\u00fce", "fal\u017fe"]
)
_SPOT_NON_STRINGS = st.one_of(
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(allow_nan=False, allow_infinity=False),
    st.lists(st.one_of(st.booleans(), _cased_spot_words()), max_size=2),
    st.dictionaries(st.sampled_from(["true", "false", "value"]), st.booleans(), max_size=2),
)

# ``None`` stands for an absent context key.
_SPOT_CONTEXT_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    _cased_spot_words(),
    _padded_spot_words(),
    _SPOT_NEAR_MISSES,
    _SPOT_TEXT,
    _SPOT_NON_STRINGS,
)


@pytest.fixture(scope="module")
def cdk_outdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shared outdir, so each bare ``cdk.App`` does not make its own temp dir."""
    return tmp_path_factory.mktemp("cdk-context")


# Feature: trikon-cloud-cost-cuts, Property 1: CDK context validation
@given(raw=_SPOT_CONTEXT_VALUES)
@settings(max_examples=200, deadline=None)
def test_property_use_fargate_spot_context_validation(cdk_outdir: Path, raw: object) -> None:
    """**Validates: Requirements 8.2**

    For any ``use_fargate_spot`` value, the orchestrator app's bool parser
    returns ``True`` when the key is absent, the bool for a JSON bool or
    ``"true"``/``"false"`` in any case, and otherwise raises ``ValueError``
    whose message starts with ``use_fargate_spot``. The value reaches the
    parser through a bare ``cdk.App`` (no stack is built).
    """
    context: dict[str, object] = {} if raw is None else {USE_FARGATE_SPOT_CONTEXT: raw}
    app = cdk.App(outdir=str(cdk_outdir), context=context)
    expected = _expected_spot_flag(raw)

    if expected is None:
        with pytest.raises(ValueError) as excinfo:
            _optional_bool_context(app, USE_FARGATE_SPOT_CONTEXT, default=True)
        assert str(excinfo.value).startswith(USE_FARGATE_SPOT_CONTEXT)
    else:
        assert _optional_bool_context(app, USE_FARGATE_SPOT_CONTEXT, default=True) is expected
