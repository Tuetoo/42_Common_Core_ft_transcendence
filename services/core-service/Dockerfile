FROM python:3.11-slim AS base
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock* /app/

FROM base AS development
RUN uv sync --frozen || uv sync
COPY shared /app/shared
COPY services/core-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS production
RUN uv sync --frozen --no-dev || uv sync --no-dev
COPY shared /app/shared
COPY services/core-service/ /app/
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]