import secrets

import aws_cdk as cdk
import aws_cdk.aws_bedrock as bedrock
import aws_cdk.aws_cloudfront as cloudfront
import aws_cdk.aws_cloudfront_origins as cloudfront_origins
import aws_cdk.aws_cognito as cognito
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_ecs as ecs
import aws_cdk.aws_ecs_patterns as ecs_patterns
import aws_cdk.aws_iam as iam
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

from .cdk_constructs import BASE_DIR, GithubActionsDeployRole, LwaLambdaFunction


def to_us_inference_profile(model: bedrock.FoundationModel) -> str:
    return "us." + model.model_arn.partition("/")[2]


class InterviewPrepStack(cdk.Stack):
    """Interview Prep Agent infrastructure stack.

    Args:
        scope: CDK app scope.
        id: Stack identifier.
        local_dev: If True, deploys only shared resources (S3, DynamoDB, Cognito)
            with destroy policies. If False, also deploys Lambda functions and
            ECS Fargate with retain policies.
        env: AWS environment (account + region).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        local_dev: bool = False,
        env: cdk.Environment,
    ) -> None:
        super().__init__(scope, id, env=env)

        removal_policy = (
            cdk.RemovalPolicy.DESTROY if local_dev else cdk.RemovalPolicy.RETAIN
        )

        # ---- S3 BUCKETS ----

        chainlit_bucket = s3.Bucket(
            self,
            "chainlit-bucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=removal_policy,
            auto_delete_objects=local_dev,
        )

        storage_bucket = s3.Bucket(
            self,
            "storage-bucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=removal_policy,
            auto_delete_objects=local_dev,
        )

        # ---- DYNAMODB ----

        chainlit_table = dynamodb.Table(
            self,
            "chainlit-table",
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal_policy,
        )
        chainlit_table.add_global_secondary_index(
            index_name="UserThread",
            partition_key=dynamodb.Attribute(
                name="UserThreadPK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="UserThreadSK",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.INCLUDE,
            non_key_attributes=["id", "name"],
        )

        # ---- BEDROCK MODELS ----

        agent_model = bedrock.FoundationModel.from_foundation_model_id(
            self,
            "agent-model",
            bedrock.FoundationModelIdentifier(
                "anthropic.anthropic.claude-haiku-4-5-20251001-v1:0"
            ),
        )

        research_subagent_model = bedrock.FoundationModel.from_foundation_model_id(
            self,
            "research-subagent-model",
            bedrock.FoundationModelIdentifier("anthropic.claude-opus-4-6-v1"),
        )

        # ---- SECRETS MANAGER ----

        tavily_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "tavily-secret",
            "subagent/tavily/api-key",
        )

        # ---- COGNITO ----

        user_pool = cognito.UserPool(
            self,
            "user-pool",
            sign_in_aliases=cognito.SignInAliases(email=True),
            removal_policy=removal_policy,
        )

        if local_dev:
            # Create pre-approved test user for local development
            _ = cognito.CfnUserPoolUser(
                self,
                "test-user",
                user_pool_id=user_pool.user_pool_id,
                username="go.monasterio@gmail.com",
                desired_delivery_mediums=["EMAIL"],
                force_alias_creation=False,
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value="test@example.com"
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
            )

        user_pool_domain = user_pool.add_domain(
            "user-pool-domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"interview-prep-{self.account}"
            ),
        )

        # ---- PRODUCTION-ONLY RESOURCES ----

        if not local_dev:
            # Lambda: Main Backend Agent (streaming, API Gateway + Cognito)
            agent_lambda = LwaLambdaFunction(
                self,
                "agent-lambda",
                use_apigw=True,
                streaming_response=True,
                uv_group="agent",
                src_dirs=["src/__init__.py", "src/agent"],
                app_module="src.agent.main:app",
                memory=512,
                timeout=cdk.Duration.minutes(5),
                cognito_authorizer_pool=user_pool,
                cors_allow_origins=["http://localhost:8000"],
                environment={
                    "AGENT_MODEL": to_us_inference_profile(agent_model),
                },
            )
            agent_lambda.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[agent_model.model_arn],
                )
            )

            # Lambda: MCP Server (no API GW, IAM-protected Function URL)
            mcp_lambda = LwaLambdaFunction(
                self,
                "mcp-lambda",
                use_apigw=False,
                streaming_response=False,
                uv_group="tools",
                src_dirs=["src/__init__.py", "src/tools"],
                app_module="src.tools.main:app",
                memory=512,
                timeout=cdk.Duration.minutes(5),
                environment={
                    "STORAGE_BUCKET": storage_bucket.bucket_name,
                },
            )
            _ = storage_bucket.grant_read_write(mcp_lambda.function)

            # Lambda: Research Subagent (no API GW, IAM-protected Function URL)
            subagent_lambda = LwaLambdaFunction(
                self,
                "subagent-lambda",
                use_apigw=False,
                streaming_response=False,
                uv_group="research-subagent",
                src_dirs=["src/research-subagent"],
                app_module="main:app",
                app_dir="src/research-subagent",
                memory=1024,
                timeout=cdk.Duration.minutes(10),
                environment={
                    "RESEARCH_SUBAGENT_MODEL": to_us_inference_profile(
                        research_subagent_model
                    ),
                    "TAVILY_SECRET": tavily_secret.secret_name,
                },
            )
            _ = tavily_secret.grant_read(subagent_lambda.function)
            subagent_lambda.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[research_subagent_model.model_arn],
                )
            )

            # Cross-service IAM grants
            mcp_lambda.grant_invoke_url(agent_lambda.function)
            subagent_lambda.grant_invoke_url(mcp_lambda.function)

            # Wire MCP URL and Subagent URL into the agent and MCP respectively
            agent_lambda.function.add_environment(
                "MCP_URL", mcp_lambda.function_url.url
            )
            mcp_lambda.function.add_environment(
                "RESEARCH_SUBAGENT_URL", subagent_lambda.function_url.url
            )

            # ---- ECS FARGATE (Chainlit) ----

            chainlit_app_dir = BASE_DIR.parent / "ui"

            chainlit_fargate = ecs_patterns.ApplicationLoadBalancedFargateService(
                self,
                "chainlit-app-fargate",
                task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                    image=ecs.ContainerImage.from_asset(str(chainlit_app_dir)),
                    container_port=8000,
                ),
                cpu=1024,
                memory_limit_mib=2048,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                listener_port=8000,
                open_listener=False,
            )
            chainlit_fargate.load_balancer.connections.allow_from(
                ec2.PrefixList.from_lookup(
                    self,
                    "cloudfront-prefix-list",
                    prefix_list_name="com.amazonaws.global.cloudfront.origin-facing",
                ),
                ec2.Port.tcp(8000),
            )
            _ = chainlit_bucket.grant_read_write(
                chainlit_fargate.task_definition.task_role
            )
            _ = chainlit_table.grant_read_write_data(
                chainlit_fargate.task_definition.task_role
            )

            # CloudFront distribution
            chainlit_distribution = cloudfront.Distribution(
                self,
                "chainlit-app-distribution",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=cloudfront_origins.LoadBalancerV2Origin(
                        chainlit_fargate.load_balancer,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=8000,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            )

            # Cognito client for production (includes CloudFront callback)
            chainlit_client = user_pool.add_client(
                "chainlit-app-client",
                generate_secret=True,
                o_auth=cognito.OAuthSettings(
                    scopes=[
                        cognito.OAuthScope.OPENID,
                        cognito.OAuthScope.PROFILE,
                        cognito.OAuthScope.EMAIL,
                    ],
                    callback_urls=[
                        "http://localhost:8000/auth/oauth/aws-cognito/callback",
                        f"https://{chainlit_distribution.domain_name}/auth/oauth/aws-cognito/callback",
                    ],
                    logout_urls=[
                        "http://localhost:8000/logout",
                        f"https://{chainlit_distribution.domain_name}/logout",
                    ],
                ),
            )

            # Inject environment variables into Fargate container
            for k, v in {
                "CHAINLIT_URL": f"https://{chainlit_distribution.domain_name}",
                "CHAINLIT_AUTH_SECRET": secrets.token_hex(64),
                "OAUTH_COGNITO_CLIENT_ID": chainlit_client.user_pool_client_id,
                "OAUTH_COGNITO_CLIENT_SECRET": chainlit_client.user_pool_client_secret.unsafe_unwrap(),
                "OAUTH_COGNITO_DOMAIN": user_pool_domain.base_url().removeprefix(
                    "https://"
                ),
                "OAUTH_COGNITO_SCOPE": "openid profile email",
                "CHAINLIT_BUCKET": chainlit_bucket.bucket_name,
                "CHAINLIT_TABLE": chainlit_table.table_name,
                "BACKEND_URL": agent_lambda.apigw.url,
            }.items():
                chainlit_fargate.task_definition.default_container.add_environment(k, v)

            _ = cdk.CfnOutput(
                self,
                "chainlit-app-url-output",
                value=f"https://{chainlit_distribution.domain_name}",
            )

            # GitHub Actions deploy role
            _ = GithubActionsDeployRole(
                self,
                "github-actions-deploy-role",
                repo="gontzalm/interview-prep",
            )

            return

        # ---- LOCAL DEV RESOURCES (Cognito client + local.env output) ----

        chainlit_client = user_pool.add_client(
            "chainlit-app-client",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.EMAIL,
                ],
                callback_urls=[
                    "http://localhost:8000/auth/oauth/aws-cognito/callback",
                ],
                logout_urls=[
                    "http://localhost:8000/logout",
                ],
            ),
        )

        _ = cdk.CfnOutput(
            self,
            "local-dotenv-output",
            value="\n".join(
                [
                    "# AUTO-GENERATED by CDK - DO NOT SHARE",
                    "",
                    "# Auth",
                    f"CHAINLIT_AUTH_SECRET={secrets.token_hex(64)}",
                    f"OAUTH_COGNITO_CLIENT_ID={chainlit_client.user_pool_client_id}",
                    f"OAUTH_COGNITO_CLIENT_SECRET={chainlit_client.user_pool_client_secret.unsafe_unwrap()}",
                    f"OAUTH_COGNITO_DOMAIN={user_pool_domain.base_url().removeprefix('https://')}",
                    'OAUTH_COGNITO_SCOPE="openid profile email"',
                    "",
                    "# AWS Resources",
                    f"CHAINLIT_BUCKET={chainlit_bucket.bucket_name}",
                    f"CHAINLIT_TABLE={chainlit_table.table_name}",
                    f"STORAGE_BUCKET={storage_bucket.bucket_name}",
                    f"AGENT_MODEL={to_us_inference_profile(agent_model)}",
                    f"RESEARCH_SUBAGENT_MODEL={to_us_inference_profile(research_subagent_model)}",
                    f"TAVILY_SECRET={tavily_secret.secret_name}",
                    f"COGNITO_USER_POOL_ID={user_pool.user_pool_id}",
                ]
            ),
        )
