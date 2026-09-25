# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every ``BaseModel`` subclass. Under this repo's ``disallow_any_explicit =
# true`` mypy config, each class definition surfaces as an ``explicit-any``
# error. The error refers to code the plugin generates, not code we write —
# silence it at the file level. The ``botocore.exceptions.ClientError`` type
# arrives from an untyped upstream (``# type: ignore[import-untyped]`` at the
# import site), so ``exc.response`` attribute access on the error path also
# surfaces as ``Any`` and is bounded by the same suppression. All hand-rolled
# public annotations in this module remain precise — ``ProvisionResult``,
# ``DeprovisionResult``, ``IamClientProtocol``, ``IamProvisioner`` — none
# declares ``Any`` on any signature.
# mypy: disable-error-code="explicit-any"
"""Runtime IAM role provisioner for per-installation Fargate task roles.

Runtime counterpart of ``.kiro/specs/trikon-cloud-orchestrator/design.md``
§5.2 and §5.3. Wraps five ``iam:*`` API calls — ``CreateRole``,
``PutRolePolicy``, ``DeleteRolePolicy``, ``DeleteRole``, ``GetRole`` —
into two idempotent operations:

* :meth:`IamProvisioner.provision` — the ``installation.created`` path.
  On redelivery (Requirement 14.2) the AWS ``EntityAlreadyExists``
  error code is folded into ``already_existed=True`` and the ARN is
  fetched via a follow-up ``iam:GetRole`` so the caller's log record
  can still carry it. Any other ``CreateRole`` error re-raises.

* :meth:`IamProvisioner.deprovision` — the ``installation.deleted``
  path. Deletes the inline policy FIRST, then the role — reversing
  the order causes ``iam:DeleteRole`` to fail with ``DeleteConflict``
  because the inline policy is still attached (design.md §5.3). A
  ``NoSuchEntity`` from either leg is folded into
  ``already_absent=True`` per Requirement 14.4.

The provisioner never calls ``sts:AssumeRole`` on the roles it creates
and its own execution role never grants ``iam:PassRole`` on the
``trikon-verify-task-role-*`` pattern (Requirement 15.5). Every
``iam:*`` operation is name-scoped by the Lifecycle_Handler execution
policy declared in this spec's CDK stack.

Byte-match contract: the JSON documents passed as
``AssumeRolePolicyDocument`` to :func:`iam_client.create_role` and as
``PolicyDocument`` to :func:`iam_client.put_role_policy` are produced
by :func:`canonical_json` — lexicographically-sorted keys, no
whitespace between tokens. This is what makes the runtime IAM
document byte-equivalent to the CDK synth output (design.md §6.5,
Property P10). The inline policy name is fixed to
``"TrikonInstallationPolicy"``; ``deprovision`` MUST reuse this same
name on ``iam:DeleteRolePolicy`` or the delete leaves an orphaned
policy attached to a soon-to-be-deleted role.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from trikon_cloud.installation_lifecycle.iam_template import (
    AssumeRolePolicyDocument,
    InstallationPolicyDocument,
    canonical_json,
)

# Ordering pinned by task 9 (tasks.md): the wrapper class first, then the
# Protocol adapter, then the two result models. Not isort-alphabetical.
__all__ = [  # noqa: RUF022
    "IamProvisioner",
    "IamClientProtocol",
    "ProvisionResult",
    "DeprovisionResult",
]


# ---------------------------------------------------------------------------
# AWS IAM API-level constants
# ---------------------------------------------------------------------------

# Fixed inline-policy name for the per-installation task role (design.md
# §5.3). Its immutability is load-bearing on the delete path:
# :meth:`IamProvisioner.deprovision` MUST call ``iam:DeleteRolePolicy``
# with this same name, or the subsequent ``iam:DeleteRole`` fails with
# ``DeleteConflict`` because an inline policy remains attached.
_INLINE_POLICY_NAME: str = "TrikonInstallationPolicy"

# The wire-level error code AWS IAM's ``CreateRole`` API returns
# (via ``ClientError.response["Error"]["Code"]``) when a role with the
# requested ``RoleName`` already exists. AWS's IAM API reference and
# moto's own ``iam/exceptions.py`` both emit the un-suffixed form
# ``EntityAlreadyExists``. The design's ``EntityAlreadyExistsException``
# phrasing (Requirement 14.2) reads as a documentation shorthand; the
# code that actually arrives on ``ClientError.response`` is
# ``EntityAlreadyExists``. Matching that form here keeps the runtime
# correct against both real AWS and moto-backed tests.
_ROLE_ALREADY_EXISTS_ERROR_CODE: str = "EntityAlreadyExists"

# The wire-level error code AWS IAM's ``DeleteRolePolicy``, ``DeleteRole``,
# and ``GetRole`` APIs return when the target does not exist. Requirement
# 14.4 folds this into ``already_absent=True`` on both delete legs.
_NO_SUCH_ENTITY_ERROR_CODE: str = "NoSuchEntity"


# ---------------------------------------------------------------------------
# TypedDict shapes for the boto3 IAM client's request / response payloads
# ---------------------------------------------------------------------------


class _IamTag(TypedDict):
    """Structural shape of one entry in the ``Tags`` list of ``iam:CreateRole``.

    AWS's boto3 IAM client accepts ``Tags`` as a ``list`` of
    ``{"Key": str, "Value": str}`` dicts. Declaring the entry as a
    :class:`typing.TypedDict` keeps the :class:`IamClientProtocol`
    adapter fully typed under ``mypy --strict`` without a runtime
    dependency on ``boto3-stubs`` (Invariant 8 / Requirement 17.2).
    """

    Key: str
    Value: str


class _IamRoleData(TypedDict):
    """The subset of the ``Role`` field on ``iam:CreateRole`` / ``iam:GetRole``.

    AWS returns many keys (``Path``, ``RoleName``, ``CreateDate``, …);
    :meth:`IamProvisioner.provision` reads only ``Arn`` to populate
    :attr:`ProvisionResult.role_arn`. Declaring a minimal TypedDict
    lets ``mypy --strict`` verify the one field we consume without
    over-committing to the boto3 response's evolving surface.
    """

    Arn: str


class _IamRoleResponse(TypedDict):
    """The subset of the ``iam:CreateRole`` / ``iam:GetRole`` response body used here."""

    Role: _IamRoleData


# ---------------------------------------------------------------------------
# Protocol adapter for the boto3 IAM client
# ---------------------------------------------------------------------------


class IamClientProtocol(Protocol):
    """Structural type for the subset of the boto3 IAM client this module invokes.

    ``boto3`` ships without type stubs; declaring the five methods
    :class:`IamProvisioner` calls as a :class:`typing.Protocol` lets
    ``mypy --strict`` type-check every call site without a runtime
    dependency on ``boto3-stubs``. Return types are typed as follows
    per task 9's "no ``Any`` return types" rule:

    * :meth:`create_role` and :meth:`get_role` return
      :class:`_IamRoleResponse` — a nested :class:`typing.TypedDict`
      exposing only the ``Role.Arn`` field :meth:`IamProvisioner.provision`
      consumes.
    * :meth:`put_role_policy`, :meth:`delete_role_policy`, and
      :meth:`delete_role` return :class:`object` — the opaque top
      type — because the provisioner never inspects their response
      bodies. Using :class:`object` (rather than ``Any``) preserves
      Invariant 8: the return value is unusable without an explicit
      cast, so no downstream code can accidentally rely on an
      untyped shape.

    Kwarg names use the boto3 SDK's PascalCase convention (``RoleName``,
    ``AssumeRolePolicyDocument``, …); the per-line ``noqa: N803``
    suppresses ruff's pep8-naming complaint at the Protocol boundary
    only.
    """

    def create_role(
        self,
        *,
        RoleName: str,  # noqa: N803
        AssumeRolePolicyDocument: str,  # noqa: N803
        Tags: list[_IamTag],  # noqa: N803
    ) -> _IamRoleResponse: ...

    def put_role_policy(
        self,
        *,
        RoleName: str,  # noqa: N803
        PolicyName: str,  # noqa: N803
        PolicyDocument: str,  # noqa: N803
    ) -> object: ...

    def delete_role_policy(
        self,
        *,
        RoleName: str,  # noqa: N803
        PolicyName: str,  # noqa: N803
    ) -> object: ...

    def delete_role(
        self,
        *,
        RoleName: str,  # noqa: N803
    ) -> object: ...

    def get_role(
        self,
        *,
        RoleName: str,  # noqa: N803
    ) -> _IamRoleResponse: ...


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class ProvisionResult(BaseModel):
    """Outcome of :meth:`IamProvisioner.provision`.

    ``role_arn`` is the fully-qualified IAM ARN of the created (or
    already-existing) role. It is populated from ``iam:CreateRole``'s
    response on the happy path and from a follow-up ``iam:GetRole``
    call on the redelivery path where ``already_existed=True``.
    ``handle_installation_created`` uses ``already_existed`` to
    decide whether to emit the INFO ``installation_already_provisioned``
    log line (design.md §5.2, Requirement 14.2).

    Frozen so a returned instance cannot be mutated in place — the
    handler either reads it once and moves on, or re-runs the
    provisioner to derive a fresh value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role_arn: str
    already_existed: bool


