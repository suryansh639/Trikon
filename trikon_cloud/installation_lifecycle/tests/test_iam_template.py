# The pydantic mypy plugin synthesizes ``__init__(self, **data: Any)`` on
# every :class:`BaseModel` subclass, and ``json.loads`` is typed as
# returning ``Any``. Under this repo's ``disallow_any_explicit = true``
# mypy config, both surface as ``explicit-any`` errors on code the plugin
# or the standard library generated. Silence at file scope — every
# ``Any`` here is bounded to library / stdlib boundary code, never
# crossed into a production module. Mirrors the pattern used by
# ``trikon_cloud/fargate_runner/tests/test_models.py``.
# mypy: disable-error-code="explicit-any"
"""Unit tests for :mod:`trikon_cloud.installation_lifecycle.iam_template`.

Covers the pure IAM policy renderer plus the :func:`canonical_json`
serializer. The renderer is the runtime counterpart of Spec 2's
CDK-side ``FargateRunnerStack.build_task_role_for_installation`` — the
byte-match property between the two sides (Property P10 in
``.kiro/specs/trikon-cloud-orchestrator/design.md`` §6.6) is enforced
by a separate contract test (``test_iam_policy_contract.py``, task
19). This suite pins the shape the renderer produces:

* Four ``Statement`` entries in the fixed order pinned by design §6.3
  (DynamoDB verdicts, DynamoDB pr_state, S3 evidence, SSM App key
  parameter).
* Each Statement's ``Action`` is a ``tuple[str, ...]`` on the Python
  side, but on the wire the ``@field_serializer`` on
  :class:`PolicyStatement` collapses single-element tuples to a bare
  string and emits multi-element tuples as a JSON list. This mirrors
  CDK's :class:`aws_cdk.aws_iam.PolicyStatement`, which emits the
  bare-string form for one-action statements and the list form for
  multi-action statements; matching that shape is what makes the P10
  byte-match hold. Concretely, statements 0 (``dynamodb:PutItem``)
  and 3 (``ssm:GetParameter``) serialize to strings;
  statements 1 (three DynamoDB actions) and 2 (``PutObject``,
  ``GetObject``) serialize to lists.
* ``Sid`` is a Python attribute on :class:`PolicyStatement` (design
  §6.3 pins the four Sid names and the structural tests below assert
  them via ``stmt.Sid``) but is ``Field(exclude=True)`` on the wire —
  Spec 2's ``FargateRunnerStack.build_task_role_for_installation``
  constructs each ``iam.PolicyStatement`` without ``sid=``, so CDK
  synth emits no ``"Sid"`` key and the runtime side drops it to
  preserve the byte-match.
* ``Condition`` on Statements 0 and 1 is a
  ``ForAllValues:StringEquals`` block whose ``dynamodb:LeadingKeys``
  entry is the LITERAL installation-id string (not the
  ``${aws:PrincipalTag/installation_id}`` template variable — that
  variant would fail the byte-match; see design §6.3 and the
  iam_template.py module docstring).
* Statements 2 and 3 have ``Condition=None`` — the scoping is baked
  into the S3 ``Resource`` ARN on Statement 2 and Statement 3 is
  unconditional read on the App_Key_Parameter ARN.

The :func:`canonical_json` tests pin the serializer's four canonical
properties: determinism, sorted keys with no whitespace, alias
serialization (``"ForAllValues:StringEquals"`` with the colon
preserved), and ``None``-suppression on the optional ``Condition``
field.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from trikon_cloud.installation_lifecycle.iam_template import (
    AssumeRolePolicyDocument,
    InstallationPolicyDocument,
    canonical_json,
    render_installation_assume_role_policy_document,
    render_installation_policy_document,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_ARN,
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    CANONICAL_INSTALLATION_ID,
)

# ---------------------------------------------------------------------------
# Canonical inputs — the same values the byte-match contract test uses.
# ---------------------------------------------------------------------------

_REGION = "us-east-1"
_EXPECTED_INSTALLATION_ID_STR = str(CANONICAL_INSTALLATION_ID)
_EXPECTED_VERDICTS_ARN = (
    f"arn:aws:dynamodb:{_REGION}:{CANONICAL_ACCOUNT_ID}:table/trikon_verdicts"
)
_EXPECTED_PR_STATE_ARN = (
    f"arn:aws:dynamodb:{_REGION}:{CANONICAL_ACCOUNT_ID}:table/trikon_pr_state"
)
_EXPECTED_S3_RESOURCE = (
    f"arn:aws:s3:::trikon-cloud-evidence/{_EXPECTED_INSTALLATION_ID_STR}/*"
)


def _make_policy() -> InstallationPolicyDocument:
    """Build the canonical policy document every test in this file reads."""
    return render_installation_policy_document(
        installation_id=CANONICAL_INSTALLATION_ID,
        app_private_key_parameter_name=CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
        account_id=CANONICAL_ACCOUNT_ID,
        region=_REGION,
    )


# ---------------------------------------------------------------------------
# render_installation_policy_document — structural pins.
# ---------------------------------------------------------------------------


def test_render_returns_four_statements_in_fixed_order() -> None:
    """The four Sids appear in the order pinned by design §6.3.

    Statement order is order-significant for the byte-match contract
    against CDK synth (design §6.5). A change here breaks the P10
    contract test; a change on the CDK side breaks the same test in
    the other direction.
    """
    doc = _make_policy()

    assert doc.Version == "2012-10-17"
    assert len(doc.Statement) == 4
    sids = [stmt.Sid for stmt in doc.Statement]
    assert sids == [
        "DynamoDBVerdictsScopedToInstallation",
        "DynamoDBPrStateScopedToInstallation",
        "S3EvidenceSpillScopedToInstallation",
        "SsmAppPrivateKeyRead",
    ]


def test_statement_0_dynamodb_verdicts_shape() -> None:
    """Statement 0: ``dynamodb:PutItem`` on ``trikon_verdicts``, LeadingKeys scoped.

    The ``Action`` field is a single-element ``tuple[str, ...]`` on the
    Python side — the ``@field_serializer`` on
    :class:`PolicyStatement` will collapse it to a bare string on the
    wire, matching CDK synth for single-action statements — and the
    ``LeadingKeys`` condition carries the LITERAL installation-id
    string per design §6.3.
    """
    stmt = _make_policy().Statement[0]

    assert stmt.Effect == "Allow"
    assert stmt.Action == ("dynamodb:PutItem",)
    assert stmt.Resource == _EXPECTED_VERDICTS_ARN
    assert stmt.Condition is not None
    assert stmt.Condition.for_all_values_string_equals == {
        "dynamodb:LeadingKeys": (_EXPECTED_INSTALLATION_ID_STR,)
    }


def test_statement_1_dynamodb_pr_state_shape() -> None:
    """Statement 1: three ``dynamodb:*`` actions on ``trikon_pr_state``.

    Same LeadingKeys condition value as Statement 0 — the renderer
    reuses one :class:`PolicyCondition` instance across both, per the
    iam_template.py implementation. The three actions appear in the
    ``(GetItem, PutItem, UpdateItem)`` order pinned by design §6.3.
    """
    stmt = _make_policy().Statement[1]

    assert stmt.Effect == "Allow"
    assert stmt.Action == (
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    )
    assert stmt.Resource == _EXPECTED_PR_STATE_ARN
    assert stmt.Condition is not None
    assert stmt.Condition.for_all_values_string_equals == {
        "dynamodb:LeadingKeys": (_EXPECTED_INSTALLATION_ID_STR,)
    }


def test_statement_2_s3_evidence_spill_shape() -> None:
    """Statement 2: S3 ``PutObject``/``GetObject`` with per-installation prefix.

    ``Condition`` is ``None`` — the scoping is baked directly into
    the ``Resource`` ARN via the LITERAL installation-id string, per
    the note in design §6.3 and the CDK-side implementation at
    ``fargate_runner_stack.py`` lines 365-370.
    """
    stmt = _make_policy().Statement[2]

    assert stmt.Effect == "Allow"
    assert stmt.Action == ("s3:PutObject", "s3:GetObject")
    assert stmt.Resource == _EXPECTED_S3_RESOURCE
    assert stmt.Condition is None


def test_statement_3_ssm_get_parameter_on_app_key_parameter_arn() -> None:
    """Statement 3: ``ssm:GetParameter`` on exactly the App_Key_Parameter ARN.

    Cost-cuts Req 4.4: one action, one resource (the Parameter_ARN built
    from the passed name, account and region), no ``Condition`` and no
    ``kms:Decrypt`` statement anywhere in the document.
    """
    doc = _make_policy()
    stmt = doc.Statement[3]

    assert stmt.Sid == "SsmAppPrivateKeyRead"
    assert stmt.Effect == "Allow"
    assert stmt.Action == ("ssm:GetParameter",)
    assert stmt.Resource == CANONICAL_APP_PRIVATE_KEY_PARAMETER_ARN
    assert stmt.Condition is None
    all_actions = [action for s in doc.Statement for action in s.Action]
    assert not any(action.startswith("kms:") for action in all_actions)


def test_statement_3_uses_custom_parameter_name_account_and_region() -> None:
    """A non-default name, account and region all land in statement 3's ARN."""
    stmt = render_installation_policy_document(
        CANONICAL_INSTALLATION_ID,
        app_private_key_parameter_name="/acme/prod/app.key_v2",
        account_id="123456789012",
        region="eu-west-1",
    ).Statement[3]

    assert stmt.Resource == "arn:aws:ssm:eu-west-1:123456789012:parameter/acme/prod/app.key_v2"


