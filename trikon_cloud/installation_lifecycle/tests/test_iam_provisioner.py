# The spy wrapper (:class:`_RecordingIamClient`) that captures every
# call the provisioner makes intentionally declares ``**kwargs: Any``
# and ``-> Any`` — boto3 has no type stubs and the response shapes
# vary per API. Additionally, ``botocore.exceptions.ClientError`` is
# imported from an untyped upstream (``# type: ignore[import-untyped]``
# at the import site), so its ``response`` attribute surfaces as
# ``Any`` on the error-inspection path. Under this repo's
# ``disallow_any_explicit = true`` mypy config, every one of these
# would fire ``explicit-any``. Silence at file scope — this is a test
# module, mirroring the pattern established by
# ``trikon_cloud/webhook_receiver/tests/test_sqs_writer.py``.
# mypy: disable-error-code="explicit-any"
"""Moto-backed unit tests for :mod:`trikon_cloud.installation_lifecycle.iam_provisioner`.

Covers the two idempotent operations exposed by :class:`IamProvisioner`
(``.kiro/specs/trikon-cloud-orchestrator/design.md`` §5.2 and §5.3):

* :meth:`IamProvisioner.provision` — ``iam:CreateRole`` +
  ``iam:PutRolePolicy``. Redelivery-safe: an ``EntityAlreadyExists``
  from ``CreateRole`` folds into ``already_existed=True`` and the ARN
  is recovered via a follow-up ``iam:GetRole`` (Requirement 14.2).
  ``PutRolePolicy`` re-runs on the redelivery path — same-name,
  same-document re-put is a no-op on the wire, so no separate
  error-code branch is needed (§5.2, module docstring).

* :meth:`IamProvisioner.deprovision` — ``iam:DeleteRolePolicy`` then
  ``iam:DeleteRole``. Order is fixed: ``iam:DeleteRole`` fails with
  ``DeleteConflict`` while any inline policy is still attached (§5.3).
  ``NoSuchEntity`` on either leg folds into a per-leg
  ``already_absent`` local; the returned
  :attr:`DeprovisionResult.already_absent` is ``True`` only when
  BOTH legs reported the target absent (Requirement 14.4, §5.3 fold
  semantics).

Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency
on re-delivery for all four event types.

Every test runs under moto's ``@mock_aws`` decorator — no live AWS
contact. A spy wrapper (:class:`_RecordingIamClient`) delegates to a
real moto-backed IAM client while recording every call, so tests can
assert on call ORDER, kwargs, and repeat counts without threading
through moto's stored state or losing the byte-form of the
canonical-JSON payload after boto3 URL-decodes it on retrieval.
"""

from __future__ import annotations

from typing import Any, cast

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from moto import mock_aws

from trikon_cloud.installation_lifecycle.iam_provisioner import (
    DeprovisionResult,
    IamClientProtocol,
    IamProvisioner,
    ProvisionResult,
)
from trikon_cloud.installation_lifecycle.iam_template import (
    AssumeRolePolicyDocument,
    InstallationPolicyDocument,
    canonical_json,
    render_installation_assume_role_policy_document,
    render_installation_policy_document,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_PRIVATE_KEY_SECRET_ARN,
    CANONICAL_INSTALLATION_ID,
)

# ---------------------------------------------------------------------------
# Constants — the two AWS API-level names the provisioner pins per
# design.md §5.2 (deterministic role-name pattern) and §5.3 (fixed
# inline-policy name). Duplicating them here rather than reaching into
# the module's private ``_INLINE_POLICY_NAME`` / ``_role_name_for``
# keeps the tests behavioral: if either value drifts on the production
# side, these asserts fire.
# ---------------------------------------------------------------------------

