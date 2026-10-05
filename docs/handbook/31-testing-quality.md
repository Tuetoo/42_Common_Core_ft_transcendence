# 31. Testing and code quality

> AlphaMind is checked at five levels. Unit and API tests run per service
> with pytest against a real, throwaway PostgreSQL and Redis
> (testcontainers). One integration test measures RAG recall against the
> running stack. Two end-to-end scripts drive the live stack through the
> gateway like a browser. A smoke check covers features an analysis does
> not touch. Prometheus alert rules have their own unit tests. On top of
> that, all Python code in `services/`, `shared/`, `tests/` and `infra/`
> is linted with flake8 (`make lint`; in CI each service workflow lints
> `services/<svc>`, `shared` and `tests/<svc>`, and the integration
> workflow lints `infra/`), and all code in `shared/` and `services/` is
> type-checked with `mypy --strict`, locally (Makefile) and in CI (GitHub
> Actions). The test files are linted but not type-checked. This chapter
> explains each layer, why it is set up the way it is, how to run it, and
> how to write a new test in the project's style (section 3.0 has the
> code-style rules).

---

## 1. The big picture

```
 fastest, no stack needed                                     needs the running stack
 <-------------------------------------------------------------------------------->

  flake8       mypy --strict       pytest per service        integration      e2e scripts       smoke
  (style,      (types, 4 runs:     tests/auth-service        tests/           e2e_check (core)  smoke_check
   bugs)        shared, auth,      tests/core-service        integration      e2e_tier_check    (ai)
                core, ai)          tests/ai-service          (RAG recall)     (ai)
                                   real Postgres + Redis     real Qdrant,     through the TLS   inside
                                   in throwaway containers   real corpus      gateway, MailHog  ai-service
                                   (testcontainers)

  promtool test rules  (alert rules, in a throwaway Prometheus container)

 Where they run:
   make lint / make mypy / make test-*  ........ your machine (uv)
   .github/workflows/{auth,core,ai}-service-ci.yml  pull requests: flake8, mypy, tests, image build
   .github/workflows/integration-ci.yml ........ infra/scripts/compose pull requests and main:
                                                 flake8 infra, CSP, compose, shell syntax, promtool;
                                                 main only: full stack smoke
   make e2e / make smoke / make test-integration  your machine, against the live stack
```

---

## 2. Concepts you need

| Term | Meaning |
|---|---|
| pytest | The Python test runner. It finds `test_*.py` files and `test_*` functions and runs them. |
| Fixture | A pytest function that prepares something a test needs (a database session, an HTTP client) and cleans up after. A test asks for it by naming it as a parameter. Fixtures in `conftest.py` are available to every test in that folder. |
| Fixture scope | How long a fixture lives: `function` (default, one per test) or `session` (one per pytest run). |
| pytest-asyncio | Plugin that runs `async def` tests on an asyncio event loop. |
| Event loop | The asyncio scheduler that runs coroutines. Database connections belong to the loop that opened them. |
| testcontainers | A library that starts a real Docker container (here `postgres:16-alpine` and `redis:7-alpine`) for the test run and stops it after. |
| Savepoint | A marker inside a database transaction you can roll back to. Used here so each test's changes are undone. |
| monkeypatch | A pytest fixture that replaces an attribute (a function, a setting) for one test and restores it afterwards. |
| Test double / fake | A small object that stands in for a real dependency (an LLM router, a market data provider) and returns scripted answers. |
| mypy `--strict` | Static type checking with all strict options on: every function must be typed, no implicit `Any`. |
| flake8 | The classic Python style checker: PEP 8 style (pycodestyle), likely bugs such as unused imports and undefined names (pyflakes), and overly complex code (mccabe, off by default). |
| Tool pragma | A comment addressed to a tool, not to a reader: `# type: ignore[...]` (mypy), `# noqa: <code>` (flake8), `# pragma: no cover` (coverage). |
| CI | Continuous integration: GitHub Actions runs the checks on every pull request. |
| promtool | Prometheus's command-line tool; `promtool test rules` checks that alert rules fire as expected on synthetic data. |

---

## 3. How it works in AlphaMind

### 3.0 Code style

The rules below apply to all Python code in the repository.

- **No comments and no docstrings.** The only comments are tool pragmas
  (`# type: ignore[...]`, `# noqa: <code>`, `# pragma: no cover`). The
  handbook is the explanation of the code. Text the program needs at run
  time is an explicit string: error messages (for example `default_message` on each `AppError` subclass, see
  [30-shared-library.md](30-shared-library.md)), a script's `--help`
  (`argparse` `description=` and `help=`) and OpenAPI descriptions.
- **No `from __future__ import annotations`.** A forward reference is
  written as a quoted type.
- **flake8 with its default settings and no configuration file** (no
  `.flake8`, `setup.cfg` or `tox.ini`, no `[tool.flake8]`). That means
  79-character lines. A line that cannot be shorter carries
  `# noqa: E501` (for example an f-string piece that cannot be split).
- **Imports.** A name from the standard library is imported by name
  (`from uuid import UUID`, `from decimal import Decimal`,
  `from pathlib import Path`), not as `uuid.UUID`. The imports form one
  block with no blank lines: standard library first, then third-party
  packages together with the service's own top-level modules (`config`,
  `clients`, `ai`, ...), then `domains` and `shared`. Each group is sorted
  and long imports wrap inside parentheses aligned with the opening
  bracket. isort does this with the settings in `[tool.isort]` of
  `pyproject.toml`.
- **Layout.** Code is packed onto as few lines as fit in 79 columns: an
  argument list fills each line before it wraps, and there is no trailing
  comma before a closing bracket on the next line. A dict literal keeps
  one entry per line. yapf does this with the settings in `.style.yapf`.
