# Design Document

## §1 Overview

This amendment inserts exactly one construct — an `AWS::SSM::Parameter` at the
well-known name `/trikon/verify-runner/active-revision` — into Spec 2's
`FargateRunnerStack`. The value of that parameter is a CloudFormation intrinsic
that extracts the bare revision integer from the `FargateTaskDefinition` ARN at
deploy time, and a `DependsOn` edge sequences the parameter update after the
task-definition update on every stack update. That single publish operation is
the entire mechanism by which Spec 3's `TaskDefinitionResolver` (see
`.kiro/specs/trikon-cloud-orchestrator/design.md` §4.3) discovers the current
revision at Lambda cold start. Without it, every message on the
`trikon-verify-jobs` SQS queue routes to the DLQ because the resolver's
`ssm_client.get_parameter(...)` call raises `ParameterNotFound` and the
handler's Terminal branch fires. Nothing else in Spec 2 moves — the ECS
cluster, task definition family, three DynamoDB tables, S3 evidence bucket,
CloudWatch log group, execution role, and per-installation task-role builder
are untouched.

Spec 2's eight architecture invariants (from
`.kiro/specs/trikon-cloud-architecture/requirements.md`) all remain intact:
Invariant 1 (LeadingKeys IAM boundary) is unaffected because the new resource
grants no IAM to any tenant task role; Invariant 2 (Never-Fail-Open) is
unaffected because this parameter is read only by Spec 3's orchestrator, not
by the runner; Invariant 4 (SDK boundary) is unaffected because no runtime
Python code is touched; Invariant 5 (10-minute task wall time) is unaffected
because the parameter is deploy-time only; Invariant 7 (product name
"Trikon") is preserved verbatim in the parameter description; Invariant 8
(no `dict[str, Any]` on public API) is preserved because the sole public
attribute added — `self.active_revision_param_name: str` — is a plain string.
The remaining two invariants (idempotency and 30-day log retention) live in
runtime and log-group configuration that this amendment does not touch.

