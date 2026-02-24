FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/

# md2pdf requires weasyprint
RUN apt update && apt install -y \
    weasyprint \
    fonts-inter \
    fonts-noto \
    fonts-noto-color-emoji

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --group tools --no-install-project

COPY src/__init__.py ./src/__init__.py
COPY src/_shared/ ./src/_shared/
COPY src/tools/ ./src/tools/

CMD ["uvicorn", "src.tools.main_a2a:app", "--host", "0.0.0.0", "--port", "8000"]
