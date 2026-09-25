# `OrchestratorStack` — deploy runbook

CDK Python stack for the Trikon Cloud orchestrator (Spec 3 of the M1
milestone). Provisions two Python 3.11 arm64 Lambda functions
(`trikon-cloud-orchestrator` and `trikon-cloud-installation-lifecycle`),
one SQS queue (`trikon-cloud-installation-events`), one dead-letter queue
(`trikon-cloud-installation-events-dlq`), and one DynamoDB TableV2
(`trikon-cloud-installations`). The orchestrator Lambda bridges Spec 1's
`trikon-verify-jobs` queue to Spec 2's Fargate `trikon-verify-runner`
task definition; the lifecycle Lambda consumes GitHub `installation` and
`installation_repositories` events and provisions the per-installation
IAM task roles that Spec 2's task definition assumes. See
`.kiro/specs/trikon-cloud-orchestrator/design.md` §8 for the CDK
topology and §9 for the coordinated amendments to Spec 1 and Spec 2.

> **Callout.** The deploy commands below are **informational**. The
> release engineer runs them, not the implementation tasks in this spec.
> All spec tasks stop at `cdk synth` (offline, no AWS API calls). The
> manual gate to `cdk deploy` is a release-engineer action per
> Requirement 16.4.

## 1. Prerequisites

- AWS account with credentials configured (env vars,
  `~/.aws/credentials`, or an SSO profile that the CDK CLI can pick up).
- Node.js and the `cdk` CLI installed (`npm install -g aws-cdk`).
- The Trikon repo checked out at HEAD.
- Repo-root dev sync: `uv sync --extra cloud --extra dev`. The `cloud`
  extra pulls both Lambdas' runtime deps (aws-lambda-powertools,
  pydantic, pydantic-settings, boto3, botocore, httpx, PyJWT[crypto]);
  the `dev` extra pulls `aws-cdk-lib` and `constructs` for the CDK synth
  step.
- **Spec 1 must have been deployed.** The orchestrator's Lambda event
  source references `arn:aws:sqs:us-east-1:<account>:trikon-verify-jobs`
  and its DLQ send grant references
  `arn:aws:sqs:us-east-1:<account>:trikon-verify-jobs-dlq`. Both are
  owned by `WebhookReceiverStack` and imported by ARN (never re-created
  by this stack). If either queue is missing at deploy time the stack
  update fails on the `EventSourceMapping` resource.
- **Spec 2 must have been deployed and its amendment applied.** The
  orchestrator's `TaskDefinitionResolver` cold-starts by reading the SSM
  parameter `/trikon/verify-runner/active-revision`; this parameter is
  written by Spec 2's amended `FargateRunnerStack` on every deploy (see
  design.md §9.2). If the parameter does not exist, the orchestrator
  Lambda's first invocation raises `ParameterNotFound` and every SQS
  message is redriven to the DLQ.
- The Secrets Manager secret holding the GitHub App's PEM-encoded
  private key must exist in the target region (us-east-1). **This stack
  does NOT create the secret** — it references it by ARN via the
  `app_private_key_secret_arn` CDK context value and only grants
  `secretsmanager:GetSecretValue` to the orchestrator Lambda role.
- Two private subnets and one security group already exist in the VPC
  Spec 2 provisioned. Their IDs feed the runner's `awsvpcConfiguration`
  via `runner_subnet_ids` and `runner_security_group_ids` context.

### 1.1 CDK context values

Every deploy of `TrikonCloudOrchestratorStack` requires five context
values, passed via `-c key=value` on the `cdk` command line:

| Context key                    | Purpose                                                                                          |
|--------------------------------|--------------------------------------------------------------------------------------------------|
| `app_private_key_secret_arn`   | ARN of the Secrets Manager secret holding the App's PEM private key (Never-Fail-Open JWT mint). |
| `github_app_id`                | The App's numeric ID (used to mint installation-scoped JWTs).                                    |
| `runner_subnet_ids`            | Comma-separated list of private subnet IDs, e.g. `subnet-a,subnet-b`.                            |
| `runner_security_group_ids`    | Comma-separated list of security group IDs, e.g. `sg-verify`.                                    |
| `aws_account_id`               | 12-digit AWS account. Falls back to `CDK_DEFAULT_ACCOUNT` when unset.                            |

