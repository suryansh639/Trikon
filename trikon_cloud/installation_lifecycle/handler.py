"""AWS Lambda entrypoint for the Trikon Cloud installation-lifecycle handler.

Implements the five-step flow from
``.kiro/specs/trikon-cloud-orchestrator/design.md`` §5.1 and dispatches
to the four ``event_type``-specific handlers per §5.2 through §5.4.

The Lifecycle_Handler is SQS-triggered on the
``trikon-cloud-installation-events`` queue with batch size 1
(Requirement 10.1); it owns per-installation IAM task-role
provisioning + deprovisioning and the ``trikon-cloud-installations``
DynamoDB table.

Cold-start caches
-----------------

Module-scope ``_ENV``, ``_BOTO_SESSION``, ``_IAM_PROV``, and
``_DDB_WRITER`` are lazy-initialized inside :func:`_bootstrap` on the
first :func:`lambda_handler` invocation. On subsequent warm
invocations the singletons are reused — one env parse, one boto3
session, one IAM client, one DynamoDB client, one
:class:`~trikon_cloud.installation_lifecycle.iam_provisioner.IamProvisioner`,
and one
:class:`~trikon_cloud.installation_lifecycle.dynamodb_writer.InstallationsTableWriter`
for the entire container lifetime. The env is parsed inside
:func:`_bootstrap` (rather than at import time) so a
:class:`pydantic.ValidationError` surfaces during a real invocation
where AWS Lambda's error reporter can capture it, rather than during
the opaque cold-start init phase.

Failure semantics
-----------------

* :class:`pydantic.ValidationError` on the SQS body → structured
  ERROR log ``malformed_installation_event`` and return
  ``{"batchItemFailures": []}``. The record is deleted from the queue
  because a body Spec 1 could not have written (or wrote wrong) is
  not recoverable by redrive (Requirement 11.3).
* Any exception raised by an event-type handler → structured ERROR
  log ``lifecycle_terminal_failure`` with ``error_class="terminal"``
  and re-raise. SQS returns the message to the queue; after
  ``maxReceiveCount=3`` the DLQ absorbs it (Requirement 15.3).
  Deviation from design.md §5.1's narrower ``except ClientError``:
  the user prompt for task 12 explicitly says "any handler
  exception" — broadening the catch closes the gap where an
  unexpected non-``ClientError`` (a Pydantic validation error deep
  in a nested call, for instance) would otherwise bypass the
  structured Terminal-failure log and hit Lambda's default error
  reporter without the ``error_class`` marker Requirement 15.3
  requires.

Structured log context
----------------------

:func:`append_lifecycle_context` fires once per invocation immediately
after the inbound :class:`InstallationEventMessage` validates
(Requirement 15.2). Every subsequent log record within the same warm
invocation carries ``installation_id``, ``event_type``, and
``delivery_id``. Payload-side keys forbidden by Invariant 6 (raw SQS
body, App private key material, AWS credentials) are never emitted
as log keys — see
:data:`trikon_cloud.installation_lifecycle.logger.LOGGING_DENYLIST`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from trikon_cloud.installation_lifecycle.dynamodb_writer import (
    InstallationsTableWriter,
)
from trikon_cloud.installation_lifecycle.iam_provisioner import IamProvisioner
from trikon_cloud.installation_lifecycle.iam_template import (
    render_installation_assume_role_policy_document,
    render_installation_policy_document,
)
from trikon_cloud.installation_lifecycle.logger import (
    append_lifecycle_context,
    get_logger,
)
from trikon_cloud.installation_lifecycle.models import (
    InstallationEventMessage,
    LifecycleEnvConfig,
)
from trikon_cloud.orchestrator.models import SqsEventEnvelope

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext


# Ordering pinned by task 12 (tasks.md): the Lambda entrypoint first, then
# the four event-type handlers in the ``event_type`` literal order declared
# on :class:`InstallationEventMessage`. Not isort-alphabetical.
__all__ = [  # noqa: RUF022
    "lambda_handler",
    "handle_installation_created",
    "handle_installation_deleted",
    "handle_repositories_added",
    "handle_repositories_removed",
]


# ---------------------------------------------------------------------------
# Cold-start caches
# ---------------------------------------------------------------------------
#
# The four singletons below are populated lazily inside :func:`_bootstrap`
# on the first :func:`lambda_handler` invocation. AWS Lambda reuses the
# module across warm invocations, so a container's second and subsequent
# invocations skip env parsing, boto3 session construction, and client
# instantiation entirely. The ``None`` sentinel is the Pythonic marker
# for "not yet initialized" — a fresh module is guaranteed to have all
# four set to ``None`` because ``_bootstrap`` is the only writer.

_ENV: LifecycleEnvConfig | None = None
_BOTO_SESSION: boto3.session.Session | None = None
_IAM_PROV: IamProvisioner | None = None
_DDB_WRITER: InstallationsTableWriter | None = None


def _bootstrap() -> tuple[LifecycleEnvConfig, IamProvisioner, InstallationsTableWriter]:
    """Lazily initialize the four cold-start singletons.

    Returns the three objects the handler needs on every invocation —
    :class:`LifecycleEnvConfig`, :class:`IamProvisioner`, and
    :class:`InstallationsTableWriter`. The boto3 session is retained on
    the module (``_BOTO_SESSION``) but not returned; callers derive
    clients through the two wrappers only.

    The env parse runs on the first invocation, not at import time, so
    a :class:`pydantic.ValidationError` on a misconfigured Lambda
    surfaces during a live invocation — AWS Lambda's error reporter
    then captures it with the request id attached — rather than during
    the opaque cold-start init phase.
    """
    global _ENV, _BOTO_SESSION, _IAM_PROV, _DDB_WRITER

    if _ENV is None:
        _ENV = LifecycleEnvConfig()
    if _BOTO_SESSION is None:
        _BOTO_SESSION = boto3.session.Session(region_name=_ENV.aws_region)
    if _IAM_PROV is None:
        _IAM_PROV = IamProvisioner(iam_client=_BOTO_SESSION.client("iam"))
    if _DDB_WRITER is None:
        _DDB_WRITER = InstallationsTableWriter(
            ddb_client=_BOTO_SESSION.client("dynamodb"),
            table_name=_ENV.trikon_installations_table,
        )
    return _ENV, _IAM_PROV, _DDB_WRITER


def _now_iso_utc_ms() -> str:
    """Return the current UTC timestamp as ISO-8601 with millisecond precision.

    Format ``YYYY-MM-DDTHH:MM:SS.sss+00:00``. Captured once per
    :func:`lambda_handler` invocation and passed byte-identical into
    every DynamoDB write for that invocation so ``created_at`` /
    ``updated_at`` stay consistent across the ``iam_prov.provision``
    → ``ddb.upsert_active`` sequence (or the analogous deprovision
    sequence). Aligned with Spec 1 / Spec 2's timestamp discipline.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def lambda_handler(
    event: dict[str, object], context: LambdaContext
) -> dict[str, object]:
    """Consume one SQS record from ``trikon-cloud-installation-events``.

    Five-step flow per design.md §5.1:

    1. Parse the SQS envelope, assert batch size 1 (Requirement 10.1),
       then parse ``record.body`` as
       :class:`InstallationEventMessage`. A validation failure logs
       ``malformed_installation_event`` at ERROR and returns
       ``{"batchItemFailures": []}`` (Requirement 11.3) — the record
       is deleted, not redriven, because a shape drift from Spec 1's
       writer will not resolve on retry.
    2. Attach ``installation_id``, ``event_type``, and ``delivery_id``
       to the Powertools log context via
       :func:`append_lifecycle_context` (Requirement 15.2).
    3. Dispatch on ``event_type`` to one of the four handler functions
       (Requirement 12.1-12.4). ``match`` matches the four literal
       values declared on :class:`InstallationEventMessage.event_type`;
       Pydantic already rejected any fifth value at parse time.
    4. Any exception raised by a handler is caught, logged at ERROR
       as ``lifecycle_terminal_failure`` with ``error_class="terminal"``,
       and re-raised so SQS returns the message to the queue. After
       ``maxReceiveCount=3`` the DLQ absorbs it (Requirement 15.3).
    5. On success return ``{"batchItemFailures": []}``.

    The ``context`` parameter is accepted for AWS Lambda signature
    compatibility but not consumed — the handler correlates work via
    ``delivery_id`` (Requirement 1.4 analogue) rather than the AWS
    request id.
    """
    del context  # AWS Lambda signature; not consumed at this layer.

    logger = get_logger()
    envelope = SqsEventEnvelope.model_validate(event)
    # Requirement 10.1: batch size 1 is enforced by the event-source
    # mapping in the OrchestratorStack (task 13); this assertion is a
    # defense-in-depth guard against a mis-provisioned mapping.
    assert len(envelope.Records) == 1, "batch size 1 enforced by event-source mapping"
    record = envelope.Records[0]

    # STEP 1 — Parse InstallationEventMessage.
    try:
        message = InstallationEventMessage.model_validate_json(record.body)
    except ValidationError as exc:
        # Requirement 11.3: malformed payloads are Terminal. Delete from
        # the queue — a redrive will only re-surface the same shape drift.
        # The error locations are logged (loc + type) but the raw body is
        # NOT — Invariant 6 forbids it via LOGGING_DENYLIST.
        logger.error(
            "malformed_installation_event",
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        return {"batchItemFailures": []}

    # STEP 2 — Attach log context.
    append_lifecycle_context(logger, message=message)

    # STEP 3 — Dispatch by event_type.
    env, iam_prov, ddb = _bootstrap()
    now_iso = _now_iso_utc_ms()

    try:
        match message.event_type:
            case "installation.created":
                handle_installation_created(
                    message,
                    iam_prov=iam_prov,
                    ddb=ddb,
                    env=env,
                    now_iso=now_iso,
                )
            case "installation.deleted":
                handle_installation_deleted(
                    message,
                    iam_prov=iam_prov,
                    ddb=ddb,
                    now_iso=now_iso,
                )
            case "installation_repositories.added":
                handle_repositories_added(
                    message,
                    ddb=ddb,
                    now_iso=now_iso,
                )
            case "installation_repositories.removed":
                handle_repositories_removed(
                    message,
                    ddb=ddb,
                    now_iso=now_iso,
                )
    except Exception as exc:
        # STEP 4 — Requirement 15.3. Any handler exception is Terminal:
        # log the marker and re-raise so SQS returns the message for
        # redrive. maxReceiveCount=3 (configured on the queue in task
        # 13's CDK stack) sends the third failure to the DLQ. The
        # exception class name is logged for triage; the exception body
        # is intentionally not stringified onto the log record because
        # a nested ClientError may embed the full boto3 response in its
        # message, which Invariant 6 forbids.
        logger.error(
            "lifecycle_terminal_failure",
            error_class="terminal",
            exception_class=type(exc).__name__,
        )
        raise

    # STEP 5 — Success.
    return {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# Event-type handlers
# ---------------------------------------------------------------------------


def handle_installation_created(
    message: InstallationEventMessage,
    *,
    iam_prov: IamProvisioner,
    ddb: InstallationsTableWriter,
    env: LifecycleEnvConfig,
    now_iso: str,
) -> None:
    """Provision the per-installation IAM task role and upsert the DDB row.

    Two-step flow per design.md §5.2:

    (a) Render the runtime IAM policy document via the pure
        :func:`render_installation_policy_document` (byte-equivalent
        to CDK synth per §6.5) and the fixed ``sts:AssumeRole`` policy
        via :func:`render_installation_assume_role_policy_document`;
        provision the role through :class:`IamProvisioner`. On
        AWS ``EntityAlreadyExists`` the provisioner returns
        ``ProvisionResult(already_existed=True)`` and the handler
        emits ``installation_already_provisioned`` INFO
        (Requirement 14.2).
    (b) Upsert the ``trikon-cloud-installations`` row through
        :class:`InstallationsTableWriter`. A
        :class:`ConditionalCheckFailedException` folds into
        ``UpsertResult(already_active=True)`` and the handler emits
        ``installation_already_active`` INFO then returns —
        idempotent success on re-delivery (Requirement 14.3).
        Otherwise the handler emits ``installation_provisioned`` INFO.

    Any :class:`botocore.exceptions.ClientError` other than the two
    idempotency codes above propagates unhandled — the outer
    :func:`lambda_handler` classifies as Terminal and re-raises to
    the DLQ.
    """
    logger = get_logger()

    # (a) Render policies (pure — no IO).
    policy = render_installation_policy_document(
        message.installation_id,
        app_private_key_secret_arn=env.trikon_app_private_key_secret_arn,
        account_id=env.trikon_aws_account_id,
        region=env.aws_region,
    )
    assume_role_policy = render_installation_assume_role_policy_document()

    # (b) Provision IAM role — idempotent on already-exists (Req 14.2).
    provision_result = iam_prov.provision(
        message.installation_id,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    if provision_result.already_existed:
        logger.info(
            "installation_already_provisioned",
            role_arn=provision_result.role_arn,
        )

    # (c) Upsert DynamoDB row — idempotent on already-active (Req 14.3).
    upsert_result = ddb.upsert_active(
        message.installation_id,
        github_app_id=message.github_app_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    if upsert_result.already_active:
        logger.info("installation_already_active")
        return

    logger.info("installation_provisioned")


def handle_installation_deleted(
    message: InstallationEventMessage,
    *,
    iam_prov: IamProvisioner,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    """Deprovision the IAM task role and flip the DDB row to ``"disabled"``.

    Two-step flow per design.md §5.3:

    (a) Deprovision the IAM role through :class:`IamProvisioner`.
        Both legs — ``iam:DeleteRolePolicy`` and ``iam:DeleteRole`` —
        fold ``NoSuchEntity`` into ``DeprovisionResult.already_absent``.
        When both legs reported absent, the handler emits
        ``installation_role_already_absent`` INFO (Requirement 14.4).
    (b) Flip the ``trikon-cloud-installations`` row's ``status`` to
        ``"disabled"`` via :meth:`InstallationsTableWriter.mark_disabled`.
        No ``ConditionExpression`` — an update on a missing row
        creates a dormant ``"disabled"`` sentinel, which is the
        intended semantics when ``installation.deleted`` races ahead
        of ``installation.created`` (Requirement 14.4; design.md §5.3).

    The handler always emits ``installation_deprovisioned`` INFO on
    the happy path — matching the user prompt's per-handler log
    marker. (design.md §5.3 shows ``installation_disabled``; the user
    prompt renames it to ``installation_deprovisioned`` for symmetry
    with ``installation_provisioned``.)
    """
    logger = get_logger()

    deprovision_result = iam_prov.deprovision(message.installation_id)
    if deprovision_result.already_absent:
        logger.info(
            "installation_role_already_absent",
            role_name=deprovision_result.role_name,
        )

    ddb.mark_disabled(message.installation_id, now_iso=now_iso)

    logger.info("installation_deprovisioned")


def handle_repositories_added(
    message: InstallationEventMessage,
    *,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    """Atomically add ``message.repositories`` to the row's SS attribute.

    Delegates to :meth:`InstallationsTableWriter.add_repositories`,
    which issues a DynamoDB ``UpdateItem`` with ``ADD`` on the
    ``repositories`` string-set attribute. DynamoDB's set-add is
    naturally idempotent — adding a member already present is a
    no-op (Requirement 14.5; design.md §5.4). No conditional
    expression is required.
    """
    ddb.add_repositories(
        message.installation_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    get_logger().info(
        "repositories_added",
        repo_count=len(message.repositories),
    )


def handle_repositories_removed(
    message: InstallationEventMessage,
    *,
    ddb: InstallationsTableWriter,
    now_iso: str,
) -> None:
    """Atomically remove ``message.repositories`` from the row's SS attribute.

    Delegates to :meth:`InstallationsTableWriter.remove_repositories`,
    which issues a DynamoDB ``UpdateItem`` with ``DELETE`` on the
    ``repositories`` string-set attribute. DynamoDB's set-delete is
    naturally idempotent — deleting a member not present is a no-op
    (Requirement 14.5; design.md §5.4). No conditional expression is
    required.
    """
    ddb.remove_repositories(
        message.installation_id,
        repositories=frozenset(message.repositories),
        now_iso=now_iso,
    )
    get_logger().info(
        "repositories_removed",
        repo_count=len(message.repositories),
    )
