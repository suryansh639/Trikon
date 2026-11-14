"""CDK app entry for the Trikon Cloud Fargate runner stack.

Invoke via ``cdk synth -c app_id=<id>`` or
``cdk deploy FargateRunnerStack -c app_id=<id>`` with
``--app "python -m trikon_cloud.fargate_runner.infra.app"``.

The GitHub App private key lives in an SSM SecureString parameter that the
operator creates out-of-band before the first deploy (Requirement 19.5;
cost-cuts Req 3.5). The stack references it by name:
``-c app_private_key_parameter_name=<name>`` overrides the default
``/trikon-cloud/github-app-private-key`` (cost-cuts Req 3.1, 3.2). An empty
name or one that does not start with ``/`` fails synthesis with a message
naming the context key (Req 3.3). The obsolete
``app_private_key_secret_arn`` key also fails synthesis, so a stale deploy
command cannot silently fall back to the default parameter name.

Region defaults to ``us-east-1`` per memo §3.8. Account is resolved from
``CDK_DEFAULT_ACCOUNT`` env var first, then CDK context (``-c account=<id>``)
as a fallback for CI runs where the AWS profile is not pre-loaded.

:func:`build_app` is pure: it makes no AWS calls (no lookups, no
credentials), so tests can synthesize the stack offline (Req 1.3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import aws_cdk as cdk

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack
from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_CONTEXT,
    APP_KEY_PARAMETER_DEFAULT,
    parameter_name_from_context,
)

__all__ = ["OBSOLETE_APP_KEY_CONTEXT", "STACK_ID", "build_app", "main"]

STACK_ID = "FargateRunnerStack"

# Removed in favour of ``app_private_key_parameter_name`` (Secrets Manager cut).
OBSOLETE_APP_KEY_CONTEXT = "app_private_key_secret_arn"


def build_app(
    context: Mapping[str, object] | None = None, *, outdir: str | None = None
) -> cdk.App:
    """Build the CDK app and its :class:`FargateRunnerStack` from ``context``.

    ``context`` is passed to :class:`aws_cdk.App` as additional context;
    CLI / ``cdk.json`` context (``CDK_CONTEXT_JSON``) takes precedence, as
    CDK defines. No AWS calls are made. Raises :class:`ValueError` for a
    missing ``app_id``, an invalid ``app_private_key_parameter_name`` or the
    obsolete ``app_private_key_secret_arn`` — the CDK CLI surfaces the
    message and exits non-zero.
    """
    app = cdk.App(context=dict(context or {}), outdir=outdir)

    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context(
        "account"
    )
    region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

    if app.node.try_get_context(OBSOLETE_APP_KEY_CONTEXT) is not None:
        raise ValueError(
            f"{OBSOLETE_APP_KEY_CONTEXT} is no longer supported: "
            "the GitHub App private key moved to SSM Parameter Store. Pass "
            f"`-c {APP_KEY_PARAMETER_CONTEXT}=<name>` instead, or omit it to use "
            f"{APP_KEY_PARAMETER_DEFAULT}"
        )

    app_private_key_parameter_name = parameter_name_from_context(
        app.node.try_get_context(APP_KEY_PARAMETER_CONTEXT),
        key=APP_KEY_PARAMETER_CONTEXT,
        default=APP_KEY_PARAMETER_DEFAULT,
    )

    app_id_ctx = app.node.try_get_context("app_id")
    if app_id_ctx is None:
        raise ValueError(
            "app_id must be provided via `-c app_id=<id>` or in cdk.json context"
        )
    app_id = int(app_id_ctx)

    FargateRunnerStack(
        app,
        STACK_ID,
        env=cdk.Environment(account=account, region=region),
        app_private_key_parameter_name=app_private_key_parameter_name,
        app_id=app_id,
    )
    return app


def main() -> None:
    """Synthesize :class:`FargateRunnerStack` from CDK context."""
    build_app().synth()


if __name__ == "__main__":
    main()