- **Formatters are helpers, not checks.** CI does not run them. To
  reformat a file, run yapf first and isort second (isort decides the
  import layout), then flake8, and wrap by hand the few places flake8
  still reports:

  ```
  uv tool run --from yapf yapf -i <files>
  uv tool run --from isort isort <files>
  ```
- **`mypy --strict`** on `shared/` and on each service (section 3.10).
- **One exception:** `services/ai-service/ai/llm/mistral_provider.py` is
  kept unchanged by the owner's decision. It keeps its comments,
  docstrings and `from __future__ import annotations`, and flake8 skips it
  (`--extend-exclude mistral_provider.py`). mypy still checks it.

Config files (Makefile, `pyproject.toml`, workflow and compose files,
shell scripts) may carry comments that explain a setting.

There is no pre-commit configuration; the checks run through the
Makefile and in CI only.

### 3.1 Layout of `tests/`

```
tests/
  README.md              how to run everything, and the rules below
  auth-service/          conftest.py, helpers.py, test_accounts.py,
                         test_organizations.py, test_platform.py
  core-service/          conftest.py, helpers.py, test_platform.py,
                         test_portfolio.py, test_realtime.py,
                         test_research.py
  ai-service/            conftest.py, helpers.py, test_agent_loop.py,
                         test_api.py, test_data_agents.py,
                         test_debate_judge.py, test_filings.py,
                         test_graph.py, test_knowledge.py, test_llm.py,
                         test_market_data.py, test_news.py, test_rag.py,
                         test_scripts.py, test_tools.py, test_workflows.py
  integration/           test_rag_recall.py + conftest.py (needs the live stack)
  fixtures/              rag_golden_set.json
  alembic_contract.py    migration checks star-imported by one file per service
  auth_tokens.py         access_token(), auth_headers(), bearer_headers() for tests
```

Each file is a topic. Tests inside a file are grouped in `Test*` classes
by sub-topic, so `grep -n "^class Test" tests/<service>/test_*.py` lists
what a service covers. Where things live:

| Topic | File |
|---|---|
| Registration, login, tokens, sessions, 2FA, OAuth, profile, connections, emails | `tests/auth-service/test_accounts.py` |
| Organizations, admin users, system roles, user state, auth events | `tests/auth-service/test_organizations.py` |
| Internal token, HTTP surface, rate limits, migrations | `tests/auth-service/test_platform.py` |
| Health, internal access, service clients, job health, user state, ownership rules, `e2e_check` helpers, migrations | `tests/core-service/test_platform.py` |
| Notes, documents, research content, document ingestion, API keys, sentiment | `tests/core-service/test_research.py` |
| Portfolio access, transactions, ledger adjustments, snapshots, scheduled jobs, exports | `tests/core-service/test_portfolio.py` |
| Connection manager, WebSockets, notifications, event listener, cross-service events, Yjs collaboration | `tests/core-service/test_realtime.py` |
| Judge, debate, reflection, position sizing, tier replacement, decision review | `tests/ai-service/test_debate_judge.py` |
| Analysis workflow, backtests, review jobs, tier monitoring | `tests/ai-service/test_workflows.py` |
| Smoke and `e2e_tier_check` scripts, operator scripts | `tests/ai-service/test_scripts.py` |
| Analysis repository, service and queue, create and read routes, backtest routes, analytics, internal routes, events, migrations | `tests/ai-service/test_api.py` |

Size (collected tests, which count each parametrized case; 602 test
functions in the 21 unit-test files):

| Suite | Test files | Collected tests | Statement coverage |
|---|---|---|---|
| `tests/auth-service` | 3 | 44 | 74% |
| `tests/core-service` | 4 | 87 | 66% |
| `tests/ai-service` | 14 | 488 | 90% |
| `tests/integration` | 1 | 1 | not measured |

The suite was reduced by removing tests that covered no code line or
branch beyond the others and by merging the rest by topic: tests that
share one setup run their cases as a loop in one test, and each assert
names its case. Each test file passes on its own, and so does
each service's whole suite.

The two modules at the top of `tests/` (`alembic_contract.py`,
`auth_tokens.py`) are imported as `tests.<module>`: every suite's
`conftest.py` puts the repo root on `sys.path`. Each service's
`helpers.py` is imported as `helpers` (`from helpers import register`;
pytest puts the test directory on `sys.path`). `tests/fixtures/` holds
the RAG golden set used by the integration test.

`tests/auth_tokens.py` signs tokens with the suite's own
`config.settings.JWT_SECRET`, exactly as auth-service would:
`user_id, headers = auth_headers(role="admin")`.

### 3.2 Why each service is a separate pytest run

Each service has top-level modules with the **same names** and different
content: `config.py`, `database.py`, `main.py`, `deps.py`. Each
`conftest.py` puts its own service directory near the front of
`sys.path` (right after the repo root, which has no `config.py`). So
`import config` means "this service's config". If two suites ran in one
Python process, `import config` would resolve to whichever service was
imported first, and the other suite would silently test against the wrong
settings and engine. So:

```bash
make test-auth      # uv run pytest tests/auth-service -v
make test-core      # uv run pytest tests/core-service -v
make test-ai        # uv run pytest tests/ai-service -v
make test           # all three, one after another
```

`pyproject.toml` deliberately sets no `testpaths`, and CI runs each suite
in its own job.

### 3.3 What a service `conftest.py` does

Take `tests/auth-service/conftest.py` (core and ai follow the same
pattern):

1. **Paths**: insert `services/auth-service` and the repo root at the
   front of `sys.path`.
