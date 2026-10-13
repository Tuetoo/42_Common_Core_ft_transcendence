.PHONY: up down logs certs backup restore test test-auth test-core test-ai test-integration promtool-test lint mypy mypy-shared mypy-auth mypy-core mypy-ai clean \
	adminer adminer-stop token e2e analyze tiers tiers-decline health psql redis-cli qdrant objectstore service-logs rebuild snapshots backups backup-check restore-drill probe smoke

# The gateway serves https://localhost:8443 with a
# self-signed certificate; `up` creates it first when it is missing
# (gen-certs.sh is idempotent). Force a new one with
# `sh infra/gateway/gen-certs.sh --force`.
up: certs
	docker compose --env-file .env.docker up --build

certs:
	sh infra/gateway/gen-certs.sh

down:
	docker compose --env-file .env.docker down

logs:
	docker compose --env-file .env.docker logs -f

# Backups and disaster recovery: see docs/DISASTER_RECOVERY.md.
backup:
	docker compose --env-file .env.docker exec scheduler /scripts/backup.sh

# make restore BACKUP=<YYYY-MM-DD_HHMM>            -> dry run (checks only)
# make restore BACKUP=<YYYY-MM-DD_HHMM> CONFIRM=yes -> real restore, then
#                                                    restore.sh --verify
# The real restore stops the app services and the scheduler. Objects go
# back through the S3 API, so objectstore (SeaweedFS) is kept running.
restore:
ifndef BACKUP
	$(error usage: make restore BACKUP=<YYYY-MM-DD_HHMM> [CONFIRM=yes])
endif
ifeq ($(CONFIRM),yes)
	docker compose --env-file .env.docker stop auth-service core-service ai-service scheduler
	docker compose --env-file .env.docker up -d --wait objectstore
	docker compose --env-file .env.docker --profile tools run --rm --build restore /scripts/restore.sh --yes /backups/$(BACKUP)
	docker compose --env-file .env.docker --profile tools run --rm restore /scripts/restore.sh --verify /backups/$(BACKUP)
	docker compose --env-file .env.docker up -d
else
	docker compose --env-file .env.docker --profile tools run --rm --build restore /scripts/restore.sh /backups/$(BACKUP)
endif

# --- Tests ------------------------------------------------------------
# See tests/README.md for why these are 4 separate invocations, not one.

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
	QDRANT_URL=http://127.0.0.1:6333 uv run --env-file .env.docker pytest tests/integration -v -m integration

# Checks the alert rules and runs their unit tests
# (infra/prometheus/alert_rules_test.yml), with promtool from the same
# Prometheus image docker-compose.yaml runs.
PROMETHEUS_IMAGE := prom/prometheus:v3.13.2

promtool-test:
	docker run --rm --entrypoint promtool \
	    -v $(CURDIR)/infra/prometheus:/rules:ro -w /rules $(PROMETHEUS_IMAGE) \
	    check rules alert_rules.yml
	docker run --rm --entrypoint promtool \
	    -v $(CURDIR)/infra/prometheus:/rules:ro -w /rules $(PROMETHEUS_IMAGE) \
	    test rules alert_rules_test.yml

# --- Lint ---------------------------------------------------------------

# Each mypy invocation needs BOTH `--explicit-package-bases` AND
# `MYPYPATH` pointed at the repo root: no directory has an
# `__init__.py`, so mypy's default module-name inference reports
# same-named files (e.g. `ai/tools/base.py` vs `ai/llm/base.py`)
# as a spurious "Duplicate module" error.
# `shared` runs from the repo root; each service must run from
# inside its own directory.
# `shared` must NOT be `cd`-ed into (the same file would then be
# seen as both "database" and "shared.database").
# The four invocations below are not interchangeable.
lint:
	uv run flake8 --extend-exclude mistral_provider.py \
	    services shared tests infra
	$(MAKE) mypy

# One target per mypy invocation so CI can run the relevant ones per
# service (see the notes above `lint`).
mypy: mypy-shared mypy-auth mypy-core mypy-ai

