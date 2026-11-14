# CDK's jsii-generated stubs type ``Template.find_resources`` /
# ``Template.find_outputs`` payloads as ``Mapping[str, Mapping[str, Any]]``.
# Under this repo's ``disallow_any_explicit = true`` mypy config those
# surface as ``explicit-any`` errors on library code we do not own.
# Suppress at file scope, mirroring ``test_ssm_active_revision.py``.
# mypy: disable-error-code="explicit-any"
"""Synth-time assertions for ``FargateRunnerStack`` and its CDK app.

* No-NAT runner networking (sandbox-minute-plans Req 14.1-14.4, 14.7):
  ``trikon-verify-vpc`` with public subnets only across two Availability
  Zones and zero NAT gateways, the egress-only ``trikon-verify-runner-sg``
  security group, and the subnet / security group outputs for the
  OrchestratorStack context.
* Cost cuts (trikon-cloud-cost-cuts Req 2.1, 3.1-3.5, 4.5, 4.8, 6.1): the
  7-day runner log group, the ``TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME``
  container env, the exact ``ssm:GetParameter`` grant from
  :meth:`FargateRunnerStack.build_task_role_for_installation`, no Secrets
  Manager reference, and :func:`build_app` context handling.
* Spot readiness (trikon-cloud-cost-cuts Req 8.1, 10.1): the 120-second
  runner ``StopTimeout``, the cluster's ``FARGATE`` / ``FARGATE_SPOT``
  association, and no ``FARGATE_SPOT`` in the default strategy.

No AWS credentials, no network, no ``moto``. The ``build_app`` tests run
under the shared ``no_network`` fixture.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack
from aws_cdk.assertions import Template

from trikon_cloud.fargate_runner.infra.app import STACK_ID, build_app
from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack

# Same fixture constants as ``test_ssm_active_revision.py``.
_TEST_ACCOUNT: str = "000000000000"
_TEST_REGION: str = "us-east-1"
_APP_KEY_PARAMETER_NAME: str = "/trikon-cloud/github-app-private-key"
_APP_KEY_PARAMETER_ARN: str = (
    "arn:aws:ssm:us-east-1:000000000000:parameter/trikon-cloud/github-app-private-key"
)
_UNUSED_APP_ID: int = 999999
_RUNNER_SG_NAME: str = "trikon-verify-runner-sg"
_TASK_ROLE_INSTALLATION_ID: int = 12345678


def _make_stack(app: App) -> FargateRunnerStack:
    return FargateRunnerStack(
        app,
        "TestFargateRunnerStack",
        app_private_key_parameter_name=_APP_KEY_PARAMETER_NAME,
        app_id=_UNUSED_APP_ID,
        env=Environment(account=_TEST_ACCOUNT, region=_TEST_REGION),
    )


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize :class:`FargateRunnerStack` once for the whole module."""
    return Template.from_stack(_make_stack(App()))


@pytest.fixture(scope="module")
def template_with_task_role() -> Template:
    """Synthesize the stack plus one per-installation task role."""
    stack = _make_stack(App())
    stack.build_task_role_for_installation(
        _TASK_ROLE_INSTALLATION_ID,
        app_private_key_parameter_name=_APP_KEY_PARAMETER_NAME,
    )
    return Template.from_stack(stack)


def _runner_environment(template: Template) -> dict[str, str]:
    """Return the runner container's ``Environment`` as a name → value dict."""
    task_defs: Mapping[str, Mapping[str, Any]] = template.find_resources(
        "AWS::ECS::TaskDefinition"
    )
    assert len(task_defs) == 1
    (task_def,) = task_defs.values()
    (container,) = task_def["Properties"]["ContainerDefinitions"]
    return {entry["Name"]: entry["Value"] for entry in container["Environment"]}


def _app_template(app: App) -> Template:
    stack = app.node.find_child(STACK_ID)
    assert isinstance(stack, Stack)
    return Template.from_stack(stack)


def _subnets(template: Template) -> Mapping[str, Mapping[str, Any]]:
    subnets: Mapping[str, Mapping[str, Any]] = template.find_resources(
        "AWS::EC2::Subnet"
    )
    return subnets


def _runner_sg(template: Template) -> tuple[str, Mapping[str, Any]]:
    """Return ``(logical_id, resource)`` of the runner security group."""
    groups: Mapping[str, Mapping[str, Any]] = template.find_resources(
        "AWS::EC2::SecurityGroup",
        {"Properties": {"GroupName": _RUNNER_SG_NAME}},
    )
    assert len(groups) == 1, (
        f"expected exactly one security group named {_RUNNER_SG_NAME!r}; "
        f"found {len(groups)}"
    )
    ((logical_id, resource),) = groups.items()
    return logical_id, resource


