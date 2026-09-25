# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every ``BaseModel`` subclass. Under this repo's ``disallow_any_explicit =
# true`` mypy config, each class definition surfaces as an ``explicit-any``
# error. The error refers to code the plugin generates, not code we write —
# silence at the file level. Mirrors the pattern established by sibling
# ``trikon_cloud/installation_lifecycle/models.py``.
# mypy: disable-error-code="explicit-any"
#
# The :class:`DynamoDbClientProtocol` methods mirror boto3's PascalCase
# ``ecs.RunTask``-style kwargs (``TableName``, ``Item``, ``Key``, …).
# Ruff's N803 (lowercase-argument-name) rule would fire on every such
# parameter; silencing at the file level keeps the protocol readable at
# the wire-shape level. The equivalent single-line pattern lives at
# ``trikon_cloud/orchestrator/ecs_dispatcher.py::SsmClientProtocol.get_parameter``.
# ruff: noqa: N803
"""DynamoDB writer for the ``trikon-cloud-installations`` lifecycle table.

Wraps the four CRUD operations the Lifecycle_Handler needs into one class
(design.md §2.2 table for ``trikon_cloud.installation_lifecycle.dynamodb_writer``
and §5.2 through §5.5):

* :meth:`InstallationsTableWriter.upsert_active` — conditional
  ``PutItem`` that succeeds either by creating a fresh ``"active"`` row
  or by overwriting an existing non-active row. On
  ``ConditionalCheckFailedException`` the writer returns
  :class:`UpsertResult` with ``already_active=True`` (Requirement 14.3;
  design.md §5.2). Any other ``ClientError`` propagates so the handler's
  outer SQS batch-item-failure retry can rescue.
* :meth:`InstallationsTableWriter.mark_disabled` — unconditional
  ``UpdateItem`` flipping ``status`` to ``"disabled"`` and bumping
  ``updated_at``. No ``ConditionExpression`` — an update on a missing key
  creates a dormant ``"disabled"`` sentinel row, which is the intended
  semantics when an ``installation.deleted`` event races ahead of the
  ``installation.created`` write (Requirement 14.4; design.md §5.3).
* :meth:`InstallationsTableWriter.add_repositories` /
  :meth:`InstallationsTableWriter.remove_repositories` — atomic
  ``UpdateItem`` with ``ADD`` / ``DELETE`` on the ``repositories`` SS
  attribute. DynamoDB set semantics are naturally idempotent — adding a
  member that already exists is a no-op; deleting a member that is not
  present is a no-op (Requirement 14.5; design.md §5.4). No conditional
  expression is required.

Boundary discipline (Invariant 8 / Requirement 17.2):

* :class:`DynamoDbClientProtocol` is a :class:`typing.Protocol` — its
  method signatures are typed with no :data:`typing.Any` on any
  parameter. Tests can pass a moto client; production passes a real
  ``boto3.client("dynamodb")``. Both satisfy the protocol structurally.
* The internal ``{"S":…, "N":…, "SS":…}`` attribute-value dicts are
  scoped to method bodies. They never cross a module boundary — every
  public signature on :class:`InstallationsTableWriter` speaks only in
  :class:`int`, :class:`str`, :class:`frozenset[str]`, and
  :class:`UpsertResult`.

Determinism:

* Every ``SS`` write sorts the input via :func:`sorted` before handing
  it to DynamoDB. DynamoDB normalizes set order server-side, but sorting
  client-side keeps wire captures byte-stable across retries and
  simplifies snapshot testing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, TypeAlias

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

# Design-order (writer → protocol → result), not alphabetical. Matches the
# ordering established by sibling ``iam_template.py`` and ``models.py``.
__all__ = [  # noqa: RUF022
    "InstallationsTableWriter",
    "DynamoDbClientProtocol",
    "UpsertResult",
]


# ---------------------------------------------------------------------------
# Private wire-shape aliases used inside the Protocol only.
# ---------------------------------------------------------------------------
#
# ``_AttributeValue`` captures the three DynamoDB descriptors this writer
# emits: ``S`` (string), ``N`` (number, wire-encoded as a string), and ``SS``
# (string-set). All three fit ``Mapping[str, str | Sequence[str]]`` — the
# scalar descriptors map to ``str`` and ``SS`` maps to ``Sequence[str]``.
# ``B`` (bytes) is intentionally omitted: no field on the installations
# table is binary (design.md §2.2 schema).
_AttributeValue: TypeAlias = Mapping[str, str | Sequence[str]]

# ``_ItemMapping`` — one row's worth of attribute-value pairs, keyed by
# attribute name. Used for both ``Item`` (put_item) and
# ``ExpressionAttributeValues`` (put_item / update_item) parameter slots.
_ItemMapping: TypeAlias = Mapping[str, _AttributeValue]

# ``_KeyMapping`` — the partition-key attribute map. The installations
# table has a single ``installation_id`` (N) key; every ``Key`` argument in
# this module is ``{"installation_id": {"N": str(installation_id)}}``.
_KeyMapping: TypeAlias = Mapping[str, Mapping[str, str]]


# ---------------------------------------------------------------------------
# Boto3 client protocol — structural type for dependency injection.
# ---------------------------------------------------------------------------


class DynamoDbClientProtocol(Protocol):
    """Structural type for the subset of boto3's DynamoDB client we call.

    Declares the four data-plane methods the writer touches — ``put_item``,
    ``get_item``, ``update_item``, ``delete_item``. Each method's
    parameters are typed precisely so the writer's call sites are checked
    against the protocol shape (Invariant 8 / Requirement 17.2).

    Return values are typed as :class:`object` (never :data:`typing.Any`)
    per the pattern established by
    :class:`trikon_cloud.orchestrator.ecs_dispatcher.EcsClientProtocol`.
    The writer never inspects a response body — the load-bearing branch
    is on :class:`botocore.exceptions.ClientError`, whose ``response``
    attribute is untyped upstream and read directly at the ``except``
    site.

    ``get_item`` and ``delete_item`` are declared even though this
    writer does not currently call them, so the protocol matches the
    design.md §2.2 API surface for the four CRUD verbs and downstream
    modules can share the same protocol without redefinition.
    """

    def put_item(
        self,
        *,
        TableName: str,
        Item: _ItemMapping,
        ConditionExpression: str = ...,
        ExpressionAttributeNames: Mapping[str, str] = ...,
        ExpressionAttributeValues: _ItemMapping = ...,
    ) -> object:
        ...

    def get_item(
        self,
        *,
        TableName: str,
        Key: _KeyMapping,
    ) -> object:
        ...

    def update_item(
        self,
        *,
        TableName: str,
        Key: _KeyMapping,
        UpdateExpression: str,
        ExpressionAttributeNames: Mapping[str, str] = ...,
        ExpressionAttributeValues: _ItemMapping = ...,
    ) -> object:
        ...

    def delete_item(
        self,
        *,
        TableName: str,
        Key: _KeyMapping,
    ) -> object:
        ...


# ---------------------------------------------------------------------------
# Result models.
# ---------------------------------------------------------------------------


class UpsertResult(BaseModel):
    """Return type of :meth:`InstallationsTableWriter.upsert_active`.

    ``already_active=True`` signals the natural-key idempotency win: the
    row already carried ``status="active"`` when the write was attempted,
    so the ``ConditionalCheckFailedException`` fired and the writer
    treated it as a successful re-delivery (Requirement 14.3; design.md
    §5.2). ``already_active=False`` covers both the fresh-create branch
    and the transition-from-disabled branch.

    Frozen so the value cannot be mutated post-construction;
    ``extra="forbid"`` so any accidental field addition is a validation
    error rather than a silent no-op.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    already_active: bool


