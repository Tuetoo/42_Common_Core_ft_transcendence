.PHONY: up down logs test test-auth test-core test-ai test-integration lint clean

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

# --- Tests ------------------------------------------------------------
# See tests/README.md for why these are 4 separate invocations, not one.

test: test-auth test-core test-ai
	@echo "Unit tests passed for all 3 services."
	@echo "Run 'make test-integration' separately with docker compose up already running."

test-auth:
	uv run pytest tests/auth_service -v

test-core:
	uv run pytest tests/core_service -v

test-ai:
	uv run pytest tests/ai_service -v

test-integration:
	uv run pytest tests/integration -v -m integration

# --- Lint ---------------------------------------------------------------

lint:
	uv run ruff check shared services
	uv run mypy shared services

clean:
	find . \( \
	    -name "__pycache__" -o \
	    -name ".pytest_cache" -o \
	    -name ".cache" \
	\) -type d -exec rm -rf {} +
	find . -name "*.py[co]" -delete