# ---------------------------------------------------------------------------
# render_installation_assume_role_policy_document — fixed shape.
# ---------------------------------------------------------------------------


def test_assume_role_policy_document_fixed_shape() -> None:
    """The assume-role policy is identical for every installation.

    Single statement, ``Effect=Allow``, ``Principal=ecs-tasks``,
    ``Action=sts:AssumeRole`` — passed verbatim to
    :meth:`iam_client.create_role` via :func:`canonical_json`.
    """
    doc: AssumeRolePolicyDocument = render_installation_assume_role_policy_document()

    assert doc.Version == "2012-10-17"
    assert len(doc.Statement) == 1
    stmt = doc.Statement[0]
    assert stmt.Effect == "Allow"
    assert stmt.Principal == {"Service": "ecs-tasks.amazonaws.com"}
    assert stmt.Action == "sts:AssumeRole"


# ---------------------------------------------------------------------------
# canonical_json — determinism, sorted keys, no whitespace, alias.
# ---------------------------------------------------------------------------


def test_canonical_json_is_deterministic_across_calls() -> None:
    """Same input → byte-identical output across N invocations.

    Determinism is a prerequisite for the byte-match contract:
    :func:`aws_cdk.assertions.Template.from_stack` output is
    normalized under ``sort_keys=True`` before comparison, so
    :func:`canonical_json` MUST produce the same string every time it
    is called on structurally-equal inputs.
    """
    doc = _make_policy()

    outputs = {canonical_json(doc) for _ in range(5)}

    assert len(outputs) == 1


