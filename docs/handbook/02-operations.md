# 02. Running and operating the stack

> This chapter is the operator's manual. It explains what you need on your
> Mac, how to set up and start AlphaMind the first time, and how to run it
> day to day: the Makefile targets and the operator scripts in `scripts/`,
> the scheduler's timetable and its "busy windows", how to deploy a code
> change (restart or rebuild), how to check health, how to run real
> analyses and end-to-end checks, how backups and restores work, where the
> logs are, and what to do when something breaks. It ends with a complete
> "bring the stack up and show it is healthy" demo.

## 1. The big picture

You operate AlphaMind from a terminal on the host (your Mac), in the
repository folder. You never log in to a container by hand for routine
work. Instead you use three layers of tools, from high to low:

```
  make <target>                     short names, defined in the Makefile
      |
      v
  bash scripts/<name>.sh            operator scripts: one task each, safe defaults,
      |                             busy-window guard, never read .env.docker
      v
  docker compose --env-file .env.docker <command> ...
      |                             (up, ps, logs, exec, restart, run)
      v
  containers: exec python -m scripts.<x> inside a service, psql inside postgres,
              redis-cli inside redis, /scripts/backup.sh inside scheduler, ...
```

A normal day looks like this (times in New York, London in brackets):

```
02:00 (07:00) review-run      02:30 (07:30) backup     03:00 (08:00) rag-ingest
04:00 (09:00) logs-cleanup    04:10 (09:10) purge-unverified  04:20 (09:20) notification-retention
09:00-10:30 (14:00-15:30)  BUSY WINDOW  09:30 (14:30) market-open (Mon-Fri)
16:55-17:50 (21:55-22:50)  BUSY WINDOW  17:00 (22:00) tier-monitoring, 17:30 (22:30) market-close (Mon-Fri)
any other time: your own work -- analyses, e2e checks, restarts, demos
```

The scheduler container runs these jobs by itself. Your jobs as an
operator are to keep the stack up (and the Mac awake), deploy changes at
safe times, check health, and look at logs and backups.

## 2. Concepts you need

| Term | Meaning here |
|---|---|
| `.env.docker` | The real settings file with secrets. Created from `.env.example`, never committed, never printed. Every Compose command needs `--env-file .env.docker`. |
| Makefile target | A short command such as `make health`. `make` runs the lines under that name in the `Makefile`. |
| Operator script | A bash script in `scripts/`. It always runs from the repository root, reaches the stack only through `docker compose --env-file .env.docker`, and never reads `.env.docker` itself (values such as the database user are read from inside the containers). Written for the bash 3.2 that ships with macOS. |
| Busy window | A time span when the scheduler's jobs run (`scripts/lib.sh`). Scripts that restart services, run end-to-end checks or rewrite snapshots refuse to run then, unless `FORCE=1`. |
| Restart vs rebuild | Restart = stop and start the same container (fast; picks up bind-mounted code). Rebuild = build a new image and recreate the container (needed for dependency, Dockerfile or image-copied file changes). |
| Recreate | Delete and create the container again from its image and the current Compose config. Needed to pick up changes in `.env.docker`. |
| Health check | A command Compose runs inside a container; `docker compose ps` shows `(healthy)` or `(unhealthy)`. |
| Heartbeat | After a scheduled job succeeds, it calls core-service `POST /internal/scheduler/heartbeat/<job>`. core-service turns it into a Prometheus gauge (label `scheduled_job`). Alert rules fire when a job has not succeeded for too long (section 3.8). |
| Dry run | A mode that checks and prints what would happen but changes nothing. `restore.sh`, `backup.sh --dry-run`, `snapshots.sh` (without `--confirm`) default to or offer it. |
| Access token | A short-lived JWT that proves "I am user X with role Y". `scripts/token.sh` prints a 2-hour one. |
| Profile | A Compose label that keeps a service out of a normal `up` (`restore` in profile `tools`, `adminer` in profile `adminer`). |
| `caffeinate` | A macOS command that keeps the Mac from sleeping while another command runs. |
| uv | A fast Python package manager. On the host it runs the tests and linters (`uv run pytest`, `uv run flake8`, `uv run mypy`). |

## 3. How it works in AlphaMind

### 3.1 Prerequisites

On the Mac (host):

| Tool | Why | Notes |
|---|---|---|
| Docker Desktop (with the `docker compose` plugin) | Runs every container. | Keep it running. If the Mac sleeps, Docker Desktop's VM pauses too, and scheduled jobs run late or not at all (section 3.14). |
| `make` | The Makefile targets. | Comes with the Xcode Command Line Tools. |
| `bash`, `curl`, `python3` | The operator scripts (`scripts/health.sh` and `scripts/qdrant.sh` use `curl` and parse JSON with `python3`). | `curl` ships with macOS; `python3` comes with the Command Line Tools. |
| `openssl` | `infra/gateway/gen-certs.sh` makes the TLS certificate. | The script works with the LibreSSL that macOS ships as `/usr/bin/openssl`. |
| uv | Host-side tests and linting: `make test-auth`, `make test-core`, `make test-ai`, `make lint`, `make mypy`. | uv installs the Python version the project needs (`requires-python = ">=3.11,<3.12"` in `pyproject.toml`). Not needed just to run the stack. |

### 3.2 First setup

1. **Create the settings file.**
   ```sh
   cp .env.example .env.docker
   ```
   Then edit `.env.docker` in your editor (do not print it in a terminal
   that is being recorded or shared). At minimum:
   - `GRAFANA_ADMIN_PASSWORD`: required; Compose refuses to start without it.
   - `S3_ACCESS_KEY` and `S3_SECRET_KEY`: required; they are the only S3
     identity of SeaweedFS. No newline or tab inside them
     (`infra/objectstore/entrypoint.sh` refuses those).
   - `INTERNAL_API_TOKEN`: a long random string, for example from
     `openssl rand -hex 32`. Empty is accepted only with
     `APP_ENV=development` or `test`, with a warning.
   - `JWT_SECRET`: at least 32 random characters. The placeholder
     `CHANGE_ME` is accepted only in development/test.
   - `TOTP_ENCRYPTION_KEY`: the Fernet key that encrypts stored 2FA
     secrets (generate it with the command in `.env.example`). Required
     outside development/test; empty in development uses a key derived
     from `JWT_SECRET`. Keep it: without it the stored 2FA secrets cannot
     be read.
   - The LLM key(s) for `LLM_DEFAULT_PROVIDER` (`google` in the example:
     `GOOGLE_API_KEY`, optionally `GOOGLE_API_KEY_2` to `_10`), and
     optional data-provider keys (`FRED_API_KEY`, `NEWSAPI_API_KEY`,
     `TAVILY_API_KEY`, `SEC_EDGAR_USER_AGENT`). Agents work with thin
     evidence when a data key is missing.
   - Optional OAuth login: `GOOGLE_CLIENT_ID`/`_SECRET`,
     `GITHUB_CLIENT_ID`/`_SECRET` (blank disables a provider). Register
     the callback `https://localhost:8443/api/v1/auth/oauth/<google|github>/callback`
     with each provider; `OAUTH_REDIRECT_BASE_URL` is the gateway's
     public base URL. Set `FRONTEND_BASE_URL` to the frontend origin
     (`https://localhost:8443` locally): the callback answers 303 to
     `<FRONTEND_BASE_URL>/oauth/complete` with the tokens, a 2FA
     challenge or `error=oauth_failed` in the URL fragment.
   - `QDRANT_API_KEY`: leave empty for the local Qdrant; set it only for
     a Qdrant server that requires a key.
   - If you change `POSTGRES_PASSWORD`, change the password inside
     `DATABASE_URL` too: `shared/config.py::assemble_database_url` uses
     `DATABASE_URL` first when it is set. The PostgreSQL image applies
     `POSTGRES_PASSWORD` only when the `postgres_data` volume is created
     for the first time.
2. **Create the TLS certificate.**
   ```sh
   make certs                      # = sh infra/gateway/gen-certs.sh
   ```
   This writes `infra/gateway/certs/server.crt` and `server.key`
   (self-signed, valid 825 days, for `localhost` and `127.0.0.1`). It does
   nothing if both files already exist; `sh infra/gateway/gen-certs.sh
   --force` makes a new pair.
3. **Build and start everything.**
   ```sh
   make up
   ```
   `make up` runs `make certs` and then
   `docker compose --env-file .env.docker up --build`. It stays attached
   to your terminal and prints every container's log; Ctrl+C stops the
   containers. To start in the background instead:
   ```sh
   sh infra/gateway/gen-certs.sh
   docker compose --env-file .env.docker up -d --build
   ```
4. **Wait for health.** The first start creates the database schemas
   (each service runs its Alembic migrations), the S3 bucket, and the
   Qdrant collection. The service health checks allow 60 s (auth, core)
   and 90 s (ai) of start time; the gateway starts only after all three
   are healthy. Check with `docker compose --env-file .env.docker ps` and
   `make health`.
5. **Open** `https://localhost:8443/status`. The browser warns once about
   the self-signed certificate; accept it for localhost.
6. **Create the first admin.** A new database has no users. Register one
   through the API (`POST /api/v1/auth/register`, for example in Swagger
   at `https://localhost:8443/docs/auth/`), open the verification link in
   MailHog (`http://127.0.0.1:8025`), then give the account the admin
   role (this writes the database and the live role in Redis):
   ```sh
   docker compose --env-file .env.docker exec auth-service python -m scripts.set_system_role --email <email> --role admin --dry-run
   ```
   ```sh
   docker compose --env-file .env.docker exec auth-service python -m scripts.set_system_role --email <email> --role admin
   ```
   The script refuses an unknown email, a deleted account, and demoting
   the last active admin (exit code 1). Tokens already issued get the new
   role at once, because every service reads the live role from Redis.
7. **Point the scripts at your ids.** The default ids in `scripts/lib.sh`
   (`ADMIN_USER_ID`, `OWNER_USER_ID`, `PORTFOLIO_ID`) belong to the
   owner's development database and do not exist on a fresh install, so
   `token.sh admin`, `e2e.sh`, `tiers.sh` and `run_analysis.sh` fail
   there. Export your own ids in the shell first, for example
   `export ADMIN_USER_ID=<uuid of the admin>`, and create a test
   portfolio through the API for `PORTFOLIO_ID` and `OWNER_USER_ID`.
