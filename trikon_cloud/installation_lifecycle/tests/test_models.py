# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass. Under the repo's
# ``disallow_any_explicit = true`` mypy config, constructing those models
# in test code surfaces as an ``explicit-any`` error the plugin
# generates, not code we write. Silence at file scope — test module.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.installation_lifecycle.models`.

Covers the three public Pydantic models exported by the sibling
package:

* :class:`InstallationEventMessage` — the SQS body written to
  ``trikon-cloud-installation-events`` by Spec 1's webhook receiver.
  Tests exercise the ``extra="forbid"`` and ``frozen=True`` config,
  the ``event_type`` ``Literal`` gate, the ``ge=1`` ``Field`` bound on
  the two identifier fields, and a JSON round-trip that preserves
  every declared field byte-for-byte.
* :class:`LifecycleTableRow` — the ``trikon-cloud-installations``
  DynamoDB row shape. Tests exercise ``frozen=True`` and the
  ``status`` ``Literal["active", "disabled"]`` gate.
* :class:`LifecycleEnvConfig` — the Lambda environment-variable
  contract. Tests exercise env-driven construction via the autouse
  ``_env_setup`` fixture, the :class:`ValidationError` surface when a
  required alias is unset, and the default values for the three
  optional aliases.

Feature: trikon-cloud-orchestrator, Property 8: InstallationEventMessage shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trikon_cloud.installation_lifecycle.models import (
    InstallationEventMessage,
    LifecycleEnvConfig,
    LifecycleTableRow,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_PRIVATE_KEY_SECRET_ARN,
    CANONICAL_DELIVERY_ID,
    CANONICAL_GITHUB_APP_ID,
    CANONICAL_INSTALLATION_ID,
    CANONICAL_REPO_FULL_NAME,
    CANONICAL_SENT_AT,
    make_installation_event_message,
)

# ---------------------------------------------------------------------------
# Canonical payload builder — kept local so tests do not couple to
# ``make_installation_event_message`` for the negative-path branches
# that need to inject invalid values the factory's :class:`Literal`
# signature would reject at the call site.
# ---------------------------------------------------------------------------


def _canonical_installation_event_dict() -> dict[str, object]:
    """Return a full-shape dict for ``InstallationEventMessage.model_validate``.

    Every value matches the canonical constants declared in
    ``conftest.py``. Negative-path tests copy this dict, mutate one
    field, and assert :class:`ValidationError` on validation.
    """
    return {
        "installation_id": CANONICAL_INSTALLATION_ID,
        "github_app_id": CANONICAL_GITHUB_APP_ID,
        "event_type": "installation.created",
        "repositories": (CANONICAL_REPO_FULL_NAME,),
        "sent_at": CANONICAL_SENT_AT,
        "delivery_id": CANONICAL_DELIVERY_ID,
    }


def _canonical_lifecycle_table_row() -> LifecycleTableRow:
    """Return a canonical :class:`LifecycleTableRow` for reuse across tests."""
    return LifecycleTableRow(
        installation_id=CANONICAL_INSTALLATION_ID,
        status="active",
        github_app_id=CANONICAL_GITHUB_APP_ID,
        created_at=CANONICAL_SENT_AT,
        updated_at=CANONICAL_SENT_AT,
        repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
    )


# ---------------------------------------------------------------------------
# InstallationEventMessage.
# ---------------------------------------------------------------------------


def test_installation_event_message_canonical_construction() -> None:
    """The conftest factory produces a fully populated model instance.

    Every declared field is populated with the canonical constant and
    exposes the expected runtime value. Establishes the baseline the
    negative-path tests below drift a single field away from.
    """
    msg = make_installation_event_message()

    assert msg.installation_id == CANONICAL_INSTALLATION_ID
    assert msg.github_app_id == CANONICAL_GITHUB_APP_ID
    assert msg.event_type == "installation.created"
    assert msg.repositories == (CANONICAL_REPO_FULL_NAME,)
    assert msg.sent_at == CANONICAL_SENT_AT
    assert msg.delivery_id == CANONICAL_DELIVERY_ID