None of these values are secrets; they are ARNs, IDs, and account
numbers safe to check in to CI configuration. The App private key
bytes themselves live in Secrets Manager and are never referenced in
this runbook.

## 2. Synth

Offline — makes no AWS API calls, exits 0 if the emitted CloudFormation
template is valid:

```
uv run cdk synth \
  -c app_private_key_secret_arn=arn:aws:secretsmanager:us-east-1:000000000000:secret:trikon/app-key-mock-abcdef \
  -c github_app_id=999999 \
  -c runner_subnet_ids=subnet-a,subnet-b \
  -c runner_security_group_ids=sg-verify \
  -c aws_account_id=000000000000 \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

The emitted template lands at
`cdk.out/TrikonCloudOrchestratorStack.template.json`. The contract test
in `trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py`
byte-matches the rendered inline policy against Spec 2's assumed
per-installation role — running the synth locally is a prerequisite
for that test.

## 3. Deploy

Manual release-engineer gate; **NOT** part of the implementation tasks
in this spec. Substitute the real ARN, App ID, subnets, security group,
and account for the placeholders:

```
uv run cdk deploy TrikonCloudOrchestratorStack \
  -c app_private_key_secret_arn=<real-app-key-arn> \
  -c github_app_id=<real-app-id> \
  -c runner_subnet_ids=<real-subnet-ids> \
  -c runner_security_group_ids=<real-sg-ids> \
  -c aws_account_id=<real-account-id> \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

## 4. Coordinated changes to other specs

Both amendments below are **NOT owned by this stack**. They ship in
coordinated PRs against Spec 1 and Spec 2 (see design.md §9). Local
unit and CDK-synth tests for Spec 3 pass without either amendment;
end-to-end deploy requires both.

- **Spec 1 (WebhookReceiverStack) — installation events routing branch.**
  Amend `trikon_cloud.webhook_receiver.handler._route_event` to route
  `X-GitHub-Event: installation` and `X-GitHub-Event: installation_repositories`
  deliveries to the new `trikon-cloud-installation-events` queue instead
  of returning HTTP 204. Requires (a) a new env var
  `TRIKON_INSTALLATION_EVENTS_QUEUE_URL` on the receiver Lambda,
  (b) a new `sqs:SendMessage` grant on the receiver role scoped to
  `arn:aws:sqs:us-east-1:<account>:trikon-cloud-installation-events`,
  (c) a new `_build_installation_event_message(...)` helper on the
  receiver that emits the `InstallationEventMessage` shape declared in
  design.md §3.2. See design.md §9.1.
- **Spec 2 (FargateRunnerStack) — SSM active-revision writer.** Amend
  `FargateRunnerStack` to write the newly-synthesized
  `trikon-verify-runner` task-definition revision integer to SSM
  Parameter Store at `/trikon/verify-runner/active-revision` on every
  deploy. Spec 3's orchestrator reads this parameter at cold start via
  `TaskDefinitionResolver.resolve()`; no read grant is required on
  Spec 2's side because Spec 3's stack (design.md §8.2 grant 5)
  handles the consumer-side `ssm:GetParameter` grant. See
  design.md §9.2.

## 5. Outputs

After a successful `cdk deploy`, the stack surfaces the following ARNs
as CloudFormation outputs (visible in the `cdk deploy` console tail
and via `aws cloudformation describe-stacks`):

- `InstallationEventsQueueArn` —
  `arn:aws:sqs:us-east-1:<account>:trikon-cloud-installation-events`
- `InstallationEventsDlqArn` —
  `arn:aws:sqs:us-east-1:<account>:trikon-cloud-installation-events-dlq`
- `InstallationsTableArn` —
  `arn:aws:dynamodb:us-east-1:<account>:table/trikon-cloud-installations`