_REGION: str = "us-east-1"
_EXPECTED_ROLE_NAME: str = f"trikon-verify-task-role-{CANONICAL_INSTALLATION_ID}"
_EXPECTED_INLINE_POLICY_NAME: str = "TrikonInstallationPolicy"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _setup_moto_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the four env vars moto's boto3 shim looks for.

    The autouse ``_env_setup`` fixture in ``conftest.py`` sets the
    three Trikon aliases + ``AWS_REGION`` but not the AWS credential
    triple. Setting them per test with dummy ``"testing"`` values
    matches Spec 1's ``test_sqs_writer.py`` pattern and guarantees
    moto activates cleanly regardless of the developer's host env.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)


def _make_inputs() -> tuple[InstallationPolicyDocument, AssumeRolePolicyDocument]:
    """Return canonical ``(policy, assume_role_policy)`` inputs for provision.

    Uses the same account id, app-key ARN, and region every test
    reads from conftest, so the resulting canonical-JSON strings are
    stable and comparable across tests.
    """
    policy = render_installation_policy_document(
        installation_id=CANONICAL_INSTALLATION_ID,
        app_private_key_secret_arn=CANONICAL_APP_PRIVATE_KEY_SECRET_ARN,
        account_id=CANONICAL_ACCOUNT_ID,
        region=_REGION,
    )
    assume_role_policy = render_installation_assume_role_policy_document()
    return policy, assume_role_policy


def _client_error(code: str, message: str = "mocked") -> ClientError:
    """Construct a ``ClientError`` with a specific ``Error.Code``.

    boto3 stashes the wire error code at
    ``ClientError.response["Error"]["Code"]``; the provisioner's
    ``_client_error_code`` helper reads exactly this path. Building
    the exception with the matching shape lets the injection tests
    exercise the two error-code branches (fold vs re-raise) without
    coupling to moto's own error surface, which does not readily
    emit ``AccessDenied`` from an ``iam:*`` call.
    """
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "MockOperation",
    )


class _RecordingIamClient:
    """Spy wrapper around a real (moto-backed) IAM client.

    Records every method the provisioner calls in the order it was
    invoked, and — via :meth:`inject_error` — can raise a configured
    :class:`ClientError` in place of the delegated call for exactly
    the branches (Requirement 14.2 non-EntityAlreadyExists,
    Requirement 14.4 non-NoSuchEntity) moto itself cannot easily
    provoke.

    ``**kwargs: Any`` and ``-> Any`` on the five methods intentionally
    mirror the untyped surface of boto3; :func:`typing.cast` at the
    call site widens the spy back to :class:`IamClientProtocol` for
    :class:`IamProvisioner`'s constructor so mypy's Protocol
    conformance check does not flag the shape difference. The
    file-level ``explicit-any`` suppression covers the ``Any``
    annotations here.
    """

    def __init__(self, wrapped: Any) -> None:
        # ``wrapped`` is a boto3 IAM client (returned by
        # ``boto3.client("iam", ...)``). boto3 has no type stubs, so
        # this attribute is intrinsically ``Any``.
        self._wrapped: Any = wrapped
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._errors: dict[str, BaseException] = {}

    def inject_error(self, method_name: str, exc: BaseException) -> None:
        """Configure the spy to raise ``exc`` on every call to ``method_name``."""
        self._errors[method_name] = exc

    def call_names(self) -> list[str]:
        """Return the ordered list of method names invoked on the spy."""
        return [name for name, _ in self.calls]

    def _dispatch(self, method_name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method_name, dict(kwargs)))
        if method_name in self._errors:
            raise self._errors[method_name]
        return getattr(self._wrapped, method_name)(**kwargs)

    def create_role(self, **kwargs: Any) -> Any:
        return self._dispatch("create_role", kwargs)

    def put_role_policy(self, **kwargs: Any) -> Any:
        return self._dispatch("put_role_policy", kwargs)

    def delete_role_policy(self, **kwargs: Any) -> Any:
        return self._dispatch("delete_role_policy", kwargs)

    def delete_role(self, **kwargs: Any) -> Any:
        return self._dispatch("delete_role", kwargs)

    def get_role(self, **kwargs: Any) -> Any:
        return self._dispatch("get_role", kwargs)


