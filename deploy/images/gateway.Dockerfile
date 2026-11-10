# Tessera AI gateway image.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app/gateway
COPY gateway/pyproject.toml gateway/uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev
COPY gateway/ ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

RUN useradd --create-home --uid 10002 gateway && chown -R gateway:gateway /app
USER gateway

EXPOSE 8100
CMD ["gateway", "serve", "--host", "0.0.0.0", "--port", "8100"]