def test_installation_event_message_extra_forbid_rejects_unknown_key() -> None:
    """``extra="forbid"`` rejects any key not declared on the model.

    Spec 1's webhook receiver is the sole producer of this SQS body.
    A shape drift there — a new key added without a coordinated
    consumer update — must fail loudly at parse rather than silently
    no-op (Requirement 11.1, design.md §3.2).
    """
    payload = _canonical_installation_event_dict()
    payload["unknown_key"] = "boom"

    with pytest.raises(ValidationError):
        InstallationEventMessage.model_validate(payload)


def test_installation_event_message_frozen_rejects_mutation() -> None:
    """``frozen=True`` blocks post-construction attribute assignment.

    Pydantic v2 raises :class:`ValidationError` on frozen assignment,
    but some historical versions raised :class:`TypeError` — accept
    either so the test tracks the pydantic contract without pinning a
    specific pydantic version.
    """
    msg = make_installation_event_message()

    with pytest.raises((ValidationError, TypeError, AttributeError)):
        msg.installation_id = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    "invalid_event_type",
    [
        "installation.foo",
        "installation.updated",
        "installation.created ",  # trailing whitespace
        "INSTALLATION.CREATED",  # wrong case
        "push",
        "",
    ],
)
def test_installation_event_message_event_type_literal_rejects_invalid(
    invalid_event_type: str,
) -> None:
    """Any ``event_type`` outside the four declared literals raises.

    The four allowed values are the exact GitHub webhook event
    strings the Lifecycle_Handler dispatches on (design.md §5.1).
    A malformed or unexpected value must not reach the dispatcher
    (Requirement 11.2).
    """
    payload = _canonical_installation_event_dict()
    payload["event_type"] = invalid_event_type

    with pytest.raises(ValidationError):
        InstallationEventMessage.model_validate(payload)


@pytest.mark.parametrize("field_name", ["installation_id", "github_app_id"])
def test_installation_event_message_id_fields_reject_below_one(
    field_name: str,
) -> None:
    """Both identifier fields carry ``Field(ge=1)`` — zero and negatives raise.

    GitHub installation and app ids are always positive integers.
    A ``0`` or negative value at parse time signals an upstream shape
    bug (Spec 1's webhook receiver, or a malformed replay); the
    :class:`ValidationError` is the correct terminal-parse response
    (Requirement 11.2).
    """
    payload = _canonical_installation_event_dict()
    payload[field_name] = 0

    with pytest.raises(ValidationError):
        InstallationEventMessage.model_validate(payload)


def test_installation_event_message_json_round_trip_preserves_all_fields() -> None:
    """``model_dump_json`` → ``model_validate_json`` preserves every field byte-for-byte.

    The Lifecycle_Handler never re-serializes the SQS body it consumes,
    but round-trip correctness is the load-bearing invariant behind
    Property 8: for every valid ``InstallationEventMessage``, the
    JSON serialization and its re-validation converge to the same
    model. Asserts (a) field-level equality via :meth:`BaseModel.__eq__`,
    (b) byte-identical JSON on re-serialization, and (c) per-field
    equality on every declared attribute so a regression on any
    single field is diagnosable from the assertion message.
    """
    original = make_installation_event_message()
    serialized = original.model_dump_json()

    reloaded = InstallationEventMessage.model_validate_json(serialized)

    # (a) Model-level equality.
    assert reloaded == original
    # (b) Byte-for-byte re-serialization.
    assert reloaded.model_dump_json() == serialized
    # (c) Per-field equality — every declared attribute.
    assert reloaded.installation_id == original.installation_id
    assert reloaded.github_app_id == original.github_app_id
    assert reloaded.event_type == original.event_type
    assert reloaded.repositories == original.repositories
    assert reloaded.sent_at == original.sent_at
    assert reloaded.delivery_id == original.delivery_id


