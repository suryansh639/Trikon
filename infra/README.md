# Trikon Infrastructure (AWS CDK, Python)

Deferred to **v0.2**. The MVP runs entirely locally — no hosted backend.

This directory contains the CDK scaffold for the hosted control plane. It mirrors Unideploy's `infra/` layout deliberately so the AWS pattern is familiar and copy-paste-able (same account, same region conventions, same authorizer pattern).

## Planned stacks

| Stack | Purpose | Mirrors Unideploy |
| --- | --- | --- |
| `TrikonApiStack` | API Gateway + Lambda authorizer + core Lambdas + DynamoDB | `unideploy-api` + `unideploy-authorizer` |
| `TrikonAuditStack` | Hash-chained audit log + export queue | `unideploy-audit` |
| `TrikonBillingStack` | License keys + Razorpay/Stripe webhook | `mcp-payment-*` lambdas |
| `TrikonWorkerStack` | ECS Fargate runners for hosted verification | (new — no direct Unideploy equivalent) |

## Deploy

```bash
cd infra
pip install -r requirements.txt
cdk bootstrap        # once per account/region
cdk deploy TrikonApiStack
```

## Region

`us-east-1` to match Unideploy's backend and to keep IAM boundaries simple if the same account is used for both products.

## Not yet built

Everything in this directory is currently a stub. See `ARCHITECTURE.md` §6 for the reuse contract and §8 for what specifically is deferred to v0.2.