8. **Seed the RAG corpus (writes data, uses embedding quota).** The
   repository holds a seed corpus in `rag_seed_documents/` (folders
   `analyst_report`, `earnings_call`, `filing`, `macro_research`,
   `news`). The ai-service image does not contain it, so copy it into the
   container first, preview, then upload it as system documents (visible
   to every user's searches):
   ```sh
   docker compose --env-file .env.docker cp rag_seed_documents ai-service:/tmp/rag_seed_documents
   ```
   ```sh
   docker compose --env-file .env.docker exec ai-service python -m scripts.upload_seed_documents --as-system --dry-run
   ```
   ```sh
   docker compose --env-file .env.docker exec ai-service python -m scripts.upload_seed_documents --as-system
   ```
   Each file goes through core-service's `POST /internal/documents/system`
   and then the normal ingestion (chunking, embedding, indexing). The
   ticker comes from the filename's leading `<TICKER>_`, the content date
   from a trailing `_YYYY-MM-DD`, the title from the filename;
   `macro_research` files have no ticker. A file whose name any owner
   already uploaded in that category and ticker is skipped
   (`SKIP <file> (already uploaded by another owner)`), because core's own
   duplicate check is per owner only and would otherwise add a second,
   system-owned copy. If that list cannot be read, the file is uploaded
   anyway (core still refuses a duplicate of the same owner). Use
   `--categories filing,news` to upload only some folders.

### 3.3 Starting, stopping, and what survives

| Action | Command | Data kept? |
|---|---|---|
| Start (foreground, with build) | `make up` | yes |
| Start (background, with build) | `docker compose --env-file .env.docker up -d --build` | yes |
| Start (background, no build) | `docker compose --env-file .env.docker up -d` | yes |
| Stop and remove containers | `make down` (= `docker compose --env-file .env.docker down`) | yes: named volumes stay |
| Stop without removing | `docker compose --env-file .env.docker stop` | yes |
| **DESTRUCTIVE: remove volumes too** | `docker compose --env-file .env.docker down -v` | **no: deletes the database, vectors, documents, avatars, Grafana and Prometheus data.** Never in a demo. |

Restarting ai-service, or the whole stack, ends any analysis that is
queued or running. On the next start ai-service marks them `failed` with
`failure_reason = service_restart` (`domains/analysis/queue.py::
fail_stale_analyses_on_startup`).

### 3.4 Makefile targets

All targets live in the repository-root `Makefile`; each operator target
runs one script from `scripts/`, whose header explains it in full.
Variables: `S` = service(s), `T` = tickers, `A` = analysis id(s), `ARGS`
= extra arguments, `BACKUP` = backup folder name, `WHO` = token user,
`PART` = e2e part, `MODEL` = model name.

| Target | Runs | Purpose |
|---|---|---|
| `make up` | `certs`, then `docker compose --env-file .env.docker up --build` | build and start, attached |
| `make certs` | `sh infra/gateway/gen-certs.sh` | create the TLS certificate if missing |
| `make down` | `docker compose ... down` | stop and remove containers (volumes stay) |
| `make logs` | `docker compose ... logs -f` | follow every container's log |
| `make backup` | `docker compose ... exec scheduler /scripts/backup.sh` | backup now (writes `./backups/<stamp>/`) |
| `make restore BACKUP=<stamp>` | restore dry run in the `restore` container | checks only |
| `make restore BACKUP=<stamp> CONFIRM=yes` | **DESTRUCTIVE**: stops auth, core, ai, scheduler, objectstore; restores; `up -d` | replaces current data |
| `make test` / `test-auth` / `test-core` / `test-ai` | `uv run pytest tests/<service> -v` | unit tests on the host (real Postgres/Redis via testcontainers) |
| `make test-integration` | `QDRANT_URL=http://127.0.0.1:6333 uv run --env-file .env.docker pytest tests/integration -v -m integration` | integration tests against the running stack |
| `make promtool-test` | `promtool check rules` and `promtool test rules` in the Prometheus image | validate alert rules and their unit tests |
| `make lint` | `uv run flake8 --extend-exclude mistral_provider.py services shared tests infra` (flake8 default settings, 79-character lines, no config file; `services/ai-service/ai/llm/mistral_provider.py` is excluded because that file is kept unchanged), then `make mypy` | lint and type-check |
| `make mypy` (`mypy-shared`, `mypy-auth`, `mypy-core`, `mypy-ai`) | four separate mypy runs with `MYPYPATH` and `--explicit-package-bases` | type-check (why four runs: [chapter 31](31-testing-quality.md)) |
| `make clean` | deletes `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.cache`, `*.pyc`, `*.pyo` | local cleanup |
| `make adminer` / `make adminer-stop` | `docker compose ... --profile adminer up -d adminer` / `stop adminer` | PostgreSQL web UI on `http://127.0.0.1:8080` |
| `make token [WHO=admin\|owner\|<uuid>]` | `bash scripts/token.sh` (default `admin`) | print a 2-hour access token |
| `make e2e [PART=core\|tier] [ARGS=...]` | `bash scripts/e2e.sh` (default `all`) | real-environment end-to-end checks |
| `make analyze T="AAPL MSFT"` | `bash scripts/run_analysis.sh` | real analyses with an output folder |
| `make tiers [ARGS=all]` | `bash scripts/tiers.sh` (default `open`) | tiers of the test portfolio |
| `make tiers-decline A="<uuid> ..."` | `bash scripts/tiers.sh decline` | decline recommendations (writes data) |
| `make health` | `bash scripts/health.sh` | one-screen health view |
| `make psql [ARGS=...]` | `bash scripts/psql.sh` | psql in the postgres container |
| `make redis-cli [ARGS=...]` | `bash scripts/redis-cli.sh` | redis-cli in the redis container |
| `make qdrant [ARGS=...]` | `bash scripts/qdrant.sh` | Qdrant collections and counts |
| `make objectstore [ARGS=...]` | `bash scripts/objectstore.sh` | buckets, keys, print an object |
| `make service-logs S=<svc>` | `bash scripts/logs.sh` | follow one service's log |
| `make rebuild S="<svc> ..."` | `bash scripts/rebuild.sh` | rebuild/restart and wait for health |
| `make snapshots [ARGS="--confirm"]` | `bash scripts/snapshots.sh` | recompute NAV snapshots (preview by default) |
| `make backups` | `bash scripts/backups.sh list` | list backups and sizes |
| `make backup-check [BACKUP=<stamp>]` | `bash scripts/backups.sh check` | restore dry run (default: newest) |
| `make restore-drill [BACKUP=<stamp>]` | `bash scripts/backups.sh drill` | restore into a throwaway project and verify (default: newest) |
| `make probe [MODEL=...] [ARGS=...]` | `bash scripts/probe.sh` (default: the configured HIGHEST primary model) | Gemini latency probe (uses quota) |
| `make smoke [ARGS=...]` | `bash scripts/smoke.sh` | smoke check of non-analysis features |

For SQL, this handbook writes the script form (`bash scripts/psql.sh -c
"..."`), because quoting through `make psql ARGS='...'` is easy to get
wrong.

### 3.5 The operator scripts

Index of the host-side scripts in `scripts/` (each is described below;
the Python scripts inside the services are in section 3.5b):

| Script | Purpose | Writes data? | Busy-window guard |
|---|---|---|---|
| `lib.sh` | shared helpers, sourced by the others (not run directly) | - | defines it |
| `token.sh` | print an access token | no | no |
| `health.sh` | one-screen health view | no | no |
| `psql.sh` | PostgreSQL shell | only what you type | no |
| `redis-cli.sh` | Redis shell | only what you type | no |
| `qdrant.sh` | Qdrant collections and counts | no | no |
| `objectstore.sh` | buckets, keys, print one object | no | no |
| `logs.sh` | follow or grep logs | no | no |
| `rebuild.sh` | rebuild or restart services, wait for health | restarts | yes |
| `run_analysis.sh` | real analyses with an output folder | yes (analyses, tiers, memory) | no |
| `tiers.sh` | open tiers; `decline` | only `decline` | no |
| `snapshots.sh` | recompute NAV snapshots | only with `--confirm` | yes |
| `backups.sh` | list, make, dry-run-check and drill backups | only `now` (`drill` writes only to its throwaway project) | no |
| `probe.sh` | Gemini latency probe, key and model health | no (uses LLM quota) | no |
| `smoke.sh` | smoke check of non-analysis features | small scratch rows, cleaned up | no |
| `e2e.sh` | real end-to-end checks | test data, cleaned up | yes |

#### `scripts/lib.sh` (shared helpers, sourced by the others)

- `cd` to the repository root, whatever your current folder is.
- `COMPOSE=(docker compose --env-file .env.docker)`.
- Default ids of the development data, overridable by environment
  variables: `ADMIN_USER_ID` (`551b898b-e87c-4872-8bca-70333bc9d00b`),
  `OWNER_USER_ID` (`5c7a7207-b4a1-4db8-8550-aae19d0a3d65`),
  `PORTFOLIO_ID` (`d72855ea-2776-41c3-bc77-ce1a4c1435d5`, the test
  portfolio).
- `psql_stdin`: runs SQL from stdin with `psql -U "$POSTGRES_USER" -d
  "$POSTGRES_DB" -v ON_ERROR_STOP=1` inside the postgres container, so the
  user and database come from the container, not from your shell.
- `make_token USER_ID ROLE [HOURS]`: runs auth-service's own
  `security._create_token(...)` inside the auth-service container.
- `in_busy_window` / `guard_busy_window`: the busy-window check
  (section 3.7). Used by `rebuild.sh`, `e2e.sh` and `snapshots.sh`.

#### `scripts/token.sh` - print an access token

```sh
bash scripts/token.sh admin              # admin account, role admin, 2 hours
bash scripts/token.sh owner              # test portfolio owner, role user
bash scripts/token.sh <user-uuid> [role] # any user; role defaults to user
HOURS=4 bash scripts/token.sh admin
TOKEN=$(bash scripts/token.sh admin)     # typical use
curl -sk https://localhost:8443/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
```

The token is signed with the live `JWT_SECRET` inside auth-service. It
carries `type: access` and the role you asked for, but every service
still checks the user's **live** role and active flag in Redis
(`shared/user_state.py`), so a token cannot give a deactivated user
access, and the live role wins over the claim. Normal logins get
`ACCESS_TOKEN_EXPIRE_MINUTES=15`-minute tokens; the script makes longer
ones for scripted work. `bash scripts/token.sh` needs an argument (no
argument is a usage error); only `make token` defaults to `admin`.

#### `scripts/health.sh` - one-screen health view

```sh
bash scripts/health.sh                   # = make health
```

Five sections:
1. `docker compose ps` (service, state, status with `(healthy)`).
2. Scheduled jobs: last success of each job from the Prometheus gauge
   `alphamind_job_last_success_timestamp_seconds`, with the age in hours.
   core-service exports it from the heartbeats and reloads the stored
   times from Redis (`core:job_last_success`) when it starts
   (`services/core-service/job_health.py`). A job that has never
   succeeded still has a value: the time core-service first started
   tracking it (`core:job_tracked_since`, written once with `HSETNX`), so
   the stale-job alerts also fire for it; `health.sh` labels such a job
   "no success yet (tracked since ...)" (it reads the job names in
   `core:job_last_success`). core-service names the job in
   a label called `scheduled_job`. It is not called `job`, because
   Prometheus already uses `job` for the scrape job (`core-service`).
   `health.sh` reads `scheduled_job`.
3. `alphamind_market_close_portfolios_skipped`: portfolios the last
   market-close run could not snapshot (a holding had no close). The
   value is also stored in Redis (`core:market_close_skipped`), so a
   core-service restart keeps it.
4. Firing alerts from Prometheus `/api/v1/alerts`, one line per alert:
   the alert name and where it fires (the `scheduled_job` label for a
   stale-job alert, else `service`, `job` or `instance`).
5. NewsAPI requests in the last 24 h, as counted by ai-service: "N of
   100 used, M left", when the oldest counted request leaves the window,
   and, when fewer than 35 are left (one 15-ticker batch), from when one
   batch fits. ai-service adds every request NewsAPI counts (not its
   cached answers, `X-Cached-Result: true`, and not rejected ones) to the
   Redis sorted set `ai:newsapi:requests` in DB 0 (score = unix time,
   entries older than 24 h removed). The NewsAPI account page
   (https://newsapi.org/account, updated hourly) is the provider's own
   count. The 100-per-day limit is NewsAPI's Developer plan.

It queries Prometheus at `PROMETHEUS_LOCAL_URL` (default
`http://127.0.0.1:9090`), so the `prometheus` container must be running.
To ask Prometheus yourself about one job, filter on `scheduled_job`:

```sh
curl -sS --get http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=alphamind_job_last_success_timestamp_seconds{scheduled_job="backup"}'
```

The same query with `job="backup"` returns nothing (`job` is always
`core-service` on this metric).

#### `scripts/psql.sh` - PostgreSQL shell

```sh
bash scripts/psql.sh                                        # interactive
bash scripts/psql.sh -c "SELECT count(*) FROM ai.analyses;"
bash scripts/psql.sh < query.sql
```

Arguments go to `psql` unchanged. Schema-qualify table names (`ai.`,
`core.`, `auth.`): the `postgres` superuser's `search_path` is not a
service's. Changing data from here is possible; treat any `INSERT`,
`UPDATE`, `DELETE` or `DROP` as a deliberate, data-changing action.

#### `scripts/redis-cli.sh` - Redis shell

```sh
bash scripts/redis-cli.sh                                   # interactive
bash scripts/redis-cli.sh INFO persistence
bash scripts/redis-cli.sh -n 2 PUBSUB CHANNELS 'events:*'   # pub/sub channels with a subscriber now
bash scripts/redis-cli.sh -n 3 --scan --pattern 'stream:*'  # durable streams live in DB 3
```

Remember the logical databases: 0 cache, 1 rate limits, 2 events and
user state, 3 durable streams ([chapter 23](23-redis.md)). Pub/sub
channels (`events:<name>`) are not keys, so `--scan` never shows them;
`PUBSUB CHANNELS` lists the ones that have a listener right now.

#### `scripts/qdrant.sh` - vector database

```sh
bash scripts/qdrant.sh                       # every collection with its exact point count
bash scripts/qdrant.sh info research_documents_gemini
bash scripts/qdrant.sh count research_documents_gemini
```

Uses the REST API on `QDRANT_LOCAL_URL` (default `http://127.0.0.1:6333`).
The dashboard is at `http://127.0.0.1:6333/dashboard`.

#### `scripts/objectstore.sh` - S3 object storage

```sh
bash scripts/objectstore.sh                  # buckets: object count and total bytes
bash scripts/objectstore.sh ls documents/    # first 200 keys under a prefix
bash scripts/objectstore.sh cat <key>        # print one object
```

`buckets` and `ls` run Python inside core-service with the application's
own S3 client (`shared/storage.py::_get_client`) and keys; `cat` uses
`shared/storage.py::download_bytes` there. Find a document's key with
`bash scripts/psql.sh -c "SELECT storage_url FROM core.documents WHERE id = '<document-id>';"`.

#### `scripts/logs.sh` - follow or search logs

```sh
bash scripts/logs.sh ai-service                        # follow, starting 10 minutes back
SINCE=2h bash scripts/logs.sh core-service
bash scripts/logs.sh ai-service grep judge_node.delayed_retry           # today's file
bash scripts/logs.sh ai-service grep analysis_marked_failed 2026-10-01  # a given day
```

Follow mode uses `docker compose logs -f --since`. Search mode greps
`logs/<service>/<YYYY-MM-DD>.log`, where the date is the New York date.

#### `scripts/rebuild.sh` - rebuild or restart, then wait for health

```sh
bash scripts/rebuild.sh ai-service                         # build image, recreate, wait
bash scripts/rebuild.sh auth-service core-service ai-service scheduler
RESTART_ONLY=1 bash scripts/rebuild.sh core-service        # restart only (code change)
FORCE=1 bash scripts/rebuild.sh core-service               # inside a busy window (think twice)
```

Without `RESTART_ONLY=1` it runs `docker compose ... up -d --build
<services>`; with it, `docker compose ... restart <services>`. Then it
polls each service's health every 5 seconds for up to 3 minutes (36
tries) and prints `docker compose ps`. A service without a health check
counts as ready once it is running. A container that exited (for
example one that crashed at startup) is listed as pending with its
state (`exited/`), never as ready, so the script then fails after the
3 minutes with `not running and healthy yet: <service>(<state>/<health>)`.
Refused in a busy window unless `FORCE=1`, and warns during the nightly
jobs (section 3.7). Do not restart ai-service while an analysis is
running: the analysis is lost.

#### `scripts/run_analysis.sh` - real analyses

See section 3.9.

#### `scripts/tiers.sh` - recommendation tiers of a portfolio

```sh
bash scripts/tiers.sh                       # open tiers (they reserve BUY cash)
bash scripts/tiers.sh all                   # every tier, newest first (up to 100)
bash scripts/tiers.sh decline <analysis-uuid> [...]   # WRITES DATA: declines the recommendation
PORTFOLIO_ID=<uuid> bash scripts/tiers.sh
```

"Open" means status `pending_acceptance` or `accepted_awaiting_trigger`
with a window that has not expired. `decline` makes a 1-hour token for
`OWNER_USER_ID` (a decision must come from the portfolio owner) and calls
the public `POST /api/v1/analyses/{id}/decisions` with
`{"action_taken": "declined", "portfolio_id": ...}` through the gateway.

#### `scripts/snapshots.sh` - recompute portfolio NAV snapshots

```sh
bash scripts/snapshots.sh                          # preview for PORTFOLIO_ID, changes nothing
bash scripts/snapshots.sh --confirm                # WRITES DATA: the flagged and missing rows
bash scripts/snapshots.sh --all --from 2026-09-01  # preview for every portfolio from a date
bash scripts/snapshots.sh --portfolio=<uuid>       # preview for another portfolio
bash scripts/snapshots.sh --to 2026-10-01          # also add a day that market-close skipped
```

Runs `python -m scripts.recompute_snapshots` in core-service. It rebuilds
each day's value from the transaction ledger and the official closes and
flags rows whose total differs by more than `--threshold` (default 1.00)
or whose cash differs by more than 0.01. It also adds missing trading
days between the first snapshot and `--to` (default: the last stored
snapshot, so pass `--to <date>` to fill a day the market-close job
skipped). Without `--portfolio` (also written `--portfolio=<id>`; repeat it
for several) or `--all`, the test portfolio is used. Refused in a busy window unless `FORCE=1`;
do not run it around 17:30 New York (22:30 London), when market-close
writes the day's snapshot.

#### `scripts/backups.sh` - list, check and drill backups

```sh
bash scripts/backups.sh               # list ./backups/<stamp>/ with sizes
bash scripts/backups.sh now           # make a backup now (same as make backup)
bash scripts/backups.sh check         # restore DRY RUN of the newest backup
bash scripts/backups.sh check 2026-10-01_0230
bash scripts/backups.sh drill         # restore drill of the newest backup
bash scripts/backups.sh drill 2026-10-01_0230   # (= make restore-drill BACKUP=...)
```

`check` runs `restore.sh` without `--yes` in the `restore` container, so
it verifies checksums and that the dump is readable, and changes nothing.
It starts the container with `docker compose ... run --rm --build`, so the
`restore` image is rebuilt from the current `infra/scheduler/` files
first (the first run may take a minute).
`drill` restores the backup into the separate compose project
`alphamind-drill` (own volumes and network, no host ports), runs
`restore.sh --verify` against it and removes the project with its volumes
afterwards, also on failure; the live stack is not touched
([../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) section 6).
A real restore is only `make restore BACKUP=<stamp> CONFIRM=yes`
(section 3.12).

#### `scripts/probe.sh` - LLM latency probe

```sh
bash scripts/probe.sh                                   # HIGHEST primary model, 2 requests per key
bash scripts/probe.sh gemini-3.6-flash --per-key 1 --cap 90
```

Runs `python -m scripts.probe_model_latency` in ai-service. Without a
model name it probes the configured HIGHEST primary model
(`LLM_GOOGLE_MODEL_HIGHEST_ACCURACY`, `gemini-3.8-flash`). It sends
Judge-sized requests, one key at a time, 5 seconds apart, with one long
time limit (`--cap`, default 120 s), and records each real latency, so one
run shows how many answers a 30, 60 or 90 second timeout would keep. The
requests use the real Judge system prompt and a stored evidence block,
`--per-key` (default 2) per configured pooled key, so 18 requests with 9
keys. A 503 or 429 is recorded, not retried. **Every request counts
against that model's daily quota per key** (about 20 per key and day on
the free tier). It changes nothing in the database. Best run while a
batch is seeing timeouts.

