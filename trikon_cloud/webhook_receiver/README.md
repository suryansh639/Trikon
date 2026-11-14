# Trikon Cloud — Webhook Receiver

Spec 1 of the Trikon Cloud M1 milestone: the GitHub webhook receiver. This package ships a Python 3.11 AWS Lambda function fronted by an API Gateway HTTP API that verifies HMAC-SHA256 signatures on `POST /webhooks/github`, routes on `X-GitHub-Event`, and enqueues verification jobs on the `trikon-verify-jobs` SQS queue for the downstream runner and orchestrator to consume. Grounding architecture memo: [`.kiro/specs/trikon-cloud-architecture/`](../../.kiro/specs/trikon-cloud-architecture/). Detailed spec for this component: [`.kiro/specs/trikon-cloud-webhook-receiver/`](../../.kiro/specs/trikon-cloud-webhook-receiver/).

## Local development

Bootstrap:

```bash
uv sync --extra cloud
```

Test:

```bash
uv run pytest trikon_cloud/webhook_receiver/tests/ -v
```

Type-check:

```bash
uv run mypy --strict trikon_cloud/webhook_receiver/
```

Lint:

```bash
uv run ruff check trikon_cloud/webhook_receiver/
```

## Webhook secret

The receiver reads the GitHub webhook secret from the SSM Parameter Store `SecureString` parameter named by `TRIKON_WEBHOOK_SECRET_PARAMETER_NAME` (`ssm:GetParameter` with `WithDecryption=True`, cached per warm container). The default name is `/trikon-cloud/github-webhook-secret`; override it with `-c webhook_secret_parameter_name=/custom/name`. The stack never creates the parameter. The operator creates it (standard tier, AWS-managed `aws/ssm` key) before the first deploy:

```bash
aws ssm put-parameter --region us-east-1 --name /trikon-cloud/github-webhook-secret \
  --type SecureString --tier Standard --value '<webhook-secret>'
```

The obsolete `webhook_secret_arn` context key fails synthesis.

## Deploy

Deploy Trikon Cloud only once hosted PR checks are wanted. The deploy order is `FargateRunnerStack`, then `WebhookReceiverStack`, then `TrikonCloudOrchestratorStack`.

```bash
cdk deploy WebhookReceiverStack \
  -c installation_events_queue_arn=arn:aws:sqs:us-east-1:<account-id>:trikon-cloud-installation-events \
  --app "python -m trikon_cloud.webhook_receiver.infra.app"
```

This is a manual release-engineer gate — not part of implementation tasks. The full runbook (prerequisites, parameters, 7-day log retention, rollback) is [`infra/README.md`](infra/README.md).
