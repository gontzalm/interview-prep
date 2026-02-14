FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --group research-subagent --no-install-project

COPY src/research_subagent/ ./src/research_subagent/

CMD ["uv", "run", "uvicorn", "src.research_subagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
