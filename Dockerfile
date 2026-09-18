# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/src \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# WeasyPrint 70.0's documented Debian wheel dependencies, plus a basic font.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        fonts-dejavu-core \
        libharfbuzz-subset0 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS dependency-builder

# Pin the installer independently of the application dependency lock.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /usr/local/bin/

# Dependency metadata is copied before source files so this layer remains cacheable.
COPY pyproject.toml uv.lock ./


FROM dependency-builder AS runtime-dependencies

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project


FROM dependency-builder AS development

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-groups --no-install-project

COPY app ./app
COPY src ./src
COPY storage ./storage

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /app/storage/quotes \
    && chown -R app:app /app/storage/quotes

ENV HOME=/home/app

USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


FROM base AS runtime

COPY --from=runtime-dependencies /opt/venv /opt/venv

# Keep manifests in the runtime image for dependency provenance and diagnostics.
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY src ./src
COPY storage ./storage

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /app/storage/quotes \
    && chown -R app:app /app/storage/quotes

ENV HOME=/home/app

USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
