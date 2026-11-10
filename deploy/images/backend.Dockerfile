# Tessera backend image: API server, workers and scheduler share it (command differs).
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# WeasyPrint (PDF) and psycopg runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
      libffi8 shared-mime-info fonts-dejavu-core libpq5 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev --extra pdf --extra storage || \
    uv sync --no-install-project --no-dev --extra pdf --extra storage

COPY backend/ ./
RUN uv sync --frozen --no-dev --extra pdf --extra storage || uv sync --no-dev --extra pdf --extra storage

RUN useradd --create-home --uid 10001 tessera && chown -R tessera:tessera /app
USER tessera

EXPOSE 8000
CMD ["tessera", "serve", "--host", "0.0.0.0", "--port", "8000"]