### §1.1 Deploy-time flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Release engineer runs:  cdk deploy FargateRunnerStack ...               │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  CDK synth  →  CloudFormation template.json                              │
│                                                                          │
│  Resources:                                                              │
│    TrikonVerifyRunnerTaskDef                                             │
│      Type: AWS::ECS::TaskDefinition                                      │
│      Properties: { family: "trikon-verify-runner", ... }                 │
│                                                                          │
│    ActiveRevisionParam<hash>                                             │
│      Type: AWS::SSM::Parameter                                           │
│      Properties:                                                         │
│        Name:  "/trikon/verify-runner/active-revision"                    │
│        Type:  "String"                                                   │
│        Value: Fn::Select[6, Fn::Split[":", Ref: TrikonVerifyRunnerTaskDef]]│
│      DependsOn: [TrikonVerifyRunnerTaskDef]                              │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  CloudFormation execution order:                                         │
│    1. Create/update TrikonVerifyRunnerTaskDef → new revision N           │
│       (ARN: arn:aws:ecs:us-east-1:123:task-definition/trikon-verify-     │
│        runner:N)                                                         │
│    2. Create/update ActiveRevisionParam → value "N"                      │
│       (DependsOn edge forces this ordering on every stack update)        │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Spec 3 OrchestratorStack (already deployed or subsequently deployed):   │
│                                                                          │
│  On first cold start after this deploy:                                  │
│    TaskDefinitionResolver.resolve(ssm_client=_ssm_client())              │
│      → ssm_client.get_parameter(Name="/trikon/verify-runner/             │
│         active-revision")                                                │
│      → resp.Parameter.Value == "N"                                       │
│      → self._cache = f"trikon-verify-runner:{N}"                         │
│      → returned to Orchestrator_Handler for RunTaskCall.task_definition  │
│                                                                          │
│  Subsequent invocations reuse the module-scope cache — no per-invocation │
│  ssm:GetParameter call.                                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

The amendment's blast radius is exactly one CDK file, one README file, and one
new test file. No runtime Python module changes. No IAM policy changes for
tenant task roles. No changes to Spec 3's stack, handler, resolver, or tests.

## §2 The CDK snippet

The construct is inserted into
`trikon_cloud/fargate_runner/infra/fargate_runner_stack.py` immediately after
the existing `task_def.add_container(...)` call that closes section 7 (around
line 259 of the current file) and immediately before the section 8 header
`# 8. Public attributes for Spec 3 to consume ...` (around line 262). The two
top-of-file imports are extended: `Fn` is added to the existing
`from aws_cdk import ...` line and a new `aws_ssm` import is added below the
`aws_s3` import.

### §2.1 Import additions

```python
from aws_cdk import Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
```

Both additions land in the existing top-of-file import block. Ruff's `I` rule
(isort) preserves the alphabetical ordering within each of the two groups
(bare-name imports, then aliased `aws_*` imports); `Fn` slots between
`Duration` and `RemovalPolicy` and `aws_ssm as ssm` slots after `aws_s3 as s3`.

### §2.2 Construct block

```python
# -------------------------------------------------------------
# 7b. SSM parameter — publishes the active task-def revision so
# Spec 3's orchestrator can resolve it at cold start (design.md
# §2 of trikon-cloud-fargate-runner-ssm-active-revision).
# -------------------------------------------------------------
active_revision_param = ssm.StringParameter(
    self,
    "ActiveRevisionParam",
    parameter_name="/trikon/verify-runner/active-revision",
    string_value=Fn.select(6, Fn.split(":", task_def.task_definition_arn)),
    description=(
        "Trikon Cloud — active revision of the trikon-verify-runner "
        "task definition. Consumed by the OrchestratorStack (Spec 3) "
        "TaskDefinitionResolver at cold start."
    ),
)
active_revision_param.node.add_dependency(task_def)

# Expose for downstream reference (e.g. cross-stack imports)
self.active_revision_param_name = active_revision_param.parameter_name
```

### §2.3 Element-by-element explanation

**`ssm.StringParameter`.** The CDK L2 construct `aws_cdk.aws_ssm.StringParameter`
synthesizes to a single `AWS::SSM::Parameter` resource with `Type: String` by
default; the `type=` kwarg is only required for `StringList` or (via the
separate `StringListParameter` construct) `SecureString`. Passing no `type=`
argument here is the correct way to satisfy Requirement 1.3 (`Type == "String"`)
and Requirements 1.4 and 1.5 (must not be `SecureString` or `StringList`). The
`parameter_name` kwarg maps to the CloudFormation `Name` property.

**`Fn.select(6, Fn.split(":", task_def.task_definition_arn))`.** An ECS
task-definition ARN has the canonical shape

```
arn:aws:ecs:{region}:{account}:task-definition/{family}:{revision}
```

with exactly seven `:`-delimited segments. Splitting on `:` yields eight tokens
(indices 0 through 7). The worked example for a fully-resolved ARN in the
us-east-1 account `123456789012` at revision 17:

| Index | Token                          |
|-------|--------------------------------|
| 0     | `arn`                          |
| 1     | `aws`                          |
| 2     | `ecs`                          |
| 3     | `us-east-1`                    |
| 4     | `123456789012`                 |
| 5     | `task-definition/trikon-verify-runner` |
| 6     | `17`                           |

`Fn.select(6, ...)` returns the token at index 6 — the bare revision integer
`"17"`. Note that the `task-definition/{family}` prefix collapses into a single
token because `/` is not the delimiter; only `:` is. Spec 3's resolver parses
the returned string with `int(parsed.Parameter.Value)` and enforces
`revision >= 1` (see
`.kiro/specs/trikon-cloud-orchestrator/design.md` §4.3), so the value at rest
must be a bare positive integer with no prefix, suffix, or `:LATEST`
literal — the `Fn.select` / `Fn.split` composition guarantees exactly that.

**`add_dependency(task_def)`.** Belt-and-suspenders. CDK already induces an
implicit dependency because `task_def.task_definition_arn` is a token that
references the task-definition resource; CFN synth will emit a `DependsOn`
edge automatically. The explicit `add_dependency` call is Requirement 3.1
verbatim — it makes the deploy-order guarantee visible in Python source
without depending on CDK's internal token-tracking to survive future
refactors. Both edges collapse to the same `DependsOn: [TrikonVerifyRunnerTaskDef]`
entry in the synthesized template.

**`self.active_revision_param_name`.** A plain-string public attribute added to
the class. Follows the same pattern as the existing `self.cluster_arn`,
`self.task_definition_arn`, and `self.verdicts_table_name` attributes in
section 8. Downstream consumers (a Spec 3 follow-up, or a future
cross-stack reference) can read it as `stack.active_revision_param_name` or
export it via a CFN Output. This amendment does not add a CFN Output; the
Python attribute alone is the extension point.

### §2.4 Placement — why between section 7 and section 8

The construct must land after both `task_def = ecs.FargateTaskDefinition(...)`
and its `task_def.add_container(...)` call because the `Fn.split` intrinsic
takes `task_def.task_definition_arn` as its operand, and the CDK token for
that ARN is only resolvable after the FargateTaskDefinition L2 construct is
instantiated. Placing the block before section 8 (public attributes)
preserves the current section-8-as-final-block convention and keeps the new
`self.active_revision_param_name` assignment adjacent to the other three
public attributes for reviewer sightlines.

### §2.5 mypy `--strict` compliance

The three names touched by this amendment (`Fn`, `ssm`, `active_revision_param`,
plus the string assignment to `self.active_revision_param_name`) all have
narrow, non-`Any` types under the file-scope
`# mypy: disable-error-code="explicit-any"` pragma. Specifically:

- `Fn` — `type[aws_cdk.Fn]`, a class with `@staticmethod` methods `select`
  and `split` whose return type is `str` (a CDK token).
- `ssm` — the `aws_cdk.aws_ssm` module.
- `active_revision_param` — `ssm.StringParameter`.
- `self.active_revision_param_name` — `str`.

To keep mypy's `--strict` mode happy, the class body must declare the new
attribute alongside the existing three:

```python
class FargateRunnerStack(Stack):
    cluster_arn: str
    task_definition_arn: str
    verdicts_table_name: str
    active_revision_param_name: str
```

That single-line class-level annotation is the fourth line touched in the
class body and lands adjacent to the existing three.

## §3 CFN template shape

The synthesized template contains one new resource, alphabetically sorted
after the existing `Trikon*` resources under `Resources`. CDK appends the
usual 8-character SHA suffix to the logical ID (`ActiveRevisionParam` +
hash) — the hash is deterministic per construct address, but tests must
find the resource by properties, not by exact logical ID.

```yaml
Resources:
  ActiveRevisionParam9E1A2C3D:
    Type: AWS::SSM::Parameter
    Properties:
      Name: /trikon/verify-runner/active-revision
      Type: String
      Value:
        Fn::Select:
          - 6
          - Fn::Split:
              - ":"
              - Ref: TrikonVerifyRunnerTaskDef
      Description: >-
        Trikon Cloud — active revision of the trikon-verify-runner
        task definition. Consumed by the OrchestratorStack (Spec 3)
        TaskDefinitionResolver at cold start.
    DependsOn:
      - TrikonVerifyRunnerTaskDef
    Metadata:
      aws:cdk:path: FargateRunnerStack/ActiveRevisionParam/Resource
```

### §3.1 What each rendered field validates

- `Type: AWS::SSM::Parameter` — Requirement 1.1 (exactly one such resource).
- `Properties.Name` — Requirement 1.2 (exact string).
- `Properties.Type` — Requirement 1.3 (exact string), 1.4 and 1.5 (negatives).
- `Properties.Value` — Requirement 2.1 (deploy-time derivation via
  `Fn.select`/`Fn.split`), 2.2 (emitted as an intrinsic expression, not a
  literal), 2.4 (not a literal string), 2.5 (does not contain `:LATEST`),
  2.6 (produces no non-numeric characters at rest).
- `Properties.Description` — Requirement 1.6 (contains "Trikon Cloud" and
  references Spec 3).
- `DependsOn` — Requirement 3.2 (names the task-def logical ID).

The `Ref: TrikonVerifyRunnerTaskDef` under the inner `Fn::Split` resolves at
deploy time to the full task-definition ARN (CloudFormation's `Ref` on an
`AWS::ECS::TaskDefinition` returns the ARN, not just the family). CDK's synth
inserts the actual logical ID CDK generated for `task_def` — literally
`TrikonVerifyRunnerTaskDef` in the current file with no hash suffix because
the L2 FargateTaskDefinition construct uses a stable name.

### §3.2 CloudFormation update-ordering guarantee (Requirement 3.3, 3.4)

The `DependsOn: [TrikonVerifyRunnerTaskDef]` edge tells CFN to complete every
create/update on the task-definition resource before starting the SSM
parameter's create/update. If the task-def update fails (image pull failure,
IAM error, ...), CFN halts the stack update and rolls back — the SSM parameter
update never fires, and the previous parameter value survives. Requirement
3.4's negative claim ("SSM parameter update SHALL NOT be applied [if task-def
fails]") is not something the CDK snippet actively enforces; it is a property
of CloudFormation's `DependsOn` semantics that the snippet inherits.

