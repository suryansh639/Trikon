# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every ``BaseModel`` subclass. Under this repo's ``disallow_any_explicit =
# true`` mypy config, each class definition surfaces as an ``explicit-any``
# error. The error refers to code the plugin generates, not code we write —
# silence it at the file level. Mirrors the pattern established by
# ``trikon_cloud/fargate_runner/models.py``.
# mypy: disable-error-code="explicit-any"
"""Pure IAM policy renderer for the per-installation Fargate task role.

This module is the runtime counterpart of Spec 2's CDK-side
``FargateRunnerStack.build_task_role_for_installation`` method
(see ``trikon_cloud/fargate_runner/infra/fargate_runner_stack.py``
lines 279-399). The runtime output of :func:`render_installation_policy_document`
MUST produce a :func:`canonical_json` string byte-equivalent to the
``PolicyDocument`` that ``aws_cdk.assertions.Template.from_stack(...)``
extracts from the same inputs. A contract test at
``trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py``
enforces this property (design.md §6.5, §6.6, Property P10).

Design references:
* §6.1 — function signature.
* §6.2 — Pydantic model hierarchy.
* §6.3 — the four ``Statement`` entries, in fixed order.
* §6.4 — scoping mechanism.
* §6.5 — byte-match strategy against CDK synth.

Purity contract:
* No ``boto3``, no ``httpx``, no filesystem reads, no environment reads.
* No side effects. All inputs are function arguments; the return value is
  a frozen Pydantic model tree.

Byte-match contract (see design.md §6.3 and CDK source at
``fargate_runner_stack.py:329-370``):
* Statements 0 and 1 use ``LeadingKeys=[str(installation_id)]`` — the
  LITERAL installation-id string, matching what CDK emits from
  ``iam.PolicyStatement(conditions={"ForAllValues:StringEquals":
  {"dynamodb:LeadingKeys": [str(installation_id)]}})``. It does NOT use
  ``${aws:PrincipalTag/installation_id}`` — that variant would fail the
  byte-match test.
* Statement 2 uses the literal installation_id in the S3 ARN
  (``.../{installation_id}/*``), NOT ``${aws:PrincipalTag/installation_id}``.
* Every ``Action`` field is ``tuple[str, ...]`` internally, but the
  serializer collapses single-element tuples to a bare string on the
  wire (see :meth:`PolicyStatement._serialize_action`). CDK synth
  emits the bare-string form for single-action statements and the
  JSON-list form only for multi-action statements; the runtime side
  matches that shape to preserve byte-equivalence.
* ``Sid`` is present on every :class:`PolicyStatement` as a Python
  attribute (design.md §6.3 pins the four Sid strings) but is
  ``Field(exclude=True)`` on the wire because Spec 2's CDK
  ``iam.PolicyStatement`` calls do not pass ``sid=`` and CDK synth
  therefore emits no ``"Sid"`` key.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# Ordering pinned by the spec (task 6, tasks.md): the three top-level
# callables first, then the five model classes in design-order (§6.2).
# The RUF022 suppression matches the pattern established by
# ``trikon_cloud/fargate_runner/models.py``.
__all__ = [  # noqa: RUF022
    "render_installation_policy_document",
    "render_installation_assume_role_policy_document",
    "canonical_json",
    "InstallationPolicyDocument",
    "PolicyStatement",
    "PolicyCondition",
    "AssumeRolePolicyDocument",
    "AssumeRolePolicyStatement",
]


# ---------------------------------------------------------------------------
# Pydantic model hierarchy — design.md §6.2 verbatim
# ---------------------------------------------------------------------------


class PolicyCondition(BaseModel):
    """The ``Condition`` block on statements 0 and 1.

    Only one shape is used in this contract:
    ``ForAllValues:StringEquals { dynamodb:LeadingKeys: [...] }``.

    The Pydantic field alias ``"ForAllValues:StringEquals"`` carries the
    colon on the wire. ``populate_by_name=True`` lets callers construct the
    model via the Pythonic snake_case name while
    ``model_dump_json(by_alias=True)`` emits the IAM-native colon key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    for_all_values_string_equals: dict[str, tuple[str, ...]] = Field(
        alias="ForAllValues:StringEquals",
    )


