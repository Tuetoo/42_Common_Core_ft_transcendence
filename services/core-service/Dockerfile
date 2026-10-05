FROM python:3.11-slim AS base
# Pinned uv (same version the lockfile is maintained with)
# so image builds are reproducible.
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock* /app/

FROM base AS development
# --frozen only: the build fails when uv.lock is stale instead of
# silently re-resolving dependencies.
RUN uv sync --frozen
COPY shared /app/shared
COPY services/core-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
EXPOSE 8000
# Trust X-Forwarded-* from the gateway (the service is only
# reachable inside the Docker network).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM base AS production
RUN uv sync --frozen --no-dev
COPY shared /app/shared
COPY services/core-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
# Run as an unprivileged user. Code and the venv stay
# root-owned (read-only for the app); only the home directory is writable.
# No /app/data/avatars here -- avatars are auth-service's volume.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app
USER app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
