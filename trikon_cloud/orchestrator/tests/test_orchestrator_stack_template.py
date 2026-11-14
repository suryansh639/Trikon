# CDK's jsii-generated stubs type ``Template.find_resources`` payloads as
# ``Mapping[str, Mapping[str, Any]]``. Under this repo's
# ``disallow_any_explicit = true`` mypy config those surface as
# ``explicit-any`` errors on library code we do not own. Suppress at file
# scope, mirroring ``test_fargate_runner_stack_template.py``.
# mypy: disable-error-code="explicit-any"
"""Offline synth assertions for the Orchestrator_Stack CDK_App.

Every template is produced by ``build_app(context, outdir=...).synth()``
under the shared ``no_network`` fixture (cost-cuts Req 1.3), so the stack
is tested in its deploy shape: region pinned to ``us-east-1`` and the
account left to CloudFormation (``Ref: AWS::AccountId``).

Covers sandbox-minute-plans design.md §11 (Req 11.14, 11.16, 11.18, 11.19,
12.1, 12.9) and trikon-cloud-cost-cuts Req 2.3, 2.4, 3.1-3.5, 4.2, 4.3,
4.7, 4.8 and 6.1:

* the orchestrator env (Usage_API vars, both parameter names and
  ``TRIKON_VERIFY_JOBS_QUEUE_URL``) and its 90 s timeout;
* ``sqs:SendMessage`` on the orchestrator role covers exactly the
  ``trikon-verify-jobs`` ARN (busy re-enqueue, Req 11.19) and the existing
  ``trikon-verify-jobs-dlq`` grant, no other queue, and no KMS action;
* ``SecureParameterRead`` grants exactly ``ssm:GetParameter`` on exactly
  the App key and Usage_Secret Parameter_ARNs, and the active-revision
  ``grant_read`` stays a separate statement on its own parameter;
* the Usage_Reporter's ``UsageSecretParameterRead`` statement, function,
  async-invoke config and DLQ;
* the installation-lifecycle Lambda gets the App key parameter name but its
  role has no SSM grant;
* explicit 7-day ``/aws/lambda/<fn>`` log groups wired through
  ``LoggingConfig``;
* no secret-store resource or action and no created SSM parameter;
* the ``trikon-verify-runner-task-stopped`` EventBridge rule's pattern and
  target retry / DLQ settings;
* CDK context validation: parameter-name defaults, invalid names and the
  obsolete ``*_secret_arn`` keys;
* Fargate Spot (cost-cuts Req 2.5, 4.7, 8.2, 9.1, 9.7): the orchestrator's
  Spot env and ``OrchestratorPrStateRead``, the ``trikon-cloud-spot-retry``
  Lambda, its exact role statements, DLQ, log group and claim table, the
  rule's second target, and the ``use_fargate_spot`` context key.

No AWS credentials, no network, no ``moto``.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Template
from pydantic_settings import BaseSettings

from trikon_cloud.installation_lifecycle.models import LifecycleEnvConfig
from trikon_cloud.orchestrator.infra.app import (
    OBSOLETE_APP_KEY_CONTEXT,
    OBSOLETE_USAGE_SECRET_CONTEXT,
    STACK_ID,
    USE_FARGATE_SPOT_CONTEXT,
    build_app,
)
from trikon_cloud.orchestrator.infra.orchestrator_stack import (
    ORCHESTRATOR_PR_STATE_READ_SID,
    SECURE_PARAMETER_READ_SID,
    SPOT_RETRY_APP_KEY_PARAMETER_READ_SID,
    SPOT_RETRY_CLAIMS_SID,
    SPOT_RETRY_PR_STATE_READ_SID,
    SPOT_RETRY_SEND_RETRY_JOB_SID,
    SPOT_RETRY_VERDICT_WRITE_SID,
    USAGE_SECRET_PARAMETER_READ_SID,
)
from trikon_cloud.orchestrator.models import OrchestratorEnvConfig, SpotRetryEnvConfig
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_CONTEXT,
    APP_KEY_PARAMETER_DEFAULT,
    APP_KEY_PARAMETER_ENV,
    USAGE_SECRET_PARAMETER_CONTEXT,
    USAGE_SECRET_PARAMETER_DEFAULT,
    USAGE_SECRET_PARAMETER_ENV,
)
from trikon_cloud.usage_reporter.models import UsageReporterEnvConfig
from trikon_cloud.webhook_receiver.infra.webhook_receiver_stack import (
    VERIFY_JOBS_MAX_RECEIVE_COUNT,
)

from .conftest import (
    CANONICAL_ACCOUNT_ID,
    CANONICAL_APP_ID,
    CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    CANONICAL_USAGE_SECRET_PARAMETER_NAME,
    CANONICAL_VERIFY_JOBS_QUEUE_URL,
    CANONICAL_WEB_API_URL,
    CANONICAL_WEB_APP_URL,
)

_TEST_REGION: str = "us-east-1"
_VERIFY_JOBS_ARN: str = (
    f"arn:aws:sqs:{_TEST_REGION}:{CANONICAL_ACCOUNT_ID}:trikon-verify-jobs"
)
_VERIFY_JOBS_DLQ_ARN: str = (
    f"arn:aws:sqs:{_TEST_REGION}:{CANONICAL_ACCOUNT_ID}:trikon-verify-jobs-dlq"
)
_ACTIVE_REVISION_PARAM: str = "/trikon/verify-runner/active-revision"
_ORCHESTRATOR_ROLE_NAME: str = "trikon-cloud-orchestrator-role"
_LIFECYCLE_ROLE_NAME: str = "trikon-cloud-installation-lifecycle-role"
_USAGE_REPORTER_ROLE_NAME: str = "trikon-cloud-usage-reporter-role"
_ORCHESTRATOR_FN_NAME: str = "trikon-cloud-orchestrator"
_LIFECYCLE_FN_NAME: str = "trikon-cloud-installation-lifecycle"
_USAGE_REPORTER_FN_NAME: str = "trikon-cloud-usage-reporter"
_SPOT_RETRY_ROLE_NAME: str = "trikon-cloud-spot-retry-role"
_SPOT_RETRY_FN_NAME: str = "trikon-cloud-spot-retry"
_FUNCTION_NAMES: tuple[str, ...] = (
    _ORCHESTRATOR_FN_NAME,
    _LIFECYCLE_FN_NAME,
    _USAGE_REPORTER_FN_NAME,
    _SPOT_RETRY_FN_NAME,
)
_USAGE_DLQ_NAME: str = "trikon-cloud-usage-reporter-dlq"
_SPOT_RETRY_DLQ_NAME: str = "trikon-cloud-spot-retry-dlq"
_SPOT_RETRIES_TABLE_NAME: str = "trikon-cloud-spot-retries"
_RULE_NAME: str = "trikon-verify-runner-task-stopped"

# Built by concatenation so the cost-guard static scan of trikon_cloud/**/*.py
# (task 19.1) does not match this test file itself.
_OLD_STORE: str = "secrets" + "manager"
_OLD_ENV_SUFFIX: str = "_SECRET" + "_ARN"

# The repo root (``Trikon/``): ``Code.from_asset("trikon_cloud")`` resolves
# relative to the cwd, so synthesis must run from here.
_TRIKON_ROOT: Path = Path(__file__).resolve().parents[3]

# Required context plus explicit (non-default) parameter names.
_REQUIRED_CONTEXT: Mapping[str, object] = {
    "app_id": CANONICAL_APP_ID,
    "verify_jobs_queue_arn": _VERIFY_JOBS_ARN,
    "verify_jobs_dlq_arn": _VERIFY_JOBS_DLQ_ARN,
    "runner_subnet_ids": "subnet-a,subnet-b",
    "runner_security_group_ids": "sg-verify",
    "trikon_web_api_url": CANONICAL_WEB_API_URL,
    "trikon_web_app_url": CANONICAL_WEB_APP_URL,
}
_BASE_CONTEXT: Mapping[str, object] = {
    **_REQUIRED_CONTEXT,
    APP_KEY_PARAMETER_CONTEXT: CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
    USAGE_SECRET_PARAMETER_CONTEXT: CANONICAL_USAGE_SECRET_PARAMETER_NAME,
}

Statement = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Synthesis fixtures
# ---------------------------------------------------------------------------


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
    loaded: Mapping[str, Any] = json.loads(template_path.read_text(encoding="utf-8"))
    return Template.from_json(loaded)


@pytest.fixture(scope="module")
def _synth_cache() -> dict[str, Template]:
    return {}


@pytest.fixture
def template(
    synth_env: None,
    _synth_cache: dict[str, Template],
    tmp_path_factory: pytest.TempPathFactory,
) -> Template:
    """The ``_BASE_CONTEXT`` template, synthesized once per module under ``no_network``.

    ``no_network`` is function-scoped, so the cache lives in a module-scoped
    fixture and the first test to ask synthesizes inside its own guard.
    """
    if "base" not in _synth_cache:
        _synth_cache["base"] = _synth(
            _BASE_CONTEXT, tmp_path_factory.mktemp("orchestrator-synth")
        )
    return _synth_cache["base"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _single(
    template: Template, resource_type: str, properties: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    """Return ``(logical_id, resource)`` of the one resource matching ``properties``."""
    found: Mapping[str, Mapping[str, Any]] = template.find_resources(
        resource_type, {"Properties": properties}
    )
    assert len(found) == 1, (
        f"expected exactly one {resource_type} matching {properties!r}; "
        f"found {len(found)}"
    )
    ((logical_id, resource),) = found.items()
    return logical_id, resource


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


def _role_statements(template: Template, role_name: str) -> list[Statement]:
    """Flatten every IAM policy statement attached to the role named ``role_name``.

    ``Action`` and ``Resource`` are normalized to lists.
    """
    role_id, _ = _single(template, "AWS::IAM::Role", {"RoleName": role_name})
    policies: Mapping[str, Mapping[str, Any]] = template.find_resources(
        "AWS::IAM::Policy"
    )
    statements: list[Statement] = []
    for policy in policies.values():
        props = policy["Properties"]
        if {"Ref": role_id} not in props.get("Roles", []):
            continue
        for raw in _as_list(props["PolicyDocument"]["Statement"]):
            assert isinstance(raw, Mapping)
            statement = dict(raw)
            statement["Action"] = _as_list(statement.get("Action", []))
            statement["Resource"] = _as_list(statement.get("Resource", []))
            statements.append(statement)
    assert statements, f"no IAM policy statements found for role {role_name!r}"
    return statements


def _grants_action(statement: Statement, action: str) -> bool:
    """True when an Allow ``statement`` covers ``action`` (wildcards included)."""
    if statement.get("Effect") != "Allow":
        return False
    return any(
        isinstance(pattern, str)
        and fnmatch.fnmatchcase(action.lower(), pattern.lower())
        for pattern in statement["Action"]
    )


def _ssm_statements(statements: Sequence[Statement]) -> list[Statement]:
    """Every statement with an ``ssm:`` action (or a ``*`` wildcard action)."""
    return [
        s
        for s in statements
        if any(
            isinstance(a, str) and (a == "*" or a.lower().startswith("ssm:"))
            for a in s["Action"]
        )
    ]


def _resource_key(resource: object) -> str:
    """Hashable form of a Resource entry; intrinsics are JSON-encoded."""
    return resource if isinstance(resource, str) else json.dumps(resource, sort_keys=True)


def _send_message_resources(statements: Sequence[Statement]) -> set[str]:
    """Every Resource on a statement that allows ``sqs:SendMessage``."""
    return {
        _resource_key(resource)
        for statement in statements
        if _grants_action(statement, "sqs:SendMessage")
        for resource in statement["Resource"]
    }


def _parameter_arn(name: str) -> Mapping[str, Any]:
    """The Parameter_ARN as synthesized for the account-agnostic ``us-east-1`` stack."""
    return {
        "Fn::Join": [
            "",
            [
                f"arn:aws:ssm:{_TEST_REGION}:",
                {"Ref": "AWS::AccountId"},
                f":parameter{name}",
            ],
        ]
    }


def _resolve_join(value: object) -> object:
    """Resolve a ``Fn::Join`` whose only intrinsic is ``Ref: AWS::URLSuffix``."""
    if not isinstance(value, Mapping) or "Fn::Join" not in value:
        return value
    separator, parts = value["Fn::Join"]
    rendered: list[str] = []
    for part in parts:
        if part == {"Ref": "AWS::URLSuffix"}:
            rendered.append("amazonaws.com")
        else:
            assert isinstance(part, str), f"unexpected intrinsic in Fn::Join: {part!r}"
            rendered.append(part)
    return str(separator).join(rendered)


def _function(template: Template, name: str) -> tuple[str, Mapping[str, Any]]:
    logical_id, resource = _single(
        template, "AWS::Lambda::Function", {"FunctionName": name}
    )
    return logical_id, resource["Properties"]


def _env(template: Template, name: str) -> Mapping[str, Any]:
    _, props = _function(template, name)
    env: Mapping[str, Any] = props["Environment"]["Variables"]
    return env


def _queue_arn_ref(template: Template, queue_name: str) -> Mapping[str, Any]:
    logical_id, _ = _single(template, "AWS::SQS::Queue", {"QueueName": queue_name})
    return {"Fn::GetAtt": [logical_id, "Arn"]}


def _usage_dlq_arn_ref(template: Template) -> Mapping[str, Any]:
    return _queue_arn_ref(template, _USAGE_DLQ_NAME)


def _spot_retry_dlq_arn_ref(template: Template) -> Mapping[str, Any]:
    return _queue_arn_ref(template, _SPOT_RETRY_DLQ_NAME)


def _spot_retries_table(template: Template) -> tuple[str, Mapping[str, Any]]:
    """``TableV2`` synthesizes ``AWS::DynamoDB::GlobalTable``."""
    return _single(
        template,
        "AWS::DynamoDB::GlobalTable",
        {"TableName": _SPOT_RETRIES_TABLE_NAME},
    )


def _fleet_table_arn(table: str) -> Mapping[str, Any]:
    """A fleet ``trikon_*`` table ARN as synthesized for the ``us-east-1`` stack."""
    return {
        "Fn::Join": [
            "",
            [
                f"arn:aws:dynamodb:{_TEST_REGION}:",
                {"Ref": "AWS::AccountId"},
                f":table/{table}",
            ],
        ]
    }


def _pr_state_read(sid: str) -> Statement:
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Action": ["dynamodb:GetItem"],
        "Resource": [_fleet_table_arn("trikon_pr_state")],
    }


def _canonical(statements: Sequence[Statement]) -> list[str]:
    """Order-insensitive form of a statement list, for exact comparisons."""
    return sorted(json.dumps(s, sort_keys=True) for s in statements)


def _secure_parameter_read(app_key_name: str, usage_name: str) -> Statement:
    return {
        "Sid": SECURE_PARAMETER_READ_SID,
        "Effect": "Allow",
        "Action": ["ssm:GetParameter"],
        "Resource": [_parameter_arn(app_key_name), _parameter_arn(usage_name)],
    }


def _usage_secret_parameter_read(usage_name: str) -> Statement:
    return {
        "Sid": USAGE_SECRET_PARAMETER_READ_SID,
        "Effect": "Allow",
        "Action": ["ssm:GetParameter"],
        "Resource": [_parameter_arn(usage_name)],
    }


# ---------------------------------------------------------------------------
# Orchestrator Lambda (Req 11.14, 11.18; cost-cuts Req 3.1, 3.4)
# ---------------------------------------------------------------------------


class TestOrchestratorFunction:
    def test_timeout_is_90_seconds(self, template: Template) -> None:
        _, props = _function(template, _ORCHESTRATOR_FN_NAME)
        assert props["Timeout"] == 90

    def test_usage_api_env(self, template: Template) -> None:
        env = _env(template, _ORCHESTRATOR_FN_NAME)
        assert env["TRIKON_WEB_API_URL"] == CANONICAL_WEB_API_URL
        assert env["TRIKON_WEB_APP_URL"] == CANONICAL_WEB_APP_URL

    def test_parameter_name_env(self, template: Template) -> None:
        env = _env(template, _ORCHESTRATOR_FN_NAME)
        assert env[APP_KEY_PARAMETER_ENV] == CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME
        assert env[USAGE_SECRET_PARAMETER_ENV] == CANONICAL_USAGE_SECRET_PARAMETER_NAME
        assert not [k for k in env if k.endswith(_OLD_ENV_SUFFIX)]

    def test_verify_jobs_queue_url_env(self, template: Template) -> None:
        env = _env(template, _ORCHESTRATOR_FN_NAME)
        assert (
            _resolve_join(env["TRIKON_VERIFY_JOBS_QUEUE_URL"])
            == CANONICAL_VERIFY_JOBS_QUEUE_URL
        )

    def test_model_defaults_are_left_unset(self, template: Template) -> None:
        env = _env(template, _ORCHESTRATOR_FN_NAME)
        assert "TRIKON_QUEUE_WAIT_LIMIT_SECONDS" not in env
        assert "TRIKON_QUOTA_TIME_BUDGET_SECONDS" not in env

    def test_spot_env(self, template: Template) -> None:
        """Spot_Flag defaults on; pr-state table and redrive count are wired (Req 8.2)."""
        env = _env(template, _ORCHESTRATOR_FN_NAME)
        assert env["TRIKON_USE_FARGATE_SPOT"] == "true"
        assert env["TRIKON_PR_STATE_TABLE"] == "trikon_pr_state"
        assert env["TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT"] == str(
            VERIFY_JOBS_MAX_RECEIVE_COUNT
        )


def test_verify_jobs_redrive_count_matches_the_model_default() -> None:
    """The shared constant is the model's documented default (Req 11.5)."""
    field = OrchestratorEnvConfig.model_fields["trikon_verify_jobs_max_receive_count"]
    assert field.default == VERIFY_JOBS_MAX_RECEIVE_COUNT


