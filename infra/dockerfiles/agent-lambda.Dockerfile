FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_PORT=8000

WORKDIR /var/task

COPY pyproject.toml uv.lock ./
RUN uv sync --group agent --no-install-project

COPY COPY src/__init__.py ./src/__init__.py
COPY src/agent ./src/agent

CMD ["uv", "run", "uvicorn", "--port", "8000", "src.agent.main:app"]
