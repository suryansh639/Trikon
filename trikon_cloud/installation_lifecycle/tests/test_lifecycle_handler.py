# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass, and ``boto3.client(...)`` surfaces
# its methods as ``dict[str, Any]`` at the moto boundary. Under the
# repo's ``disallow_any_explicit = true`` mypy config, each such surface
# fires an ``explicit-any`` error on library-generated / library-boundary
# code, not on hand-written signatures. Suppress the check at file scope
# — this is a test module and every ``Any`` here is bounded to the moto /
# pydantic / MagicMock / Powertools-JSON boundary. Mirrors the pattern
# used by sibling ``test_dynamodb_writer.py`` and ``test_iam_provisioner.py``.
# mypy: disable-error-code="explicit-any"
"""End-to-end tests for :mod:`trikon_cloud.installation_lifecycle.handler`.

Feature: trikon-cloud-orchestrator.
Covers the five-step flow (design.md §5.1), plus:

* Property 9 (Event-type routing is a total function on the four
  allowed values) — parametrized totality test walking each of
  ``installation.created``, ``installation.deleted``,
  ``installation_repositories.added``, and
  ``installation_repositories.removed`` and asserting the resulting
  IAM + DDB state matches the intended handler's effect
  (design.md §5.2 through §5.4).
* Property 11 (Lifecycle idempotency on re-delivery) — two dedicated
  tests: a duplicate ``installation.created`` and an
  ``installation.deleted`` for an installation that was never
  provisioned (design.md §5.5 idempotency table).

All tests exercise :func:`lambda_handler` through the real SQS event
envelope shape (:class:`trikon_cloud.orchestrator.models.SqsEventEnvelope`)
so the batch-size-1 guard, malformed-body branch, log-context
attachment, and terminal-failure classifier are all covered against
the same input model the AWS Lambda runtime feeds the function
in production (design.md §5.1 STEP 1).

Boundary tools used:

* :func:`moto.mock_aws` — moto v5 unified decorator; wraps each test in
  a fresh IAM + DynamoDB control plane.
* An autouse fixture patches the four handler cold-start caches
  (``_ENV``, ``_BOTO_SESSION``, ``_IAM_PROV``, ``_DDB_WRITER``) back to
  ``None`` on every test entry so bootstrap re-runs against the
  moto-backed clients of the current test.
* The :func:`log_stream` fixture rebinds the Powertools logger's
  :class:`logging.StreamHandler` to :class:`io.StringIO` so JSON log
  records emitted from the handler can be parsed and asserted on
  directly. Mirrors the pattern established by
  ``trikon_cloud/orchestrator/tests/test_logger.py``.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import IO, Any, Literal
from unittest.mock import MagicMock

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.installation_lifecycle import handler
from trikon_cloud.installation_lifecycle.dynamodb_writer import (
    InstallationsTableWriter,
)
from trikon_cloud.installation_lifecycle.iam_provisioner import IamProvisioner
from trikon_cloud.installation_lifecycle.models import (
    InstallationEventMessage,
    LifecycleEnvConfig,
)
from trikon_cloud.installation_lifecycle.tests.conftest import (
    CANONICAL_DELIVERY_ID,
    CANONICAL_GITHUB_APP_ID,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_SENT_AT,
    make_installation_event_message,
)

# ---------------------------------------------------------------------------
# Module-level fixture constants.
# ---------------------------------------------------------------------------

_TABLE_NAME: str = "trikon-cloud-installations"
_REGION: str = "us-east-1"
_EXPECTED_ROLE_NAME: str = f"trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
_EXPECTED_INLINE_POLICY_NAME: str = "TrikonInstallationPolicy"
_SECOND_REPO: str = "octocat/second-repo"

# The exhaustive :data:`typing.Literal` of the four ``event_type`` values
# the Lifecycle_Handler routes on. Pinned here so the totality-test
# parametrization matches the field's Literal declaration byte-for-byte;
# adding a fifth value here without adding the corresponding route in
# ``handler.py`` would fail mypy strict at the pattern-match site.
_EVENT_TYPE_LITERAL = Literal[
    "installation.created",
    "installation.deleted",
    "installation_repositories.added",
    "installation_repositories.removed",
]


# ---------------------------------------------------------------------------
# Autouse fixture — reset handler module caches between tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_handler_module_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the four cold-start singletons before each test entry.

    :func:`handler._bootstrap` lazily initializes ``_ENV``,
    ``_BOTO_SESSION``, ``_IAM_PROV``, and ``_DDB_WRITER`` on the first
    call and reuses them on every warm invocation. Under pytest, one
    test's cached moto client would leak into the next test's fresh
    moto context and hit "resource already exists" / "credential
    mismatch" surprises. Setting all four to ``None`` here forces
    every test to re-bootstrap against its own moto-backed clients.

    Uses :meth:`pytest.MonkeyPatch.setattr` (not raw assignment) so the
    prior values are restored on teardown, keeping the module's state
    hermetic across the whole session.
    """
    monkeypatch.setattr(handler, "_ENV", None)
    monkeypatch.setattr(handler, "_BOTO_SESSION", None)
    monkeypatch.setattr(handler, "_IAM_PROV", None)
    monkeypatch.setattr(handler, "_DDB_WRITER", None)