@pytest.mark.parametrize(
    ("function_name", "model"),
    [
        (_ORCHESTRATOR_FN_NAME, OrchestratorEnvConfig),
        (_LIFECYCLE_FN_NAME, LifecycleEnvConfig),
        (_USAGE_REPORTER_FN_NAME, UsageReporterEnvConfig),
        (_SPOT_RETRY_FN_NAME, SpotRetryEnvConfig),
    ],
    ids=["orchestrator", "installation-lifecycle", "usage-reporter", "spot-retry"],
)
def test_env_sets_every_required_model_alias(
    template: Template, function_name: str, model: type[BaseSettings]
) -> None:
    """Each Lambda's env carries every alias its settings model requires.

    Guards against a stack/model drift such as the lifecycle Lambda still
    receiving the removed App-key env var after its model moved to the
    parameter name.
    """
    env = _env(template, function_name)
    required = {
        field.alias
        for field in model.model_fields.values()
        if field.is_required() and field.alias is not None
    }
    assert required, f"{model.__name__} has no required aliases"
    assert required - set(env) == set()


# ---------------------------------------------------------------------------
# Orchestrator role (Req 11.16, 11.18, 11.19; cost-cuts Req 4.2, 4.8)
# ---------------------------------------------------------------------------


class TestOrchestratorRole:
    def test_send_message_on_exactly_verify_jobs_and_its_dlq(
        self, template: Template
    ) -> None:
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        assert _send_message_resources(statements) == {
            _VERIFY_JOBS_ARN,
            _VERIFY_JOBS_DLQ_ARN,
        }

    def test_helper_catches_missing_verify_jobs_grant(
        self, template: Template
    ) -> None:
        """Self-check: dropping the busy re-enqueue grant must break the check above."""
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        without_requeue = [
            s
            for s in statements
            if not (
                _grants_action(s, "sqs:SendMessage")
                and s["Resource"] == [_VERIFY_JOBS_ARN]
            )
        ]
        assert len(without_requeue) == len(statements) - 1
        assert _send_message_resources(without_requeue) == {_VERIFY_JOBS_DLQ_ARN}
        assert _send_message_resources(without_requeue) != {
            _VERIFY_JOBS_ARN,
            _VERIFY_JOBS_DLQ_ARN,
        }

    def test_secure_parameter_read_is_exact(self, template: Template) -> None:
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        secure = [s for s in statements if s.get("Sid") == SECURE_PARAMETER_READ_SID]
        assert secure == [
            _secure_parameter_read(
                CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
                CANONICAL_USAGE_SECRET_PARAMETER_NAME,
            )
        ]

    def test_active_revision_grant_is_separate(self, template: Template) -> None:
        """The only other SSM statement is ``grant_read`` on the plain String param."""
        statements = _ssm_statements(
            _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        )
        others = [s for s in statements if s.get("Sid") != SECURE_PARAMETER_READ_SID]
        assert len(others) == 1
        (active_revision,) = others
        assert "ssm:GetParameter" in active_revision["Action"]
        assert len(active_revision["Resource"]) == 1
        rendered = json.dumps(active_revision["Resource"][0])
        assert f":parameter{_ACTIVE_REVISION_PARAM}" in rendered
        assert CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME not in rendered
        assert CANONICAL_USAGE_SECRET_PARAMETER_NAME not in rendered

    def test_secure_parameters_only_reachable_through_get_parameter(
        self, template: Template
    ) -> None:
        """No other statement (e.g. a ``grant_read``) names a Secure_Parameter."""
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        for statement in statements:
            if statement.get("Sid") == SECURE_PARAMETER_READ_SID:
                continue
            rendered = json.dumps(statement["Resource"])
            assert CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME not in rendered
            assert CANONICAL_USAGE_SECRET_PARAMETER_NAME not in rendered

    def test_pr_state_read_is_exact(self, template: Template) -> None:
        """``OrchestratorPrStateRead`` is ``dynamodb:GetItem`` on ``trikon_pr_state`` only."""
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        assert [s for s in statements if s.get("Sid") == ORCHESTRATOR_PR_STATE_READ_SID] == [
            _pr_state_read(ORCHESTRATOR_PR_STATE_READ_SID)
        ]

    def test_no_kms_action(self, template: Template) -> None:
        statements = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        actions = [a for s in statements for a in s["Action"]]
        offending = [
            a
            for a in actions
            if not isinstance(a, str) or a == "*" or a.lower().startswith("kms:")
        ]
        assert offending == [], f"unexpected KMS/wildcard actions: {offending!r}"


