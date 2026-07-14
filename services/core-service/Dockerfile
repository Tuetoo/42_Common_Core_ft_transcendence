FROM python:3.13-slim AS base
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock* .
RUN uv sync --frozen --no-dev || uv sync --no-dev
COPY shared /app/shared
COPY services/auth-service/ /app/
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]