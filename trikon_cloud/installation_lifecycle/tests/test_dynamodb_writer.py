# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass, and the boto3 client surfaces its
# ``get_item`` / ``update_item`` return values as ``dict[str, Any]`` at
# the moto boundary. Under the repo's ``disallow_any_explicit = true``
# mypy config both surface as ``explicit-any`` errors on library-generated
# / library-boundary code, not on hand-written signatures. Silence at
# file scope — this is a test module and every ``Any`` here is bounded
# to the moto / pydantic / MagicMock fixture surface. Mirrors the pattern
# used by ``trikon_cloud/fargate_runner/tests/test_dynamodb_writer.py``.
# mypy: disable-error-code="explicit-any"
"""Moto-backed unit tests for :class:`InstallationsTableWriter`.

Covers the four CRUD verbs the writer wraps against the
``trikon-cloud-installations`` DynamoDB table (design.md §2.2, §5.2
through §5.5). All tests exercise the writer through the real boto3
low-level ``dynamodb`` client under :func:`moto.mock_aws`, except for
the single error-propagation test that swaps in a
:class:`unittest.mock.MagicMock` to force a non-idempotent
:class:`~botocore.exceptions.ClientError`.

The table is created afresh inside every test's ``@mock_aws()``
context so per-test isolation is guaranteed — moto-v5 does not
compose fixtures across a single test the way v4 did. The schema
matches design.md §2.2 verbatim: partition key ``installation_id``
(``N``), ``PAY_PER_REQUEST`` billing, and point-in-time recovery
enabled.

Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency
on re-delivery. The ``upsert_active`` idempotency-win test and the
``add_repositories`` / ``remove_repositories`` no-op tests together
pin the DynamoDB-side leg of Property 11 for three of the four event
types the Lifecycle_Handler dispatches on (``installation.created``,
``installation_repositories.added``, ``installation_repositories.removed``);
the ``mark_disabled`` sentinel-creation test covers the fourth
(``installation.deleted``, Requirement 14.4).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.installation_lifecycle.dynamodb_writer import (
    InstallationsTableWriter,
    UpsertResult,
)

from .conftest import (
    CANONICAL_GITHUB_APP_ID,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_SENT_AT,
)

# ---------------------------------------------------------------------------
# Fixture constants — canonical wire values every test in this file reads.
# ---------------------------------------------------------------------------

_TABLE_NAME = "trikon-cloud-installations"
# A second, distinct ``sent_at`` string so tests that assert
# ``updated_at`` was bumped can distinguish "before" from "after" by
# byte comparison, not by wall-clock ordering.
_LATER_SENT_AT = "2024-11-14T13:45:00.000+00:00"


# ---------------------------------------------------------------------------
# Per-test moto setup helper.
# ---------------------------------------------------------------------------


def _create_installations_table(client: Any) -> None:
    """Create the ``trikon-cloud-installations`` table per design.md §2.2.

    Single-attribute partition key ``installation_id`` of DynamoDB type
    ``N`` (Number), ``PAY_PER_REQUEST`` billing mode, and point-in-time
    recovery enabled via a follow-up ``update_continuous_backups`` call.
    The CDK stack under
    ``trikon_cloud.orchestrator.infra.orchestrator_stack`` uses
    ``dynamodb.Billing.on_demand()`` which lowers to ``PAY_PER_REQUEST``
    on the CloudFormation wire, and configures PITR through
    ``PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)``
    (design.md §8.1). This helper mirrors that shape so the test-time
    table is byte-shape-equivalent to production.
    """
    client.create_table(
        TableName=_TABLE_NAME,
        KeySchema=[{"AttributeName": "installation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "installation_id", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.update_continuous_backups(
        TableName=_TABLE_NAME,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )


def _make_writer(client: Any) -> InstallationsTableWriter:
    """Build an :class:`InstallationsTableWriter` bound to the moto client."""
    return InstallationsTableWriter(ddb_client=client, table_name=_TABLE_NAME)


def _get_item(client: Any, installation_id: int) -> dict[str, Any]:
    """Read the row for ``installation_id`` back through the raw client.

    Returns the ``Item`` dict directly — callers assert on the raw
    wire shape (``{"S":…, "N":…, "SS":…}``) so drift in the writer's
    attribute-value construction is caught. Raises :class:`KeyError`
    if the row is absent, which is louder than the boto3 default of
    silently returning a response with no ``"Item"`` key.
    """
    response = client.get_item(
        TableName=_TABLE_NAME,
        Key={"installation_id": {"N": str(installation_id)}},
    )
    item: dict[str, Any] = response["Item"]
    return item


# ---------------------------------------------------------------------------
# upsert_active — create / re-upsert / disabled→active / error propagation.
# ---------------------------------------------------------------------------


@mock_aws()
def test_upsert_active_creates_row_with_status_active_and_all_fields() -> None:
    """First ``upsert_active`` writes every design.md §2.2 attribute.

    The row must carry ``status="active"`` plus the four data fields
    (``github_app_id``, ``created_at``, ``updated_at``, ``repositories``)
    and the partition key, and the returned :class:`UpsertResult` must
    signal that this was NOT an idempotent re-delivery
    (``already_active=False``).
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    result = writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    assert result == UpsertResult(already_active=False)

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    assert item["installation_id"] == {"N": str(CANONICAL_INSTALLATION_ID)}
    assert item["status"] == {"S": "active"}
    assert item["github_app_id"] == {"N": str(CANONICAL_GITHUB_APP_ID)}
    assert item["created_at"] == {"S": CANONICAL_SENT_AT}
    assert item["updated_at"] == {"S": CANONICAL_SENT_AT}
    # SS attributes come back as a Python list on the wire — DynamoDB
    # does not guarantee element order, so normalize to ``set`` before
    # comparing.
    assert item["repositories"].keys() == {"SS"}
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}