2. **`pytest_configure`** (runs once, before any test module is imported):
   start `PostgresContainer("postgres:16-alpine")` and
   `RedisContainer("redis:7-alpine")`, then write their host and port into
   `os.environ` (`POSTGRES_*`, `REDIS_*`). It also sets a test
   `JWT_SECRET` (`test-secret-not-for-production-0123456789`, 41
   characters, the same in all three suites, so no weak-secret warning
   appears), a test `INTERNAL_API_TOKEN`,
   `USER_STATE_CHECK_ENABLED=false` (there is no auth-service to ask), and
   for auth `SMTP_HOST=localhost`, `SMTP_PORT=1`. This must happen before
   `config` is imported, because `get_settings()` is cached and reads the
   environment once. The containers are built in this hook rather than
   at import time, so a missing Docker daemon shows up as one clear error
   when the run starts. `pytest_unconfigure` stops them.
3. **`_schema_ready`** (session scope): creates the service schema
   (auth-service drops it first), imports every model module so
   `Base.metadata` knows all tables, and runs `Base.metadata.create_all`.
   This is faster than running migrations for every session. Afterwards
   it disposes the engine and closes the Redis pools while the event loop
   is still alive (otherwise an "Event loop is closed" warning appears at
   the end of every run).
4. **`db_session`** (per test): opens a connection, begins a transaction,
   and creates an `AsyncSession(..., join_transaction_mode="create_savepoint")`.
   When application code calls `session.commit()`, only a savepoint is
   released; the outer transaction is rolled back at the end of the test.
   Tests never see each other's data and need no cleanup.
5. **`client`** (per test): an `httpx.AsyncClient` over
   `ASGITransport(app=main.app)` (no network; requests go straight into
   the FastAPI app), with `get_db` overridden to yield `db_session`, and
   the `X-Internal-Token` header preset so internal routes work.
6. **`real_client`** (auth and core only; ai-service has none): the same
   without the override; each request gets a
   real session from the pool and commits for real. Needed for tests that
   race two requests (a single shared session cannot race itself), for
   code that opens its own `SessionLocal()` (for example the public API
   middleware), and when a request must be refused and rolled back. Its
   data is **not** rolled back, so use unique names.
7. Helpers (auth): `mock_email` (captures emails), `captured_events`
   (captures events published after the commit, except
   `user.presence.changed`, which every login queues and which has its own
   tests), `state_check` (turns the live role check back on for one test),
   and the autouse `_reset_rate_limits`, which empties the rate-limit
   database before every test (`flushdb` on database 1), because every
   test request comes from the same client address.

core-service and ai-service add `mock_cross_service`, which replaces the
HTTP clients to other services (and, in ai-service, the event publisher)
with fakes. core also has `organization_roles` and `published_events`;
ai has `owners`, `rows`, `workflow_env` and `_portfolio_backend`
(section 7.3). Each service's `helpers.py` holds the shared builders,
fakes and functions its test files use. ai-service also has an autouse `_no_agent_prefetch` that sets
`AGENT_PREFETCH_ENABLED=False`, so no test reaches Yahoo, SEC or FRED
through the agents' prefetch tools (otherwise a test could pass offline
and fail with network access, or the other way round).

There is no switch to use an existing local PostgreSQL instead: every
suite starts its own containers, so **Docker must be running** on the
machine that runs `make test-*`.

### 3.4 Event loop settings (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

- `asyncio_mode = "auto"`: every `async def test_*` runs as an asyncio
  test without `@pytest.mark.asyncio` (some files still add the marker;
  it is harmless).
- Both loop scopes are `session`. `_schema_ready` is session-scoped and
  each service's `database.py` creates its engine once at import time;
  both assume **one event loop for the whole run**. With pytest-asyncio's
  default (a new loop per test), connections opened in one loop are used
  in another and fail with "got Future ... attached to a different loop".
  Setting only one of the two options is worse (the fixture loop and the
  test loop then differ). Both must be `session`.

Also in that section: the `integration` marker and three `filterwarnings`
lines that silence known, harmless warnings, each matched by its exact
message: qdrant-client's server version probe (unit tests build Qdrant
clients without a Qdrant server), Starlette's notice that its `httpx`
TestClient backend is deprecated (used by the core WebSocket tests), and
passlib's use of Python's deprecated `crypt` module (harmless until
Python 3.13). There is deliberately no `testpaths` setting (section
3.2).

### 3.5 The Alembic contract tests

Unit tests build tables with `create_all` (fast). One test file in each
suite (`auth-service/test_platform.py`, `core-service/test_platform.py`,
`ai-service/test_api.py`) star-imports `tests/alembic_contract.py`, which builds a scratch
schema with the **real migrations** (`alembic upgrade head`) and checks it
matches the models exactly (types, nullability, server defaults, indexes,
constraints), that downgrade and upgrade work, that no column has a
default frozen at table-creation time, and that the one-time conversion
of old databases behaves. This catches a model change without a
migration.

### 3.6 Integration test (`tests/integration`)

`tests/integration/test_rag_recall.py` (`@pytest.mark.integration`)
loads `tests/fixtures/rag_golden_set.json` and computes Recall@5 for BM25,
vector and fused retrieval against the **running** Qdrant and its real
corpus. Floors: `_MIN_RECALL_BM25 = 0.87`, `_MIN_RECALL_VECTOR = 0.72`,
`_MIN_RECALL_FUSED = 0.86`. Its `conftest.py` starts no containers; a
session fixture rebuilds the in-memory BM25 index from Qdrant first.

```bash
make test-integration
# = QDRANT_URL=http://127.0.0.1:6333 uv run --env-file .env.docker pytest tests/integration -v -m integration
```

It needs the stack up (Qdrant is reachable on `127.0.0.1:6333`) and an
embedding key, which is why CI does not run it.

