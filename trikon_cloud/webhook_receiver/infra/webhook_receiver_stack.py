# CDK's constructors use ``**kwargs: Any`` in their generated jsii type
# stubs, and the repo's ``disallow_any_explicit = true`` mypy setting
# flags every stack ``__init__`` that forwards ``**kwargs: Any`` to
# ``super().__init__``. Suppress at file scope — the ``Any`` is CDK's,
# not ours.
# mypy: disable-error-code="explicit-any"
"""CDK stack for the Trikon Cloud GitHub webhook receiver.

Provisions a single AWS Lambda function fronted by an API Gateway
HTTP API, with its work-item drain queue (``trikon-verify-jobs``)
plus dead-letter queue (``trikon-verify-jobs-dlq``), a dedicated
CloudWatch log group with 7-day retention, and a minimum-viable IAM
policy: ``sqs:SendMessage`` on the two queues and ``ssm:GetParameter``
on exactly the Webhook_Secret_Parameter ARN. No DynamoDB, no S3, no
EC2, no VPC (Invariant 1 + memo §3.9).

The SSM SecureString parameter holding the webhook secret is
**referenced by name**, never created here (cost-cuts Req 3.5): the
operator creates it out-of-band before the first deploy. ``cdk
destroy`` is therefore safe: destroying the stack removes only the
Lambda, API Gateway, SQS queues and IAM role (the log group is
retained).

The Lambda code is a prebuilt Lambda_Asset directory (trikon-go-live
Req 7.4), and the stack outputs the GitHub App webhook URL as
``WebhookUrl`` (Req 8.1, 8.2).

Deploy commands, prerequisites, and rollback notes live in
``README.md`` alongside this module.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from trikon_cloud.secure_params import WEBHOOK_SECRET_PARAMETER_ENV, parameter_arn

__all__ = [
    "VERIFY_JOBS_MAX_RECEIVE_COUNT",
    "WEBHOOK_PATH",
    "WEBHOOK_SECRET_PARAMETER_READ_SID",
    "WEBHOOK_URL_OUTPUT_ID",
    "WebhookReceiverStack",
]

# Path of the single ``POST`` route. The route and the ``WebhookUrl``
# output share it, so the printed URL always matches the deployed route.
WEBHOOK_PATH = "/webhooks/github"

# Logical id of the CloudFormation output holding the webhook URL (Req 8.1).
WEBHOOK_URL_OUTPUT_ID = "WebhookUrl"

# Sid of the inline statement granting ``ssm:GetParameter`` on exactly the
# Webhook_Secret_Parameter ARN (cost-cuts Req 4.1).
WEBHOOK_SECRET_PARAMETER_READ_SID = "WebhookSecretParameterRead"

# ``maxReceiveCount`` of the ``trikon-verify-jobs`` redrive policy. The
# OrchestratorStack passes the same value to the orchestrator as
# ``TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT`` so a Retry_Job fails closed on
# its last receive instead of dead-lettering (cost-cuts Req 11.5).
VERIFY_JOBS_MAX_RECEIVE_COUNT = 3


class WebhookReceiverStack(Stack):
    """CloudFormation stack for the webhook-receiver Lambda + its ingress.

    Args:
        scope: Parent construct (typically the ``cdk.App`` from
            ``app.py``).
        id: Stack logical id (typically ``"WebhookReceiverStack"``).
        webhook_secret_parameter_name: Name (``/``-rooted) of the
            pre-existing SSM SecureString parameter holding the GitHub
            App webhook secret. The stack does NOT create the
            parameter; it passes the name to the Lambda as
            ``TRIKON_WEBHOOK_SECRET_PARAMETER_NAME`` and grants the
            Lambda role ``ssm:GetParameter`` on exactly its ARN.
        installation_events_queue_arn: Complete ARN of the pre-existing
            ``trikon-cloud-installation-events`` SQS queue owned by
            Spec 3's ``OrchestratorStack``. The stack does NOT create
            the queue; it imports it by ARN and grants the Lambda
            role ``sqs:SendMessage`` on it. A missing/empty value
            raises at synth time (fail-loud choice).
        lambda_asset_dir: Path of a Lambda_Asset directory written by
            ``python -m trikon_cloud.lambda_asset`` and already checked by
            :func:`trikon_cloud.lambda_asset.validate_asset_dir`. The
            function's code is packaged from it as-is.
        dlq_arn: Optional ARN of a pre-existing DLQ to reuse (e.g.,
            when redeploying against an environment that already owns
            the queue). When ``None`` (the default), the stack creates
            a fresh ``trikon-verify-jobs-dlq``.
        kwargs: Forwarded to :class:`aws_cdk.Stack`. Typically carries
            the ``env=cdk.Environment(account=..., region=...)`` pin
            supplied by ``app.py``.

    Public attributes:
        queue_url: URL of the main ``trikon-verify-jobs`` queue.
        api_endpoint: HTTPS endpoint of the HTTP API. The GitHub App
            webhook URL is this endpoint followed by :data:`WEBHOOK_PATH`,
            emitted as the ``WebhookUrl`` stack output.
        function_arn: ARN of the ``trikon-cloud-webhook-receiver``
            Lambda function.
    """

    queue_url: str
    api_endpoint: str
    function_arn: str

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        webhook_secret_parameter_name: str,
        installation_events_queue_arn: str,
        lambda_asset_dir: str,
        dlq_arn: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # (1) CloudWatch log group — 7-day retention (cost-cuts Req 2.2),
        # RETAIN so that logs outlive a stack teardown (post-mortem support).
        log_group = logs.LogGroup(
            self,
            "WebhookReceiverLogs",
            log_group_name="/aws/lambda/trikon-cloud-webhook-receiver",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # (2) Dead-letter queue — either import an existing DLQ by ARN
        # or create a fresh one with 14-day message retention.
        dlq: sqs.IQueue
        if dlq_arn is not None:
            dlq = sqs.Queue.from_queue_arn(self, "ImportedDlq", dlq_arn)
        else:
            dlq = sqs.Queue(
                self,
                "VerifyJobsDlq",
                queue_name="trikon-verify-jobs-dlq",
                retention_period=Duration.days(14),
            )

        # (3) Main queue — standard queue (memo §3.3), 15-minute
        # visibility timeout aligning with the runner's max work-item
        # duration, VERIFY_JOBS_MAX_RECEIVE_COUNT (3) receives before
        # failover to the DLQ.
        queue = sqs.Queue(
            self,
            "VerifyJobsQueue",
            queue_name="trikon-verify-jobs",
            visibility_timeout=Duration.minutes(15),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=VERIFY_JOBS_MAX_RECEIVE_COUNT, queue=dlq
            ),
        )

        # (3a) Installation-events queue — owned by Spec 3's
        # ``OrchestratorStack`` and imported here by ARN. We do NOT
        # create the queue construct here (Requirement 5.2). The
        # returned ``IQueue`` is sufficient for both
        # ``grant_send_messages`` and ``.queue_url`` access; the URL
        # resolves at deploy time via CloudFormation's ``Fn::GetAtt``.
        installation_events_queue = sqs.Queue.from_queue_arn(
            self,
            "InstallationEventsQueue",
            installation_events_queue_arn,
        )

        # (4) Lambda function — Python 3.11 on arm64, 512 MB, 10-second
        # timeout (matches GitHub's webhook timeout — API Gateway hard-
        # limits at 30 s but we want to fail fast so GitHub sees a
        # useful 5xx rather than a timeout).
        #
        # ``Code.from_asset`` packages the prebuilt Lambda_Asset directory
        # as-is (no Docker bundling). Build it with
        # ``python -m trikon_cloud.lambda_asset --out build/lambda-asset``
        # (see :mod:`trikon_cloud.lambda_asset`): it holds the
        # ``trikon_cloud`` sources plus the arm64 CPython 3.11 wheels of
        # ``lambda-requirements.txt``, which is why the function runs on
        # arm64 (trikon-go-live Req 7.6).
        function = lambda_.Function(
            self,
            "WebhookReceiverFunction",
            function_name="trikon-cloud-webhook-receiver",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="trikon_cloud.webhook_receiver.handler.handler",
            code=lambda_.Code.from_asset(lambda_asset_dir),
            memory_size=512,
            timeout=Duration.seconds(10),
            architecture=lambda_.Architecture.ARM_64,
            environment={
                WEBHOOK_SECRET_PARAMETER_ENV: webhook_secret_parameter_name,
                "TRIKON_VERIFY_JOBS_QUEUE_URL": queue.queue_url,
                "TRIKON_INSTALLATION_EVENTS_QUEUE_URL": installation_events_queue.queue_url,
                "TRIKON_LOG_LEVEL": "INFO",
            },
            log_group=log_group,
        )

        # (5) IAM — narrow grants only. ``grant_send_messages`` emits
        # ``sqs:SendMessage`` (plus the queue-attribute reads CDK pairs
        # with it) on each queue ARN. The webhook secret is read through
        # one inline statement: ``ssm:GetParameter`` on exactly the
        # Parameter_ARN (cost-cuts Req 4.1). No ``ssm`` ``grant_read``
        # helper, which would add DescribeParameters / GetParameters /
        # GetParameterHistory, and no ``kms:Decrypt``: the AWS-managed
        # ``aws/ssm`` key lets account principals decrypt through SSM.
        # No ``dynamodb:*``, no ``s3:*``, no ``ec2:*`` — Invariant 1
        # (Requirement 11.1).
        queue.grant_send_messages(function)
        installation_events_queue.grant_send_messages(function)
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid=WEBHOOK_SECRET_PARAMETER_READ_SID,
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[
                    parameter_arn(
                        region=self.region,
                        account=self.account,
                        name=webhook_secret_parameter_name,
                    )
                ],
            )
        )

        # (6) API Gateway HTTP API — regional endpoint (default), no
        # custom domain at MVP. IAM auth would be inappropriate for a
        # public GitHub webhook target; the Lambda itself verifies
        # HMAC-SHA256 on the request body (design.md §2 boundary A).
        http_api = apigw.HttpApi(
            self,
            "WebhookApi",
            api_name="trikon-cloud-webhook-receiver-api",
        )

        # (7) Route — a single ``POST /webhooks/github`` (WEBHOOK_PATH)
        # proxying to the Lambda via the powertools resolver.
        http_api.add_routes(
            path=WEBHOOK_PATH,
            methods=[apigw.HttpMethod.POST],
            integration=apigw_int.HttpLambdaIntegration(
                "WebhookLambdaIntegration", function
            ),
        )

        # (8) Webhook URL output, printed by ``cdk deploy`` so the operator
        # can paste it into the GitHub App settings (Req 8.1, 8.2).
        CfnOutput(
            self,
            WEBHOOK_URL_OUTPUT_ID,
            value=f"{http_api.api_endpoint}{WEBHOOK_PATH}",
            description="GitHub App webhook URL (paste into the Trikon Verify App settings)",
        )

        # (9) Public attributes for the app entry / release engineer.
        self.queue_url = queue.queue_url
        self.api_endpoint = http_api.api_endpoint
        self.function_arn = function.function_arn
