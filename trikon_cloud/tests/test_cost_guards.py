# CloudFormation templates are untyped JSON; this module types them as
# ``Mapping[str, Any]``. Under the repo's ``disallow_any_explicit = true``
# that is an ``explicit-any`` error, so suppress it at file scope, mirroring
# the ``*_stack_template.py`` tests.
# mypy: disable-error-code="explicit-any"
"""Offline cost guards over all three Trikon Cloud CDK_Apps (task 19.1).

Each CDK_App is synthesized once per module from the ``Trikon/`` root with
outbound sockets blocked and the ambient AWS / CDK environment removed (the
same guard as the shared ``no_network`` fixture, which is function-scoped
and so cannot back a module-scoped fixture). Every template test is
parametrized over the three Synthesized_Templates:

* each app synthesizes offline and attempts no connection (Req 1.3, 1.4,
  13.3, 14.2);
* every ``AWS::Logs::LogGroup`` keeps logs 7 days (Req 2.6);
* every ``AWS::Lambda::Function`` logs to a same-template
  ``/aws/lambda/<FunctionName>`` group through ``LoggingConfig`` (Req 2.7),
  and the function inventory is pinned, so a CDK-generated helper Lambda
  (log-retention or bucket-deployment singleton) fails the guard instead of
  slipping past it;
* no NAT gateway or load balancer (Req 13.1, 13.2);
* no Secrets Manager resource, action or reference (Req 6.4);
* no ``kms:Decrypt`` grant without a ``kms:ViaService`` condition limited to
  ``ssm.<region>.amazonaws.com`` (Req 4.6; today no stack grants it at all);
* no ``AWS::SSM::Parameter`` named after a Secure_Parameter, which the
  operator creates out-of-band (Req 3.5).

A static scan of ``trikon_cloud/**/*.py`` asserts that no Secrets Manager
client, construct, IAM action or ``*_SECRET_ARN`` variable remains
(Req 6.1). Its patterns are built by string concatenation so they do not
match this file, which the scan also skips.
"""

from __future__ import annotations

import fnmatch
import json
import re
import socket
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from trikon_cloud.fargate_runner.infra import app as fargate_runner_app
from trikon_cloud.lambda_asset import LAMBDA_ASSET_DIR_CONTEXT
from trikon_cloud.orchestrator.infra import app as orchestrator_app
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_DEFAULT,
    USAGE_SECRET_PARAMETER_DEFAULT,
    WEBHOOK_SECRET_PARAMETER_DEFAULT,
)
from trikon_cloud.webhook_receiver.infra import app as webhook_receiver_app

Template = Mapping[str, Any]

_ACCOUNT = "123456789012"
_REGION = "us-east-1"

# Synthesis runs from the ``Trikon/`` root, as the CDK CLI does.
_TRIKON_ROOT: Path = Path(__file__).resolve().parents[2]
_TRIKON_CLOUD_ROOT: Path = _TRIKON_ROOT / "trikon_cloud"

# Test context per CDK_App. Parameter names are left at their defaults, so
# the Secure_Parameter names below are the ones the stacks reference. The
# two Lambda apps also need ``lambda_asset_dir``; their module fixtures add
# the session Lambda_Asset (see ``_with_asset``).
FARGATE_RUNNER_CONTEXT: Mapping[str, object] = {
    "account": _ACCOUNT,
    "app_id": "999999",
    "runner_image_uri": f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/trikon-cloud-runner:runner-test",
}
ORCHESTRATOR_CONTEXT: Mapping[str, object] = {
    "app_id": 999999,
    "verify_jobs_queue_arn": f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-verify-jobs",
    "verify_jobs_dlq_arn": f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-verify-jobs-dlq",
    "runner_subnet_ids": "subnet-a,subnet-b",
    "runner_security_group_ids": "sg-verify",
    "trikon_web_api_url": "https://api.trikon.example.test",
    "trikon_web_app_url": "https://trikon.example.test",
}
WEBHOOK_RECEIVER_CONTEXT: Mapping[str, object] = {
    "account": _ACCOUNT,
    "installation_events_queue_arn": (
        f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:trikon-cloud-installation-events"
    ),
}

_SECURE_PARAMETER_NAMES: frozenset[str] = frozenset(
    {
        APP_KEY_PARAMETER_DEFAULT,
        WEBHOOK_SECRET_PARAMETER_DEFAULT,
        USAGE_SECRET_PARAMETER_DEFAULT,
    }
)

