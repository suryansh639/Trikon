# Trikon Cloud — Fargate Runner

Spec 2 of the Trikon Cloud M1 milestone. This subpackage ships the ECS Fargate
verify-runner container: a thin entrypoint that reads the memo §5.4 env-var
contract, shallow-fetches the target repo, invokes `trikon.sdk.verify(...)`,
persists the verdict to DynamoDB, and posts a byte-identical Markdown summary
to both a Check Run and a PR comment. See
[`.kiro/specs/trikon-cloud-architecture/`](../../.kiro/specs/trikon-cloud-architecture/)
for the overall architecture memo and
[`.kiro/specs/trikon-cloud-fargate-runner/`](../../.kiro/specs/trikon-cloud-fargate-runner/)
for this spec's requirements, design, and tasks.

## Local development

Bootstrap the `cloud` extra (installs runner + shared cloud deps):

```bash
uv sync --extra cloud
```

Run the runner's unit tests:

```bash
uv run pytest trikon_cloud/fargate_runner/tests/ -v
```

Type-check the runner package under strict mypy:

```bash
uv run mypy --strict trikon_cloud/fargate_runner/
```

Lint the runner package:

```bash
uv run ruff check trikon_cloud/fargate_runner/
```

## Docker build

Build the runner container image locally from the repo root:

```bash
docker build -f trikon_cloud/fargate_runner/Dockerfile -t trikon-cloud-runner:local .
```

Manual release-engineer gate. Publishing the image to ECR Public is out of
scope for the implementation tasks in this spec.

## Deploy

### When to deploy

Deploy Trikon Cloud only once hosted PR checks are wanted. Until then
nothing is deployed and nothing is billed on AWS. The CDK app still
synthesizes offline, so the stack stays ready to deploy.

### Before the first deploy

Create the three SSM Parameter Store `SecureString` parameters in us-east-1
(standard tier, AWS-managed `aws/ssm` key, values up to 4 KB). The stacks
reference them by name and never create them:

```bash
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

Leave out `--key-id` so SSM uses the `aws/ssm` key. The usage secret must
equal `TRIKON_CLOUD_USAGE_SECRET` in Trikon Web. The runner reads only the
App private key, through the parameter named in
`TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME`. To use a different name, pass
`-c app_private_key_parameter_name=/custom/name` to the runner and
orchestrator deploys (`webhook_secret_parameter_name` and
`usage_secret_parameter_name` work the same way for the other two).

### Deploy order

Deploy the three stacks in this order: `FargateRunnerStack`,
`WebhookReceiverStack`, `TrikonCloudOrchestratorStack`. Register the GitHub
App webhook URL after the orchestrator is up. Deploy the runner stack,
which provisions the ECS cluster (`FARGATE` and `FARGATE_SPOT` capacity
providers), task definition (120-second stop timeout), DynamoDB tables, S3
evidence bucket, log group (7-day retention), VPC with no NAT gateway, and
per-installation IAM template:

```bash
uv run cdk deploy FargateRunnerStack \
  -c app_id=<github-app-numeric-id> \
  --app "python -m trikon_cloud.fargate_runner.infra.app"
```

Manual release-engineer gate. The CDK app requires AWS credentials with the
appropriate permissions and is not invoked from CI. See
[`infra/README.md`](infra/README.md) for the full runbook: the other two
deploy commands, name overrides, Spot capacity and rollback.
