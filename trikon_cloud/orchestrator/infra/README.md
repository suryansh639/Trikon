# `OrchestratorStack` — deploy runbook

CDK Python stack for the Trikon Cloud orchestrator (Spec 3 of the M1
milestone). Provisions four Python 3.11 arm64 Lambda functions
(`trikon-cloud-orchestrator`, `trikon-cloud-installation-lifecycle`,
`trikon-cloud-usage-reporter` and `trikon-cloud-spot-retry`), the
`trikon-cloud-installation-events` queue and its
`trikon-cloud-installation-events-dlq` dead-letter queue, the
`trikon-cloud-usage-reporter-dlq` and `trikon-cloud-spot-retry-dlq`
dead-letter queues, two DynamoDB TableV2 tables
(`trikon-cloud-installations` and `trikon-cloud-spot-retries`), one
explicit log group per Lambda, and the `trikon-verify-runner-task-stopped`
EventBridge rule. The orchestrator Lambda bridges Spec 1's
`trikon-verify-jobs` queue to Spec 2's Fargate `trikon-verify-runner`
task definition; the lifecycle Lambda consumes GitHub `installation` and
`installation_repositories` events and provisions the per-installation
IAM task roles that Spec 2's task definition assumes. See
`.kiro/specs/trikon-cloud-orchestrator/design.md` §8 for the CDK
topology and §9 for the coordinated amendments to Spec 1 and Spec 2,
and `.kiro/specs/trikon-cloud-cost-cuts/design.md` for the Parameter
Store, log retention and Fargate Spot changes.

> **Callout.** The deploy commands below are **informational**. The
> release engineer runs them, not the implementation tasks in this spec.
> All spec tasks stop at `cdk synth` (offline, no AWS API calls). The
> manual gate to `cdk deploy` is a release-engineer action per
> Requirement 16.4.

## When to deploy

Deploy Trikon Cloud only once hosted PR checks are wanted. Until then
none of the three stacks is deployed and Trikon Cloud costs nothing. The
three CDK apps stay synth-able offline in the meantime (§2), and the test
suite synthesizes all three.

## 1. Prerequisites

- AWS account with credentials configured (env vars,
  `~/.aws/credentials`, or an SSO profile that the CDK CLI can pick up).
- Node.js and the `cdk` CLI installed (`npm install -g aws-cdk`).
- The Trikon repo checked out at HEAD.
- Repo-root dev sync: `uv sync --extra cloud --extra dev`. The `cloud`
  extra pulls the Lambdas' runtime deps (aws-lambda-powertools,
  pydantic, pydantic-settings, boto3, botocore, httpx, PyJWT[crypto]);
  the `dev` extra pulls `aws-cdk-lib` and `constructs` for the CDK synth
  step.
- **Spec 1 must have been deployed.** The orchestrator's Lambda event
  source references `arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs`
  and its DLQ send grant references
  `arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs-dlq`. Both are
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
- The App_Key_Parameter and the Usage_Secret_Parameter (§1.2) must exist
  in us-east-1. **This stack does NOT create them.** It references each
  one by name and grants `ssm:GetParameter` on exactly its parameter ARN
  to the roles that read it.
- The runner subnets and security group come from `FargateRunnerStack`.
  Its `RunnerSubnetIds` output (comma-separated public subnet ids) feeds
  `runner_subnet_ids`, and its `RunnerSecurityGroupId` output feeds
  `runner_security_group_ids`. Runner tasks run in public subnets with a
  public IP and no NAT gateway.

### 1.1 Deploy order

Deploy the three stacks in this order:

1. `FargateRunnerStack` (`trikon_cloud/fargate_runner/infra/`). It creates
   the VPC, the `trikon-verify-cluster` cluster with the `FARGATE` and
   `FARGATE_SPOT` capacity providers, the DynamoDB tables, the evidence
   bucket, the `/trikon/verify-runner/active-revision` parameter and the
   subnet and security-group outputs used below.
2. `WebhookReceiverStack` (`trikon_cloud/webhook_receiver/infra/`). It
   creates `trikon-verify-jobs` and `trikon-verify-jobs-dlq`.
3. `TrikonCloudOrchestratorStack` (this stack). It consumes
   `trikon-verify-jobs` and owns the `trikon-cloud-installation-events`
   queue the webhook receiver writes to.

Register the GitHub App webhook URL only after step 3.

### 1.2 Secure parameters

Create the three Secure_Parameters before the first deploy of any stack.
This stack reads the App key and the Usage_Secret; the webhook secret is
read by `WebhookReceiverStack`, but all three are listed here so one run
of these commands covers every stack:

