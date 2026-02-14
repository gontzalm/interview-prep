import textwrap
from pathlib import Path
from string import Template
from typing import final

import aws_cdk as cdk
import aws_cdk.aws_cognito as cognito
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

BASE_DIR = Path(__file__).parent
DOCKERFILES_DIR = BASE_DIR / "dockerfiles"


@final
class LwaLambdaFunction(Construct):
    """AWS Lambda Web Adapter construct for deploying web frameworks as Lambda functions.

    Args:
        scope: CDK construct scope.
        id: Construct identifier.
        use_apigw: Whether to front the Lambda with API Gateway + Cognito authorizer.
        streaming_response: Whether to enable response streaming.
        uv_group: The uv dependency group to install.
        app_module: The ASGI app module path (e.g. ``src.agent.main:app``).
        memory: Lambda memory in MB.
        timeout: Lambda timeout duration.
        environment: Environment variables for the Lambda function.
        cognito_authorizer_pool: Cognito user pool for API Gateway authorizer.
        cors_allow_origins: Allowed CORS origins.
    """

    _DOCKERFILE_TEMPLATE = Template(
        textwrap.dedent("""\
            FROM python:3.13
            COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
            COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

            ENV AWS_LWA_PORT=8000

            WORKDIR /var/task

            COPY pyproject.toml uv.lock ./
            RUN uv sync --group ${uv_group} --no-install-project

            COPY ${copy_dirs}

            CMD [${cmd}]
        """)
    )

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        use_apigw: bool = False,
        streaming_response: bool = False,
        uv_group: str,
        src_dirs: list[str],
        app_module: str,
        app_dir: str | None = None,
        memory: int = 256,
        timeout: cdk.Duration = cdk.Duration.seconds(30),
        environment: dict[str, str] | None = None,
        cognito_authorizer_pool: cognito.UserPool | None = None,
        cors_allow_origins: list[str] | None = None,
    ) -> None:
        super().__init__(scope, id)

        env = environment or {}

        if streaming_response:
            env["AWS_LWA_INVOKE_MODE"] = "RESPONSE_STREAM"

        if cors_allow_origins:
            env["CORS_ALLOW_ORIGINS"] = ",".join(cors_allow_origins)

        # Generate Dockerfile
        DOCKERFILES_DIR.mkdir(parents=True, exist_ok=True)

        copy_lines = "\n".join(f"COPY {d} ./{d}" for d in src_dirs)

        cmd_parts = ["uv", "run", "uvicorn", "--port", "8000"]
        if app_dir:
            cmd_parts.extend(["--app-dir", app_dir])
        cmd_parts.append(app_module)
        cmd = ", ".join(f'"{p}"' for p in cmd_parts)

        dockerfile_path = DOCKERFILES_DIR / f"{id}.Dockerfile"
        dockerfile_content = self._DOCKERFILE_TEMPLATE.safe_substitute(
            {
                "uv_group": uv_group,
                "copy_dirs": copy_lines,
                "cmd": cmd,
            }
        )
        _ = dockerfile_path.write_text(dockerfile_content)

        project_root = BASE_DIR.parent

        self.function = lambda_.DockerImageFunction(
            scope,
            f"{id}-function",
            code=lambda_.DockerImageCode.from_image_asset(
                str(project_root),
                file=str(dockerfile_path.relative_to(project_root)),
            ),
            architecture=lambda_.Architecture.ARM_64,  # pyright: ignore[reportAny]
            memory_size=memory,
            timeout=timeout,
            environment=environment,
        )

        if use_apigw:
            lambda_integration = apigw.LambdaIntegration(self.function)  # pyright: ignore[reportArgumentType]

            self.apigw = apigw.RestApi(
                scope,
                f"{id}-apigw",
                default_cors_preflight_options=apigw.CorsOptions(
                    allow_origins=cors_allow_origins or ["*"],
                    allow_credentials=True,
                ),
            )

            _ = self.apigw.root.add_proxy(
                default_integration=lambda_integration,
                default_method_options=apigw.MethodOptions(
                    authorizer=apigw.CognitoUserPoolsAuthorizer(
                        self,
                        f"{id}-cognito-authorizer",
                        cognito_user_pools=[cognito_authorizer_pool],
                    ),
                )
                if cognito_authorizer_pool is not None
                else None,
            )
        else:
            # Use Function URL directly
            invoke_mode = (
                lambda_.InvokeMode.RESPONSE_STREAM
                if streaming_response
                else lambda_.InvokeMode.BUFFERED
            )
            self.function_url = self.function.add_function_url(
                auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
                invoke_mode=invoke_mode,
            )

    def grant_invoke_url(self, grantee: iam.IGrantable) -> None:
        """Grant permission to invoke this function's URL."""
        _ = self.function.grant_invoke(grantee)


@final
class GithubActionsDeployRole(iam.Role):
    """IAM role for GitHub Actions OIDC-based deployments."""

    _PROVIDER_URL = "token.actions.githubusercontent.com"

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        repo: str,
        role_name: str = "GithubActionsDeployRole",
    ) -> None:
        account = cdk.Stack.of(scope).account

        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            scope,
            f"{id}-provider",
            f"arn:aws:iam::{account}:oidc-provider/{self._PROVIDER_URL}",
        )

        super().__init__(
            scope,
            id,
            role_name=role_name,
            assumed_by=iam.FederatedPrincipal(  # pyright: ignore[reportArgumentType]
                provider.open_id_connect_provider_arn,
                {
                    "StringLike": {f"{self._PROVIDER_URL}:sub": f"repo:{repo}:*"},
                    "StringEquals": {f"{self._PROVIDER_URL}:aud": "sts.amazonaws.com"},
                },
                "sts:AssumeRoleWithWebIdentity",
            ),
            description="Role assumed by GitHub Actions to deploy the stack",
        )

        _ = self.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:aws:iam::{account}:role/cdk-{cdk.DefaultStackSynthesizer.DEFAULT_QUALIFIER}-*"  # pyright: ignore[reportAny]
                ],
            )
        )