**Key and model health (`--health`).**

```sh
bash scripts/probe.sh --health
make probe ARGS=--health
```

This checks every configured Google key with one tiny "pong" request on
the CHEAP model (a broken Google project, typically one without the
Generative Language API enabled, is named by its key slot), then each
distinct configured model id (`LLM_GOOGLE_MODEL_CHEAP`, `..._CHEAP_FALLBACK`,
`..._HIGHEST_ACCURACY`, `..._HIGHEST_ACCURACY_FALLBACK`) once on the first
healthy key, so a model id that does not resolve shows up. Exit code 1
when anything failed. Cost: one request per key plus one per model.
A first argument that starts with `--` is passed through as an option,
so no model name is needed.

#### `scripts/smoke.sh` - smoke check

```sh
bash scripts/smoke.sh                          # all checks, no LLM call
bash scripts/smoke.sh --only db redis qdrant
bash scripts/smoke.sh --with-llm               # plus one cheap completion and one embedding
```

Runs `python -m scripts.smoke_check` in ai-service. Checks (from
`CHECKS` in `services/ai-service/scripts/smoke_check.py`): `db`, `redis`,
`qdrant`, `bm25`, `retrieval`, `kg`, `market_data`, `fx`, `fred`,
`core_service`, `internal_api`, `backtest_metrics`, `backtest_replay`,
`tier_monitoring_dry`, `review`, `queue`, and `llm` (only with
`--with-llm`: one CHEAP completion and one embedding). One line per check
(`OK`, `FAIL`, `SKIP`) with its detail or reason, then a summary; exit
code 1 if any check failed. Each check runs on its own with its own time
limit (`bm25`, `retrieval` and `llm` 120 s, `market_data` 90 s,
`backtest_replay` 180 s, every other check 60 s), so one failure
never stops the others. `fred` reads the Macro agent's FRED data through
an alias (`fed_funds_rate`, 90 days) and a raw series id (`DGS10`, 30
days); it is SKIP when `FRED_API_KEY` is empty and FAIL when a series has
no observation in its window. `tier_monitoring_dry` is always SKIP for
now: there is no side-effect-free trigger evaluation it could call (the
tier check executes trades), so it only counts the open tiers.

Side effects by design: `market_data` refreshes AAPL's cached price bars (the same rows any analysis writes), `core_service` may
refresh the 30-second holdings cache in Redis, and `backtest_replay`
creates one scratch system backtest (strategy name
`smoke_check`) and deletes it with its metrics before the check
returns. No analyses, tiers, recommendations, decisions, transactions or
documents are written, and no events are published.

#### `scripts/e2e.sh` - real end-to-end checks

See section 3.11.

### 3.5b Python operator scripts inside the services

Every service has a `scripts/` folder with Python tools that need the
service's own code and settings. They run **inside** the container (the
gateway does not forward `/internal/*`, and the container has the
internal token and the database settings):

```sh
docker compose --env-file .env.docker exec -T <service> python -m scripts.<name> [options]
```

Common rules, followed by every script that writes: the default is a
preview or dry run, a flag (`--apply`, `--confirm`) is needed to change
anything, and a re-run is safe (rows already handled are skipped or
reported). Run them outside the busy windows (section 3.7); none of them
checks the clock.

