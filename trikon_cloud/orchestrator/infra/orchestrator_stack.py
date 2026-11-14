# CDK's jsii-generated stubs type ``**kwargs`` as ``Any`` on every
# construct ``__init__``. Under the repo's ``disallow_any_explicit = true``
# mypy config, forwarding ``**kwargs: Any`` to ``super().__init__`` — plus
# every ``iam.PolicyStatement(conditions=...)`` mapping value — surfaces
# as an ``explicit-any`` error the construct authors, not us, control.
# Suppress at file scope; every ``Any`` in this module is CDK's, matching
# the pattern in :mod:`trikon_cloud.webhook_receiver.infra.webhook_receiver_stack`
# and :mod:`trikon_cloud.fargate_runner.infra.fargate_runner_stack`.
# mypy: disable-error-code="explicit-any"
"""CDK stack for the Trikon Cloud orchestrator + installation-lifecycle Lambdas.

Provisions everything Spec 3 owns (design.md §8): the two Python 3.11 /
arm64 Lambdas (``trikon-cloud-orchestrator`` at reserved-concurrency 10
and ``trikon-cloud-installation-lifecycle`` at reserved-concurrency 5),
their SQS event-source mappings (batch size 1, report-batch-item-failures
enabled), the new ``trikon-cloud-installation-events`` queue plus its
``trikon-cloud-installation-events-dlq`` DLQ, and the
``trikon-cloud-installations`` DynamoDB table (PAY_PER_REQUEST, point-in-
time recovery on, RETAIN on stack teardown).

References existing (Spec 1-owned) resources by ARN — ``trikon-verify-jobs``
and ``trikon-verify-jobs-dlq`` — plus the SSM parameter
``/trikon/verify-runner/active-revision`` (populated out-of-band by
Spec 2 per design.md §9.2). The two SSM SecureString parameters (the
GitHub App private key and the Usage_Secret) are likewise referenced by
name and never created here (cost-cuts Req 3.5): each role that reads
one gets an inline ``ssm:GetParameter`` statement on exactly its
Parameter_ARN (cost-cuts Req 4.2, 4.3), never a ``grant_read`` helper.

Each Lambda logs to an explicit ``/aws/lambda/<function_name>`` group
with 7-day retention (cost-cuts Req 2.3, 2.4), created by
:func:`_function_log_group` and wired through ``log_group=``.

IAM policy shapes are pinned by Requirements 8.2-8.6 (orchestrator) and
15.4-15.5 (lifecycle) — the eight orchestrator grants live in
:func:`_grant_orchestrator_permissions` and the four lifecycle grants
in :func:`_grant_lifecycle_permissions`, matching design.md §8.2/§8.3
one-for-one. Requirement 15.5 is enforced by absence: the lifecycle
role has NO ``iam:PassRole`` grant.

The sandbox-minute-plans spec (design.md §11) adds quota enforcement and
usage metering: the orchestrator gains the Trikon_Web Usage_API env vars,
a 90 s timeout, ``sqs:SendMessage`` on its own source queue (busy
re-enqueue, Req 11.18-11.19) and read access to the Usage_Secret
(Req 11.16, now part of ``SecureParameterRead``). A new
``trikon-cloud-usage-reporter`` Lambda is invoked by the
``trikon-verify-runner-task-stopped`` EventBridge rule on runner tasks
reaching ``STOPPED`` (Req 12.1), with failed events landing in
``trikon-cloud-usage-reporter-dlq`` (Req 12.8-12.9).

The trikon-cloud-cost-cuts spec (design.md §4-5) adds Fargate Spot: the
orchestrator gets the Spot_Flag (``TRIKON_USE_FARGATE_SPOT``), the
``trikon_pr_state`` table name and read grant, and the verify-jobs
``maxReceiveCount``. A new ``trikon-cloud-spot-retry`` Lambda is the second
target of the task-stopped rule; it re-queues a Spot-interrupted Attempt 1
once (``sqs:SendMessage`` on ``trikon-verify-jobs``, Req 9.7), dedupes per
task ARN in the ``trikon-cloud-spot-retries`` table, and fails closed
otherwise. Failed events land in ``trikon-cloud-spot-retry-dlq``.

Region is fixed at ``us-east-1`` (Requirement 16.1) via the ``env`` on
the enclosing ``cdk.App`` (see ``app.py``, task 14). Deploy commands,
prerequisites, and rollback notes live in ``README.md`` alongside this
module (task 15).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as lambda_events
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from trikon_cloud.secure_params import (
    APP_KEY_PARAMETER_ENV,
    USAGE_SECRET_PARAMETER_ENV,
    parameter_arn,
)
from trikon_cloud.webhook_receiver.infra.webhook_receiver_stack import (
    VERIFY_JOBS_MAX_RECEIVE_COUNT,
)

__all__ = [
    "ORCHESTRATOR_PR_STATE_READ_SID",
    "SECURE_PARAMETER_READ_SID",
    "SPOT_RETRY_APP_KEY_PARAMETER_READ_SID",
    "SPOT_RETRY_CLAIMS_SID",
    "SPOT_RETRY_PR_STATE_READ_SID",
    "SPOT_RETRY_SEND_RETRY_JOB_SID",
    "SPOT_RETRY_VERDICT_WRITE_SID",
    "USAGE_SECRET_PARAMETER_READ_SID",
    "OrchestratorStack",
]

# Sids of the inline ``ssm:GetParameter`` statements on Secure_Parameters
# (cost-cuts Req 4.2, 4.3). The orchestrator role reads the App key and the
# Usage_Secret; the Usage_Reporter role reads only the Usage_Secret.
SECURE_PARAMETER_READ_SID = "SecureParameterRead"
USAGE_SECRET_PARAMETER_READ_SID = "UsageSecretParameterRead"

# The orchestrator reads the Interim_Check_Run id from ``trikon_pr_state`` on
# the Retry_Job dispatch-failure path (cost-cuts Req 11.5).
ORCHESTRATOR_PR_STATE_READ_SID = "OrchestratorPrStateRead"

# Sids of the Spot_Retry_Handler role statements (cost-cuts design §5).
SPOT_RETRY_APP_KEY_PARAMETER_READ_SID = "SpotRetryAppKeyParameterRead"
SPOT_RETRY_SEND_RETRY_JOB_SID = "SpotRetrySendRetryJob"
SPOT_RETRY_VERDICT_WRITE_SID = "SpotRetryVerdictWrite"
SPOT_RETRY_PR_STATE_READ_SID = "SpotRetryPrStateRead"
SPOT_RETRY_CLAIMS_SID = "SpotRetryClaims"

# Fleet-wide table names shared by the orchestrator and the Spot_Retry_Handler.
_VERDICTS_TABLE = "trikon_verdicts"
_PR_STATE_TABLE = "trikon_pr_state"
_SPOT_RETRIES_TABLE = "trikon-cloud-spot-retries"
_CHECK_RUN_DETAILS_URL_TEMPLATE = (
    "https://api.trikon.unideploy.com/audits/{delivery_id}"
)


class OrchestratorStack(Stack):
    """CloudFormation stack for Spec 3 — orchestrator + lifecycle Lambdas.

    Provisions two Python 3.11 / arm64 Lambdas, one new SQS queue + DLQ
    (installation-events), and one new DynamoDB table (installations).
    References Spec 1's ``trikon-verify-jobs`` queue + DLQ by ARN, and
    Spec 2's ``/trikon/verify-runner/active-revision`` SSM parameter by
    name. Does NOT create the App-key or Usage_Secret SSM SecureString
    parameters — the release engineer creates them out-of-band before
    the first deploy (Requirement 16.4; cost-cuts Req 3.5).

    Args:
        scope: Parent construct (typically the ``cdk.App`` from
            ``app.py``).
        construct_id: Stack logical id (typically
            ``"TrikonCloudOrchestratorStack"`` — design.md §8.5).
        app_id: GitHub App numeric ID; baked into the orchestrator's
            ``TRIKON_APP_ID`` env var so the GitHub JWT minter in
            :mod:`trikon_cloud.orchestrator.never_fail_open` can produce
            valid Check Run tokens.
        app_private_key_parameter_name: Name (``/``-rooted) of the
            pre-existing SSM SecureString parameter holding the GitHub App
            private key. Passed as ``TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME``
            to the orchestrator and the installation-lifecycle Lambda
            (which bakes it into per-installation task-role policies).
            Only the orchestrator role may read it (``SecureParameterRead``).
        verify_jobs_queue_arn: ARN of Spec 1's ``trikon-verify-jobs``
            queue — the orchestrator's SQS event source (Requirement
            1.1). Passed by ARN so this stack has no cross-stack CFN
            dependency on Spec 1.
        verify_jobs_dlq_arn: ARN of Spec 1's ``trikon-verify-jobs-dlq``
            queue. The orchestrator role gets ``sqs:SendMessage`` on
            this ARN (Requirement 8.5).
        runner_subnet_ids: List of VPC subnet IDs the Fargate runner
            attaches to. Serialized comma-joined into the
            ``TRIKON_RUNNER_SUBNET_IDS`` env var.
        runner_security_group_ids: List of VPC security-group IDs.
            Serialized comma-joined into the
            ``TRIKON_RUNNER_SECURITY_GROUP_IDS`` env var.
        verify_runner_active_revision_ssm_param: SSM parameter name
            holding the active task-definition revision (populated by
            Spec 2's amendment per design.md §9.2). Read by the
            orchestrator via :class:`TaskDefinitionResolver`.
        trikon_web_api_url: Trikon_Web Usage_API base URL (https). Set
            as ``TRIKON_WEB_API_URL`` on the orchestrator and the
            Usage_Reporter (Req 11.14).
        trikon_web_app_url: Trikon_Web_App_URL (https) used for the
            Connect/Upgrade links in Fail_Closed_Results. Set as
            ``TRIKON_WEB_APP_URL`` on the orchestrator (Req 11.14).
        usage_secret_parameter_name: Name (``/``-rooted) of the
            pre-existing SSM SecureString parameter holding the
            Usage_Secret. Passed as
            ``TRIKON_CLOUD_USAGE_SECRET_PARAMETER_NAME`` to the orchestrator
            and the Usage_Reporter; both roles get ``ssm:GetParameter`` on
            exactly its Parameter_ARN (Req 11.16; cost-cuts Req 4.2, 4.3).
        lambda_asset_dir: Path of a Lambda_Asset directory written by
            ``python -m trikon_cloud.lambda_asset`` and already checked by
            :func:`trikon_cloud.lambda_asset.validate_asset_dir`. Every
            Lambda's code is packaged from it as-is (trikon-go-live
            Req 7.4).
        use_fargate_spot: Spot_Flag (cost-cuts Req 8.2). Set on the
            orchestrator as ``TRIKON_USE_FARGATE_SPOT`` (``"true"`` or
            ``"false"``); when on, Attempt 1 runs on ``FARGATE_SPOT``.
        kwargs: Forwarded to :class:`aws_cdk.Stack`. Typically carries
            the ``env=cdk.Environment(account=..., region="us-east-1")``
            pin supplied by ``app.py``.

    Public attributes:
        orchestrator_function_arn: ARN of the
            ``trikon-cloud-orchestrator`` Lambda.
        lifecycle_function_arn: ARN of the
            ``trikon-cloud-installation-lifecycle`` Lambda.
        installation_events_queue_url: URL of the
            ``trikon-cloud-installation-events`` queue — Spec 1's
            webhook receiver amendment writes here.
        installations_table_name: Name of the
            ``trikon-cloud-installations`` DynamoDB table.
        usage_reporter_function_arn: ARN of the
            ``trikon-cloud-usage-reporter`` Lambda.
        usage_reporter_dlq_url: URL of the
            ``trikon-cloud-usage-reporter-dlq`` queue.
        spot_retry_function_arn: ARN of the ``trikon-cloud-spot-retry``
            Lambda.
        spot_retry_dlq_url: URL of the ``trikon-cloud-spot-retry-dlq``
            queue.
        spot_retries_table_name: Name of the
            ``trikon-cloud-spot-retries`` DynamoDB table.
    """

    orchestrator_function_arn: str
    lifecycle_function_arn: str
    installation_events_queue_url: str
    installations_table_name: str
    usage_reporter_function_arn: str
    usage_reporter_dlq_url: str
    spot_retry_function_arn: str
    spot_retry_dlq_url: str
    spot_retries_table_name: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_id: int,
        app_private_key_parameter_name: str,
        verify_jobs_queue_arn: str,
        verify_jobs_dlq_arn: str,
        runner_subnet_ids: list[str],
        runner_security_group_ids: list[str],
        trikon_web_api_url: str,
        trikon_web_app_url: str,
        usage_secret_parameter_name: str,
        lambda_asset_dir: str,
        verify_runner_active_revision_ssm_param: str = "/trikon/verify-runner/active-revision",
        use_fargate_spot: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------------------------------------------------
        # 1. Reference existing (Spec 1-owned) SQS resources by ARN.
        # design.md §8.1 imports these via ``from_queue_arn`` so this
        # stack has no CFN cross-stack dependency on Spec 1 — the ARNs
        # are passed in as constructor kwargs from ``app.py``.
        # -------------------------------------------------------------
        verify_jobs_queue = sqs.Queue.from_queue_arn(
            self,
            "VerifyJobsQueue",
            queue_arn=verify_jobs_queue_arn,
        )
        verify_jobs_dlq = sqs.Queue.from_queue_arn(
            self,
            "VerifyJobsDlq",
            queue_arn=verify_jobs_dlq_arn,
        )

        # -------------------------------------------------------------
        # 2. Reference the SSM active-revision parameter written by
        # Spec 2 (design.md §9.2). ``grant_read`` on this handle emits
        # a scoped ``ssm:GetParameter`` policy — see Grant 5 below.
        # -------------------------------------------------------------
        active_revision_param = ssm.StringParameter.from_string_parameter_name(
            self,
            "VerifyRunnerActiveRevisionParam",
            string_parameter_name=verify_runner_active_revision_ssm_param,
        )

        # -------------------------------------------------------------
        # 3. New (this-spec-owned) SQS queue + DLQ for installation
        # lifecycle events. 14-day retention on the DLQ (Requirement
        # 10.3); redrive after ``maxReceiveCount=3`` (Requirement 10.2);
        # 2-minute visibility timeout matches the Lifecycle Lambda's
        # 30-second timeout plus a 4x safety margin per the SQS long-
        # poll best practice.
        # -------------------------------------------------------------
        lifecycle_dlq = sqs.Queue(
            self,
            "InstallationEventsDlq",
            queue_name="trikon-cloud-installation-events-dlq",
            retention_period=Duration.days(14),
        )
        lifecycle_queue = sqs.Queue(
            self,
            "InstallationEventsQueue",
            queue_name="trikon-cloud-installation-events",
            visibility_timeout=Duration.minutes(2),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=lifecycle_dlq,
                max_receive_count=3,
            ),
        )

        # -------------------------------------------------------------
        # 4. New DynamoDB table for installation state. TableV2 +
        # ``Billing.on_demand()`` (PAY_PER_REQUEST) + PITR on +
        # ``RemovalPolicy.RETAIN`` so ``cdk destroy`` never drops
        # tenant state (Requirement 14.1).
        # -------------------------------------------------------------
        installations_table = dynamodb.TableV2(
            self,
            "InstallationsTable",
            table_name="trikon-cloud-installations",
            partition_key=dynamodb.Attribute(
                name="installation_id",
                type=dynamodb.AttributeType.NUMBER,
            ),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -------------------------------------------------------------
        # 5. Shared deployment asset. Every Lambda in this stack ships the
        # same prebuilt Lambda_Asset directory, packaged as-is by
        # ``Code.from_asset`` (no Docker bundling), matching
        # :mod:`trikon_cloud.webhook_receiver.infra.webhook_receiver_stack`.
        # Build it with ``python -m trikon_cloud.lambda_asset --out
        # build/lambda-asset`` (see :mod:`trikon_cloud.lambda_asset`): it
        # holds the ``trikon_cloud`` sources plus the arm64 CPython 3.11
        # wheels of ``lambda-requirements.txt``.
        # -------------------------------------------------------------
        lambda_code = _lambda.Code.from_asset(lambda_asset_dir)

        # -------------------------------------------------------------
        # 6. Parameter_ARNs of the two SSM SecureString parameters. The
        # App key lets the orchestrator mint installation-scoped GitHub
        # JWTs for the Never-Fail-Open Check Run POST (design §7.3); the
        # Usage_Secret signs Usage_API requests. Neither is created here
        # (cost-cuts Req 3.5). No ``kms:Decrypt`` grant is needed: the
        # AWS-managed ``aws/ssm`` key lets account principals decrypt
        # through SSM.
        # -------------------------------------------------------------
        app_private_key_parameter_arn = parameter_arn(
            region=self.region,
            account=self.account,
            name=app_private_key_parameter_name,
        )
        usage_secret_parameter_arn = parameter_arn(
            region=self.region,
            account=self.account,
            name=usage_secret_parameter_name,
        )

        # -------------------------------------------------------------
        # 7. Orchestrator role — Lambda basic-execution + the eight
        # scoped grants from design.md §8.2. Role name is stable so
        # log-based debugging can reference it directly.
        # -------------------------------------------------------------
        orchestrator_role = iam.Role(
            self,
            "OrchestratorRole",
            role_name="trikon-cloud-orchestrator-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        _grant_orchestrator_permissions(
            orchestrator_role,
            verify_jobs_queue=verify_jobs_queue,
            verify_jobs_dlq=verify_jobs_dlq,
            active_revision_param=active_revision_param,
            app_private_key_parameter_arn=app_private_key_parameter_arn,
            usage_secret_parameter_arn=usage_secret_parameter_arn,
            account_id=self.account,
        )

        # -------------------------------------------------------------
        # 8. Orchestrator Lambda. Python 3.11 / arm64 / 512 MB /
        # reserved concurrency 10 per Requirement 8.1. The timeout is
        # 90 s (sandbox-minute-plans design.md §11): quota 10 s +
        # RunTask + 0-minute release 30 s + Check Run 30 s, plus one
        # SendMessage on the busy path; still well under the queue's
        # 15-minute visibility timeout. Env vars mirror
        # :class:`OrchestratorEnvConfig`'s CDK-populated aliases
        # (``AWS_REGION`` is runtime-injected by Lambda, so we don't
        # set it here — the value falls through to
        # :class:`OrchestratorEnvConfig`'s default of ``"us-east-1"``).
        # ``TRIKON_QUOTA_TIME_BUDGET_SECONDS`` and
        # ``TRIKON_QUEUE_WAIT_LIMIT_SECONDS`` are deliberately left
        # unset so their model defaults apply.
        # -------------------------------------------------------------
        orchestrator_fn = _lambda.Function(
            self,
            "OrchestratorFn",
            function_name="trikon-cloud-orchestrator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(90),
            reserved_concurrent_executions=10,
            handler="trikon_cloud.orchestrator.handler.lambda_handler",
            code=lambda_code,
            role=orchestrator_role,
            environment={
                "TRIKON_AWS_ACCOUNT_ID": self.account,
                "TRIKON_RUNNER_SUBNET_IDS": ",".join(runner_subnet_ids),
                "TRIKON_RUNNER_SECURITY_GROUP_IDS": ",".join(
                    runner_security_group_ids
                ),
                "TRIKON_VERIFY_RUNNER_ACTIVE_REVISION_SSM_PARAM": (
                    verify_runner_active_revision_ssm_param
                ),
                "TRIKON_VERIFY_JOBS_DLQ_URL": verify_jobs_dlq.queue_url,
                "TRIKON_VERDICTS_TABLE": _VERDICTS_TABLE,
                APP_KEY_PARAMETER_ENV: app_private_key_parameter_name,
                "TRIKON_APP_ID": str(app_id),
                "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE": (
                    _CHECK_RUN_DETAILS_URL_TEMPLATE
                ),
                "TRIKON_LOG_LEVEL": "INFO",
                "TRIKON_WEB_API_URL": trikon_web_api_url,
                "TRIKON_WEB_APP_URL": trikon_web_app_url,
                USAGE_SECRET_PARAMETER_ENV: usage_secret_parameter_name,
                # Busy re-enqueue target (Req 11.18). ``from_queue_arn``
                # derives the URL from the ARN, so this works for the
                # imported queue.
                "TRIKON_VERIFY_JOBS_QUEUE_URL": verify_jobs_queue.queue_url,
                # Spot_Flag (cost-cuts Req 8.2): Attempt 1 on FARGATE_SPOT.
                "TRIKON_USE_FARGATE_SPOT": "true" if use_fargate_spot else "false",
                # Interim_Check_Run lookup on the Retry_Job failure path.
                "TRIKON_PR_STATE_TABLE": _PR_STATE_TABLE,
                # Same constant as the trikon-verify-jobs redrive policy, so
                # the last Retry_Job receive fails closed (Req 11.5).
                "TRIKON_VERIFY_JOBS_MAX_RECEIVE_COUNT": str(
                    VERIFY_JOBS_MAX_RECEIVE_COUNT
                ),
            },
            log_group=_function_log_group(
                self, "OrchestratorLogs", "trikon-cloud-orchestrator"
            ),
        )

        # Batch size 1 + report-batch-item-failures per Requirement 1.1
        # and design.md §4.5. ``SqsEventSource`` emits an
        # ``EventSourceMapping`` with the right event-source-arn wiring
        # and adds ``lambda:InvokeFunction`` to the queue's implicit
        # source policy.
        orchestrator_fn.add_event_source(
            lambda_events.SqsEventSource(
                verify_jobs_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
                report_batch_item_failures=True,
            ),
        )

        # -------------------------------------------------------------
        # 9. Lifecycle role — mirror shape of the orchestrator role,
        # with the four scoped grants from design.md §8.3. Requirement
        # 15.5 is enforced by absence: no ``iam:PassRole`` on
        # ``trikon-verify-task-role-*``.
        # -------------------------------------------------------------
        lifecycle_role = iam.Role(
            self,
            "LifecycleRole",
            role_name="trikon-cloud-installation-lifecycle-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        _grant_lifecycle_permissions(
            lifecycle_role,
            lifecycle_queue=lifecycle_queue,
            lifecycle_dlq=lifecycle_dlq,
            installations_table=installations_table,
            account_id=self.account,
        )

        # -------------------------------------------------------------
        # 10. Lifecycle Lambda. Same runtime/arch/memory/timeout as the
        # orchestrator per Requirement 15.1, but reserved concurrency
        # 5 (installation churn is O(N-tenants) not O(N-PRs)).
        # -------------------------------------------------------------
        lifecycle_fn = _lambda.Function(
            self,
            "LifecycleFn",
            function_name="trikon-cloud-installation-lifecycle",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            reserved_concurrent_executions=5,
            handler="trikon_cloud.installation_lifecycle.handler.lambda_handler",
            code=lambda_code,
            role=lifecycle_role,
            environment={
                "TRIKON_AWS_ACCOUNT_ID": self.account,
                # Baked into per-installation task-role policies by the
                # Runtime_IAM_Renderer. The lifecycle role never reads the
                # parameter value, so it has no SSM grant (cost-cuts Req 4.7).
                APP_KEY_PARAMETER_ENV: app_private_key_parameter_name,
                "TRIKON_INSTALLATIONS_TABLE": installations_table.table_name,
                "TRIKON_LOG_LEVEL": "INFO",
            },
            log_group=_function_log_group(
                self, "LifecycleLogs", "trikon-cloud-installation-lifecycle"
            ),
        )

        # Batch size 1 per Requirement 10.1 — mirrors the orchestrator's
        # event-source configuration.
        lifecycle_fn.add_event_source(
            lambda_events.SqsEventSource(
                lifecycle_queue,
                batch_size=1,
                max_batching_window=Duration.seconds(0),
                report_batch_item_failures=True,
            ),
        )

        # -------------------------------------------------------------
        # 11. Usage_Reporter (sandbox-minute-plans design.md §11). One
        # DLQ (14-day retention) catches both Lambda async-invoke
        # failures (function errors after 2 retries / 6 h) and
        # EventBridge delivery failures (185 retries / 24 h), Req 12.8,
        # 12.9. The role carries only basic execution +
        # UsageSecretParameterRead (cost-cuts Req 4.3); CDK adds
        # ``sqs:SendMessage`` on the DLQ for the function's
        # ``dead_letter_queue`` and a queue policy for the rule target.
        # -------------------------------------------------------------
        usage_dlq = sqs.Queue(
            self,
            "UsageReporterDlq",
            queue_name="trikon-cloud-usage-reporter-dlq",
            retention_period=Duration.days(14),
        )
        usage_reporter_role = iam.Role(
            self,
            "UsageReporterRole",
            role_name="trikon-cloud-usage-reporter-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        usage_reporter_role.add_to_principal_policy(
            _parameter_read_statement(
                USAGE_SECRET_PARAMETER_READ_SID, [usage_secret_parameter_arn]
            ),
        )
        usage_fn = _lambda.Function(
            self,
            "UsageReporterFn",
            function_name="trikon-cloud-usage-reporter",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=256,
            timeout=Duration.seconds(45),
            reserved_concurrent_executions=5,
            handler="trikon_cloud.usage_reporter.handler.lambda_handler",
            code=lambda_code,
            role=usage_reporter_role,
            environment={
                "TRIKON_WEB_API_URL": trikon_web_api_url,
                USAGE_SECRET_PARAMETER_ENV: usage_secret_parameter_name,
                "TRIKON_LOG_LEVEL": "INFO",
            },
            log_group=_function_log_group(
                self, "UsageReporterLogs", "trikon-cloud-usage-reporter"
            ),
            retry_attempts=2,
            max_event_age=Duration.hours(6),
            dead_letter_queue=usage_dlq,
        )

        # -------------------------------------------------------------
        # 12. Spot_Retry_Handler (cost-cuts design §5, Req 9, 11). Same
        # DLQ shape as the Usage_Reporter: one 14-day queue catches async
        # invoke failures (2 retries / 6 h) and EventBridge delivery
        # failures (185 retries / 24 h). The claim table dedupes
        # re-queues per task ARN (D3); its data is ephemeral, so it has
        # a TTL, no PITR and DESTROY.
        # -------------------------------------------------------------
        spot_retry_dlq = sqs.Queue(
            self,
            "SpotRetryDlq",
            queue_name="trikon-cloud-spot-retry-dlq",
            retention_period=Duration.days(14),
        )
        spot_retries_table = dynamodb.TableV2(
            self,
            "SpotRetriesTable",
            table_name=_SPOT_RETRIES_TABLE,
            partition_key=dynamodb.Attribute(
                name="task_arn",
                type=dynamodb.AttributeType.STRING,
            ),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )
        spot_retry_role = iam.Role(
            self,
            "SpotRetryRole",
            role_name="trikon-cloud-spot-retry-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole",
                ),
            ],
        )
        _grant_spot_retry_permissions(
            spot_retry_role,
            app_private_key_parameter_arn=app_private_key_parameter_arn,
            verify_jobs_queue_arn=verify_jobs_queue.queue_arn,
            spot_retries_table_arn=spot_retries_table.table_arn,
            account_id=self.account,
        )
        # Env mirrors every :class:`SpotRetryEnvConfig` alias except
        # ``AWS_REGION``, which the Lambda runtime injects.
        spot_retry_fn = _lambda.Function(
            self,
            "SpotRetryFn",
            function_name="trikon-cloud-spot-retry",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=256,
            timeout=Duration.seconds(90),
            reserved_concurrent_executions=5,
            handler="trikon_cloud.orchestrator.spot_retry_handler.lambda_handler",
            code=lambda_code,
            role=spot_retry_role,
            environment={
                "TRIKON_APP_ID": str(app_id),
                APP_KEY_PARAMETER_ENV: app_private_key_parameter_name,
                "TRIKON_VERIFY_JOBS_QUEUE_URL": verify_jobs_queue.queue_url,
                "TRIKON_VERDICTS_TABLE": _VERDICTS_TABLE,
                "TRIKON_PR_STATE_TABLE": _PR_STATE_TABLE,
                "TRIKON_SPOT_RETRIES_TABLE": spot_retries_table.table_name,
                "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE": (
                    _CHECK_RUN_DETAILS_URL_TEMPLATE
                ),
                "TRIKON_LOG_LEVEL": "INFO",
            },
            log_group=_function_log_group(
                self, "SpotRetryLogs", "trikon-cloud-spot-retry"
            ),
            retry_attempts=2,
            max_event_age=Duration.hours(6),
            dead_letter_queue=spot_retry_dlq,
        )

        # Req 12.1, cost-cuts Req 9.1: route runner tasks on
        # ``trikon-verify-cluster`` reaching STOPPED to the Usage_Reporter
        # and the Spot_Retry_Handler. Both handlers re-check cluster, group,
        # and status, so the pattern is a cost filter, not the only guard.
        events.Rule(
            self,
            "VerifyRunnerTaskStoppedRule",
            rule_name="trikon-verify-runner-task-stopped",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [
                        f"arn:aws:ecs:us-east-1:{self.account}:cluster/trikon-verify-cluster",
                    ],
                    "lastStatus": ["STOPPED"],
                    "group": ["family:trikon-verify-runner"],
                },
            ),
            targets=[
                targets.LambdaFunction(
                    usage_fn,
                    dead_letter_queue=usage_dlq,
                    retry_attempts=185,
                    max_event_age=Duration.hours(24),
                ),
                targets.LambdaFunction(
                    spot_retry_fn,
                    dead_letter_queue=spot_retry_dlq,
                    retry_attempts=185,
                    max_event_age=Duration.hours(24),
                ),
            ],
        )

        # -------------------------------------------------------------
        # 13. Public attributes — surfaced for app.py / release
        # engineer / contract tests (task 19) via CFN outputs would go
        # here in a follow-up; at this task's scope they live on the
        # Python object only.
        # -------------------------------------------------------------
        self.orchestrator_function_arn = orchestrator_fn.function_arn
        self.lifecycle_function_arn = lifecycle_fn.function_arn
        self.installation_events_queue_url = lifecycle_queue.queue_url
        self.installations_table_name = installations_table.table_name
        self.usage_reporter_function_arn = usage_fn.function_arn
        self.usage_reporter_dlq_url = usage_dlq.queue_url
        self.spot_retry_function_arn = spot_retry_fn.function_arn
        self.spot_retry_dlq_url = spot_retry_dlq.queue_url
        self.spot_retries_table_name = spot_retries_table.table_name


# ---------------------------------------------------------------------------
# Private helpers — one per role, matching design.md §8.2 / §8.3.
# ---------------------------------------------------------------------------


def _function_log_group(
    scope: Construct, construct_id: str, function_name: str
) -> logs.LogGroup:
    """Return the explicit ``/aws/lambda/<function_name>`` log group (cost-cuts Req 2.3, 2.4).

    7-day retention; RETAIN so logs outlive a stack teardown (matching the
    runner and webhook-receiver groups). A retained group must be deleted
    before the stack is re-created after ``cdk destroy``, because the
    deploy would otherwise fail on the name collision.
    """
    return logs.LogGroup(
        scope,
        construct_id,
        log_group_name=f"/aws/lambda/{function_name}",
        retention=logs.RetentionDays.ONE_WEEK,
        removal_policy=RemovalPolicy.RETAIN,
    )


def _grant_orchestrator_permissions(
    role: iam.Role,
    *,
    verify_jobs_queue: sqs.IQueue,
    verify_jobs_dlq: sqs.IQueue,
    active_revision_param: ssm.IStringParameter,
    app_private_key_parameter_arn: str,
    usage_secret_parameter_arn: str,
    account_id: str,
) -> None:
    """Attach the orchestrator IAM statements per design.md §8.2.

    Grants 1-6 and 8 are Spec 3's and sandbox-minute-plans'. The former
    Grants 7 (App key) and 9 (Usage_Secret) are replaced by one inline
    ``SecureParameterRead`` statement: ``ssm:GetParameter`` on exactly the
    App_Key_Parameter and Usage_Secret_Parameter ARNs (cost-cuts Req 4.2).

    Grants 1-2, 6 and ``SecureParameterRead`` are inline policy
    statements; grants 3-5 and 8 use the CDK ``grant_*`` helpers which
    emit equivalent inline statements scoped to the referenced resource.
    Grant 5 (``grant_read`` on the plain String active-revision parameter)
    is not a Secure_Parameter grant and stays a separate statement.

    Args:
        role: The orchestrator Lambda's execution role.
        verify_jobs_queue: Spec 1's ``trikon-verify-jobs`` queue (imported).
        verify_jobs_dlq: Spec 1's ``trikon-verify-jobs-dlq`` queue (imported).
        active_revision_param: Spec 2's active-revision SSM parameter.
        app_private_key_parameter_arn: Parameter_ARN of the App key.
        usage_secret_parameter_arn: Parameter_ARN of the Usage_Secret.
        account_id: The AWS account ID — resolved from ``Stack.of(role).account``
            at the call site. Used to construct the ECS + IAM resource
            ARNs for Grants 1, 2, and 6.
    """
    # Grant 1 — ``ecs:RunTask`` on the runner task-definition family
    # (Requirement 8.2). Wildcard ``:*`` scopes to any revision within
    # the ``trikon-verify-runner`` family; the specific revision is
    # resolved at RunTask time via SSM (Grant 5).
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="EcsRunTaskOnRunnerFamily",
            effect=iam.Effect.ALLOW,
            actions=["ecs:RunTask"],
            resources=[
                f"arn:aws:ecs:us-east-1:{account_id}:task-definition/trikon-verify-runner:*",
            ],
        ),
    )

    # Grant 2 — ``iam:PassRole`` on the per-installation task role
    # pattern (Requirement 8.3). The ``PassedToService`` condition
    # locks pass-through to ECS tasks only, so a compromised
    # orchestrator cannot pass the role to other services.
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="IamPassRoleForTaskRoles",
            effect=iam.Effect.ALLOW,
            actions=["iam:PassRole"],
            resources=[
                f"arn:aws:iam::{account_id}:role/trikon-verify-task-role-*",
            ],
            conditions={
                "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"},
            },
        ),
    )

    # Grant 3 — SQS ``Receive``/``Delete``/``GetQueueAttributes`` on
    # the verify-jobs queue (Requirement 8.4). CDK's
    # ``grant_consume_messages`` expands to exactly these three
    # actions on the queue ARN.
    verify_jobs_queue.grant_consume_messages(role)

    # Grant 4 — SQS ``SendMessage`` on the verify-jobs DLQ
    # (Requirement 8.5). Used for explicit failovers from the
    # dispatcher (rare — SQS's redrive policy handles the normal path).
    verify_jobs_dlq.grant_send_messages(role)

    # Grant 5 — ``ssm:GetParameter`` on the active-revision parameter
    # (Open Item b resolution — design.md §4.3). Read once per
    # container lifetime by :class:`TaskDefinitionResolver`.
    active_revision_param.grant_read(role)

    # Grant 6 — ``dynamodb:PutItem`` on ``trikon_verdicts`` — the
    # Never-Fail-Open row write (design.md §7.2, §8.2 Grant 6). The
    # ``LeadingKeys=["*"]`` wildcard is required because the
    # orchestrator role is a *fleet* role, not a per-installation
    # role: it has no ``PrincipalTag/installation_id`` to substitute.
    # Runtime enforcement is that
    # :func:`write_orchestrator_failure_verdict` writes rows whose
    # partition key equals the incoming ``sqs_message.installation_id``
    # — no other value is possible in the code path. The strictly
    # tighter alternative (per-message ``sts:AssumeRole`` into a
    # per-installation role) is deferred to M2 per design.md §11.
    role.add_to_principal_policy(
        _verdict_write_statement("NeverFailOpenVerdictWrite", account_id),
    )

    # ``OrchestratorPrStateRead`` — ``dynamodb:GetItem`` on ``trikon_pr_state``
    # so a failed Retry_Job dispatch completes the Interim_Check_Run the
    # Runner recorded there (cost-cuts Req 11.5).
    role.add_to_principal_policy(
        _pr_state_read_statement(ORCHESTRATOR_PR_STATE_READ_SID, account_id),
    )

    # ``SecureParameterRead`` (replaces Grants 7 and 9) — ``ssm:GetParameter``
    # on exactly the App key and Usage_Secret Parameter_ARNs (cost-cuts
    # Req 4.2). The App key signs the GitHub JWT for the Never-Fail-Open
    # Check Run POST (design.md §7.3); the Usage_Secret signs Usage_API
    # requests (Req 11.16).
    role.add_to_principal_policy(
        _parameter_read_statement(
            SECURE_PARAMETER_READ_SID,
            [app_private_key_parameter_arn, usage_secret_parameter_arn],
        ),
    )

    # Grant 8 — busy re-enqueue (Req 11.18, 11.19). ``grant_send_messages``
    # expands to ``sqs:SendMessage``, ``sqs:GetQueueAttributes`` and
    # ``sqs:GetQueueUrl`` on the ``trikon-verify-jobs`` ARN only. The
    # queue has no customer-managed KMS key, so no KMS grant is needed.
    # The ``ChangeMessageVisibility`` fallback is covered by Grant 3.
    verify_jobs_queue.grant_send_messages(role)


def _grant_spot_retry_permissions(
    role: iam.Role,
    *,
    app_private_key_parameter_arn: str,
    verify_jobs_queue_arn: str,
    spot_retries_table_arn: str,
    account_id: str,
) -> None:
    """Attach the five inline Spot_Retry_Handler statements (cost-cuts design §5).

    The sixth statement, ``sqs:SendMessage`` on ``trikon-cloud-spot-retry-dlq``,
    is generated by CDK for the function's ``dead_letter_queue``. Every
    statement here is a single-purpose inline grant on exact resources, so
    the role gets no ``sqs:GetQueueAttributes`` / ``GetQueueUrl`` and no
    SSM read beyond ``ssm:GetParameter`` on the App key (Req 4.7, 4.8).

    Args:
        role: The Spot_Retry_Handler Lambda's execution role.
        app_private_key_parameter_arn: Parameter_ARN of the App key, read to
            mint the GitHub JWT for Fail_Closed_Result Check Runs.
        verify_jobs_queue_arn: ARN of ``trikon-verify-jobs``, the Retry_Job
            destination (Req 9.7).
        spot_retries_table_arn: ARN of the ``trikon-cloud-spot-retries``
            claim table.
        account_id: The AWS account ID, for the fleet table ARNs.
    """
    role.add_to_principal_policy(
        _parameter_read_statement(
            SPOT_RETRY_APP_KEY_PARAMETER_READ_SID, [app_private_key_parameter_arn]
        ),
    )
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid=SPOT_RETRY_SEND_RETRY_JOB_SID,
            effect=iam.Effect.ALLOW,
            actions=["sqs:SendMessage"],
            resources=[verify_jobs_queue_arn],
        ),
    )
    role.add_to_principal_policy(
        _verdict_write_statement(SPOT_RETRY_VERDICT_WRITE_SID, account_id),
    )
    role.add_to_principal_policy(
        _pr_state_read_statement(SPOT_RETRY_PR_STATE_READ_SID, account_id),
    )
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid=SPOT_RETRY_CLAIMS_SID,
            effect=iam.Effect.ALLOW,
            actions=[
                "dynamodb:PutItem",
                "dynamodb:GetItem",
                "dynamodb:UpdateItem",
            ],
            resources=[spot_retries_table_arn],
        ),
    )


def _verdict_write_statement(sid: str, account_id: str) -> iam.PolicyStatement:
    """Return the fleet-role ``dynamodb:PutItem`` statement on ``trikon_verdicts``.

    ``LeadingKeys=["*"]`` because fleet roles carry no
    ``PrincipalTag/installation_id``; see Grant 6 in
    :func:`_grant_orchestrator_permissions`.
    """
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["dynamodb:PutItem"],
        resources=[
            f"arn:aws:dynamodb:us-east-1:{account_id}:table/{_VERDICTS_TABLE}",
        ],
        conditions={
            "ForAllValues:StringLike": {
                "dynamodb:LeadingKeys": ["*"],
            },
        },
    )


def _pr_state_read_statement(sid: str, account_id: str) -> iam.PolicyStatement:
    """Return ``dynamodb:GetItem`` on ``trikon_pr_state`` (Interim_Check_Run id)."""
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["dynamodb:GetItem"],
        resources=[
            f"arn:aws:dynamodb:us-east-1:{account_id}:table/{_PR_STATE_TABLE}",
        ],
    )


def _parameter_read_statement(sid: str, resources: list[str]) -> iam.PolicyStatement:
    """Return an inline ``ssm:GetParameter`` statement on exactly ``resources``.

    Secure_Parameter grants never use the ``grant_read`` helpers, which
    would add ``ssm:DescribeParameters``, ``ssm:GetParameters`` and
    ``ssm:GetParameterHistory`` (cost-cuts Req 4.8).
    """
    return iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.ALLOW,
        actions=["ssm:GetParameter"],
        resources=resources,
    )


def _grant_lifecycle_permissions(
    role: iam.Role,
    *,
    lifecycle_queue: sqs.IQueue,
    lifecycle_dlq: sqs.IQueue,
    installations_table: dynamodb.ITableV2,
    account_id: str,
) -> None:
    """Attach the four lifecycle IAM statements per design.md §8.3.

    Requirement 15.5 is enforced by absence: NO ``iam:PassRole``. The
    lifecycle handler creates per-installation roles via
    ``iam:CreateRole`` / ``iam:PutRolePolicy`` and deletes them via
    ``iam:DeleteRolePolicy`` / ``iam:DeleteRole``, but never assumes
    or passes them.

    Args:
        role: The lifecycle Lambda's execution role.
        lifecycle_queue: The new ``trikon-cloud-installation-events`` queue.
        lifecycle_dlq: The new ``trikon-cloud-installation-events-dlq`` DLQ.
        installations_table: The new ``trikon-cloud-installations`` table.
        account_id: The AWS account ID — used to construct the IAM
            resource ARN for Grant 1.
    """
    # Grant 1 — IAM management on the per-installation task-role
    # pattern (Requirement 15.4). ``iam:TagRole`` is included so
    # ``handle_installation_created`` can stamp the
    # ``installation_id`` tag that the runtime IAM policy's
    # ``PrincipalTag`` condition keys off (design.md §6.4).
    role.add_to_principal_policy(
        iam.PolicyStatement(
            sid="IamManageTaskRoles",
            effect=iam.Effect.ALLOW,
            actions=[
                "iam:CreateRole",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:TagRole",
            ],
            resources=[
                f"arn:aws:iam::{account_id}:role/trikon-verify-task-role-*",
            ],
        ),
    )

    # Grant 2 — DynamoDB CRUD on the installations table
    # (Requirement 15.4). Scoped to the exact table via
    # ``installations_table.grant(...)``.
    installations_table.grant(
        role,
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    )

    # Grant 3 — SQS ``Receive``/``Delete``/``GetQueueAttributes`` on
    # the installation-events queue (Requirement 15.4).
    lifecycle_queue.grant_consume_messages(role)

    # Grant 4 — SQS ``SendMessage`` on the installation-events DLQ
    # (Requirement 15.4). Used for explicit failover from the
    # lifecycle handler — normal DLQ path is via the queue's redrive
    # policy after ``maxReceiveCount=3``.
    lifecycle_dlq.grant_send_messages(role)