class PolicyStatement(BaseModel):
    """One entry in the ``Statement`` array of an IAM policy document.

    Field names use IAM's PascalCase directly; no alias remapping is needed.
    ``Action`` is always ``tuple[str, ...]`` internally so callers see a
    stable Python type regardless of arity — but the JSON serializer
    collapses single-element tuples to a bare string (see the
    ``@field_serializer`` on ``Action`` below) because CDK's
    :class:`aws_cdk.aws_iam.PolicyStatement` emits the bare-string form
    for single-action lists at synth time and byte-match against CDK
    synth (design.md §6.5, Property P10) requires matching that shape.

    ``Sid`` is declared with ``Field(exclude=True)`` because Spec 2's
    ``FargateRunnerStack.build_task_role_for_installation`` builds each
    :class:`aws_cdk.aws_iam.PolicyStatement` without a ``sid=`` argument,
    so CDK synth never emits a ``"Sid"`` key. The runtime side keeps
    ``Sid`` as a Python attribute (unit tests in
    ``test_iam_template.py`` assert on the design.md §6.3 Sid names via
    the attribute) but drops it from the wire form so canonical JSON
    byte-matches CDK synth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    Sid: str = Field(exclude=True)
    Effect: Literal["Allow"]
    Action: tuple[str, ...]
    Resource: str
    Condition: PolicyCondition | None = None

    @field_serializer("Action")
    def _serialize_action(self, actions: tuple[str, ...]) -> str | list[str]:
        """Collapse a single-element ``Action`` tuple to a bare string.

        CDK's :class:`aws_cdk.aws_iam.PolicyStatement` emits the JSON
        list form only when the ``actions=`` list has two or more
        entries; a single-action list is condensed to a bare string
        on the wire. The runtime side must match that shape to preserve
        the byte-match invariant against CDK synth (design.md §6.5,
        Property P10; contract test at ``test_iam_policy_contract.py``).

        Multi-action tuples are returned as ``list[str]`` (JSON-array
        form) — the natural Python-side representation of a JSON
        list — while the single-element case returns the bare string.
        """
        if len(actions) == 1:
            return actions[0]
        return list(actions)


class InstallationPolicyDocument(BaseModel):
    """The per-installation task-role inline policy document.

    ``Statement`` is a fixed-length four-tuple; positions are order-significant
    and pinned to Spec 2's ``add_to_policy`` call order in
    ``fargate_runner_stack.py:329-370``:

        0. ``DynamoDBVerdictsScopedToInstallation``
        1. ``DynamoDBPrStateScopedToInstallation``
        2. ``S3EvidenceSpillScopedToInstallation``
        3. ``SecretsManagerAppPrivateKeyRead``
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    Version: Literal["2012-10-17"]
    Statement: tuple[
        PolicyStatement,  # 0 — DynamoDBVerdictsScopedToInstallation
        PolicyStatement,  # 1 — DynamoDBPrStateScopedToInstallation
        PolicyStatement,  # 2 — S3EvidenceSpillScopedToInstallation
        PolicyStatement,  # 3 — SecretsManagerAppPrivateKeyRead
    ]


class AssumeRolePolicyStatement(BaseModel):
    """The single statement inside the assume-role policy document.

    ``Principal`` is a plain ``dict[str, str]`` (not a nested model) because
    IAM's Principal block is a single key/value pair for the ECS-tasks case
    (``{"Service": "ecs-tasks.amazonaws.com"}``) and no further type
    discipline is warranted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    Effect: Literal["Allow"]
    Principal: dict[str, str]
    Action: Literal["sts:AssumeRole"]


class AssumeRolePolicyDocument(BaseModel):
    """The assume-role policy document passed to ``iam:CreateRole``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    Version: Literal["2012-10-17"]
    Statement: tuple[AssumeRolePolicyStatement]


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_installation_policy_document(
    installation_id: int,
    *,
    app_private_key_secret_arn: str,
    account_id: str,
    region: str,
) -> InstallationPolicyDocument:
    """Build the per-installation task-role inline policy.

    See design.md §6.1, §6.3 and the CDK-side byte-match target at
    ``trikon_cloud/fargate_runner/infra/fargate_runner_stack.py:329-370``.

    Args:
        installation_id: The GitHub App installation ID this role scopes
            permissions to. Baked LITERALLY into ``dynamodb:LeadingKeys`` on
            statements 0 and 1 and into the S3 ARN on statement 2, matching
            what CDK's ``iam.PolicyStatement`` emits from
            ``[str(installation_id)]`` and
            ``f"arn:aws:s3:::trikon-cloud-evidence/{installation_id}/*"``.
        app_private_key_secret_arn: The Secrets Manager ARN of the GitHub App
            private key; placed verbatim into statement 3's ``Resource``.
        account_id: The 12-digit AWS account ID that hosts the two DynamoDB
            tables; substituted into the ``Resource`` ARNs for statements
            0 and 1.
        region: The AWS region for the DynamoDB ``Resource`` ARNs. At M1 this
            is always ``"us-east-1"`` — plumbing the parameter through
            preserves optionality for a hypothetical M3 multi-region
            deployment (design.md §6.3).

    Returns:
        A frozen :class:`InstallationPolicyDocument` whose canonical JSON is
        byte-equivalent to the CDK-synthesized ``PolicyDocument`` for the
        same inputs (Property P10, design.md §6.6).
    """
    installation_id_str = str(installation_id)
    leading_keys_condition = PolicyCondition(
        for_all_values_string_equals={"dynamodb:LeadingKeys": (installation_id_str,)},
    )

    # 0. DynamoDB PutItem on trikon_verdicts, scoped by LeadingKeys.
    stmt_verdicts = PolicyStatement(
        Sid="DynamoDBVerdictsScopedToInstallation",
        Effect="Allow",
        Action=("dynamodb:PutItem",),
        Resource=f"arn:aws:dynamodb:{region}:{account_id}:table/trikon_verdicts",
        Condition=leading_keys_condition,
    )

    # 1. DynamoDB GetItem/PutItem/UpdateItem on trikon_pr_state, scoped by
    # LeadingKeys.
    stmt_pr_state = PolicyStatement(
        Sid="DynamoDBPrStateScopedToInstallation",
        Effect="Allow",
        Action=("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"),
        Resource=f"arn:aws:dynamodb:{region}:{account_id}:table/trikon_pr_state",
        Condition=leading_keys_condition,
    )

    # 2. S3 PutObject/GetObject on the per-installation evidence prefix.
    # NO Condition block — the prefix is baked into the Resource ARN using
    # the LITERAL installation_id, matching the CDK source at
    # fargate_runner_stack.py:365-370.
    stmt_s3 = PolicyStatement(
        Sid="S3EvidenceSpillScopedToInstallation",
        Effect="Allow",
        Action=("s3:PutObject", "s3:GetObject"),
        Resource=f"arn:aws:s3:::trikon-cloud-evidence/{installation_id_str}/*",
        Condition=None,
    )

    # 3. Secrets Manager GetSecretValue on the App private key ARN.
    stmt_secret = PolicyStatement(
        Sid="SecretsManagerAppPrivateKeyRead",
        Effect="Allow",
        Action=("secretsmanager:GetSecretValue",),
        Resource=app_private_key_secret_arn,
        Condition=None,
    )

    return InstallationPolicyDocument(
        Version="2012-10-17",
        Statement=(stmt_verdicts, stmt_pr_state, stmt_s3, stmt_secret),
    )