## §4 Test design

The new test file lives at
`trikon_cloud/fargate_runner/tests/test_ssm_active_revision.py`. It contains
one test class with four synth-time assertions. All four tests share a
module-scope fixture that builds the stack once (CDK synth is the dominant
cost) — the class body is thin.

### §4.1 File header and imports

```python
# mypy: disable-error-code="explicit-any"
"""Synth-time assertions for the SSM Active-Revision Parameter (design.md §3).

Verifies the amendment adds by ``.kiro/specs/trikon-cloud-fargate-runner-
ssm-active-revision``: exactly one ``AWS::SSM::Parameter`` at the well-known
name ``/trikon/verify-runner/active-revision``, with ``Type: String``, value
derived at deploy time via ``Fn::Select`` over ``Fn::Split`` on the
task-definition ARN, and a ``DependsOn`` edge on the task definition.

Spec 2's pre-existing 77 tests are NOT modified — those files stay on the
77-passing baseline. This file is additive and dedicated to the amendment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack
```

### §4.2 Fixtures — mirror Spec 2's IAM-contract test setup

The `App() → FargateRunnerStack(...)` construction matches the pattern already
used in `trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py`
lines 122-131. Mock kwargs come from the same canonical constants used there:
`app_id=999999`, `account="000000000000"`, `region="us-east-1"`, and a mock
Secrets Manager ARN string. The Template is built once at module scope
because Template is read-only after synth.

