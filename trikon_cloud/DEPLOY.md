# Trikon Cloud go-live runbook

> **Shared AWS account. Read this first.**
>
> - Trikon Cloud deploys to account `818515814116`, region `us-east-1`. The same account also holds the **Unideploy production stacks**. A wrong delete or a wrong deploy here can take Unideploy down.
> - Before every phase, run `aws sts get-caller-identity --query Account --output text` and stop if it does not print `818515814116`. Each phase below starts with this check.
> - Act only on resources whose names start with `trikon`, `Trikon`, `FargateRunnerStack`, `WebhookReceiverStack`, `/trikon`, `/aws/lambda/trikon` or `/aws/ecs/trikon`. Leave every other resource alone, even if it looks unused.
> - Always name the stack. Never run `cdk deploy --all`, `cdk destroy --all` or `cdk bootstrap`. Never run a delete command without an explicit `trikon` name, and never use a wildcard, a prefix filter or a `--query` result as the input to a delete.
> - Run `cdk diff <StackId>` before every deploy and read it. Stop if it shows a resource outside the names above, or an environment other than `aws://818515814116/us-east-1`.

This runbook covers Phases 3 to 5: the AWS deploy of the three Trikon Cloud stacks, the GitHub App webhook, and an end-to-end test pull request. Phases 0 to 2 (merges, web prerequisites, Render and DNS) are in the web runbook, [`trikon-web/DEPLOY.md`](../../trikon-web/DEPLOY.md), in the `suryansh639/trikon-web` repository. Finish those first: Trikon Cloud calls the Trikon Web server for quota and usage.

Nothing in this runbook has been run for you. Every AWS call, deploy and GitHub setting change is yours.

The per-stack references are [`fargate_runner/infra/README.md`](fargate_runner/infra/README.md), [`webhook_receiver/infra/README.md`](webhook_receiver/infra/README.md) and [`orchestrator/infra/README.md`](orchestrator/infra/README.md). They describe each stack for any account. This file is the ordered go-live sequence for the shared account.

## Phase index

| Phase | What | Runbook |
|---|---|---|
| Phase 0 | Merge the feature branches to `main` | `trikon-web/DEPLOY.md` |
| Phase 1 | Web prerequisites: legal details, Neon, usage secret, OAuth, GitHub App, Resend, PayPal live, admins | `trikon-web/DEPLOY.md` |
| Phase 2 | Render blueprint, Cloudflare CNAMEs, web smoke test | `trikon-web/DEPLOY.md` |
| Phase 3 | AWS: pre-flight, SSM parameters, ECR and runner image, Lambda asset, three `cdk deploy` runs, verification | this file |
| Phase 4 | GitHub App webhook and ping | this file |
| Phase 5 | End-to-end test pull request | this file |

## Step tags

Every numbered step starts with one of these tags:

- `[user]`: only you can do this step (a console, an account, a credential or a decision).
- `[automatable]`: an agent or a script could do this step, but you run it for this launch.

Conventions:

- Commands are PowerShell on Windows, run from the `Trikon\` repository root, unless the step says WSL. WSL commands run through `wsl -d Ubuntu-22.04 -e …`.
- Run all of Phase 3 in **one PowerShell window**. Later steps use variables (`$image`, `$appId`, `$subnets`, `$sg`, `$webhookUrl`) set by earlier steps. If you open a new window, redo [3a step 1](#3a-session-setup-and-pre-flight) and the steps that set the variables you need.
- `<angle-brackets>` are placeholders. Replace them with your own values. This file holds no secret, token or key, and none should ever be added to it.
- You need the AWS CLI v2, the `cdk` CLI (Node.js), `uv`, and Docker inside the WSL distribution `Ubuntu-22.04`.

## Phase 3: AWS deploy

### 3a. Session setup and pre-flight

1. [user] Open PowerShell in the Trikon repository root and pin the region for this window. The CDK apps take their region from `CDK_DEFAULT_REGION`, which the `cdk` CLI sets from your AWS configuration. Without these lines a profile with a different default region would deploy the runner and receiver stacks to that region.
   ```powershell
   Set-Location C:\Users\surya\Downloads\demounideploy-main\Trikon
   $env:AWS_REGION = "us-east-1"
   $env:AWS_DEFAULT_REGION = "us-east-1"
   # Only if you use a named profile:
   # $env:AWS_PROFILE = "<profile-name>"
   ```
2. [automatable] Confirm the account. Stop if this throws.
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
3. [automatable] Check the tools. `uv` must be on PATH, because the repository `.venv` has no `pip` and the Lambda asset builder installs wheels with `uv`. Docker must answer inside WSL.
   ```powershell
   aws --version
   cdk --version
   uv --version
   wsl -d Ubuntu-22.04 -e docker info --format "{{.ServerVersion}}"
   ```
   If `docker info` fails, start Docker in WSL (for example `wsl -d Ubuntu-22.04 -e sudo service docker start`) and retry.
4. [automatable] The CDK bootstrap stack `CDKToolkit` already exists in the account. Check that it is there and note its bootstrap version:
   ```powershell
   aws cloudformation describe-stacks --region us-east-1 --stack-name CDKToolkit `
     --query "Stacks[0].Outputs[?OutputKey=='BootstrapVersion'].OutputValue" --output text
   ```
   The command must print a number. Never run `cdk bootstrap` in this account: `CDKToolkit` is shared with Unideploy, and re-bootstrapping with other settings changes its roles and policies. If a later `cdk deploy` says the bootstrap version is too old, stop and agree an upgrade with the owner of the Unideploy stacks.