class TestNoNat:
    """Requirement 14.1: zero NAT gateways (and so no NAT Elastic IPs)."""

    def test_no_nat_gateway(self, template: Template) -> None:
        template.resource_count_is("AWS::EC2::NatGateway", 0)

    def test_no_elastic_ip(self, template: Template) -> None:
        template.resource_count_is("AWS::EC2::EIP", 0)


class TestPublicSubnetsOnly:
    """Requirement 14.2: public subnets only, across 2 Availability Zones."""

    def test_exactly_two_subnets(self, template: Template) -> None:
        template.resource_count_is("AWS::EC2::Subnet", 2)

    def test_every_subnet_maps_public_ip_on_launch(self, template: Template) -> None:
        for logical_id, subnet in _subnets(template).items():
            assert subnet["Properties"].get("MapPublicIpOnLaunch") is True, (
                f"subnet {logical_id} must set MapPublicIpOnLaunch: true; "
                f"got {subnet['Properties'].get('MapPublicIpOnLaunch')!r}"
            )

    def test_subnets_are_in_two_distinct_azs(self, template: Template) -> None:
        azs = [s["Properties"]["AvailabilityZone"] for s in _subnets(template).values()]
        assert len(azs) == 2
        assert azs[0] != azs[1], f"subnets share one AZ: {azs!r}"

    def test_vpc_is_named_trikon_verify_vpc(self, template: Template) -> None:
        template.resource_count_is("AWS::EC2::VPC", 1)
        template.has_resource_properties(
            "AWS::EC2::VPC",
            {"Tags": [{"Key": "Name", "Value": "trikon-verify-vpc"}]},
        )


class TestRunnerSecurityGroup:
    """Requirement 14.3: zero ingress rules, outbound traffic permitted."""

    def test_no_inline_ingress(self, template: Template) -> None:
        _, sg = _runner_sg(template)
        assert "SecurityGroupIngress" not in sg["Properties"], (
            f"runner SG must have no ingress; got "
            f"{sg['Properties'].get('SecurityGroupIngress')!r}"
        )

    def test_no_standalone_ingress_resource_targets_runner_sg(
        self, template: Template
    ) -> None:
        logical_id, _ = _runner_sg(template)
        ingress: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::EC2::SecurityGroupIngress"
        )
        targeting = [
            rid
            for rid, rule in ingress.items()
            if rule["Properties"].get("GroupId")
            == {"Fn::GetAtt": [logical_id, "GroupId"]}
        ]
        assert targeting == [], f"ingress rules target the runner SG: {targeting!r}"

    def test_all_traffic_egress_to_anywhere(self, template: Template) -> None:
        _, sg = _runner_sg(template)
        egress = sg["Properties"].get("SecurityGroupEgress")
        assert isinstance(egress, list) and len(egress) == 1, (
            f"runner SG must have exactly one egress rule; got {egress!r}"
        )
        (rule,) = egress
        assert rule["CidrIp"] == "0.0.0.0/0"
        assert rule["IpProtocol"] == "-1"


class TestOutputs:
    """Requirement 14.4: subnet ids and security group id are stack outputs."""

    def test_runner_subnet_ids_output_joins_both_subnets(
        self, template: Template
    ) -> None:
        outputs: Mapping[str, Mapping[str, Any]] = template.find_outputs(
            "RunnerSubnetIds"
        )
        assert len(outputs) == 1, f"RunnerSubnetIds output missing: {outputs!r}"
        (output,) = outputs.values()
        join = output["Value"]["Fn::Join"]
        assert join[0] == ","
        referenced = sorted(part["Ref"] for part in join[1])
        assert referenced == sorted(_subnets(template).keys())

    def test_runner_security_group_id_output(self, template: Template) -> None:
        logical_id, _ = _runner_sg(template)
        template.has_output(
            "RunnerSecurityGroupId",
            {"Value": {"Fn::GetAtt": [logical_id, "GroupId"]}},
        )


class TestRunnerLogRetention:
    """Cost-cuts Req 2.1: the runner log group keeps logs for 7 days."""

    def test_runner_log_group_is_seven_days(self, template: Template) -> None:
        groups: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::Logs::LogGroup"
        )
        assert len(groups) == 1, f"expected one log group; found {sorted(groups)}"
        (group,) = groups.values()
        assert group["Properties"]["LogGroupName"] == "/aws/ecs/trikon-verify-runner"
        assert group["Properties"]["RetentionInDays"] == 7
        assert group["DeletionPolicy"] == "Retain"