# ---------------------------------------------------------------------------
# Installation_Lifecycle (cost-cuts Req 3.4, 4.7)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_env_carries_app_key_parameter_name(self, template: Template) -> None:
        env = _env(template, _LIFECYCLE_FN_NAME)
        assert env[APP_KEY_PARAMETER_ENV] == CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME
        assert USAGE_SECRET_PARAMETER_ENV not in env
        assert not [k for k in env if k.endswith(_OLD_ENV_SUFFIX)]

    def test_role_has_no_ssm_grant(self, template: Template) -> None:
        statements = _role_statements(template, _LIFECYCLE_ROLE_NAME)
        assert _ssm_statements(statements) == []
        rendered = json.dumps(statements)
        assert CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME not in rendered
        assert ":parameter/" not in rendered


# ---------------------------------------------------------------------------
# Usage_Reporter (Req 11.16, 12.1, 12.9; cost-cuts Req 4.3)
# ---------------------------------------------------------------------------


class TestUsageReporter:
    def test_function_settings(self, template: Template) -> None:
        _, props = _function(template, _USAGE_REPORTER_FN_NAME)
        assert props["Runtime"] == "python3.11"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 256
        assert props["Timeout"] == 45
        assert props["ReservedConcurrentExecutions"] == 5
        assert props["Handler"] == "trikon_cloud.usage_reporter.handler.lambda_handler"
        assert props["Environment"]["Variables"] == {
            "TRIKON_WEB_API_URL": CANONICAL_WEB_API_URL,
            USAGE_SECRET_PARAMETER_ENV: CANONICAL_USAGE_SECRET_PARAMETER_NAME,
            "TRIKON_LOG_LEVEL": "INFO",
        }

    def test_function_dead_letter_queue(self, template: Template) -> None:
        _, props = _function(template, _USAGE_REPORTER_FN_NAME)
        assert props["DeadLetterConfig"] == {"TargetArn": _usage_dlq_arn_ref(template)}

    def test_async_invoke_config(self, template: Template) -> None:
        fn_id, _ = _function(template, _USAGE_REPORTER_FN_NAME)
        _, resource = _single(
            template,
            "AWS::Lambda::EventInvokeConfig",
            {"FunctionName": {"Ref": fn_id}},
        )
        props = resource["Properties"]
        assert props["MaximumRetryAttempts"] == 2
        assert props["MaximumEventAgeInSeconds"] == 6 * 3600

    def test_dlq_retains_14_days(self, template: Template) -> None:
        _, resource = _single(
            template, "AWS::SQS::Queue", {"QueueName": _USAGE_DLQ_NAME}
        )
        assert resource["Properties"]["MessageRetentionPeriod"] == 14 * 86400

    def test_role_reads_exactly_the_usage_secret(self, template: Template) -> None:
        statements = _role_statements(template, _USAGE_REPORTER_ROLE_NAME)
        assert _ssm_statements(statements) == [
            _usage_secret_parameter_read(CANONICAL_USAGE_SECRET_PARAMETER_NAME)
        ]


