"""API Gateway + core Lambdas + DynamoDB tables for the Trikon control plane.

STATUS: v0.2 stub. Nothing here is wired.

Mirrors demounideploy-main/infra/stacks/api_stack.py so the same AWS pattern
(REST API Gateway + custom authorizer + DynamoDB + Cognito) that Unideploy
already runs in production is reused deliberately.

Resources planned:
    DynamoDB:
        trikon-api-keys    (PK: api_key_hash)
        trikon-verdicts    (PK: verdict_id;   GSI: repo-index)
        trikon-policies    (PK: org_id;       SK: policy_id)
        trikon-audit-log   (PK: org_id;       SK: entry_hash)   # hash-chained

    Lambda:
        trikon-authorizer      # copy of unideploy-authorizer pattern
        trikon-verdicts        # POST/GET /verdicts
        trikon-policies        # POST/GET/DELETE /policies
        trikon-audit           # GET /audit/events
        trikon-billing-verify  # Stripe/Razorpay webhook

    API Gateway (REST):
        POST /verdicts
        GET  /verdicts?repo=...
        POST /policies
        GET  /policies/{id}
        GET  /audit/events
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct


class TrikonApiStack(cdk.Stack):
    """Control-plane stack. Currently empty — see module docstring for resources."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # TODO (v0.2): wire up DynamoDB tables, Lambdas, API Gateway, authorizer.
