FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --group research-subagent --no-install-project

COPY src/research_subagent/ ./src/research_subagent/

CMD ["uvicorn", "src.research_subagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