@mock_aws()
def test_upsert_active_on_already_active_row_returns_already_active_true() -> None:
    """A second ``upsert_active`` on an active row returns the idempotency-win result.

    Property 11 (Lifecycle idempotency on re-delivery) for the
    ``installation.created`` event type. The
    ``ConditionalCheckFailedException`` fired by DynamoDB against the
    ``attribute_not_exists(installation_id) OR #s <> :active``
    condition is caught by the writer and translated to
    ``UpsertResult(already_active=True)`` (Requirement 14.3; design.md
    §5.2). The row's data fields must NOT be overwritten by the
    second call — the ``created_at`` timestamp is the load-bearing
    witness because it is only ever written on the create path.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    result = writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=_LATER_SENT_AT,
    )

    assert result == UpsertResult(already_active=True)

    # The conditional write was rejected, so ``created_at`` MUST still
    # carry the first-write timestamp — the second call did not
    # overwrite the row.
    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    assert item["created_at"] == {"S": CANONICAL_SENT_AT}
    assert item["updated_at"] == {"S": CANONICAL_SENT_AT}


@mock_aws()
def test_upsert_active_after_mark_disabled_transitions_row_back_to_active() -> None:
    """A disabled row is transitioned back to ``"active"`` on the next upsert.

    The condition on ``upsert_active`` is
    ``attribute_not_exists(installation_id) OR #s <> :active`` — the
    second disjunct allows overwriting any row whose ``status`` is
    anything but ``"active"``. When the disabled sentinel exists from
    a prior ``installation.deleted``, a fresh ``installation.created``
    for the same id must reactivate the row byte-for-byte (design.md
    §5.2) and return ``already_active=False`` because the write
    genuinely mutated the row.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    # (a) Seed a disabled sentinel. ``mark_disabled`` has no
    # ``ConditionExpression`` so this creates the row from empty.
    writer.mark_disabled(CANONICAL_INSTALLATION_ID, now_iso=CANONICAL_SENT_AT)
    assert _get_item(client, CANONICAL_INSTALLATION_ID)["status"] == {"S": "disabled"}

    # (b) Now upsert active — the ``#s <> :active`` disjunct fires,
    # the row is overwritten, and the writer reports a genuine flip.
    result = writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=_LATER_SENT_AT,
    )

    assert result == UpsertResult(already_active=False)
    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    assert item["status"] == {"S": "active"}
    assert item["github_app_id"] == {"N": str(CANONICAL_GITHUB_APP_ID)}
    assert item["created_at"] == {"S": _LATER_SENT_AT}
    assert item["updated_at"] == {"S": _LATER_SENT_AT}
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}


