# Requirements Document

## Introduction

This feature amends the Trikon Cloud `FargateRunnerStack` (Spec 2) so that every deploy publishes the current verification-runner task-definition revision number to a well-known AWS Systems Manager (SSM) Parameter Store parameter. Spec 3's orchestrator (`TaskDefinitionResolver` in `trikon_cloud/orchestrator/ecs_dispatcher.py`) reads this parameter at cold start to discover which task-definition revision to launch. Without this parameter, Spec 3 cannot resolve a concrete revision and cold-start dispatch fails.

The amendment adds one `AWS::SSM::Parameter` resource of type `String` at path `/trikon/verify-runner/active-revision`, whose value is derived at deploy time from the seventh colon-delimited segment of the current `FargateTaskDefinition` ARN using the CloudFormation intrinsic `Fn.select(6, Fn.split(":", task_def.task_definition_arn))`. An explicit CDK dependency (`ssm_param.node.add_dependency(task_def)`) sequences the parameter update after the task-definition update on every stack update, so the parameter never points at a stale revision.

The construct is added to `trikon_cloud/fargate_runner/infra/fargate_runner_stack.py` immediately below the existing `FargateTaskDefinition` (around line 237) and before the `# 8. Public attributes` block (around line 275). Spec 2's existing 77 tests remain untouched; a dedicated new test file `trikon_cloud/fargate_runner/tests/test_ssm_active_revision.py` verifies the new resource against synthesized CloudFormation. Spec 2's infra README is amended with a "Deploy Ordering" section documenting that `FargateRunnerStack` must be deployed before `OrchestratorStack`.

## Glossary

- **SSM Active-Revision Parameter**: The single `AWS::SSM::Parameter` resource created by `FargateRunnerStack` at name `/trikon/verify-runner/active-revision`, of type `String`, whose value is the bare integer revision number (e.g. `"17"`) of the current verification-runner task definition. Sole consumer is Spec 3's `TaskDefinitionResolver.resolve` method, which reads it via `ssm_client.get_parameter(Name=...)` and parses the value as `int`.
- **Task-Definition ARN Revision Suffix**: The seventh colon-delimited segment (zero-indexed position 6) of an ECS task-definition ARN of the form `arn:aws:ecs:{region}:{account}:task-definition/{family}:{revision}`. Extracted at deploy time via `Fn.select(6, Fn.split(":", task_def.task_definition_arn))`. This segment is always a positive integer as a string, never containing `:LATEST` or any non-numeric characters.
- **Deploy-Order Dependency**: An explicit CDK node dependency declared as `ssm_param.node.add_dependency(task_def)` that forces CloudFormation to apply the task-definition revision update before the SSM parameter update on every stack update, guaranteeing the parameter never publishes a stale revision number.

## Requirements

### Requirement 1

**User Story:** As a Trikon Cloud platform operator, I want the `FargateRunnerStack` to publish exactly one SSM parameter at a well-known name and type, so that Spec 3's `TaskDefinitionResolver` can reliably discover the current verification-runner task-definition revision at cold start.

#### Acceptance Criteria

1. THE FargateRunnerStack SHALL create exactly one `AWS::SSM::Parameter` resource in the synthesized CloudFormation template.
2. THE FargateRunnerStack SHALL set the SSM parameter `Name` property to the exact string `/trikon/verify-runner/active-revision`.
3. THE FargateRunnerStack SHALL set the SSM parameter `Type` property to the exact string `String`.
4. THE FargateRunnerStack SHALL NOT set the SSM parameter `Type` property to `SecureString`.
5. THE FargateRunnerStack SHALL NOT set the SSM parameter `Type` property to `StringList`.
6. THE FargateRunnerStack SHALL set the SSM parameter `Description` property to a non-empty string that contains the phrase `Trikon Cloud` and references Spec 3 as the consumer.

### Requirement 2

**User Story:** As a Trikon Cloud platform operator, I want the SSM parameter value to be extracted from the CURRENT task-definition ARN at deploy time, so that the parameter always reflects the revision CloudFormation just created rather than a hardcoded or stale value.

#### Acceptance Criteria

1. THE FargateRunnerStack SHALL derive the SSM parameter `Value` property at deploy time from the `FargateTaskDefinition` constructed in the same stack by applying `aws_cdk.Fn.select(6, aws_cdk.Fn.split(":", task_def.task_definition_arn))`.
2. WHEN the CloudFormation template is synthesized, THE FargateRunnerStack SHALL emit the SSM parameter `Value` property as a CloudFormation intrinsic expression referencing `Fn::Select` over `Fn::Split` applied to the task-definition ARN, not as a literal string.
3. WHEN the stack is deployed, THE SSM parameter value at rest SHALL parse as a positive integer of at least 1, matching the `>= 1` precondition enforced by Spec 3's `TaskDefinitionResolver.resolve`.
4. THE FargateRunnerStack SHALL NOT set the SSM parameter `Value` property to a hardcoded string literal.
5. THE FargateRunnerStack SHALL NOT set the SSM parameter `Value` property to any expression that contains the substring `:LATEST`.
6. THE FargateRunnerStack SHALL NOT set the SSM parameter `Value` property to any expression that produces non-numeric characters at rest, including but not limited to prefixes such as `revision-`, suffixes, or JSON envelopes.

### Requirement 3

**User Story:** As a Trikon Cloud platform operator, I want the SSM parameter update to be explicitly sequenced after the task-definition update on every stack update, so that the parameter never publishes a revision number that CloudFormation has not yet created.

#### Acceptance Criteria

1. THE FargateRunnerStack SHALL declare an explicit CDK node dependency by invoking `ssm_param.node.add_dependency(task_def)` after both constructs are instantiated.
2. WHEN the CloudFormation template is synthesized, THE SSM parameter resource SHALL include a `DependsOn` entry that names the `FargateTaskDefinition` logical ID.
3. WHEN the stack is updated with a task-definition change, THE CloudFormation update ordering SHALL apply the task-definition revision update before the SSM parameter update.
4. IF the task-definition update fails during a stack update, THEN THE SSM parameter update SHALL NOT be applied, leaving the previous parameter value in place.

### Requirement 4

**User Story:** As a Trikon Cloud maintainer, I want the new construct verified in isolation and the deploy ordering documented, so that Spec 2's existing test surface stays untouched and future operators know Spec 2 must be deployed before Spec 3.

#### Acceptance Criteria

1. THE Spec 2 pre-existing suite of 77 tests SHALL continue to pass without any modification to those test files.
2. THE FargateRunnerStack amendment SHALL be verified by a dedicated new test file at the exact path `trikon_cloud/fargate_runner/tests/test_ssm_active_revision.py`.
3. THE new test file SHALL synthesize the `FargateRunnerStack` and assert that the resulting CloudFormation template contains an `AWS::SSM::Parameter` resource with `Name` equal to `/trikon/verify-runner/active-revision`.
4. THE new test file SHALL assert that the `AWS::SSM::Parameter` resource has `Type` equal to `String`.
5. THE new test file SHALL assert that the `AWS::SSM::Parameter` resource `Value` property is a `Fn::Select` intrinsic over a `Fn::Split` intrinsic applied to the task-definition ARN, not a literal string.
6. THE new test file SHALL assert that the `AWS::SSM::Parameter` resource `DependsOn` entry names the `FargateTaskDefinition` logical ID.
7. THE Spec 2 infra README SHALL be amended with a section titled `Deploy Ordering` that states `FargateRunnerStack` must be deployed before `OrchestratorStack` because Spec 3's `TaskDefinitionResolver` reads the SSM Active-Revision Parameter at cold start.