- `OrchestratorFnArn` —
  `arn:aws:lambda:us-east-1:<account>:function:trikon-cloud-orchestrator`
- `LifecycleFnArn` —
  `arn:aws:lambda:us-east-1:<account>:function:trikon-cloud-installation-lifecycle`

The `trikon-verify-jobs` main queue and its DLQ are Spec 1 outputs and
are referenced here only by ARN — this stack does not re-emit them.

## 6. Post-deploy validation

- Trigger a GitHub `installation.created` webhook against a test
  installation. Within seconds an IAM role named
  `trikon-verify-task-role-<installation_id>` should exist in the
  console and the `trikon-cloud-installations` table should carry an
  `active` row keyed on that installation ID.
- Trigger a `pull_request.opened` webhook. The orchestrator Lambda
  should invoke `ecs:RunTask` exactly once against
  `trikon-verify-cluster` and the resulting task ARN should appear in
  a `run_task_dispatched` log record.
- Confirm no log record in either Lambda contains the raw SQS body,
  the `ecs.RunTask` response body, the App private key, the App JWT,
  or the installation access token (Invariant 6).

## 7. Rollback

```
uv run cdk destroy TrikonCloudOrchestratorStack \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

`cdk destroy` is largely safe because this stack does **not** own:
- The `trikon-verify-jobs` queue or its DLQ (owned by Spec 1).
- The Secrets Manager App-key secret (referenced by ARN, not created).
- The `/trikon/verify-runner/active-revision` SSM parameter (owned by
  Spec 2 after the §9.2 amendment).

Destroying the stack removes only the two Lambdas, their event-source
mappings, their IAM roles, the `trikon-cloud-installation-events` queue
and its DLQ, and the `trikon-cloud-installations` DynamoDB table.

> **Warning — installation lifecycle history.** The
> `trikon-cloud-installations` table records every provisioned
> installation and its active/disabled state. Destroying it loses
> that history and forces every installation to be re-provisioned on
> the next `installation.created` webhook. The stack declares
> `removal_policy=RemovalPolicy.RETAIN` on the table, so a plain
> `cdk destroy` should leave the table in place. Belt-and-suspenders:
> pass `--retain-except-if-created-with-force` to any teardown that
> intends to preserve the installations table across a stack rebuild,
> and verify in the CloudFormation console that the table's
> `DeletionPolicy` shows `Retain` before confirming the destroy.

## 8. Lint, type-check, and test invocations

Run these from the repo root before proposing a deploy:

```
uv run ruff check trikon_cloud/orchestrator trikon_cloud/installation_lifecycle
uv run mypy --strict trikon_cloud/orchestrator trikon_cloud/installation_lifecycle
uv run pytest trikon_cloud/orchestrator/tests trikon_cloud/installation_lifecycle/tests
```

The CDK-synth contract test in
`trikon_cloud/installation_lifecycle/tests/test_iam_policy_contract.py`
is part of the pytest run and enforces byte-match between this stack's
rendered per-installation IAM policy and Spec 2's assumed policy — a
drift in either direction fails the test and blocks the merge.

## 9. Deviations flagged for the release engineer

- The stack packages both Lambdas via `Code.from_asset(...)` on the
  repo root; the release engineer must vendor the `cloud`-extra deps
  into the asset before deploy (same pattern as Spec 1 — see that
  runbook's §2 note).
- Grant 6 in design.md §8.2 (the Never-Fail-Open `dynamodb:PutItem`
  on `trikon_verdicts`) uses a `ForAllValues:StringLike` LeadingKeys
  wildcard because the orchestrator role is a fleet role, not a
  per-installation role. Runtime-side scoping is enforced by the
  frozen `VerdictRow` model whose partition key is always the incoming
  `sqs_message.installation_id`. A stricter per-installation
  `sts:AssumeRole` alternative is deferred to M2.
- No custom domain, no WAF, no CMK-scoped KMS grants at M1 — all
  deferred to follow-up hardening specs.