### 3.7 End-to-end scripts

**`services/core-service/scripts/e2e_check.py`** runs inside the
core-service container and uses the public API through the TLS gateway
(`https://gateway`, Host `localhost`), WebSockets through
`wss://gateway/ws/...`, and MailHog (`http://mailhog:8025`,
`/api/v2/search`) to pick up verification and invitation links. Sections,
in order:

`platform`, `auth`, `roles`, `org`, `portfolio`, `apikey`, `documents`,
`research`, `realtime`, `yjs`, `analytics`, `backtest`, `analysis` (only
with `--with-analysis`), `admin`, `cleanup`.

`--only a,b` adds the sections they depend on (`SECTION_DEPS`; for
example `yjs` needs `auth` and `org`). It creates its own users
(`e2e-<runid>-<name>@example.com`) and data named `E2E <runid> ...`, and
`cleanup` removes them (skip with `--keep`). Output is one line per check
(`OK`, `FAIL`, `SKIP`) and a `SUMMARY`; exit code 1 if any check failed,
2 for usage errors or a held lock. It needs an admin token in
`E2E_ADMIN_TOKEN`.

**`services/ai-service/scripts/e2e_tier_check.py`** runs inside
ai-service and drives the tier chain with a synthetic SELL recommendation
(targets at 99999, so the daily job never fills them): decision ->
accepted tiers -> one SELL executed through core (sent twice, traded once)
-> one SELL of too many shares refused -> portfolio deleted -> remaining
tier cancelled, checking notifications and the `analysis:<id>` WebSocket
at each step.

Both take the Redis lock in `shared/e2e_lock.py`, so only one run at a
time. The easiest way to run them is the operator script, which makes a
fresh admin token:

```bash
make e2e                    # both
make e2e PART=core ARGS="--only auth,portfolio --verbose"
make e2e PART=tier
```

`scripts/e2e.sh` refuses to run inside the scheduler's busy windows
(09:00-10:30 and 16:55-17:50 New York, 14:00-15:30 and 21:55-22:50
London) unless `FORCE=1`. Remember the auth limits: 10 registrations per
address per hour, and each core run registers three users.

### 3.8 Smoke check

`services/ai-service/scripts/smoke_check.py` (`make smoke`, or
`bash scripts/smoke.sh --only db redis qdrant`) runs inside ai-service and
checks what an analysis batch does not exercise: `db`, `redis`, `qdrant`,
`bm25`, `retrieval`, `kg`, `market_data`, `fx`, `fred`, `core_service`,
`internal_api`, `backtest_metrics`, `backtest_replay`,
`tier_monitoring_dry`, `review`, `queue`, and `llm` (only with
`--with-llm`); 17 checks in `CHECKS`. Its report goes to stdout; service
logs are lowered to WARNING on stderr so they do not mix with it. Each check has its own
timeout; one failure does not stop the rest. Side effects: `market_data`
refreshes AAPL's cached price bars (last 10 days), and `backtest_replay`
creates one scratch backtest and deletes it again (with the SPY benchmark
rows it added, when SPY was not stored before).

### 3.9 Alert rule tests

`infra/prometheus/alert_rules_test.yml` feeds synthetic series (for
example `up{job="core-service"}` going to 0) into the rules of
`alert_rules.yml` and states which alerts must fire with which labels.

```bash
make promtool-test
```

runs `promtool check rules` and `promtool test rules` in the same
Prometheus image the stack uses (`prom/prometheus:v3.13.2`).

### 3.10 mypy `--strict`

Configuration in `pyproject.toml` `[tool.mypy]`: `python_version = "3.11"`,
`strict = true`, `warn_unused_configs`, `warn_return_any`,
`disallow_untyped_defs`, `explicit_package_bases = true`.

Four separate runs (Makefile targets):

```make
mypy-shared:  MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases shared
mypy-auth:    cd services/auth-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .
mypy-core:    cd services/core-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .
mypy-ai:      cd services/ai-service && MYPYPATH=$(CURDIR) uv run mypy --strict --explicit-package-bases .
mypy:         all four
```

Why each piece:

- **`--explicit-package-bases`**: no directory has an
  `__init__.py`. Without this flag mypy infers module names from file
  paths and reports two different files with the same name in different
  folders (for example `ai/tools/base.py` and `ai/llm/base.py`) as a
  "Duplicate module" error that has nothing to do with types.
- **`MYPYPATH` = repo root**: so `import shared.security` is found and
  checked as real code instead of becoming `Any`. It is an absolute path,
  so it cannot be written into `pyproject.toml`; it is set per call.
- **`cd` into each service**: its own same-named files only resolve
  correctly when the service directory is the base. `shared` must **not**
  be checked from inside `shared/`, because then the same file would be
  both `database` and `shared.database`. All
  candidate shapes were tried against all four targets; only this
  combination is clean on every one, so the four lines cannot be
  simplified further. `make lint` runs flake8 and then these four.
- **Stub packages** (dev dependencies in `pyproject.toml`):
  `types-python-jose`, `types-passlib`, `boto3-stubs[s3]` give mypy the
  types of jose, passlib and boto3's S3 client (`shared/storage.py`
  imports `mypy_boto3_s3.client.S3Client` under `TYPE_CHECKING`).
- **No `ignore_missing_imports`**: `pyproject.toml` does not switch
  this option on. So every third-party import must come with types, or
  mypy reports an error. The `[tool.mypy]` section adds nothing hidden:
  `mypy --strict --config-file /dev/null` (strict mode with no config
  file, plus the same `MYPYPATH` and `--explicit-package-bases`) is also
  clean on all four targets.