def test_canonical_json_has_sorted_keys_and_no_whitespace() -> None:
    """The output uses ``separators=(",", ":")`` and sorted keys.

    Two structural properties in one test because they are the same
    property from different angles: canonical JSON has no
    inter-token whitespace (``", "`` and ``": "`` must be absent) and
    every object's keys appear in lexicographic order at every
    nesting depth.
    """
    output = canonical_json(_make_policy())

    # No whitespace between tokens.
    assert ": " not in output
    assert ", " not in output

    # Keys sorted at every level: the top-level object must start with
    # ``Statement`` before ``Version`` (S < V lexicographically). A
    # separate parse without ``object_pairs_hook`` gives us plain
    # dicts for the nested-statement key-order check below.
    top_level_pairs = json.loads(output, object_pairs_hook=list)
    top_level_keys = [k for k, _ in top_level_pairs]
    assert top_level_keys == sorted(top_level_keys)

    parsed = json.loads(output)
    for stmt in parsed["Statement"]:
        stmt_keys = list(stmt.keys())
        assert stmt_keys == sorted(stmt_keys)


def test_canonical_json_omits_none_condition_field() -> None:
    """Statements with ``Condition=None`` do NOT emit ``"Condition":null``.

    :func:`canonical_json` calls :meth:`model_dump_json` with
    ``exclude_none=True`` because CDK's
    :class:`aws_cdk.aws_iam.PolicyStatement` renders JSON without a
    ``"Condition"`` key when no conditions are configured. Emitting
    ``"Condition":null`` would break the byte-match on Statements 2
    (S3) and 3 (SSM) — see the iam_template.py docstring
    on :func:`canonical_json` for the full rationale.
    """
    output = canonical_json(_make_policy())

    assert '"Condition":null' not in output

    # Statements 0 and 1 DO carry a Condition and it must appear.
    parsed = json.loads(output)
    statements = parsed["Statement"]
    assert "Condition" in statements[0]
    assert "Condition" in statements[1]
    assert "Condition" not in statements[2]
    assert "Condition" not in statements[3]


def test_canonical_json_serializes_alias_key_with_colon() -> None:
    """The Pydantic alias ``"ForAllValues:StringEquals"`` survives to JSON.

    :class:`PolicyCondition` declares
    ``for_all_values_string_equals: dict[str, tuple[str, ...]] =
    Field(alias="ForAllValues:StringEquals")`` and
    :func:`canonical_json` passes ``by_alias=True``. The colon in the
    alias key is IAM-native syntax; a plain snake_case emission would
    break the byte-match against CDK synth.
    """
    output = canonical_json(_make_policy())

    # The colon-carrying alias appears verbatim.
    assert '"ForAllValues:StringEquals":' in output

    # The Python-side snake_case name does NOT leak to the wire.
    assert "for_all_values_string_equals" not in output

    # And the nested ``dynamodb:LeadingKeys`` sub-key is present with
    # the LITERAL installation-id string (not the ``${aws:...}``
    # template variable — that variant would fail the byte-match).
    assert '"dynamodb:LeadingKeys":' in output
    assert f'"{_EXPECTED_INSTALLATION_ID_STR}"' in output