class TestAppKeyParameter:
    """Cost-cuts Req 3.4, 3.5, 4.5, 4.8, 6.1: the App key is an SSM parameter."""

    def test_container_env_carries_parameter_name(self, template: Template) -> None:
        env = _runner_environment(template)
        assert env["TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME"] == _APP_KEY_PARAMETER_NAME
        assert not any(name.endswith("_SECRET" + "_ARN") for name in env)

    def test_stack_creates_no_secure_parameter(self, template: Template) -> None:
        # Req 3.5: the operator creates the App_Key_Parameter; the only
        # SSM parameter the stack owns is the active-revision String.
        params: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::SSM::Parameter"
        )
        names = [p["Properties"]["Name"] for p in params.values()]
        assert names == ["/trikon/verify-runner/active-revision"]

    def test_task_role_grants_exactly_ssm_get_parameter_on_app_key(
        self, template_with_task_role: Template
    ) -> None:
        policies: Mapping[str, Mapping[str, Any]] = template_with_task_role.find_resources(
            "AWS::IAM::Policy"
        )
        task_role_policies = [
            p for key, p in policies.items() if "TrikonVerifyRunnerTaskRole" in key
        ]
        assert len(task_role_policies) == 1
        statements = task_role_policies[0]["Properties"]["PolicyDocument"]["Statement"]
        ssm_statements = [
            s for s in statements if "ssm:" in json.dumps(s["Action"])
        ]
        assert ssm_statements == [
            {
                "Action": "ssm:GetParameter",
                "Effect": "Allow",
                "Resource": _APP_KEY_PARAMETER_ARN,
            }
        ]
        assert "kms:" not in json.dumps(statements)

    def test_no_secrets_manager_reference(
        self, template_with_task_role: Template
    ) -> None:
        rendered = json.dumps(template_with_task_role.to_json())
        assert "secrets" + "manager" not in rendered.lower()


class TestSpotReadiness:
    """Cost-cuts Req 8.1, 10.1: capacity providers and the runner stop timeout."""

    def test_runner_stop_timeout_is_120_seconds(self, template: Template) -> None:
        task_defs: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::ECS::TaskDefinition"
        )
        (task_def,) = task_defs.values()
        (container,) = task_def["Properties"]["ContainerDefinitions"]
        assert container["StopTimeout"] == 120

    def test_cluster_associates_fargate_and_fargate_spot(
        self, template: Template
    ) -> None:
        associations: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::ECS::ClusterCapacityProviderAssociations"
        )
        assert len(associations) == 1, (
            f"expected one capacity provider association; found {sorted(associations)}"
        )
        (association,) = associations.values()
        providers = association["Properties"]["CapacityProviders"]
        assert sorted(providers) == ["FARGATE", "FARGATE_SPOT"]

    def test_default_strategy_has_no_fargate_spot(self, template: Template) -> None:
        # Design D5: an explicit RunTask strategy is always sent, but the
        # cluster default must never silently place work on Spot.
        associations: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::ECS::ClusterCapacityProviderAssociations"
        )
        (association,) = associations.values()
        default = association["Properties"]["DefaultCapacityProviderStrategy"]
        assert isinstance(default, list)
        assert "FARGATE_SPOT" not in json.dumps(default)


@pytest.mark.usefixtures("no_network")
class TestBuildApp:
    """Cost-cuts Req 1.3, 3.1-3.3: ``build_app`` synthesizes offline from context."""

    def test_default_parameter_name(self, tmp_path: Path) -> None:
        app = build_app({"app_id": "999999"}, outdir=str(tmp_path))
        app.synth()
        env = _runner_environment(_app_template(app))
        assert env["TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME"] == _APP_KEY_PARAMETER_NAME
        assert env["TRIKON_APP_ID"] == "999999"

    def test_custom_parameter_name(self, tmp_path: Path) -> None:
        app = build_app(
            {"app_id": "999999", "app_private_key_parameter_name": "/acme/app-key"},
            outdir=str(tmp_path),
        )
        env = _runner_environment(_app_template(app))
        assert env["TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME"] == "/acme/app-key"

    @pytest.mark.parametrize("bad_name", ["", "acme/app-key", "/has space"])
    def test_invalid_parameter_name_names_the_key(
        self, tmp_path: Path, bad_name: str
    ) -> None:
        with pytest.raises(ValueError, match=r"^app_private_key_parameter_name"):
            build_app(
                {"app_id": "999999", "app_private_key_parameter_name": bad_name},
                outdir=str(tmp_path),
            )

    def test_obsolete_secret_arn_key_fails_and_names_replacement(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="app_private_key_parameter_name") as exc:
            build_app(
                {"app_id": "999999", "app_private_key_secret_arn": "arn:aws:example"},
                outdir=str(tmp_path),
            )
        assert str(exc.value).startswith("app_private_key_secret_arn")

    def test_missing_app_id_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="app_id"):
            build_app({}, outdir=str(tmp_path))