class DeprovisionResult(BaseModel):
    """Outcome of :meth:`IamProvisioner.deprovision`.

    ``role_name`` is the deterministic ``trikon-verify-task-role-{id}``
    string; the handler uses it in the INFO
    ``installation_role_already_absent`` log line when the role was
    already gone.

    ``already_absent`` is ``True`` only when BOTH legs of the two-step
    delete (``DeleteRolePolicy`` then ``DeleteRole``) reported the
    target absent. A partial-cleanup outcome — inline policy already
    gone, role still present — returns ``already_absent=False`` so
    the caller can distinguish "second delete arrived" (fully idempotent)
    from "recovering from a half-completed prior delete" (also success,
    but non-idempotent on the wire).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role_name: str
    already_absent: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _role_name_for(installation_id: int) -> str:
    """Return the deterministic per-installation task-role name.

    The pattern ``trikon-verify-task-role-{installation_id}`` is
    Invariant-1 load-bearing: the runtime task-role ARN embedded in
    the ``ecs.RunTask`` call (design.md §3.3) uses the identical
    template, and the Lifecycle_Handler execution-role policy grants
    the ``iam:*`` verbs only under this name pattern (Requirement
    15.4).
    """
    return f"trikon-verify-task-role-{installation_id}"


def _client_error_code(exc: ClientError) -> str:
    """Return the AWS error code from a :class:`botocore.exceptions.ClientError`.

    boto3 nests error metadata under
    ``ClientError.response["Error"]["Code"]``. Missing keys or a
    non-string value fold to an empty string, which will never match
    :data:`_ROLE_ALREADY_EXISTS_ERROR_CODE` or
    :data:`_NO_SUCH_ENTITY_ERROR_CODE`, so the caller's equality check
    falls through to the re-raise branch — an unexpected error shape
    is not silently swallowed.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error_block = response.get("Error")
    if not isinstance(error_block, dict):
        return ""
    code = error_block.get("Code")
    return code if isinstance(code, str) else ""