# ---------------------------------------------------------------------------
# log_stream fixture — capture Powertools JSON output for assertion.
# ---------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Rebind the Powertools logger's :class:`StreamHandler` to :class:`StringIO`.

    Powertools :class:`Logger(service="trikon-cloud-installation-lifecycle")`
    wraps a stdlib :class:`logging.Logger` registered under the service
    name; a :class:`logging.StreamHandler` is attached at Logger
    construction with a JSON formatter and ``stream=sys.stdout``.
    Swapping the handler's stream to :class:`io.StringIO` lets tests
    parse the exact JSON emitted for a log call independent of
    pytest's own stdout-capture mode. The original stream + level are
    restored on teardown so tests that follow this one see the handler
    configured as at import time.

    Mirrors the pattern established by
    ``trikon_cloud/orchestrator/tests/test_logger.py``.
    """
    buf = io.StringIO()
    stdlib_logger = logging.getLogger("trikon-cloud-installation-lifecycle")
    original_level = stdlib_logger.level
    original_streams: list[tuple[logging.StreamHandler[IO[str]], IO[str]]] = []
    for h in stdlib_logger.handlers:
        if isinstance(h, logging.StreamHandler):
            original_streams.append((h, h.stream))
            h.setStream(buf)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        for h, stream in original_streams:
            h.setStream(stream)
        stdlib_logger.setLevel(original_level)


# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------


def _setup_moto_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the AWS credential triple moto's boto3 shim looks for.

    The autouse ``_env_setup`` fixture in ``conftest.py`` sets the three
    Trikon aliases + ``AWS_REGION`` but not the credential triple.
    Setting them per test with dummy ``"testing"`` values matches the
    pattern in ``test_iam_provisioner.py`` and guarantees moto activates
    cleanly regardless of the developer's host env.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)


def _create_installations_table(ddb_client: Any) -> None:
    """Create the ``trikon-cloud-installations`` table per design.md §2.2.

    Single-attribute partition key ``installation_id`` of DynamoDB type
    ``N`` (Number), ``PAY_PER_REQUEST`` billing mode. Mirrors the
    schema written by the sibling ``test_dynamodb_writer.py`` helper
    so tests across both modules read from an identical shape.
    """
    ddb_client.create_table(
        TableName=_TABLE_NAME,
        KeySchema=[{"AttributeName": "installation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "installation_id", "AttributeType": "N"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _wrap_sqs(body_json: str, *, message_id: str = "msg-1") -> dict[str, Any]:
    """Wrap a raw JSON body into a single-record SQS event envelope.

    Batch size 1 (design.md §5.1 STEP 1 / Requirement 10.1) is fixed
    by the event-source mapping in production; this test-time helper
    mirrors the shape. The ``attributes`` object carries the one AWS
    attribute :class:`SqsRecordAttributes` reads (``ApproximateReceiveCount``);
    the rest of the AWS-added attribute keys pass through via
    ``extra="allow"`` on that model.
    """
    return {
        "Records": [
            {
                "messageId": message_id,
                "receiptHandle": "handle-1",
                "body": body_json,
                "attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }


def _wrap_message(message: InstallationEventMessage) -> dict[str, Any]:
    """Serialize an :class:`InstallationEventMessage` into an SQS event envelope."""
    return _wrap_sqs(message.model_dump_json())


def _make_lambda_context() -> Any:
    """Return a stand-in ``LambdaContext`` sufficient for the handler.

    ``handler.lambda_handler`` accepts ``context`` for signature
    compatibility but discards it via ``del context`` on entry — the
    correlation happens through ``delivery_id`` (design.md §5.1). A
    :class:`unittest.mock.MagicMock` with a few common attributes is
    enough for any downstream code that inadvertently touches the
    object.
    """
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id"
    ctx.function_name = "trikon-cloud-installation-lifecycle-test"
    ctx.invoked_function_arn = (
        "arn:aws:lambda:us-east-1:000000000000:function:test"
    )
    return ctx


def _get_ddb_item(ddb_client: Any, installation_id: int) -> dict[str, Any]:
    """Read the row for ``installation_id`` through the raw DynamoDB client.

    Raises :class:`KeyError` if the row is absent (louder than boto3's
    default behavior of silently returning a response without an
    ``"Item"`` key). Callers assert on the wire shape (``{"S":…, "N":…,
    "SS":…}``) directly.
    """
    response = ddb_client.get_item(
        TableName=_TABLE_NAME,
        Key={"installation_id": {"N": str(installation_id)}},
    )
    item: dict[str, Any] = response["Item"]
    return item


def _read_log_records(buf: io.StringIO) -> list[dict[str, Any]]:
    """Parse the buffer's contents as one JSON record per non-empty line."""
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _iam_role_exists(iam_client: Any, role_name: str) -> bool:
    """Return ``True`` iff ``iam:GetRole`` succeeds for ``role_name``."""
    try:
        iam_client.get_role(RoleName=role_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchEntity":
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# Happy path — installation.created.
# ---------------------------------------------------------------------------


@mock_aws()
def test_installation_created_happy_path_provisions_iam_and_ddb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``installation.created`` → IAM role + DDB row + empty batch response.

    Design.md §5.2 two-step flow: (a) provision the per-installation
    IAM task role with a fixed ``AssumeRolePolicyDocument`` and the
    rendered inline policy; (b) upsert the ``trikon-cloud-installations``
    row with ``status="active"``. Assertions verify the wire-level
    effects of both steps: the role exists under the deterministic
    ``trikon-verify-task-role-{installation_id}`` name (design.md §6.4)
    with the ``installation_id`` tag; the inline policy is present under
    the fixed name ``"TrikonInstallationPolicy"`` (design.md §5.3); and
    the DDB row carries ``status="active"`` with the passed repository
    set. The handler returns ``{"batchItemFailures": []}`` so SQS
    deletes the record from the queue (design.md §5.1 STEP 5).
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    iam = boto3.client("iam", region_name=_REGION)
    _create_installations_table(ddb)

    message = make_installation_event_message(event_type="installation.created")
    event = _wrap_message(message)

    result = handler.lambda_handler(event, _make_lambda_context())

    assert result == {"batchItemFailures": []}

    # (a) IAM role provisioned with tag + fixed inline policy name.
    role_response = iam.get_role(RoleName=_EXPECTED_ROLE_NAME)
    assert role_response["Role"]["RoleName"] == _EXPECTED_ROLE_NAME
    tags = {t["Key"]: t["Value"] for t in role_response["Role"].get("Tags", [])}
    assert tags.get("installation_id") == str(CANONICAL_INSTALLATION_ID)
    inline_policies = iam.list_role_policies(RoleName=_EXPECTED_ROLE_NAME)
    assert _EXPECTED_INLINE_POLICY_NAME in inline_policies["PolicyNames"]

    # (b) DDB row upserted with status="active" and the full data set.
    item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
    assert item["status"] == {"S": "active"}
    assert item["github_app_id"] == {"N": str(CANONICAL_GITHUB_APP_ID)}
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME}


