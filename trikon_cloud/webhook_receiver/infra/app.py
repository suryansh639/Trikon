"""CDK app entry for the Trikon Cloud webhook receiver stack.

Invoke via ``cdk synth -c webhook_secret_arn=<arn>`` or
``cdk deploy WebhookReceiverStack -c webhook_secret_arn=<arn>`` with
``--app "python -m trikon_cloud.webhook_receiver.infra.app"``. The
stack references the Secrets Manager secret **by ARN** — the secret's
material is populated out-of-band before deploy (Requirement 17.4),
so this app deliberately raises ``ValueError`` when the ARN context
value is absent rather than fabricating a placeholder.

Region defaults to ``us-east-1`` per memo §3.8. Account is resolved
from the standard CDK environment variables first, then the CDK
context (``-c account=<id>``) as a fallback for CI runs.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from trikon_cloud.webhook_receiver.infra.webhook_receiver_stack import (
    WebhookReceiverStack,
)


def main() -> None:
    """Synthesize the ``WebhookReceiverStack`` from CDK context."""
    app = cdk.App()

    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context(
        "account"
    )
    region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

    webhook_secret_arn = app.node.try_get_context("webhook_secret_arn")
    if webhook_secret_arn is None:
        raise ValueError(
            "webhook_secret_arn must be provided via "
            "`-c webhook_secret_arn=<arn>` or in cdk.json context"
        )

    installation_events_queue_arn = app.node.try_get_context("installation_events_queue_arn")
    if not installation_events_queue_arn:
        raise ValueError(
            "installation_events_queue_arn must be provided via "
            "`-c installation_events_queue_arn=<arn>` or in cdk.json context"
        )

    WebhookReceiverStack(
        app,
        "WebhookReceiverStack",
        env=cdk.Environment(account=account, region=region),
        webhook_secret_arn=webhook_secret_arn,
        installation_events_queue_arn=installation_events_queue_arn,
    )

    app.synth()


if __name__ == "__main__":
    main()