# ---------------------------------------------------------------------------
# The provisioner
# ---------------------------------------------------------------------------


class IamProvisioner:
    """Provisions and de-provisions the per-installation Fargate task role.

    Constructor accepts the boto3 IAM client via dependency injection
    (typed through :class:`IamClientProtocol`) so tests can supply
    moto's IAM client or a hand-rolled fake without an intermediate
    ``cast`` at every call site. The provisioner is stateless — it
    holds only a reference to the IAM client — and safe to share
    across warm invocations of the Lifecycle_Handler Lambda.

    Design contract:

    * :meth:`provision` performs ``CreateRole`` then ``PutRolePolicy``;
      redelivery-safe on ``EntityAlreadyExists`` from ``CreateRole``.
      ``PutRolePolicy`` is naturally idempotent — repeated calls
      overwrite the same-named inline policy with the same document —
      so no error-code branch is required on the second leg.
    * :meth:`deprovision` performs ``DeleteRolePolicy`` then
      ``DeleteRole``; both legs treat ``NoSuchEntity`` as success.
      Order is fixed: reversing it triggers ``DeleteConflict`` on
      ``DeleteRole`` because the inline policy is still attached.
    """

    def __init__(self, *, iam_client: IamClientProtocol) -> None:
        self._iam_client: IamClientProtocol = iam_client

    def provision(
        self,
        installation_id: int,
        *,
        policy: InstallationPolicyDocument,
        assume_role_policy: AssumeRolePolicyDocument,
    ) -> ProvisionResult:
        """Create the per-installation task role and attach the inline policy.

        Two-step, idempotent (Requirement 14.2):

        1. ``iam:CreateRole`` with the fixed ``AssumeRolePolicyDocument``
           and a single ``installation_id`` tag. An ``EntityAlreadyExists``
           error code folds into ``already_existed=True`` and the ARN
           is fetched via a follow-up ``iam:GetRole`` so the returned
           :class:`ProvisionResult` still carries a usable value for
           the handler's log record. Any other error code re-raises
           unchanged so the handler's outer classifier decides
           Terminal vs Transient (Requirement 15.3).

        2. ``iam:PutRolePolicy`` with the fixed inline-policy name
           ``"TrikonInstallationPolicy"`` and the canonical-JSON
           serialization of the passed
           :class:`~trikon_cloud.installation_lifecycle.iam_template.InstallationPolicyDocument`.
           ``PutRolePolicy`` is naturally idempotent — a re-put with
           the same document is a no-op on the wire — so this leg
           does not branch on an error code.

        Args:
            installation_id: GitHub App installation ID. Baked into
                the role name (``trikon-verify-task-role-{id}``) and
                emitted as the role's single ``installation_id`` tag.
            policy: The rendered inline-policy document from
                :func:`~trikon_cloud.installation_lifecycle.iam_template.render_installation_policy_document`.
                Serialized via :func:`~trikon_cloud.installation_lifecycle.iam_template.canonical_json`
                so runtime and CDK-synth documents are byte-equivalent
                (design.md §6.5).
            assume_role_policy: The fixed ``sts:AssumeRole`` policy
                from :func:`~trikon_cloud.installation_lifecycle.iam_template.render_installation_assume_role_policy_document`.
                Also serialized via ``canonical_json``.

        Returns:
            A frozen :class:`ProvisionResult` carrying the role ARN
            and the ``already_existed`` flag the handler consults to
            decide whether to log ``installation_already_provisioned``.

        Raises:
            botocore.exceptions.ClientError: For any ``CreateRole``
                error code other than ``EntityAlreadyExists``, or any
                ``GetRole`` / ``PutRolePolicy`` error at all. The
                handler's outer ``try/except`` maps these to
                Terminal-vs-Transient and either fails the SQS message
                (Transient — retried) or logs ERROR + fails the message
                (Terminal — DLQ'd on ``maxReceiveCount``) per
                Requirement 15.3.
        """
        role_name = _role_name_for(installation_id)
        already_existed = False
        role_arn: str

        try:
            create_response = self._iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=canonical_json(assume_role_policy),
                Tags=[
                    _IamTag(Key="installation_id", Value=str(installation_id)),
                ],
            )
            role_arn = create_response["Role"]["Arn"]
        except ClientError as exc:
            if _client_error_code(exc) != _ROLE_ALREADY_EXISTS_ERROR_CODE:
                raise
            already_existed = True
            # The role exists but we do not have its ARN in hand. GetRole
            # returns the same ``{"Role": {"Arn": ...}}`` shape so
            # ``ProvisionResult.role_arn`` is populated identically on
            # both paths.
            get_response = self._iam_client.get_role(RoleName=role_name)
            role_arn = get_response["Role"]["Arn"]

        # PutRolePolicy is idempotent by design — a same-name, same-document
        # re-put is a no-op on the wire. Requirement 14.2's idempotency
        # semantics do not need a separate error-code branch here.
        self._iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=_INLINE_POLICY_NAME,
            PolicyDocument=canonical_json(policy),
        )

        return ProvisionResult(role_arn=role_arn, already_existed=already_existed)

    def deprovision(self, installation_id: int) -> DeprovisionResult:
        """Delete the inline policy and the role, in that order.

        Order matters (design.md §5.3): ``iam:DeleteRole`` fails with
        ``DeleteConflict`` while any inline policy is still attached.
        The inline policy MUST be deleted first.

        Both legs fold ``NoSuchEntity`` into a per-leg
        ``already_absent`` local (Requirement 14.4). The returned
        :attr:`DeprovisionResult.already_absent` is ``True`` only
        when BOTH legs reported the target absent — a mixed outcome
        (policy already gone, role still present, e.g. recovering
        from a prior partial delete) completes the cleanup and
        returns ``already_absent=False`` so the caller can
        distinguish a duplicate delete from a repair.

        Args:
            installation_id: GitHub App installation ID; determines
                the role name (``trikon-verify-task-role-{id}``).

        Returns:
            A frozen :class:`DeprovisionResult` carrying the role
            name (deterministic from the installation id) and the
            combined ``already_absent`` flag.

        Raises:
            botocore.exceptions.ClientError: For any error code other
                than ``NoSuchEntity`` on either leg. The handler's
                outer classifier decides Terminal vs Transient at
                the call site.
        """
        role_name = _role_name_for(installation_id)

        policy_absent = self._delete_inline_policy(role_name)
        role_absent = self._delete_role(role_name)

        return DeprovisionResult(
            role_name=role_name,
            already_absent=policy_absent and role_absent,
        )

    def _delete_inline_policy(self, role_name: str) -> bool:
        """Delete the inline policy; return ``True`` iff it was already absent.

        ``NoSuchEntity`` covers both "the role has no such inline
        policy" and "the role itself does not exist" — IAM returns
        the same error code for both, and both mean the target of
        this delete leg is absent, which is what we care about here.
        """
        try:
            self._iam_client.delete_role_policy(
                RoleName=role_name,
                PolicyName=_INLINE_POLICY_NAME,
            )
        except ClientError as exc:
            if _client_error_code(exc) != _NO_SUCH_ENTITY_ERROR_CODE:
                raise
            return True
        return False

    def _delete_role(self, role_name: str) -> bool:
        """Delete the role; return ``True`` iff it was already absent."""
        try:
            self._iam_client.delete_role(RoleName=role_name)
        except ClientError as exc:
            if _client_error_code(exc) != _NO_SUCH_ENTITY_ERROR_CODE:
                raise
            return True
        return False
