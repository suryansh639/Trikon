# `WebhookReceiverStack` — deploy runbook

CDK Python stack for the Trikon Cloud webhook receiver (Spec 1 of the M1
milestone). Provisions a single Lambda function fronted by an API Gateway
HTTP API, with the `trikon-verify-jobs` SQS queue + DLQ, a dedicated
CloudWatch log group, and a minimum-viable IAM role.

> **Callout.** The deploy commands below are **informational**. The release
> engineer runs them, not the implementation tasks in this spec. All spec
> tasks stop at `cdk synth` (offline, no AWS API calls) — the manual gate
> to `cdk deploy` is a release-engineer action per Requirement 17.3.

## 1. Prerequisites

- AWS account with credentials configured (env vars, `~/.aws/credentials`,
  or an SSO profile that the CDK CLI can pick up).
- Node.js and the `cdk` CLI installed (`npm install -g aws-cdk`).
- The Trikon repo checked out at HEAD.
- Repo-root dev sync: `uv sync --extra cloud --extra dev`. The `cloud`
  extra pulls the receiver's runtime deps (aws-lambda-powertools, pydantic,
  pydantic-settings, boto3, botocore); the `dev` extra pulls `aws-cdk-lib`
  and `constructs` for the CDK synth step.
- The GitHub App marketplace listing must be complete before the first
  deploy (the marketplace listing owns the webhook-secret bytes).
- The Secrets Manager secret `trikon-cloud/github-app-webhook-secret`
  must exist in the target region (us-east-1) with the App's webhook
  secret bytes as its `SecretString`. **This stack does NOT create the
  secret** — it references it by ARN and only grants `GetSecretValue`
  to the Lambda role. Populate the secret out-of-band before deploy.

## 2. Synth

Offline — makes no AWS API calls, exits 0 if the emitted CloudFormation
template is valid:

```
uv run cdk synth \
  -c webhook_secret_arn=arn:aws:secretsmanager:us-east-1:<account>:secret:trikon-cloud/github-app-webhook-secret-<suffix> \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

The emitted template lands under `cdk.out/WebhookReceiverStack.template.json`.

> **Note on the Lambda code asset.** The stack packages the
> `trikon_cloud/webhook_receiver/` directory via `Code.from_asset(...)`
> **without Docker bundling**. Before running `cdk deploy`, the release
> engineer must vendor the receiver's runtime deps into the asset
> directory — either (a) `pip install --target trikon_cloud/webhook_receiver
> -r <requirements-cloud.txt>` before deploy, (b) rewrite the stack to
> use `lambda_python_alpha.PythonFunction` when the experimental
> module is available in the venv, or (c) provide a Docker daemon and
> switch to `Code.from_asset(..., bundling=BundlingOptions(...))`.
> Option (b) is preferred for post-M1 hardening; option (a) is the
> quickest for a one-off manual deploy.

## 3. Deploy

Manual release-engineer gate; **NOT** part of the implementation tasks in
this spec:

```
uv run cdk deploy WebhookReceiverStack \
  -c webhook_secret_arn=<arn> \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

## 4. Post-deploy validation

- The stack emits `api_endpoint` (the HTTPS URL for API Gateway) as a
  public attribute. Copy it from the `cdk deploy` output.
- Register that URL as the GitHub App's **Webhook URL** on the
  marketplace listing.
- Trigger a GitHub `ping` webhook from the App settings page. The
  receiver should respond `200` with body `{"status": "pong"}`.
- Send an `X-GitHub-Event: pull_request` `opened` webhook (from a test
  installation) and check the `trikon-verify-jobs` SQS queue for the
  enqueued message body — it must match the memo §5.2 shape.

## 5. Rollback

```
uv run cdk destroy WebhookReceiverStack \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

`cdk destroy` is safe because the stack does **not** own the Secrets
Manager secret (referenced by ARN, not created) and does **not** own any
DynamoDB table (the webhook receiver has no data plane). Destroying the
stack removes only the Lambda, API Gateway HTTP API, SQS main queue,
SQS DLQ, IAM role, and log group. The CloudWatch log group's removal
policy is `RETAIN` — historical logs survive the teardown.

## 6. Deviations flagged for the release engineer

- The stack uses `lambda_.Function` with an un-bundled asset because the
  `lambda_python_alpha.PythonFunction` module was not available in the
  implementation-time venv and Docker was not available for
  `BundlingOptions`. Before deploy, resolve the vendoring story (see
  §2 note above).
- No custom domain at MVP — the auto-generated `execute-api` URL is
  registered on the GitHub App directly. Adding a custom domain is a
  post-M1 spec.
- No WAF rule allow-listing GitHub webhook IPs at MVP — see the
  `trikon-cloud-webhook-receiver-waf-hardening` follow-up spec.