# ---------------------------------------------------------------------------
# Spot_Retry_Handler (cost-cuts Req 2.5, 4.7, 4.8, 9.1, 9.7)
# ---------------------------------------------------------------------------


class TestSpotRetry:
    def test_function_settings(self, template: Template) -> None:
        _, props = _function(template, _SPOT_RETRY_FN_NAME)
        assert props["Runtime"] == "python3.11"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 256
        assert props["Timeout"] == 90
        assert props["ReservedConcurrentExecutions"] == 5
        assert props["Handler"] == (
            "trikon_cloud.orchestrator.spot_retry_handler.lambda_handler"
        )

    def test_env_wires_every_model_alias(self, template: Template) -> None:
        """Every :class:`SpotRetryEnvConfig` alias but ``AWS_REGION`` is set explicitly."""
        env = _env(template, _SPOT_RETRY_FN_NAME)
        table_id, _ = _spot_retries_table(template)
        assert {k: v for k, v in env.items() if k != "TRIKON_VERIFY_JOBS_QUEUE_URL"} == {
            "TRIKON_APP_ID": str(CANONICAL_APP_ID),
            APP_KEY_PARAMETER_ENV: CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME,
            "TRIKON_VERDICTS_TABLE": "trikon_verdicts",
            "TRIKON_PR_STATE_TABLE": "trikon_pr_state",
            "TRIKON_SPOT_RETRIES_TABLE": {"Ref": table_id},
            "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE": (
                "https://api.trikon.unideploy.com/audits/{delivery_id}"
            ),
            "TRIKON_LOG_LEVEL": "INFO",
        }
        assert (
            _resolve_join(env["TRIKON_VERIFY_JOBS_QUEUE_URL"])
            == CANONICAL_VERIFY_JOBS_QUEUE_URL
        )
        aliases = {
            field.alias
            for field in SpotRetryEnvConfig.model_fields.values()
            if field.alias is not None
        }
        assert aliases - set(env) == {"AWS_REGION"}

    def test_function_dead_letter_queue(self, template: Template) -> None:
        _, props = _function(template, _SPOT_RETRY_FN_NAME)
        assert props["DeadLetterConfig"] == {
            "TargetArn": _spot_retry_dlq_arn_ref(template)
        }

    def test_async_invoke_config(self, template: Template) -> None:
        fn_id, _ = _function(template, _SPOT_RETRY_FN_NAME)
        _, resource = _single(
            template,
            "AWS::Lambda::EventInvokeConfig",
            {"FunctionName": {"Ref": fn_id}},
        )
        props = resource["Properties"]
        assert props["MaximumRetryAttempts"] == 2
        assert props["MaximumEventAgeInSeconds"] == 6 * 3600

    def test_dlq_retains_14_days(self, template: Template) -> None:
        _, resource = _single(
            template, "AWS::SQS::Queue", {"QueueName": _SPOT_RETRY_DLQ_NAME}
        )
        assert resource["Properties"]["MessageRetentionPeriod"] == 14 * 86400

    def test_role_statements_are_exact(self, template: Template) -> None:
        """The design's six statements and nothing else (cost-cuts design §5)."""
        table_id, _ = _spot_retries_table(template)
        statements = _role_statements(template, _SPOT_RETRY_ROLE_NAME)
        expected: list[Statement] = [
            {
                "Sid": SPOT_RETRY_APP_KEY_PARAMETER_READ_SID,
                "Effect": "Allow",
                "Action": ["ssm:GetParameter"],
                "Resource": [_parameter_arn(CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME)],
            },
            {
                "Sid": SPOT_RETRY_SEND_RETRY_JOB_SID,
                "Effect": "Allow",
                "Action": ["sqs:SendMessage"],
                "Resource": [_VERIFY_JOBS_ARN],
            },
            {
                "Sid": SPOT_RETRY_VERDICT_WRITE_SID,
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem"],
                "Resource": [_fleet_table_arn("trikon_verdicts")],
                "Condition": {
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["*"]},
                },
            },
            _pr_state_read(SPOT_RETRY_PR_STATE_READ_SID),
            {
                "Sid": SPOT_RETRY_CLAIMS_SID,
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                ],
                "Resource": [{"Fn::GetAtt": [table_id, "Arn"]}],
            },
            {
                "Effect": "Allow",
                "Action": ["sqs:SendMessage"],
                "Resource": [_spot_retry_dlq_arn_ref(template)],
            },
        ]
        assert _canonical(statements) == _canonical(expected)

    def test_send_message_on_exactly_verify_jobs_and_its_dlq(
        self, template: Template
    ) -> None:
        """Req 9.7: the Retry_Job destination plus the CDK-generated DLQ grant."""
        statements = _role_statements(template, _SPOT_RETRY_ROLE_NAME)
        assert _send_message_resources(statements) == {
            _VERIFY_JOBS_ARN,
            _resource_key(_spot_retry_dlq_arn_ref(template)),
        }

    def test_ssm_is_only_get_parameter_on_the_app_key(
        self, template: Template
    ) -> None:
        statements = _role_statements(template, _SPOT_RETRY_ROLE_NAME)
        assert [s["Sid"] for s in _ssm_statements(statements)] == [
            SPOT_RETRY_APP_KEY_PARAMETER_READ_SID
        ]
        rendered = json.dumps(statements)
        assert CANONICAL_USAGE_SECRET_PARAMETER_NAME not in rendered
        assert "kms:" not in rendered

    def test_claims_table(self, template: Template) -> None:
        """PAY_PER_REQUEST, key ``task_arn`` (S), TTL ``expires_at``, no PITR, DESTROY."""
        _, resource = _spot_retries_table(template)
        props = resource["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["KeySchema"] == [{"AttributeName": "task_arn", "KeyType": "HASH"}]
        assert props["AttributeDefinitions"] == [
            {"AttributeName": "task_arn", "AttributeType": "S"}
        ]
        assert props["TimeToLiveSpecification"] == {
            "AttributeName": "expires_at",
            "Enabled": True,
        }
        assert "PointInTimeRecoverySpecification" not in json.dumps(props)
        assert resource["DeletionPolicy"] == "Delete"
        assert resource["UpdateReplacePolicy"] == "Delete"


def test_app_key_parameter_readable_only_by_its_readers(template: Template) -> None:
    """Cost-cuts Req 4.7: only the orchestrator and Spot_Retry_Handler roles read the App key."""
    roles: Mapping[str, Mapping[str, Any]] = template.find_resources("AWS::IAM::Role")
    readers = {
        str(role["Properties"]["RoleName"])
        for role in roles.values()
        if any(
            CANONICAL_APP_PRIVATE_KEY_PARAMETER_NAME in json.dumps(s["Resource"])
            for s in _ssm_statements(
                _role_statements(template, str(role["Properties"]["RoleName"]))
            )
        )
    }
    assert readers == {_ORCHESTRATOR_ROLE_NAME, _SPOT_RETRY_ROLE_NAME}


# ---------------------------------------------------------------------------
# Log groups (cost-cuts Req 2.3, 2.4)
# ---------------------------------------------------------------------------


class TestLogGroups:
    def test_exactly_one_group_per_function(self, template: Template) -> None:
        groups: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::Logs::LogGroup"
        )
        names = sorted(g["Properties"]["LogGroupName"] for g in groups.values())
        assert len(names) == 4
        assert names == sorted(f"/aws/lambda/{fn}" for fn in _FUNCTION_NAMES)

    @pytest.mark.parametrize("function_name", _FUNCTION_NAMES)
    def test_function_logs_to_its_7_day_retained_group(
        self, template: Template, function_name: str
    ) -> None:
        group_id, group = _single(
            template,
            "AWS::Logs::LogGroup",
            {"LogGroupName": f"/aws/lambda/{function_name}"},
        )
        assert group["Properties"]["RetentionInDays"] == 7
        assert group["DeletionPolicy"] == "Retain"
        assert group["UpdateReplacePolicy"] == "Retain"

        _, props = _function(template, function_name)
        assert props["LoggingConfig"]["LogGroup"] == {"Ref": group_id}


