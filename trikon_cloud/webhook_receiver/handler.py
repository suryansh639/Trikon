# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on every
# BaseModel subclass. Under the repo's ``disallow_any_explicit = true`` mypy
# config, each SqsJobMessage / GithubWebhookPayload construction surfaces as an
# ``explicit-any`` error inside the model classes. The error refers to code the
# plugin generates, not code we write — silence it at the file level (same
# rationale documented at the top of ``models.py``).
# mypy: disable-error-code="explicit-any"
"""Lambda handler for the Trikon Cloud GitHub webhook receiver.

Composition layer for Spec 1 of the Trikon Cloud M1 milestone. Imports
the four Wave-2 leaves (:mod:`models`, :mod:`hmac_verifier`,
:mod:`sqs_writer`, :mod:`logger`) and wires them into the single
``POST /webhooks/github`` route exposed via
:class:`aws_lambda_powertools.event_handler.APIGatewayHttpResolver`.

The handler realises the extended 15-path never-fail-open enumeration
in design.md §6.1 (amendment) — an extension of Spec 1's original
13-path tree with two new terminals for successful installation-event
enqueue (``202``) and malformed installation-event payload (``400``).
There is exactly one path to ``200`` (the ``ping`` event short-circuit
that predates HMAC verification per the §7 pin) and exactly two paths
to ``202`` (a successful ``sqs.SendMessage`` on the ``trikon-verify-jobs``
queue for a ``pull_request`` delivery, and a successful send on the
``trikon-cloud-installation-events`` queue for an installation-lifecycle
delivery). Every other code path returns a ``4xx`` or ``5xx`` status.
The routing default for unknown event types is fail-closed: ``204``
rather than ``202``.

Module-level state below (``_env_config``, ``_webhook_secret``,
``_sqs_writer``, ``_installation_events_writer``, ``_secrets_client``)
is lazy-initialised on the first warm invocation and reused across
subsequent invocations to keep the cold-start budget minimal. The design.md §3.1 rationale for module-level
caching applies: the Lambda's cold-start-including-init lands under
1.5 s at 512 MB memory when secret fetch + boto3 client construction
happen exactly once per process lifetime.

Invariant 6 (secrets never enter observability planes) constrains the
log surface: the webhook body is never logged at INFO level, the
webhook secret bytes are never logged at any level, and the raw
``str(ValidationError)`` returned in the ``malformed_payload`` response
passes through the :class:`logger._PiiRedactionFilter` before it
reaches CloudWatch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

import boto3  # type: ignore[import-untyped]
from aws_lambda_powertools.event_handler import (
    APIGatewayHttpResolver,
    Response,
    content_types,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from trikon_cloud.installation_lifecycle.models import InstallationEventMessage
from trikon_cloud.webhook_receiver import hmac_verifier, logger
from trikon_cloud.webhook_receiver.models import (
    GithubInstallationPayload,
    GithubWebhookPayload,
    ReceiverEnvConfig,
    SqsJobMessage,
)
from trikon_cloud.webhook_receiver.sqs_writer import SqsWriteError, SqsWriter

__all__ = ["handler"]


class _SecretsManagerClient(Protocol):
    """Structural type for the subset of the boto3 Secrets Manager client we call.

    ``boto3`` ships without type stubs; this protocol lets ``mypy
    --strict`` type-check the single ``get_secret_value`` call site
    without a runtime dependency on ``boto3-stubs``. We only ever read
    ``SecretString`` off the response, so a ``dict[str, str]`` return
    type is faithful for our use of the API (``SecretBinary`` bytes
    aren't accessed by this receiver).
    """

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        ...


# ---------------------------------------------------------------------------
# Module-level state (lazy-initialised on first warm invocation).
# ---------------------------------------------------------------------------

app: APIGatewayHttpResolver = APIGatewayHttpResolver()

_env_config: ReceiverEnvConfig | None = None
_webhook_secret: bytes | None = None
_sqs_writer: SqsWriter | None = None
_installation_events_writer: SqsWriter | None = None
_secrets_client: _SecretsManagerClient | None = None
_LOG = logger.get_logger()


# Extended router return type — the shipping ``"enqueue"`` literal is renamed
# to ``"enqueue_verify"`` so the two enqueue branches (verify jobs and
# installation events) have symmetric names. See design.md §4.1.
_Route = Literal["enqueue_verify", "enqueue_installation", "pong", "non_enqueue"]


# ---------------------------------------------------------------------------
# Private helpers — memoised loaders and pure routing/serialisation.
# ---------------------------------------------------------------------------


def _load_env_config() -> ReceiverEnvConfig:
    """Return the memoised :class:`ReceiverEnvConfig`, constructing on first call."""
    global _env_config
    if _env_config is None:
        _env_config = ReceiverEnvConfig()
    return _env_config


def _load_webhook_secret() -> bytes:
    """Fetch and cache the webhook secret bytes from AWS Secrets Manager.

    Raises the underlying :class:`botocore.exceptions.BotoCoreError` /
    :class:`botocore.exceptions.ClientError` on failure so the handler
    can map it to the ``500 internal_error`` response body per §7 path
    5. The bytes are cached across warm invocations; a rotation forces
    a cold start (which is the current M1 acceptable trade-off — post-M1
    a scheduled secret-refresh may be added).
    """
    global _webhook_secret, _secrets_client
    if _webhook_secret is None:
        env = _load_env_config()
        if _secrets_client is None:
            _secrets_client = cast(_SecretsManagerClient, boto3.client("secretsmanager"))
        response = _secrets_client.get_secret_value(SecretId=env.webhook_secret_arn)
        _webhook_secret = response["SecretString"].encode("utf-8")
    return _webhook_secret


def _get_sqs_writer() -> SqsWriter:
    """Return the memoised Verify Jobs Queue :class:`SqsWriter`.

    Constructed on first call and reused across warm invocations. The
    identifier ``_sqs_writer`` is deliberately retained (rather than
    renamed to ``_verify_jobs_writer``) so the shipping tests that reach
    into ``handler._sqs_writer`` for spy-based assertions continue to
    pass unchanged.
    """
    global _sqs_writer
    if _sqs_writer is None:
        env = _load_env_config()
        _sqs_writer = SqsWriter(queue_url=env.verify_jobs_queue_url)
    return _sqs_writer


def _get_installation_events_writer() -> SqsWriter:
    """Return the memoised Installation Events Queue :class:`SqsWriter`.

    Mirror of :func:`_get_sqs_writer`. Bound to the Installation Events
    Queue URL supplied via ``ReceiverEnvConfig.installation_events_queue_url``
    (design.md §7.4).
    """
    global _installation_events_writer
    if _installation_events_writer is None:
        env = _load_env_config()
        _installation_events_writer = SqsWriter(
            queue_url=env.installation_events_queue_url
        )
    return _installation_events_writer


def _route_event(event_type: str, action: str | None) -> _Route:
    """Pure routing function over ``X-GitHub-Event`` + ``payload.action``.

    Extends the shipping router (Spec 1 design.md §7) with two
    installation-lifecycle routes:

    * ``ping`` → ``pong`` (unchanged; shipping path 4).
    * ``pull_request`` with action in ``{opened, synchronize}`` →
      ``enqueue_verify`` (unchanged behaviour; shipping paths 11-12).
      Renamed from ``"enqueue"`` to ``"enqueue_verify"``.
    * ``installation`` with action in ``{created, deleted}`` →
      ``enqueue_installation`` (new; Requirement 1.1, 1.2).
    * ``installation_repositories`` with action in ``{added, removed}``
      → ``enqueue_installation`` (new; Requirement 1.3, 1.4).
    * every other combination → ``non_enqueue`` (unchanged fail-closed
      default; shipping paths 8-9, extended by Requirement 1.5).
    """
    if event_type == "ping":
        return "pong"
    if event_type == "pull_request" and action in {"opened", "synchronize"}:
        return "enqueue_verify"
    if event_type == "installation" and action in {"created", "deleted"}:
        return "enqueue_installation"
    if event_type == "installation_repositories" and action in {"added", "removed"}:
        return "enqueue_installation"
    return "non_enqueue"


def _peek_action(body_bytes: bytes) -> str | None:
    """Return ``payload.action`` from raw JSON body, or ``None`` if unreadable.

    Used only by the router to select a branch. Deliberately non-raising:
    if the body is malformed JSON or missing ``action``, the router
    treats the request as ``non_enqueue`` and falls through to 204 (for
    unknown events) or lets the branch-specific parser surface a 400
    (for known events). Keeps the router pure — no exceptions.
    """
    try:
        parsed = json.loads(body_bytes)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    action = parsed.get("action")
    if isinstance(action, str):
        return action
    return None


def _is_json_object(body_bytes: bytes) -> bool:
    """Return ``True`` iff ``body_bytes`` parses as a JSON object.

    Companion to :func:`_peek_action`. When action peek returns ``None``
    the caller cannot tell whether the body is malformed JSON or valid
    JSON with a missing ``action`` — this helper distinguishes them so
    the handler can (a) let malformed bodies for known enqueue-eligible
    event types reach the branch-specific parser for a 400 response
    (Requirement 3.2, shipping test ``test_malformed_json_returns_400``)
    while (b) still returning 204 for well-formed JSON that carries no
    valid action (Requirement 1.5, shipping test
    ``test_installation_event_returns_204``).
    """
    try:
        parsed = json.loads(body_bytes)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict)


def _build_installation_message(
    payload: GithubInstallationPayload,
    event_type: str,
    delivery_id: str,
    sent_at: str,
) -> InstallationEventMessage:
    """Assemble the :class:`InstallationEventMessage` from a validated payload.

    Field mapping per Requirement 2 acceptance criteria 1-11. Pure
    function — no I/O, no globals. Callers (only
    :func:`on_github_webhook`) MUST have filtered the request through
    :func:`_route_event` before invoking this helper; the ``else``
    branch below is a defensive raise that the router keeps unreachable.

    Args:
        payload: The parsed installation payload.
        event_type: Verbatim value of the ``X-GitHub-Event`` header
            (``"installation"`` or ``"installation_repositories"``).
        delivery_id: Verbatim value of the ``X-GitHub-Delivery`` header
            (Requirement 2.9).
        sent_at: The single UTC timestamp captured at handler entry
            (Requirement 2.10).

    Returns:
        A fully-populated :class:`InstallationEventMessage` ready for
        SQS emission.

    Raises:
        ValueError: If the ``(event_type, payload.action)`` pair is
            not one of the four accepted combinations. The router
            filters these out before this helper runs, so a raise
            here indicates a routing bug and MUST fail loudly.
    """
    combined_type = f"{event_type}.{payload.action}"
    repos: tuple[str, ...]
    if combined_type == "installation.created":
        repos = tuple(r.full_name for r in (payload.repositories or ()))
    elif combined_type == "installation.deleted":
        # Requirement 2.6 — deleted events always emit ``()`` regardless
        # of what the payload carries.
        repos = ()
    elif combined_type == "installation_repositories.added":
        repos = tuple(r.full_name for r in (payload.repositories_added or ()))
    elif combined_type == "installation_repositories.removed":
        repos = tuple(r.full_name for r in (payload.repositories_removed or ()))
    else:
        # Unreachable when the router is correct. A raise here surfaces
        # a routing bug loudly rather than emitting a malformed message.
        raise ValueError(
            f"unexpected event_type/action combination: {combined_type!r}"
        )

    return InstallationEventMessage(
        installation_id=payload.installation.id,
        github_app_id=payload.installation.app_id,
        event_type=combined_type,
        repositories=repos,
        sent_at=sent_at,
        delivery_id=delivery_id,
    )


def _build_sqs_message(
    payload: GithubWebhookPayload,
    event_type: str,
    delivery_id: str,
    sent_at: str,
) -> SqsJobMessage:
    """Assemble the memo §5.2 SQS body from the validated webhook payload.

    ``event_type`` on the wire is the dotted form ``pull_request.<action>``
    (Requirement 5.2). ``sent_at`` is the single ISO-8601 UTC timestamp
    captured once at handler entry (Requirement 2.10) with millisecond
    precision and a ``Z`` suffix rather than ``+00:00``.
    """
    del event_type  # header value; we render the dotted form below
    return SqsJobMessage(
        installation_id=payload.installation.id,
        repo_full_name=payload.repository.full_name,
        pr_number=payload.pull_request.number,
        head_sha=payload.pull_request.head.sha,
        base_sha=payload.pull_request.base.sha,
        event_type=f"pull_request.{payload.action}",
        sent_at=sent_at,
        delivery_id=delivery_id,
    )


def _handle_malformed(exc: ValidationError, error_code: str) -> Response[str]:
    """Distinguish malformed JSON from schema violations for a 400 response.

    Shared between the two enqueue branches. When Pydantic v2 surfaces a
    JSON parse failure the first error's ``type`` is ``"json_invalid"``;
    every other error type indicates a schema violation on well-formed
    JSON. The ``error_code`` argument identifies the calling branch and
    doubles as the switch that selects the response shape:

    * ``"malformed_payload"`` (the ``pull_request`` branch) preserves
      Spec 1's shipping response bodies verbatim — ``{"error":
      "malformed_json"}`` with no ``detail`` for the JSON-invalid case,
      and ``{"error": "malformed_payload", "detail": str(exc)}`` for
      schema violations (Requirement 6.1).
    * Any other ``error_code`` (currently only
      ``"malformed_installation_payload"``) emits the design.md §6.3
      shape — a structured JSON body with both ``error`` and ``detail``
      keys on every 400 response, satisfying Requirement 3.1 and 3.2.
    """
    first_error_type = ""
    errors = exc.errors()
    if errors:
        first_error_type = str(errors[0].get("type", ""))
    if first_error_type == "json_invalid":
        _LOG.warning("malformed_json")
        if error_code == "malformed_payload":
            # Shipping-shape 400 body (Requirement 6.1). No ``detail`` key.
            return _json_response(400, {"error": "malformed_json"})
        return _json_response(
            400, {"error": "malformed_json", "detail": "invalid JSON body"}
        )
    _LOG.warning(error_code)
    return _json_response(400, {"error": error_code, "detail": str(exc)})


def _json_response(status_code: int, body: dict[str, str]) -> Response[str]:
    """Build a JSON :class:`Response` with the standard content type."""
    return Response(
        status_code=status_code,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body),
    )


def _no_content_response() -> Response[str]:
    """Build the shared ``204`` response used by non-enqueue paths (§7 paths 8-9)."""
    return Response(
        status_code=204,
        content_type=content_types.APPLICATION_JSON,
        body="",
    )


# ---------------------------------------------------------------------------
# Route handler + Lambda entry point.
# ---------------------------------------------------------------------------


@app.post("/webhooks/github")
def on_github_webhook() -> Response[str]:
    """Handle ``POST /webhooks/github`` per the extended 15-path enumeration.

    Header reads are case-insensitive: API Gateway HTTP API lowercases
    header names by convention, but we normalise defensively so the
    handler works against both raw AWS events and pytest fixtures with
    canonical GitHub-style casing.
    """
    # ---- ONE-SHOT timestamp capture (Requirement 2.10) ----
    # Captured once at handler entry and threaded through both enqueue
    # branches. Downstream helpers do NOT call ``datetime.now`` again.
    sent_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

    raw_headers = app.current_event.headers or {}
    headers = {k.lower(): v for k, v in raw_headers.items()}
    event_type = headers.get("x-github-event", "")
    delivery_id = headers.get("x-github-delivery", "")
    signature_header = headers.get("x-hub-signature-256")

    _LOG.append_keys(delivery_id=delivery_id, event_type=event_type)

    # §7 path 3: missing / empty ``X-GitHub-Event``.
    if event_type == "":
        _LOG.warning("missing_event_header")
        return _json_response(400, {"error": "missing_event_header"})

    # §7 path 4: ``ping`` short-circuits before any secret load or HMAC check.
    if event_type == "ping":
        _LOG.info("ping_received")
        return _json_response(200, {"status": "pong"})

    # §7 path 5: Secrets Manager read failure → 500.
    try:
        secret = _load_webhook_secret()
    except (ClientError, BotoCoreError):
        _LOG.exception("secrets_manager_read_failed")
        return _json_response(500, {"error": "internal_error"})

    decoded_body = app.current_event.decoded_body
    body_bytes = decoded_body.encode("utf-8") if decoded_body is not None else b""

    # §7 paths 6-7: missing / malformed / mismatched HMAC signature.
    if not hmac_verifier.verify_signature(body_bytes, signature_header, secret):
        _LOG.warning("hmac_verification_failed")
        return _json_response(401, {"error": "hmac_verification_failed"})

    # ---- Router (extended in §4) ----
    # ``_peek_action`` is a non-raising JSON peek so a malformed body
    # cannot crash the router; malformed-body responses are surfaced by
    # the branch-specific parser below.
    payload_action = _peek_action(body_bytes)
    route = _route_event(event_type, payload_action)

    # §7 path 9 (extended semantics — installation events removed from
    # this set and routed to paths 13-15 below). Fallback: for known
    # enqueue-eligible event types with a body that isn't a JSON object
    # (either non-JSON or a JSON scalar/array), force entry to the
    # matching enqueue branch so the branch parser can surface a 400 —
    # preserves Spec 1's ``test_malformed_json_returns_400`` behaviour
    # (Requirement 6.1) and satisfies Requirement 3.2 for installation
    # events. Well-formed JSON without a valid action still routes to
    # 204 (shipping ``test_installation_event_returns_204``, Requirement 1.5).
    if route == "non_enqueue":
        if not _is_json_object(body_bytes):
            if event_type == "pull_request":
                route = "enqueue_verify"
            elif event_type in {"installation", "installation_repositories"}:
                route = "enqueue_installation"
        if route == "non_enqueue":
            _LOG.info("non_enqueue_event")
            return _no_content_response()

    # ---- pull_request branch (§7 paths 8, 10, 11, 12 — behaviour unchanged) ----
    if route == "enqueue_verify":
        try:
            pr_payload = GithubWebhookPayload.model_validate_json(body_bytes)
        except ValidationError as exc:
            return _handle_malformed(exc, "malformed_payload")

        _LOG.append_keys(
            installation_id=pr_payload.installation.id,
            repo_full_name=pr_payload.repository.full_name,
            pr_number=pr_payload.pull_request.number,
        )

        pr_message = _build_sqs_message(pr_payload, event_type, delivery_id, sent_at)
        verify_writer = _get_sqs_writer()
        try:
            verify_writer.send_job(pr_message)
        except SqsWriteError:
            _LOG.exception("enqueue_failed")
            return _json_response(502, {"error": "enqueue_failed"})

        _LOG.info("accepted")
        return _json_response(
            202, {"status": "accepted", "delivery_id": delivery_id}
        )

    # ---- installation branch (NEW §7 paths 13, 14, 15) ----
    # Router only returns ``"enqueue_installation"`` here — ``"pong"`` is
    # handled by the ping short-circuit above.
    assert route == "enqueue_installation"

    # §7 path 13: missing / empty ``X-GitHub-Delivery`` header (Requirement 3.3).
    if delivery_id == "":
        _LOG.warning("missing_delivery_id")
        return _json_response(
            400,
            {
                "error": "missing_delivery_id",
                "detail": "X-GitHub-Delivery header is required for installation events",
            },
        )

    # §7 path 14: installation payload parsing (Requirement 3.1, 3.2).
    try:
        installation_payload = GithubInstallationPayload.model_validate_json(
            body_bytes
        )
    except ValidationError as exc:
        return _handle_malformed(exc, "malformed_installation_payload")

    _LOG.append_keys(
        installation_id=installation_payload.installation.id,
        github_app_id=installation_payload.installation.app_id,
    )

    # §7 path 15: build + enqueue on the Installation Events Queue.
    installation_message = _build_installation_message(
        installation_payload, event_type, delivery_id, sent_at
    )
    installation_writer = _get_installation_events_writer()
    try:
        installation_writer.send_installation_event(installation_message)
    except SqsWriteError:
        _LOG.exception("enqueue_failed")
        return _json_response(502, {"error": "enqueue_failed"})

    _LOG.info("installation_event_accepted")
    return _json_response(
        202, {"status": "accepted", "delivery_id": delivery_id}
    )


def handler(event: dict[str, object], context: LambdaContext) -> dict[str, object]:
    """AWS Lambda entry point. Delegates to the powertools resolver."""
    return app.resolve(event, context)
