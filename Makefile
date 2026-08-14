.PHONY: up down logs test test-auth test-core test-ai test-integration lint clean

up:
	docker compose --env-file .env.docker up --build

down:
	docker compose --env-file .env.docker down

logs:
	docker compose --env-file .env.docker logs -f

test: test-auth test-core test-ai
	@echo "Unit tests passed for all 3 services."
	@echo "Run 'make test-integration' separately with docker compose up already running."

test-auth:
	uv run pytest tests/auth-service -v

test-core:
	uv run pytest tests/core-service -v

test-ai:
	uv run pytest tests/ai-service -v

test-integration:
	QDRANT_URL=http://localhost:6333 uv run --env-file .env.docker pytest tests/integration -v -m integration

# --- Lint ---------------------------------------------------------------

lint:
	uv run ruff check shared services
	MYPYPATH=$(CURDIR) uv run mypy --explicit-package-bases shared
	MYPYPATH=$(CURDIR) uv run mypy --explicit-package-bases services/auth-service
	cd services/core-service && MYPYPATH=$(CURDIR) uv run mypy --explicit-package-bases .
	cd services/ai-service && MYPYPATH=$(CURDIR) uv run mypy --explicit-package-bases .

clean:
	find . \( \
	    -name "__pycache__" -o \
	    -name ".pytest_cache" -o \
	    -name ".cache" \
	\) -type d -exec rm -rf {} +
	find . -name "*.py[co]" -delete