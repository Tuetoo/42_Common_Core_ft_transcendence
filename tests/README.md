# Tests

## Layout

```
tests/
  auth-service/     conftest.py, helpers.py, test_accounts.py,
                    test_organizations.py, test_platform.py
  core-service/     conftest.py, helpers.py, test_platform.py,
                    test_portfolio.py, test_realtime.py, test_research.py
  ai-service/       conftest.py, helpers.py, test_agent_loop.py, test_api.py,
                    test_data_agents.py, test_debate_judge.py,
                    test_filings.py, test_graph.py, test_knowledge.py,
                    test_llm.py, test_market_data.py, test_news.py,
                    test_rag.py, test_scripts.py, test_tools.py,
                    test_workflows.py
  integration/      test_rag_recall.py: RAG recall against the running
                    stack and its real corpus (sets sys.path and rehydrates
                    BM25 itself)
  fixtures/         rag_golden_set.json
  alembic_contract.py   migration-versus-model checks (see below)
  auth_tokens.py        access_token(), auth_headers(), bearer_headers()
```

The unit suites hold 54 (auth), 102 (core) and 414 (ai) collected tests,
570 in total, from 558 test functions in 21 files: 3 (auth), 4 (core) and
14 (ai), plus the 4 alembic_contract.py checks, which every suite
collects. With the one integration test, the repository has 563 test
functions. Coverage of the service code is about 75% (auth), 66% (core)
and 90% (ai); coverage of `shared/` is 78%, 80% and 77% in the three
runs. Tests
are grouped by topic into `Test*` classes; one test covers one feature or
flow, with several steps where they share setup. Each
test file passes on its own, and so does each service's whole suite.

`alembic_contract.py` holds the checks that a schema built by the real
Alembic migrations equals the models (compared by its `differences()`), that downgrade and upgrade work, that
no model default is a quoted function call, and that a schema with tables
but no Alembic revision is refused. It is star-imported
by `auth-service/test_platform.py`, `core-service/test_platform.py` and
`ai-service/test_api.py`, so each suite runs it against its own schema.

`auth_tokens.py` and `alembic_contract.py` are imported as
`tests.<module>`: every suite's `conftest.py` puts the repo root on
`sys.path`. Both files start with `# pyright: reportMissingImports=false`:
their `config` and `database` imports resolve to the service of whichever
suite imports them, which the editor cannot know. The `[tool.pyright]`
section of `pyproject.toml` gives the editor the same import paths as the
conftests (import resolution only; mypy is the type checker).

## Running

```bash
make test-auth          # uv run pytest tests/auth-service -v
make test-core          # uv run pytest tests/core-service -v
make test-ai            # uv run pytest tests/ai-service -v
make test               # the three unit suites, one after another
make test-integration   # needs the running stack (make up) and an
                        # embedding key; talks to Qdrant on 127.0.0.1:6333
```

Docker must be running: every unit suite starts its own Postgres and Redis
containers. To run one file: `uv run pytest tests/core-service/test_portfolio.py -q`.

### Why each service is a separate `pytest` run

The three services have top-level modules with the same names and
different content: `config.py`, `database.py`, `main.py`, `deps.py`. Each
service's `conftest.py` puts only its own service directory on `sys.path`,
so `import config` means that service's config. In one process, whichever
service was imported first would win and the other suite would silently
run against the wrong settings and engine. (The `domains.*` subpackages
have distinct names and do not collide.) CI runs each service's tests as
its own job for the same reason.

## Database: real Postgres and Redis, not SQLite or mocks

Each unit suite's `conftest.py` starts throwaway Postgres and Redis
containers (testcontainers) once per session. The schema uses
Postgres-only features that SQLite cannot reproduce: the generated
`TSVECTOR` column with a GIN index on `research_notes.search_vector`, the
generated `connections.pair_key` column (`LEAST`/`GREATEST`), and
`SELECT ... FOR UPDATE` row locks in portfolio buy and sell. Tables are
created once with `create_all` from the models (fast); the Alembic
contract tests build a separate scratch schema with the real migrations.

