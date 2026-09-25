# CDK's jsii-generated stubs type ``Template.find_resources`` return
# payloads as ``Mapping[str, Mapping[str, Any]]`` and forward construct
# ``**kwargs`` as ``Any``. Under this repo's
# ``disallow_any_explicit = true`` mypy config, both surface as
# ``explicit-any`` errors on library code we do not own. Suppress at
# file scope — every ``Any`` here is bounded to the CDK assertions
# surface, never crossed into a production module. Mirrors the pattern
# used by ``trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py``
# and the file-scope pragma on ``fargate_runner_stack.py`` itself.
# mypy: disable-error-code="explicit-any"
"""Synth-time assertions for the SSM Active-Revision Parameter (design.md §3).

Verifies the amendment added by
``.kiro/specs/trikon-cloud-fargate-runner-ssm-active-revision``:
exactly one ``AWS::SSM::Parameter`` at the well-known name
``/trikon/verify-runner/active-revision``, with ``Type: String``, value
derived at deploy time via ``Fn::Select`` over ``Fn::Split`` on the
task-definition ARN, and a ``DependsOn`` edge on the
``AWS::ECS::TaskDefinition`` logical id that CDK synthesizes.

Spec 2's pre-existing 77 tests are NOT modified — those nine files stay
on the 77-passing baseline. This file is additive and dedicated to the
amendment; all four methods are synth-time (``Template.from_stack`` +
JSON inspection only) and require no AWS credentials, no ``moto``, and
no import from ``trikon_cloud.orchestrator.*``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack

# ---------------------------------------------------------------------------
# Canonical test inputs — mirror
# ``trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py``
# so the two synth-time test files share one set of fixture constants.
# ---------------------------------------------------------------------------

_TEST_ACCOUNT: str = "000000000000"
_TEST_REGION: str = "us-east-1"
_MOCK_APP_KEY_ARN: str = (
    "arn:aws:secretsmanager:us-east-1:000000000000:"
    "secret:trikon-cloud/github-app-private-key-abcdef"
)
# ``FargateRunnerStack`` requires ``app_id`` for the runner task-def
# environment; the SSM Active-Revision parameter is orthogonal to it,
# so a fixed placeholder is sufficient.
_UNUSED_APP_ID: int = 999999


# ---------------------------------------------------------------------------
# Module-scope fixture — synthesize once, share across every test method.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize :class:`FargateRunnerStack` once and return its Template.

    Module scope because :class:`Template` is immutable after synth and
    every test method in this file reads from the same synth output.
    Rebuilding per-test would multiply the ~1-2 second synth wall-clock
    by four for no isolation gain — the stack holds no mutable state a
    test could pollute.
    """
    app = App()
    stack = FargateRunnerStack(
        app,
        "TestFargateRunnerStack",
        app_private_key_secret_arn=_MOCK_APP_KEY_ARN,
        app_id=_UNUSED_APP_ID,
        env=Environment(account=_TEST_ACCOUNT, region=_TEST_REGION),
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Synth-time assertions — one method per requirement clause so each can
# fail independently and identify which clause drifted.
# ---------------------------------------------------------------------------


class TestSsmActiveRevisionParameter:
    """Requirement 4.2-4.6: dedicated synth-time coverage for the amendment."""

    def test_ssm_parameter_exists_with_correct_name_and_type(
        self, template: Template
    ) -> None:
        """Requirements 1.1, 1.2, 1.3, 1.4, 1.5.

        Exactly one ``AWS::SSM::Parameter`` resource exists, with ``Name``
        equal to ``/trikon/verify-runner/active-revision`` and ``Type``
        equal to ``String`` (never ``SecureString`` or ``StringList``).
        The negatives (1.4, 1.5) are covered transitively by
        :meth:`Template.has_resource_properties`, which requires an
        exact match on the ``Type`` field.
        """
        template.resource_count_is("AWS::SSM::Parameter", 1)
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {
                "Name": "/trikon/verify-runner/active-revision",
                "Type": "String",
            },
        )

    def test_ssm_parameter_value_uses_fn_select_fn_split_over_task_def_arn(
        self, template: Template
    ) -> None:
        """Requirements 2.1, 2.2, 2.4, 2.5, 2.6.

        The ``Value`` property is a CloudFormation intrinsic expression
        — a ``Fn::Select`` whose second operand is a ``Fn::Split`` — not
        a literal string. This is what guarantees the value at rest is
        the bare revision integer extracted from the task-definition
        ARN.

        Manual traversal (rather than a ``Match`` matcher) keeps the
        failure message precise: a reviewer sees exactly which layer of
        the intrinsic drifted — outer select, index, inner split,
        delimiter, or ARN reference.
        """
        resources: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::SSM::Parameter"
        )
        assert len(resources) == 1, (
            f"expected exactly one AWS::SSM::Parameter; found {len(resources)}"
        )
        (only_resource,) = resources.values()
        value = only_resource["Properties"]["Value"]

        assert isinstance(value, Mapping), (
            f"SSM parameter Value must be an intrinsic dict, not a literal; "
            f"got {type(value).__name__}: {value!r}"
        )
        assert "Fn::Select" in value, (
            f"SSM parameter Value must be a Fn::Select intrinsic; got {value!r}"
        )
        select_args = value["Fn::Select"]
        assert select_args[0] == 6, (
            f"Fn::Select index must be 6 (revision segment); got {select_args[0]!r}"
        )
        assert isinstance(select_args[1], Mapping), (
            f"Fn::Select second arg must be a Fn::Split intrinsic; "
            f"got {type(select_args[1]).__name__}"
        )
        assert "Fn::Split" in select_args[1], (
            f"Fn::Select second arg must contain Fn::Split; got {select_args[1]!r}"
        )
        split_args = select_args[1]["Fn::Split"]
        assert split_args[0] == ":", (
            f"Fn::Split delimiter must be ':'; got {split_args[0]!r}"
        )
        # The second Fn::Split arg is a Ref to the task-def logical ID —
        # a dict of shape ``{"Ref": "TrikonVerifyRunnerTaskDef..."}``.
        assert isinstance(split_args[1], Mapping) and "Ref" in split_args[1], (
            f"Fn::Split second arg must be a Ref intrinsic; got {split_args[1]!r}"
        )

    def test_ssm_parameter_depends_on_task_definition(
        self, template: Template
    ) -> None:
        """Requirement 3.2.

        The ``AWS::SSM::Parameter``'s ``DependsOn`` list must be
        non-empty and must name the ``AWS::ECS::TaskDefinition`` logical
        id that the stack synthesizes.

        The task-def logical id is looked up defensively from a second
        :meth:`Template.find_resources` call rather than hardcoded to
        ``"TrikonVerifyRunnerTaskDef"`` — this keeps the test resilient
        to any future CDK hash-suffix on the task-def logical id.
        """
        task_defs: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::ECS::TaskDefinition"
        )
        assert len(task_defs) == 1, (
            f"expected exactly one AWS::ECS::TaskDefinition; found {len(task_defs)}"
        )
        (task_def_logical_id,) = task_defs.keys()

        ssm_params: Mapping[str, Mapping[str, Any]] = template.find_resources(
            "AWS::SSM::Parameter"
        )
        assert len(ssm_params) == 1, (
            f"expected exactly one AWS::SSM::Parameter; found {len(ssm_params)}"
        )
        (only_ssm_resource,) = ssm_params.values()
        depends_on = only_ssm_resource.get("DependsOn")
        assert depends_on is not None and len(depends_on) > 0, (
            f"SSM parameter must declare a non-empty DependsOn; got {depends_on!r}"
        )
        assert task_def_logical_id in depends_on, (
            f"SSM parameter DependsOn ({depends_on!r}) must include the "
            f"task-definition logical id {task_def_logical_id!r}"
        )

    def test_ssm_parameter_description_mentions_trikon_cloud_and_spec_3(
        self, template: Template
    ) -> None:
        """Requirement 1.6.

        The parameter's ``Description`` is a non-empty string containing
        both the phrase ``Trikon Cloud`` and a reference to Spec 3's
        ``OrchestratorStack`` as the consumer of this parameter.
        Enforces Invariant 7 (product name is exactly ``Trikon``) in
        the CDK-authored description. The alternation in the regex
        accepts either ordering of the two required phrases because
        neither Requirement 1.6 nor any downstream code cares about the
        exact wording between them.
        """
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {
                "Description": Match.string_like_regexp(
                    r"Trikon Cloud.*OrchestratorStack.*Spec 3|"
                    r"Trikon Cloud.*Spec 3.*OrchestratorStack"
                ),
            },
        )
