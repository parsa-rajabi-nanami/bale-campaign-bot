FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 bot && \
    useradd --uid 10001 --gid bot --create-home --shell /usr/sbin/nologin bot && \
    mkdir -p /app/data /app/config && chown -R bot:bot /app
COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot pyproject.toml uv.lock alembic.ini ./
COPY --chown=bot:bot src ./src
USER 10001:10001
CMD ["python", "-m", "bale_campaign_bot.main"]