def _make_spy_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[IamProvisioner, _RecordingIamClient]:
    """Build a provisioner wrapping a moto-backed IAM client through the spy.

    MUST be called from inside a ``@mock_aws``-decorated test — the
    ``boto3.client("iam", ...)`` returns a moto-backed client only
    while the mock context is active.
    """
    _setup_moto_credentials(monkeypatch)
    real_client = boto3.client("iam", region_name=_REGION)
    spy = _RecordingIamClient(real_client)
    provisioner = IamProvisioner(iam_client=cast(IamClientProtocol, spy))
    return provisioner, spy


# ---------------------------------------------------------------------------
# provision — happy path, redelivery, canonical-JSON, tags, error path.
# ---------------------------------------------------------------------------


@mock_aws
def test_provision_happy_path_creates_role_and_returns_arn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First provision creates the role and returns ``already_existed=False``.

    The ARN comes directly from ``iam:CreateRole``'s response body,
    so no follow-up ``iam:GetRole`` runs on the happy path — only
    ``CreateRole`` then ``PutRolePolicy``. ``RoleName`` is pinned to
    the deterministic ``trikon-verify-task-role-{installation_id}``
    pattern (design.md §5.2, Invariant 1).
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    result = provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    assert isinstance(result, ProvisionResult)
    assert result.already_existed is False
    # moto returns an ARN of the form
    # ``arn:aws:iam::<account>:role/<role-name>``; asserting the
    # suffix keeps the test decoupled from moto's synthetic account id.
    assert result.role_arn.endswith(f":role/{_EXPECTED_ROLE_NAME}")
    assert spy.call_names() == ["create_role", "put_role_policy"]
    create_kwargs = spy.calls[0][1]
    assert create_kwargs["RoleName"] == _EXPECTED_ROLE_NAME


@mock_aws
def test_provision_reprovision_folds_entity_already_exists_into_already_existed_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency on re-delivery.

    A second provision on the same ``installation_id`` folds
    ``iam:CreateRole``'s ``EntityAlreadyExists`` into
    ``already_existed=True`` and populates the ARN via a follow-up
    ``iam:GetRole`` (design.md §5.2, Requirement 14.2). The ARN on
    the redelivery path matches the ARN from the initial creation —
    ``GetRole`` reads back the same role, not a fresh one.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    first = provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    second = provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    assert first.already_existed is False
    assert second.already_existed is True
    assert second.role_arn == first.role_arn
    # Redelivery exercises CreateRole (fails), GetRole (recovers ARN),
    # then PutRolePolicy (idempotent re-put). Slice past the first
    # provision's two calls to isolate the second provision's sequence.
    second_call_names = spy.call_names()[2:]
    assert second_call_names == ["create_role", "get_role", "put_role_policy"]


@mock_aws
def test_provision_put_role_policy_runs_even_when_role_already_existed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``iam:PutRolePolicy`` runs on the redelivery path.

    The provisioner treats ``PutRolePolicy`` as naturally idempotent:
    a same-name, same-document re-put is a no-op on the wire, so
    there is no error-code branch guarding it. Redelivery MUST
    therefore still call it — the count of ``put_role_policy`` calls
    across two consecutive provisions is exactly two (design.md
    §5.2, module docstring).
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    put_calls = [name for name in spy.call_names() if name == "put_role_policy"]
    assert len(put_calls) == 2


@mock_aws
def test_provision_uses_fixed_inline_policy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline policy is put under the exact name ``"TrikonInstallationPolicy"``.

    Immutability is load-bearing (design.md §5.3): the delete leg
    of :meth:`IamProvisioner.deprovision` MUST call
    ``iam:DeleteRolePolicy`` with this same name, or the subsequent
    ``iam:DeleteRole`` fails with ``DeleteConflict`` because the
    inline policy remains attached.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    put_kwargs = next(
        kwargs for name, kwargs in spy.calls if name == "put_role_policy"
    )
    assert put_kwargs["PolicyName"] == _EXPECTED_INLINE_POLICY_NAME
    # And ``RoleName`` on the same call must match the deterministic
    # pattern — a mismatch would attach the policy to the wrong role.
    assert put_kwargs["RoleName"] == _EXPECTED_ROLE_NAME


