# `FargateRunnerStack` — deploy runbook

CDK Python stack for the Trikon Cloud Fargate verify-runner (Spec 2 of the
M1 milestone). Provisions the ECS cluster (with the `FARGATE` and
`FARGATE_SPOT` capacity providers), the task definition, three DynamoDB
tables (`trikon_installations`, `trikon_verdicts`, `trikon_pr_state`) with
their GSIs, the S3 evidence bucket, the CloudWatch log group (7-day
retention), a public-subnet-only VPC with no NAT gateway, and the
per-installation IAM task-role template.

> **Callout.** The deploy commands below are **informational**. The release
> engineer runs them, not the implementation tasks in this spec. All spec
> tasks stop at `cdk synth` (offline, no AWS API calls) and `docker build`
> (local, no push). The manual gates to `cdk deploy` and `docker push` are
> release-engineer actions per Requirements 19.3 and 19.4.

## 1. When to deploy

Deploy Trikon Cloud only once hosted PR checks are wanted. Until then none
of the three stacks is deployed, and Trikon Cloud costs nothing on AWS.
The three CDK apps still synthesize offline (§4), and the test suite
synthesizes each of them, so they stay ready to deploy.

When hosted PR checks are wanted:

1. Create the three SSM parameters in §3.
2. Build and push the runner image (§5).
3. Deploy the three stacks in the order in §10: `FargateRunnerStack`,
   then `WebhookReceiverStack`, then `TrikonCloudOrchestratorStack`.
4. Register the GitHub App webhook URL only after step 3.

## 2. Prerequisites

- AWS account with credentials configured (env vars, `~/.aws/credentials`,
  or an SSO profile that the CDK CLI can pick up).
- Node.js + `cdk` CLI installed (`npm install -g aws-cdk`).
- Docker (for the runner image build in §5).
- The Trikon repo checked out at HEAD.
- Repo-root dev sync: `uv sync --extra cloud --extra dev`. The `cloud` extra
  pulls the runner's runtime deps (httpx, PyJWT[crypto], structlog, boto3,
  etc.); the `dev` extra pulls `aws-cdk-lib` and `constructs` for the CDK
  synth step.
- The GitHub App marketplace listing must be complete before the first
  deploy (the marketplace listing owns the App private key).
- The three SSM SecureString parameters in §3 must exist in the target
  region (us-east-1). **This stack does NOT create them.** It uses only the
  App private key parameter, by name: it passes the name to the runner
  container as `TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME`, and the
  per-installation task roles get `ssm:GetParameter` on exactly that
  parameter's ARN (Requirement 19.5).
- The runner Docker image must be published to ECR Public at
  `public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp` before
  the first `cdk deploy` — see §5.

## 3. Secure parameters (create before the first deploy)

Trikon Cloud keeps its three secrets in SSM Parameter Store as
`SecureString` parameters. Create all three once, in us-east-1, before the
first deploy of any stack:

| Default name | Holds | Read by |
|---|---|---|
| `/trikon-cloud/github-app-private-key` | GitHub App private key (PEM) | runner tasks (this stack), orchestrator, Spot retry handler |
| `/trikon-cloud/github-webhook-secret` | GitHub App webhook secret | webhook receiver |
| `/trikon-cloud/usage-secret` | Usage secret shared with Trikon Web | orchestrator, usage reporter |

Placeholder commands (replace each `<...>` value; never commit or paste
real values into docs):

```
aws ssm put-parameter --region us-east-1 \
  --name /trikon-cloud/github-app-private-key \
  --type SecureString --tier Standard \
  --value file://<path-to-app-private-key.pem>

aws ssm put-parameter --region us-east-1 \
  --name /trikon-cloud/github-webhook-secret \
  --type SecureString --tier Standard \
  --value '<webhook-secret>'

aws ssm put-parameter --region us-east-1 \
  --name /trikon-cloud/usage-secret \
  --type SecureString --tier Standard \
  --value '<same-value-as-TRIKON_CLOUD_USAGE_SECRET-in-trikon-web>'
```

- **Standard tier.** Standard parameters have no monthly charge. A
  standard-tier value is limited to 4 KB; a GitHub App PEM is about 1.7 KB.
- **`aws/ssm` key.** The commands omit `--key-id`, so SSM encrypts with the
  AWS-managed `aws/ssm` key. The stacks grant no `kms:Decrypt`; a
  customer-managed key would need one, so keep the default.