def test_upsert_active_non_conditional_client_error_propagates() -> None:
    """A ``ClientError`` whose code is not the idempotency win escapes unhandled.

    The writer must NOT swallow generic ``PutItem`` failures — only
    the specific ``ConditionalCheckFailedException`` folds into an
    ``UpsertResult(already_active=True)``. Any other error code (here:
    ``ThrottlingException``) propagates so the Lifecycle_Handler's
    outer SQS batch-item-failure retry loop can rescue (Requirement
    14.3 / design.md §5.2).

    Uses a :class:`unittest.mock.MagicMock` via the ``ddb_client`` DI
    seam rather than moto — the point is to verify the writer's
    ``except`` discriminator, not the DynamoDB service semantics.
    """
    patched_client: Any = MagicMock()
    patched_client.put_item.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "mocked"}},
        "PutItem",
    )
    writer = _make_writer(patched_client)

    with pytest.raises(ClientError) as exc_info:
        writer.upsert_active(
            CANONICAL_INSTALLATION_ID,
            github_app_id=CANONICAL_GITHUB_APP_ID,
            repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
            now_iso=CANONICAL_SENT_AT,
        )

    assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"


# ---------------------------------------------------------------------------
# mark_disabled — existing row flip + missing-row sentinel creation.
# ---------------------------------------------------------------------------


@mock_aws()
def test_mark_disabled_flips_status_on_existing_row_and_bumps_updated_at() -> None:
    """An existing active row's ``status`` flips to ``"disabled"`` and ``updated_at`` bumps.

    The writer emits ``SET #s = :disabled, updated_at = :now`` without
    a ``ConditionExpression`` (design.md §5.3). The other row
    attributes — ``created_at``, ``github_app_id``, ``repositories`` —
    must be preserved byte-for-byte because ``SET`` on individual
    attributes never touches sibling attributes.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    writer.mark_disabled(CANONICAL_INSTALLATION_ID, now_iso=_LATER_SENT_AT)

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    # ``status`` flipped.
    assert item["status"] == {"S": "disabled"}
    # ``updated_at`` refreshed to the newer timestamp.
    assert item["updated_at"] == {"S": _LATER_SENT_AT}
    # ``created_at`` preserved — the SET expression does not touch it.
    assert item["created_at"] == {"S": CANONICAL_SENT_AT}
    # Sibling data attributes preserved.
    assert item["github_app_id"] == {"N": str(CANONICAL_GITHUB_APP_ID)}
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}


@mock_aws()
def test_mark_disabled_on_missing_row_creates_sentinel_with_three_attrs() -> None:
    """A ``mark_disabled`` against an absent partition key creates a sentinel.

    Requirement 14.4: an ``installation.deleted`` event that races
    ahead of the ``installation.created`` write must still leave a
    consistent audit trail. The writer omits ``ConditionExpression``,
    so DynamoDB creates a fresh row containing exactly the partition
    key (from the ``Key`` argument), ``status="disabled"``, and
    ``updated_at=now_iso`` (from the ``SET`` clause) — no
    ``created_at``, no ``github_app_id``, no ``repositories`` (design.md
    §5.3).
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.mark_disabled(CANONICAL_INSTALLATION_ID, now_iso=CANONICAL_SENT_AT)

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    # Exactly three attributes populated on the sentinel row.
    assert set(item.keys()) == {"installation_id", "status", "updated_at"}
    assert item["installation_id"] == {"N": str(CANONICAL_INSTALLATION_ID)}
    assert item["status"] == {"S": "disabled"}
    assert item["updated_at"] == {"S": CANONICAL_SENT_AT}


# ---------------------------------------------------------------------------
# add_repositories — new member + already-present no-op.
# ---------------------------------------------------------------------------


