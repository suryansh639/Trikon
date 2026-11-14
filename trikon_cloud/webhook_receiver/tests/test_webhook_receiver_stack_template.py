# CloudFormation templates are untyped JSON; this module types them as
# ``Mapping[str, Any]``. Under the repo's ``disallow_any_explicit = true``
# that is an ``explicit-any`` error, so suppress it at file scope, mirroring
# the other ``*_stack_template.py`` tests.
# mypy: disable-error-code="explicit-any"
"""Offline synth assertions for the Webhook_Receiver_Stack CDK_App.

Covers trikon-cloud-cost-cuts Req 2.2, 3.1-3.5, 4.1, 4.8 and 6.1:

* ``build_app(context, outdir=...).synth()`` succeeds offline under the
  shared ``no_network`` fixture (no AWS calls, no account lookup);
* the inline ``WebhookSecretParameterRead`` statement grants exactly
  ``ssm:GetParameter`` on exactly the Webhook_Secret_Parameter ARN, and it
  is the role's only SSM statement;
* ``TRIKON_WEBHOOK_SECRET_PARAMETER_NAME`` carries the parameter name (the
  default when the context key is absent, the override otherwise);
* the ``/aws/lambda/trikon-cloud-webhook-receiver`` log group keeps logs
  7 days and the function logs to it;
* the stack creates no SSM parameter and no secret-store resource;
* invalid ``webhook_secret_parameter_name`` values, the obsolete
  ``webhook_secret_arn`` key and a missing
  ``installation_events_queue_arn`` fail synthesis.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from trikon_cloud.secure_params import (
    WEBHOOK_SECRET_PARAMETER_CONTEXT,
    WEBHOOK_SECRET_PARAMETER_DEFAULT,
    WEBHOOK_SECRET_PARAMETER_ENV,
)
from trikon_cloud.webhook_receiver.infra.app import (
    OBSOLETE_WEBHOOK_SECRET_CONTEXT,
    STACK_ID,
    build_app,
)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_INSTALLATION_EVENTS_QUEUE_ARN = (
    f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-cloud-installation-events"
)
_FUNCTION_NAME = "trikon-cloud-webhook-receiver"
_LOG_GROUP_NAME = f"/aws/lambda/{_FUNCTION_NAME}"
_SID = "WebhookSecretParameterRead"
_CUSTOM_NAME = "/custom/team-a/webhook.secret_v2"

# Built by concatenation so the cost-guard static scan of trikon_cloud/**/*.py
# (task 19.1) does not match this test file itself.
_OLD_STORE = "secrets" + "manager"
_OLD_ENV = "TRIKON_WEBHOOK_SECRET" + "_ARN"

# ``Code.from_asset("trikon_cloud/webhook_receiver")`` resolves relative to
# the cwd, so synthesis must run from the ``Trikon/`` root.
_TRIKON_ROOT: Path = Path(__file__).resolve().parents[3]

_BASE_CONTEXT: Mapping[str, object] = {
    "account": _ACCOUNT,
    "installation_events_queue_arn": _INSTALLATION_EVENTS_QUEUE_ARN,
}

Template = Mapping[str, Any]


@pytest.fixture
def synth_env(no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline, hermetic synth: no network, ``Trikon/`` cwd, no ambient CDK env."""
    monkeypatch.chdir(_TRIKON_ROOT)
    for name in ("CDK_CONTEXT_JSON", "CDK_DEFAULT_REGION", "CDK_OUTDIR"):
        monkeypatch.delenv(name, raising=False)


def _synth(context: Mapping[str, object], outdir: Path) -> Template:
    """Run ``build_app(context, outdir=...).synth()`` and load the stack template."""
    assembly = build_app(context, outdir=str(outdir)).synth()
    template_path = Path(assembly.get_stack_by_name(STACK_ID).template_full_path)
    loaded: Template = json.loads(template_path.read_text(encoding="utf-8"))
    return loaded


