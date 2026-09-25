# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass, and the boto3 client's attribute-value
# dicts surface as ``dict[str, Any]`` at the moto boundary. Under the repo's
# ``disallow_any_explicit = true`` mypy config both surface as
# ``explicit-any`` errors that refer to library-generated / library-boundary
# code, not to hand-written signatures. Silence at file scope — this is a
# test module and every ``Any`` here is bounded to the moto / pydantic
# fixture surface.
# mypy: disable-error-code="explicit-any"
"""Moto-backed tests for :class:`DynamoDBWriter` (Wave-6, task 6.6).

Covers the three data-plane touchpoints the writer wraps:

* ``dynamodb:PutItem`` on ``trikon_verdicts`` with the
  ``attribute_not_exists(installation_id) AND attribute_not_exists(sk)``
  condition — the load-bearing idempotency mechanism for Property 1
  (``Idempotency_On_Natural_Key``). Verified via a same-row second put
  raising :class:`VerdictAlreadyCommittedError` chained from a
  :class:`ClientError` carrying ``ConditionalCheckFailedException``.
* ``dynamodb:GetItem`` / ``dynamodb:PutItem`` on ``trikon_pr_state`` —
  round-trips the nullable ``last_comment_id`` / ``last_check_run_id``
  pair as DynamoDB ``NULL`` attributes.
* ``s3:PutObject`` on ``trikon-cloud-evidence`` — writes the spilled
  evidence blob at ``<installation_id>/<audit_id>.json.gz`` with
  ``ContentEncoding: gzip``.

Every test creates its own moto DynamoDB tables + S3 bucket inside a
``@mock_aws()`` context so per-test isolation is guaranteed — the
composite conftest fixtures (``dynamodb_tables``, ``evidence_bucket``)
do not compose across a single test, so tests decorate themselves
per the moto-v5 pattern documented in ``conftest.py``.
"""

from __future__ import annotations

import gzip
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.fargate_runner.dynamodb_writer import (
    DynamoDBWriter,
    VerdictAlreadyCommittedError,
    VerdictWriteError,
)
from trikon_cloud.fargate_runner.models import PrStateRow, VerdictRow

# ---------------------------------------------------------------------------
# Canonical row builder — every test tweaks a subset of fields.
# ---------------------------------------------------------------------------


def _make_verdict_row(**overrides: object) -> VerdictRow:
    """Return a canonical :class:`VerdictRow` with inline evidence.

    Defaults construct a well-formed inline row (``evidence_blob``
    non-``None``, ``evidence_s3_key`` ``None``). Callers pass
    keyword overrides to flip individual fields — most commonly to
    force the spilled branch by setting ``evidence_blob=None`` and
    ``evidence_s3_key="..."``.
    """
    defaults: dict[str, object] = {
        "installation_id": 12345678,
        "sk": "2024-01-15T10:00:00.000Z#a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "repo_full_name": "octocat/hello-world",
        "pr_number": 42,
        "head_sha": "6aabf09b" + "0" * 32,
        "base_sha": "87f7a31b" + "0" * 32,
        "decision": "block",
        "matched_rule": "test",
        "blast_radius_score": 42,
        "new_errors": 3,
        "new_warnings": 1,
        "preexisting_errors": 7,
        "duration_ms": 1500,
        "fargate_task_arn": "arn:aws:ecs:us-east-1:123:task/x",
        "schema_version": 2,
        "evidence_blob": gzip.compress(b'{"static":{"new_errors":3}}'),
        "evidence_s3_key": None,
        "risk_bucket_sk": "0042#2024-01-15T10:00:00.000Z",
    }
    defaults.update(overrides)
    return VerdictRow(**defaults)


# ---------------------------------------------------------------------------
# Per-test moto setup helpers.
# ---------------------------------------------------------------------------


def _create_verdicts_table(client: Any) -> None:
    """Create the ``trikon_verdicts`` table with the design.md §5.2 key schema."""
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