5. [automatable] The three stack names must be free. Each command must fail with "Stack with id … does not exist". `FargateRunnerStack` and `WebhookReceiverStack` are generic names, so stop if either already exists and find out what it is.
   ```powershell
   foreach ($s in "FargateRunnerStack", "WebhookReceiverStack", "TrikonCloudOrchestratorStack") {
     aws cloudformation describe-stacks --region us-east-1 --stack-name $s --query "Stacks[0].StackStatus" --output text
   }
   ```
6. [automatable] The S3 bucket name `trikon-cloud-evidence` is global across all AWS accounts. Check that it is free:
   ```powershell
   aws s3api head-bucket --bucket trikon-cloud-evidence
   ```
   - An error with `404` (Not Found) means the name is free. Continue.
   - An error with `403` (Forbidden) means another account owns the name. Stop. A configurable bucket name is a code follow-up (blocker B13).
   - No error means the bucket already exists in this account, left over from an earlier deploy. Stop and decide what to do with it (see [Rollback](#rollback), leftovers).
7. [automatable] No Trikon resource may exist yet. Every stack uses fixed names, so a leftover makes the deploy fail halfway. Each command must print an empty list (`[]`) or nothing:
   ```powershell
   aws dynamodb list-tables --region us-east-1 --query "TableNames[?starts_with(@, 'trikon')]"
   aws iam list-roles --query "Roles[?starts_with(RoleName, 'trikon') || starts_with(RoleName, 'Trikon') || starts_with(RoleName, 'FargateRunnerStack') || starts_with(RoleName, 'WebhookReceiverStack')].RoleName"
   aws logs describe-log-groups --region us-east-1 --log-group-name-prefix /aws/lambda/trikon --query "logGroups[].logGroupName"
   aws logs describe-log-groups --region us-east-1 --log-group-name-prefix /aws/ecs/trikon --query "logGroups[].logGroupName"
   aws sqs list-queues --region us-east-1 --queue-name-prefix trikon
   aws lambda list-functions --region us-east-1 --query "Functions[?starts_with(FunctionName, 'trikon')].FunctionName"
   aws ssm describe-parameters --region us-east-1 --parameter-filters "Key=Name,Option=BeginsWith,Values=/trikon" --query "Parameters[].Name"
   aws ecr describe-repositories --region us-east-1 --query "repositories[?starts_with(repositoryName, 'trikon')].repositoryName"
   aws ecs describe-clusters --region us-east-1 --clusters trikon-verify-cluster --query "clusters[?status=='ACTIVE'].clusterName"
   aws events list-rules --region us-east-1 --name-prefix trikon --query "Rules[].Name"
   aws apigatewayv2 get-apis --region us-east-1 --query "Items[?starts_with(Name, 'trikon')].Name"
   aws ec2 describe-vpcs --region us-east-1 --filters "Name=tag:Name,Values=trikon-verify-vpc" --query "Vpcs[].VpcId"
   ```
   These commands only list. If any prints a name, stop: either an earlier Trikon deploy left it behind (delete it by its explicit name, see [Rollback](#rollback)) or something else uses the name.
8. [automatable] The runner stack creates one VPC. Check that the region has room (the default quota is 5 VPCs per region):
   ```powershell
   aws ec2 describe-vpcs --region us-east-1 --query "length(Vpcs)"
   ```
   If the count is at the quota, stop. Ask for a quota increase; do not delete another VPC.
9. [automatable] The runner Dockerfile builds on `suryansh639/trikon:0.5.0` (`ARG BASE_IMAGE` in `trikon_cloud/fargate_runner/Dockerfile`). Check that Docker in WSL can pull it:
   ```powershell
   wsl -d Ubuntu-22.04 -e docker pull suryansh639/trikon:0.5.0
   ```

### 3b. SSM parameters

Trikon Cloud reads three SSM SecureString parameters. The stacks reference them by name and never create them. The commands omit `--key-id`, so SSM encrypts each value with the AWS-managed `aws/ssm` key, and no `kms:Decrypt` grant is needed. They also omit `--overwrite`, so a command fails instead of replacing a value that already exists.

| Parameter | Value | Read by |
|---|---|---|
| `/trikon-cloud/github-app-private-key` | A Trikon Cloud-only private key (PEM) of the Trikon Verify GitHub App | runner tasks, orchestrator, Spot retry handler |
| `/trikon-cloud/github-webhook-secret` | A new random secret; the same value goes into the App's webhook settings in Phase 4 | webhook receiver |
| `/trikon-cloud/usage-secret` | The `TRIKON_CLOUD_USAGE_SECRET` value from Phase 1 of the web runbook | orchestrator, usage reporter |

1. [automatable] Confirm the account:
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
2. [user] In the Trikon Verify GitHub App settings (GitHub → Settings → Developer settings → GitHub Apps → the App → General → Private keys), click **Generate a private key**. Use this key for Trikon Cloud only. Trikon Web has its own key from Phase 1, so each can be revoked alone. Note where the `.pem` file was saved.
3. [automatable] Generate the webhook secret into a variable and copy it to the clipboard. Save it in your password manager now; Phase 4 needs it.
   ```powershell
   $webhookSecret = wsl -d Ubuntu-22.04 -e openssl rand -hex 32
   $webhookSecret | Set-Clipboard
   ```
4. [user] Read the usage secret from your password manager into a variable. `Read-Host` input is not saved in the PowerShell history.
   ```powershell
   $usageSecret = Read-Host "Paste TRIKON_CLOUD_USAGE_SECRET"
   ```
   It must be exactly the value stored in Render as `TRIKON_CLOUD_USAGE_SECRET`. If the two copies differ, every PR check fails closed with "quota unavailable".
5. [automatable] Create the three parameters. `file://` reads the PEM from the file, which keeps it out of the shell history and keeps its line breaks:
   ```powershell
   aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-app-private-key `
     --type SecureString --tier Standard --value "file://<path-to-trikon-cloud-app-key>.pem"
   aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-webhook-secret `
     --type SecureString --tier Standard --value $webhookSecret
   aws ssm put-parameter --region us-east-1 --name /trikon-cloud/usage-secret `
     --type SecureString --tier Standard --value $usageSecret
   ```
   A standard-tier value is limited to 4 KB; a GitHub App PEM is about 1.7 KB.
6. [user] Delete the PEM file and clear the variables:
   ```powershell
   Remove-Item "<path-to-trikon-cloud-app-key>.pem"
   Remove-Variable webhookSecret, usageSecret
   ```
7. [automatable] Check that the three parameters exist as standard-tier SecureStrings (this prints names, not values):
   ```powershell
   aws ssm describe-parameters --region us-east-1 `
     --parameter-filters "Key=Name,Option=BeginsWith,Values=/trikon-cloud/" `
     --query "Parameters[].[Name,Type,Tier]" --output table
   ```

### 3c. ECR repository and runner image

The runner image lives in the private ECR repository `trikon-cloud-runner`, which you create here with the AWS CLI. The stack does not create it, so the image exists before the task definition that names it, and the repository survives a `cdk destroy`. The task execution role has the AWS-managed `AmazonECSTaskExecutionRolePolicy`, which allows pulls from private ECR in the same account.

1. [automatable] Confirm the account:
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
2. [automatable] Create the repository with immutable tags and scan-on-push:
   ```powershell
   aws ecr create-repository --region us-east-1 --repository-name trikon-cloud-runner `
     --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true
   ```
3. [automatable] Add a lifecycle policy that keeps the last 5 images. The policy file goes in `%TEMP%`, outside the repository, and is ASCII so the CLI parses it:
   ```powershell
   $policy = Join-Path $env:TEMP "trikon-ecr-lifecycle.json"
   Set-Content -Path $policy -Encoding ascii -Value '{"rules":[{"rulePriority":1,"description":"Keep the last 5 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}'
   aws ecr put-lifecycle-policy --region us-east-1 --repository-name trikon-cloud-runner `
     --lifecycle-policy-text "file://$policy"
   Remove-Item $policy
   ```
   The policy expires the oldest images first. Five pushes without a deploy would expire the image the task definition names, so deploy each image you push.
4. [automatable] Pick the image tag from the current commit. The tag is `runner-<12-character git sha>`; the CDK app rejects the tag `latest`. Build from a clean checkout, so the tag names exactly the code in the image:
   ```powershell
   git status --porcelain   # must print nothing
   $tag = "runner-$(git rev-parse --short=12 HEAD)"
   $image = "818515814116.dkr.ecr.us-east-1.amazonaws.com/trikon-cloud-runner:$tag"
   $image
   ```
5. [automatable] Build the image inside WSL for `linux/amd64`. The runner task definition in `fargate_runner/infra/fargate_runner_stack.py` sets no runtime platform (no `runtime_platform`, so no `cpu_architecture`), and Fargate then runs the task on Linux X86_64:
   ```powershell
   wsl -d Ubuntu-22.04 -e bash -lc "cd /mnt/c/Users/surya/Downloads/demounideploy-main/Trikon && docker build --platform linux/amd64 -f trikon_cloud/fargate_runner/Dockerfile -t $image ."
   ```
   The build context is the Trikon root. `.dockerignore` keeps tests, infra code, `.venv` and `.git` out of it.
6. [automatable] Log in to ECR and push the image:
   ```powershell
   aws ecr get-login-password --region us-east-1 | wsl -d Ubuntu-22.04 -e docker login --username AWS --password-stdin 818515814116.dkr.ecr.us-east-1.amazonaws.com
   wsl -d Ubuntu-22.04 -e docker push $image
   ```
7. [automatable] Check that ECR has the image and look at the scan result:
   ```powershell
   aws ecr describe-images --region us-east-1 --repository-name trikon-cloud-runner --image-ids imageTag=$tag `
     --query "imageDetails[0].[imageTags[0],imageDigest,imageScanStatus.status]" --output table
   ```
   Tags are immutable: a changed image needs a new tag, normally from a new commit. Pushing the same tag twice fails.

### 3d. Lambda asset

All five Lambda functions run from one asset directory. The builder copies the `trikon_cloud` sources under a `trikon_cloud/` root and installs the third-party packages from `trikon_cloud/lambda-requirements.txt` as CPython 3.11 `manylinux2014_aarch64` wheels. It uses `uv` when it is on PATH and falls back to `python -m pip`; it needs no Docker.

1. [automatable] Build the asset from the Trikon root. It downloads wheels from PyPI, so it needs internet access:
   ```powershell
   .venv\Scripts\python.exe -m trikon_cloud.lambda_asset --out build/lambda-asset
   ```
   On success it prints `Lambda asset written to … MB unzipped (Lambda limit 250 MB)`. A non-zero exit means no asset was written; the message names the cause. `build/` is git-ignored.
2. [user] Rebuild the asset after any change to the `trikon_cloud` sources, including a `git checkout` or `git pull`. Synthesis compares a digest of the sources with the asset and refuses a stale asset with a message starting `lambda_asset_dir … is stale`.

### 3e. Deploys

Deploy the three stacks in this order: `FargateRunnerStack`, `WebhookReceiverStack`, `TrikonCloudOrchestratorStack`. There is no `cdk.json`, so every command passes `--app` with the repository's Python. Run every command from the Trikon root, in the window that holds `$image`.

Keep the default approval prompt. `cdk deploy` asks before it creates IAM roles and policies; read the list and answer `y`. Never pass `--require-approval never`.

Values that pass between stacks:

| Value | From | To | How |
|---|---|---|---|
| `RunnerSubnetIds` (comma-separated public subnet ids) | `FargateRunnerStack` output | orchestrator `-c runner_subnet_ids=` | step 4 reads it into `$subnets` |
| `RunnerSecurityGroupId` | `FargateRunnerStack` output | orchestrator `-c runner_security_group_ids=` | step 4 reads it into `$sg` |
| `/trikon/verify-runner/active-revision` | `FargateRunnerStack` creates it on every deploy | orchestrator, at cold start, by its default name | nothing to pass; [3f step 2](#3f-verify) checks it |
| ARN of `trikon-verify-jobs` and of `trikon-verify-jobs-dlq` | `WebhookReceiverStack` creates both queues | orchestrator `-c verify_jobs_queue_arn=` and `-c verify_jobs_dlq_arn=` | derived from the fixed queue names |
| ARN of `trikon-cloud-installation-events` | `TrikonCloudOrchestratorStack` creates the queue | receiver `-c installation_events_queue_arn=` | derived from the fixed queue name and passed **before** the queue exists; the receiver imports it by ARN and does not check it (see [`webhook_receiver/infra/README.md`](webhook_receiver/infra/README.md), "Deploy order") |
| `WebhookUrl` | `WebhookReceiverStack` output | GitHub App webhook URL, Phase 4 | step 7 reads it into `$webhookUrl` |
| `https://api.trikon.unideploy.com` and `https://trikon.unideploy.com` | Trikon Web (web runbook Phase 2) | orchestrator `-c trikon_web_api_url=` and `-c trikon_web_app_url=` | literal base URLs with no path; the usage client appends `/api/v1/cloud/quota` and `/api/v1/cloud/usage` itself |

Required context keys per app (each `build_app` raises without them):

| Stack | Required `-c` keys |
|---|---|
| `FargateRunnerStack` | `app_id`, `runner_image_uri` |
| `WebhookReceiverStack` | `installation_events_queue_arn`, `lambda_asset_dir` |
| `TrikonCloudOrchestratorStack` | `app_id`, `verify_jobs_queue_arn`, `verify_jobs_dlq_arn`, `runner_subnet_ids`, `runner_security_group_ids`, `trikon_web_api_url`, `trikon_web_app_url`, `lambda_asset_dir` |

The optional keys keep their defaults: the three `*_parameter_name` keys default to the SSM names in 3b, `use_fargate_spot` defaults to `true`, and `verify_runner_active_revision_ssm_param` defaults to `/trikon/verify-runner/active-revision`.

1. [user] Set the Trikon Verify App ID: the numeric App ID on the App's General page, the same value as `GITHUB_APP_ID` in Render.
   ```powershell
   $appId = "<trikon-verify-app-id>"
   ```
2. [automatable] Diff the runner stack and read the output. The environment must be `aws://818515814116/us-east-1`, and every resource must be a Trikon resource (tables `trikon_*`, bucket `trikon-cloud-evidence`, `trikon-verify-*` VPC, security group, cluster and task definition, log group `/aws/ecs/trikon-verify-runner`, parameter `/trikon/verify-runner/active-revision`, and `FargateRunnerStack-*` roles).
   ```powershell
   cdk diff FargateRunnerStack `
     -c app_id=$appId `
     -c runner_image_uri=$image `
     --app ".venv\Scripts\python.exe -m trikon_cloud.fargate_runner.infra.app"
   ```
3. [user] Deploy the runner stack:
   ```powershell
   cdk deploy FargateRunnerStack `
     -c app_id=$appId `
     -c runner_image_uri=$image `
     --app ".venv\Scripts\python.exe -m trikon_cloud.fargate_runner.infra.app"
   ```
4. [automatable] Read the runner outputs into variables. Use the variables in the orchestrator commands: a literal `subnet-a,subnet-b` typed on a PowerShell command line becomes an array and is passed with a space instead of the comma.
   ```powershell
   $subnets = aws cloudformation describe-stacks --region us-east-1 --stack-name FargateRunnerStack `
     --query "Stacks[0].Outputs[?OutputKey=='RunnerSubnetIds'].OutputValue" --output text
   $sg = aws cloudformation describe-stacks --region us-east-1 --stack-name FargateRunnerStack `
     --query "Stacks[0].Outputs[?OutputKey=='RunnerSecurityGroupId'].OutputValue" --output text
   $subnets; $sg
   ```
   `$subnets` must show two `subnet-…` ids separated by a comma, and `$sg` one `sg-…` id.
5. [automatable] Diff the webhook receiver stack. Expect the `trikon-cloud-webhook-receiver` function (arm64), the `trikon-cloud-webhook-receiver-api` HTTP API, the `trikon-verify-jobs` queue and its DLQ, the log group `/aws/lambda/trikon-cloud-webhook-receiver`, a `WebhookReceiverStack-*` role, and the `WebhookUrl` output.
   ```powershell
   cdk diff WebhookReceiverStack `
     -c installation_events_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-cloud-installation-events `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.webhook_receiver.infra.app"
   ```
6. [user] Deploy the webhook receiver stack:
   ```powershell
   cdk deploy WebhookReceiverStack `
     -c installation_events_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-cloud-installation-events `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.webhook_receiver.infra.app"
   ```
7. [automatable] Read the webhook URL. `cdk deploy` also prints it as `WebhookReceiverStack.WebhookUrl = https://…/webhooks/github`.
   ```powershell
   $webhookUrl = aws cloudformation describe-stacks --region us-east-1 --stack-name WebhookReceiverStack `
     --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text
   $webhookUrl
   ```
8. [automatable] Diff the orchestrator stack. Expect the four `trikon-cloud-*` functions (orchestrator, installation lifecycle, usage reporter, Spot retry), their `trikon-cloud-*-role` roles and `/aws/lambda/trikon-cloud-*` log groups, the `trikon-cloud-installation-events` queue and the three `trikon-cloud-*-dlq` queues, the `trikon-cloud-installations` and `trikon-cloud-spot-retries` tables, and the `trikon-verify-runner-task-stopped` rule.
   ```powershell
   cdk diff TrikonCloudOrchestratorStack `
     -c app_id=$appId `
     -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs `
     -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs-dlq `
     -c runner_subnet_ids=$subnets `
     -c runner_security_group_ids=$sg `
     -c trikon_web_api_url=https://api.trikon.unideploy.com `
     -c trikon_web_app_url=https://trikon.unideploy.com `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.orchestrator.infra.app"
   ```
9. [user] Deploy the orchestrator stack:
   ```powershell
   cdk deploy TrikonCloudOrchestratorStack `
     -c app_id=$appId `
     -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs `
     -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs-dlq `
     -c runner_subnet_ids=$subnets `
     -c runner_security_group_ids=$sg `
     -c trikon_web_api_url=https://api.trikon.unideploy.com `
     -c trikon_web_app_url=https://trikon.unideploy.com `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.orchestrator.infra.app"
   ```
   To run every task on on-demand Fargate instead of Spot, add `-c use_fargate_spot=false` to both the diff and the deploy.

### 3f. Verify

1. [automatable] Confirm the account:
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
2. [automatable] Check the active-revision parameter. `FargateRunnerStack` creates `/trikon/verify-runner/active-revision` and updates it on every deploy, so there is nothing to set by hand. The orchestrator reads it at cold start.
   ```powershell
   aws ssm get-parameter --region us-east-1 --name /trikon/verify-runner/active-revision --query Parameter.Value --output text
   ```
   It must print the task definition revision number (`1` after the first deploy). If the parameter is missing, the runner deploy did not finish. Do not create the parameter by hand; fix and rerun [3e step 3](#3e-deploys).
3. [automatable] Check that all five functions are `Active`. Nothing is invoked by hand.
   ```powershell
   foreach ($fn in "trikon-cloud-webhook-receiver", "trikon-cloud-orchestrator", "trikon-cloud-installation-lifecycle", "trikon-cloud-usage-reporter", "trikon-cloud-spot-retry") {
     "$fn " + (aws lambda get-function --region us-east-1 --function-name $fn --query Configuration.State --output text)
   }
   ```
4. [automatable] Check that the receiver code loads. A request without the `X-GitHub-Event` header is rejected before any secret is read or anything is queued:
   ```powershell
   curl.exe -i -X POST $webhookUrl
   ```
   Expect `400` with `{"error": "missing_event_header"}`. A `500` with "Internal Server Error" means the function failed to start; read `aws logs tail /aws/lambda/trikon-cloud-webhook-receiver --region us-east-1 --since 15m` (often an import error from a wrong or stale Lambda asset).

## Phase 4: GitHub App webhook

Do this in the settings of the production Trikon Verify GitHub App (GitHub → Settings → Developer settings → GitHub Apps → the App). It is the same App whose Setup URL and callback URL point at `https://trikon.unideploy.com` from Phase 1 of the web runbook.

1. [automatable] Confirm the account (Phase 5 reads AWS again):
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
2. [user] Under **Permissions & events → Repository permissions**, set at least: Checks read and write, Contents read-only, Pull requests read-only, Metadata read-only. Under **Organization permissions**, keep Members read-only. Save. Accounts that already installed the App must accept the new permissions.
3. [user] Under **General → Webhook**:
   - turn **Active** on;
   - set **Webhook URL** to the `$webhookUrl` value from [3e step 7](#3e-deploys) (it ends in `/webhooks/github`);
   - set **Webhook secret** to the `/trikon-cloud/github-webhook-secret` value from 3b step 3 (from your password manager);
   - keep **SSL verification** enabled.

   Save. GitHub App webhooks are always JSON.
4. [user] Under **Permissions & events → Subscribe to events**, tick **"Pull request"** only, and leave every other event unticked. The checkbox appears once the Pull requests permission from step 2 is saved. GitHub delivers `installation` and `installation_repositories` events to every App webhook without a subscription, and the receiver routes them to the installation lifecycle function. Save.
5. [user] Under **Advanced → Recent Deliveries**, open the `ping` delivery GitHub sent when the webhook became active and click **Redeliver**. Expect response `200` with body `{"status": "pong"}`. The receiver answers `ping` before it checks the signature, so a `pong` proves the URL, not the secret. Phase 5 step 4 checks the secret.

## Phase 5: end-to-end test pull request

1. [automatable] Confirm the account:
   ```powershell
   $account = aws sts get-caller-identity --query Account --output text
   if ($account -ne "818515814116") { throw "Wrong AWS account: $account. Stop." }
   ```
2. [user] Create a private test repository with a little code in it (for example one Python module and one test).
3. [user] Install the Trikon Verify App on **only** that repository, from `https://github.com/apps/<app-slug>/installations/new`. Install it after Phase 4: the `installation` `created` delivery is what makes Trikon Cloud create the installation's task role. If the App was installed on that account before Phase 4, uninstall it there first, then install it again.
4. [user] In **Advanced → Recent Deliveries**, the `installation` delivery must show `202` with `{"status": "accepted", …}`. A `401` with `hmac_verification_failed` means the App's webhook secret differs from `/trikon-cloud/github-webhook-secret`: set the App's secret to the stored value and redeliver.
5. [automatable] Check that the task role exists. The installation id is the number at the end of the installation's settings URL (`https://github.com/settings/installations/<installation-id>`, or under the organization's settings):
   ```powershell
   aws iam get-role --role-name trikon-verify-task-role-<installation-id> --query Role.RoleName --output text
   ```
6. [user] Sign in at `https://trikon.unideploy.com` and connect the installation on `https://trikon.unideploy.com/dashboard/installations`.
7. [user] Check that the account has sandbox minutes left on the dashboard. An account without a subscription is on the free plan and gets its monthly allowance; a paid plan works too.
8. [user] Open a small pull request in the test repository (for example a one-line change to the module).
9. [user] Check the pull request's **Checks**. Expected result: a Check Run named `Trikon` on the head commit that completes, usually within a few minutes, with a verdict summary and one of these conclusions:
   - `success`: the verdict is ALLOW;
   - `failure`: the verdict is BLOCK;
   - `neutral`: the verdict is REQUIRE HUMAN REVIEW.

   Any of the three proves the pipeline end to end. A Check Run with one of these titles means a step failed (see Troubleshooting below): "Connect your Trikon account", "Monthly sandbox minutes used up", "Trikon plan unavailable", "PR check couldn't start: too many checks in progress", "Trikon Cloud verification unavailable".
10. [automatable] Check that the verdict was stored. The count must be at least 1:
    ```powershell
    aws dynamodb scan --region us-east-1 --table-name trikon_verdicts --select COUNT --query Count
    ```
11. [user] Reload the dashboard. The minutes used for the period must have gone up.
12. [automatable] Check that every dead-letter queue is empty (each line must end in `0`):
    ```powershell
    foreach ($q in "trikon-verify-jobs-dlq", "trikon-cloud-installation-events-dlq", "trikon-cloud-usage-reporter-dlq", "trikon-cloud-spot-retry-dlq") {
      $url = aws sqs get-queue-url --region us-east-1 --queue-name $q --query QueueUrl --output text
      "$q " + (aws sqs get-queue-attributes --region us-east-1 --queue-url $url --attribute-names ApproximateNumberOfMessages --query Attributes.ApproximateNumberOfMessages --output text)
    }
    ```

Troubleshooting:

- **No Check Run appears.** Look at the `pull_request` delivery in Recent Deliveries (`202` means it was queued). Then read the logs in order: `aws logs tail /aws/lambda/trikon-cloud-webhook-receiver --region us-east-1 --since 30m`, the same for `/aws/lambda/trikon-cloud-orchestrator`, then `/aws/ecs/trikon-verify-runner`. A message in `trikon-verify-jobs-dlq` means the orchestrator failed the job three times.
- **"Connect your Trikon account"** (`not_connected`): connect the installation on the dashboard (step 6), then push a new commit to the pull request.
- **"Trikon Cloud verification unavailable"** about quota: `/trikon-cloud/usage-secret` differs from Render's `TRIKON_CLOUD_USAGE_SECRET`, or `https://api.trikon.unideploy.com` is unreachable. Check the Render server is up, then compare the two secrets.
- **The task fails to start.** A missing `trikon-verify-task-role-<installation-id>` role means the `installation` delivery never arrived: reinstall the App (step 3). An image pull error means `runner_image_uri` names an image that is not in ECR.
- **Messages in a DLQ.** Read the matching function's logs before you delete or redrive anything.

## Expected monthly cost

Approximate us-east-1 and Render list prices, assuming the account's AWS free tier is already used up (Unideploy may use it). Fargate on demand costs $0.04048 per vCPU-hour and $0.004445 per GB-hour, so a runner task (1 vCPU, 2 GB) costs about $0.049 per hour, and Spot about 30% of that ([AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)). Render Starter is about $7 per month ([Render pricing](https://render.com/pricing)). Recheck both pricing pages before launch.

| Item | Idle month | 500 PR runs × 4 min |
|---|---|---|
| Render `trikon-server` Starter | $7.00 | $7.00 |
| Render `trikon-client` static site, Neon free, Cloudflare DNS, Resend free | $0 | $0 |
| Fargate Spot (33 task-hours) | $0 | ≈ $0.50 (≈ $1.65 if all on demand) |
| Public IPv4 on runner tasks ($0.005 per hour) | $0 | ≈ $0.17 |
| Lambda arm64 ×5, API Gateway HTTP API, SQS | ≈ $0 | < $0.20 |
| DynamoDB on-demand ×5 tables (point-in-time recovery on one) | < $0.05 | < $0.15 |
| CloudWatch Logs (7-day retention) | ≈ $0 | < $0.25 |
| ECR private storage (at most 5 images, shared layers) | ≈ $0.10–0.50 | same |
| SSM Standard parameters, S3 evidence bucket | $0 | ≈ $0 |
| **Total** | **≈ $7.10–7.50** | **≈ $8–10** |

PayPal transaction fees are not included. There is no NAT gateway: runner tasks use public subnets with a public IP.

## Rollback

Run the account check from 3a step 2 before any rollback step, and redo [3a step 1](#3a-session-setup-and-pre-flight) in a new window. `cdk destroy` synthesizes the app first, so it needs the same required context as the deploy and a non-stale Lambda asset.

1. [user] Stop new jobs first: in the Trikon Verify App settings, turn the webhook **Active** off and save. GitHub stops delivering at once; jobs already queued still run.
2. [user] Image rollback: list the images, set `$image` to the previous tag, and rerun [3e steps 2 and 3](#3e-deploys) (diff, then deploy `FargateRunnerStack`). The lifecycle policy keeps the last 5 images.
   ```powershell
   aws ecr describe-images --region us-east-1 --repository-name trikon-cloud-runner `
     --query "sort_by(imageDetails, &imagePushedAt)[].[imageTags[0],imagePushedAt]" --output table
   $image = "818515814116.dkr.ecr.us-east-1.amazonaws.com/trikon-cloud-runner:<previous-tag>"
   ```
3. [user] Lambda code rollback: check out the previous good commit, rebuild the asset ([3d step 1](#3d-lambda-asset)), set the variables from 3e steps 1, 4 and 7 again, and rerun the diff and deploy of the affected stack from [3e](#3e-deploys).
4. [user] Turn the webhook back on (step 1 in reverse) once the rollback is deployed.
5. [user] Full teardown, only if you mean to remove Trikon Cloud: keep the webhook off, rebuild the asset from the current checkout ([3d step 1](#3d-lambda-asset)), set `$appId`, `$image`, `$subnets` and `$sg` as in 3e, then destroy the stacks in reverse deploy order, each by its explicit name. Answer the confirmation prompt each time; never pass `--force` or `--all`.
   ```powershell
   cdk destroy TrikonCloudOrchestratorStack `
     -c app_id=$appId `
     -c verify_jobs_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs `
     -c verify_jobs_dlq_arn=arn:aws:sqs:us-east-1:818515814116:trikon-verify-jobs-dlq `
     -c runner_subnet_ids=$subnets `
     -c runner_security_group_ids=$sg `
     -c trikon_web_api_url=https://api.trikon.unideploy.com `
     -c trikon_web_app_url=https://trikon.unideploy.com `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.orchestrator.infra.app"
   ```
   ```powershell
   cdk destroy WebhookReceiverStack `
     -c installation_events_queue_arn=arn:aws:sqs:us-east-1:818515814116:trikon-cloud-installation-events `
     -c lambda_asset_dir=build/lambda-asset `
     --app ".venv\Scripts\python.exe -m trikon_cloud.webhook_receiver.infra.app"
   ```
   ```powershell
   cdk destroy FargateRunnerStack `
     -c app_id=$appId `
     -c runner_image_uri=$image `
     --app ".venv\Scripts\python.exe -m trikon_cloud.fargate_runner.infra.app"
   ```

What survives a full teardown:

- the DynamoDB tables `trikon_installations`, `trikon_verdicts`, `trikon_pr_state` and `trikon-cloud-installations` (`RemovalPolicy.RETAIN`);
- the S3 bucket `trikon-cloud-evidence` (`RETAIN`);
- the log groups `/aws/ecs/trikon-verify-runner`, `/aws/lambda/trikon-cloud-webhook-receiver`, `/aws/lambda/trikon-cloud-orchestrator`, `/aws/lambda/trikon-cloud-installation-lifecycle`, `/aws/lambda/trikon-cloud-usage-reporter` and `/aws/lambda/trikon-cloud-spot-retry` (`RETAIN`; their logs still expire after 7 days);
- the per-installation roles `trikon-verify-task-role-<installation-id>`, which the installation lifecycle function creates at run time, outside CloudFormation;
- the three `/trikon-cloud/*` SSM parameters and the `trikon-cloud-runner` ECR repository, which no stack owns.

The SSM parameters and the ECR repository can stay: a re-create reuses them. The other leftovers keep their fixed names and make a re-create fail. Before re-creating a stack, delete the leftovers it owns, each by its explicit name. Tables and the bucket hold data; back up what you need first. Never touch the CDK bootstrap bucket or any resource outside this list.

| Leftover | Owning stack | Delete by explicit name |
|---|---|---|
| Table `trikon_installations`, `trikon_verdicts` or `trikon_pr_state` | `FargateRunnerStack` | `aws dynamodb create-backup --region us-east-1 --table-name <table> --backup-name <table>-before-recreate`, then `aws dynamodb delete-table --region us-east-1 --table-name <table>` |
| Bucket `trikon-cloud-evidence` (unversioned) | `FargateRunnerStack` | `aws s3 rb s3://trikon-cloud-evidence --force` (deletes the bucket's objects, then the bucket) |
| Log group `/aws/ecs/trikon-verify-runner` | `FargateRunnerStack` | `aws logs delete-log-group --region us-east-1 --log-group-name /aws/ecs/trikon-verify-runner` |
| Log group `/aws/lambda/trikon-cloud-webhook-receiver` | `WebhookReceiverStack` | `aws logs delete-log-group --region us-east-1 --log-group-name /aws/lambda/trikon-cloud-webhook-receiver` |
| Log groups `/aws/lambda/trikon-cloud-orchestrator`, `/aws/lambda/trikon-cloud-installation-lifecycle`, `/aws/lambda/trikon-cloud-usage-reporter`, `/aws/lambda/trikon-cloud-spot-retry` | `TrikonCloudOrchestratorStack` | `aws logs delete-log-group --region us-east-1 --log-group-name <one of these names>`, once per name |
| Table `trikon-cloud-installations` | `TrikonCloudOrchestratorStack` | back up, then `aws dynamodb delete-table --region us-east-1 --table-name trikon-cloud-installations` |
| Role `trikon-verify-task-role-<installation-id>` | none (created at run time) | `aws iam delete-role-policy --role-name trikon-verify-task-role-<installation-id> --policy-name TrikonInstallationPolicy`, then `aws iam delete-role --role-name trikon-verify-task-role-<installation-id>` |

## Deploy-blocker appendix

The go-live audit found these blockers. "Fixed" means this spec changed code or configuration; otherwise the named runbook step resolves it. "Web runbook" means [`trikon-web/DEPLOY.md`](../../trikon-web/DEPLOY.md).

| # | Blocker | Status | Resolved by |
|---|---|---|---|
| B1 | Server and client URLs came from `RENDER_EXTERNAL_URL` (`*.onrender.com`) | Fixed: literal production URLs and `domains:` in `render.yaml` (Req 1) | Web runbook Phase 2 steps 5 and 6 (CNAMEs and domain verification) |
| B2 | The free Render server sleeps after 15 minutes idle and takes about a minute to wake; the quota call budget is 10 seconds | Fixed: `plan: starter` (Req 2) | Web runbook Phase 2 step 3 (payment method) |
| B3 | The usage secret was said to be in "AWS Secrets Manager" | Fixed: the files name `/trikon-cloud/usage-secret` (Req 3) | 3b steps 4 and 5 |
| B4 | Runner image hard-coded to an old ECR Public image under an alias the project may not own | Fixed: required `runner_image_uri` context (Req 5) | 3c steps 2 to 7, 3e steps 2 and 3 |
| B5 | Lambda zips lacked the `trikon_cloud/` root, the receiver asset lacked `installation_lifecycle`, and no third-party packages were included | Fixed: Lambda asset builder and required `lambda_asset_dir` context (Req 6, 7) | 3d steps 1 and 2, 3f step 4 |
| B6 | `cdk deploy` printed no webhook URL | Fixed: `WebhookUrl` output (Req 8) | 3e step 7, Phase 4 step 3 |
| B7 | The cookie banner linked to `<your-url-here>`, and there was no privacy policy | Fixed: `/privacy` and `/terms` pages (Req 9) | Web runbook "Legal details" and "Legal review" |
| B8 | The webhook receiver Lambda was x86_64 and the others arm64, so one asset with native wheels could not serve both | Fixed: receiver on arm64 (Req 7.6) | None needed |
| B9 | `/trikon/verify-runner/active-revision` must exist before the orchestrator runs | No code change: `FargateRunnerStack` creates it on every deploy | 3f step 2 |
| B10 | The orchestrator needs the Trikon Web API and app base URLs | Runbook | 3e steps 8 and 9 (`-c trikon_web_api_url=https://api.trikon.unideploy.com -c trikon_web_app_url=https://trikon.unideploy.com`) |
| B11 | CDK bootstrap | Already done in the account; never re-bootstrap | 3a step 4 |
| B12 | Missing Wasp production env keys | None missing; a static test pins the list (Req 4) | Web runbook "Secrets table" |
| B13 | The global bucket name `trikon-cloud-evidence` may belong to another account | Runbook; a configurable name is a follow-up | 3a step 6 (stop on `403`) |
| B14 | Fixed-name resources collide with leftovers in the shared account | Runbook | 3a steps 5 and 7, [Rollback](#rollback) leftovers table |
| B15 | A GitHub App has one Setup URL; pointing it at production breaks local installs | Runbook: the production App gets the production URLs; local testing uses a separate development App | Web runbook "Trikon Verify GitHub App (web settings)" step 2; Phase 4 here configures the same production App |
| B16 | The runner base image `suryansh639/trikon:0.5.0` must be pullable | Runbook | 3a step 9 |

Known non-blocking issues:

- `REACT_APP_GOOGLE_ANALYTICS_ID` is unset. Accepting analytics cookies logs a caught console error, and nothing is sent.
- `trikon_cloud/orchestrator/never_fail_open.py` and `trikon_cloud/fargate_runner/entrypoint.py` hard-code `schema_version=2` for fail-closed verdicts. This is an engine follow-up.
- `landing-page/contentSections.tsx` in trikon-web still holds Open SaaS sample data. Only the unused `LandingPage.original.tsx` imports it.