- **mistral_provider.py**: flake8 skips this file (section 3.0), mypy
  does not; it is checked with the rest of ai-service.
- **Two untyped libraries**: `yfinance` and `bm25s` ship no type
  information and have no stub package. Their three import lines carry
  `# type: ignore[import-untyped]`:
  `services/ai-service/ai/providers/yahoo_finance.py` (`import yfinance as yf`),
  `services/ai-service/ai/rag/retriever.py` and
  `services/ai-service/ai/rag/indexing.py` (`import bm25s`). The error code
  in brackets limits the ignore to that one problem.

### 3.11 flake8

flake8 runs with its default settings; there is no configuration file and
no `[tool.flake8]` section. The defaults are pycodestyle (`E`, `W`, with
the maximum line length of 79 characters) and pyflakes (`F`: unused
imports, undefined names, redefinitions); the mccabe complexity check is
off. flake8's default exclude list skips `.git`, `__pycache__`, `.tox`,
`.nox`, `.eggs` and `*.egg`; the `.venv` folder is never reached because
only the four source folders are passed.

```make
lint:
	uv run flake8 --extend-exclude mistral_provider.py \
	    services shared tests infra
	$(MAKE) mypy
```

- **Folders**: `services` (including migrations and scripts), `shared`,
  `tests` and `infra` (`infra/gateway/csp_hashes.py`).
- **`--extend-exclude mistral_provider.py`**: keeps the default excludes
  and adds the one file the owner keeps unchanged (section 3.0). The
  pattern matches the file name in any folder.
- **Pragmas**: a `# noqa: <code>` names the one error it allows, for
  example `E402` on the imports in each `migrations/env.py` that must come
  after `sys.path` is set, `F401` on imports kept only to register models
  (`db_bootstrap.py`, the test `conftest.py` files), `F401,F403` on the
  `from tests.alembic_contract import *` lines, `E741` on one variable
  name in the Easter formula of `shared/market_calendar.py`, and `E501`
  on a few lines that cannot be wrapped.

`make clean` removes `__pycache__`, `.pytest_cache`, `.mypy_cache` and
`.cache` folders and compiled `.pyc` files.

### 3.12 CI workflows (`.github/workflows/`)