```python
_TEST_ACCOUNT: str = "000000000000"
_TEST_REGION: str = "us-east-1"
_MOCK_APP_KEY_ARN: str = (
    "arn:aws:secretsmanager:us-east-1:000000000000:"
    "secret:trikon-cloud/github-app-private-key-abcdef"
)
_UNUSED_APP_ID: int = 999999


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize FargateRunnerStack once and return its Template.

    Module scope because Template is immutable after synth and every test
    method in this file reads from the same synth. Rebuilding per-test would
    triple the wall-clock of the suite for no isolation gain — the stack
    holds no mutable state a test could pollute.
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
```

### §4.3 Test class and four methods

```python
class TestSsmActiveRevisionParameter:
    """Requirement 4.2-4.6: dedicated synth-time coverage for the amendment."""

    def test_ssm_parameter_exists_with_correct_name_and_type(
        self, template: Template
    ) -> None:
        """Requirements 1.1, 1.2, 1.3, 1.4, 1.5.

        Exactly one AWS::SSM::Parameter resource exists, with Name equal to
        ``/trikon/verify-runner/active-revision`` and Type equal to
        ``String`` (never ``SecureString`` or ``StringList``).
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

        The Value property is a CloudFormation intrinsic expression — a
        ``Fn::Select`` whose second operand is a ``Fn::Split`` — not a
        literal string. This is what guarantees the value at rest is the
        bare revision integer extracted from the task-definition ARN.
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
        # a dict of shape {"Ref": "TrikonVerifyRunnerTaskDef..."}.
        assert isinstance(split_args[1], Mapping) and "Ref" in split_args[1], (
            f"Fn::Split second arg must be a Ref intrinsic; got {split_args[1]!r}"
        )

    def test_ssm_parameter_depends_on_task_definition(
        self, template: Template
    ) -> None:
        """Requirement 3.2.

        The AWS::SSM::Parameter's DependsOn list must be non-empty and must
        name the AWS::ECS::TaskDefinition logical ID that the stack
        synthesizes.
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

        The parameter's Description is a non-empty string containing both
        the phrase ``Trikon Cloud`` and a reference to Spec 3 as the
        consumer of this parameter. Enforces Invariant 7 (product name is
        exactly ``Trikon``) in the CDK-authored description.
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
```