def _create_pr_state_table(client: Any) -> None:
    """Create the ``trikon_pr_state`` table with the design.md §5.3 key schema."""
    client.create_table(
        TableName="trikon_pr_state",
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _make_writer() -> DynamoDBWriter:
    """Build a writer wired to the canonical moto table + bucket names."""
    return DynamoDBWriter(
        verdicts_table_name="trikon_verdicts",
        pr_state_table_name="trikon_pr_state",
        evidence_bucket_name="trikon-cloud-evidence",
    )


# ---------------------------------------------------------------------------
# put_verdict — shape / idempotency / error translation.
# ---------------------------------------------------------------------------


@mock_aws
def test_put_verdict_writes_expected_shape() -> None:
    """Every design.md §5.2 attribute lands with the correct DynamoDB type prefix.

    The item shape is the memo §5.5 contract — numbers as ``{"N": "..."}``,
    strings as ``{"S": "..."}``, the gzipped evidence blob as
    ``{"B": b"..."}``. Verifies exactly the inline-evidence branch:
    ``evidence_blob`` is present, ``evidence_s3_key`` is absent (the
    ``model_validator`` guarantees they cannot both be non-``None``, and
    the writer only sets one of the two).
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    row = _make_verdict_row()
    writer = _make_writer()

    writer.put_verdict(row=row)

    response = dynamodb.get_item(
        TableName="trikon_verdicts",
        Key={
            "installation_id": {"N": str(row.installation_id)},
            "sk": {"S": row.sk},
        },
    )
    item = response["Item"]

    # Every string field lands as {"S": ...}.
    assert item["sk"] == {"S": row.sk}
    assert item["repo_full_name"] == {"S": row.repo_full_name}
    assert item["head_sha"] == {"S": row.head_sha}
    assert item["base_sha"] == {"S": row.base_sha}
    assert item["decision"] == {"S": row.decision}
    assert item["matched_rule"] == {"S": row.matched_rule}
    assert item["fargate_task_arn"] == {"S": row.fargate_task_arn}
    assert item["risk_bucket_sk"] == {"S": row.risk_bucket_sk}

    # Every numeric field lands as {"N": "..."} with a string body.
    assert item["installation_id"] == {"N": str(row.installation_id)}
    assert item["pr_number"] == {"N": str(row.pr_number)}
    assert item["blast_radius_score"] == {"N": str(row.blast_radius_score)}
    assert item["new_errors"] == {"N": str(row.new_errors)}
    assert item["new_warnings"] == {"N": str(row.new_warnings)}
    assert item["preexisting_errors"] == {"N": str(row.preexisting_errors)}
    assert item["duration_ms"] == {"N": str(row.duration_ms)}
    assert item["schema_version"] == {"N": str(row.schema_version)}

    # The blob lands as {"B": <bytes>}. boto3's low-level client returns
    # the raw bytes (the wire-level base64 is transparent to callers).
    assert "evidence_blob" in item
    assert row.evidence_blob is not None
    assert item["evidence_blob"] == {"B": row.evidence_blob}

    # The spill pointer is absent on the inline branch.
    assert "evidence_s3_key" not in item


@mock_aws
def test_put_verdict_condition_expression_prevents_double_write() -> None:
    """A repeat put on the same ``(installation_id, sk)`` raises the specific error.

    The load-bearing Property 1 (``Idempotency_On_Natural_Key``)
    assertion: the second put fails with
    :class:`VerdictAlreadyCommittedError` (subclass of
    :class:`VerdictWriteError`), and its ``__cause__`` chain preserves
    the underlying :class:`ClientError` with
    ``ConditionalCheckFailedException`` so the entrypoint's ``except``
    can distinguish the natural-key win from a generic write failure.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    row = _make_verdict_row()
    writer = _make_writer()

    writer.put_verdict(row=row)  # first put succeeds

    with pytest.raises(VerdictAlreadyCommittedError) as exc_info:
        writer.put_verdict(row=row)

    cause = exc_info.value.__cause__
    assert isinstance(cause, ClientError)
    assert cause.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_put_verdict_translates_other_client_errors_to_verdict_write_error() -> None:
    """Non-conditional ``ClientError`` translates to :class:`VerdictWriteError`.

    Uses a :class:`MagicMock` injected via the ``dynamodb_client`` DI
    seam so the test does not need moto — the point is to verify the
    error-code discrimination in ``put_verdict``: any code other than
    ``ConditionalCheckFailedException`` maps to the generic write-error
    class (never to the idempotency-win subclass).
    """
    patched_client = MagicMock()
    patched_client.put_item.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "mocked"}},
        "PutItem",
    )
    writer = DynamoDBWriter(
        verdicts_table_name="trikon_verdicts",
        pr_state_table_name="trikon_pr_state",
        evidence_bucket_name="trikon-cloud-evidence",
        dynamodb_client=patched_client,
    )

    row = _make_verdict_row()

    with pytest.raises(VerdictWriteError) as exc_info:
        writer.put_verdict(row=row)

    # The specific subclass must NOT fire for a non-conditional error.
    assert not isinstance(exc_info.value, VerdictAlreadyCommittedError)


