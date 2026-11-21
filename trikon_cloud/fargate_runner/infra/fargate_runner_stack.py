# CDK's jsii-generated stubs type ``**kwargs`` as ``Any`` on every
# construct ``__init__``. Under the repo's ``disallow_any_explicit = true``
# mypy config, forwarding ``**kwargs: Any`` to ``super().__init__``
# surfaces as an ``explicit-any`` error the construct authors — not we —
# control. Suppress at file scope; every ``Any`` in this module is CDK's.
# mypy: disable-error-code="explicit-any"
"""CDK stack for the Trikon Cloud Fargate verify-runner data plane.

Provisions everything Spec 2 owns: the shared ECS cluster
(``trikon-verify-cluster``), the single task-definition family
(``trikon-verify-runner``), the three DynamoDB tables
(``trikon_installations``, ``trikon_verdicts``, ``trikon_pr_state``)
including the two GSIs on ``trikon_verdicts``, the S3 evidence bucket
(``trikon-cloud-evidence``), the CloudWatch log group
(``/aws/ecs/trikon-verify-runner``), the public-subnet-only
``trikon-verify-vpc`` (zero NAT gateways) with the egress-only
``trikon-verify-runner-sg`` security group, and a per-installation IAM
task-role template exposed via
:meth:`FargateRunnerStack.build_task_role_for_installation` for Spec 3
to invoke at ``ecs.RunTask`` time.

The SSM SecureString parameter holding the GitHub App private key
(default ``/trikon-cloud/github-app-private-key``) is **referenced by
name**, never created here (Requirement 19.5; cost-cuts Req 3.5) — its
value is put out-of-band by the release engineer before the first
deploy. ``cdk destroy`` is therefore non-destructive: the
``RemovalPolicy.RETAIN`` on the tables + bucket guarantees data
survives a stack teardown; only the cluster, task definition, IAM
roles, and log group vanish.

Deploy commands, prerequisites, and rollback notes live in
``README.md`` alongside this module.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from trikon_cloud.secure_params import APP_KEY_PARAMETER_ENV, parameter_arn

__all__ = ["FargateRunnerStack"]


class FargateRunnerStack(Stack):
    """CDK stack for the Trikon Cloud Fargate verify-runner data plane.

    Provisions the shared ECS cluster (``trikon-verify-cluster``), the
    single task-definition family (``trikon-verify-runner``), the three
    DynamoDB tables (``trikon_installations``, ``trikon_verdicts``,
    ``trikon_pr_state``), the S3 evidence bucket, the CloudWatch log
    group, and a per-installation IAM task-role template.

    Does NOT create the App_Key_Parameter (Requirement 19.5; cost-cuts
    Req 3.5) — its name is passed in as a constructor argument. The
    release engineer puts the SecureString value out-of-band before
    deploy.

    Does NOT create the runner image repository either (go-live design
    D1). The operator creates the private ECR repository
    ``trikon-cloud-runner`` and pushes an immutable tag before deploying;
    the stack only names the image through ``runner_image_uri``.

    Args:
        scope: Parent construct (typically ``cdk.App``).
        id: Stack logical id (typically ``"FargateRunnerStack"``).
        app_private_key_parameter_name: "/"-rooted name of the
            pre-existing SSM SecureString parameter holding the GitHub
            App private key. Passed to the runner container as
            ``TRIKON_APP_PRIVATE_KEY_PARAMETER_NAME``.
        app_id: GitHub App numeric ID (baked into the task-definition
            env as ``TRIKON_APP_ID``).
        runner_image_uri: Runner container image reference, already
            validated by
            :func:`trikon_cloud.fargate_runner.infra.app.runner_image_uri_from_context`
            when built through the CDK app. Used unchanged as the
            container image (go-live Req 5.4).
        kwargs: Forwarded to :class:`aws_cdk.Stack`.

    Public attributes:
        cluster_arn: ARN of the ECS cluster.
        task_definition_arn: ARN of the runner task definition.
        verdicts_table_name: Name of the verdicts DynamoDB table.
        runner_subnet_ids: Ids of the VPC's public subnets (one per AZ)
            that runner tasks launch into with a public IP.
        runner_security_group_id: Id of the egress-only
            ``trikon-verify-runner-sg`` security group.
    """

    cluster_arn: str
    task_definition_arn: str
    verdicts_table_name: str
    active_revision_param_name: str
    runner_subnet_ids: list[str]
    runner_security_group_id: str

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        app_private_key_parameter_name: str,
        app_id: int,
        runner_image_uri: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # -------------------------------------------------------------
        # 1. DynamoDB tables — installations, verdicts (+ GSI1, GSI2),
        # pr_state. All PAY_PER_REQUEST + AWS-managed encryption +
        # RemovalPolicy.RETAIN so `cdk destroy` never drops data
        # (Requirement 16.4).
        # -------------------------------------------------------------
        installations_table = dynamodb.Table(
            self,
            "TrikonInstallationsTable",
            table_name="trikon_installations",
            partition_key=dynamodb.Attribute(
                name="installation_id",
                type=dynamodb.AttributeType.NUMBER,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        verdicts_table = dynamodb.Table(
            self,
            "TrikonVerdictsTable",
            table_name="trikon_verdicts",
            partition_key=dynamodb.Attribute(
                name="installation_id",
                type=dynamodb.AttributeType.NUMBER,
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # GSI1: repo_full_name_index — PK repo_full_name, SK sk.
        # Supports "list all verdicts for a repo" (memo §4.2).
        verdicts_table.add_global_secondary_index(
            index_name="repo_full_name_index",
            partition_key=dynamodb.Attribute(
                name="repo_full_name",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # GSI2: risk_bucket_index — PK installation_id, SK risk_bucket_sk.
        # Supports "list highest-risk verdicts for an installation".
        verdicts_table.add_global_secondary_index(
            index_name="risk_bucket_index",
            partition_key=dynamodb.Attribute(
                name="installation_id",
                type=dynamodb.AttributeType.NUMBER,
            ),
            sort_key=dynamodb.Attribute(
                name="risk_bucket_sk",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        pr_state_table = dynamodb.Table(
            self,
            "TrikonPrStateTable",
            table_name="trikon_pr_state",
            partition_key=dynamodb.Attribute(
                name="pr_key",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -------------------------------------------------------------
        # 2. S3 evidence bucket — for evidence blobs over ~350 KB that
        # spill out of the DynamoDB row (design §3.9). Block-all-public
        # + S3-managed encryption + RETAIN.
        # -------------------------------------------------------------
        evidence_bucket = s3.Bucket(
            self,
            "TrikonEvidenceBucket",
            bucket_name="trikon-cloud-evidence",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -------------------------------------------------------------
        # 3. CloudWatch log group — /aws/ecs/trikon-verify-runner,
        # 7-day retention (cost-cuts Req 2.1), RETAIN so logs survive
        # stack teardown.
        # -------------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "TrikonVerifyRunnerLogs",
            log_group_name="/aws/ecs/trikon-verify-runner",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -------------------------------------------------------------
        # 4. VPC + runner security group + ECS cluster — Spec 3
        # references the cluster by name. 2-AZ VPC with public subnets
        # only and zero NAT gateways (sandbox-minute-plans Req 14.1,
        # 14.2): runner tasks get a public IP (``assignPublicIp=
        # ENABLED``) and reach ECR / package indexes through the
        # internet gateway, so no NAT hourly or per-GB charge accrues.
        # Changing the subnet layout replaces the VPC's subnets on
        # deploy; deploy when no runner task is running.
        # -------------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "TrikonVerifyVpc",
            vpc_name="trikon-verify-vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        # Egress-only: ``allow_all_outbound=True`` and no ingress rule is
        # ever added (Req 14.3). Security groups are stateful, so return
        # traffic for outbound connections is still admitted.
        runner_sg = ec2.SecurityGroup(
            self,
            "TrikonVerifyRunnerSecurityGroup",
            vpc=vpc,
            security_group_name="trikon-verify-runner-sg",
            description="Egress-only security group for trikon-verify-runner tasks",
            allow_all_outbound=True,
        )

        # Outputs the release engineer copies into the OrchestratorStack
        # ``runner_subnet_ids`` / ``runner_security_group_ids`` CDK
        # context (Req 14.4).
        public_subnet_ids = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PUBLIC
        ).subnet_ids
        CfnOutput(
            self,
            "RunnerSubnetIds",
            value=Fn.join(",", public_subnet_ids),
            description=(
                "Comma-separated public subnet ids for the orchestrator "
                "runner_subnet_ids context"
            ),
        )
        CfnOutput(
            self,
            "RunnerSecurityGroupId",
            value=runner_sg.security_group_id,
            description=(
                "Runner security group id for the orchestrator "
                "runner_security_group_ids context"
            ),
        )
        self.runner_subnet_ids = public_subnet_ids
        self.runner_security_group_id = runner_sg.security_group_id

        cluster = ecs.Cluster(
            self,
            "TrikonVerifyCluster",
            cluster_name="trikon-verify-cluster",
            vpc=vpc,
            # Associates FARGATE and FARGATE_SPOT so RunTask can name either
            # in its capacityProviderStrategy (cost-cuts Req 8.1). The
            # default strategy stays free of FARGATE_SPOT.
            enable_fargate_capacity_providers=True,
        )

        # -------------------------------------------------------------
        # 5. Task execution role — used by Fargate itself to pull the
        # image and write CloudWatch logs. NOT the task role (which
        # Spec 3 supplies per-installation at RunTask time).
        # -------------------------------------------------------------
        execution_role = iam.Role(
            self,
            "TrikonVerifyRunnerExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        # -------------------------------------------------------------
        # 6. Task definition — 1 vCPU / 2 GiB, family
        # ``trikon-verify-runner``. task_role is left unset because
        # Spec 3's orchestrator supplies a per-installation task role
        # at ``ecs.RunTask`` time via the ``overrides.taskRoleArn``
        # field (memo §5.3).
        # -------------------------------------------------------------
        task_def = ecs.FargateTaskDefinition(
            self,
            "TrikonVerifyRunnerTaskDef",
            family="trikon-verify-runner",
            cpu=1024,
            memory_limit_mib=2048,
            execution_role=execution_role,
        )

        # -------------------------------------------------------------
        # 7. Container in the task definition. The image is
        # ``runner_image_uri`` unchanged: normally an immutable
        # ``runner-<git short sha>`` tag in the private ECR repository
        # ``trikon-cloud-runner``, which the operator creates and pushes
        # to before deploying (trikon_cloud/DEPLOY.md). The execution
        # role's AmazonECSTaskExecutionRolePolicy (§5) already allows
        # pulls from same-account private ECR, so no extra grant is
        # added. Environment values are the CDK-configurable half of the
        # memo §5.4 env-var contract; Spec 3 supplies the RunTask-time
        # half via the ``overrides.containerOverrides.environment`` list.
        # -------------------------------------------------------------
        task_def.add_container(
            id="runner",
            image=ecs.ContainerImage.from_registry(runner_image_uri),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="runner",
                log_group=log_group,
            ),
            environment={
                APP_KEY_PARAMETER_ENV: app_private_key_parameter_name,
                "TRIKON_APP_ID": str(app_id),
                "TRIKON_VERDICTS_TABLE": "trikon_verdicts",
                "TRIKON_PR_STATE_TABLE": "trikon_pr_state",
                "TRIKON_EVIDENCE_BUCKET": "trikon-cloud-evidence",
                "TRIKON_LOG_LEVEL": "INFO",
            },
            # 120 s is the Fargate maximum and matches the Spot two-minute
            # warning, so a SIGTERM'd runner has time to post its
            # Interim_Check_Run before SIGKILL (cost-cuts Req 10.1).
            stop_timeout=Duration.seconds(120),
        )

        # -------------------------------------------------------------
        # 7b. SSM parameter — publishes the active task-def revision so
        # Spec 3's orchestrator can resolve it at cold start (design.md
        # §2 of trikon-cloud-fargate-runner-ssm-active-revision).
        # -------------------------------------------------------------
        active_revision_param = ssm.StringParameter(
            self,
            "ActiveRevisionParam",
            parameter_name="/trikon/verify-runner/active-revision",
            string_value=Fn.select(6, Fn.split(":", task_def.task_definition_arn)),
            description=(
                "Trikon Cloud — active revision of the trikon-verify-runner "
                "task definition. Consumed by the OrchestratorStack (Spec 3) "
                "TaskDefinitionResolver at cold start."
            ),
        )
        active_revision_param.node.add_dependency(task_def)

        # Expose for downstream reference (e.g. cross-stack imports)
        self.active_revision_param_name = active_revision_param.parameter_name

        # -------------------------------------------------------------
        # 8. Public attributes for Spec 3 to consume via cross-stack
        # references or via `Fn::ImportValue` after CFN synth.
        # -------------------------------------------------------------
        self.cluster_arn = cluster.cluster_arn
        self.task_definition_arn = task_def.task_definition_arn
        self.verdicts_table_name = verdicts_table.table_name

        # Silence linter warnings for tables + bucket held only via the
        # construct tree. CDK's synth walks the tree, so the tables /
        # bucket must be instantiated even though this file never
        # references them again — Spec 3 references them by name.
        del installations_table, pr_state_table, evidence_bucket

    def build_task_role_for_installation(
        self,
        installation_id: int,
        *,
        app_private_key_parameter_name: str,
    ) -> iam.Role:
        """Build a per-installation task role scoped by ``LeadingKeys``.

        Called by Spec 3's orchestrator at ``ecs.RunTask`` time to
        build the role that the Fargate task assumes. The role's IAM
        policy scopes every DynamoDB action to rows keyed on
        ``installation_id`` (enforced via the
        ``dynamodb:LeadingKeys`` condition) and every S3 action to the
        ``<installation_id>/*`` prefix. Tenant isolation is enforced
        at the IAM boundary, not the application layer (Invariant 1).

        The full IAM policy JSON lives at design.md §9 (worked example
        for ``installation_id = 12345678``). This method emits the CDK
        equivalent using stringified installation IDs in the
        ``LeadingKeys`` condition — DynamoDB requires ``LeadingKeys``
        values to be strings even when the partition key is numeric.

        Args:
            installation_id: The GitHub App installation ID this role
                will scope permissions to.
            app_private_key_parameter_name: The App_Key_Parameter name
                the runner reads via ``ssm:GetParameter``. The statement
                grants exactly that one action on exactly its
                Parameter_ARN (cost-cuts Req 4.5).

        Returns:
            An :class:`aws_cdk.aws_iam.Role` with the per-installation
            inline policy attached.
        """
        role = iam.Role(
            self,
            f"TrikonVerifyRunnerTaskRole-{installation_id}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            role_name=f"trikon-verify-task-role-{installation_id}",
        )

        # dynamodb:PutItem on trikon_verdicts, scoped by LeadingKeys.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/trikon_verdicts"
                ],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": [str(installation_id)],
                    },
                },
            )
        )

        # dynamodb:GetItem/PutItem/UpdateItem on trikon_pr_state,
        # scoped by LeadingKeys.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/trikon_pr_state"
                ],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": [str(installation_id)],
                    },
                },
            )
        )

        # s3:PutObject/GetObject on the evidence bucket, scoped by
        # per-installation prefix.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[
                    f"arn:aws:s3:::trikon-cloud-evidence/{installation_id}/*"
                ],
            )
        )

        # SsmAppPrivateKeyRead: ssm:GetParameter on exactly the
        # App_Key_Parameter ARN. No ``sid=`` so the synthesized statement
        # byte-matches the Runtime_IAM_Renderer (which drops Sid on the
        # wire). Inline, never ``grant_read``, which would add
        # ssm:DescribeParameters / GetParameters / GetParameterHistory.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[
                    parameter_arn(
                        region=self.region,
                        account=self.account,
                        name=app_private_key_parameter_name,
                    )
                ],
            )
        )

        return role