| Parameter | Default name | Read by (this stack) | Name override (CDK context) |
|---|---|---|---|
| App_Key_Parameter (GitHub App private key, PEM) | `/trikon-cloud/github-app-private-key` | `trikon-cloud-orchestrator`, `trikon-cloud-spot-retry` | `app_private_key_parameter_name` |
| Webhook_Secret_Parameter (GitHub webhook secret) | `/trikon-cloud/github-webhook-secret` | none (webhook receiver only) | `webhook_secret_parameter_name` (webhook receiver app) |
| Usage_Secret_Parameter (Usage_Secret shared with Trikon_Web) | `/trikon-cloud/usage-secret` | `trikon-cloud-orchestrator`, `trikon-cloud-usage-reporter` | `usage_secret_parameter_name` |

Replace each `<...>` placeholder with the real value. Never paste a real
value into this file, a ticket or CI configuration.

```
aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-app-private-key \
  --type SecureString --tier Standard --value file://<path-to-app-private-key.pem>
aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-webhook-secret \
  --type SecureString --tier Standard --value '<webhook-secret>'
aws ssm put-parameter --region us-east-1 --name /trikon-cloud/usage-secret \
  --type SecureString --tier Standard --value '<usage-secret-same-as-trikon-web>'
```

- **Standard tier, `aws/ssm` key.** The commands omit `--key-id`, so SSM
  encrypts each value with the AWS-managed `aws/ssm` KMS key. The readers
  need only `ssm:GetParameter`; the stack adds no `kms:Decrypt` grant.
- **4 KB limit.** A standard-tier value is limited to 4 KB. A GitHub App
  PEM is about 1.7 KB.
- **`file://` for the PEM.** It reads the key from the file, which keeps
  it out of shell history and preserves its line breaks.
- **Usage_Secret.** The Usage_Secret_Parameter value must be exactly the
  same string as the Usage_Secret configured in Trikon_Web
  (`TRIKON_CLOUD_USAGE_SECRET`). The orchestrator and the Usage_Reporter
  sign their requests with it, and Trikon_Web rejects requests signed with
  any other value.
- **Custom names.** To use a different name, create the parameter under
  that name and pass `-c <context-key>=/custom/name` with the context key
  from the table above, for example
  `-c app_private_key_parameter_name=/custom/github-app-private-key`. A
  name must start with `/`. An empty name, or one that does not start
  with `/`, fails synthesis with an error that names the context key.
  Pass the same App key name to `FargateRunnerStack` and this stack.

### 1.3 CDK context values

Every deploy of `TrikonCloudOrchestratorStack` requires seven context
values, passed via `-c key=value` on the `cdk` command line. A missing
or empty value fails synthesis:

| Context key                  | Purpose                                                                                   |
|------------------------------|-------------------------------------------------------------------------------------------|
| `app_id`                     | The GitHub App's numeric ID (used to mint App JWTs).                                      |
| `verify_jobs_queue_arn`      | ARN of `trikon-verify-jobs` (created by `WebhookReceiverStack`).                          |
| `verify_jobs_dlq_arn`        | ARN of `trikon-verify-jobs-dlq` (created by `WebhookReceiverStack`).                      |
| `runner_subnet_ids`          | Comma-separated subnet IDs from the `RunnerSubnetIds` output, e.g. `subnet-a,subnet-b`.  |
| `runner_security_group_ids`  | Comma-separated security group IDs from the `RunnerSecurityGroupId` output.               |
| `trikon_web_api_url`         | Base URL of the Trikon_Web API (Quota and Usage endpoints).                               |
| `trikon_web_app_url`         | Base URL of the Trikon_Web app (Connect and Upgrade links in fail-closed Check Runs).     |

Optional context values:

| Context key                               | Default                                  | Purpose |
|-------------------------------------------|------------------------------------------|---------|
| `app_private_key_parameter_name`          | `/trikon-cloud/github-app-private-key`   | Name of the App_Key_Parameter. |
| `usage_secret_parameter_name`             | `/trikon-cloud/usage-secret`             | Name of the Usage_Secret_Parameter. |
| `use_fargate_spot`                        | `true`                                   | Spot_Flag (§3.1). `true` or `false`, in any case. Any other value fails synthesis. |
| `verify_runner_active_revision_ssm_param` | `/trikon/verify-runner/active-revision`  | SSM parameter holding the active runner task-definition revision. |

None of these values are secrets; they are IDs, ARNs, URLs and parameter
names safe to check in to CI configuration. The secret values live only
in the Secure_Parameters and never appear in this runbook.

The obsolete context keys `app_private_key_secret_arn` and
`usage_secret_arn` (from the Secrets Manager design) now fail synthesis
with a message naming the replacement `*_parameter_name` key, so a stale
deploy command cannot silently fall back to the default names.