# Every Lambda each app may define. A new function (including a CDK
# singleton helper) must be added here and must log to its own group.
_EXPECTED_FUNCTIONS: Mapping[str, frozenset[str]] = {
    "fargate_runner": frozenset(),
    "orchestrator": frozenset(
        {
            "trikon-cloud-orchestrator",
            "trikon-cloud-installation-lifecycle",
            "trikon-cloud-usage-reporter",
            "trikon-cloud-spot-retry",
        }
    ),
    "webhook_receiver": frozenset({"trikon-cloud-webhook-receiver"}),
}

_FORBIDDEN_NETWORK_TYPES: tuple[str, ...] = (
    "AWS::EC2::NatGateway",
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::ElasticLoadBalancing::LoadBalancer",
)

# Built by concatenation so neither this file nor the scan matches itself.
_OLD_STORE = "secrets" + "manager"
_OLD_STORE_TYPE_PREFIX = "AWS::Secrets" + "Manager::"

_KMS_VIA_SERVICE_VALUES: frozenset[str] = frozenset(
    {f"ssm.{_REGION}.amazonaws.com", "ssm.${AWS::Region}.amazonaws.com"}
)


# ---------------------------------------------------------------------------
# Offline synthesis
# ---------------------------------------------------------------------------


class _NetworkBlockedError(RuntimeError):
    """Raised when synthesis tries to open a socket connection."""


@dataclass(frozen=True)
class Synthesized:
    app: str
    template: Template
    connect_attempts: tuple[object, ...]