| Script | Service | Writes? | What it does | Chapter |
|---|---|---|---|---|
| `set_system_role --email E --role guest\|user\|moderator\|admin [--dry-run]` | auth | yes | Sets a user's system role in the database and in the live Redis mirror, so issued tokens get it at once. How the first admin is made. Refuses an unknown email, a deleted account and demoting the last active admin (exit 1). | 11 |
| `alembic_convert [--apply]` | auth, core, ai | with `--apply` | One-time conversion of a schema from the archived migration chain to the current baseline, in one transaction. Take a backup first. Exit 0 when converted, already converted, or preview done; 1 when refused. Kept only until no backup from before the baseline is left, then deleted. | 20 |
| `delete_documents --id U [--id U ...] [--confirm]` | core | with `--confirm` | Deletes documents like the API does, but without the owner check (for documents owned by the ingestion user or the system): the row and a `document.deleted` outbox entry in one commit, the event published at once (or later by the outbox dispatcher), then the S3 object. Each document is committed on its own; ids already gone are reported as not found. | 12, 22 |
| `backfill_sentiment [--apply] [--kind note\|comment\|all] [--limit N] [--rate 30]` | core | with `--apply` | Fills `sentiment_label` for notes and comments that have none (oldest first, hidden rows included, empty notes skipped) through ai `POST /internal/sentiment`, at most `--rate` calls per minute so the Lite LLM tier is not flooded. A result is stored only if the text did not change meanwhile; `updated_at` is not touched; a failed call leaves the row empty for the next run. | 12 |
| `recompute_snapshots` (through `scripts/snapshots.sh`) | core | with `--confirm` | Rebuilds NAV snapshots from the ledger and official closes; see section 3.5. | 12 |
| `e2e_check` (through `scripts/e2e.sh`) | core | test data only, cleaned up | Whole-stack end-to-end check; see section 3.11. | 31 |
| `analysis_cli create\|status\|get` | ai | `create` | Internal analysis client used by `run_analysis.sh`; see section 3.9. | 13 |
| `upload_seed_documents --as-system [--dry-run] [--categories ...] [--seed-root DIR]` | ai | yes | Uploads the repository's `rag_seed_documents/` as system documents; see section 3.2 step 8. Without `--as-system` it uploads as a user (`--email`, `--password`) through the public API from the host. | 17 |
| `measure_rag_recall [--limit N] [--top-k 10] < golden_set.json` | ai | no | Recall@1/5/10 and MRR@10 of the live index (BM25-only, vector-only, fused, and the production default) against the golden set read from stdin; questions whose chunk no longer exists are skipped and counted. One batched embedding pass, no chat model call. | 17 |
| `measure_rag_recall --sweep [--apply]` | ai | only with `--apply` | Tunes the per-category BM25/vector weights in `ai/rag/category_weights.json` (categories with at least 10 questions; a win of 2 hits or fewer is flagged as noise). The file is inside the image: copy it out with `docker compose cp`, commit it, rebuild. | 17 |
| `generate_rag_golden_set --output /tmp/x.json [--merge] [--category C] [--limit N] [--dry-run]` | ai | no (writes a file in the container) | Makes golden-set questions (one CHEAP call per chunk); with `--merge` it reads the existing set from stdin and only adds chunks without a question. Copy the result out with `docker compose cp`. | 17 |
| `reembed_collection --source C [--target C] [--model M] [--dims N] [--all] [--confirm]` | ai | with `--confirm` | Copies a Qdrant collection into a new one with vectors from another embedding model (same point ids and payloads, so PostgreSQL and BM25 stay valid; the source stays as the rollback copy). | 21 |
| `delete_analyses [--id U] [--failed] [--since D --until D] [--report U] [--confirm]` | ai | with `--confirm` | Deletes whole analyses and everything they wrote (archived notes with their vectors and KG relations, tiers, debate, memory), then sweeps orphans in the ai schema and the RAG store. Never deletes a `pending`/`running` analysis or one with a user decision. Afterwards restart ai-service (BM25 rebuild) and run the review job. | 13 |
| `backfill_filing_records [--ticker T] [--filename F] [--latest N] [--redo] [--tier T] [--apply]` | ai | with `--apply` | Structured records (`ai.filing_records`) for stored filings, one records-only LLM call per filing, raw text from `raw_filings/` in object storage (else SEC EDGAR). Waits 65 s before a filing above 500,000 characters. | 18 |
| `regenerate_filing_summaries --filename F [--apply]` | ai | with `--apply` | Two steps: write a new summary of a stored filing to `/app/_regenerated/` (no database write, prints the current document id); after the old document is deleted with core's `delete_documents`, `--apply` stores the new one. A filing whose old document still exists is skipped. | 18, 22 |
| `smoke_check` (through `scripts/smoke.sh`) | ai | small, cleaned up | See section 3.5. | 31 |
| `e2e_tier_check` (through `scripts/e2e.sh tier`) | ai | synthetic rows, cleaned up | See section 3.11. | 19 |
| `probe_model_latency` (through `scripts/probe.sh`) | ai | no (uses LLM quota) | See section 3.5. | 14 |
| `verify_portfolio_backtest --list` / `--portfolio U [--start D] [--end D] [--benchmark SPY] [--run [--keep]]` | ai | only with `--run` (row deleted unless `--keep`) | Recomputes a portfolio backtest independently in three layers: each snapshot against the ledger, the TWR metrics with its own code, and with `--run` the production backtest. | 19 |

`services/ai-service/scripts/_text.py` is a helper module for the
scripts' output, not a command.

### 3.6 Deploying a change: restart, rebuild or recreate

In development the Python source is bind-mounted and uvicorn runs
**without** `--reload` (`docker-compose.override.yml`; the reason is in
[chapter 01](01-architecture.md), section 3.9). So nothing changes in a
running service until you act. What to do depends on what changed:

| What changed | What to do | Command |
|---|---|---|
| Python code under `services/<svc>/` | restart that service | `RESTART_ONLY=1 bash scripts/rebuild.sh <svc>` |
| Python code under `shared/` | restart all three services | `RESTART_ONLY=1 bash scripts/rebuild.sh auth-service core-service ai-service` |
| An agent prompt or tool in ai-service | restart ai-service (prompts are Python code) | `RESTART_ONLY=1 bash scripts/rebuild.sh ai-service` |
| A new Alembic migration | restart the service: it runs `alembic upgrade head` at startup. Make a backup first. | `make backup`, then `RESTART_ONLY=1 bash scripts/rebuild.sh <svc>` |
| `pyproject.toml` or `uv.lock` | rebuild the images (dependencies are installed in the image, in `/opt/venv`) | `bash scripts/rebuild.sh auth-service core-service ai-service` |
| A service `Dockerfile` | rebuild that service | `bash scripts/rebuild.sh <svc>` |
| `infra/scheduler/*` (crontab, job scripts, `backup.sh`, `restore.sh`, `s3-remote.sh`, `heartbeat.sh`) | rebuild the scheduler (files are copied into the image). The `restore` tool image is skipped by `up --build` because of its profile, but `make backup-check`, `make restore` and `make restore-drill` rebuild it themselves (`run --build`) | `bash scripts/rebuild.sh scheduler` |
| `infra/gateway/nginx.conf`, `status.html`, certificates | restart the gateway (after editing `status.html`, also run `python3 infra/gateway/csp_hashes.py --write`) | `docker compose --env-file .env.docker restart gateway` |
| `infra/prometheus/*.yml` | check, then restart Prometheus | `make promtool-test`, then `docker compose --env-file .env.docker restart prometheus` |
| `infra/alertmanager/alertmanager.yml` | restart Alertmanager | `docker compose --env-file .env.docker restart alertmanager` |
| `infra/vector/vector.toml` | restart vector | `docker compose --env-file .env.docker restart vector` |
| `infra/grafana/provisioning/*` | restart Grafana | `docker compose --env-file .env.docker restart grafana` |
| `.env.docker` | **recreate** the containers that read it (a `restart` keeps the old environment). The scheduler writes its job environment file only at start, so include it. | `docker compose --env-file .env.docker up -d --force-recreate <services>` |
| `docker-compose.yaml` | apply the new configuration | `docker compose --env-file .env.docker up -d` |

**How do migrations run?** Not by hand. Each service's `lifespan` calls
`db_bootstrap.ensure_schema` (for example
`services/core-service/db_bootstrap.py`), which calls
`shared/alembic_bootstrap.py::bootstrap_schema`:

1. `CREATE SCHEMA IF NOT EXISTS "<schema>"`.
2. If the schema still has a revision from the migration chain that was
   archived (`migrations/archive/`), refuse to start and print the
   one-time conversion command (`python -m scripts.alembic_convert`,
   preview first, then `--apply`).
3. If the schema has tables but no Alembic revision, refuse: nothing can
   tell what state those tables are in.
4. Otherwise run `alembic upgrade head` (in a worker thread) and log
   `schema_bootstrap_complete` with `revision_before` and
   `revision_after`.

The migrations live in `services/<svc>/migrations/versions/`. To see what
the database is at:

```sh
bash scripts/psql.sh -c "SELECT 'auth' AS schema, version_num FROM auth.alembic_version UNION ALL SELECT 'core', version_num FROM core.alembic_version UNION ALL SELECT 'ai', version_num FROM ai.alembic_version;"
```

**After every deploy, verify the running process, not the file.** A
change is only live when the process restarted and loaded it. Read a
setting back from inside the container, for example:

```sh
docker compose --env-file .env.docker exec -T ai-service python -c "from config import settings; print(settings.ANALYSIS_TIMEOUT_SECONDS)"
```

or look for a log line that only the new code writes. A correctly edited
file says nothing about the running process: a configuration change can
stay inactive across several deploys until a read-back shows the
container still runs the old values.

**When to deploy:** outside the busy windows (section 3.7), and when no
analysis is running if ai-service is involved:

```sh
bash scripts/psql.sh -c "SELECT id, ticker, status, created_at FROM ai.analyses WHERE status IN ('pending', 'running') ORDER BY created_at;"
```

### 3.7 The scheduler's timetable and busy windows

The `scheduler` container runs `dcron` with `TZ=America/New_York`
(`infra/scheduler/Dockerfile`), so the times follow New York daylight
saving automatically. The crontab is `infra/scheduler/crontab`:

| Job | Cron | New York | London (usual) | Calls | curl limit |
|---|---|---|---|---|---|
| `review-run` | `0 2 * * *` | 02:00 daily | 07:00 | ai `POST /internal/review/run` | 1800 s |
| `backup` | `30 2 * * *` | 02:30 daily | 07:30 | `/scripts/backup.sh` (local) | - |
| `rag-ingest` | `0 3 * * *` | 03:00 daily | 08:00 | ai `POST /internal/scheduler/rag-ingest` | 3600 s |
| `logs-cleanup` | `0 4 * * *` | 04:00 daily | 09:00 | `/scripts/logs-cleanup.sh` (local) | - |
| `purge-unverified` | `10 4 * * *` | 04:10 daily | 09:10 | auth `POST /internal/scheduler/purge-unverified` (accounts never verified for 7 days) | 300 s |
| `notification-retention` | `20 4 * * *` | 04:20 daily | 09:20 | core `POST /internal/scheduler/notification-retention` (read notifications older than 90 days) | 300 s |
| `market-open` | `30 9 * * 1-5` | 09:30 Mon-Fri | 14:30 | core `POST /internal/scheduler/market-open` | 600 s |
| `tier-monitoring` | `0 17 * * 1-5` | 17:00 Mon-Fri | 22:00 | ai `POST /internal/scheduler/tier-monitoring` | 900 s |
| `market-close` | `30 17 * * 1-5` | 17:30 Mon-Fri | 22:30 | core `POST /internal/scheduler/market-close` | 600 s |

Why these times: the review, backup, ingestion and log cleanup run every
day, because nothing they do depends on the trading session (SEC posts
filings on any day; on a day with nothing new ingestion is a no-op).
The backup sits between review (02:00) and ingestion (03:00), the
quietest moment. Tier monitoring waits one hour after the 16:00 close,
so the data provider's final daily bar is settled. Market close runs
after tier monitoring (which ends within its 900 s limit), so trades the
tiers executed are in the day's snapshot. The curl limits match the
work each route does inline: market-close recomputes every portfolio's
snapshot, market-open makes one ai-service create call (up to 15 s) per
held ticker of each `auto_analyze_at_open` portfolio, at most 10 at once,
rag-ingest fetches filings and news for every tracked ticker and writes
the macro research report (the slowest job).

How a job runs:

- `infra/scheduler/entrypoint.sh` stays PID 1. It writes the variables the
  jobs need (`INTERNAL_API_TOKEN`, `POSTGRES_*`, Qdrant settings,
  `BACKUP_*`, `TZ`, ...) to `/scripts/env.sh` (readable by root only,
  because it holds secrets; every value single-quoted so any character
  survives), because cron does not pass its own environment to jobs.
  Then it starts `crond -f -l 2` as a **child** and forwards `SIGTERM`
  and `SIGINT` to it, so `docker compose stop` stops it cleanly. crond is
  not made PID 1 because dcron calls `setpgid()` at start, which fails
  for PID 1 (a session leader); crond then exits and the container
  restarts in a loop.
- The five HTTP jobs (market-open, market-close, tier-monitoring,
  review-run, rag-ingest) all run through one script,
  `call-job.sh <job>`: it sources `env.sh`, looks up the job's route and
  time limit, calls the route with `--max-time <limit>` and the
  `X-Internal-Token` header, prints the answer, and on success calls
  `/scripts/heartbeat.sh <job>`. On failure it prints the curl exit code
  to stderr and exits non-zero, so no heartbeat is sent and the stale-job
  alert eventually fires. It uses `curl -sS --fail-with-body`, so a
  failed run prints the answer body with its counts.
- A job counts as failed when its endpoint answers an HTTP error. The
  rag-ingest, tier-monitoring, review-run and market-open endpoints answer `500`
  with their counts (plus `"all_units_failed": [<step names>]`) when a
  step tried at least one item and every item failed
  (`shared/scheduled_jobs.py::job_response`, log
  `scheduled_job_all_units_failed`); a step with nothing to do is a
  success. So a night in which every ticker failed to ingest gets no
  heartbeat.
- `heartbeat.sh` never fails the job itself: a heartbeat that cannot be
  sent is logged, and the job then simply looks stale, which is the
  point (core-service or the network is not working).
- Every job writes to `/proc/1/fd/1` and `/proc/1/fd/2` (the entrypoint's
  stdout and stderr), so `docker compose logs scheduler` and
  `./logs/scheduler/<date>.log` show every job line.