Each test runs in its own transaction
(`AsyncSession(..., join_transaction_mode="create_savepoint")`) that is
rolled back afterwards, so tests do not see each other's data.

## Fixtures (`conftest.py`) and helpers (`helpers.py`)

Fixtures every suite has:

- `_schema_ready` (session): creates the schema. A test that does not use
  `db_session` but opens its own sessions (`SessionLocal()`, an event
  handler under test) must request it:
  `@pytest.mark.usefixtures("_schema_ready")`.
- `db_session`: the rolled-back session.
- `client`: HTTP client over the ASGI app; every request shares the test's
  session and transaction.

Fixtures per service:

- auth: `real_client`, `mock_email`, `captured_events`, `state_check`, and
  the autouse `_reset_rate_limits`.
- core: `real_client`, `mock_cross_service`, `organization_roles`,
  `published_events`.
- ai: `mock_cross_service`, `owners`, `rows` (writes committed rows through
  `SessionLocal()`), `workflow_env` (patches the analysis workflow's side
  effects), `_portfolio_backend`, and the autouse `_no_agent_prefetch`
  (no network from agent prefetch tools).

`real_client` gives each request a real session from the pool and commits
for real. Use it only when a test needs that: racing two requests (the
`pair_key` race, the row-locking race), code that opens its own session,
or a request that must be refused and rolled back. Its data is not rolled
back, so use distinctive names. A job that scans every row of a table sees
other tests' committed rows too: limit it to the test's own rows. Code
that reads the clock twice fails across UTC midnight: pass a fixed time or
patch the module's clock.

`helpers.py` holds the shared builders, fakes and small functions of one
service (for example `register`, `login`, `make_user` for auth;
`create_portfolio` for core; scripted routers, state builders and fake
providers for ai). Test files import them with `from helpers import ...`
(pytest puts the test directory on `sys.path`).

`USER_STATE_CHECK_ENABLED` (the live role and active check) is set to
`false` by every `conftest.py`. Tests of roles, deactivation and guests
turn it on for one test with
`monkeypatch.setattr(settings, "USER_STATE_CHECK_ENABLED", True)`
(auth has the `state_check` fixture).

## Conventions

- No test file imports another test file. Shared code lives in
  `helpers.py` or `conftest.py` of the same service, or in the two modules
  at the top of `tests/`.
- Parametrize cases get readable ids (`pytest.param(..., id="sell-not-held")`).
- A test that needs the database requests `db_session`, `client` or
  `real_client`, or `_schema_ready` when it opens its own sessions.
- No network: LLM routers, market data, SEC, FRED and other services are
  test doubles. Providers are replaced by monkeypatching the name in the
  module that uses it.
- Add a case to the test class of its topic; add a new file only for a new
  area.

## Manual checks against the running stack

`make up` (after `cp .env.example .env.docker`) starts everything behind the
TLS gateway. The public base URL is **`https://localhost:8443`** (self-signed
development certificate, so use `curl -k` or trust
`infra/gateway/certs/server.crt`); `http://localhost:8000` only redirects to
it. For example:

```bash
curl -k https://localhost:8443/health/auth
curl -k https://localhost:8443/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email": "you@example.com", "password": "..."}'
```

WebSocket clients connect to `wss://localhost:8443/ws/...`. The status page is
https://localhost:8443/status and Grafana is https://localhost:8443/grafana/.
`make test-integration` talks to Qdrant on `http://127.0.0.1:6333` (bound to
the loopback interface only).

## End-to-end check of the running stack (`scripts/e2e_check.py`)

`services/core-service/scripts/e2e_check.py` exercises the whole running
stack through the gateway like a browser (`https://gateway`, Host
`localhost`, self-signed certificate; `wss://gateway/ws/...`; MailHog at
`http://mailhog:8025` for the verification and invitation emails). It
creates its own users (`e2e-<runid>-{alice,bob,gina}@example.com`) and
data (names start with `E2E <runid>`), checks the sections `platform`,
`auth`, `roles`, `org`, `portfolio`, `apikey`, `documents`, `research`,
`realtime`, `yjs`, `analytics`, `backtest`, `analysis` (only with
`--with-analysis`), `admin`, and finally `cleanup` removes everything it
created and soft-deletes its users (`--keep` skips that). It never
changes anything it did not create.

