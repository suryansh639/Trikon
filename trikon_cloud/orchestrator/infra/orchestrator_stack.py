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
Spec 2 per design.md §9.2). The App private-key Secrets Manager secret
is likewise referenced by ARN and never created here (Requirement 16.4,
matching Spec 1/2 conventions).

IAM policy shapes are pinned by Requirements 8.2-8.6 (orchestrator) and
15.4-15.5 (lifecycle) — the eight orchestrator grants live in
:func:`_grant_orchestrator_permissions` and the four lifecycle grants
in :func:`_grant_lifecycle_permissions`, matching design.md §8.2/§8.3
one-for-one. Requirement 15.5 is enforced by absence: the lifecycle
role has NO ``iam:PassRole`` grant.

Region is fixed at ``us-east-1`` (Requirement 16.1) via the ``env`` on
the enclosing ``cdk.App`` (see ``app.py``, task 14). Deploy commands,
prerequisites, and rollback notes live in ``README.md`` alongside this
module (task 15).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as lambda_events
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

__all__ = ["OrchestratorStack"]


class OrchestratorStack(Stack):
    """CloudFormation stack for Spec 3 — orchestrator + lifecycle Lambdas.

    Provisions two Python 3.11 / arm64 Lambdas, one new SQS queue + DLQ
    (installation-events), and one new DynamoDB table (installations).
    References Spec 1's ``trikon-verify-jobs`` queue + DLQ by ARN, and
    Spec 2's ``/trikon/verify-runner/active-revision`` SSM parameter by
    name. Does NOT create the App private-key Secrets Manager secret —
    the release engineer populates it out-of-band before the first
    deploy (Requirement 16.4).

    Args:
        scope: Parent construct (typically the ``cdk.App`` from
            ``app.py``).
        construct_id: Stack logical id (typically
            ``"TrikonCloudOrchestratorStack"`` — design.md §8.5).
        app_id: GitHub App numeric ID; baked into the orchestrator's
            ``TRIKON_APP_ID`` env var so the GitHub JWT minter in
            :mod:`trikon_cloud.orchestrator.never_fail_open` can produce
            valid Check Run tokens.
        app_private_key_secret_arn: Complete ARN of the pre-existing
            Secrets Manager secret holding the GitHub App private key.
            Grants read-only access to the orchestrator role.
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
    """

    orchestrator_function_arn: str
    lifecycle_function_arn: str
    installation_events_queue_url: str
    installations_table_name: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_id: int,
        app_private_key_secret_arn: str,
        verify_jobs_queue_arn: str,
        verify_jobs_dlq_arn: str,
        runner_subnet_ids: list[str],
        runner_security_group_ids: list[str],
        verify_runner_active_revision_ssm_param: str = "/trikon/verify-runner/active-revision",
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
        # 5. Shared deployment asset. Both Lambdas ship the same
        # ``trikon_cloud/`` package zip; ``Code.from_asset`` bundles
        # the folder as-is, matching the pattern in
        # :mod:`trikon_cloud.webhook_receiver.infra.webhook_receiver_stack`.
        # The release engineer installs the ``cloud`` extra deps into
        # the asset directory before ``cdk deploy`` — see README.md
        # §Deploy (task 15). Docker-based bundling via
        # ``aws_lambda_python_alpha.PythonFunction`` is out of scope
        # for this task (the alpha module is not currently in the
        # ``dev`` extra).
        # -------------------------------------------------------------
        lambda_code = _lambda.Code.from_asset("trikon_cloud")

        # -------------------------------------------------------------
        # 6. Reference the App private-key Secrets Manager secret by
        # ARN. Used by the orchestrator to mint installation-scoped
        # GitHub JWTs for the Never-Fail-Open Check Run POST (design
        # §7.3). NOT created here — populated out-of-band.
        # -------------------------------------------------------------
        app_private_key_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "AppPrivateKeySecret",
            secret_complete_arn=app_private_key_secret_arn,
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
            app_private_key_secret=app_private_key_secret,
            account_id=self.account,
        )

        # -------------------------------------------------------------
        # 8. Orchestrator Lambda. Runtime configuration is pinned by
        # Requirement 8.1: Python 3.11 / arm64 / 512 MB / 30 s /
        # reserved concurrency 10. Env vars mirror
        # :class:`OrchestratorEnvConfig`'s ten CDK-populated aliases
        # (``AWS_REGION`` is runtime-injected by Lambda, so we don't
        # set it here — the value falls through to
        # :class:`OrchestratorEnvConfig`'s default of ``"us-east-1"``).
        # -------------------------------------------------------------
        orchestrator_fn = _lambda.Function(
            self,
            "OrchestratorFn",
            function_name="trikon-cloud-orchestrator",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
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
                "TRIKON_VERDICTS_TABLE": "trikon_verdicts",
                "TRIKON_APP_PRIVATE_KEY_SECRET_ARN": app_private_key_secret_arn,
                "TRIKON_APP_ID": str(app_id),
                "TRIKON_CHECK_RUN_DETAILS_URL_TEMPLATE": (
                    "https://api.trikon.unideploy.com/audits/{delivery_id}"
                ),
                "TRIKON_LOG_LEVEL": "INFO",
            },
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
                "TRIKON_APP_PRIVATE_KEY_SECRET_ARN": app_private_key_secret_arn,
                "TRIKON_INSTALLATIONS_TABLE": installations_table.table_name,
                "TRIKON_LOG_LEVEL": "INFO",
            },
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
        # 11. Public attributes — surfaced for app.py / release
        # engineer / contract tests (task 19) via CFN outputs would go
        # here in a follow-up; at this task's scope they live on the
        # Python object only.
        # -------------------------------------------------------------
        self.orchestrator_function_arn = orchestrator_fn.function_arn
        self.lifecycle_function_arn = lifecycle_fn.function_arn
        self.installation_events_queue_url = lifecycle_queue.queue_url
        self.installations_table_name = installations_table.table_name