# ---------------------------------------------------------------------------
# Happy path — installation.deleted.
# ---------------------------------------------------------------------------


@mock_aws()
def test_installation_deleted_happy_path_deprovisions_iam_and_marks_ddb_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``installation.deleted`` → IAM role gone + DDB row status ``"disabled"``.

    Design.md §5.3 two-step flow: (a) deprovision the IAM role
    (``iam:DeleteRolePolicy`` before ``iam:DeleteRole`` — the reverse
    order fails with ``DeleteConflict``); (b) flip the DDB row's
    ``status`` to ``"disabled"``. Setup first invokes
    ``installation.created`` against the same installation id so both
    delete legs have a target to remove — the deprovision path against
    a pre-existing role is a genuine happy path (not the Property 11
    idempotency branch, which is exercised by a dedicated test below).
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    iam = boto3.client("iam", region_name=_REGION)
    _create_installations_table(ddb)

    # Seed: create the IAM role + active DDB row that .deleted will tear down.
    seed = make_installation_event_message(event_type="installation.created")
    assert handler.lambda_handler(_wrap_message(seed), _make_lambda_context()) == {
        "batchItemFailures": []
    }
    assert _iam_role_exists(iam, _EXPECTED_ROLE_NAME)

    # Now the actual .deleted invocation.
    message = make_installation_event_message(event_type="installation.deleted")
    result = handler.lambda_handler(_wrap_message(message), _make_lambda_context())

    assert result == {"batchItemFailures": []}
    assert not _iam_role_exists(iam, _EXPECTED_ROLE_NAME)
    item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
    assert item["status"] == {"S": "disabled"}