@mock_aws
def test_provision_serializes_assume_role_policy_via_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CreateRole.AssumeRolePolicyDocument`` is byte-identical to ``canonical_json``.

    Sorted keys, no whitespace between tokens — the byte-match
    contract against Spec 2's CDK synth output (design.md §6.5,
    Property P10). Asserting the exact string here anchors the
    runtime side of the contract; the reverse direction is enforced
    by task 19's dedicated ``test_iam_policy_contract.py`` synth
    comparison.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    create_kwargs = spy.calls[0][1]
    assert create_kwargs["AssumeRolePolicyDocument"] == canonical_json(
        assume_role_policy
    )


@mock_aws
def test_provision_serializes_policy_document_via_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PutRolePolicy.PolicyDocument`` is byte-identical to ``canonical_json``.

    Same byte-match contract as the assume-role policy — sorted
    keys, no whitespace, aliased ``"ForAllValues:StringEquals"``,
    ``None`` conditions omitted. Anchors the runtime side of Property
    P10 for the inline policy document.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    put_kwargs = next(
        kwargs for name, kwargs in spy.calls if name == "put_role_policy"
    )
    assert put_kwargs["PolicyDocument"] == canonical_json(policy)


@mock_aws
def test_provision_tags_include_installation_id_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role is tagged with the installation id as a string value.

    design.md §6.4: the ``installation_id`` tag scopes the runtime
    principal for downstream ``${aws:PrincipalTag/installation_id}``
    conditions on other Lifecycle policies. The tag ``Value`` MUST
    be the string form of the integer id — a numeric Value would be
    an AWS-side validation error, and a mismatched string would
    silently misalign every principal-tag condition.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()

    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    create_kwargs = spy.calls[0][1]
    tags = create_kwargs["Tags"]
    assert {
        "Key": "installation_id",
        "Value": str(CANONICAL_INSTALLATION_ID),
    } in tags


@mock_aws
def test_provision_propagates_non_entity_already_exists_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-``EntityAlreadyExists`` errors from ``CreateRole`` bubble up unchanged.

    The handler's outer classifier decides Terminal vs Transient
    (Requirement 15.3) — the provisioner MUST NOT silently swallow
    an ``AccessDenied`` or any other error code. Also asserts that
    ``PutRolePolicy`` does NOT run when ``CreateRole`` failed for a
    non-fold-worthy reason: attaching a policy to a role that failed
    to create would be a wire-level error and leave the caller in a
    confused state.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    spy.inject_error("create_role", _client_error("AccessDenied"))
    policy, assume_role_policy = _make_inputs()

    with pytest.raises(ClientError) as exc_info:
        provisioner.provision(
            CANONICAL_INSTALLATION_ID,
            policy=policy,
            assume_role_policy=assume_role_policy,
        )

    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
    assert "put_role_policy" not in spy.call_names()


# ---------------------------------------------------------------------------
# deprovision — happy path, order, fold semantics, mixed case, error path.
# ---------------------------------------------------------------------------