def test_canonical_json_round_trip_produces_four_statements() -> None:
    """The output is valid JSON that parses back to four statements.

    Structural round-trip: serialize → parse → inspect. Anchors the
    ``canonical_json`` output against the same four-statement shape
    the structural tests above pin on the Pydantic model, closing
    the loop between the model form and the wire form.

    ``Sid`` is asserted on the Python-side model (``stmt.Sid``)
    rather than the wire: :class:`PolicyStatement` declares
    ``Sid: str = Field(exclude=True)`` because Spec 2's CDK
    ``iam.PolicyStatement`` calls do not pass ``sid=``, so CDK synth
    emits no ``"Sid"`` key and the runtime side drops it to
    byte-match.

    ``Action`` shape on the wire is arity-dependent: statements 0
    (``dynamodb:PutItem``) and 3 (``ssm:GetParameter``)
    are single-action tuples that the
    :meth:`PolicyStatement._serialize_action` field serializer
    collapses to bare strings, while statements 1 (three DynamoDB
    actions) and 2 (``PutObject``, ``GetObject``) stay as JSON
    lists — the exact shape CDK synth emits for the same inputs.
    """
    doc = _make_policy()
    output = canonical_json(doc)

    parsed = json.loads(output)

    assert parsed["Version"] == "2012-10-17"
    assert len(parsed["Statement"]) == 4

    # Sid: assert on the Python-side model, not the wire.
    assert [stmt.Sid for stmt in doc.Statement] == [
        "DynamoDBVerdictsScopedToInstallation",
        "DynamoDBPrStateScopedToInstallation",
        "S3EvidenceSpillScopedToInstallation",
        "SsmAppPrivateKeyRead",
    ]
    # ...and confirm it does NOT appear on the wire.
    for stmt in parsed["Statement"]:
        assert "Sid" not in stmt

    # Action shape on the wire: bare string for single-action
    # statements (0 and 3), JSON list for multi-action statements
    # (1 and 2). Matches CDK synth's arity-dependent emission.
    assert isinstance(parsed["Statement"][0]["Action"], str)
    assert isinstance(parsed["Statement"][1]["Action"], list)
    assert isinstance(parsed["Statement"][2]["Action"], list)
    assert isinstance(parsed["Statement"][3]["Action"], str)


# ---------------------------------------------------------------------------
# Property 2 (renderer half): the App-key statement uses the Parameter_ARN.
# The parameter_arn half lives in trikon_cloud/tests/test_secure_params.py.
# ---------------------------------------------------------------------------

# Valid SSM parameter names: "/" followed by 1-1010 characters of the SSM
# name character set (the same set parameter_name_from_context accepts).
_PARAMETER_NAMES = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/",
    min_size=1,
    max_size=1010,
).map(lambda tail: "/" + tail)
_ACCOUNTS = st.text(alphabet="0123456789", min_size=12, max_size=12)
_REGIONS = st.from_regex(r"[a-z]{2}(-gov)?-[a-z]{4,9}-[1-9]", fullmatch=True)


# Feature: trikon-cloud-cost-cuts, Property 2: App-key statement uses the Parameter_ARN
@given(
    installation_id=st.integers(min_value=1, max_value=2**63 - 1),
    name=_PARAMETER_NAMES,
    account=_ACCOUNTS,
    region=_REGIONS,
)
@settings(max_examples=200, deadline=None)
def test_property_app_key_statement_uses_parameter_arn(
    installation_id: int, name: str, account: str, region: str
) -> None:
    """**Validates: Requirements 4.4, 6.1**

    For any installation id >= 1, valid parameter name, 12-digit account
    and region, statement 3 is ``ssm:GetParameter`` on exactly
    ``arn:aws:ssm:{region}:{account}:parameter{name}``, on the model and on
    the wire, and the canonical JSON carries no Secrets Manager action.
    """
    doc = render_installation_policy_document(
        installation_id,
        app_private_key_parameter_name=name,
        account_id=account,
        region=region,
    )
    expected_arn = f"arn:aws:ssm:{region}:{account}:parameter{name}"

    stmt = doc.Statement[3]
    assert stmt.Sid == "SsmAppPrivateKeyRead"
    assert stmt.Effect == "Allow"
    assert stmt.Action == ("ssm:GetParameter",)
    assert stmt.Resource == expected_arn
    assert stmt.Condition is None

    output = canonical_json(doc)
    wire_stmt = json.loads(output)["Statement"][3]
    assert wire_stmt == {
        "Action": "ssm:GetParameter",
        "Effect": "Allow",
        "Resource": expected_arn,
    }
    # Built by concatenation so the Req 6.1 static scan (task 19.1) does
    # not flag this negative assertion as a live reference.
    assert "secrets" + "manager:" not in output
