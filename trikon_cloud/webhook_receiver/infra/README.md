# `WebhookReceiverStack` — deploy runbook

> **Production go-live.** For the production go-live sequence (SSM
> parameters, ECR repository and runner image, Lambda asset, the three
> deploys and the GitHub App webhook), follow
> [`../../DEPLOY.md`](../../DEPLOY.md). This file is the per-stack
> reference for `WebhookReceiverStack`.

CDK Python stack for the Trikon Cloud webhook receiver (Spec 1 of the M1
milestone). Provisions a single Python 3.11 arm64 Lambda function fronted by an API Gateway
HTTP API, with the `trikon-verify-jobs` SQS queue + DLQ, a dedicated
CloudWatch log group (`/aws/lambda/trikon-cloud-webhook-receiver`, 7-day
retention), and a minimum-viable IAM role.

> **Callout.** The deploy commands below are **informational**. The release
> engineer runs them, not the implementation tasks in this spec. All spec
> tasks stop at `cdk synth` (offline, no AWS API calls) — the manual gate
> to `cdk deploy` is a release-engineer action per Requirement 17.3.

## When to deploy

Deploy Trikon Cloud only once hosted PR checks are wanted. Until then none
of the three stacks is deployed and nothing is billed. `cdk synth` keeps
working offline in the meantime.

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
  deploy (the webhook secret is the value set in the App's settings).
- The three SSM SecureString parameters below must exist in the target
  region (us-east-1) before the first deploy of any stack.
- A Lambda asset built from the current sources (§2). Every synth, deploy
  and destroy names it with `-c lambda_asset_dir=build/lambda-asset`.

### Deploy order

Deploy the three stacks in this order:

1. `FargateRunnerStack` (`trikon_cloud/fargate_runner/infra/`). It creates
   the DynamoDB tables, the cluster, the VPC and the outputs for the
   runner subnet and security-group ids.
2. `WebhookReceiverStack` (this stack). It creates `trikon-verify-jobs`
   and its DLQ. It needs `installation_events_queue_arn`, the ARN of the
   `trikon-cloud-installation-events` queue that step 3 creates. The
   stack imports that queue by ARN and does not check that it exists, so
   pass the ARN built from the fixed queue name:
   `arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events`.
3. `TrikonCloudOrchestratorStack` (`trikon_cloud/orchestrator/infra/`). It
   consumes `trikon-verify-jobs` (pass the queue and DLQ ARNs from step 2
   as `verify_jobs_queue_arn` and `verify_jobs_dlq_arn`) and owns the installation-events queue the receiver writes to.

Register the GitHub App webhook URL only after step 3 (see §4).

### SSM parameters

Trikon Cloud keeps its secrets in SSM Parameter Store as `SecureString`
parameters, standard tier, encrypted with the AWS-managed `aws/ssm` key.
A standard-tier value is limited to 4 KB. The stacks reference each
parameter by name and never create it; the operator creates them
out-of-band.

| Parameter (default name) | Read by | Created per |
|---|---|---|
| `/trikon-cloud/github-webhook-secret` | Webhook receiver | this runbook |
| `/trikon-cloud/github-app-private-key` | Runner, orchestrator | `fargate_runner/infra/README.md` |
| `/trikon-cloud/usage-secret` | Orchestrator, usage reporter | `orchestrator/infra/README.md` |

Create the webhook secret parameter with a placeholder value replaced by
the App's webhook secret. Omitting `--key-id` selects the `aws/ssm` key:

```bash
aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-webhook-secret \
  --type SecureString --tier Standard --value '<webhook-secret>'
```

To keep the value out of shell history, write it to a file and pass
`--value file://<path-to-webhook-secret-file>` instead.

The receiver reads the parameter with `ssm:GetParameter` and
`WithDecryption=True`. The stack passes the name to the Lambda as
`TRIKON_WEBHOOK_SECRET_PARAMETER_NAME` and grants the Lambda role
`ssm:GetParameter` on exactly that parameter's ARN (IAM statement Sid
`WebhookSecretParameterRead`). No `kms:Decrypt` grant is needed for the
AWS-managed key.

To use a different name, create the parameter under that name and pass
`-c webhook_secret_parameter_name=/custom/name` to `cdk synth` and
`cdk deploy`. The name must start with `/`; an empty name or one without
a leading `/` fails synthesis with a message naming the context key.

The obsolete `webhook_secret_arn` context key is no longer accepted.
Passing it fails synthesis, so a stale deploy command cannot silently
fall back to the default parameter name.

