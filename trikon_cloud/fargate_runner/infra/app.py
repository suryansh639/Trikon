"""CDK app entry for the Trikon Cloud Fargate runner stack.

Invoke via ``cdk synth -c app_private_key_secret_arn=<arn> -c app_id=<id>``
or ``cdk deploy FargateRunnerStack -c app_private_key_secret_arn=<arn> -c app_id=<id>``
with ``--app "python -m trikon_cloud.fargate_runner.infra.app"``. The stack
references the Secrets Manager secret by ARN — the secret's material is
populated out-of-band before deploy (Requirement 19.5), so a missing or
empty ``app_private_key_secret_arn`` context value is a deliberate
loud-fail here rather than a silent default.

Region defaults to ``us-east-1`` per memo §3.8. Account is resolved from
``CDK_DEFAULT_ACCOUNT`` env var first, then CDK context (``-c account=<id>``)
as a fallback for CI runs where the AWS profile is not pre-loaded.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from trikon_cloud.fargate_runner.infra.fargate_runner_stack import FargateRunnerStack


def main() -> None:
    """Synthesize :class:`FargateRunnerStack` from CDK context.

    Resolves the ``account`` / ``region`` targeting for the stack and
    the two required context values (``app_private_key_secret_arn``
    and ``app_id``), instantiates the stack, then calls
    :meth:`aws_cdk.App.synth`. Missing required context raises
    :class:`ValueError` — the CDK CLI surfaces the message and exits
    non-zero, which is the intended fail-loud behavior for the deploy
    entrypoint.
    """
    app = cdk.App()

    account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context(
        "account"
    )
    region = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")

    app_private_key_secret_arn = app.node.try_get_context(
        "app_private_key_secret_arn"
    )
    if app_private_key_secret_arn is None:
        raise ValueError(
            "app_private_key_secret_arn must be provided via "
            "`-c app_private_key_secret_arn=<arn>` or in cdk.json context"
        )

    app_id_ctx = app.node.try_get_context("app_id")
    if app_id_ctx is None:
        raise ValueError(
            "app_id must be provided via `-c app_id=<id>` or in cdk.json context"
        )
    app_id = int(app_id_ctx)

    FargateRunnerStack(
        app,
        "FargateRunnerStack",
        env=cdk.Environment(account=account, region=region),
        app_private_key_secret_arn=app_private_key_secret_arn,
        app_id=app_id,
    )

    app.synth()


if __name__ == "__main__":
    main()
