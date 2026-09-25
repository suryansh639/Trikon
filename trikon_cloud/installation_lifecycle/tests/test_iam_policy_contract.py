# CDK's jsii-generated stubs type every construct kwarg and every
# ``Template.find_resources`` return payload as ``Any``. Under this repo's
# ``disallow_any_explicit = true`` mypy config, both surface as
# ``explicit-any`` errors on library code we do not own. Suppress at
# file scope — every ``Any`` here is bounded to the CDK / hypothesis
# surface, never crossed into a production module. Mirrors the pattern
# used by ``trikon_cloud/fargate_runner/infra/fargate_runner_stack.py``
# and ``trikon_cloud/fargate_runner/tests/test_summary_builder.py``.
# mypy: disable-error-code="explicit-any"
"""Byte-match contract test — runtime IAM renderer vs Spec 2's CDK synth.

Feature: trikon-cloud-orchestrator,
Property 10: IAM policy runtime output byte-matches CDK synth.

Validates: Requirements 13.1, 13.2, 13.3, 13.4, 17.4.

This is the single highest-value test in the ``trikon-cloud-orchestrator``
spec (see ``.kiro/specs/trikon-cloud-orchestrator/design.md`` §6.5, §6.6,
§10.4 Property 10, §10.5). It locks the runtime per-installation IAM
policy renderer at
``trikon_cloud/installation_lifecycle/iam_template.py`` to the
CDK-synthesized ``PolicyDocument`` emitted by Spec 2's
``FargateRunnerStack.build_task_role_for_installation`` for the same
inputs. Any drift on either side — statement re-order, Sid rename,
condition-key change, action-list edit, `LeadingKeys` value change —
fails this test at contract-test time, before any live
``iam:PutRolePolicy`` call.

Test surface — two cases (per tasks.md task 19):

* ``test_iam_policy_byte_matches_cdk_synth_at_canonical_id`` — the
  single parametrized case at ``installation_id = 12345678``
  (Requirement 13.4) and the canonical App-key ARN. This is the
  minimum viable contract: if the sweep passes, this passes; if this
  fails, the sweep is uninformative.
* ``test_iam_policy_byte_matches_cdk_synth_hypothesis_sweep`` — a
  100-example hypothesis sweep over ``installation_id ∈ [1, 2⁶³)`` per
  design.md §10.4 Property 10. Catches any installation-id-dependent
  drift that a single fixed value would miss (e.g., a substring bug
  in an ARN template that only trips on multi-digit IDs).

On failure, both cases emit a unified :func:`difflib.ndiff` view of the
two canonical JSON strings so a reviewer can pinpoint which byte
drifted (Sid rename, Action re-order, LeadingKeys change, missing
Statement, etc.) without re-running under a debugger.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from typing import Any

import hypothesis.strategies as st
from aws_cdk import App, Environment
from aws_cdk.assertions import Template
from hypothesis import given, settings

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack
from trikon_cloud.installation_lifecycle.iam_template import (
    canonical_json,
    render_installation_policy_document,
)

# ---------------------------------------------------------------------------
# Canonical test inputs — pinned by Requirement 13.4 and design.md §10.5.
# ---------------------------------------------------------------------------

# Requirement 13.4 fixes the canonical single-case installation id.
_CANONICAL_INSTALLATION_ID: int = 12345678

# Requirement 13.4 fixes the canonical App private-key secret ARN. The
# runtime renderer places this string verbatim into Statement 3's
# ``Resource`` field; the CDK side embeds it via a Python f-string on
# the same field. The two must agree byte-for-byte.
_CANONICAL_APP_KEY_ARN: str = (
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-abcdef"
)

# design.md §10.5: the CDK App is bound to this account/region so the
# synthesized template resolves ``${AWS::AccountId}`` /
# ``${AWS::Region}`` intrinsics to concrete strings — no ``Fn::Sub``
# remains in the output. The runtime renderer is invoked with the same
# two values so both sides produce identical ARN substitutions.
_TEST_ACCOUNT_ID: str = "000000000000"
_TEST_REGION: str = "us-east-1"

# The ``FargateRunnerStack`` constructor requires ``app_id`` — Spec 2's
# GitHub App numeric id, baked into the ECS task-definition env. It is
# irrelevant to the per-installation IAM policy under test (that method
# never reads ``app_id``); a fixed placeholder is fine.
_UNUSED_APP_ID: int = 999999


# ---------------------------------------------------------------------------
# Helpers — one per side of the contract, plus the diff-emitting assert.
# ---------------------------------------------------------------------------


def _canonical_from_cdk(installation_id: int, app_key_arn: str) -> str:
    """Synthesize the CDK stack and canonicalize the per-installation policy.

    Implements design.md §10.5 verbatim. Instantiates a fresh
    :class:`aws_cdk.App` on every call (CDK constructs are single-use
    trees; reusing an ``App`` across ``installation_id`` values would
    collide on the ``role_name`` uniqueness constraint inside
    :meth:`FargateRunnerStack.build_task_role_for_installation`).

    Bound to ``account=000000000000`` and ``region=us-east-1`` per
    design.md §6.5 so the synthesized CloudFormation template contains
    fully-resolved ARN strings — no ``Fn::Sub`` intrinsics survive.

    The stack synth exposes multiple ``AWS::IAM::Policy`` resources
    (the ECS task-execution-role default policy plus the per-installation
    task-role default policy). The task-role policy is the one under
    test; it is identified by the ``TrikonVerifyRunnerTaskRole`` prefix
    in its CloudFormation logical id (see
    :meth:`FargateRunnerStack.build_task_role_for_installation` naming).
    """
    app = App()
    stack = FargateRunnerStack(
        app,
        "TestStack",
        app_private_key_secret_arn=app_key_arn,
        app_id=_UNUSED_APP_ID,
        env=Environment(account=_TEST_ACCOUNT_ID, region=_TEST_REGION),
    )
    stack.build_task_role_for_installation(
        installation_id,
        app_private_key_secret_arn=app_key_arn,
    )
    template = Template.from_stack(stack)
    resources: Mapping[str, Mapping[str, Any]] = template.find_resources(
        "AWS::IAM::Policy"
    )

    # The stack contains an ``AWS::IAM::Policy`` on the task-execution
    # role as well as on the per-installation task role; filter to the
    # latter by logical-id prefix. Using ``next`` with a generator
    # expression raises ``StopIteration`` if the task-role policy is
    # missing from the synth, which would itself be a byte-match
    # failure — surface it via an explicit ``AssertionError`` so a
    # reviewer sees a clear diagnostic instead of a bare stack trace.
    task_role_policy_key = next(
        (k for k in resources if "TrikonVerifyRunnerTaskRole" in k),
        None,
    )
    if task_role_policy_key is None:
        raise AssertionError(
            "CDK synth produced no AWS::IAM::Policy resource whose logical id "
            "contains 'TrikonVerifyRunnerTaskRole'; expected one per call to "
            "FargateRunnerStack.build_task_role_for_installation. "
            f"Available policy resource keys: {sorted(resources.keys())}"
        )

    policy_document = resources[task_role_policy_key]["Properties"]["PolicyDocument"]
    return json.dumps(policy_document, sort_keys=True, separators=(",", ":"))


def _canonical_from_runtime(installation_id: int, app_key_arn: str) -> str:
    """Render the per-installation policy at runtime and canonicalize.

    Invokes :func:`render_installation_policy_document` with the same
    ``account_id`` / ``region`` context the CDK synth is bound to (so
    ARN substitutions match on both sides), then feeds the returned
    frozen :class:`InstallationPolicyDocument` through
    :func:`canonical_json` (design.md §6.5 canonicalization: sorted
    keys, no whitespace, aliases applied, ``None``-valued fields
    suppressed).
    """
    return canonical_json(
        render_installation_policy_document(
            installation_id,
            app_private_key_secret_arn=app_key_arn,
            account_id=_TEST_ACCOUNT_ID,
            region=_TEST_REGION,
        )
    )


def _assert_byte_match(installation_id: int, app_key_arn: str) -> None:
    """Assert byte-equivalence of the two canonical JSON strings.

    On mismatch, emits a unified :func:`difflib.ndiff` of the two
    canonical strings so the reviewer can see exactly which byte
    drifted (task 19 sub-bullet 5). The diff is line-oriented over
    single-line canonical strings, so it is effectively a two-line
    contrast — one line per side — annotated with ``-`` / ``+`` /
    ``?`` markers pinpointing the offending column.
    """
    cdk_canonical = _canonical_from_cdk(installation_id, app_key_arn)
    runtime_canonical = _canonical_from_runtime(installation_id, app_key_arn)

    if cdk_canonical != runtime_canonical:
        diff = "\n".join(
            difflib.ndiff([cdk_canonical], [runtime_canonical]),
        )
        raise AssertionError(
            "IAM policy byte-match failure "
            f"(installation_id={installation_id}, app_key_arn={app_key_arn!r}). "
            "Runtime iam_template.py has drifted from Spec 2's CDK synth "
            "(design.md §6.5). ndiff (- CDK / + runtime):\n" + diff
        )


# ---------------------------------------------------------------------------
# Contract tests — Property 10.
# ---------------------------------------------------------------------------


def test_iam_policy_byte_matches_cdk_synth_at_canonical_id() -> None:
    """Feature: trikon-cloud-orchestrator, Property 10: IAM policy runtime output byte-matches CDK synth.

    Validates: Requirements 13.1, 13.2, 13.3, 13.4, 17.4.

    Pinned canonical case at ``installation_id = 12345678`` and the
    canonical App private-key ARN (Requirement 13.4). The single-case
    variant is the minimum viable byte-match contract; the hypothesis
    sweep below stress-tests it across the ``installation_id`` space.
    """
    _assert_byte_match(_CANONICAL_INSTALLATION_ID, _CANONICAL_APP_KEY_ARN)


@given(installation_id=st.integers(min_value=1, max_value=2**63 - 1))
@settings(max_examples=100, deadline=None)
def test_iam_policy_byte_matches_cdk_synth_hypothesis_sweep(
    installation_id: int,
) -> None:
    """Feature: trikon-cloud-orchestrator, Property 10: IAM policy runtime output byte-matches CDK synth.

    Validates: Requirements 13.1, 13.2, 13.3, 13.4, 17.4.

    Sweeps ``installation_id`` over the DynamoDB-legal integer range
    ``[1, 2⁶³)`` (per design.md §10.4 Property 10) to catch drift that
    depends on the id value — e.g., a substring bug in an ARN template
    that only trips on multi-digit ids, or an off-by-one in the
    ``LeadingKeys`` stringification.

    ``deadline=None`` disables hypothesis's per-example timeout because
    each example instantiates a fresh CDK ``App`` + ``FargateRunnerStack``
    and runs ``Template.from_stack`` — the synth cost per example is
    the dominant term, well beyond hypothesis's default 200 ms
    deadline.
    """
    _assert_byte_match(installation_id, _CANONICAL_APP_KEY_ARN)