# ---------------------------------------------------------------------------
# Secret-store absence (cost-cuts Req 3.5, 6.1)
# ---------------------------------------------------------------------------


def test_no_old_secret_store_and_no_created_parameter(template: Template) -> None:
    raw: Mapping[str, Any] = template.to_json()
    resource_types = {r["Type"] for r in raw["Resources"].values()}
    assert not any(_OLD_STORE in t.lower() for t in resource_types)
    assert "AWS::SSM::Parameter" not in resource_types
    rendered = json.dumps(raw).lower()
    assert (_OLD_STORE + ":") not in rendered
    assert _OLD_ENV_SUFFIX.lower() not in rendered
    assert "kms:" not in rendered


# ---------------------------------------------------------------------------
# EventBridge rule (Req 12.1, 12.9)
# ---------------------------------------------------------------------------


class TestTaskStoppedRule:
    def test_event_pattern(self, template: Template) -> None:
        _, resource = _single(template, "AWS::Events::Rule", {"Name": _RULE_NAME})
        assert resource["Properties"]["EventPattern"] == {
            "source": ["aws.ecs"],
            "detail-type": ["ECS Task State Change"],
            "detail": {
                "clusterArn": [
                    {
                        "Fn::Join": [
                            "",
                            [
                                f"arn:aws:ecs:{_TEST_REGION}:",
                                {"Ref": "AWS::AccountId"},
                                ":cluster/trikon-verify-cluster",
                            ],
                        ]
                    }
                ],
                "lastStatus": ["STOPPED"],
                "group": ["family:trikon-verify-runner"],
            },
        }

    def test_targets_usage_reporter_and_spot_retry_with_dlq_and_retry_policy(
        self, template: Template
    ) -> None:
        """Req 12.1, 12.9 and cost-cuts Req 9.1: two targets, each with its own DLQ."""
        usage_id, _ = _function(template, _USAGE_REPORTER_FN_NAME)
        spot_id, _ = _function(template, _SPOT_RETRY_FN_NAME)
        _, resource = _single(template, "AWS::Events::Rule", {"Name": _RULE_NAME})
        targets = resource["Properties"]["Targets"]
        assert len(targets) == 2
        by_arn = {json.dumps(t["Arn"], sort_keys=True): t for t in targets}
        expected_dlq = {
            usage_id: _usage_dlq_arn_ref(template),
            spot_id: _spot_retry_dlq_arn_ref(template),
        }
        assert set(by_arn) == {
            json.dumps({"Fn::GetAtt": [fn_id, "Arn"]}, sort_keys=True)
            for fn_id in expected_dlq
        }
        for fn_id, dlq_ref in expected_dlq.items():
            target = by_arn[json.dumps({"Fn::GetAtt": [fn_id, "Arn"]}, sort_keys=True)]
            assert target["DeadLetterConfig"] == {"Arn": dlq_ref}
            assert target["RetryPolicy"] == {
                "MaximumRetryAttempts": 185,
                "MaximumEventAgeInSeconds": 86400,
            }


