"""Property tests for :func:`runner_image_uri_from_context` (trikon-go-live).

Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

The oracle is independent of the validator's ``_IMAGE_REF`` regex: valid
references are generated from their parts (a lower-case DNS registry with an
optional port, one or more OCI repository path components, then a Docker tag
other than ``latest`` or a ``sha256`` digest). Each rejection test applies one
mutation to a generated valid reference and expects a ``ValueError`` whose
message starts with ``runner_image_uri``.

**Validates: Requirements 5.2, 5.3, 5.4**

No AWS credentials, no network. The synthesis test runs under the shared
``no_network`` fixture.
"""

from __future__ import annotations

import string
import tempfile
from typing import NamedTuple

import pytest
from aws_cdk import Stack
from aws_cdk.assertions import Template
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trikon_cloud.fargate_runner.infra.app import (
    RUNNER_IMAGE_URI_CONTEXT,
    STACK_ID,
    build_app,
    runner_image_uri_from_context,
)

_LOWER_ALNUM = string.ascii_lowercase + string.digits
_TAG_FIRST = string.ascii_letters + string.digits + "_"
_TAG_REST = _TAG_FIRST + ".-"
_HEX = "0123456789abcdef"
_MESSAGE_PREFIX = r"^runner_image_uri"

# ASCII and Unicode whitespace; the validator must reject every one.
_WHITESPACE = [" ", "\t", "\n", "\r", "\x0b", "\x0c", "\x85", "\u00a0", "\u2003", "\u3000"]

_AWS_REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]


class _ImageRef(NamedTuple):
    """A valid Runner_Image_URI split into its parts."""

    registry: str
    repository: str
    reference: str  # ``:<tag>`` or ``@sha256:<64 hex>``

    @property
    def uri(self) -> str:
        return f"{self.registry}/{self.repository}{self.reference}"


# --- Generators for valid parts --------------------------------------------

_alnum_char = st.sampled_from(_LOWER_ALNUM)

# A DNS label: one lower-case alphanumeric, or alphanumerics around hyphens.
_dns_label = st.one_of(
    _alnum_char,
    st.builds(
        lambda first, middle, last: first + middle + last,
        _alnum_char,
        st.text(alphabet=_LOWER_ALNUM + "-", max_size=10),
        _alnum_char,
    ),
)

_port = st.integers(min_value=1, max_value=65535).map(lambda port: f":{port}")

_generic_registry = st.builds(
    lambda labels, port: ".".join(labels) + port,
    st.lists(_dns_label, min_size=1, max_size=4),
    st.one_of(st.just(""), _port),
)

_ecr_registry = st.builds(
    lambda account, region: f"{account}.dkr.ecr.{region}.amazonaws.com",
    st.text(alphabet=string.digits, min_size=12, max_size=12),
    st.sampled_from(_AWS_REGIONS),
)

_registry = st.one_of(_ecr_registry, _generic_registry)

# OCI path component: alphanumeric runs joined by ``.``, ``_``, ``__`` or ``-+``.
_run = st.text(alphabet=_LOWER_ALNUM, min_size=1, max_size=8)
_separator = st.one_of(
    st.sampled_from([".", "_", "__"]),
    st.integers(min_value=1, max_value=3).map(lambda n: "-" * n),
)
_component = st.builds(
    lambda first, rest: first + "".join(sep + run for sep, run in rest),
    _run,
    st.lists(st.tuples(_separator, _run), max_size=3),
)
_repository = st.lists(_component, min_size=1, max_size=4).map("/".join)

_tag = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(_TAG_FIRST),
    st.text(alphabet=_TAG_REST, max_size=127),
).filter(lambda tag: tag != "latest")

_reference = st.one_of(
    _tag.map(lambda tag: f":{tag}"),
    st.text(alphabet=_HEX, min_size=64, max_size=64).map(lambda hex_: f"@sha256:{hex_}"),
)

_image_refs = st.builds(_ImageRef, _registry, _repository, _reference)

