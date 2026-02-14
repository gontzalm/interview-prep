FROM python:3.13
COPY --from=ghcr.io/astral-sh/uv /uv /uvx /bin/

# md2pdf requires weasyprint
RUN apt update && apt install -y \
    weasyprint \
    fonts-inter \
    fonts-noto \
    fonts-noto-color-emoji

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --group tools --no-install-project

COPY src/__init__.py ./src/__init__.py
COPY src/tools/ ./src/tools/

CMD ["uv", "run", "fastmcp", "run", "src/tools/main.py:mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