## 2. Build the Lambda asset, then synth

The stack packages the Lambda code from a prebuilt Lambda asset directory
named by the required `lambda_asset_dir` context key. Build the asset from
the repo root with the Lambda asset builder:

```
uv run python -m trikon_cloud.lambda_asset --out build/lambda-asset
```

The builder copies the `trikon_cloud` package sources into
`build/lambda-asset/trikon_cloud/`, installs the packages listed in
`trikon_cloud/lambda-requirements.txt` as CPython 3.11
`manylinux2014_aarch64` (arm64) wheels, and writes the manifest
`trikon-lambda-asset.json` last. It needs no Docker daemon and makes no
AWS call. The same asset serves `TrikonCloudOrchestratorStack`. Rebuild it
after any change under `trikon_cloud/`: synthesis fails with a message
starting `lambda_asset_dir` when the value is missing, names a directory
without the manifest, or names an asset built from older sources.

Synth is offline — it makes no AWS API calls and exits 0 if the emitted
CloudFormation template is valid. Run it from the repo root, because
`build/lambda-asset` is relative to the current directory:

```
uv run cdk synth \
  -c installation_events_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events \
  -c lambda_asset_dir=build/lambda-asset \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

Add `-c webhook_secret_parameter_name=/custom/name` only when the
parameter does not use the default name.

The emitted template lands under `cdk.out/WebhookReceiverStack.template.json`.

## 3. Deploy

Manual release-engineer gate; **NOT** part of the implementation tasks in
this spec. Deploy `FargateRunnerStack` first (see "Deploy order") and
build the Lambda asset (§2):

```
uv run cdk deploy WebhookReceiverStack \
  -c installation_events_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events \
  -c lambda_asset_dir=build/lambda-asset \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

Then deploy `TrikonCloudOrchestratorStack` with the ARNs of
`trikon-verify-jobs` and `trikon-verify-jobs-dlq`, which follow from the
fixed queue names (`arn:aws:sqs:us-east-1:<account-id>:trikon-verify-jobs`
and `...:trikon-verify-jobs-dlq`).

## 4. Post-deploy validation

Run these checks after all three stacks are deployed.

- `cdk deploy` prints the stack output `WebhookUrl`: the HTTP API
  endpoint followed by `/webhooks/github`, which is the full GitHub App
  webhook URL. Copy it from the `cdk deploy` output, or read it later
  with
  `aws cloudformation describe-stacks --region us-east-1 --stack-name WebhookReceiverStack --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text`.
- Register that URL unchanged as the **Webhook URL** in the GitHub App
  settings.
- Trigger a GitHub `ping` webhook from the App settings page. The
  receiver should respond `200` with body `{"status": "pong"}`.
- Send an `X-GitHub-Event: pull_request` `opened` webhook (from a test
  installation) and check the `trikon-verify-jobs` SQS queue for the
  enqueued message body — it must match the memo §5.2 shape.

## 5. Rollback

```
uv run cdk destroy WebhookReceiverStack \
  -c installation_events_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events \
  -c lambda_asset_dir=build/lambda-asset \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

`cdk destroy` synthesizes the app first, so it needs the same required
context as the deploy and a current Lambda asset (§2).

`cdk destroy` is safe because the stack does **not** own the webhook
secret parameter (referenced by name, not created) and does **not** own
any DynamoDB table (the webhook receiver has no data plane). Destroying
the stack removes only the Lambda, API Gateway HTTP API, SQS main queue,
SQS DLQ and IAM role. The parameter survives the teardown.

The CloudWatch log group's removal policy is `RETAIN`, so it also
survives the teardown and its logs still expire after 7 days. Delete
`/aws/lambda/trikon-cloud-webhook-receiver` before re-creating the stack
after a `cdk destroy`; otherwise the deploy fails on the name collision.

## 6. Deviations flagged for the release engineer

- The stack uses `lambda_.Function` with the prebuilt Lambda asset (§2)
  instead of `lambda_python_alpha.PythonFunction` or CDK Docker bundling,
  so a deploy needs no Docker daemon. Build the asset before every
  synth, deploy or destroy that follows a source change.
- No custom domain at MVP — the auto-generated `execute-api` URL is
  registered on the GitHub App directly. Adding a custom domain is a
  post-M1 spec.
- No WAF rule allow-listing GitHub webhook IPs at MVP — see the
  `trikon-cloud-webhook-receiver-waf-hardening` follow-up spec.
