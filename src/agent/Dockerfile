FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --group agent --no-install-project

COPY src/__init__.py ./src/__init__.py
COPY src/agent/ ./src/agent/

CMD ["uv", "run", "uvicorn", "src.agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
