FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry

WORKDIR /app

# ── Builder ──────────────────────────────────────────────────────────────────
FROM base AS builder

COPY pyproject.toml poetry.lock* ./
RUN poetry install --only=main --no-root

COPY guild_mcp/ guild_mcp/

# ── Production ───────────────────────────────────────────────────────────────
FROM python:3.13-slim AS production

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN groupadd -g 16458 appgroup && \
    useradd -u 16458 -g appgroup -m -d /home/app -s /bin/bash appuser && \
    mkdir -p /home/app/.nal && \
    chown -R appuser:appgroup /home/app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/guild_mcp /app/guild_mcp

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app

USER appuser

# Token cache location (mount a named volume at /home/app/.nal to persist
# across restarts so OAuth device flow only runs once per host).
VOLUME ["/home/app/.nal"]

CMD ["python", "-m", "guild_mcp.server"]
