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

## Deploy

```bash
cdk deploy WebhookReceiverStack
```

This is a manual release-engineer gate — not part of implementation tasks.