def render_installation_assume_role_policy_document() -> AssumeRolePolicyDocument:
    """Return the fixed ``sts:AssumeRole`` policy for the ECS-tasks principal.

    The assume-role policy is identical for every installation — the only
    principal is ``ecs-tasks.amazonaws.com``. It is passed to
    ``iam:CreateRole`` as ``AssumeRolePolicyDocument`` via
    :func:`canonical_json` (design.md §6.2).
    """
    return AssumeRolePolicyDocument(
        Version="2012-10-17",
        Statement=(
            AssumeRolePolicyStatement(
                Effect="Allow",
                Principal={"Service": "ecs-tasks.amazonaws.com"},
                Action="sts:AssumeRole",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Canonical serializer
# ---------------------------------------------------------------------------


def canonical_json(doc: InstallationPolicyDocument | AssumeRolePolicyDocument) -> str:
    """Serialize a policy document to canonical JSON.

    Canonical form (design.md §6.5):
    * Field aliases applied (so ``for_all_values_string_equals`` emits as
      ``"ForAllValues:StringEquals"``).
    * Keys sorted lexicographically at every level.
    * No whitespace between tokens (``separators=(",", ":")``).
    * Fields whose value is ``None`` are omitted entirely (see below).

    The two-step ``json.loads(model.model_dump_json(by_alias=True, ...))``
    round-trip is deliberate: it produces a plain-Python object graph that
    :func:`json.dumps` can then re-emit under ``sort_keys=True``. A single
    ``model.model_dump_json(by_alias=True)`` call would honor Pydantic's
    declaration order rather than sorted order, which would break the
    byte-match against CDK synth (CDK's ``Template.from_stack`` output is
    itself normalized under ``sort_keys=True`` before comparison).

    ``exclude_none=True`` is required by the byte-match contract even though
    it deviates by three characters from the literal form quoted in design
    §6.5 point 7. CDK's ``aws_cdk.aws_iam.PolicyStatement`` renders JSON
    without a ``"Condition"`` key when no conditions are configured; the
    default Pydantic serialization of ``Condition: PolicyCondition | None =
    None`` would emit ``"Condition":null``, breaking the byte-match on
    statements 2 (S3) and 3 (Secrets Manager). ``exclude_none=True``
    suppresses only ``None``-valued fields — every non-optional field in
    the model hierarchy is required, so the flag has no effect elsewhere.

    Pure function — no IO, no side effects.
    """
    return json.dumps(
        json.loads(doc.model_dump_json(by_alias=True, exclude_none=True)),
        sort_keys=True,
        separators=(",", ":"),
    )