# ---------------------------------------------------------------------------
# Happy path — installation_repositories.added.
# ---------------------------------------------------------------------------


@mock_aws()
def test_repositories_added_happy_path_grows_ddb_repositories_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``installation_repositories.added`` → row's SS attribute grows.

    Design.md §5.4: the handler issues one DynamoDB ``UpdateItem`` with
    ``ADD repositories :repos``. DynamoDB set-union semantics — adding
    a new member surfaces on the wire as a strictly-larger set. IAM is
    NOT touched by this handler; only DDB. Setup first upserts an
    active row via ``installation.created`` so the ``ADD`` operation
    has a baseline to grow from.
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    _create_installations_table(ddb)

    seed = make_installation_event_message(event_type="installation.created")
    handler.lambda_handler(_wrap_message(seed), _make_lambda_context())

    added = make_installation_event_message(
        event_type="installation_repositories.added",
        repositories=(_SECOND_REPO,),
    )
    result = handler.lambda_handler(_wrap_message(added), _make_lambda_context())

    assert result == {"batchItemFailures": []}
    item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
    assert set(item["repositories"]["SS"]) == {CANONICAL_REPO_FULL_NAME, _SECOND_REPO}


# ---------------------------------------------------------------------------
# Happy path — installation_repositories.removed.
# ---------------------------------------------------------------------------


