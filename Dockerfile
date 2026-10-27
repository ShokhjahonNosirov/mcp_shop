FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, in their own layer, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    SHOP_MCP_TRANSPORT=streamable-http \
    SHOP_MCP_HTTP_HOST=0.0.0.0 \
    SHOP_MCP_HTTP_PORT=8000
EXPOSE 8000

CMD ["shop-mcp"]