@mock_aws()
def test_add_repositories_adds_new_repo_to_ss_attribute() -> None:
    """A new repo is added to the row's ``repositories`` SS attribute.

    Establishes a baseline row through ``upsert_active`` with a
    single-member SS, then ``add_repositories`` a distinct member.
    The resulting SS must contain both entries — DynamoDB set-union
    semantics (design.md §5.4). ``updated_at`` must be refreshed to
    the newer timestamp because the ``SET updated_at = :now`` clause
    runs unconditionally alongside the ``ADD``.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    writer.add_repositories(
        CANONICAL_INSTALLATION_ID,
        repositories=frozenset({"octocat/new-repo"}),
        now_iso=_LATER_SENT_AT,
    )

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    assert set(item["repositories"]["SS"]) == {
        CANONICAL_REPO_FULL_NAME,
        "octocat/new-repo",
    }
    assert item["updated_at"] == {"S": _LATER_SENT_AT}


@mock_aws()
def test_add_repositories_already_present_is_noop_on_set_membership() -> None:
    """Re-adding a member already in the SS is a no-op — set unchanged, no error.

    Property 11 (Lifecycle idempotency on re-delivery) for the
    ``installation_repositories.added`` event type. Requirement 14.5:
    DynamoDB's ``ADD`` on a string set is naturally idempotent — the
    membership does not grow, no exception is raised, and no
    ``ConditionExpression`` is required (design.md §5.4).

    ``updated_at`` still refreshes because the writer's
    ``SET updated_at = :now`` clause runs unconditionally — that is
    the intended semantics for re-delivery bookkeeping.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    # Re-adding the exact same member the row already carries.
    writer.add_repositories(
        CANONICAL_INSTALLATION_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=_LATER_SENT_AT,
    )

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    # Set membership unchanged — still exactly one member.
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}
    # ``updated_at`` still refreshed.
    assert item["updated_at"] == {"S": _LATER_SENT_AT}


# ---------------------------------------------------------------------------
# remove_repositories — existing member + absent-member no-op.
# ---------------------------------------------------------------------------


@mock_aws()
def test_remove_repositories_removes_existing_member_from_ss() -> None:
    """Removing an existing repo drops it from the ``repositories`` SS.

    Establishes a two-member SS via ``upsert_active``, then removes
    one of the two. The remaining member must survive — DynamoDB
    set-difference semantics (design.md §5.4).
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME, "octocat/second"}),
        now_iso=CANONICAL_SENT_AT,
    )

    writer.remove_repositories(
        CANONICAL_INSTALLATION_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=_LATER_SENT_AT,
    )

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    assert set(item["repositories"]["SS"]) == {"octocat/second"}
    assert item["updated_at"] == {"S": _LATER_SENT_AT}


@mock_aws()
def test_remove_repositories_absent_member_is_noop() -> None:
    """Removing a member the SS does not contain is a no-op — no error, set unchanged.

    Property 11 (Lifecycle idempotency on re-delivery) for the
    ``installation_repositories.removed`` event type. Requirement
    14.5: DynamoDB's ``DELETE`` on a string set is naturally
    idempotent — deleting a member that is not present leaves the set
    untouched and does not raise (design.md §5.4).

    As with the ``add_repositories`` no-op case, ``updated_at`` still
    refreshes because the writer's ``SET updated_at = :now`` clause is
    unconditional — the re-delivery bookkeeping stays consistent even
    when the set-membership delta is empty.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_installations_table(client)
    writer = _make_writer(client)

    writer.upsert_active(
        CANONICAL_INSTALLATION_ID,
        github_app_id=CANONICAL_GITHUB_APP_ID,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        now_iso=CANONICAL_SENT_AT,
    )

    # Attempt to remove a repo that was never in the SS.
    writer.remove_repositories(
        CANONICAL_INSTALLATION_ID,
        repositories=frozenset({"octocat/never-added"}),
        now_iso=_LATER_SENT_AT,
    )

    item = _get_item(client, CANONICAL_INSTALLATION_ID)
    # Set membership unchanged — the one original repo is still there.
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}
    assert item["updated_at"] == {"S": _LATER_SENT_AT}