| Workflow | Trigger | Steps |
|---|---|---|
| `auth-service-ci.yml` | pull request touching `services/auth-service/**`, `shared/**`, `tests/auth-service/**`, the shared test helpers (`tests/alembic_contract.py`, `tests/auth_tokens.py`), `Makefile`, `pyproject.toml`, `uv.lock` or the workflow | `uv sync --frozen` (uv 0.8.17); `uv run flake8 --extend-exclude mistral_provider.py services/auth-service shared tests/auth-service`; `make mypy-shared mypy-auth`; `make test-auth` (testcontainers on the runner's Docker); `docker build ... --target production` |
| `core-service-ci.yml` | same, for core, plus `infra/scheduler/**` and `infra/gateway/nginx.conf` (read by its tests) | same with `services/core-service`, `tests/core-service`, `mypy-core`, `test-core` |
| `ai-service-ci.yml` | same, for ai, plus `infra/scheduler/**`, `infra/vector/**`, `scripts/**`, `.env.example` and `.gitignore` (read by `test_scripts.py`) | same with `services/ai-service`, `tests/ai-service`, `mypy-ai`, `test-ai` |
| `integration-ci.yml` ("stack smoke CI"), job `config-checks` | pull request touching `infra/**`, `scripts/**`, the compose files, `Makefile`, `.env.example` or the workflow; push to `main` | `uv run flake8 infra`; `csp_hashes.py --check`; `docker compose config -q` of the stack and of the restore-drill project; `bash -n` / `sh -n` of every shell script; `make promtool-test` |
| `integration-ci.yml`, job `stack-smoke` | push to `main` only | makes `.env.docker` from `.env.example` plus random `GRAFANA_ADMIN_PASSWORD`, `INTERNAL_API_TOKEN`, `JWT_SECRET`; `make certs`; `docker compose --env-file .env.docker -f docker-compose.yaml up -d --build` (the explicit `-f` skips `docker-compose.override.yml`, so CI builds and runs the `production` image stages: non-root user, no bind mounts, no `LOG_LEVEL=DEBUG`); waits up to 5 minutes for `/health/{auth,core,ai}` (on timeout it prints `ps` and the last 100 log lines and fails); checks `http://.../status` = 301, `https://.../status` = 200, `/internal/x` = 404, `/metrics` = 404; `down -v` (always, also after a failure) |

| `deploy.yml` | "stack smoke CI" succeeded for a push to `main` (`workflow_run`), or started by hand | SSH to the cloud host with a key that may only run `scripts/deploy.sh` (fast-forward to `origin/main`, `docker compose up -d --build --wait`); see [../DEPLOYMENT.md](../DEPLOYMENT.md) |

The stack smoke job does not run `tests/integration` (no seeded corpus or
embedding key in CI) despite the file name.

---

## 4. Configuration

| Setting / file | Value | Meaning |
|---|---|---|
| `pyproject.toml` `[tool.pytest.ini_options]` `asyncio_mode` | `auto` | Async tests need no marker. |
| `asyncio_default_fixture_loop_scope`, `asyncio_default_test_loop_scope` | `session`, `session` | One event loop per run (3.4). |
| `markers` | `integration` | Marks tests that need the live stack. |
| `[dependency-groups] dev` | pytest, pytest-asyncio, `testcontainers[postgres]`, flake8, mypy, `types-python-jose`, `types-passlib`, `boto3-stubs[s3]` | Test and quality tools. |
| `[tool.uv] constraint-dependencies` | `grpcio>=1.82.1`, `charset-normalizer>=3.4.9` | Not used directly (they come in through qdrant-client and requests); pinned only to keep uv away from two yanked releases (grpcio 1.82.0 had a bad protobuf dependency, charset-normalizer 3.4.8 a decode regression). |
| `[tool.mypy]` | `strict`, `explicit_package_bases`, ... | 3.10. |
| Test env (set by each `conftest.py`) | `USER_STATE_CHECK_ENABLED=false`, test `JWT_SECRET` and `INTERNAL_API_TOKEN`, container host/ports | Test-only overrides. |
| `Makefile` | `test`, `test-auth`, `test-core`, `test-ai`, `test-integration`, `promtool-test`, `lint`, `mypy`, `mypy-*`, `clean`, `e2e`, `smoke` | Entry points. |

---

## 5. Design decisions and why

**Real PostgreSQL in tests, not SQLite.** The schema uses features SQLite
does not have: a generated `TSVECTOR` column with a GIN index for note
search, a generated `pair_key` column (`LEAST`/`GREATEST`) that prevents
duplicate friend requests, and `SELECT ... FOR UPDATE` row locks for buys
and sells. A suite passing on SQLite would skip exactly the behaviour most
worth testing.

**One transaction per test, rolled back.** Faster than truncating tables
and keeps tests independent. The `real_client` escape hatch exists for
the cases where real commits matter.

**`create_all` for speed, plus one migration contract test.** Running
every migration for every session would be slow; one test per service
proves migrations and models agree.

**Live-state check off by default in tests.** Otherwise every test token
would need a Redis entry. Tests about roles and deactivation turn it on
with `state_check` or `monkeypatch.setattr(settings,
"USER_STATE_CHECK_ENABLED", True)`.

**No network in unit tests.** LLM routers, market data, SEC, FRED and the
other services are replaced by fakes; agent prefetch is off. A test must
give the same result offline and online.

**End-to-end checks as scripts, not pytest.** They run against the live
stack inside the containers, create and clean up their own data, need an
admin token and take minutes. Their pure helper functions are still unit
tested (`TestE2eCheckScript` in `tests/core-service/test_platform.py`,
`TestE2eTierCheck` in `tests/ai-service/test_scripts.py`).

**mypy strict on everything.** The codebase passes data between three
services and many async layers; types catch wrong shapes before runtime.
The two `type: ignore[import-untyped]` lines are the only exceptions for
imports, each limited to one error code.

**flake8 with default settings, no comments in code.** The owner's rule:
the style is the standard one (PEP 8, 79 characters) with nothing to
configure, and the explanation of the code lives in one place, this
handbook, instead of being split between comments and documents. Text the
program shows at run time is an explicit string, so removing comments can
never change behaviour.

---

## 6. When things go wrong

| Symptom | Cause | What to do |
|---|---|---|
| Collection or startup error mentioning Docker | Docker is not running; testcontainers cannot start Postgres/Redis. | Start Docker Desktop, run again. |
| `got Future ... attached to a different loop` | Loop scopes changed, or a fixture creates its own loop. | Keep both loop scopes `session`. |
| `relation "..." does not exist` in a test that opens `SessionLocal()` | It does not use `db_session`, so `_schema_ready` never ran. | Add `@pytest.mark.usefixtures("_schema_ready")`. |
| A test passes alone but fails in the full run | It depends on committed data from another test (`real_client`), or a job that scans every row sees other tests' rows. | Use unique names; limit the job to the test's own rows. |
| A date test fails near midnight UTC | The test and the code read the clock separately. | Pass a fixed time or patch the module's clock. |
| `Duplicate module named ...` from mypy | Wrong directory or missing flag. | Use `make mypy-*`. |
| `Cannot find implementation or library stub` | A new third-party import without types. | Install a stub package, or `# type: ignore[import-untyped]` on that import if none exists. |
| E2E exits with code 2 and names another run | The e2e lock is held. | Wait, or `--break-lock` if that run is gone (`docker compose --env-file .env.docker top core-service`). |
| E2E auth checks get 429 | Registration/login limits for your address. | Wait an hour, or run fewer sections. |
| Integration test skipped | No golden set file. | It lives at `tests/fixtures/rag_golden_set.json`. |

---

## 7. Writing a new test

### 7.1 Choose the suite and the fixtures

- Put the test in the `Test*` class of its topic in
  `tests/<service>/test_<topic>.py` (see the table in 3.1). A new file is
  only for a new area.
- Through HTTP, isolated: `client`. Real commits or races: `real_client`.
- Direct database work: `db_session`.
- Code that opens its own sessions: `@pytest.mark.usefixtures("_schema_ready")`
  and `SessionLocal()`.
- Tokens: `from tests.auth_tokens import auth_headers`.
- Emails (auth): `mock_email`. Published events: `captured_events` (auth),
  `published_events` (core) or the list returned by `mock_cross_service`
  (core, ai).
- Shared builders go in the service's `helpers.py` (or a fixture in its
  `conftest.py`). A test file never imports another test file.
- Several inputs for one behaviour: `pytest.mark.parametrize` with
  readable ids, `pytest.param(..., id="sell-not-held")`.

### 7.2 Example: a reset link works once

```python
# Illustrative test in the style of tests/auth-service/test_accounts.py
import re

from helpers import PASSWORD, register, uid


class TestPasswordReset:
    async def test_reset_link_works_only_once(self, client, mock_email):
        key = uid()
        email = f"reset-{key}@example.com"
        await register(client, mock_email, email, f"reset_{key}")

        asked = await client.post("/api/v1/auth/password-reset/request",
                                  json={"email": email})
        assert asked.status_code == 200
        token = re.search(r"token=([^\s\"'<&]+)",
                          mock_email[-1]["html_body"]).group(1)

        body = {"token": token, "new_password": PASSWORD + "-2"}
        first = await client.post("/api/v1/auth/password-reset/confirm",
                                  json=body)
        assert first.status_code == 200
        second = await client.post("/api/v1/auth/password-reset/confirm",
                                   json=body)
        assert second.status_code == 422
```