- Market holidays: cron only knows weekdays. On weekends and NYSE
  holidays the services answer `200 {"skipped": true, "reason": ...}`
  themselves (`shared/market_calendar.py`). Early-close days are treated
  as normal days.
- Order on trading days: tier monitoring (17:00) may execute tiers into
  the transaction ledger; market close (17:30) then snapshots every
  portfolio, so those trades are included the same day.

**Busy windows** (`scripts/lib.sh::in_busy_window`):

| Window (New York) | London (usual) | Why |
|---|---|---|
| 09:00-10:30 | 14:00-15:30 | market-open at 09:30 asks ai-service for an analysis of every held ticker of every portfolio with `auto_analyze_at_open`. They join the serial analysis queue. A restart of ai-service would fail them (`service_restart`); a restart of core-service during the request would break the job's HTTP call. |
| 16:55-17:50 | 21:55-22:50 | tier monitoring (17:00, up to 900 s) executes tiers through core-service; market close (17:30, up to 600 s) writes the day's snapshots. A restart or an e2e run in between can break a job or disturb the e2e checks' counters. |
| 01:55-04:30 (warning only) | 06:55-09:30 | the nightly jobs: review-run (02:00, up to 1800 s), backup (02:30), rag-ingest (03:00, up to 3600 s), logs-cleanup (04:00), purge-unverified (04:10), notification-retention (04:20). A restart of ai-service aborts review-run or rag-ingest, which then sends no heartbeat until the next night (`DailyJobStale` after 26 hours). |

`rebuild.sh`, `e2e.sh` and `snapshots.sh` (and their make targets) call
`guard_busy_window` and stop with `refused: the scheduler is busy now
(...). Wait, or run again with FORCE=1.` With `FORCE=1` they run and
print a warning. In the nightly window they only print `warning: ...
during the nightly jobs ...` and go on (it is the working morning in
Europe); a nightly job they interrupted can be run again by hand, for
example `docker compose --env-file .env.docker exec scheduler
/scripts/call-job.sh rag-ingest` (or `/scripts/backup.sh`). Nothing else is guarded: a plain `docker compose
restart`, the Python scripts of section 3.5b and `run_analysis.sh` do
not check the clock, so look at it yourself. `run_analysis.sh` analyses
join the same queue as the market-open analyses, so a manual batch
started at 09:25 New York shares the queue with them.

The guard only compares the New York clock time with the windows.
It does not know weekends or NYSE holidays, so it also refuses on a
Saturday at 09:15 although no market job runs then. On such a day
`FORCE=1` is safe (the scheduled jobs skip closed market days
themselves).

Note on London times: London is usually 5 hours ahead of New York. For
a few weeks in March and in October/November the two cities change
clocks on different dates, and the gap is 4 hours; the scheduler always
follows New York.

Details of each job: [chapter 26](26-scheduler-jobs.md).

### 3.8 Checking health

| Where | What you see | How to reach it |
|---|---|---|
| `make health` | containers, last success of every scheduled job, market-close skipped count, NewsAPI requests in the last 24 h, firing alerts | terminal |
| `docker compose --env-file .env.docker ps` | `(healthy)` / `(unhealthy)` per container | terminal |
| `https://localhost:8443/status` | one row per service with its dependencies; refreshes every 15 s | browser, through the gateway |
| `curl -sk https://localhost:8443/health/auth` (and `/core`, `/ai`) | `{"status":"ok", ...}` with HTTP 200, or `"degraded"` with 503 | terminal |
| `curl -sk https://localhost:8443/healthz` | the frontend: `{"status":"ok"}` with 200 only when its server side reaches the API through the gateway's internal listener, else 503 | terminal |
| Grafana `https://localhost:8443/grafana/` | dashboards "AlphaMind - Service Overview" and "AlphaMind - Infrastructure & AI pipeline" | browser; user `admin`, password `GRAFANA_ADMIN_PASSWORD` |
| Prometheus `http://127.0.0.1:9090` | `/targets` (every scrape job up?), `/alerts` (rules and states), queries | browser, local only |
| Alertmanager `http://127.0.0.1:9093` | alerts that were sent, silences | browser, local only |
| MailHog `http://127.0.0.1:8025` | every email: verification, reset, invitations, notifications, alert emails to `alerts@alphamind.local` | browser, local only |
| Qdrant `http://127.0.0.1:6333/dashboard` | collections, points, a search console | browser, local only |
| Adminer `http://127.0.0.1:8080` (after `make adminer`) | tables and SQL in a web page | browser, local only |

Adminer login: open
`http://127.0.0.1:8080/?pgsql=postgres&username=postgres&db=alphamind&ns=core`
(`make adminer` prints it). The link selects System "PostgreSQL", Server
`postgres`, the user and the database, so only the `POSTGRES_PASSWORD`
from `.env.docker` is left to type. Two traps:

- The plain login form defaults to System "MySQL". With MySQL selected,
  Adminer tries port 3306 on `postgres`, where nothing listens, and shows
  "Connection refused".
- After login Adminer shows the `public` schema, which is empty. The
  tables are in the schemas `auth`, `core` and `ai`: pick one in the
  "Schema" list at the top left, or change `ns=core` in the link.

It can change data; use its SQL page for `SELECT`s unless a change is
intended. Stop it afterwards with `make adminer-stop`.

Grafana's admin password is applied only when the `grafana_data` volume
is first created. To change it later:
`docker compose --env-file .env.docker exec grafana grafana cli admin reset-admin-password '<new password>'`.

Prometheus scrapes every 15 seconds (`infra/prometheus/prometheus.yml`):
`auth-service`, `core-service`, `ai-service` (`/metrics`), `postgres`
(postgres-exporter:9187), `redis` (redis-exporter:9121), `alertmanager`,
and itself. Alert rules (`infra/prometheus/alert_rules.yml`):
`ServiceDown`, `PostgresDown`, `RedisDown`, `High5xxRate`,
`HighLatencyP95`, `AnalysisFailureRateHigh`, `AnalysisQueueBacklog`,
`LLMProviderErrorsHigh`, `DailyJobStale` (no success for over 26 hours:
backup, review-run, rag-ingest, logs-cleanup, purge-unverified,
notification-retention), `TradingDayJobStale` (over
100 hours: market-open, market-close, tier-monitoring),
`MarketCloseSkippedPortfolios` and `OutboxBacklog` (durable events
waiting in `core.event_outbox` for 15 minutes). The two stale-job alerts
filter on the `scheduled_job` label (see `health.sh` above) and fire
after 10 minutes above their limit. `MarketCloseSkippedPortfolios` has
no waiting time: it fires at the first evaluation after a skip.
Alertmanager groups by alert name and service and emails MailHog
(`infra/alertmanager/alertmanager.yml`: `group_wait` 30 s,
`group_interval` 5 min, `repeat_interval` 4 h). See
[chapter 27](27-observability.md).

### 3.9 Running real analyses

```sh
caffeinate -i bash scripts/run_analysis.sh AMD                # one ticker
caffeinate -i bash scripts/run_analysis.sh AAPL MSFT NVDA     # a batch
PORTFOLIO_ID=<uuid> caffeinate -i bash scripts/run_analysis.sh AMD
caffeinate -i make analyze T="AAPL MSFT"                       # same, through make
```

What it does, per ticker, one after another (`scripts/run_analysis.sh`):

1. **Create** a system analysis bound to `PORTFOLIO_ID` (default: the test
   portfolio) by running
   `python -m scripts.analysis_cli create <TICKER> --portfolio <id>`
   inside ai-service. The CLI calls `POST /internal/analyses` on
   `http://localhost:8000` inside the container, with the
   `X-Internal-Token` from the container's environment (standard library
   only; the image has no curl). If creation fails (for example `503`
   when 50 analyses are already pending), the error is written to
   `<T>_create_ERROR.txt` and the batch moves on.
2. **Poll** `analysis_cli status <id>` every 5 seconds, for up to 3600
   seconds. The wide limit allows for a ticker whose newest filing is
   not summarised yet: it is summarised inline first (up to 300 s per
   attempt, `_FILING_SUMMARY_TIMEOUT_SECONDS` in
   `ai/knowledge/filing_summary.py`). The analysis itself has a hard
   45-minute limit (`ANALYSIS_TIMEOUT_SECONDS=2700`) in ai-service. The
   script's limit does not count time the analysis spent waiting in the
   queue, so in a busy queue it can stop polling before the analysis
   started (a known limit; the analysis still runs).
3. **Wait for the queue job to finish** (up to 2 minutes, 24 x 5 s):
   the status turns `completed` or `failed` before the post-verdict
   writes (archived notes, knowledge-graph facts, procedural memory) are
   done. Without this wait the export would miss the last note and the
   next ticker would start while they still run. The script looks for
   the log event `analysis_queue_job_finished` with the analysis id.
4. **Export** the results and the log window. It waits 5 seconds before
   reading the log, because `docker compose logs --until` has one-second
   resolution and would drop lines written later in the same second.

One ticker failing or timing out never stops the rest (`set -uo
pipefail`, without `-e`). The script sources `scripts/lib.sh` (repository
root, Compose command, `PORTFOLIO_ID` default, `psql_stdin`), so it works
from any folder.

Output folder `runs/run_<timestamp>/` in the repository root (`runs/` is
gitignored):

```
runs/run_<YYYYMMDD_HHMMSS>/
  _summary.txt               start time, tickers, portfolio; then per ticker:
                             "<T>: analysis_id=<id> status=<status> elapsed=<s>s"
  <T>_result.json            GET /analyses/{id} body: agent runs and outputs,
                             debate, reflections, recommendation
  <T>_agent_reports.txt      ai.agent_reports rows of this analysis
  <T>_tiers.txt              ai.recommendation_tiers of its recommendation
                             (side, target price, quantity, status, window end)
  <T>_filings.txt            core.documents of category 'filing' for the ticker
  <T>_ai-service.log         ai-service log from the start of this ticker to the end
  <T>_create_ERROR.txt       only when the create call failed
```

Consequences to know:

- It writes real data: an analysis row, agent outputs, a recommendation,
  memory and knowledge-graph entries. For a BUY or SELL verdict, the
  position sizing writes tiers with status `pending_acceptance` on the
  portfolio. They do not trade unless accepted. Decline unwanted ones
  with `bash scripts/tiers.sh decline <analysis-id>`.
- It spends LLM quota.
- Analyses run one at a time in ai-service, so a batch of N tickers takes
  roughly N times one analysis (usually 1.5 to 4 minutes each; a
  15-ticker batch about 30 to 40 minutes).
- A batch also uses NewsAPI requests (about 35 for 15 tickers, of 100
  per day); `make health` shows how many are left.
- Do not restart ai-service while it runs. Pressing Ctrl+C stops the
  script's polling, but not the analysis inside ai-service (Ctrl+C on
  `docker compose exec -T` does not stop the process in the container
  either; check with `docker compose --env-file .env.docker top
  ai-service`).

`analysis_cli` can also be used alone (`services/ai-service/scripts/
analysis_cli.py`):

```sh
docker compose --env-file .env.docker exec -T ai-service python -m scripts.analysis_cli create AMD --portfolio d72855ea-2776-41c3-bc77-ce1a4c1435d5
docker compose --env-file .env.docker exec -T ai-service python -m scripts.analysis_cli status <analysis-id>
docker compose --env-file .env.docker exec -T ai-service python -m scripts.analysis_cli get <analysis-id>
docker compose --env-file .env.docker exec -T ai-service python -m scripts.analysis_cli create AMD --user <user-uuid>
```

`create` prints `{"data": {"id": ...}}`, `status` prints `{"data":
{"status": ...}}`, `get` prints the same body as the public `GET
/api/v1/analyses/{id}`. With `--user` the analysis belongs to that user
(it appears in their history and notifications); with `--portfolio` as
well, ai-service checks that the user may use that portfolio. Without
`--user` it is a system analysis. On an HTTP error the body is printed
and the exit code is 1.

Watching progress live:

```sh
bash scripts/logs.sh ai-service
bash scripts/psql.sh -c "SELECT id, ticker, status, failure_reason, created_at FROM ai.analyses ORDER BY created_at DESC LIMIT 5;"
```

### 3.10 Tokens for manual API calls

```sh
TOKEN=$(bash scripts/token.sh admin)
curl -sk https://localhost:8443/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
curl -sk https://localhost:8443/api/v1/portfolios -H "Authorization: Bearer $(bash scripts/token.sh owner)"
```

