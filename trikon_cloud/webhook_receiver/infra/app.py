"""CDK app entry for the Trikon Cloud webhook receiver stack.

Invoke via ``cdk synth`` or ``cdk deploy WebhookReceiverStack`` with
``--app "python -m trikon_cloud.webhook_receiver.infra.app"``,
``-c installation_events_queue_arn=<arn>`` and
``-c lambda_asset_dir=build/lambda-asset``.

The Lambda code comes from a Lambda_Asset directory built beforehand with
``python -m trikon_cloud.lambda_asset --out build/lambda-asset`` (see
:mod:`trikon_cloud.lambda_asset`). ``-c lambda_asset_dir=<dir>`` names it
(trikon-go-live Req 7.1). A missing or empty value, a directory without an
Asset_Manifest, or an asset built from older sources fails synthesis with a
message starting with ``lambda_asset_dir`` that names the builder command
(Req 7.2, 7.3).

The webhook secret lives in an SSM SecureString parameter that the
operator creates out-of-band before the first deploy (cost-cuts Req 3.5).
The stack references it by name: ``-c webhook_secret_parameter_name=<name>``
overrides the default ``/trikon-cloud/github-webhook-secret`` (Req 3.1,
3.2). An empty name or one that does not start with ``/`` fails synthesis
with a message naming the context key (Req 3.3). The obsolete
``webhook_secret_arn`` key also fails synthesis, so a stale deploy command
cannot silently fall back to the default parameter name.

Region defaults to ``us-east-1`` per memo §3.8. Account is resolved
from the standard CDK environment variables first, then the CDK
context (``-c account=<id>``) as a fallback for CI runs.

:func:`build_app` is pure: it makes no AWS calls (no lookups, no
credentials), so tests can synthesize the stack offline (Req 1.3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import aws_cdk as cdk

from trikon_cloud.lambda_asset import LAMBDA_ASSET_DIR_CONTEXT, validate_asset_dir
from trikon_cloud.secure_params import (
    WEBHOOK_SECRET_PARAMETER_CONTEXT,
    WEBHOOK_SECRET_PARAMETER_DEFAULT,
    parameter_name_from_context,
)
from trikon_cloud.webhook_receiver.infra.webhook_receiver_stack import (
    WebhookReceiverStack,
)

__all__ = ["OBSOLETE_WEBHOOK_SECRET_CONTEXT", "STACK_ID", "build_app", "main"]

STACK_ID = "WebhookReceiverStack"

# Removed in favour of ``webhook_secret_parameter_name`` (Secrets Manager cut).
OBSOLETE_WEBHOOK_SECRET_CONTEXT = "webhook_secret_arn"


def build_app(
    context: Mapping[str, object] | None = None, *, outdir: str | None = None
) -> cdk.App:
    """Build the CDK app and its ``WebhookReceiverStack`` from ``context``.

    ``context`` is passed to :class:`aws_cdk.App` as additional context;
    CLI / ``cdk.json`` context (``CDK_CONTEXT_JSON``) takes precedence, as
    CDK defines. No AWS calls are made. Raises :class:`ValueError` for a
    missing ``installation_events_queue_arn``, an invalid
    ``webhook_secret_parameter_name``, the obsolete ``webhook_secret_arn``,
    or a ``lambda_asset_dir`` that is missing, is not a Lambda_Asset or is
    stale (checked by :func:`trikon_cloud.lambda_asset.validate_asset_dir`).
    """
    app = cdk.App(context=dict(context or {}), outdir=outdir)

    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context(
        "account"
    )
    region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

    if app.node.try_get_context(OBSOLETE_WEBHOOK_SECRET_CONTEXT) is not None:
        raise ValueError(
            f"{OBSOLETE_WEBHOOK_SECRET_CONTEXT} is no longer supported: the "
            "webhook secret moved to SSM Parameter Store. Pass "
            f"`-c {WEBHOOK_SECRET_PARAMETER_CONTEXT}=<name>` instead, or omit it "
            f"to use {WEBHOOK_SECRET_PARAMETER_DEFAULT}"
        )

    webhook_secret_parameter_name = parameter_name_from_context(
        app.node.try_get_context(WEBHOOK_SECRET_PARAMETER_CONTEXT),
        key=WEBHOOK_SECRET_PARAMETER_CONTEXT,
        default=WEBHOOK_SECRET_PARAMETER_DEFAULT,
    )

    installation_events_queue_arn = app.node.try_get_context("installation_events_queue_arn")
    if not installation_events_queue_arn:
        raise ValueError(
            "installation_events_queue_arn must be provided via "
            "`-c installation_events_queue_arn=<arn>` or in cdk.json context"
        )

    lambda_asset_dir = validate_asset_dir(
        app.node.try_get_context(LAMBDA_ASSET_DIR_CONTEXT)
    )

    WebhookReceiverStack(
        app,
        STACK_ID,
        env=cdk.Environment(account=account, region=region),
        webhook_secret_parameter_name=webhook_secret_parameter_name,
        installation_events_queue_arn=installation_events_queue_arn,
        lambda_asset_dir=lambda_asset_dir,
    )
    return app


def main() -> None:
    """Synthesize the ``WebhookReceiverStack`` from CDK context."""
    build_app().synth()


if __name__ == "__main__":
    main()