What it shows: `register` from `helpers.py` creates and verifies a user
and reads the verification link from `mock_email`; the `client` fixture
shares one rolled-back transaction; background tasks run inside the ASGI
call, so `mock_email` already holds the reset email when `post` returns;
and the 422 proves the link is single use. `uid()` keeps the email unique.

### 7.3 Patterns from `tests/ai-service/test_debate_judge.py`

This file is a good model for ai-service tests. It tests the Judge,
Reflection, the recommendation engine, position sizing and tier
replacement without any LLM or network.

- **Small builders** for state objects (`_state`, `_ok`, `_verdict`,
  `_args` in `helpers.py`): a test states only what matters to it.
- **Fake routers**: `_Router(*contents, repeat=False)` (in the test file)
  has the same `complete(tier, messages, **kwargs)` method as the real
  `LLMRouter`, returns an `LLMResponse` with the next scripted text (or
  raises a scripted exception) and records `calls`, `kwargs` and `tiers`;
  `_RaisingRouter` always raises (for example a 503
  `LLMServerOverloadedError`). They are installed by **monkeypatching the
  name in the module that uses it**:
  `monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)`.
  Patching `ai.llm.router.LLMRouter` itself would not work, because
  `ai/agents/judge.py` already imported the name.
- **Fake providers**: the `sizing_env` fixture replaces
  `get_market_data_provider`, `get_portfolio_holdings` and
  `get_portfolio_cash_balance` in the modules that call them, with values
  from a dict the test can change.
- **Settings for one test**: `monkeypatch.setattr(sizing_module.settings,
  "MAX_POSITION_WEIGHT_PCT", 100.0)`; patching `asyncio.sleep` in the
  module under test records sleeps instead of waiting.
- **`rows` fixture** (`conftest.py`, async, depends on `_schema_ready`):
  a small factory that writes **committed** rows (`analysis(...)`,
  `recommendation(...)` with tiers) through `SessionLocal()` and reads
  tier statuses back with SQL, for code under test that opens its own
  sessions (used in `test_graph.py`).
- **`workflow_env` fixture** (`conftest.py`): patches the analysis
  workflow's side effects (`maybe_remember_trajectory`,
  `archive_agent_report`, `get_portfolio_holdings`) and returns the
  workflow module and the list of captured events from
  `mock_cross_service`. Tests then replace the whole LangGraph graph with
  `_StubGraph(result)` (in `helpers.py`; its `ainvoke` returns a fixed
  final state) and assert on the database rows and the events.
- **Parametrized cases**: one test body, several named cases (for example
  `buy-without-budget`, `sell-not-held` in
  `TestPositionSizing.test_no_tiers_without_calling_the_llm`).

A minimal ai-service test in that style (the imports and the `_Router`,
`_state`, `_ok` and `_args` names are the ones at the top of
`test_debate_judge.py`):

```python
import json

import ai.agents.judge as judge_module
from ai.agents.judge import JudgeAgent


class TestJudge:
    async def test_both_sides_unavailable_skips_the_call(self, monkeypatch):
        router = _Router(json.dumps({"converged": True, "leaning": "buy",
                                     "confidence": 0.8, "rationale": "x"}),
                         repeat=True)
        monkeypatch.setattr(judge_module, "LLMRouter", lambda: router)
        state = _state(
            agent_outputs={AgentRole.MARKET: _ok(AgentRole.MARKET)},
            debate=DebateState(arguments=_args(bull_ok=False, bear_ok=False)),
        )
        verdict = await JudgeAgent().assess_round(state)
        assert verdict.judge_failed
        assert verdict.failure_reason == "debate_unavailable"
        assert router.calls == []
```

### 7.4 Checklist before you commit

```bash
uv run pytest tests/<service>/test_<topic>.py -q   # the new file
make test-<service>                                 # the whole suite
make lint                                           # flake8 + all four mypy runs
```

If you changed `shared/`, run all three suites.

---

## 8. Demo walkthrough

Run on your machine (host), from the repository root, with `uv` installed
(`uv sync` once) and Docker running.

1. **One fast test file.**

   ```bash
   uv run pytest "tests/core-service/test_platform.py::TestE2eCheckScript" -q
   ```

   It starts the Postgres and Redis containers (a few seconds), then runs
   the pure tests of the end-to-end script's helpers (MailHog parsing, link
   extraction, PDF building): 11 test functions, some of them
   parametrized. Point at the startup: "Even this class gets a real
   database, because the suite's `conftest.py` sets it up once per run."
   Expected last line: `N passed` with no failures.

2. **An API test with a real database.**

   ```bash
   uv run pytest "tests/core-service/test_research.py::TestApiKeys" -v
   ```

   Show one test in the editor: it creates a key, uses it on
   `/api/v1/public/...`, and checks 401 for a missing and a wrong key.

3. **mypy for one service.**

   ```bash
   make mypy-auth
   ```

   Expected: `Success: no issues found in <N> source files` (the number
   changes as modules are added or removed). Then `make mypy-shared`, the
   same line for `shared/`. Say:
   "Strict mode: every function typed; `shared` is followed through
   `MYPYPATH`."