# ---------------------------------------------------------------------------
# Private helpers — one per role, matching design.md §8.2 / §8.3.
# ---------------------------------------------------------------------------


def _grant_orchestrator_permissions(
    role: iam.Role,
    *,
    verify_jobs_queue: sqs.IQueue,
    verify_jobs_dlq: sqs.IQueue,
    active_revision_param: ssm.IStringParameter,
    app_private_key_secret: secretsmanager.ISecret,
    account_id: str,
) -> None:
    """Attach the eight orchestrator IAM statements per design.md §8.2.

    Grants 1-2 and 6-7 are inline policy statements; grants 3-5 use the
    CDK ``grant_*`` helpers which emit equivalent inline statements
    scoped to the referenced resource. The order matches design.md
    §8.2 one-for-one so a diff of the synth output maps to a diff of
    this function body.

    Args:
        role: The orchestrator Lambda's execution role.
        verify_jobs_queue: Spec 1's ``trikon-verify-jobs`` queue (imported).
        verify_jobs_dlq: Spec 1's ``trikon-verify-jobs-dlq`` queue (imported).
        active_revision_param: Spec 2's active-revision SSM parameter.
        app_private_key_secret: The App private-key Secrets Manager secret.
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
        iam.PolicyStatement(
            sid="NeverFailOpenVerdictWrite",
            effect=iam.Effect.ALLOW,
            actions=["dynamodb:PutItem"],
            resources=[
                f"arn:aws:dynamodb:us-east-1:{account_id}:table/trikon_verdicts",
            ],
            conditions={
                "ForAllValues:StringLike": {
                    "dynamodb:LeadingKeys": ["*"],
                },
            },
        ),
    )

    # Grant 7 — ``secretsmanager:GetSecretValue`` on the App private
    # key. Used by the GitHub JWT minter to sign installation-scoped
    # tokens for the Never-Fail-Open Check Run POST (design.md §7.3).
    app_private_key_secret.grant_read(role)


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
