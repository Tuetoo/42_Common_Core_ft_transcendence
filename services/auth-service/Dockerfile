FROM python:3.11-slim AS base
# Pinned uv (same version the lockfile is maintained with)
# so image builds are reproducible.
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
# Installed outside /app deliberately: docker-compose.override.yml
# bind-mounts the live source directory onto /app in
# development, which would otherwise hide a venv living under /app.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock* /app/

FROM base AS development
# --frozen only: the build fails when uv.lock is stale instead of
# silently re-resolving dependencies.
RUN uv sync --frozen
COPY shared /app/shared
COPY services/auth-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
EXPOSE 8000
# Trust X-Forwarded-* from the gateway (the service is only
# reachable inside the Docker network).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM base AS production
RUN uv sync --frozen --no-dev
COPY shared /app/shared
COPY services/auth-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
# Run as an unprivileged user. Code and the venv stay
# root-owned (read-only for the app); only /app/data (auth-service avatars:
# AVATAR_STORAGE_DIR=/app/data/avatars, a named volume that inherits this
# ownership when first created) and the home directory are writable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /app/data/avatars \
    && chown -R app:app /app/data
USER app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