mypy-shared:
	MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases shared

mypy-auth:
	cd services/auth-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .

mypy-core:
	cd services/core-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .

mypy-ai:
	cd services/ai-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .

clean:
	find . \( \
	    -name "__pycache__" -o \
	    -name ".pytest_cache" -o \
	    -name ".mypy_cache" -o \
	    -name ".cache" \
	\) -type d -exec rm -rf {} +
	find . -name "*.py[co]" -delete

# --- Operator shortcuts (scripts/) -----------------------------------------
# Each target runs one script from scripts/; the script's header explains
# it in full. Variables: S = service(s), T = tickers, A = analysis id(s),
# ARGS = extra arguments passed through, BACKUP = backup folder name.

# Adminer (PostgreSQL web UI) on http://127.0.0.1:8080, off by default.
# The link pre-selects System PostgreSQL, Server postgres, the user, the
# database and a schema (Adminer's default login form says MySQL).
ADMINER_URL := http://127.0.0.1:8080/?pgsql=postgres&username=postgres&db=alphamind&ns
adminer:
	docker compose --env-file .env.docker --profile adminer up -d adminer
	@echo "Adminer (enter the POSTGRES_PASSWORD from .env.docker):"
	@echo "  $(ADMINER_URL)=core"
	@echo "Change ns=core to ns=auth or ns=ai for the other schemas (public is empty)."

adminer-stop:
	docker compose --env-file .env.docker --profile adminer stop adminer

# make token WHO=admin|owner|<user-uuid>
token:
	@bash scripts/token.sh $(or $(WHO),admin)

# make e2e [PART=core|tier] [ARGS="--verbose"]
e2e:
	bash scripts/e2e.sh $(or $(PART),all) $(ARGS)

# make analyze T="AAPL MSFT"   (wrap the make call in caffeinate -i for long batches)
analyze:
ifndef T
	$(error usage: make analyze T="TICKER [TICKER ...]")
endif
	bash scripts/run_analysis.sh $(T)

tiers:
	bash scripts/tiers.sh $(or $(ARGS),open)

# make tiers-decline A="<analysis-uuid> [<analysis-uuid> ...]"
tiers-decline:
ifndef A
	$(error usage: make tiers-decline A="<analysis-uuid> ...")
endif
	bash scripts/tiers.sh decline $(A)

health:
	bash scripts/health.sh

psql:
	bash scripts/psql.sh $(ARGS)

redis-cli:
	bash scripts/redis-cli.sh $(ARGS)

qdrant:
	bash scripts/qdrant.sh $(ARGS)

objectstore:
	bash scripts/objectstore.sh $(ARGS)

# make service-logs S=ai-service   (follow; `make logs` follows every service)
service-logs:
ifndef S
	$(error usage: make service-logs S=<service>)
endif
	bash scripts/logs.sh $(S)

# make rebuild S="ai-service"   (RESTART_ONLY=1 for a restart without a build)
rebuild:
ifndef S
	$(error usage: make rebuild S="<service> [service ...]")
endif
	bash scripts/rebuild.sh $(S)

# make snapshots [ARGS="--confirm"]
snapshots:
	bash scripts/snapshots.sh $(ARGS)

backups:
	bash scripts/backups.sh list

# make backup-check [BACKUP=<YYYY-MM-DD_HHMM>]   (restore dry run, default newest)
backup-check:
	bash scripts/backups.sh check $(BACKUP)

# make restore-drill [BACKUP=<YYYY-MM-DD_HHMM>]  (default newest): restores the
# backup into a separate throwaway compose project and verifies it; see
# docs/DISASTER_RECOVERY.md section 6. The live stack is not touched.
restore-drill:
	bash scripts/backups.sh drill $(BACKUP)

# make probe [MODEL=gemini-3.6-flash] [ARGS="--per-key 1"]
probe:
	bash scripts/probe.sh $(MODEL) $(ARGS)

smoke:
	bash scripts/smoke.sh $(ARGS)
