# One image for the API and the MCP server: they share the workspace and the lock.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
COPY shared/pyproject.toml shared/
COPY mcp/pyproject.toml mcp/
COPY agent/pyproject.toml agent/
COPY api/pyproject.toml api/
RUN mkdir -p shared/ga_core mcp/ga_mcp agent/ga_agent api/ga_api \
 && touch shared/ga_core/__init__.py mcp/ga_mcp/__init__.py agent/ga_agent/__init__.py api/ga_api/__init__.py \
 && uv sync --frozen --no-dev --no-install-workspace

COPY shared shared
COPY mcp mcp
COPY agent agent
COPY api api
RUN uv sync --frozen --no-dev \
 && python -m ga_mcp.warehouse --path /app/data/warehouse.duckdb

RUN useradd --system --uid 10001 app && chown -R app /app/data
USER app
ENV GA_WAREHOUSE=/app/data/warehouse.duckdb