@mock_aws()
def test_repositories_removed_happy_path_shrinks_ddb_repositories_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``installation_repositories.removed`` → row's SS attribute shrinks.

    Design.md §5.4: the handler issues one DynamoDB ``UpdateItem`` with
    ``DELETE repositories :repos``. Setup upserts a two-member SS via
    a seed ``installation.created`` so the deletion removes exactly
    one entry, leaving the other member behind — this both exercises
    the shrink and avoids DynamoDB's empty-set attribute-removal edge
    (an SS with zero members is dropped from the row entirely).
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    _create_installations_table(ddb)

    seed = make_installation_event_message(
        event_type="installation.created",
        repositories=(CANONICAL_REPO_FULL_NAME, _SECOND_REPO),
    )
    handler.lambda_handler(_wrap_message(seed), _make_lambda_context())

    removed = make_installation_event_message(
        event_type="installation_repositories.removed",
        repositories=(CANONICAL_REPO_FULL_NAME,),
    )
    result = handler.lambda_handler(_wrap_message(removed), _make_lambda_context())

    assert result == {"batchItemFailures": []}
    item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
    assert set(item["repositories"]["SS"]) == {_SECOND_REPO}


# ---------------------------------------------------------------------------
# Step 1 — malformed body (Requirement 11.3).
# ---------------------------------------------------------------------------


def test_step1_malformed_body_returns_empty_batch_and_logs_error(
    log_stream: io.StringIO,
) -> None:
    """Requirement 11.3: SQS body missing a required field → empty batch + ERROR log.

    Design.md §5.1 STEP 1: a body that fails
    :meth:`InstallationEventMessage.model_validate_json` is Terminal.
    The handler logs ``malformed_installation_event`` at ERROR (carrying
    the pydantic error locations under ``errors`` but never the raw
    body — Invariant 6 forbids the raw payload as a log key) and
    returns ``{"batchItemFailures": []}`` so SQS deletes the record
    from the queue rather than redriving. A shape drift from Spec 1's
    writer will not resolve on retry, so retrying would just re-log the
    same failure until ``maxReceiveCount``.
    """
    # Deliberately omit ``installation_id`` — a required field on
    # :class:`InstallationEventMessage`. Every other field is well-formed
    # so the failure isolates to STEP 1's pydantic validator.
    bad_body = json.dumps(
        {
            "github_app_id": CANONICAL_GITHUB_APP_ID,
            "event_type": "installation.created",
            "repositories": [CANONICAL_REPO_FULL_NAME],
            "sent_at": CANONICAL_SENT_AT,
            "delivery_id": CANONICAL_DELIVERY_ID,
        }
    )
    event = _wrap_sqs(bad_body)

    result = handler.lambda_handler(event, _make_lambda_context())

    assert result == {"batchItemFailures": []}

    records = _read_log_records(log_stream)
    malformed_records = [
        r for r in records if r.get("message") == "malformed_installation_event"
    ]
    assert len(malformed_records) == 1, (
        f"expected exactly one malformed_installation_event log, got: {records!r}"
    )
    rec = malformed_records[0]
    assert rec["level"] == "ERROR"
    # ``errors`` carries the pydantic error locations (loc + type) but
    # NOT the raw body — Invariant 6 via LOGGING_DENYLIST. Just assert
    # the key is present and shaped like a list of dicts.
    assert isinstance(rec.get("errors"), list)
    assert all(isinstance(e, dict) for e in rec["errors"])


# ---------------------------------------------------------------------------
# Step 2 — log context propagation (Requirement 15.2).
# ---------------------------------------------------------------------------