@mock_aws
def test_deprovision_happy_path_deletes_policy_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First deprovision after a provision returns ``already_absent=False``.

    Both delete legs succeed and neither reports the target absent
    — the role and its inline policy existed at call time. The
    returned ``role_name`` is the deterministic
    ``trikon-verify-task-role-{installation_id}`` pattern, which
    the handler uses in the ``installation_role_already_absent``
    log line on the sibling redelivery path.
    """
    provisioner, _spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()
    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )

    result = provisioner.deprovision(CANONICAL_INSTALLATION_ID)

    assert isinstance(result, DeprovisionResult)
    assert result.role_name == _EXPECTED_ROLE_NAME
    assert result.already_absent is False


@mock_aws
def test_deprovision_deletes_inline_policy_before_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``iam:DeleteRolePolicy`` runs strictly before ``iam:DeleteRole``.

    Order matters (design.md §5.3): reversing it triggers
    ``DeleteConflict`` on ``iam:DeleteRole`` because the inline
    policy is still attached. Both calls carry the deterministic
    role name; the policy leg additionally carries the fixed
    ``"TrikonInstallationPolicy"`` name so it targets the same
    policy :meth:`IamProvisioner.provision` created.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()
    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    # Isolate the deprovision sequence from the provision calls so
    # the ordering assertion reads left-to-right off the spy log.
    spy.calls.clear()

    provisioner.deprovision(CANONICAL_INSTALLATION_ID)

    assert spy.call_names() == ["delete_role_policy", "delete_role"]
    policy_kwargs = spy.calls[0][1]
    role_kwargs = spy.calls[1][1]
    assert policy_kwargs["RoleName"] == _EXPECTED_ROLE_NAME
    assert policy_kwargs["PolicyName"] == _EXPECTED_INLINE_POLICY_NAME
    assert role_kwargs["RoleName"] == _EXPECTED_ROLE_NAME


@mock_aws
def test_deprovision_no_such_entity_on_both_legs_returns_already_absent_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 11: Lifecycle idempotency on re-delivery.

    ``NoSuchEntity`` on BOTH delete legs folds into
    ``already_absent=True`` (design.md §5.3 fold semantics,
    Requirement 14.4). Under moto, deprovisioning a never-provisioned
    installation_id hits ``NoSuchEntity`` on ``DeleteRolePolicy``
    (the role does not exist, so no inline policy exists to delete)
    and again on ``DeleteRole`` — the pure double-absence case that
    makes redelivery of ``installation.deleted`` a safe no-op.
    """
    provisioner, _spy = _make_spy_provisioner(monkeypatch)

    result = provisioner.deprovision(CANONICAL_INSTALLATION_ID)

    assert result.role_name == _EXPECTED_ROLE_NAME
    assert result.already_absent is True


@mock_aws
def test_deprovision_mixed_policy_absent_role_present_returns_already_absent_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy absent + role present → ``already_absent=False``.

    A half-completed prior delete (inline policy already gone, role
    still present) is completed by this call. The result reports
    ``already_absent=False`` so the caller can distinguish a fully
    duplicate delete (Property 11) from a repair that finished the
    job — the design.md §5.3 fold semantics deliberately fold only
    when BOTH legs report the target absent, so partial-cleanup
    outcomes remain observable in the return value even though the
    wire-level outcome is still "success".
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    policy, assume_role_policy = _make_inputs()
    provisioner.provision(
        CANONICAL_INSTALLATION_ID,
        policy=policy,
        assume_role_policy=assume_role_policy,
    )
    # Detach the inline policy directly on the underlying moto
    # client (bypassing the spy) so the first delete leg of
    # ``deprovision`` hits ``NoSuchEntity`` while the role itself
    # remains present.
    spy._wrapped.delete_role_policy(
        RoleName=_EXPECTED_ROLE_NAME,
        PolicyName=_EXPECTED_INLINE_POLICY_NAME,
    )

    result = provisioner.deprovision(CANONICAL_INSTALLATION_ID)

    assert result.role_name == _EXPECTED_ROLE_NAME
    assert result.already_absent is False


@mock_aws
def test_deprovision_propagates_non_no_such_entity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-``NoSuchEntity`` errors on either delete leg bubble up unchanged.

    An ``AccessDenied`` on ``iam:DeleteRolePolicy`` is not
    fold-worthy — the handler's outer classifier decides Terminal
    vs Transient (Requirement 15.3). Also asserts ``DeleteRole``
    does NOT run when the policy-delete leg errored for a
    non-``NoSuchEntity`` reason: proceeding with the role delete
    while the inline policy is still attached would trigger a
    ``DeleteConflict`` and mask the original error.
    """
    provisioner, spy = _make_spy_provisioner(monkeypatch)
    spy.inject_error("delete_role_policy", _client_error("AccessDenied"))

    with pytest.raises(ClientError) as exc_info:
        provisioner.deprovision(CANONICAL_INSTALLATION_ID)

    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
    assert "delete_role" not in spy.call_names()