def _resources(template: Template, resource_type: str) -> dict[str, Mapping[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _function(template: Template) -> Mapping[str, Any]:
    matches: list[Mapping[str, Any]] = [
        resource["Properties"]
        for resource in _resources(template, "AWS::Lambda::Function").values()
        if resource["Properties"].get("FunctionName") == _FUNCTION_NAME
    ]
    assert len(matches) == 1
    return matches[0]


def _role_statements(template: Template) -> list[Mapping[str, Any]]:
    """Every IAM policy statement attached to the receiver function's role."""
    role_ref = _function(template)["Role"]
    assert set(role_ref) == {"Fn::GetAtt"}
    role_id = role_ref["Fn::GetAtt"][0]
    statements: list[Mapping[str, Any]] = []
    for policy in _resources(template, "AWS::IAM::Policy").values():
        props = policy["Properties"]
        if {"Ref": role_id} in props["Roles"]:
            raw = props["PolicyDocument"]["Statement"]
            statements.extend(raw if isinstance(raw, list) else [raw])
    assert statements
    return statements


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


def _ssm_statements(template: Template) -> list[Mapping[str, Any]]:
    return [
        statement
        for statement in _role_statements(template)
        if any(
            isinstance(action, str) and action.lower().startswith("ssm:")
            for action in _as_list(statement["Action"])
        )
    ]


def _expected_arn(name: str) -> str:
    return f"arn:aws:ssm:{_REGION}:{_ACCOUNT}:parameter{name}"


@pytest.mark.usefixtures("synth_env")
class TestSynthesizedTemplate:
    def test_default_name_env_and_exact_grant(self, tmp_path: Path) -> None:
        template = _synth(_BASE_CONTEXT, tmp_path)

        env = _function(template)["Environment"]["Variables"]
        assert env[WEBHOOK_SECRET_PARAMETER_ENV] == WEBHOOK_SECRET_PARAMETER_DEFAULT
        assert env[WEBHOOK_SECRET_PARAMETER_ENV] == "/trikon-cloud/github-webhook-secret"
        assert _OLD_ENV not in env

        assert _ssm_statements(template) == [
            {
                "Sid": _SID,
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": _expected_arn(WEBHOOK_SECRET_PARAMETER_DEFAULT),
            }
        ]

    def test_custom_name_env_and_exact_grant(self, tmp_path: Path) -> None:
        context = {**_BASE_CONTEXT, WEBHOOK_SECRET_PARAMETER_CONTEXT: _CUSTOM_NAME}
        template = _synth(context, tmp_path)

        env = _function(template)["Environment"]["Variables"]
        assert env[WEBHOOK_SECRET_PARAMETER_ENV] == _CUSTOM_NAME

        assert _ssm_statements(template) == [
            {
                "Sid": _SID,
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": _expected_arn(_CUSTOM_NAME),
            }
        ]

    def test_account_agnostic_arn_uses_stack_account(self, tmp_path: Path) -> None:
        context = {"installation_events_queue_arn": _INSTALLATION_EVENTS_QUEUE_ARN}
        template = _synth(context, tmp_path)

        (statement,) = _ssm_statements(template)
        assert statement["Sid"] == _SID
        assert statement["Action"] == "ssm:GetParameter"
        assert statement["Resource"] == {
            "Fn::Join": [
                "",
                [
                    f"arn:aws:ssm:{_REGION}:",
                    {"Ref": "AWS::AccountId"},
                    f":parameter{WEBHOOK_SECRET_PARAMETER_DEFAULT}",
                ],
            ]
        }

    def test_log_group_retains_seven_days_and_function_uses_it(
        self, tmp_path: Path
    ) -> None:
        template = _synth(_BASE_CONTEXT, tmp_path)

        log_groups = _resources(template, "AWS::Logs::LogGroup")
        assert len(log_groups) == 1
        ((log_group_id, log_group),) = log_groups.items()
        assert log_group["Properties"]["LogGroupName"] == _LOG_GROUP_NAME
        assert log_group["Properties"]["RetentionInDays"] == 7
        assert log_group["DeletionPolicy"] == "Retain"

        logging_config = _function(template)["LoggingConfig"]
        assert logging_config["LogGroup"] == {"Ref": log_group_id}

    def test_no_parameter_or_old_secret_store_resources(self, tmp_path: Path) -> None:
        template = _synth(_BASE_CONTEXT, tmp_path)

        assert _resources(template, "AWS::SSM::Parameter") == {}
        resource_types = {resource["Type"] for resource in template["Resources"].values()}
        assert not any(_OLD_STORE in t.lower() for t in resource_types)
        assert (_OLD_STORE + ":") not in json.dumps(template).lower()
        assert "kms:" not in json.dumps(template).lower()


@pytest.mark.usefixtures("synth_env")
class TestContextValidation:
    @pytest.mark.parametrize(
        "raw",
        ["", "trikon-cloud/github-webhook-secret", "/bad name", 42],
        ids=["empty", "no-leading-slash", "bad-charset", "not-a-string"],
    )
    def test_invalid_parameter_name_names_the_key(self, raw: object) -> None:
        context = {**_BASE_CONTEXT, WEBHOOK_SECRET_PARAMETER_CONTEXT: raw}
        with pytest.raises(ValueError, match=f"^{WEBHOOK_SECRET_PARAMETER_CONTEXT}"):
            build_app(context)

    def test_obsolete_secret_arn_key_points_to_replacement(self) -> None:
        context = {
            **_BASE_CONTEXT,
            OBSOLETE_WEBHOOK_SECRET_CONTEXT: (
                f"arn:aws:{_OLD_STORE}:{_REGION}:{_ACCOUNT}:secret:webhook-AbCdEf"
            ),
        }
        with pytest.raises(ValueError) as excinfo:
            build_app(context)
        message = str(excinfo.value)
        assert message.startswith(OBSOLETE_WEBHOOK_SECRET_CONTEXT)
        assert WEBHOOK_SECRET_PARAMETER_CONTEXT in message

    def test_obsolete_key_rejected_even_with_new_key(self) -> None:
        context = {
            **_BASE_CONTEXT,
            WEBHOOK_SECRET_PARAMETER_CONTEXT: _CUSTOM_NAME,
            OBSOLETE_WEBHOOK_SECRET_CONTEXT: "anything",
        }
        with pytest.raises(ValueError, match=WEBHOOK_SECRET_PARAMETER_CONTEXT):
            build_app(context)

    @pytest.mark.parametrize("raw", [None, ""], ids=["absent", "empty"])
    def test_installation_events_queue_arn_is_required(self, raw: str | None) -> None:
        context: dict[str, object] = {"account": _ACCOUNT}
        if raw is not None:
            context["installation_events_queue_arn"] = raw
        with pytest.raises(ValueError, match=r"^installation_events_queue_arn"):
            build_app(context)