Use `-k` with curl because the certificate is self-signed. Internal
routes cannot be reached this way (the gateway answers 404); use the
container tools instead (`analysis_cli`, the e2e scripts).

### 3.11 End-to-end checks

```sh
bash scripts/e2e.sh                       # both: core e2e_check, then the tier chain
bash scripts/e2e.sh all --verbose         # both, each with --verbose (also --keep, --break-lock)
bash scripts/e2e.sh core                  # only core-service scripts.e2e_check
bash scripts/e2e.sh core --only auth,portfolio --verbose
bash scripts/e2e.sh core --with-analysis  # adds a full LLM analysis of AMD (up to 15 min)
bash scripts/e2e.sh tier                  # only ai-service scripts.e2e_tier_check
make e2e PART=core ARGS="--verbose"
```

`e2e.sh` makes a fresh 2-hour admin token and passes it as
`E2E_ADMIN_TOKEN`. It is refused in a busy window unless `FORCE=1`.

- **`scripts.e2e_check`** (core-service container) drives the public API
  through the TLS gateway (`https://gateway`, Host `localhost`), the
  WebSockets through `wss://gateway/ws/...`, and reads verification and
  invitation emails from MailHog. Sections: `platform`, `auth`, `roles`,
  `org`, `portfolio`, `apikey`, `documents`, `research`, `realtime`,
  `yjs`, `analytics`, `backtest`, `analysis`, `admin`, `cleanup`.
  `--only` adds a section's prerequisites automatically. Output: one line
  per check (`OK`, `FAIL`, `SKIP`) and a `SUMMARY` line; exit code 1 if a
  check failed. Test data is named `e2e-<runid>-...` / `E2E <runid> ...`
  and removed by the `cleanup` section (skip it with `--keep`). Rate
  limits to remember: auth-service allows 10 registrations per address
  per hour (a run uses 3) and 30 logins per address per 15 minutes.
- **`scripts.e2e_tier_check`** (ai-service container) runs the tier chain
  with a synthetic SELL recommendation, so it does not depend on the LLM
  answering BUY or SELL: a portfolio with a deposit and a buy of 10 AMD,
  the "followed" decision, one tier executed (and the same order sent
  again, which must not trade twice), one tier refused by core (more
  shares than held), and one tier cancelled when the portfolio is
  deleted. It watches the `analysis:<id>` WebSocket and the admin's
  notifications. It cleans up its synthetic rows unless `--keep`.
- **Only one e2e run at a time.** Both scripts take the Redis lock
  `e2e:run_lock` (`shared/e2e_lock.py`, 45-minute expiry). After a run
  was killed, remove the lock at once with `--break-lock`
  (`bash scripts/e2e.sh core --break-lock`).

### 3.12 Backups and restore

**What a backup contains** (`infra/scheduler/backup.sh`):

```
backups/<YYYY-MM-DD_HHMM>/      (host folder ./backups, gitignored)
  postgres.dump                 pg_dump -Fc of the whole database (auth, core, ai)
  qdrant/<collection>.snapshot  one snapshot per collection (created, downloaded,
                                then deleted from Qdrant)
  objectstore/<bucket>/<key>    every object of every bucket, copied through the
                                S3 API (rclone); one plain file per object
  avatars.tar.gz                auth-service avatars (when the volume is mounted)
  SHA256SUMS                    checksums of every file above, objects included
  manifest.json                 format 3: time, sizes, checksums, Qdrant collections
                                with exact point counts, objects and bytes per
                                bucket, the Alembic revision of each schema
```

**When and how long:** every day at 02:30 New York (07:30 London), and
on demand with `make backup` (or `bash scripts/backups.sh now`, or
`docker compose --env-file .env.docker exec scheduler /scripts/backup.sh
--dry-run` to only print the plan). Backups older than
`BACKUP_RETENTION_DAYS` (default 7) are deleted **only after** a new
backup succeeded, and the newest 3 complete backups are always kept,
whatever their age (so a week with the Mac asleep never leaves just one).

**Safety rules in the script:**

- The run is built in `.incomplete-<stamp>` and renamed only when every
  step succeeded. A failed run is kept as `<stamp>-FAILED` for
  inspection, older backups are not pruned, and the script exits 1.
- One run at a time: the lock folder `/backups/.lock` holds the running
  script's pid. A lock whose process is gone (a killed run) is removed
  automatically with the log line `removing stale lock`. Two runs in the
  same minute would get the same stamp; the second refuses.
- A Qdrant snapshot is deleted from Qdrant again after it was downloaded,
  also when a later step fails (the exit trap does it; if even that
  fails, the log asks you to delete it by hand).
- The dump is checked by reading its table of contents with
  `pg_restore -l`, which proves it is a complete, readable custom-format
  dump.
- A run killed outright (for example the container recreated
  mid-backup) skips the exit trap and leaves `.incomplete-<stamp>`; such
  folders older than a day are removed by the next successful run.
- `BACKUP_RETENTION_DAYS` must be a whole number of at least 1, or the
  backup stops before doing anything.