- **`file://` for the PEM.** The CLI reads the key from the file, so the
  PEM never appears on the command line or in shell history. Delete the
  local copy afterwards if you don't need it.
- **Usage secret.** The value must equal `TRIKON_CLOUD_USAGE_SECRET` in
  Trikon Web, or Trikon Web rejects every usage report.
- **The webhook secret** must equal the secret configured on the GitHub
  App's webhook.
- To rotate a value, rerun the command with `--overwrite`. The readers
  read each parameter at most once per warm Lambda or task, so warm
  Lambdas and running tasks may keep the old value until they are
  replaced.

**Custom names.** Each stack reads the parameter names from CDK context.
Leave a key unset to use the default name:

| Context key | Default | Used by |
|---|---|---|
| `app_private_key_parameter_name` | `/trikon-cloud/github-app-private-key` | `FargateRunnerStack`, `TrikonCloudOrchestratorStack` |
| `webhook_secret_parameter_name` | `/trikon-cloud/github-webhook-secret` | `WebhookReceiverStack` |
| `usage_secret_parameter_name` | `/trikon-cloud/usage-secret` | `TrikonCloudOrchestratorStack` |

Pass the override to every stack that uses the key, for example
`-c app_private_key_parameter_name=/custom/app-key` on both the runner and
the orchestrator deploys. A name that is empty or does not start with `/`
fails synthesis with an error that names the context key. The obsolete
`app_private_key_secret_arn` key (and the orchestrator's and webhook
receiver's old `*_secret_arn` keys) also fail synthesis, so an old deploy
command cannot silently fall back to the default names.

## 4. Synth

Offline — makes no AWS API calls, exits 0 if the emitted CloudFormation
template is valid:

```
uv run cdk synth \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

`app_id` is required; synthesis fails without it. Add
`-c app_private_key_parameter_name=<name>` only when you use a custom name
(§3). The emitted template lands at
`cdk.out/FargateRunnerStack.template.json`.

## 5. Build + push the runner image (manual gate)

Build the container locally from the repo root:

```
docker build -f trikon_cloud/fargate_runner/Dockerfile -t trikon-cloud-runner:local .
```

Push to ECR Public (release-engineer action; requires `docker login` to
`public.ecr.aws`):

```
aws ecr-public get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin public.ecr.aws

docker tag trikon-cloud-runner:local \
  public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp

docker push public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp
```

## 6. Deploy the stack (manual gate)

```
uv run cdk deploy FargateRunnerStack \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

The deploy creates the shared ECS cluster (`trikon-verify-cluster`), the
task definition family (`trikon-verify-runner`), the three DynamoDB tables
with `RemovalPolicy.RETAIN`, the S3 bucket, the log group, the
`trikon-verify-vpc` VPC and the `trikon-verify-runner-sg` security group.
Spec 3's orchestrator invokes `ecs.RunTask` on this cluster + task
definition per PR event.

What the stack sets for cost:

- **Spot capacity.** The cluster has both the `FARGATE` and `FARGATE_SPOT`
  capacity providers. Its default strategy does not include
  `FARGATE_SPOT`; the orchestrator picks the capacity on each `RunTask`.
  With the orchestrator's `use_fargate_spot` context at its default
  (`true`), the first attempt runs on `FARGATE_SPOT`. If Spot reclaims the
  task, the job is retried once on `FARGATE`, and only the retry is billed
  to the customer. `-c use_fargate_spot=false` on the orchestrator deploy
  runs every attempt on `FARGATE`.
- **120-second stop timeout.** The runner container's `stopTimeout` is 120
  seconds, the Fargate maximum and the length of the Spot warning. A
  runner that gets SIGTERM has that long to post its "interrupted,
  retrying" Check Run (or a fail-closed result) before SIGKILL.
- **7-day log retention.** `/aws/ecs/trikon-verify-runner` keeps logs for
  7 days.
- **No NAT gateway.** Runner tasks launch in the public subnets with a
  public IP and reach the internet through the internet gateway.

## 7. Post-deploy validation

- The CDK deploy output lists `RunnerSubnetIds` (comma-separated) and
  `RunnerSecurityGroupId`; the orchestrator deploy needs both (§10).
  Confirm the `trikon-verify-cluster` cluster, the `trikon-verify-runner`
  task definition and the three tables exist in the AWS console.
- Once Spec 3's orchestrator is deployed, register the App's webhook
  endpoint on GitHub and trigger a canary PR event. The runner container
  should launch on Fargate, invoke `sdk.verify`, and write a verdict row
  to `trikon_verdicts` visible in the DynamoDB console within ~60s of the
  PR event.

## 8. Rollback

```
uv run cdk destroy FargateRunnerStack \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

`cdk destroy` is safe because:
- The stack does NOT own the App private key parameter (referenced by
  name, not created). The three SSM parameters in §3 survive.
- The three DynamoDB tables have `RemovalPolicy.RETAIN` — data survives.
- The S3 evidence bucket has `RemovalPolicy.RETAIN` — spilled evidence
  survives.
- The CloudWatch log group has `RemovalPolicy.RETAIN` — logs survive
  until their 7-day retention expires.

Destroying the stack removes only the ECS cluster, task definition, IAM
roles, active-revision SSM parameter, VPC, subnets and security group.

The retained resources keep their fixed names, so a later deploy of the
stack fails on a name collision. Before re-creating the stack after a
`cdk destroy`, delete the `/aws/ecs/trikon-verify-runner` log group, and
delete (or back up and delete) the three tables and the
`trikon-cloud-evidence` bucket.

## 9. Deviations flagged for the release engineer

- The stack provisions a 2-AZ VPC with public subnets only and zero NAT
  gateways. Runner tasks get a public IP and are protected by an
  egress-only security group. Production hardening (private subnets with
  VPC endpoints for DynamoDB / S3 / SSM) is out of M1 scope and is
  deferred to `trikon-cloud-fargate-runner-vpc-hardening`.
- The `build_task_role_for_installation` helper on `FargateRunnerStack`
  is called by Spec 3 (orchestrator) per-installation. Spec 3 must invoke
  it during the `installation.created` webhook handler to bind the
  role's `installation_id` before the first RunTask.
- No WAF rule allow-listing GitHub webhook IPs at MVP — see the
  `trikon-cloud-webhook-receiver-waf-hardening` follow-up spec.

## 10. Deploy Ordering

For a from-scratch deploy, create the parameters in §3 first, then deploy
the three stacks in this order. Each step takes values from the one before:

1. **`FargateRunnerStack`** (this stack) creates the tables, the cluster,
   the VPC and the active-revision parameter, and outputs
   `RunnerSubnetIds` and `RunnerSecurityGroupId`.
2. **`WebhookReceiverStack`** creates the `trikon-verify-jobs` queue and
   its DLQ `trikon-verify-jobs-dlq`. It needs the ARN of the orchestrator's
   `trikon-cloud-installation-events` queue, which does not exist yet.
   The queue name is fixed, so pass its ARN in advance; nothing is sent
   to it until the webhook URL is registered after step 3.
3. **`TrikonCloudOrchestratorStack`** consumes `trikon-verify-jobs`,
   dispatches runner tasks into the subnets and security group from
   step 1, and creates the installation-events queue the receiver writes
   to.

```
uv run cdk deploy FargateRunnerStack \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"

uv run cdk deploy WebhookReceiverStack \
  -c installation_events_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"

uv run cdk deploy TrikonCloudOrchestratorStack \
  -c app_id=<github-app-numeric-id> \
  -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs \
  -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs-dlq \
  -c runner_subnet_ids=<RunnerSubnetIds-output> \
  -c runner_security_group_ids=<RunnerSecurityGroupId-output> \
  -c trikon_web_api_url=<trikon-web-api-url> \
  -c trikon_web_app_url=<trikon-web-app-url> \
  --app "python -m trikon_cloud.orchestrator.infra.app"
```

Add the §3 name overrides to each command if you use custom names. See the
webhook receiver and orchestrator runbooks for their other options.

`FargateRunnerStack` MUST be deployed before `TrikonCloudOrchestratorStack`.
On the first cold start after deploy, the orchestrator Lambda calls
`ssm:GetParameter` on `/trikon/verify-runner/active-revision` to resolve
the task-definition revision it will pass to `ecs.RunTask`. (This is a
plain `String` parameter that this stack creates, not one of the §3
secrets.) If the parameter is missing, every incoming SQS message hits the
resolver's `ParameterNotFound` branch, the handler's Terminal path fires,
and every job routes to the DLQ.

Subsequent redeploys of either stack can run in any order because CDK
regenerates the SSM parameter on every `FargateRunnerStack` deploy and the
orchestrator picks up the new value on its next cold start.