### §4.4 Rationale for the four methods

Each method addresses a distinct clause of the requirements document and can
fail independently — the split matches Requirement 4.3-4.6 verbatim, which
enumerates four assertions.

1. **`test_ssm_parameter_exists_with_correct_name_and_type`** collapses
   Requirements 1.1/1.2/1.3 (positives) with 1.4/1.5 (negatives). The
   `has_resource_properties` matcher passes only when both `Name` and
   `Type` match exactly; if `Type` were `SecureString` or `StringList` the
   assertion fails, so 1.4 and 1.5 are covered transitively without a
   separate test.

2. **`test_ssm_parameter_value_uses_fn_select_fn_split_over_task_def_arn`**
   walks the intrinsic tree manually because CDK's `Match` matchers cannot
   assert deeply-nested intrinsic shapes as tersely. Manual traversal keeps
   the failure message precise — a reviewer sees exactly which layer of the
   intrinsic drifted.

3. **`test_ssm_parameter_depends_on_task_definition`** cross-references the
   two `find_resources` calls so the test tolerates the CDK hash suffix on
   the task-definition logical ID. Hardcoding the literal
   `"TrikonVerifyRunnerTaskDef"` would break the test if CDK's L2 construct
   ever appended a hash; looking it up defensively is future-proof.

4. **`test_ssm_parameter_description_mentions_trikon_cloud_and_spec_3`** uses
   a regex matcher because either ordering of the two required phrases is
   acceptable and neither Requirement 1.6 nor any downstream code cares
   about the exact wording between them.

### §4.5 What the tests do NOT do

- They do not deploy the stack. All four are synth-time — they call
  `Template.from_stack(...)` and inspect the resulting JSON. No AWS
  credentials, no `moto`, no network I/O.
- They do not exercise the `build_task_role_for_installation` method. That
  path is covered by Spec 2's existing
  `test_iam_policy_contract.py` and remains unchanged.
- They do not import `trikon_cloud.orchestrator.*`. Spec 3 is a separate
  package and this test file must run without Spec 3 installed. The
  amendment is validated against the CFN template only.

## §5 README amendment

The prose lands in `trikon_cloud/fargate_runner/infra/README.md` as a new
`## 8. Deploy Ordering` section inserted after the current `## 7. Deviations
flagged for the release engineer` block. The section is ~100 words and
states the ordering requirement in operational terms.

```markdown
## 8. Deploy Ordering

`FargateRunnerStack` (this Spec 2 stack) MUST be deployed before Spec 3's
`OrchestratorStack`. On the first cold start after deploy, the orchestrator
Lambda calls `ssm:GetParameter` on `/trikon/verify-runner/active-revision`
to resolve the task-definition revision it will pass to `ecs.RunTask`. If
the parameter is missing (Spec 3 deployed before Spec 2), every incoming
SQS message hits the resolver's `ParameterNotFound` branch, the handler's
Terminal path fires, and every job routes to the DLQ. Recommended sequence
for a from-scratch deploy:

```
uv run cdk deploy FargateRunnerStack \
  -c app_private_key_secret_arn=<arn> \
  -c app_id=<id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"

uv run cdk deploy TrikonCloudOrchestratorStack \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