- Retention looks at folders named like a backup (`<stamp>` and
  `<stamp>-FAILED`, by the folder's modification time), never at other
  files in `./backups/`, so a hand-made `.dump` file there is never
  deleted.
- A successful run sends the `backup` heartbeat.

**Why the S3 API and not the SeaweedFS data directory:** every S3 `GET`
returns one complete object and rclone checks its MD5, so each object in
the backup is consistent even when a document is uploaded or deleted
during the run. A file copy of the live volume (filer database and volume
files written at different moments) can be torn. The objects are restored
through the same API, into any SeaweedFS version. `rclone` uses the
services' own `S3_ENDPOINT_URL`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` from
`.env.docker` (`infra/scheduler/s3-remote.sh`); the keys stay in the
environment, never on a command line.

**Checking a backup (changes nothing):**

```sh
make backups                         # list
make backup-check                    # restore dry run of the newest
make backup-check BACKUP=2026-10-01_0230
make restore-drill                   # restore the newest into a throwaway
                                     # project and verify it (DR section 6)
```

The `restore` image belongs to the `tools` profile, so `up --build` never
rebuilds it. That is why `make backup-check` and `make restore` both start
it with `docker compose ... run --rm --build restore ...`: the image is
rebuilt from the current files every time. An old `restore` image would
run an old `restore.sh` (for example one without `rclone`, which cannot
restore the `objectstore/` folders).

**Restoring (DESTRUCTIVE, replaces current data):**

```sh
make restore BACKUP=2026-10-01_0230              # dry run: checksums, dump readable, plan
make restore BACKUP=2026-10-01_0230 CONFIRM=yes  # DESTRUCTIVE real restore
```

The real restore stops `auth-service core-service ai-service scheduler`,
makes sure `objectstore` is running (`up -d --wait`; it is stopped
instead for an older backup that holds `objectstore-data.tar.gz`), runs
`restore.sh --yes` in the `restore` container (with `run --rm --build`),
then `restore.sh --verify`, which compares the running data with the
backup and stops the target on any difference, and finally `up -d`. What
`restore.sh` does per part:

- `postgres`: converts the dump to SQL first (a broken dump stops here),
  then in **one transaction** drops the `auth`, `core` and `ai` schemas
  with `CASCADE` and runs that SQL. Any error changes nothing. `CASCADE`
  matters for the "restore after a bad migration" case: the live
  database then holds objects newer than the backup, and a drop without
  it would fail. On their next start the services run `alembic upgrade
  head`, so a backup from older code is migrated forward.
- `qdrant`: uploads each snapshot with `POST /collections/<name>/
  snapshots/upload?priority=snapshot`, replacing that collection.
  Restart ai-service afterwards so it rebuilds its BM25 index from
  Qdrant.
- `objectstore`: for each `objectstore/<bucket>/` folder, creates the
  bucket if needed and makes it identical to the folder through the S3
  API (`rclone sync --checksum`: missing or changed objects are uploaded,
  objects newer than the backup are deleted). Needs `objectstore`
  running. An older backup with `objectstore-data.tar.gz` is extracted
  into the `objectstore_data` volume instead (needs the service stopped;
  `restore.sh` refuses while the S3 API answers). A backup made while the
  stack still used MinIO holds `minio-data.tar.gz`, which SeaweedFS
  cannot read: that part is skipped (and refused with `--only
  objectstore`).
- `avatars`: empties the `avatar_data` volume and extracts
  `avatars.tar.gz` (needs the volume read-write, which is why it runs in
  the `restore` container, not the scheduler; stop auth-service first).

Single parts: `--only postgres|qdrant|objectstore|avatars`, for example
(DESTRUCTIVE)
`docker compose --env-file .env.docker --profile tools run --rm --build restore /scripts/restore.sh --yes --only qdrant /backups/2026-10-01_0230`.
The full procedures, including a new machine and the monthly restore
drill, are in [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md).

The backups sit on the same disk as the live data. Copy `./backups/`
elsewhere regularly, and keep a safe copy of `.env.docker` (it is not in
the backups).

### 3.13 Logs

There are two copies of every container's output:

1. **Docker's own log**, capped at 5 files of 20 MB per container
   (`x-logging` in `docker-compose.yaml`) and lost when the container is
   recreated. Read with `make logs` (all), `bash scripts/logs.sh <svc>`
   or `docker compose --env-file .env.docker logs <svc>`.
2. **Files written by `vector`** (`infra/vector/vector.toml`):
   `./logs/<service>/<YYYY-MM-DD>.log`, where `<service>` is the Compose
   service name and the date is the **New York** date of the line. Each
   line is `<timestamp, New York, with offset> <stdout|stderr> <original
   line>`. Only compose containers are written (including `docker
   compose run` ones); containers started outside compose
   (testcontainers from `make test`, `docker run` one-offs) are skipped.
   vector only captures lines written while it runs, so keep it up with
   the rest of the stack.

Retention: `infra/scheduler/logs-cleanup.sh` runs daily at 04:00 New York
(09:00 London) and deletes `*.log` files not modified for more than
`RETENTION_DAYS` days (default 14), then removes empty folders (never
`./logs` itself, which is a mount point). `find -mtime +14` matches files
at least 15 full days old, so today's file and the previous 14 days'
files are always kept. vector closes a file after 60 seconds without new
lines, so a file is never being written when it is deleted, and the New
York date boundary of the files matches the job's time zone. By hand:
`docker compose --env-file .env.docker exec scheduler /scripts/logs-cleanup.sh`,
or on the host `LOGS_DIR=./logs sh infra/scheduler/logs-cleanup.sh`
(there it prints `... not found, success not reported`, because
`/scripts/heartbeat.sh` exists only in the container, and exits 0).

The services log JSON lines with an `event` name (structlog). Useful
events to search for:

| Event | Service | Meaning |
|---|---|---|
| `service_startup`, `service_shutdown` | all | process start and stop |
| `schema_bootstrap_complete` | all | Alembic upgrade done, with `revision_before`/`revision_after` |
| `bm25_index_rebuilt` | ai | keyword index rebuilt from Qdrant at startup, with `chunk_count` |
| `analysis_startup_reconciliation_complete` | ai | stale analyses failed at startup, with `failed_count` |
| `analysis_queue_job_started`, `analysis_queue_job_finished`, `analysis_queue_job_cancelled` | ai | queue worker progress |
| `analysis_marked_failed` | ai | an analysis failed, with `failure_reason` |
| `analysis_queue_full` | ai | a create was refused (queue cap) |
| `analysis_queue_job_skipped_owner_inactive` | ai | a queued analysis of a deactivated or deleted user was not run (marked `failed` / `cancelled`) |
| `market_open_job_completed`, `market_open_analysis_request_failed`, `market_open_owner_inactive_skipped` | core | the 09:30 job (the last one: a portfolio skipped because its owner is deactivated or deleted) |
| `tracked_tickers_inactive_owners_skipped` | core | the nightly ingestion's ticker list left out holdings and watchlists of inactive owners |
| `scheduled_job_all_units_failed` | core, ai | a scheduled run tried at least one item and every item failed; the job answers 500 and gets no heartbeat |

Example: `bash scripts/logs.sh ai-service grep analysis_marked_failed`.

### 3.14 Keeping the Mac awake

Docker Desktop runs Linux in a virtual machine. When macOS sleeps, that
machine is paused: no analysis progresses, no cron job fires, and HTTP
calls in flight can break. Typical signs are scheduled market-open runs
that start late and agents that take thousands of seconds; the macOS
power log (`pmset -g log`) then shows the Mac asleep during the
scheduled window, waking only for short maintenance moments. A job whose
time falls while the Mac sleeps runs late or not at all; a missed market
close leaves that day's snapshot to `scripts/snapshots.sh`. This limit
disappears on a server.

- **During a long run:** wrap the command in `caffeinate -i`, which
  prevents idle sleep until the command ends:
  `caffeinate -i bash scripts/run_analysis.sh AAPL MSFT NVDA` or
  `caffeinate -i make analyze T="AAPL MSFT"`.
- **For the scheduled jobs:** the scheduler is a Linux container and
  cannot call macOS `caffeinate`. The keep-awake has to be done on the
  host, for example by running `caffeinate -i` (optionally with `-t
  <seconds>`) in a terminal that stays open across the job times. How
  you do this on your Mac (a terminal session, a scheduled wake in macOS
  power settings) is outside the repository.
- **Signs the Mac slept:** a large job age in `make health`, late
  `[date] market-open: ...` lines in `logs/scheduler/<date>.log`, or a
  missing backup folder for a day. `DailyJobStale` (over 26 h) and
  `TradingDayJobStale` (over 100 h) fire 10 minutes after the limit and
  send an email to MailHog.

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `ADMIN_USER_ID` | `551b898b-e87c-4872-8bca-70333bc9d00b` | admin account used by `token.sh admin` and `e2e.sh` | `scripts/lib.sh` |
| `OWNER_USER_ID` | `5c7a7207-b4a1-4db8-8550-aae19d0a3d65` | test portfolio owner (`token.sh owner`, `tiers.sh decline`) | `scripts/lib.sh` |
| `PORTFOLIO_ID` | `d72855ea-2776-41c3-bc77-ce1a4c1435d5` | test portfolio for `run_analysis.sh`, `tiers.sh`, `snapshots.sh` | `scripts/lib.sh` |
| `FORCE` | `0` | `1` overrides the busy-window refusal | `scripts/lib.sh` |
| `GATEWAY_URL` | `https://localhost:8443` | public gateway address for scripts that call the public API (`tiers.sh decline`) | `scripts/lib.sh` |
| `HOURS` | `2` | token lifetime in `token.sh` | `scripts/token.sh` |
| `RESTART_ONLY` | `0` | `1` = restart instead of rebuild | `scripts/rebuild.sh` |
| `SINCE` | `10m` | how far back `logs.sh` starts following | `scripts/logs.sh` |
| `PROMETHEUS_LOCAL_URL` | `http://127.0.0.1:9090` | Prometheus for `health.sh` | `scripts/health.sh` |
| `QDRANT_LOCAL_URL` | `http://127.0.0.1:6333` | Qdrant for `qdrant.sh` | `scripts/qdrant.sh` |
| `POLL_INTERVAL_SECONDS` / `MAX_WAIT_SECONDS` | `5` / `3600` | polling of one analysis (fixed in the script, not environment variables) | `scripts/run_analysis.sh` |
| `LOGS_DIR` (log cleanup) | `/logs` | folder `logs-cleanup.sh` prunes; on the host `LOGS_DIR=./logs sh infra/scheduler/logs-cleanup.sh` | `infra/scheduler/logs-cleanup.sh` |
| `BACKUP_RETENTION_DAYS` | `7` | days of backups kept (newest 3 always kept) | `.env.example`, `infra/scheduler/backup.sh` |
| `BACKUP_DIR` | `/backups` | backup folder inside scheduler/restore (host `./backups`) | `docker-compose.yaml` |
| `AVATAR_DATA_DIR` | `/avatar-data` | avatar volume, read-only in the scheduler (backup), read-write in `restore` | `docker-compose.yaml` |
| `OBJECTSTORE_DATA_DIR` | `/objectstore-data` | SeaweedFS data volume, mounted read-write in `restore` only, for backups that hold `objectstore-data.tar.gz` | `docker-compose.yaml` |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | from `.env.docker` | the object storage's S3 API, used by `backup.sh` and `restore.sh` through rclone | `infra/scheduler/s3-remote.sh` |
| `RETENTION_DAYS` (log cleanup) | `14` | days of `./logs` kept | `infra/scheduler/logs-cleanup.sh` |
| `TZ` (scheduler) | `America/New_York` | cron time zone | `infra/scheduler/Dockerfile` |
| `CORE_SERVICE_URL`, `AI_SERVICE_URL` (scheduler) | `http://core-service:8000`, `http://ai-service:8000` | job targets | `docker-compose.yaml` |
| Docker log limit | `max-size: 20m`, `max-file: 5` | per container | `docker-compose.yaml` |
| `ANALYSIS_TIMEOUT_SECONDS` | `2700.0` | hard limit of one analysis | `services/ai-service/config.py` |
| `ANALYSIS_QUEUE_MAX_PENDING` | `50` | global cap of pending analyses (503) | `services/ai-service/config.py` |
| e2e lock | key `e2e:run_lock`, 45 min | one e2e run at a time | `shared/e2e_lock.py` |

## 5. Design decisions and why

**Operator scripts never read `.env.docker`.** They get values (database
user, internal token, keys) from inside the containers. The file holds
real secrets, and a script that prints or parses it is one copy-paste
away from leaking them. It also means the scripts always use exactly the
values the running containers use.

**Busy-window guard in the scripts, not in the services.** The danger is
an operator action (restart, e2e run, snapshot rewrite) colliding with a
scheduled job. Guarding the operator tools is simple and visible; `FORCE=1`
remains for a real emergency. The guard is a plain clock check: it does
not read the market calendar, so it errs on the side of refusing.

**No auto-reload; explicit restarts.** Auto-reload restarted services on
any file change and killed running analyses, WebSockets and editing
rooms. An explicit restart is one command and happens when you choose.

**Migrations at startup, in every environment.** One path builds a new
database and upgrades an old one, so a fresh install and a restored
backup end in the same state. Services refuse to guess when a schema is
in an unknown state, instead of damaging it.

**Restore is a dry run by default and runs in its own container.** The
scheduler mounts the data volumes read-only, so a mistake in the nightly
job can never overwrite data. The `restore` service has write access and
only starts when asked. The PostgreSQL part runs in one transaction, so a
failed restore leaves the database as it was.

**Backups keep the newest three whatever their age.** Retention by age
alone would, after a long sleep or outage, delete all but the newest
backup on the next successful run.

**Heartbeats plus alerts for scheduled jobs.** A failed backup or
ingestion run would otherwise leave only one log line that nobody reads.
Each job reports success, `make health` shows how old the last success
is, and the two stale-job alert rules fire when it is too old. A run in
which every item failed answers 500, so it does not count as a success.

**`run_analysis.sh` uses internal routes inside the container.** The
public create route needs a user token and the gateway blocks
`/internal`. Running a small standard-library client inside ai-service
avoids both, and the analyses are clearly marked as system analyses.

**Commands one per line in docs and demos.** Chained commands pasted
into zsh can be mangled (a `; echo` becomes part of a URL and looks like
a gateway failure). Every command in this chapter stands alone.

## 6. When things go wrong

| Problem | Likely cause | Fix |
|---|---|---|
| `required variable ... set GRAFANA_ADMIN_PASSWORD in .env.docker` (or `S3_ACCESS_KEY`) | Compose ran without `--env-file .env.docker`, or the value is empty. | Always use `docker compose --env-file .env.docker ...` or the make targets. Fill the value. |
| The browser says the connection is not private | Self-signed certificate. | Accept it for localhost; use `curl -k`. Regenerate with `sh infra/gateway/gen-certs.sh --force` if expired (825 days). |
| `https://localhost:8443` refuses the connection | The gateway is not running, usually because a service is not healthy. | `docker compose --env-file .env.docker ps`; `bash scripts/logs.sh <unhealthy service>`. |
| A service restarts in a loop with `SchemaBootstrapError` | The schema is on the archived migration chain, or has tables but no Alembic revision. | Follow the printed command (`python -m scripts.alembic_convert`, preview, then `--apply`), or restore a backup. If the container keeps restarting, use `docker compose --env-file .env.docker run --rm <svc> python -m scripts.alembic_convert`. See [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) section 8. |
| ai-service fails at startup about the embedding provider or vector size | `EMBEDDING_PROVIDER`, `QDRANT_VECTOR_SIZE` and `QDRANT_COLLECTION_RESEARCH` do not match (`check_embedding_configuration`, `ensure_collection`). | Set the three together (see `.env.example`). |
| A code change has no effect | No auto-reload. | `RESTART_ONLY=1 bash scripts/rebuild.sh <svc>`; then confirm a log line from the new code. |
| A `.env.docker` change has no effect | `restart` keeps the old environment. | `docker compose --env-file .env.docker up -d --force-recreate <services>`, then read the value back inside the container. |
| `401 Missing or invalid internal token` in logs, or scheduler jobs fail with curl exit 22 | Containers disagree on `INTERNAL_API_TOKEN` (some not recreated after a change). | Recreate the services and the scheduler together. |
| A script stops with `refused: the scheduler is busy now` | Inside 09:00-10:30 or 16:55-17:50 New York (01:55-04:30 only warns). | Wait, or `FORCE=1` if you accept the risk. |
| Analyses are `failed` with `service_restart` or `cancelled` | ai-service was restarted or stopped while they were queued or running. | Re-run them; restart only when the queue is empty. |
| An analysis stays `pending` for a long time | The queue is serial; others are ahead (up to 45 minutes each). | `bash scripts/psql.sh -c "SELECT id, ticker, status, created_at FROM ai.analyses WHERE status IN ('pending', 'running') ORDER BY created_at;"` |
| Create returns 503 (queue full) or 429 | Global cap of 50 pending analyses, or 3 pending/running per user on the public route. | Wait for the queue to drain. |
| Many analyses fail with LLM errors | Provider quota (429) or overload (503). | Check `LLMProviderErrorsHigh`, the Grafana "LLM errors by provider and kind" panel, and the provider's own status before suspecting code. `make probe` measures latency (costs quota). |
| A restore dry run or restore says `rclone: not found`, or does not know `--verify` | The `restore` image is older than the scripts (only possible when you start it without `--build`). | Use the make targets (`backup-check`, `restore`, `restore-drill`), which build the image first, or add `--build` to your `docker compose ... run` command. |
| `restore.sh` says `object storage at ... does not answer` | `objectstore` is not running, and the objects are restored through its S3 API. | `docker compose --env-file .env.docker up -d --wait objectstore`, then run the restore again. |
| `restore.sh --verify` reports `FAIL: ...` | The running data differs from the backup (a part was not restored, a service wrote after the restore, or a copy is damaged). | Read the FAIL lines; restore that part again with `--only <part>` before starting the services. |
| A `<stamp>-FAILED` folder in `./backups/` | A backup step failed; older backups were not pruned. | `bash scripts/logs.sh scheduler grep "backup: ERROR"`; fix, then `make backup`. |
| A job's age in `make health` is above 26 hours (daily job) or 100 hours (trading-day job) | The Mac slept, the scheduler is down, or the job fails. `DailyJobStale` / `TradingDayJobStale` fire after 10 minutes above the limit. | `docker compose --env-file .env.docker logs scheduler`; `make health`; section 3.14. |
| `MarketCloseSkippedPortfolios` alert | A holding had no close for the session, so no snapshot was written for some portfolios. | Later, `bash scripts/snapshots.sh --portfolio=<id> --to <date>` (preview), then the same with `--confirm` to add the missing day. |
| `OutboxBacklog` alert | core-service could not publish durable events (document indexing or removal, tier cancellation) to Redis for 15 minutes; they wait in `core.event_outbox`. | Check Redis (`bash scripts/redis-cli.sh PING`) and `bash scripts/logs.sh core-service grep outbox_publish_failed`; the dispatcher publishes them by itself once Redis works. |
| e2e run says another run holds the lock | A previous run is still running, or was killed. | Check `docker compose --env-file .env.docker top core-service` / `top ai-service`; if nothing runs, `bash scripts/e2e.sh core --break-lock`. |
| e2e auth checks hit 429 | auth-service's limits (10 registrations per hour, 30 logins per 15 minutes per address) or the gateway's 10 req/s on `/api/v1/auth`. | Wait for the window, then re-run. |
| Ctrl+C did not stop a script's work | `docker compose exec -T` does not forward Ctrl+C to the process in the container. | `docker compose --env-file .env.docker top <svc>` to see it. |
| Grafana rejects the password from `.env.docker` | The password applies only when the Grafana volume is first created. | `docker compose --env-file .env.docker exec grafana grafana cli admin reset-admin-password '<new password>'` |
| No new lines in `./logs/` | vector is not running. | `docker compose --env-file .env.docker up -d vector`; lines from the time it was down exist only in Docker's own log. |
| Port 8443 or 8000 already in use | Another program uses it. | Stop that program, or change the mapping in `docker-compose.yaml` **and** `$external_https_port` in `infra/gateway/nginx.conf`. |

## 7. Demo walkthrough: bring the stack up and show it is healthy

Goal: start AlphaMind from stopped and prove, layer by layer, that it is
healthy. Nothing in this script deletes or overwrites data. Run each
command on its own line, from the repository root.

1. **Check the time.** Say: "The scheduler works in New York time; I avoid
   its busy windows for restarts."
   ```sh
   TZ=America/New_York date
   ```
   The audience sees the New York time. If it is between 09:00-10:30 or
   16:55-17:50, mention that scheduled jobs may be running.

2. **Show that Docker is running and the settings file exists** (without
   showing its content).
   ```sh
   docker info --format '{{.ServerVersion}}'
   ```
   ```sh
   ls -l .env.example .env.docker
   ```
   Say: "`.env.docker` holds the secrets; it is never committed or
   printed. `.env.example` is the documented template."

3. **Create the certificate if missing.**
   ```sh
   make certs
   ```
   They see either `wrote ... server.crt` or `already exists`.

4. **Start the stack in the background.**
   ```sh
   docker compose --env-file .env.docker up -d --build
   ```
   They see the containers being created and started; the gateway is
   started last, after the three services report healthy.

5. **Watch the containers become healthy.** Repeat until every health
   check says `healthy`:
   ```sh
   docker compose --env-file .env.docker ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
   ```
   They see 17 services `running`, with `(healthy)` on postgres, redis,
   qdrant, objectstore, auth-service, core-service, ai-service and
   frontend. Say:
   "The gateway only starts when all three services pass their health
   checks; ai-service gets 90 seconds of start time because it rebuilds
   its keyword index from Qdrant."

6. **The one-screen health view.**
   ```sh
   make health
   ```
   They see the container table, the last success time of each scheduled
   job, the market-close skipped count, and `none` under firing alerts.

7. **Health through the gateway, one service per line.**
   ```sh
   curl -sk https://localhost:8443/health/auth
   ```
   ```sh
   curl -sk https://localhost:8443/health/core
   ```
   ```sh
   curl -sk https://localhost:8443/health/ai
   ```
   They see `"status":"ok"` with `database`, `redis` (and `storage`,
   `qdrant`) all `true`. Say: "Any false here turns the answer into a 503
   and the container into unhealthy." The frontend has its own check,
   `https://localhost:8443/healthz`, which answers `{"status":"ok"}` only
   when the frontend reaches the API.

8. **The status page.** Open `https://localhost:8443/status`. Three green
   rows, refreshing every 15 seconds.

9. **Plain HTTP only redirects.**
   ```sh
   curl -sI http://localhost:8000/status
   ```
   They see `HTTP/1.1 301` and `Location: https://localhost:8443/status`.

10. **Metrics and alerts.** Open `http://127.0.0.1:9090/targets`: every
    target (auth-service, core-service, ai-service, postgres, redis,
    alertmanager, prometheus) is `UP`. Open `http://127.0.0.1:9090/alerts`:
    the rules are listed, none firing. Open `http://127.0.0.1:9093`: no
    active alerts.

11. **Dashboards.** Open `https://localhost:8443/grafana/`, log in as
    `admin`, open "AlphaMind - Service Overview" (request rate, p95
    latency, 5xx rate by service) and "AlphaMind - Infrastructure & AI
    pipeline" (targets, firing alerts, analysis queue, LLM errors,
    PostgreSQL and Redis panels).

