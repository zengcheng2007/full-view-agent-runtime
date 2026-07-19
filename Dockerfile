FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r agent && useradd -r -g agent -d /app -s /sbin/nologin agent

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY scripts/ scripts/
RUN uv sync --frozen --no-dev && chown -R agent:agent /app

USER agent

ENV FULL_VIEW_RUNTIME_PROFILE=production
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://127.0.0.1:8000/health/live || exit 1

CMD ["uv", "run", "--no-sync", "python", "-m", "full_view_agent.server"]