4. **Show why the flags matter (optional).**

   ```bash
   (cd services/core-service && uv run mypy --cache-dir /tmp/mypy-demo .)
   ```

   Without `MYPYPATH` mypy cannot find the shared package. It reports
   about 200 errors. Most of them are `Cannot find implementation or library stub for module named
   "shared.logging"  [import-not-found]`. The rest follow from those,
   for example `Class cannot subclass "Base" (has type "Any")`. None of
   them is a real type error. `make mypy-core` on the same code is
   clean.

   The separate `--cache-dir` matters: a cache written by a run without
   `MYPYPATH` can make the next normal run report a false
   `Module "shared" has no attribute "e2e_lock"` error.

5. **flake8.** `uv run flake8 --extend-exclude mistral_provider.py services shared tests infra`:
   no output and exit code 0 means every file passes. To show what it
   reports, run it on the excluded file:
   `uv run flake8 services/ai-service/ai/llm/mistral_provider.py | head`
   (long lines, `E501`).

6. **Alert rule tests** (starts a throwaway Prometheus container):
   `make promtool-test`, showing `SUCCESS` for the rules check and the
   tests.

7. **Live stack checks** (create and delete test data; respect the busy
   windows): `make smoke` (read-mostly; see its side effects in 3.8), then
   `make e2e PART=core ARGS="--only auth,portfolio"` and show the `OK` lines
   and the `SUMMARY`.

---

## 9. Check your understanding

<details>
<summary>1. Why can't <code>pytest tests/</code> run all suites in one process?</summary>

Each service has its own top-level `config`, `database`, `main` and
`deps` modules. In one process `import config` would resolve to one
service only, and the other suite would test against the wrong settings.
</details>

<details>
<summary>2. How does a test stay isolated even though the app calls <code>session.commit()</code>?</summary>

`db_session` binds the session to an outer transaction with
`join_transaction_mode="create_savepoint"`; commits only release
savepoints, and the outer transaction is rolled back after the test.
</details>

<details>
<summary>3. When do you need <code>real_client</code>?</summary>

For truly concurrent requests, for code that opens its own session (for
example the API key middleware), and when a refused request must really
roll back. Its data is committed, so use unique names.
</details>

<details>
<summary>4. Why are both asyncio loop scopes set to <code>session</code>?</summary>

The session fixture and the module-level engine assume one loop for the
whole run; per-test loops make connections from one loop be used in
another.
</details>

<details>
<summary>5. Why does mypy need <code>MYPYPATH</code> and <code>--explicit-package-bases</code>?</summary>

`MYPYPATH` (repo root) lets mypy find `shared.*` as real code;
`--explicit-package-bases` stops same-named files in folders without
`__init__.py` from colliding as duplicate modules.
</details>

<details>
<summary>6. Why is the Judge's router patched on <code>ai.agents.judge</code> and not on the router module?</summary>

`judge.py` imported the name `LLMRouter` into its own namespace; the
agent looks it up there. Patching the original module would leave the
judge's reference unchanged.
</details>

<details>
<summary>7. What does CI run on a pull request that only changes <code>shared/</code>?</summary>

All three service workflows (each watches `shared/**`): flake8, mypy for
shared and the service, the service's tests, and a production image
build.
</details>

<details>
<summary>8. Why are the end-to-end checks scripts and not pytest tests?</summary>

They need the live stack, an admin token and real external services,
take minutes, and create and clean up real data; pytest suites must run
anywhere with only Docker.
</details>

---

## 10. Code map

| File | What it holds |
|---|---|
| `tests/README.md` | How to run each layer; the rules in section 3. |
| `tests/<service>/conftest.py` | Containers, env, `_schema_ready`, `db_session`, `client`, `real_client`, fakes. |
| `tests/<service>/helpers.py` | Shared builders, fakes and functions of one service's tests. |
| `tests/auth_tokens.py` | Test access tokens. |
| `tests/alembic_contract.py` | Migration-versus-model checks, star-imported by `tests/{auth,core}-service/test_platform.py` and `tests/ai-service/test_api.py`. |
| `tests/integration/test_rag_recall.py`, `conftest.py` | RAG recall floors against the live corpus. |
| `tests/ai-service/test_debate_judge.py` | Reference for fake routers and `sizing_env`; `rows` and `workflow_env` are in `conftest.py`. |
| `tests/core-service/test_platform.py` | Includes the unit tests of the e2e script's helpers (`TestE2eCheckScript`). |
| `tests/ai-service/test_scripts.py` | Includes the unit tests of `e2e_tier_check` and the smoke runner. |
| `services/core-service/scripts/e2e_check.py` | Full end-to-end check. |
| `services/ai-service/scripts/e2e_tier_check.py` | Tier chain end-to-end check. |
| `services/ai-service/scripts/smoke_check.py` | Smoke check. |
| `scripts/e2e.sh`, `scripts/smoke.sh` | Operator wrappers (`make e2e`, `make smoke`). |
| `infra/prometheus/alert_rules_test.yml` | promtool tests. |
| `pyproject.toml` | pytest and mypy configuration and dev dependencies (flake8 has no configuration). |
| `.github/workflows/*.yml` | CI. |
| `Makefile` | `test*`, `lint`, `mypy*`, `promtool-test`, `e2e`, `smoke`. |

---

## 11. Related chapters

- [02-operations.md](02-operations.md): the operator scripts and busy
  windows.
- [20-postgresql.md](20-postgresql.md): migrations and the Alembic
  bootstrap the contract tests check.
- [17-ai-rag.md](17-ai-rag.md): what RAG recall measures.
- [27-observability.md](27-observability.md): the alert rules under test.
- [28-email.md](28-email.md): MailHog, used by `e2e_check`.
- [30-shared-library.md](30-shared-library.md): `shared/e2e_lock.py`.
- [32-ci-cd.md](32-ci-cd.md): how CI gates the deployment to the cloud host.