Subsequent redeploys of either stack can run in any order because CDK
regenerates the SSM parameter on every `FargateRunnerStack` deploy and the
orchestrator picks up the new value on its next cold start.
```

### §5.1 Note on the section 7 numbering

The current README has sections 1-7 with section 7 titled "Deviations
flagged for the release engineer". Inserting Deploy Ordering as section 8
preserves every existing anchor and cross-reference. The Deviations
section stays where it is; Deploy Ordering slots after it.

## §6 Coordinated changes (out of scope)

This amendment is intentionally narrow. It does NOT touch:

- **Spec 3's stack or handler code.** The orchestrator's
  `TaskDefinitionResolver.resolve` method already reads the parameter at
  the correct name and parses the value as `int` (see
  `trikon_cloud/orchestrator/ecs_dispatcher.py`); no changes are needed
  there. Spec 3's IAM policy for the Lambda execution role already grants
  `ssm:GetParameter` on `arn:aws:ssm:*:*:parameter/trikon/verify-runner/
  active-revision`.

- **Spec 2's task definition, containers, IAM roles, DynamoDB tables, S3
  bucket, VPC, ECS cluster, log group, or execution role.** Every existing
  construct in `fargate_runner_stack.py` stays byte-identical.

- **Spec 2's existing 77 tests** in
  `trikon_cloud/fargate_runner/tests/test_*.py` (nine files). They stay
  green as-is; this amendment adds a tenth file next to them.

- **Custom Resources.** The amendment does NOT introduce a Lambda-backed
  Custom Resource. The pure CFN intrinsic `Fn.select`/`Fn.split` composition
  is deploy-time-only, requires no runtime code, and cannot fail at
  deploy time except in ways that also fail the task-def deploy.

- **CFN Outputs.** The amendment does NOT add a `CfnOutput` for the
  parameter name or ARN. Spec 3 hard-codes the parameter name via its
  own env var `TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM` (defaulting
  to `/trikon/verify-runner/active-revision`); cross-stack CFN discovery
  is not required. The Python-level `self.active_revision_param_name`
  attribute is the only new extension point.

- **Runtime behavior.** No `.py` file under `trikon_cloud/fargate_runner/`
  outside `infra/` is touched. The Docker image, entrypoint, models, GitHub
  client, DynamoDB writer, summary builder, token cache, git ops, and
  logger all stay on their current baseline.

## §7 Testing strategy summary

**Test count.** Spec 2's baseline is 77 passing tests across nine test files
under `trikon_cloud/fargate_runner/tests/`. This amendment adds one new
file (`test_ssm_active_revision.py`) with four synth-time test methods.
After the amendment lands:

```
uv run pytest trikon_cloud/fargate_runner/tests -v
```

exits 0 with 81 tests (77 pre-existing + 4 new).

**Static checks.** All three quality gates pass on the amendment:

- `uv run mypy --strict trikon_cloud/fargate_runner/infra/fargate_runner_stack.py`
  clean. The two new imports (`Fn`, `aws_ssm as ssm`) resolve to fully
  typed CDK classes; the new construct call and public attribute
  assignment are all narrowly typed (`ssm.StringParameter`, `str`).
- `uv run mypy --strict trikon_cloud/fargate_runner/tests/test_ssm_active_revision.py`
  clean. The file-scope `# mypy: disable-error-code="explicit-any"`
  pragma matches the existing test conftest pragma and covers the
  `Mapping[str, Any]` used by `template.find_resources` (CDK's
  jsii-generated stub types that as `Any` on the value side).
- `uv run ruff check trikon_cloud/fargate_runner/` clean. New imports slot
  into the existing isort groups; the new test file follows the same
  docstring and blank-line conventions as `test_iam_policy_contract.py`.

**Synth check.** `uv run cdk synth FargateRunnerStack -c app_private_key_secret_arn=...
-c app_id=... --app "python -m trikon_cloud.fargate_runner.infra.app"`
succeeds and emits a template.json in `cdk.out/` that contains one
`AWS::SSM::Parameter` resource. The four test methods run against a
`Template.from_stack(...)` build of the same stack, so the synth check
and the test-suite check exercise the same code path.

**No changes required to existing test files.** Requirement 4.1 explicitly
mandates the 77-baseline stays untouched. The four new tests are contained
in a single new file and neither reads nor writes any state shared with
existing test files. `test_iam_policy_contract.py` uses the same
`App → FargateRunnerStack → Template.from_stack` pattern, so a reviewer
familiar with that file will recognize the setup here immediately.

**Test wall-clock budget.** Each of the four tests runs against a shared
module-scope `Template` fixture, so the CDK synth cost (~1-2 seconds for
this stack) is paid once, not four times. Total added wall-clock is under
3 seconds, keeping the full runner suite well inside the pre-commit
budget.