12. **The data stores answer.**
    ```sh
    bash scripts/psql.sh -c "SELECT 'auth' AS schema, version_num FROM auth.alembic_version UNION ALL SELECT 'core', version_num FROM core.alembic_version UNION ALL SELECT 'ai', version_num FROM ai.alembic_version;"
    ```
    They see one Alembic revision per schema.
    ```sh
    bash scripts/redis-cli.sh INFO persistence
    ```
    They see `aof_enabled:1` among the lines (append-only file on).
    ```sh
    make qdrant
    ```
    They see `research_documents_gemini` with its point count.
    ```sh
    make objectstore
    ```
    They see the `research-documents` bucket with its object count and
    size.

13. **Mail and the vector dashboard.** Open `http://127.0.0.1:8025`
    (MailHog inbox) and `http://127.0.0.1:6333/dashboard` (Qdrant
    collections). Say: "Developer tools are on 127.0.0.1 only; another
    machine on the network cannot open them."

14. **Logs on disk.**
    ```sh
    ls logs/
    ```
    ```sh
    bash scripts/logs.sh ai-service grep schema_bootstrap_complete
    ```
    They see one folder per service and today's startup line of
    ai-service with its Alembic revision. Say: "vector writes every
    container's output here by New York date; files are kept 14 days."

15. **Backups exist and are valid.**
    ```sh
    make backups
    ```
    ```sh
    make backup-check
    ```
    They see the backup folders with sizes, then `checksums OK` and the
    `DRY RUN` plan of what a restore would do. Say: "The restore is a dry
    run unless I add CONFIRM=yes, which I never do in a demo."

16. **Feature smoke check without spending LLM quota.**
    ```sh
    make smoke
    ```
    They see one `OK`/`SKIP` line per check and a summary. (It refreshes
    AAPL's cached prices and creates and deletes one scratch backtest;
    nothing else is written.)

Optional, if time allows and you want to show alerting live (it stops
one exporter for about two minutes; no data is touched):

17. `docker compose --env-file .env.docker stop redis-exporter`. After
    about one minute `ServiceDown` turns firing in
    `http://127.0.0.1:9090/alerts`, and about half a minute later an
    email arrives in MailHog. Then
    `docker compose --env-file .env.docker start redis-exporter`; a
    resolved email follows.

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **You edited `services/core-service/domains/portfolio/service.py`. What
   do you run?** `RESTART_ONLY=1 bash scripts/rebuild.sh core-service`,
   outside a busy window. No rebuild is needed: the source is
   bind-mounted.
2. **You added a package to `pyproject.toml`. What do you run?** Lock it
   (`uv lock` on the host), then rebuild the images:
   `bash scripts/rebuild.sh auth-service core-service ai-service`. The
   build uses `uv sync --frozen`, so a stale lock fails the build.
3. **You changed `INTERNAL_API_TOKEN`. Is `docker compose restart`
   enough?** No. Containers keep the environment they were created with.
   Recreate the services and the scheduler with
   `docker compose --env-file .env.docker up -d --force-recreate ...`.
4. **When do database migrations run?** At every service start, in every
   environment: `ensure_schema` runs `alembic upgrade head` on that
   service's schema.
5. **Why does `scripts/rebuild.sh` refuse at 09:40 New York?** It is in
   the market-open busy window (09:00-10:30). Restarting ai-service would
   fail the analyses the 09:30 job queued.
6. **What is in a `runs/run_<timestamp>/` folder?** `_summary.txt` and per
   ticker: `_result.json`, `_agent_reports.txt`, `_tiers.txt`,
   `_filings.txt`, `_ai-service.log` (and `_create_ERROR.txt` if the
   create failed).
7. **`make restore BACKUP=2026-10-01_0230` - does it change data?** No,
   without `CONFIRM=yes` it is a dry run. With `CONFIRM=yes` it is
   destructive.
8. **Where do you find yesterday's ai-service logs after the container
   was recreated?** `logs/ai-service/<yesterday's New York date>.log`,
   written by vector, or `bash scripts/logs.sh ai-service grep <pattern>
   <date>`.
9. **Why wrap analysis batches in `caffeinate -i`?** If the Mac sleeps,
   Docker Desktop's VM pauses, the analysis stalls and HTTP calls can
   break.
10. **How many backups survive a two-week holiday with the Mac asleep?**
    At least the newest three complete ones: pruning only happens after
    a successful backup, and the newest three are always kept.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `Makefile` | Build, test, lint and operator targets (each operator target runs one script from `scripts/`). |
| `.env.example` | Template for `.env.docker`, with every setting explained. |
| `docker-compose.yaml`, `docker-compose.override.yml` | The stack and the development override. |
| `scripts/lib.sh` | Repository root, Compose command, default ids, `psql_stdin`, `make_token`, busy windows. |
| `scripts/token.sh` | Access tokens. |
| `scripts/health.sh` | Health overview from Compose and Prometheus. |
| `scripts/psql.sh`, `scripts/redis-cli.sh`, `scripts/qdrant.sh`, `scripts/objectstore.sh` | Shells and views of the data stores. |
| `scripts/logs.sh` | Follow or grep logs. |
| `scripts/rebuild.sh` | Rebuild or restart, wait for health. |
| `scripts/run_analysis.sh` | Real analyses with an output folder. |
| `scripts/tiers.sh` | Open tiers; decline. |
| `scripts/snapshots.sh` | NAV snapshot recompute (preview / `--confirm`). |
| `scripts/backups.sh` | List, make, dry-run-check and drill backups. |
| `scripts/probe.sh`, `scripts/smoke.sh`, `scripts/e2e.sh` | Latency probe, smoke check, end-to-end checks. |
| `services/ai-service/scripts/analysis_cli.py` | Internal analysis client (create/status/get). |
| `services/ai-service/scripts/smoke_check.py` | Smoke checks. |
| `services/ai-service/scripts/e2e_tier_check.py` | Tier chain end-to-end check. |
| `services/ai-service/scripts/probe_model_latency.py` | Gemini latency probe. |
| `services/*/scripts/*.py` | Every Python operator script (index in section 3.5b). |
| `rag_seed_documents/` | Seed corpus for the RAG store (section 3.2 step 8). |
| `shared/scheduled_jobs.py` | The 200/500 answer rule of the scheduler-triggered routes. |
| `services/core-service/scripts/e2e_check.py` | Whole-stack end-to-end check. |
| `services/core-service/scripts/recompute_snapshots.py` | NAV snapshot recompute. |
| `shared/e2e_lock.py` | The e2e run lock. |
| `shared/alembic_bootstrap.py`, `services/*/db_bootstrap.py` | Migrations at startup. |
| `infra/gateway/gen-certs.sh` | Self-signed certificate. |
| `infra/scheduler/crontab`, `entrypoint.sh`, `heartbeat.sh`, `call-job.sh`, `logs-cleanup.sh` | The scheduler and its jobs. |
| `infra/scheduler/backup.sh`, `infra/scheduler/restore.sh`, `infra/scheduler/s3-remote.sh` | Backup, restore and verify (`--verify`); the rclone remote for the object storage's S3 API. |
| `infra/drill/docker-compose.drill.yml` | Compose override of the restore drill. |
| `services/core-service/job_health.py` | Job heartbeat gauge. |
| `infra/prometheus/`, `infra/alertmanager/`, `infra/grafana/provisioning/`, `infra/vector/vector.toml` | Monitoring and logging configuration. |

## 10. Related chapters

- [01-architecture.md](01-architecture.md) - what all these containers are
- [10-gateway.md](10-gateway.md) - TLS, routing, the status page
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md) - what happens inside an analysis
- [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md) - tier monitoring and backtests
- [20-postgresql.md](20-postgresql.md) - schemas and migrations
- [23-redis.md](23-redis.md) - logical databases, streams
- [26-scheduler-jobs.md](26-scheduler-jobs.md) - each scheduled job in detail
- [27-observability.md](27-observability.md) - logs, metrics, alerts, dashboards
- [28-email.md](28-email.md) - MailHog and SMTP
- [31-testing-quality.md](31-testing-quality.md) - tests, lint, CI
- [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) - backup and restore procedures
- [../API_SPEC.md](../API_SPEC.md) - every public route