## 2. Synth

Offline — makes no AWS API calls, exits 0 if the emitted CloudFormation
template is valid. The values below are dummies for synthesis only:

```
uv run cdk synth \
  -c app_id=999999 \
  -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:000000000000:trikon-verify-jobs \
  -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:000000000000:trikon-verify-jobs-dlq \
  -c runner_subnet_ids=subnet-a,subnet-b \
  -c runner_security_group_ids=sg-verify \
  -c trikon_web_api_url=https://api.example.invalid \
  -c trikon_web_app_url=https://app.example.invalid \
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
in this spec. Deploy only after §1.1 steps 1 and 2 and after the
Secure_Parameters in §1.2 exist. Substitute the real values for the
placeholders:

```
uv run cdk deploy TrikonCloudOrchestratorStack \
  -c app_id=<github-app-id> \
  -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs \
  -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs-dlq \
  -c runner_subnet_ids=<RunnerSubnetIds-output> \
  -c runner_security_group_ids=<RunnerSecurityGroupId-output> \
  -c trikon_web_api_url=<trikon-web-api-url> \
  -c trikon_web_app_url=<trikon-web-app-url> \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

Add `-c app_private_key_parameter_name=<name>` or
`-c usage_secret_parameter_name=<name>` only if you created the
parameters under non-default names. Add `-c use_fargate_spot=false` to
run every task on on-demand capacity (§3.1).

### 3.1 Fargate Spot and the Spot_Retry_Handler

By default the first attempt of each verification runs on Fargate Spot
(`FARGATE_SPOT`), which is cheaper than on-demand `FARGATE`:

- The orchestrator sends `ecs:RunTask` with a capacity provider strategy
  of `FARGATE_SPOT` for Attempt 1 and `FARGATE` for Attempt 2, and never
  sends `launchType`. It passes the attempt and capacity to the runner as
  `TRIKON_ATTEMPT` and `TRIKON_CAPACITY`.
- When Spot capacity is reclaimed, the runner posts an in-progress
  `Trikon` Check Run ("interrupted, retrying") with no conclusion, so a
  required check stays pending, and exits with code 75.
- The `trikon-verify-runner-task-stopped` rule has two targets: the
  Usage_Reporter and the Spot_Retry_Handler (`trikon-cloud-spot-retry`).
  The Spot_Retry_Handler re-queues a Spot-interrupted Attempt 1 exactly
  once to `trikon-verify-jobs` as Attempt 2, with the same Run_Id and
  Reservation_Id. The orchestrator runs that retry on `FARGATE` without a
  second quota check.
- Every other outcome ends in a require-human `Trikon` Check Run with
  conclusion `action_required`, never `success`: an interruption of
  Attempt 2, a failed re-queue, incomplete task overrides, or a runner
  that exited 75 without a Spot interruption.
- Billing: the Usage_Reporter sends no Usage_Report for a Spot-interrupted
  task. Only the retry is billed.

**Turning Spot off.** Deploy with `-c use_fargate_spot=false`. The
orchestrator then gets `TRIKON_USE_FARGATE_SPOT=false` and runs Attempt 1
on `FARGATE`. Retries always run on `FARGATE`. Redeploy without the flag
(or with `-c use_fargate_spot=true`) to turn Spot back on.

**Spot_Retry_Handler resources.**

- `trikon-cloud-spot-retry`: 256 MB, 90-second timeout, reserved
  concurrency 5, role `trikon-cloud-spot-retry-role`. It reads the
  App_Key_Parameter to publish fail-closed Check Runs.
- `trikon-cloud-spot-retry-dlq` (14-day retention): receives events the
  handler could not process, both after 2 async-invoke retries (6-hour
  maximum event age) and after 185 EventBridge delivery retries (24-hour
  maximum event age). A message here means the handler gave up on that
  stopped task. The pull request's `Trikon` check then stays pending or
  shows a require-human result; it never passes. Investigate each message.
- `trikon-cloud-spot-retries`: DynamoDB table (PAY_PER_REQUEST, partition
  key `task_arn`, TTL attribute `expires_at`, 7 days after the claim). The
  handler records a conditional claim per interrupted task here so a
  re-delivered event never sends a second retry. The data is short-lived,
  so the table has no point-in-time recovery and `RemovalPolicy.DESTROY`.

### 3.2 Log retention

Each Lambda in this stack logs to an explicit log group with 7-day
retention and `RemovalPolicy.RETAIN`:

- `/aws/lambda/trikon-cloud-orchestrator`
- `/aws/lambda/trikon-cloud-installation-lifecycle`
- `/aws/lambda/trikon-cloud-usage-reporter`
- `/aws/lambda/trikon-cloud-spot-retry`