The section `deletion` is opt-in (`bash scripts/e2e.sh core --only
deletion --verbose`); the default run skips it. It registers one more
throwaway user (`e2e-<runid>-dora@example.com`), gives it a private
document, note with comment, portfolio, watchlist, API key and a
one-member organization with an open invitation, deletes it with the
admin route, and checks that login, the old token and the API key fail,
that the organization and the invitation are gone in auth, and that no
core row and no S3 object of the user is left. Then it calls the two
nightly deleted-user sweeps (core and ai) and expects `failed=0` with no
`lookup_failed` or `refused`. Data of a deleted account is removed by the
deletion itself, so `cleanup` does not try to remove it again.

It needs an access token of your admin account (2 hours):

```bash
TOKEN=$(docker compose --env-file .env.docker exec -T auth-service python -c \
  "from datetime import timedelta; from security import _create_token; print(_create_token('<ADMIN_USER_ID>', timedelta(hours=2), 'access', {'role': 'admin'}))")
docker compose --env-file .env.docker exec -T -e E2E_ADMIN_TOKEN="$TOKEN" \
  core-service python -m scripts.e2e_check [--with-analysis] [--keep] \
  [--only SECTION[,SECTION]] [--verbose]
```

Output is one line per check (`OK` / `FAIL` / `SKIP`) and a `SUMMARY`
line; the exit code is 1 when a check failed. `--only` adds the
sections a section depends on (for example `--only yjs` also runs `auth`
and `org`). Checks that need the LLM or the embedding provider
(document ingestion and RAG, note sentiment, the analysis) fail with a
clear message when those are not configured. auth-service allows 10
registrations per client address per hour, so at most three runs per
hour. The pure helpers are unit-tested in
`tests/core-service/test_platform.py` (`TestE2eCheckScript`).

Only one end-to-end run at a time. `e2e_check` and
`e2e_tier_check` (below) share a Redis lock (`shared/redis.py`, `acquire_e2e_lock`); a
second run started meanwhile exits with code 2 and names the run holding
the lock. A killed run's lock expires after 45 minutes, or pass
`--break-lock`. Both scripts check the admin token first and exit with
code 2 before creating anything when it is expired or not an admin's.
Ctrl+C on `docker compose exec -T` stops only the local docker client: the
script keeps running inside the container (and keeps the lock) until it
ends. `docker compose --env-file .env.docker top core-service` (or
`ai-service`) shows whether `scripts.e2e_check` / `scripts.e2e_tier_check`
is still running; use `--break-lock` only when it is not.

### Tier chain (`services/ai-service/scripts/e2e_tier_check.py`)

`e2e_check --with-analysis` only reaches the tier chain when the LLM
answers BUY or SELL. `e2e_tier_check` runs it deterministically inside
the ai-service container, as the admin account, with a synthetic SELL
recommendation (targets far above any real price, so the daily
tier-monitoring job never fills them): "followed" decision -> tiers
accepted -> one SELL executed through core with the tier-monitoring
job's own execution code (and sent again: no second trade) -> one SELL of
more shares than held refused by core (`execution_failed`) -> portfolio
deleted -> the remaining tier cancelled (`portfolio_deleted`), with the
notification and the `analysis:<id>` WebSocket event of each step (the
socket's first message must be the `analysis.status` snapshot). The
cleanup deletes the synthetic analysis rows; `--keep` keeps them.

```bash
docker compose --env-file .env.docker exec -T -e E2E_ADMIN_TOKEN="$TOKEN" \
  ai-service python -m scripts.e2e_tier_check [--keep] [--verbose]
```

Not while the scheduler's tier monitoring (17:00 New York time) or market
close (17:30) runs. Its database half is tested in
`tests/ai-service/test_scripts.py` (`TestE2eTierCheck`).