# ---------------------------------------------------------------------------
# LifecycleTableRow.
# ---------------------------------------------------------------------------


def test_lifecycle_table_row_construction_frozen_and_status_literal() -> None:
    """The row constructs cleanly, is frozen, and rejects an unknown ``status``.

    Three assertions in one test because the invariants are tightly
    coupled — the row's only mutation-shaped surface is
    ``status``, which is also the field the ``Literal`` gate covers,
    and both the mutation guard and the literal gate must hold
    simultaneously for the two-state lifecycle (design.md §2.2) to
    be enforceable at the type layer.
    """
    row = _canonical_lifecycle_table_row()

    # Construction: every field surfaces the canonical value.
    assert row.installation_id == CANONICAL_INSTALLATION_ID
    assert row.status == "active"
    assert row.repositories == frozenset({CANONICAL_REPO_FULL_NAME})

    # ``frozen=True``: attribute assignment on an existing instance
    # raises. Accept the widened exception tuple for the same
    # cross-pydantic-version reason as the InstallationEventMessage
    # frozen test.
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        row.status = "disabled"  # type: ignore[misc]

    # ``status`` ``Literal["active", "disabled"]``: an out-of-set
    # value is rejected at construction, not silently coerced.
    with pytest.raises(ValidationError):
        LifecycleTableRow(
            installation_id=CANONICAL_INSTALLATION_ID,
            status="pending",
            github_app_id=CANONICAL_GITHUB_APP_ID,
            created_at=CANONICAL_SENT_AT,
            updated_at=CANONICAL_SENT_AT,
            repositories=frozenset({CANONICAL_REPO_FULL_NAME}),
        )


# ---------------------------------------------------------------------------
# LifecycleEnvConfig.
# ---------------------------------------------------------------------------


def test_lifecycle_env_config_loads_required_and_applies_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-driven construction populates required aliases and defaults optional ones.

    The autouse ``_env_setup`` fixture in ``conftest.py`` already
    populates ``TRIKON_AWS_ACCOUNT_ID``, ``TRIKON_APP_PRIVATE_KEY_SECRET_ARN``,
    and ``AWS_REGION``. This test additionally clears the three
    optional aliases so the defaults declared on the model
    (design.md §2.3) govern — the fallback values are the load-bearing
    contract when a fresh Lambda deployment leaves them unset.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("TRIKON_INSTALLATIONS_TABLE", raising=False)
    monkeypatch.delenv("TRIKON_LOG_LEVEL", raising=False)

    config = LifecycleEnvConfig()

    # Required aliases — sourced from the autouse fixture.
    assert config.trikon_aws_account_id == CANONICAL_ACCOUNT_ID
    assert config.trikon_app_private_key_secret_arn == (
        CANONICAL_APP_PRIVATE_KEY_SECRET_ARN
    )
    # Defaults for the three optional aliases (design.md §2.3).
    assert config.trikon_installations_table == "trikon-cloud-installations"
    assert config.aws_region == "us-east-1"
    assert config.trikon_log_level == "INFO"


def test_lifecycle_env_config_raises_on_missing_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsetting a required alias raises :class:`ValidationError` at construction.

    A missing ``TRIKON_AWS_ACCOUNT_ID`` (or its sibling secret ARN)
    must not silently fall back to a placeholder — the Runtime_IAM_Renderer
    bakes the account id into every ``PolicyStatement.Resource`` ARN it
    emits, so a wrong / empty value would produce a per-installation
    role that grants access to the wrong AWS account's tables
    (Requirement 11.4 / design.md §6.3). Failing at env-load is the
    only safe surface.
    """
    monkeypatch.delenv("TRIKON_AWS_ACCOUNT_ID", raising=False)

    with pytest.raises(ValidationError):
        LifecycleEnvConfig()