The groups outlive `cdk destroy` (§7). Delete a retained group before
re-creating the stack, because the deploy fails if a log group with the
same name already exists.

### 3.3 Reservation_Timeout in Trikon_Web

Keep Trikon_Web's Reservation_Timeout
(`TRIKON_CLOUD_RESERVATION_TIMEOUT_MINUTES`, default 30) at 30 minutes or
more. A Spot retry runs a second task under the same Reservation. The
worst case is the 10-minute run cap, up to 2 minutes of Spot warning and
stop timeout, under a minute of EventBridge and SQS latency, and the
10-minute retry cap: about 23 minutes in total. Trikon_Web accepts any
value above 10, but with a value from 11 to 22 the Reservation can stop
holding minutes and release its concurrency slot before the retry
finishes. The retry's Usage_Report still settles.

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
  `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events`,
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

The stack defines no CloudFormation outputs. Every resource has a fixed
name, so its ARN follows from the account ID:

- `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events`
- `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events-dlq`
- `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-usage-reporter-dlq`
- `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-spot-retry-dlq`
- `arn:aws:dynamodb:us-east-1:<account-id>:table/trikon-cloud-installations`
- `arn:aws:dynamodb:us-east-1:<account-id>:table/trikon-cloud-spot-retries`
- `arn:aws:lambda:us-east-1:<account-id>:function:trikon-cloud-orchestrator`
- `arn:aws:lambda:us-east-1:<account-id>:function:trikon-cloud-installation-lifecycle`
- `arn:aws:lambda:us-east-1:<account-id>:function:trikon-cloud-usage-reporter`
- `arn:aws:lambda:us-east-1:<account-id>:function:trikon-cloud-spot-retry`

The `trikon-verify-jobs` main queue and its DLQ are Spec 1 resources and
are referenced here only by ARN.

## 6. Post-deploy validation

- Trigger a GitHub `installation.created` webhook against a test
  installation. Within seconds an IAM role named
  `trikon-verify-task-role-<installation_id>` should exist in the
  console and the `trikon-cloud-installations` table should carry an
  `active` row keyed on that installation ID.
- Trigger a `pull_request.opened` webhook. The orchestrator Lambda
  should invoke `ecs:RunTask` exactly once against
  `trikon-verify-cluster` and the resulting task ARN should appear in
  a `run_task_dispatched` log record, with `attempt` 1 and `capacity`
  `FARGATE_SPOT` (or `FARGATE` when deployed with
  `-c use_fargate_spot=false`).
- Confirm no log record in any Lambda contains the raw SQS body,
  the `ecs.RunTask` response body, the App private key, the
  Usage_Secret, the App JWT, or the installation access token
  (Invariant 6).

## 7. Rollback

```
uv run cdk destroy TrikonCloudOrchestratorStack \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

`cdk destroy` is largely safe because this stack does **not** own:
- The `trikon-verify-jobs` queue or its DLQ (owned by Spec 1).
- The App_Key_Parameter and the Usage_Secret_Parameter (referenced by
  name, not created).
- The `/trikon/verify-runner/active-revision` SSM parameter (owned by
  Spec 2 after the §9.2 amendment).

Destroying the stack removes the four Lambdas, their event-source
mappings, their IAM roles, the task-stopped rule, the
`trikon-cloud-installation-events` queue and its DLQ, the
`trikon-cloud-usage-reporter-dlq` and `trikon-cloud-spot-retry-dlq`
queues, and the `trikon-cloud-spot-retries` table. The four
`/aws/lambda/...` log groups (§3.2) and the `trikon-cloud-installations`
table are retained. Before re-creating the stack, delete the retained
log groups; a deploy fails on any resource name that already exists.

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

- The stack packages the Lambdas via `Code.from_asset(...)` on the
  repo root; the release engineer must vendor the `cloud`-extra deps
  into the asset before deploy (same pattern as Spec 1 — see that
  runbook's §2 note).
- Grant 6 in design.md §8.2 (the Never-Fail-Open `dynamodb:PutItem`
  on `trikon_verdicts`) uses a `ForAllValues:StringLike` LeadingKeys
  wildcard because the orchestrator role is a fleet role, not a
  per-installation role. The Spot_Retry_Handler role's
  `SpotRetryVerdictWrite` statement uses the same condition. Runtime-side
  scoping is enforced by the frozen `VerdictRow` model whose partition
  key is always the incoming installation ID. A stricter
  per-installation `sts:AssumeRole` alternative is deferred to M2.
- No custom domain, no WAF, no CMK-scoped KMS grants at M1 — all
  deferred to follow-up hardening specs.