# ---------------------------------------------------------------------------
# CDK context (cost-cuts Req 3.1-3.3)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synth_env")
class TestContext:
    def test_default_parameter_names(self, tmp_path: Path) -> None:
        template = _synth(_REQUIRED_CONTEXT, tmp_path)

        assert _env(template, _ORCHESTRATOR_FN_NAME)[APP_KEY_PARAMETER_ENV] == (
            APP_KEY_PARAMETER_DEFAULT
        )
        assert _env(template, _ORCHESTRATOR_FN_NAME)[USAGE_SECRET_PARAMETER_ENV] == (
            USAGE_SECRET_PARAMETER_DEFAULT
        )
        assert _env(template, _LIFECYCLE_FN_NAME)[APP_KEY_PARAMETER_ENV] == (
            APP_KEY_PARAMETER_DEFAULT
        )
        assert _env(template, _USAGE_REPORTER_FN_NAME)[USAGE_SECRET_PARAMETER_ENV] == (
            USAGE_SECRET_PARAMETER_DEFAULT
        )

        orchestrator = _role_statements(template, _ORCHESTRATOR_ROLE_NAME)
        assert [s for s in orchestrator if s.get("Sid") == SECURE_PARAMETER_READ_SID] == [
            _secure_parameter_read(
                APP_KEY_PARAMETER_DEFAULT, USAGE_SECRET_PARAMETER_DEFAULT
            )
        ]
        usage = _role_statements(template, _USAGE_REPORTER_ROLE_NAME)
        assert _ssm_statements(usage) == [
            _usage_secret_parameter_read(USAGE_SECRET_PARAMETER_DEFAULT)
        ]

    @pytest.mark.parametrize(
        "key", [APP_KEY_PARAMETER_CONTEXT, USAGE_SECRET_PARAMETER_CONTEXT]
    )
    @pytest.mark.parametrize(
        "raw",
        ["", "trikon-cloud/no-leading-slash", "/bad name", 42],
        ids=["empty", "no-leading-slash", "bad-charset", "not-a-string"],
    )
    def test_invalid_parameter_name_names_the_key(self, key: str, raw: object) -> None:
        context = {**_REQUIRED_CONTEXT, key: raw}
        with pytest.raises(ValueError, match=f"^{key}"):
            build_app(context)

    @pytest.mark.parametrize(
        ("obsolete", "replacement"),
        [
            (OBSOLETE_APP_KEY_CONTEXT, APP_KEY_PARAMETER_CONTEXT),
            (OBSOLETE_USAGE_SECRET_CONTEXT, USAGE_SECRET_PARAMETER_CONTEXT),
        ],
    )
    def test_obsolete_key_points_to_replacement(
        self, obsolete: str, replacement: str
    ) -> None:
        context = {
            **_REQUIRED_CONTEXT,
            obsolete: (
                f"arn:aws:{_OLD_STORE}:{_TEST_REGION}:{CANONICAL_ACCOUNT_ID}"
                ":secret:trikon/old-AbCdEf"
            ),
        }
        with pytest.raises(ValueError) as excinfo:
            build_app(context)
        message = str(excinfo.value)
        assert message.startswith(obsolete)
        assert replacement in message

    @pytest.mark.parametrize(
        "obsolete", [OBSOLETE_APP_KEY_CONTEXT, OBSOLETE_USAGE_SECRET_CONTEXT]
    )
    def test_obsolete_key_rejected_even_with_new_keys(self, obsolete: str) -> None:
        context = {**_BASE_CONTEXT, obsolete: "anything"}
        with pytest.raises(ValueError, match=f"^{obsolete}"):
            build_app(context)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(False, "false"), ("false", "false"), ("FALSE", "false"), ("True", "true")],
        ids=["json-false", "str-false", "str-FALSE", "str-True"],
    )
    def test_use_fargate_spot_reaches_the_orchestrator(
        self, tmp_path: Path, raw: object, expected: str
    ) -> None:
        """Cost-cuts Req 8.2: the Spot_Flag (default ``"true"``) is passed through."""
        template = _synth({**_BASE_CONTEXT, USE_FARGATE_SPOT_CONTEXT: raw}, tmp_path)
        assert _env(template, _ORCHESTRATOR_FN_NAME)["TRIKON_USE_FARGATE_SPOT"] == (
            expected
        )

    @pytest.mark.parametrize(
        "raw", ["", "yes", "0", " true", 1, 0, ["true"]],
        ids=["empty", "yes", "zero-str", "padded", "int-1", "int-0", "list"],
    )
    def test_invalid_use_fargate_spot_names_the_key(self, raw: object) -> None:
        context = {**_REQUIRED_CONTEXT, USE_FARGATE_SPOT_CONTEXT: raw}
        with pytest.raises(ValueError, match=f"^{USE_FARGATE_SPOT_CONTEXT}"):
            build_app(context)

    def test_required_context_still_fails_loud(self) -> None:
        context = {k: v for k, v in _REQUIRED_CONTEXT.items() if k != "app_id"}
        with pytest.raises(ValueError, match=r"^app_id"):
            build_app(context)
