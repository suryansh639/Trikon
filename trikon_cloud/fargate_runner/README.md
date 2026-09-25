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

Deploy the CDK stack that provisions the ECS cluster, task definition,
DynamoDB tables, S3 evidence bucket, and per-installation IAM template:

```bash
cdk deploy FargateRunnerStack
```

Manual release-engineer gate. The CDK app requires AWS credentials with the
appropriate permissions and is not invoked from CI.