@mock_aws
def test_put_verdict_with_evidence_s3_key_writes_string_attr() -> None:
    """The spilled branch writes ``evidence_s3_key`` and omits ``evidence_blob``."""
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    row = _make_verdict_row(
        evidence_blob=None,
        evidence_s3_key="12345678/abc.json.gz",
    )
    writer = _make_writer()

    writer.put_verdict(row=row)

    response = dynamodb.get_item(
        TableName="trikon_verdicts",
        Key={
            "installation_id": {"N": str(row.installation_id)},
            "sk": {"S": row.sk},
        },
    )
    item = response["Item"]

    assert item["evidence_s3_key"] == {"S": "12345678/abc.json.gz"}
    assert "evidence_blob" not in item


# ---------------------------------------------------------------------------
# get_pr_state — read-path round trips.
# ---------------------------------------------------------------------------


@mock_aws
def test_get_pr_state_returns_row_when_present() -> None:
    """A populated row round-trips into a :class:`PrStateRow` model.

    Puts a raw item directly via boto3 (skipping the writer's own
    upsert path) so the read path is exercised against the exact wire
    shape the runner will see in production. Asserts every field maps
    back to the model with the expected Python type.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    pr_key = "12345678#octocat/hello-world#42"
    dynamodb.put_item(
        TableName="trikon_pr_state",
        Item={
            "pr_key": {"S": pr_key},
            "last_comment_id": {"N": "222"},
            "last_check_run_id": {"N": "111"},
            "last_head_sha": {"S": "a" * 40},
            "last_updated_at": {"S": "2024-01-15T10:00:00.000Z"},
        },
    )

    writer = _make_writer()
    row = writer.get_pr_state(
        installation_id=12345678,
        repo_full_name="octocat/hello-world",
        pr_number=42,
    )

    assert row is not None
    assert isinstance(row, PrStateRow)
    assert row.pr_key == pr_key
    assert row.last_comment_id == 222
    assert row.last_check_run_id == 111
    assert row.last_head_sha == "a" * 40
    assert row.last_updated_at == "2024-01-15T10:00:00.000Z"


@mock_aws
def test_get_pr_state_returns_none_when_absent() -> None:
    """A missing row on the composite key resolves to ``None``.

    The first-run path — no prior invocation for this PR has upserted
    a row, so ``get_pr_state`` returns ``None`` and the caller falls
    through to the create-comment / create-check-run branch.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    writer = _make_writer()
    row = writer.get_pr_state(
        installation_id=12345678,
        repo_full_name="octocat/hello-world",
        pr_number=42,
    )

    assert row is None


@mock_aws
def test_get_pr_state_null_ids_round_trip_to_none() -> None:
    """DynamoDB ``NULL`` attributes for the two ID fields decode to ``None``.

    Mirrors the shape the writer's ``upsert_pr_state`` emits on the
    first-run path — the row exists but the last-known-artifact IDs
    are absent because there has been no prior GitHub post to
    reference.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    pr_key = "12345678#octocat/hello-world#42"
    dynamodb.put_item(
        TableName="trikon_pr_state",
        Item={
            "pr_key": {"S": pr_key},
            "last_comment_id": {"NULL": True},
            "last_check_run_id": {"NULL": True},
            "last_head_sha": {"S": "a" * 40},
            "last_updated_at": {"S": "2024-01-15T10:00:00.000Z"},
        },
    )

    writer = _make_writer()
    row = writer.get_pr_state(
        installation_id=12345678,
        repo_full_name="octocat/hello-world",
        pr_number=42,
    )

    assert row is not None
    assert row.last_comment_id is None
    assert row.last_check_run_id is None


# ---------------------------------------------------------------------------
# upsert_pr_state — write-path round trips.
# ---------------------------------------------------------------------------


@mock_aws
def test_upsert_pr_state_overwrites_prior_row() -> None:
    """A second upsert on the same ``pr_key`` overwrites the prior values.

    The table is a last-known-state table, not append-only —
    :meth:`upsert_pr_state` intentionally issues a plain ``PutItem``
    with no ``ConditionExpression`` so the second write clobbers the
    first. Guards against a regression that adds a conditional-put
    and breaks the same-PR update path.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    writer = _make_writer()
    pr_key = "12345678#octocat/hello-world#42"

    row_v1 = PrStateRow(
        pr_key=pr_key,
        last_comment_id=222,
        last_check_run_id=111,
        last_head_sha="a" * 40,
        last_updated_at="2024-01-15T10:00:00.000Z",
    )
    row_v2 = PrStateRow(
        pr_key=pr_key,
        last_comment_id=333,
        last_check_run_id=222,
        last_head_sha="b" * 40,
        last_updated_at="2024-01-15T11:00:00.000Z",
    )

    writer.upsert_pr_state(row=row_v1)
    writer.upsert_pr_state(row=row_v2)

    response = dynamodb.get_item(
        TableName="trikon_pr_state",
        Key={"pr_key": {"S": pr_key}},
    )
    item = response["Item"]

    assert item["last_check_run_id"] == {"N": "222"}
    assert item["last_comment_id"] == {"N": "333"}
    assert item["last_head_sha"] == {"S": "b" * 40}