# Values that are not strings, including the bytes of a valid reference.
_non_strings = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False),
    st.binary(),
    _image_refs.map(lambda ref: ref.uri.encode()),
    st.lists(_image_refs.map(lambda ref: ref.uri), min_size=1, max_size=2),
    st.dictionaries(st.just(RUNNER_IMAGE_URI_CONTEXT), _image_refs.map(lambda ref: ref.uri)),
)


def _assert_rejected(value: object) -> None:
    with pytest.raises(ValueError, match=_MESSAGE_PREFIX):
        runner_image_uri_from_context(value)


# --- Pass-through ------------------------------------------------------------


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(ref=_image_refs)
@settings(max_examples=200, deadline=None)
def test_valid_reference_is_returned_unchanged(ref: _ImageRef) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    **Validates: Requirements 5.2, 5.3, 5.4**
    """
    result = runner_image_uri_from_context(ref.uri)
    assert type(result) is str
    assert result == ref.uri


# --- Rejections --------------------------------------------------------------


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(ref=_image_refs, whitespace=st.sampled_from(_WHITESPACE), data=st.data())
@settings(max_examples=200, deadline=None)
def test_whitespace_anywhere_is_rejected(
    ref: _ImageRef, whitespace: str, data: st.DataObject
) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    **Validates: Requirements 5.3**
    """
    uri = ref.uri
    position = data.draw(st.integers(min_value=0, max_value=len(uri)), label="position")
    _assert_rejected(uri[:position] + whitespace + uri[position:])


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(ref=_image_refs, dangling=st.sampled_from(["", ":", "@", "@sha256:"]))
@settings(max_examples=200, deadline=None)
def test_missing_tag_or_digest_is_rejected(ref: _ImageRef, dangling: str) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    **Validates: Requirements 5.3**
    """
    _assert_rejected(f"{ref.registry}/{ref.repository}{dangling}")


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(ref=_image_refs)
@settings(max_examples=200, deadline=None)
def test_latest_tag_is_rejected(ref: _ImageRef) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    **Validates: Requirements 5.3**
    """
    _assert_rejected(f"{ref.registry}/{ref.repository}:latest")


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(ref=_image_refs, upper=st.sampled_from(string.ascii_uppercase), data=st.data())
@settings(max_examples=200, deadline=None)
def test_upper_case_repository_is_rejected(
    ref: _ImageRef, upper: str, data: st.DataObject
) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    One alphanumeric character of the repository becomes an upper-case letter.

    **Validates: Requirements 5.3**
    """
    positions = [i for i, ch in enumerate(ref.repository) if ch in _LOWER_ALNUM]
    i = data.draw(st.sampled_from(positions), label="position")
    repository = ref.repository[:i] + upper + ref.repository[i + 1 :]
    _assert_rejected(_ImageRef(ref.registry, repository, ref.reference).uri)


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@given(value=st.one_of(st.just(""), _non_strings))
@settings(max_examples=200, deadline=None)
def test_empty_or_non_string_is_rejected(value: object) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    **Validates: Requirements 5.2**
    """
    _assert_rejected(value)


# --- Synthesized container image -------------------------------------------


# Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through
@pytest.mark.usefixtures("no_network")
@given(ref=_image_refs)
@settings(
    # Each example synthesizes the whole stack through jsii (about 6 s on a
    # dev laptop), so this half of the property runs far fewer examples than
    # the validator half. Fixed examples live in
    # test_fargate_runner_stack_template.py.
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_accepted_reference_is_the_container_image(ref: _ImageRef) -> None:
    """Feature: trikon-go-live, Property 1: Runner image URI validation and pass-through.

    For any accepted value, the synthesized runner container image equals it.

    **Validates: Requirements 5.4**
    """
    with tempfile.TemporaryDirectory() as outdir:
        app = build_app(
            {"app_id": "999999", RUNNER_IMAGE_URI_CONTEXT: ref.uri}, outdir=outdir
        )
        stack = app.node.find_child(STACK_ID)
        assert isinstance(stack, Stack)
        task_defs = Template.from_stack(stack).find_resources("AWS::ECS::TaskDefinition")
    assert len(task_defs) == 1
    (task_def,) = task_defs.values()
    images = [c["Image"] for c in task_def["Properties"]["ContainerDefinitions"]]
    assert images == [ref.uri]