# ---------------------------------------------------------------------------
# Writer.
# ---------------------------------------------------------------------------


class InstallationsTableWriter:
    """CRUD wrapper over the ``trikon-cloud-installations`` DynamoDB table.

    Every method scopes its operations to the ``installation_id`` passed
    in. The four methods are idempotent by construction — see the module
    docstring and design.md §5.5's idempotency table for the specific
    AWS-side mechanism used by each verb.

    The writer holds no state beyond the injected ``ddb_client`` and the
    ``table_name`` string. A single instance can safely serve every
    Lifecycle_Handler invocation on a warm container.
    """

    def __init__(
        self,
        *,
        ddb_client: DynamoDbClientProtocol,
        table_name: str,
    ) -> None:
        self._ddb_client = ddb_client
        self._table_name = table_name

    def upsert_active(
        self,
        installation_id: int,
        *,
        github_app_id: int,
        repositories: frozenset[str],
        now_iso: str,
    ) -> UpsertResult:
        """Insert (or overwrite non-active) the row with ``status="active"``.

        Uses ``PutItem`` with
        ``ConditionExpression="attribute_not_exists(installation_id) OR
        #s <> :active"`` (``#s`` aliases the reserved word ``status``;
        ``:active`` is the string literal ``"active"``). The write
        succeeds when the row is missing OR its current status is
        anything other than ``"active"`` (design.md §5.2).

        On ``ConditionalCheckFailedException`` the row is already active
        and the writer returns :class:`UpsertResult` with
        ``already_active=True`` (Requirement 14.3). Any other
        ``ClientError`` propagates unhandled so the handler's outer SQS
        batch-item-failure retry can rescue.

        ``repositories`` is sorted before serialization — DynamoDB
        normalizes SS order server-side but a deterministic client wire
        simplifies snapshot testing and retry-byte-comparison.
        """
        item: dict[str, dict[str, str | list[str]]] = {
            "installation_id": {"N": str(installation_id)},
            "status": {"S": "active"},
            "github_app_id": {"N": str(github_app_id)},
            "created_at": {"S": now_iso},
            "updated_at": {"S": now_iso},
            "repositories": {"SS": sorted(repositories)},
        }
        try:
            self._ddb_client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(installation_id) OR #s <> :active"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":active": {"S": "active"}},
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ConditionalCheckFailedException":
                return UpsertResult(already_active=True)
            raise
        return UpsertResult(already_active=False)

    def mark_disabled(self, installation_id: int, *, now_iso: str) -> None:
        """Flip the row's ``status`` to ``"disabled"``.

        Uses ``UpdateItem`` with
        ``UpdateExpression="SET #s = :disabled, updated_at = :now"`` and
        no ``ConditionExpression``. If the row does not yet exist,
        DynamoDB creates a minimal ``"disabled"`` sentinel row with only
        ``installation_id``, ``status``, and ``updated_at`` populated —
        the intended semantics when an ``installation.deleted`` event
        races ahead of the ``installation.created`` write (Requirement
        14.4; design.md §5.3).
        """
        self._ddb_client.update_item(
            TableName=self._table_name,
            Key={"installation_id": {"N": str(installation_id)}},
            UpdateExpression="SET #s = :disabled, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":disabled": {"S": "disabled"},
                ":now": {"S": now_iso},
            },
        )

    def add_repositories(
        self,
        installation_id: int,
        *,
        repositories: frozenset[str],
        now_iso: str,
    ) -> None:
        """Atomically add ``repositories`` to the row's SS attribute.

        Uses ``UpdateItem`` with
        ``UpdateExpression="ADD repositories :repos SET updated_at = :now"``.
        DynamoDB's ``ADD`` on a set is naturally idempotent — adding a
        member that already exists is a no-op (Requirement 14.5;
        design.md §5.4). No ``ConditionExpression`` is required.

        ``repositories`` is sorted before serialization for wire-byte
        stability across retries (see :meth:`upsert_active`).
        """
        self._ddb_client.update_item(
            TableName=self._table_name,
            Key={"installation_id": {"N": str(installation_id)}},
            UpdateExpression="ADD repositories :repos SET updated_at = :now",
            ExpressionAttributeValues={
                ":repos": {"SS": sorted(repositories)},
                ":now": {"S": now_iso},
            },
        )

    def remove_repositories(
        self,
        installation_id: int,
        *,
        repositories: frozenset[str],
        now_iso: str,
    ) -> None:
        """Atomically remove ``repositories`` from the row's SS attribute.

        Uses ``UpdateItem`` with
        ``UpdateExpression="DELETE repositories :repos SET updated_at = :now"``.
        DynamoDB's ``DELETE`` on a set is naturally idempotent — deleting
        a member that is not present is a no-op (Requirement 14.5;
        design.md §5.4). No ``ConditionExpression`` is required.

        ``repositories`` is sorted before serialization for wire-byte
        stability across retries (see :meth:`upsert_active`).
        """
        self._ddb_client.update_item(
            TableName=self._table_name,
            Key={"installation_id": {"N": str(installation_id)}},
            UpdateExpression="DELETE repositories :repos SET updated_at = :now",
            ExpressionAttributeValues={
                ":repos": {"SS": sorted(repositories)},
                ":now": {"S": now_iso},
            },
        )