@mock_aws
def test_upsert_pr_state_null_ids_written_as_null_attr() -> None:
    """Nullable IDs on the input model become DynamoDB ``NULL`` attributes.

    The read path (:meth:`get_pr_state`) round-trips this back to
    ``None`` on the Python side — verified separately in
    :func:`test_get_pr_state_null_ids_round_trip_to_none`.
    """
    dynamodb = boto3.client("dynamodb", region_name="us-east-1")
    _create_verdicts_table(dynamodb)
    _create_pr_state_table(dynamodb)

    writer = _make_writer()
    pr_key = "12345678#octocat/hello-world#42"

    row = PrStateRow(
        pr_key=pr_key,
        last_comment_id=None,
        last_check_run_id=None,
        last_head_sha="a" * 40,
        last_updated_at="2024-01-15T10:00:00.000Z",
    )

    writer.upsert_pr_state(row=row)

    response = dynamodb.get_item(
        TableName="trikon_pr_state",
        Key={"pr_key": {"S": pr_key}},
    )
    item = response["Item"]

    assert item["last_comment_id"] == {"NULL": True}
    assert item["last_check_run_id"] == {"NULL": True}


# ---------------------------------------------------------------------------
# spill_evidence_to_s3 — object key + content encoding.
# ---------------------------------------------------------------------------


@mock_aws
def test_spill_evidence_to_s3_writes_object() -> None:
    """The spilled evidence lands at ``<installation_id>/<audit_id>.json.gz``.

    Key format is load-bearing: the per-installation IAM policy grants
    ``s3:PutObject`` on the ``LeadingKeys`` prefix
    ``<installation_id>/`` (design.md §9), so the write is only
    authorized when the object key starts with the caller's own
    installation id. ``ContentEncoding: gzip`` lets downstream
    consumers stream the body without an extra metadata call.
    """
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="trikon-cloud-evidence")

    writer = _make_writer()
    audit_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    key = writer.spill_evidence_to_s3(
        installation_id=12345678,
        audit_id=audit_id,
        gzipped_bytes=b"gzip data",
    )

    assert key == f"12345678/{audit_id}.json.gz"

    got = s3.get_object(Bucket="trikon-cloud-evidence", Key=key)
    assert got["Body"].read() == b"gzip data"
    assert got["ContentEncoding"] == "gzip"


def test_spill_evidence_to_s3_raises_verdict_write_error_on_client_error() -> None:
    """A ``ClientError`` from S3 translates to :class:`VerdictWriteError`.

    Uses a :class:`MagicMock` via the ``s3_client`` DI seam so the test
    does not need moto. Verifies the writer's error-translation
    contract: no raw boto3 exceptions escape to the entrypoint.
    """
    patched_s3 = MagicMock()
    patched_s3.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "mocked"}},
        "PutObject",
    )
    writer = DynamoDBWriter(
        verdicts_table_name="trikon_verdicts",
        pr_state_table_name="trikon_pr_state",
        evidence_bucket_name="trikon-cloud-evidence",
        s3_client=patched_s3,
    )

    with pytest.raises(VerdictWriteError):
        writer.spill_evidence_to_s3(
            installation_id=12345678,
            audit_id=uuid4(),
            gzipped_bytes=b"gzip data",
        )


# ---------------------------------------------------------------------------
# compute_evidence_blob — pure helper.
# ---------------------------------------------------------------------------


def test_compute_evidence_blob_gzips_correctly() -> None:
    """The static helper produces a gzip-decompressible byte string.

    The helper is a :func:`staticmethod` because callers need to
    compute the compressed size (to decide between inline / spill)
    before materializing a writer instance. Verifies round-trip via
    :func:`gzip.decompress`.
    """
    result = DynamoDBWriter.compute_evidence_blob(evidence_json='{"key": "value"}')

    assert isinstance(result, bytes)
    assert gzip.decompress(result) == b'{"key": "value"}'