@mock_aws()
def test_step2_log_records_carry_natural_key_context_after_parse(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """Requirement 15.2: after parse, log records carry the three natural keys.

    Design.md §5.1 STEP 2:
    :func:`~trikon_cloud.installation_lifecycle.logger.append_lifecycle_context`
    fires immediately after the inbound
    :class:`InstallationEventMessage` validates. Every subsequent log
    record within the same invocation must then carry
    ``installation_id``, ``event_type``, and ``delivery_id`` byte-for-byte
    from the parsed message. The ``installation_provisioned`` INFO
    emitted by ``handle_installation_created`` at the tail of the
    happy path is the natural witness: it is emitted after
    ``append_lifecycle_context`` fired and after the DDB upsert
    succeeded.
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    _create_installations_table(ddb)

    message = make_installation_event_message(event_type="installation.created")
    handler.lambda_handler(_wrap_message(message), _make_lambda_context())

    records = _read_log_records(log_stream)
    # Filter to INFO records — those are the handler-emitted markers
    # that fire after ``append_lifecycle_context``.
    info_records = [r for r in records if r.get("level") == "INFO"]
    assert info_records, (
        f"expected at least one INFO log record after parse, got: {records!r}"
    )
    for rec in info_records:
        assert rec.get("installation_id") == CANONICAL_INSTALLATION_ID
        assert rec.get("event_type") == "installation.created"
        assert rec.get("delivery_id") == CANONICAL_DELIVERY_ID


# ---------------------------------------------------------------------------
# Step 3 — Property 9: event-type routing is a total function.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "installation.created",
        "installation.deleted",
        "installation_repositories.added",
        "installation_repositories.removed",
    ],
)
@mock_aws()
def test_property_9_event_type_routing_is_a_total_function(
    monkeypatch: pytest.MonkeyPatch,
    event_type: _EVENT_TYPE_LITERAL,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 9: Event-type routing.

    Design.md §5.1 STEP 3 dispatches on ``event_type`` via a
    ``match`` statement against the four Literal values declared on
    :class:`InstallationEventMessage.event_type`. Property 9 says
    the dispatch is a total function — every allowed value routes,
    and each routes to a distinct handler.

    Rather than instrumenting the handler with call-site spies, this
    test asserts on the observable state after invocation: an
    ``installation.created`` produces an IAM role + active DDB row;
    an ``installation.deleted`` removes the role + flips the DDB row
    to ``"disabled"``; an ``installation_repositories.added`` grows the
    DDB SS without touching IAM; and an
    ``installation_repositories.removed`` shrinks the DDB SS without
    touching IAM. Because each terminal state is uniquely produced
    by exactly one handler, an observable state match proves the
    dispatch fired the intended branch and no other.
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    iam = boto3.client("iam", region_name=_REGION)
    _create_installations_table(ddb)

    # Seed for every event_type EXCEPT installation.created: invoke a
    # synthetic .created first so there's an IAM role + active row for
    # the subsequent .deleted / .added / .removed to operate on. For
    # .removed the seed carries two SS members so the delete has a
    # meaningful shrink target that avoids the empty-set edge.
    if event_type != "installation.created":
        seed_repos = (
            (CANONICAL_REPO_FULL_NAME, _SECOND_REPO)
            if event_type == "installation_repositories.removed"
            else (CANONICAL_REPO_FULL_NAME,)
        )
        seed = make_installation_event_message(
            event_type="installation.created",
            repositories=seed_repos,
        )
        seed_result = handler.lambda_handler(
            _wrap_message(seed), _make_lambda_context()
        )
        assert seed_result == {"batchItemFailures": []}

    # Build the message for the parametrized event_type. For .added
    # the payload's ``repositories`` carries the second-repo delta so
    # the ``ADD`` operation surfaces a strictly-larger set; every other
    # branch carries the canonical single-entry tuple (for .removed
    # that's the repo removed from the two-member seed; for .created
    # and .deleted the value is irrelevant to the assertions).
    delta_repos: tuple[str, ...] = (
        (_SECOND_REPO,)
        if event_type == "installation_repositories.added"
        else (CANONICAL_REPO_FULL_NAME,)
    )
    message = make_installation_event_message(
        event_type=event_type,
        repositories=delta_repos,
    )
    result = handler.lambda_handler(_wrap_message(message), _make_lambda_context())
    assert result == {"batchItemFailures": []}

    # Per-event_type state assertions — each terminal state is uniquely
    # produced by exactly one handler (design.md §5.5 idempotency table
    # + §5.2 through §5.4 flow diagrams).
    if event_type == "installation.created":
        assert _iam_role_exists(iam, _EXPECTED_ROLE_NAME)
        item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
        assert item["status"] == {"S": "active"}
    elif event_type == "installation.deleted":
        assert not _iam_role_exists(iam, _EXPECTED_ROLE_NAME)
        item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
        assert item["status"] == {"S": "disabled"}
    elif event_type == "installation_repositories.added":
        # IAM role from the seed is untouched — the .added handler
        # takes no IAM path.
        assert _iam_role_exists(iam, _EXPECTED_ROLE_NAME)
        item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
        assert set(item["repositories"]["SS"]) == {
            CANONICAL_REPO_FULL_NAME,
            _SECOND_REPO,
        }
    else:  # installation_repositories.removed
        assert _iam_role_exists(iam, _EXPECTED_ROLE_NAME)
        item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
        assert set(item["repositories"]["SS"]) == {_SECOND_REPO}


# ---------------------------------------------------------------------------
# Step 4 — Terminal failure (Requirement 15.3).
# ---------------------------------------------------------------------------


def test_step4_terminal_failure_reraises_with_marker_log_record(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """Requirement 15.3: a handler exception → ``lifecycle_terminal_failure`` ERROR + re-raise.

    Design.md §5.1 STEP 4: any exception raised by an event-type
    handler is caught, logged at ERROR as ``lifecycle_terminal_failure``
    with ``error_class="terminal"`` and ``exception_class=<type>``, and
    re-raised so SQS returns the message to the queue. After
    ``maxReceiveCount=3`` the DLQ absorbs it.

    We force the failure by pre-populating the handler's cold-start
    caches with a broken IAM client (``create_role`` raises
    ``AccessDenied``) and a spy DDB writer whose ``put_item`` /
    ``update_item`` we assert was never invoked. This proves the
    "half-configured state avoided" property: the DDB row must NOT
    be written when the preceding IAM leg fails, so a retry can start
    from a clean slate.

    The broken IAM client is a :class:`MagicMock` rather than moto —
    moto's IAM doesn't have a way to force an ``AccessDenied`` without
    real IAM policy plumbing, and the point of this test is the
    handler's classifier, not the IAM service semantics.
    """
    _setup_moto_credentials(monkeypatch)

    broken_iam_client = MagicMock()
    broken_iam_client.create_role.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied by policy"}},
        "CreateRole",
    )
    spy_ddb_client = MagicMock()

    # Pre-populate the four caches so :func:`handler._bootstrap`
    # returns our injected pair unchanged. The autouse
    # ``_reset_handler_module_caches`` fixture set all four to ``None``
    # a moment ago; ``monkeypatch.setattr`` is idempotent so these
    # override cleanly.
    monkeypatch.setattr(handler, "_ENV", LifecycleEnvConfig())
    monkeypatch.setattr(handler, "_BOTO_SESSION", MagicMock())
    monkeypatch.setattr(
        handler, "_IAM_PROV", IamProvisioner(iam_client=broken_iam_client)
    )
    monkeypatch.setattr(
        handler,
        "_DDB_WRITER",
        InstallationsTableWriter(
            ddb_client=spy_ddb_client, table_name=_TABLE_NAME
        ),
    )

    message = make_installation_event_message(event_type="installation.created")
    event = _wrap_message(message)

    with pytest.raises(ClientError) as exc_info:
        handler.lambda_handler(event, _make_lambda_context())
    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

    # No DynamoDB write occurred — the IAM leg failed before the DDB
    # upsert had a chance to run. This is the "half-configured state
    # avoided" guarantee.
    spy_ddb_client.put_item.assert_not_called()
    spy_ddb_client.update_item.assert_not_called()

    records = _read_log_records(log_stream)
    terminal_records = [
        r for r in records if r.get("message") == "lifecycle_terminal_failure"
    ]
    assert len(terminal_records) == 1, (
        f"expected exactly one lifecycle_terminal_failure record, got: {records!r}"
    )
    rec = terminal_records[0]
    assert rec["level"] == "ERROR"
    assert rec["error_class"] == "terminal"
    assert rec["exception_class"] == "ClientError"


# ---------------------------------------------------------------------------
# Property 11 — installation.created redelivery is idempotent.
# ---------------------------------------------------------------------------


@mock_aws()
def test_property_11_installation_created_redelivery_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency on re-delivery.

    Requirement 14.2 + 14.3: a duplicate ``installation.created``
    delivery for the same installation must succeed without error and
    must NOT re-mutate the DDB row's ``created_at`` timestamp. The
    two SDK-side folds fire in tandem: the IAM ``EntityAlreadyExists``
    from ``iam:CreateRole`` folds into ``already_existed=True`` and
    the handler emits ``installation_already_provisioned`` INFO; the
    DynamoDB ``ConditionalCheckFailedException`` from the ``PutItem``
    conditional folds into ``already_active=True`` and the handler
    emits ``installation_already_active`` INFO.

    The buffer is truncated between invocations so the second-call
    log records isolate from the first-call markers — otherwise a
    ``installation_provisioned`` (first call) would coexist with a
    ``installation_already_provisioned`` (second call) in the same
    parse and the ``in`` check would be ambiguous.
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    _create_installations_table(ddb)

    message = make_installation_event_message(event_type="installation.created")
    event = _wrap_message(message)

    # First delivery — genuine create.
    first = handler.lambda_handler(event, _make_lambda_context())
    assert first == {"batchItemFailures": []}

    # Isolate the second delivery's log records so we can assert on
    # exactly what the redelivery path emitted.
    log_stream.seek(0)
    log_stream.truncate(0)

    # Second delivery — identical message, MUST NOT raise, MUST log
    # both idempotency markers.
    second = handler.lambda_handler(event, _make_lambda_context())
    assert second == {"batchItemFailures": []}

    records = _read_log_records(log_stream)
    messages = [r.get("message") for r in records]
    assert "installation_already_provisioned" in messages, (
        f"expected installation_already_provisioned in second-call logs, got {messages!r}"
    )
    assert "installation_already_active" in messages, (
        f"expected installation_already_active in second-call logs, got {messages!r}"
    )


# ---------------------------------------------------------------------------
# Property 11 — installation.deleted for never-provisioned is idempotent.
# ---------------------------------------------------------------------------


@mock_aws()
def test_property_11_installation_deleted_for_never_provisioned_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency on re-delivery.

    Requirement 14.4: an ``installation.deleted`` event that arrives
    for an installation which was never provisioned must complete
    without error. Both IAM delete legs (``iam:DeleteRolePolicy``
    followed by ``iam:DeleteRole``) fold ``NoSuchEntity`` into
    ``already_absent=True``; when both fold, the handler emits
    ``installation_role_already_absent`` INFO. The DDB path has no
    ``ConditionExpression`` on ``mark_disabled``, so an update on a
    missing key creates a dormant ``"disabled"`` sentinel row
    carrying exactly three attributes — ``installation_id``, ``status``,
    ``updated_at`` — which is the intended audit trail for a delete
    that races ahead of a create (design.md §5.3).
    """
    _setup_moto_credentials(monkeypatch)
    ddb = boto3.client("dynamodb", region_name=_REGION)
    _create_installations_table(ddb)

    message = make_installation_event_message(event_type="installation.deleted")
    result = handler.lambda_handler(_wrap_message(message), _make_lambda_context())

    assert result == {"batchItemFailures": []}

    records = _read_log_records(log_stream)
    messages = [r.get("message") for r in records]
    assert "installation_role_already_absent" in messages, (
        f"expected installation_role_already_absent in logs, got {messages!r}"
    )

    # DDB sentinel row created with exactly three attributes.
    item = _get_ddb_item(ddb, CANONICAL_INSTALLATION_ID)
    assert set(item.keys()) == {"installation_id", "status", "updated_at"}
    assert item["status"] == {"S": "disabled"}