@contextmanager
def _offline_synth_env() -> Iterator[list[object]]:
    """The ``no_network`` guard, usable from a module-scoped fixture.

    Blocks ``socket.socket.connect`` / ``connect_ex`` (recording every
    attempt), drops ``AWS_PROFILE`` and ``CDK_DEFAULT_ACCOUNT`` plus ambient
    CDK context, region and outdir, and runs from the ``Trikon/`` root.
    """
    attempts: list[object] = []

    def _refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise _NetworkBlockedError(f"network access blocked during synth: {address!r}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _refuse)
        mp.setattr(socket.socket, "connect_ex", _refuse)
        for name in (
            "AWS_PROFILE",
            "CDK_DEFAULT_ACCOUNT",
            "CDK_CONTEXT_JSON",
            "CDK_DEFAULT_REGION",
            "CDK_OUTDIR",
        ):
            mp.delenv(name, raising=False)
        mp.chdir(_TRIKON_ROOT)
        yield attempts


def _synth(
    app_name: str, build: Callable[[str], cdk.App], stack_id: str, outdir: Path
) -> Synthesized:
    with _offline_synth_env() as attempts:
        assembly = build(str(outdir)).synth()
        template_path = Path(assembly.get_stack_by_name(stack_id).template_full_path)
        template: Template = json.loads(template_path.read_text(encoding="utf-8"))
    return Synthesized(app_name, template, tuple(attempts))


@pytest.fixture(scope="module")
def fargate_runner_synth(tmp_path_factory: pytest.TempPathFactory) -> Synthesized:
    return _synth(
        "fargate_runner",
        lambda outdir: fargate_runner_app.build_app(FARGATE_RUNNER_CONTEXT, outdir=outdir),
        fargate_runner_app.STACK_ID,
        tmp_path_factory.mktemp("cost-guard-fargate-runner"),
    )


def _with_asset(context: Mapping[str, object], lambda_asset_dir: str) -> dict[str, object]:
    """``context`` plus ``lambda_asset_dir``, required by both Lambda apps."""
    return {**context, LAMBDA_ASSET_DIR_CONTEXT: lambda_asset_dir}


@pytest.fixture(scope="module")
def orchestrator_synth(
    tmp_path_factory: pytest.TempPathFactory, lambda_asset_dir: str
) -> Synthesized:
    context = _with_asset(ORCHESTRATOR_CONTEXT, lambda_asset_dir)
    return _synth(
        "orchestrator",
        lambda outdir: orchestrator_app.build_app(context, outdir=outdir),
        orchestrator_app.STACK_ID,
        tmp_path_factory.mktemp("cost-guard-orchestrator"),
    )


@pytest.fixture(scope="module")
def webhook_receiver_synth(
    tmp_path_factory: pytest.TempPathFactory, lambda_asset_dir: str
) -> Synthesized:
    context = _with_asset(WEBHOOK_RECEIVER_CONTEXT, lambda_asset_dir)
    return _synth(
        "webhook_receiver",
        lambda outdir: webhook_receiver_app.build_app(context, outdir=outdir),
        webhook_receiver_app.STACK_ID,
        tmp_path_factory.mktemp("cost-guard-webhook-receiver"),
    )


@pytest.fixture(params=["fargate_runner", "orchestrator", "webhook_receiver"])
def synthesized(request: pytest.FixtureRequest) -> Synthesized:
    """One Synthesized_Template per CDK_App; every template test runs on all three."""
    result = request.getfixturevalue(f"{request.param}_synth")
    assert isinstance(result, Synthesized)
    return result


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _resources(template: Template, resource_type: str) -> dict[str, Mapping[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


def _statements(node: object) -> Iterator[Mapping[str, Any]]:
    """Every IAM-style policy statement anywhere in ``node``.

    Covers ``AWS::IAM::Policy``, inline role policies, managed policies and
    resource policies alike: any mapping with ``Effect`` and an
    ``Action`` / ``NotAction`` is a statement.
    """
    if isinstance(node, Mapping):
        if "Effect" in node and ("Action" in node or "NotAction" in node):
            yield node
        for value in node.values():
            yield from _statements(value)
    elif isinstance(node, list):
        for item in node:
            yield from _statements(item)


def _action_strings(statement: Mapping[str, Any]) -> list[str]:
    actions = _as_list(statement.get("Action", [])) + _as_list(
        statement.get("NotAction", [])
    )
    return [action for action in actions if isinstance(action, str)]


def _allows(statement: Mapping[str, Any], action: str) -> bool:
    """True when an ``Allow`` statement grants ``action`` (wildcards included)."""
    if statement.get("Effect") != "Allow":
        return False
    wanted = action.lower()
    if "Action" in statement:
        return any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(wanted, pattern.lower())
            for pattern in _as_list(statement["Action"])
        )
    # ``NotAction`` grants everything it does not list.
    return not any(
        isinstance(pattern, str) and fnmatch.fnmatchcase(wanted, pattern.lower())
        for pattern in _as_list(statement["NotAction"])
    )


def _render(value: object) -> str | None:
    """Render a literal / ``Fn::Join`` / ``Fn::Sub`` / ``Ref AWS::Region`` string."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and len(value) == 1:
        ((key, arg),) = value.items()
        if key == "Ref" and arg == "AWS::Region":
            return "${AWS::Region}"
        if key == "Fn::Sub" and isinstance(arg, str):
            return arg
        if key == "Fn::Join" and isinstance(arg, list) and len(arg) == 2:
            separator, parts = arg
            if isinstance(separator, str) and isinstance(parts, list):
                rendered = [_render(part) for part in parts]
                if all(part is not None for part in rendered):
                    return separator.join(part for part in rendered if part is not None)
    return None


def _has_ssm_via_service(statement: Mapping[str, Any]) -> bool:
    condition = statement.get("Condition")
    if not isinstance(condition, Mapping):
        return False
    string_equals = condition.get("StringEquals")
    if not isinstance(string_equals, Mapping) or "kms:ViaService" not in string_equals:
        return False
    values = [_render(value) for value in _as_list(string_equals["kms:ViaService"])]
    return bool(values) and all(value in _KMS_VIA_SERVICE_VALUES for value in values)


# ---------------------------------------------------------------------------
# Template guards (parametrized over the three Synthesized_Templates)
# ---------------------------------------------------------------------------


def test_app_synthesizes_offline(synthesized: Synthesized) -> None:
    """Req 1.3, 1.4, 13.3: synthesis succeeds with every connection refused."""
    assert synthesized.connect_attempts == ()
    assert synthesized.template["Resources"]


def test_every_log_group_keeps_logs_seven_days(synthesized: Synthesized) -> None:
    """Req 2.6."""
    log_groups = _resources(synthesized.template, "AWS::Logs::LogGroup")
    assert log_groups, "every Cloud_Stack defines at least one log group"
    retention = {
        logical_id: group["Properties"].get("RetentionInDays")
        for logical_id, group in log_groups.items()
    }
    assert retention == dict.fromkeys(log_groups, 7)


def test_every_lambda_logs_to_its_own_group(synthesized: Synthesized) -> None:
    """Req 2.3-2.5, 2.7: ``LoggingConfig.LogGroup`` Refs ``/aws/lambda/<FunctionName>``."""
    template = synthesized.template
    log_groups = _resources(template, "AWS::Logs::LogGroup")
    functions = _resources(template, "AWS::Lambda::Function")

    names: set[str] = set()
    for logical_id, function in functions.items():
        props = function["Properties"]
        name = props.get("FunctionName")
        assert isinstance(name, str), f"{logical_id} has no literal FunctionName"
        names.add(name)
        log_group_ref = props.get("LoggingConfig", {}).get("LogGroup")
        assert isinstance(log_group_ref, Mapping) and set(log_group_ref) == {"Ref"}, (
            f"{name} does not Ref a log group through LoggingConfig: {log_group_ref!r}"
        )
        group_id = log_group_ref["Ref"]
        assert group_id in log_groups, f"{name} logs to {group_id}, not a LogGroup here"
        assert log_groups[group_id]["Properties"]["LogGroupName"] == f"/aws/lambda/{name}"

    assert names == _EXPECTED_FUNCTIONS[synthesized.app]


def test_no_nat_gateway_or_load_balancer(synthesized: Synthesized) -> None:
    """Req 13.1, 13.2."""
    present = sorted(
        f"{logical_id} ({resource['Type']})"
        for logical_id, resource in synthesized.template["Resources"].items()
        if resource["Type"] in _FORBIDDEN_NETWORK_TYPES
    )
    assert present == []


def test_no_secrets_manager(synthesized: Synthesized) -> None:
    """Req 6.4: no ``AWS::SecretsManager::*`` resource and no Secrets Manager action."""
    template = synthesized.template
    resources = sorted(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if str(resource["Type"]).startswith(_OLD_STORE_TYPE_PREFIX)
    )
    assert resources == []

    actions = sorted(
        action
        for statement in _statements(template)
        for action in _action_strings(statement)
        if action.lower().startswith(_OLD_STORE + ":")
    )
    assert actions == []
    # No ARN, env value or other reference to the old store anywhere either.
    assert _OLD_STORE not in json.dumps(template).lower()


def test_kms_decrypt_only_via_ssm(synthesized: Synthesized) -> None:
    """Req 4.6: any ``kms:Decrypt`` grant is limited to ``ssm.<region>.amazonaws.com``.

    Design: readers rely on the ``aws/ssm`` key policy, so no stack grants
    ``kms:Decrypt`` today and this holds vacuously; it fails if an
    unconditioned grant is added.
    """
    offending = [
        statement
        for statement in _statements(synthesized.template)
        if _allows(statement, "kms:Decrypt") and not _has_ssm_via_service(statement)
    ]
    assert offending == []


def test_no_secure_parameter_is_created(synthesized: Synthesized) -> None:
    """Req 3.5: Secure_Parameters are referenced by name, never created."""
    created = sorted(
        str(parameter["Properties"].get("Name"))
        for parameter in _resources(synthesized.template, "AWS::SSM::Parameter").values()
        if parameter["Properties"].get("Name") in _SECURE_PARAMETER_NAMES
    )
    assert created == []


# ---------------------------------------------------------------------------
# Static scan (Req 6.1)
# ---------------------------------------------------------------------------

_SCAN_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "boto3 client name": re.compile("[\"']" + _OLD_STORE + "[\"']", re.IGNORECASE),
    "CDK construct module": re.compile("aws_" + _OLD_STORE, re.IGNORECASE),
    "IAM action": re.compile(_OLD_STORE + ":", re.IGNORECASE),
    "ARN environment variable": re.compile("_SECRET" + "_ARN"),
}


def _scanned_files() -> list[Path]:
    this_file = Path(__file__).resolve()
    files: list[Path] = []
    for path in sorted(_TRIKON_CLOUD_ROOT.rglob("*.py")):
        relative = path.relative_to(_TRIKON_CLOUD_ROOT)
        if any(part.startswith("cdk.out") or part == "__pycache__" for part in relative.parts):
            continue
        if path.resolve() == this_file:
            continue
        files.append(path)
    return files


def test_static_scan_covers_source_infra_and_tests() -> None:
    relative = {path.relative_to(_TRIKON_CLOUD_ROOT).as_posix() for path in _scanned_files()}
    for expected in (
        "fargate_runner/infra/app.py",
        "orchestrator/infra/orchestrator_stack.py",
        "webhook_receiver/handler.py",
        "usage_client/secret.py",
        "orchestrator/tests/test_orchestrator_stack_template.py",
    ):
        assert expected in relative
    assert "tests/test_cost_guards.py" not in relative


@pytest.mark.parametrize("label", sorted(_SCAN_PATTERNS))
def test_static_scan_finds_no_secrets_manager_remnant(label: str) -> None:
    pattern = _SCAN_PATTERNS[label]
    hits: list[str] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                relative = path.relative_to(_TRIKON_CLOUD_ROOT).as_posix()
                hits.append(f"{relative}:{line_number}: {line.strip()}")
    assert hits == [], f"{label} found:\n" + "\n".join(hits)
