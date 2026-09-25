# `FargateRunnerStack` — deploy runbook

CDK Python stack for the Trikon Cloud Fargate verify-runner (Spec 2 of the
M1 milestone). Provisions the ECS cluster, task definition, three DynamoDB
tables (`trikon_installations`, `trikon_verdicts`, `trikon_pr_state`) with
their GSIs, the S3 evidence bucket, the CloudWatch log group, and the
per-installation IAM task-role template.

> **Callout.** The deploy commands below are **informational**. The release
> engineer runs them, not the implementation tasks in this spec. All spec
> tasks stop at `cdk synth` (offline, no AWS API calls) and `docker build`
> (local, no push). The manual gates to `cdk deploy` and `docker push` are
> release-engineer actions per Requirements 19.3 and 19.4.

## 1. Prerequisites

- AWS account with credentials configured (env vars, `~/.aws/credentials`,
  or an SSO profile that the CDK CLI can pick up).
- Node.js + `cdk` CLI installed (`npm install -g aws-cdk`).
- Docker (for the runner image build in §3).
- The Trikon repo checked out at HEAD.
- Repo-root dev sync: `uv sync --extra cloud --extra dev`. The `cloud` extra
  pulls the runner's runtime deps (httpx, PyJWT[crypto], structlog, boto3,
  etc.); the `dev` extra pulls `aws-cdk-lib` and `constructs` for the CDK
  synth step.
- The GitHub App marketplace listing must be complete before the first
  deploy (the marketplace listing owns the App private key).
- The Secrets Manager secret `trikon-cloud/github-app-private-key` must
  exist in the target region (us-east-1) with the App's PEM-encoded RSA
  private key as its `SecretString`. **This stack does NOT create the
  secret** — it references it by ARN and only grants `GetSecretValue` to
  the per-installation task roles (Requirement 19.5).
- The runner Docker image must be published to ECR Public at
  `public.ecr.aws/trikon/trikon-cloud-runner:0.3.6-runner-mvp` before
  the first `cdk deploy` — see §3.

## 2. Synth

Offline — makes no AWS API calls, exits 0 if the emitted CloudFormation
template is valid:

```
uv run cdk synth \
  -c app_private_key_secret_arn=arn:aws:secretsmanager:us-east-1:<account>:secret:trikon-cloud/github-app-private-key-<suffix> \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

The emitted template lands at `cdk.out/FargateRunnerStack.template.json`.

## 3. Build + push the runner image (manual gate)

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

## 4. Deploy the stack (manual gate)

```
uv run cdk deploy FargateRunnerStack \
  -c app_private_key_secret_arn=<arn> \
  -c app_id=<id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

The deploy creates the shared ECS cluster (`trikon-verify-cluster`), the
task definition family (`trikon-verify-runner`), the three DynamoDB tables
with `RemovalPolicy.RETAIN`, the S3 bucket, and the log group. Spec 3's
orchestrator (when it lands) will invoke `ecs.RunTask` on this cluster +
task definition per PR event.

## 5. Post-deploy validation

- The CDK deploy output lists the cluster ARN, task-definition ARN, and
  the verdicts table name. Confirm all three exist in the AWS console.
- Once Spec 3's orchestrator is deployed, register the App's webhook
  endpoint on GitHub and trigger a canary PR event. The runner container
  should launch on Fargate, invoke `sdk.verify`, and write a verdict row
  to `trikon_verdicts` visible in the DynamoDB console within ~60s of the
  PR event.

## 6. Rollback

```
uv run cdk destroy FargateRunnerStack \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

`cdk destroy` is safe because:
- The stack does NOT own the Secrets Manager secret (referenced by ARN,
  not created).
- The three DynamoDB tables have `RemovalPolicy.RETAIN` — data survives.
- The S3 evidence bucket has `RemovalPolicy.RETAIN` — spilled evidence
  survives.
- The CloudWatch log group has `RemovalPolicy.RETAIN` — logs survive.

Destroying the stack removes only the ECS cluster, task definition, IAM
roles, VPC, and NAT gateway.

## 7. Deviations flagged for the release engineer

- The stack provisions a minimal 2-AZ VPC with a single NAT gateway (memo
  §3.9 — egress-only). Production hardening (multi-AZ NAT, private
  endpoints for DynamoDB / S3 / Secrets Manager) is out of M1 scope and
  is deferred to `trikon-cloud-fargate-runner-vpc-hardening`.
- The `build_task_role_for_installation` helper on `FargateRunnerStack`
  is called by Spec 3 (orchestrator) per-installation. Spec 3 must invoke
  it during the `installation.created` webhook handler to bind the
  role's `installation_id` before the first RunTask.
- No WAF rule allow-listing GitHub webhook IPs at MVP — see the
  `trikon-cloud-webhook-receiver-waf-hardening` follow-up spec.

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
