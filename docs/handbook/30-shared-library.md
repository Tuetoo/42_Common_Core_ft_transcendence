# 30. The shared library (`shared/`)

> `shared/` is a small Python package that all three services import:
> settings, database plumbing, Redis and HTTP clients, events, errors,
> security, permissions, rate limiting, object storage, email, the market
> calendar, the ticker rule, the live user state, caches, locks, schema
> bootstrapping, Prometheus collector helpers, the scheduled-job answer
> rule, enums and cross-service schemas. It exists so that a
> rule that must be the same everywhere (what a ticker is, which role may
> do what, how a 429 looks, which Redis database holds what) is written
> once. This chapter lists every module, what it provides and who uses
> it, explains how each service gets the code at run time, in tests and
> in type checking, and gives the rule for what belongs here.

---

## 1. The big picture

```
                     repo root
                        |
       +----------------+-----------------+
       |                                  |
   shared/                           services/
   (one copy in git)                  auth-service/  core-service/  ai-service/
       |                                  |               |              |
       |   production image:  COPY shared /app/shared, COPY services/<svc>/ /app/
       |   development:       bind mount ./shared -> /app/shared
       |                      bind mount ./services/<svc> -> /app
       v
   inside every container:   /app/shared/...   /app/main.py, /app/config.py, ...
                             PYTHONPATH=/app  ->  "import shared.security" works

   tests:   tests/<svc>/conftest.py puts <repo root> and services/<svc> on sys.path
   mypy:    MYPYPATH=<repo root>  (+ --explicit-package-bases)
   flake8:  uv run flake8 --extend-exclude mistral_provider.py services shared tests infra
```

Direction of dependencies: services import `shared`; `shared` never
imports a service module (no `config`, `database` or `domains` import
anywhere under `shared/`). Within `shared/`, `config.py` is the root that
almost everything else depends on.

```
 config.py <- database.py <- alembic_bootstrap.py <-> alembic_convert.py
     ^                         (these two import each other inside functions)
     |    <- redis.py <- events.py, rate_limit.py, user_state.py,
     |                   org_membership_cache.py, e2e_lock.py
     |    <- http_client.py <- user_state.py, security.py
     |    <- security.py (uses permissions.py, user_state.py,
     |                    INTERNAL_TOKEN_HEADER from http_client.py)
     |    <- logging.py, storage.py, email.py
 exceptions.py <- org_membership_cache.py, rate_limit.py (error_response)
 enums.py <- permissions.py, schemas.py
 (no shared imports at all): exceptions.py, tickers.py, market_calendar.py,
                             concurrency.py, events_registry.py,
                             request_id.py, metrics.py, scheduled_jobs.py
```

Read `A <- B` as "B imports A".

---

## 2. Concepts you need

| Term | Meaning |
|---|---|
| Package / module | A module is one `.py` file; a package is a directory of modules you import with dots (`shared.security`). |
| Namespace package | A package directory without `__init__.py`. Python 3 still imports it. No folder in the repo has an `__init__.py`. |
| `PYTHONPATH` | Extra directories Python searches for imports. The images set `PYTHONPATH=/app`, so `/app/shared` is importable as `shared`. |
| Bind mount | A Docker volume that maps a host directory into a container. In development `./shared` is mounted at `/app/shared`, so editing a file on the host changes it in the container (a restart is still needed; uvicorn runs without auto-reload). |
| Settings object | A pydantic-settings class that reads environment variables into typed fields. `BaseServiceSettings` is the shared base; each service subclasses it. |
| Dependency (FastAPI) | A function FastAPI calls before a route (`Depends(...)`), used here for the database session, the current user and the internal-token check. |
| Singleton / pool | One shared object per process (for example one Redis connection pool per database number), created on first use and reused. |
| `MYPYPATH` | Extra directories mypy searches for imports; it must contain the repo root so `shared.*` is type-checked as real code. |

---

## 3. How it works in AlphaMind

### 3.1 How each service sees `shared/`

- **Production image** (`services/<svc>/Dockerfile`, stage `production`):
  `COPY shared /app/shared`, then `COPY services/<svc>/ /app/`, and
  `ENV PYTHONPATH=/app`. The build context is the repo root, which is
  why compose builds with `context: .`. `.dockerignore` keeps the rest of
  the repo (venv, caches, backups, logs, `.env.docker`) out of the build.
- **Development** (`docker-compose.override.yml`): the services use the
  `development` stage and bind-mount `./shared:/app/shared` and
  `./services/<svc>:/app`. Because the second mount sits inside the
  first, Docker creates an empty `shared/` folder inside each service
  directory on the host. It is harmless and ignored by git
  (`.gitignore`). A change to a shared
  file reaches all three services, but only after you restart them
  (`RESTART_ONLY=1 make rebuild S="auth-service core-service ai-service"`).
- **Tests**: each `tests/<svc>/conftest.py` inserts the repo root and
  `services/<svc>` at the front of `sys.path`.
- **mypy**: `MYPYPATH=$(CURDIR)` (the repo root) on each `mypy-*` Makefile
  target. Without it, `shared.*` imports would not be followed as real
  code.
- **flake8**: `uv run flake8 --extend-exclude mistral_provider.py services shared tests infra`
  (`make lint`).
- The **scheduler** image does not contain `shared/`; its jobs are shell
  scripts that call the services' internal routes.

Because the code is copied into each image, a change to `shared/` needs
**all three** service images rebuilt in production
(`make rebuild S="auth-service core-service ai-service"`), otherwise the
services run different versions of the same rule.

### 3.2 Module by module

The "Used by" lists below come from searching `services/` for imports of each
module (scripts included).

#### `config.py` -- settings

Provides `BaseServiceSettings` (pydantic-settings, `case_sensitive=True`,
`extra="ignore"`, no `env_file`: values come only from the environment.
Containers get theirs from compose (`env_file: .env.docker`), host
commands from `--env-file .env.docker`, tests from `os.environ`; reading a
`.env` file from the working directory would let a stray file leak into
test runs) and `get_settings()` (cached with `lru_cache`). Fields
shared by every service: `SERVICE_NAME`, `APP_ENV`, `API_V1_PREFIX`
(`/api/v1`), the `POSTGRES_*` values and `POSTGRES_SCHEMA`, `DATABASE_URL`
/ `SYNC_DATABASE_URL`, `DB_POOL_SIZE` (20) and `DB_MAX_OVERFLOW` (10),
`REDIS_*` and the four Redis database numbers, `JWT_SECRET`,
`JWT_ALGORITHM`, `INTERNAL_API_TOKEN`, `USER_STATE_CHECK_ENABLED`,
`USER_STATE_CACHE_TTL_SECONDS`, the three service URLs,
`FRONTEND_BASE_URL`, `SMTP_*`, `BACKEND_CORS_ORIGINS` (with
`cors_origins()`), `LOG_LEVEL`, `LOG_JSON`, and the `S3_*` values. Helper
methods build the async (`postgresql+asyncpg://`), sync
(`postgresql+psycopg://`) and Redis URLs; an explicit `DATABASE_URL`,
`SYNC_DATABASE_URL` or `REDIS_URL` wins over the parts. Two validators
refuse to start outside development/test with a weak `JWT_SECRET`
(placeholder `CHANGE_ME`, empty, or shorter than 32 characters) or an
empty `INTERNAL_API_TOKEN` (chapter 29); in development/test they only
warn, the internal-token warning once per process (every service builds
two settings objects).

Pool sizing: a connection is held only for one database round trip, not
the whole request, so the pool is modest. `(DB_POOL_SIZE +
DB_MAX_OVERFLOW) x replicas x 3 services` must stay well under
PostgreSQL's `max_connections` (default 100). With the defaults and one
replica per service that is 90 connections, already close to the limit,
so raise `max_connections` before running more replicas.

Used by: every service's `config.py` subclasses it
(`AuthServiceSettings`, `CoreServiceSettings`, `AIServiceSettings`) and
adds its own fields and its schema name (`auth`, `core`, `ai`). Inside
`shared/`, the internal-token check (`security.require_internal_token`)
and the HTTP client hook use `get_settings()`, a second settings object
built from the same environment.

#### `database.py` -- SQLAlchemy plumbing

- `Base`: the declarative base for every model, with a naming convention
  for indexes (`ix_`), unique (`uq_`), check (`ck_`), foreign key (`fk_`)
  and primary key (`pk_`) constraints, so Alembic generates stable names.
- `create_service_engine(settings)`: async engine with the pool sizes,
  `pool_pre_ping=True`, and the schema search path set through asyncpg's
  `server_settings` (`search_path = <schema>,public`). The setting is
  applied in the wire protocol when the connection is opened, so it is
  correct before any query can run. Setting it from a SQLAlchemy
  `connect` event is not reliable: some pooled connections ended up with
  PostgreSQL's default search path, and the service's tables were then
  invisible on that connection (`relation "X" does not exist`).
- `create_session_factory(engine)`: `expire_on_commit=False`,
  `autoflush=False`.
- `make_get_db(factory)`: the request session dependency; commits on
  success, rolls back on error. Routes declare it with
  `Depends(get_db, scope="function")` so the commit happens before the
  response and before background tasks (chapter 28, section 3.4). With
  FastAPI's default ("request") scope the commit would run after the
  response and after the background tasks: a client could see a 2xx for a
  write that later fails to commit, and events published in the
  background could precede the commit.
- `count_query(session, stmt)`: a real `SELECT COUNT(*)` over a
  subquery, for paginated lists (loading every row just to count it would
  be costly once a table holds thousands of rows).
- `check_database_connection(engine)`: `SELECT 1` for `/health`.
- Column mixins: `UUIDPKMixin` (`id` UUID primary key with
  `server_default=gen_random_uuid()`) and `TimestampColumnsMixin`
  (`created_at`, `updated_at` with `server_default=now()`, `updated_at`
  also `onupdate=now()`). Used by model classes in all three services.

Used by: each service's `database.py` (`engine`, `SessionLocal`,
`get_db`), every model module (`Base` and the mixins), the repositories.

#### `logging.py` -- structured logs

`configure_logging(settings)`: structlog with the service name, log level,
ISO timestamp and exception info on every line; JSON output when
`LOG_JSON=true` (the default), a console renderer otherwise. It lowers
`httpx`, `httpcore`, `botocore`, `urllib3` and `s3transfer` to WARNING so
request URLs with API keys and S3 key ids never reach the logs. It also
installs `QueryStringFilter` on the `uvicorn.access` and `uvicorn.error`
loggers (`install_query_string_filter`, idempotent): every log argument
that is a path with a query string becomes `<path>?[redacted]`, so
uvicorn's access line and its WebSocket "accepted" line never carry the
WebSocket `?token=<jwt>` or the OAuth `?code=...&state=...` (chapter 29,
section 3.1). Called from each service's lifespan, so no uvicorn flag or
logging config file is needed.

#### `redis.py` -- Redis clients

One `redis.asyncio` pool per (URL, database number), `decode_responses=True`,
`max_connections=20`, `socket_timeout=15` and `socket_connect_timeout=15`.

| Function | Database setting | Default | Holds |
|---|---|---|---|
| `get_cache_client` | `REDIS_DB_CACHE` | 0 | caches, OAuth state and 2FA-challenge markers, membership cache, job heartbeats, NewsAPI usage log and source catalog, API key validation cache, e2e lock |
| `get_rate_limit_client` | `REDIS_DB_RATE_LIMIT` | 1 | rate-limit counters (auth, public API, RAG assistant) and the API key `last_used_at` gate |
| `get_events_client` | `REDIS_DB_EVENTS` | 2 | pub/sub events, the live user state |
| `get_queue_client` | `REDIS_DB_QUEUE` | 3 | durable event streams |

Two rules protect this layout. First, a database number inside
`REDIS_URL` (for example `redis://redis:6379/0`) overrides the `db=`
argument in `redis.from_url`, so all four clients would silently use
database 0 (the durable event stream would then sit in database 0, where
nobody looks). The module therefore strips any database path from the
URL, always; the four `get_*_client` functions decide the database.
Second, redis-py's default 5-second socket timeout races the 5-second
blocking stream read in `events.py`, so idle reads fail with a timeout;
15 seconds outlasts the longest block, and it is set for all four
databases so a future blocking command anywhere is safe too. Pools are
kept in a dictionary keyed by (URL, database), not with `lru_cache` on
the settings, because pydantic settings objects are not hashable. Also
`check_redis_connection` (a `PING` on the cache client) and
`close_redis_connections`.

Used by: all three services (caches, limits, events), and most other
shared modules.

#### `events.py` and `events_registry.py` -- events between services

`events_registry.py` is the list of every event name (`EventName`, a
`StrEnum`, for example `document.uploaded`, `analysis.completed`,
`user.deactivated`) and `EVENT_OWNERS`, which service publishes each one.
The payload of every event is documented in the table of chapter 24,
section 3.1.

`events.py` has two transports (chapter 24):

- **Broadcast**, Redis pub/sub: `publish_cross_service` (adds a random
  `event_id` to each message) and `CrossServiceSubscriber`. Every pub/sub
  listener runs on `run_pubsub_listener(settings, name=..., subscribe=...,
  handle=...)`: open a pubsub connection, subscribe, log
  `<name>_started`, hand every message to `handle`, and on any error log
  `<name>_failed` (or `<name>_stream_ended` when the stream closes),
  close the connection and retry with backoff (1, 2, 5, 10, 30 seconds;
  `RECONNECT_BACKOFF_SECONDS`, reset after a successful subscribe).
  `CrossServiceSubscriber` uses `name="cross_service_subscriber"`. At
  most once: a subscriber that is down misses the message. Handlers run
  one after another in the listener loop, which keeps the order of
  events.
- **Durable**, Redis Streams: `publish_durable_event` (stream
  `stream:<name>`, trimmed to about 10000 entries) and
  `DurableEventSubscriber`. It reads as part of a consumer group named
  `workers`. Each subscriber must use a stable consumer name (the same
  name after a restart), so it can pick up its own unfinished messages.
  It reclaims stuck messages (`XAUTOCLAIM` after 60 seconds idle). After
  5 failed deliveries it dead-letters a message: it acknowledges it and
  logs `durable_event_dead_lettered`. The subscriber never stops on its
  own: creating the consumer groups is part of its retry loop, so a
  Redis that is unreachable at start-up only delays it, and a `NOGROUP`
  error (the stream and its group were lost, for example after a Redis
  reset, and a producer's `XADD` created the stream again without the
  group) makes it create the groups again (from id `0`, so the new
  messages are read). Every error is logged as
  `durable_event_subscriber_loop_error` (with `groups_ready` and
  `retry_in_seconds`) and retried with the same backoff as pub/sub;
  `durable_event_subscriber_started` is logged each time the groups are
  (re)created. Durable events are used where
  losing an event is a real bug: document uploaded or deleted, document
  status changed, portfolio deleted. core-service does not call
  `publish_durable_event` straight from its routes: it first stores the
  event in its outbox table and publishes it from there
  (`services/core-service/outbox.py`, chapter 24).
- **Background tasks.** `start_background_task(coro, name)` creates a
  named task with `log_unexpected_task_end` as its done-callback: a task
  that ends by an exception logs `background_task_crashed` (with the
  traceback), one that returns logs `background_task_ended`, and a
  cancelled one (normal shutdown) logs nothing. Long-running listeners
  should be started with it, so their end is never silent.

Used by: auth-service (`events_out.py`, presence), core-service
(`event_listener.py`, `websocket_manager.py`, `yjs_relay.py`, portfolio
and research domains), ai-service (`event_listener.py`, `ai_events.py`,
workflows, analysis queue).

#### `http_client.py` -- service-to-service HTTP

`get_http_client(timeout=5.0)`: one pooled `httpx.AsyncClient` per timeout
value (`max_connections=100`, `max_keepalive_connections=20`). Every
`clients/*.py` module must take its client from here instead of opening
`httpx.AsyncClient(...)` per call: a per-call client opens a new
connection for every request, so a list endpoint that resolves N
references concurrently would open N fresh connections instead of
reusing a small keep-alive pool. The only race is the first construction
for a timeout value, which is harmless (each service uses two or three
timeout values).
`build_http_client(timeout, transport)` makes a new one (tests pass an
`httpx.MockTransport`). Every client has a request hook that adds
`X-Internal-Token` to calls to another AlphaMind service's `/internal/*`
path and to nothing else. `internal_headers()` gives the header for other
clients; `close_http_clients()` for shutdown.

Used by: every `clients/*.py` in core-service and ai-service, the
ai-service data providers (`ai/providers/fred.py`, `newsapi.py`,
`sec_edgar.py`, `tavily.py`; their hosts are not AlphaMind services, so
the hook never adds the token), auth-service's admin domain,
`user_state.py`, a few scripts, and each `main.py` (to close the clients
at shutdown).

#### `exceptions.py` -- errors and the error body

`AppError` and its subclasses with HTTP status codes
(`EXCEPTION_STATUS_MAP`): `NotFoundError` 404 (code `not_found`),
`ConflictError` 409 (`conflict`), `ValidationAppError` 422
(`validation_error`), `PermissionDeniedError` 403 (`permission_denied`),
`InvalidCredentialsError` 401 (`invalid_credentials`),
`RateLimitExceededError` 429 (`rate_limit_exceeded`),
`UpstreamServiceError` 502 (`upstream_service_error`),
`ServiceUnavailableError` 503 (`service_unavailable`; a subclass may set
`retry_after_seconds`, which the handler sends as `Retry-After`; used by
ai-service's `ReviewUnavailableError`, code `review_unavailable`).
`_status_for`
walks the class hierarchy (MRO), so a service's own subclass (for example
`TwoFactorRequiredError`) inherits its nearest registered parent's status
without being registered itself; an `AppError` with no registered
ancestor gets 400. `register_exception_handlers(app)` turns them into
`{"error": {"code", "message", "request_id"}}` (logged as `app_error`)
and turns any other exception into a 500 with code
`internal_server_error` (logged as `unhandled_exception`).
`error_response(request, status_code, code, message, headers=None)` is
the one builder of that body (also used by `rate_limit.py` and core's
API key middleware). FastAPI's own `HTTPException` (used by
`security.py`) keeps FastAPI's `{"detail": ...}` body.

**The default message is the class attribute `default_message`.**
`AppError.__init__` uses `message or self.default_message or self.code`.
So an error raised without a message (for example
`raise InvalidCredentialsError()` in auth-service's login, or
`TwoFactorRequiredError()`) shows its `default_message` to the client
("Email/password combination is incorrect.", "Login succeeded on password
but the account requires a TOTP code."). A subclass that sets no
`default_message` inherits its parent's; `AppError` itself has an empty
one, so a bare `AppError()` falls back to the code (`app_error`).

The `request_id` comes from `request.state.request_id`, which
`request_id.py` sets for every HTTP request (a random UUID is used only
if it is missing). So the id in an error body is the same id that is on
every log line of that request. The 500 handler logs it explicitly with
`unhandled_exception` (it runs outside the middleware's log context) and
also returns it in the `X-Request-ID` header.

Used by: all three services (domain code raises these).

#### `request_id.py` -- one id per HTTP request

`RequestIdMiddleware` is pure ASGI middleware (streaming and SSE responses
are not buffered), added in all three `main.py` files. For HTTP requests
only (WebSockets pass through unchanged) it uses the caller's
`X-Request-ID` when it matches `[A-Za-z0-9._-]{1,64}`, otherwise a new
`uuid4` hex. It stores the id in `request.state.request_id` (the error
body's `request_id`), binds `request_id` as a structlog context variable
for every log line of the request, sets the module's context variable
(read with `current_request_id()`, `None` outside a request) for the
duration of the request, and returns it in the `X-Request-ID` response
header. `schemas.APIResponse` uses `current_request_id()` as the default of
its `request_id` field, so every success body built with `APIResponse`
carries the same id as the header and the logs, without any route passing
it. The context variable is copied into the threadpool that runs sync
routes and validation, so sync routes get the id too; an `APIResponse`
built outside a request (a script, a background task after the response)
has `request_id: null`.

#### `security.py` -- authentication dependencies

`CurrentUser(user_id, role, auth_time=None)` (the id as `uuid.UUID`;
`auth_time` is the token's integer `auth_time` claim, the Unix time of the
login that started the session, or `None`; auth-service uses it for the
recent-login rule of 2FA setup, chapter 29), `decode_token`,
`make_get_current_user(settings)`,
`require_permission(permission)(get_current_user)`, `ensure_permission`,
`require_internal_token`, `is_uuid_claim` (`INTERNAL_TOKEN_HEADER` is
defined once, in `http_client.py`, and imported here).
Details in chapter 29. Used by every service's `deps.py`, all
`api/internal.py` routers, core's WebSocket helpers, and auth-service's
special-token decoding.

#### `permissions.py` -- roles to permissions

`Permission` (12 values such as `portfolio:write`, `rag:ask`,
`user:manage`), `ROLE_PERMISSIONS`, `permissions_for`, `has_permission`,
`sorted_permissions`. The single source of truth: route code never
checks role names. Role summary: `admin` = everything a user can do plus
user management (list, view, edit profile and role, deactivate,
reactivate, delete) and site statistics; `moderator` = everything a user
can do plus hiding or deleting anyone's notes and comments and
deactivating (not deleting or re-roling) users, never acting on an admin
or another moderator; `user` = normal use; `guest` = read-only (own
account, password, 2FA, friends, and accepting invitations so shared
content becomes visible). Used by all three services (route dependencies, the
`/me` response, core's public API middleware, ai-service internal routes).

#### `rate_limit.py` -- the shared fixed-window counter

`fixed_window_hits(settings, keys, window)` counts one hit per key in a
Redis transaction (`SET NX EX`, `INCR`, `TTL`) and returns
`(count, seconds_left)` per key, or `None` when Redis fails (the caller
allows the request; logged `rate_limit_unavailable`). The window starts
at the first hit. A non-positive TTL is reported as the full window. `rate_limited_response` builds the 429 with
`Retry-After` through `shared.exceptions.error_response`. Used by auth-service (`rate_limit.py`) and core-service
(`middleware.py`). ai-service's RAG limit has its own per-minute and
per-day counters in `domains/rag/qa.py`.

#### `storage.py` -- object storage (S3 API)

One boto3 client per endpoint (a dictionary keyed by endpoint URL, for the
same reason as the Redis pools), pointed at `S3_ENDPOINT_URL` (SeaweedFS
in compose, reachable as `minio:9000`). boto3 was chosen over a
vendor-specific library because it is the same code path for SeaweedFS,
AWS S3 or Cloudflare R2; changing the store is a configuration change
only. `ensure_bucket` is idempotent and
runs at every startup. `close_storage_clients` only clears the cache
(boto3 holds no persistent connection), for symmetry with the other
`close_*` functions. Every call runs in
`asyncio.to_thread` because boto3 is synchronous. For the type checker,
the client is typed as `S3Client` from the `mypy_boto3_s3` stub package.
That import sits under `if TYPE_CHECKING:`, so it only runs during type
checking, never at run time (the stubs are a dev dependency, see
chapter 31). Functions:
`ensure_bucket`, `upload_bytes`, `download_bytes`, `delete_object`,
`check_storage_connection`, `close_storage_clients`. Used by core-service
(upload and delete of research documents, `main.py` bucket check) and
ai-service (reading documents for RAG, SEC filing ingestion), plus a few
scripts (chapter 22).

#### `email.py` -- SMTP email

`send_email` (SMTP), `send_quietly` (log a failure instead of raising,
for emails sent after the response), `EmailSendError` and four template
helpers. Used by auth-service (account emails) and core-service
(notification emails). See chapter 28.

#### `market_calendar.py` -- New York time and trading days

- `MARKET_TZ = ZoneInfo("America/New_York")` and `market_today(now=None)`
  (today's date in New York; a naive `now` counts as UTC). One shared
  definition instead of seven copies in core and ai.
- Rules-based, no third-party package: the NYSE holiday rules have been
  stable for decades. New Year's Day on a Sunday is observed on Monday
  January 2; on a Saturday it is NOT observed on the Friday before (that
  Friday is in the previous year and stays a trading day). Juneteenth,
  Independence Day and Christmas move Saturday -> Friday and Sunday ->
  Monday. Good Friday uses the anonymous Gregorian Easter algorithm
  (Meeus/Jones/Butcher). `nyse_holidays` is cached per year
  (`lru_cache(maxsize=64)`).
- `nyse_holidays(year)` (rules for New Year, Martin Luther King Jr. Day,
  Washington's Birthday, Good Friday, Memorial Day, Juneteenth from 2022,
  Independence Day, Labor Day, Thanksgiving, Christmas, with the weekend
  shifts), `EXTRA_CLOSED_DATES` (one-off closures announced by the
  exchange: 2018-12-05 for President G.H.W. Bush and 2025-01-09 for
  President Carter, national days of mourning; add new ones by hand),
  `is_trading_day`, `previous_trading_day` (the last trading day strictly
  before a date).

Used by: core-service (scheduler internal routes, portfolio service and
ledger, snapshots, `ai_client`), ai-service (tier monitoring, market data
cache, internal routes, a verification script). This is how the 09:30
and 17:30 New York jobs (14:30 and 22:30 London) skip exchange holidays.

#### `tickers.py` -- the ticker rule

`TICKER_RE = ^[A-Z0-9.\-]{1,10}$` and `normalize_ticker(value)` (strip,
upper-case, validate; `ValueError` otherwise, which pydantic turns into a
422). One rule instead of five copies; inputs are bounded before they
reach a market data provider or the database. Two pydantic field types
for request schemas: `Ticker` (`Annotated[str, BeforeValidator(...)]`: a
string goes through `normalize_ticker`, anything else is left to
pydantic's own type check) and `OptionalTicker` (`None` or a blank string
becomes `None`). Used by core-service (`domains/portfolio/schema.py`) and
ai-service (analysis, backtest, RAG, market and analytics schemas and
routes).

#### `user_state.py` -- live role and active flag

`UserState(role, is_active)`, key `auth:user_state:<user_id>` in the events
database, `store_user_state` (auth-service only, no expiry) and
`get_user_state` (Redis, then `GET /internal/users/{id}/state` with a
2-second timeout, cached for `USER_STATE_CACHE_TTL_SECONDS` with
`nx=True` so the write-back never overwrites a key auth-service wrote
meanwhile; a 404 from auth means inactive; `None` when both fail). Used by `security.py` and by core-service's WebSocket helpers,
public API middleware and internal routes, and ai-service's internal
routes; written by `services/auth-service/user_state_sync.py`. Chapter 29,
section 3.8.

#### `org_membership_cache.py` -- organization roles of a user

`organization_roles(settings, client, auth_service_url, user_id)` returns
`{organization_id: role}` from
`GET /internal/users/{id}/organizations`, cached under
`auth_client:org_roles:<user_id>` in the cache database for
`MEMBERSHIP_CACHE_TTL_SECONDS` (60). A Redis error counts as a miss; an
auth-service error raises `UpstreamServiceError`, so callers fail closed.
A membership with no role is treated as `viewer`
(`FALLBACK_ORGANIZATION_ROLE`: fail closed, read access and no writes;
logged `org_membership_cache.role_missing`); roles are lower-cased.
`invalidate_membership_cache` deletes the entries (best effort: a Redis
failure is logged and the TTL bounds the staleness); auth-service calls
it before the commit of a membership change (so the response never comes
before it) and again after the commit (so an entry cached from the old
data during the commit is dropped too). A removed, departed or demoted
member therefore loses access at once, and a new member gets it at once,
instead of after the TTL. Used by core-service and ai-service `clients/auth_client.py`
(read) and auth-service `organization/service.py` and `events_out.py`
(invalidate).

#### `concurrency.py` -- bounded `gather`

`gather_bounded(coros, limit=10)`: like `asyncio.gather`, with at most
`limit` coroutines running at once (a semaphore). Results keep the input
order. Plain `asyncio.gather` starts everything at once, which can
overwhelm the downstream service or exceed the HTTP client's connection
pool. Used by core-service's
portfolio service (building list responses, `limit=20`) and research
service, so a page of results does not open dozens of simultaneous calls
to other services.

#### `e2e_lock.py` -- one end-to-end run at a time

`acquire(settings, holder, *, break_lock=False)` sets `e2e:run_lock` (cache
database) with `NX` and a 45-minute expiry (`LOCK_TTL_SECONDS`); it
returns the current holder when the lock is taken. `release` deletes the
key only if it still holds this run's value (a small Lua script). Used by
`services/core-service/scripts/e2e_check.py` and
`services/ai-service/scripts/e2e_tier_check.py`. Two runs at the same time
disturb each other: `e2e_check` compares site-wide counters before and
after its own changes (with two overlapping runs, the "user count
increased" check fails), and `e2e_tier_check` creates and deletes a
portfolio. The 45-minute expiry means a killed run does not block the
next one for long; `--break-lock` removes the lock at once.

#### `alembic_bootstrap.py` and `alembic_convert.py` -- schema at startup

`bootstrap_schema(engine, *, schema, alembic_ini, register_models)` runs at
every service start (from each `db_bootstrap.py`): it creates the schema
if needed, refuses a schema that has tables but no Alembic revision, or a
revision from the old archived migration chain, and otherwise runs
`alembic upgrade head` in a worker thread. `build_alembic_config` builds
the Alembic config in memory so Alembic does not reset the service's
logging. `schema_differences` compares the database with the models,
skipping the `alembic_version` table with `exclude_alembic_version`;
each service's migration `env.py` can pass the same predicate as its
`include_object`.

Each service keeps its own `alembic_version` table inside its own schema,
so the three services can bootstrap at the same time. Alembic's `env.py`
files are synchronous (psycopg), so the Alembic commands run in a worker
thread (`asyncio.to_thread`). The baseline migration of each service
creates every table; there is no `create_all` path.

`alembic_convert.py` holds `CONVERSIONS` (one per schema) and `convert`,
the one-time conversion of a database from the old migration chain to the
current baselines. All four steps (align definitions, rewrite the
`alembic_version` row, run later migrations, compare with the models) run
in one transaction; without `--apply` it is rolled back, so the preview
shows exactly what would happen. It is kept only for restoring an old
backup. Run per service as
`python -m scripts.alembic_convert [--apply]`. Chapter 20 has the
details.

#### `metrics.py` -- Prometheus collectors

`get_or_create(cls, name, doc, labels=())` returns the `Counter`, `Gauge`
or `Histogram` already registered under `name` in the default
`prometheus_client` registry, or creates it. A module imported twice
(tests, reloads) then reuses its collector instead of failing with
"Duplicated timeseries". `prometheus_client` registers a counter under
both `<name>` and `<name>_total`, and either key finds it. Used by
core-service's `job_health.py` and `outbox.py`, and ai-service's
`observability.py` (chapter 27).

#### `scheduled_jobs.py` -- the answer of a scheduler-triggered route

`job_response(job, result, *, steps)` answers 200 with `result`, or 500
(`ALL_UNITS_FAILED_STATUS`) with `result` plus `"all_units_failed":
[step names]` when a step attempted at least one unit and every unit
failed (`all_units_failed(units, failed)`; logged as the error
`scheduled_job_all_units_failed`). A step with zero units is a success.
`steps` maps a step name to `(units attempted, units failed)`. The
scheduler's scripts send a heartbeat only on a 2xx answer, so a run in
which everything failed is not counted as a success. Used by
core-service's market-open and notification-retention routes,
ai-service's tier-monitoring, review-run and rag-ingest routes, and
auth-service's purge-unverified route ([26-scheduler-jobs.md](26-scheduler-jobs.md) section
3.4).

#### `enums.py` -- shared vocabularies

All are `StrEnum`, so a value is also a plain string in JSON and the
database. One module holds every shared enum, grouped by topic:

| Topic | Enums |
|---|---|
| analysis | `AnalysisType`, `AnalysisFailureReason` |
| connection | `ConnectionStatus` (`pending`, `accepted`, `blocked`; friend requests) |
| document | `DocumentCategory`, `DocumentStatus` |
| market | `AssetType` (`equity`, `etf`, `crypto`: the types the market data provider reports), `TransactionType`, `Currency` |
| notification | `NotificationType`, `NotificationChannel` (`in_app`, `email`, `websocket`) |
| oauth | `OAuthProvider` (`google`, `github`) |
| recommendation | `RecommendationAction`, `TierSide`, `TierStatus` |
| role | `SystemRole` (admin, moderator, user, guest), `OrganizationRole` (owner, admin, member, viewer) |
| workflow | `WorkflowStatus` (`pending`, `running`, `completed`, `failed`), `AgentStatus` (the result of one agent run: `completed` or `failed`; the metrics rollup ignores any other stored status), `AgentRole` (the nine data agents plus `bull` and `bear`) |

Import the names you need (`from shared.enums import SystemRole`).

What the values mean, where the code does not say it by name:

- `AnalysisFailureReason`: every value `ai.analyses.failure_reason` can
  take, and the one list the Prometheus `failure_reason` label is built
  from. Run endings: `timeout`, `cancelled` (also used when a queued
  analysis is skipped because its owner was deactivated), `service_restart`,
  `worker_error`, `workflow_error`. "No verdict" endings:
  `judge_unavailable`, `no_evidence`,
  `debate_unavailable`, `insufficient_evidence`.
- `DocumentCategory`: retrieval and chunking key off the category, not
  the file format (a PDF and a text transcript of an earnings call chunk
  the same way; a 10-K and an analyst report do not). `risk_analysis`
  holds the Risk agent's archived reports (`ai/knowledge/archival.py`);
  Macro, News and Fundamental archive into `macro_research`, `news` and
  `analyst_report`. A future catch-all category would be a new value,
  not a reuse of `risk_analysis`.
- `DocumentStatus` (`uploaded`, `parsing`, `chunking`, `embedding`,
  `ready`, `failed`): published by ai-service, stored and filtered by
  core-service; core's forward-only rank covers every member (a test
  fails when a status is added without a rank).
- `TransactionType`: `stock_split` covers forward splits, reverse splits
  and stock dividends with one ratio (`split_ratio` 2.0 for 2-for-1, 0.2
  for 1-for-5, 1.05 for a 5% stock dividend), because all three rescale
  share count and per-share cost with no cash moving. `adjustment` moves
  shares in (positive quantity) or out (negative) at a stated cost with
  no cash (a transfer from another broker, a correction, an opening
  position); backtests treat it as an external flow, never as return.
- `TierSide` is separate from `RecommendationAction` because a tier exists
  only for BUY or SELL (a HOLD has no tiers). `TierStatus`: acceptance is
  for all tiers of a recommendation at once, and the window starts at
  generation and is not reset by acceptance; `declined` is the owner's
  explicit, immediate "no" (different from leaving a tier to expire);
  `execution_failed` means core refused the trade for a business reason
  and is terminal, so the tier is not retried every day. There is no
  `cancelled` status: cancelled tiers become `expired` with a note.
- `NotificationType` is stored as a plain string
  (`core.notifications.type`), so a new value needs no migration. But the
  read schema validates against the enum, so a stored row whose type is
  no longer a member would fail to load.

#### `schemas.py` -- shared pydantic models

- Base models: `BaseSchema` (`from_attributes=True`, so it can be built from
  an ORM row; `populate_by_name=True`), `IDMixin`, `TimestampMixin`,
  `PaginationParams` (`page` 1..`MAX_PAGE`=100000, `page_size`
  1..`MAX_PAGE_SIZE`=100, default 20), `PaginatedResponse[T]`
  (`items, total, page, page_size`) and `APIResponse[T]`
  (`data, request_id`). `MAX_PAGE` exists so `(page - 1) * page_size`
  always fits a PostgreSQL bigint `OFFSET`; a larger page is a 422, not a
  500. Every `page` query parameter should use `le=MAX_PAGE` too.
  How the envelopes are used: single objects are wrapped as
  `{"data": ..., "request_id": "<id>"}` (`APIResponse`; the field defaults
  to `request_id.current_request_id()`, so it equals the `X-Request-ID`
  header and the error body's id; a route never sets it). Paginated
  lists are returned unwrapped as `{"items", "total", "page",
  "page_size"}` (`PaginatedResponse`), with one exception: auth-service's
  `GET /api/v1/admin/users` wraps the page in `data`. `/internal/*` routes
  return plain JSON. Errors use `{"error": {...}}` or FastAPI's
  `{"detail": ...}` (see `exceptions.py` above).
- `SYSTEM_DOCUMENT_OWNER_ID` (`00000000-0000-0000-0000-0000000005ec`):
  the owner id of system documents (the ones the ingestion jobs add). One
  definition, used by core-service (document read rule, system document
  routes, `is_system`) and ai-service (an upload event with this owner
  becomes a system chunk in the RAG index).
- Refs: small cross-service reference objects: `UserRef` and
  `OrganizationRef` (produced by auth-service, consumed by core-service
  and ai-service), `AnalysisRef` and `RecommendationRef` (produced by
  ai-service, consumed by core-service's notification and portfolio
  code).

### 3.3 The rule for what belongs in `shared/`

The code shows a consistent rule. A module belongs in `shared/` when
**both** are true:

1. **More than one service needs the same behaviour**, and it must stay
   identical. Several modules exist precisely because copies drifted:
   five ticker regexes (`tickers.py`), seven "today in New York" helpers
   (`market_calendar.py`), two rate-limit counters with different failure
   behaviour (`rate_limit.py`), two membership caches with different TTLs
   (`org_membership_cache.py`).
2. **It depends on nothing service-specific.** It takes a
   `BaseServiceSettings` (or plain values) as an argument and never
   imports a service's `config`, `database` or `domains`. `email.py` takes
   finished links, not a service's settings shape; `org_membership_cache`
   takes the caller's HTTP client and auth URL.

What does **not** belong: domain logic of one service (portfolio ledger,
agents, RAG), anything that only one service uses (even if it is
generic), and anything that would make `shared/` import FastAPI or heavy
libraries where they are not needed (`INTERNAL_TOKEN_HEADER` is defined
once, in `http_client.py`, and `security.py` imports it from there; the
dependency goes that way so `http_client.py` never imports
`security.py`, which pulls in FastAPI and jose).

Two practical consequences: a change to `shared/` must be type-checked
and tested for **all three** services (`make mypy`, `make test`), and
deployed to all three.

---

## 4. Configuration

`shared/` itself has only the settings of `BaseServiceSettings`
(section 3.2, `config.py`). Constants that live in the modules:

| Constant | Value | File |
|---|---|---|
| Redis pool size / socket timeouts | 20 / 15 s / 15 s | `shared/redis.py` |
| HTTP client limits | 100 connections, 20 keep-alive, default timeout 5 s | `shared/http_client.py` |
| Durable stream length, consumer group, max deliveries, reclaim idle | 10000, `workers`, 5, 60 s | `shared/events.py` |
| Reconnect backoff (pub/sub listeners and the durable subscriber) | 1, 2, 5, 10, 30 s | `shared/events.py` |
| User-state lookup timeout | 2 s | `shared/user_state.py` |
| Membership cache TTL | 60 s | `shared/org_membership_cache.py` |
| E2E lock key / TTL | `e2e:run_lock` / 45 min | `shared/e2e_lock.py` |
| Page limits | page <= 100000, page_size <= 100, default 20 | `shared/schemas.py` |
| JWT secret minimum length | 32 characters | `shared/config.py` |
| Answer status when every unit of a scheduled job failed | 500 | `shared/scheduled_jobs.py` |

---

## 5. Design decisions and why

**One shared package instead of a published library.** The alternative
is to version `shared` as a pip package and pin it per service. In a
single repository with three services that are always deployed together,
copying the directory into each image is simpler, and there is never a
"which version of shared does core run" question as long as all three
images are rebuilt.

**Copy at build time, bind-mount in development.** Production images are
self-contained (no host directory needed). Development mounts the source
so a restart picks up a change without a rebuild.

**Pass settings in, never import them.** Every shared function takes
`settings` (or values) as a parameter. This keeps `shared/` free of any
service's configuration and makes the modules easy to test with a fake
settings object.

**Consolidate when copies drift.** Copies of the same rule do drift in
practice: two rate limiters that disagree on Redis errors (one allows,
one answers 500), or two membership caches with different lifetimes and
error handling, give users different behaviour depending on which service
they call. One shared module removes that class of bug.

**Shared rules, local policy.** The permission map is shared, but each
service decides which permission a route needs. The rate counter is
shared, but each service picks its keys and limits.

---

## 6. When things go wrong

| Symptom | Cause | What to do |
|---|---|---|
| One service behaves with an old rule after a shared change | Only some images were rebuilt (production) or restarted (development). | Rebuild or restart all three services. |
| `ModuleNotFoundError: No module named 'shared'` in a script | Run outside the container without the repo root on the path. | Run inside the container (`docker compose --env-file .env.docker exec -T <svc> python -m ...`) or set `PYTHONPATH` to the repo root. |
| mypy reports `Duplicate module named ...` | `--explicit-package-bases` missing, or mypy run from the wrong directory. | Use the Makefile targets (chapter 31). |
| Redis data "missing" | Looking in the wrong database number. | Use `-n 0..3` with `bash scripts/redis-cli.sh` (table in 3.2). |
| `e2e_check` exits with code 2 and names another run | `e2e:run_lock` is held. | Wait, or `--break-lock` if that run is really gone. |
| A service refuses to start with `SchemaBootstrapError` | The schema has tables but no revision, or an archived revision. | Follow the message (chapter 20). |

---

## 7. Demo walkthrough

All read-only.

1. **The same code in every container.**

   ```bash
   for s in auth-service core-service ai-service; do
     docker compose --env-file .env.docker exec -T $s sh -c 'echo "$HOSTNAME"; ls /app/shared | head -5; echo PYTHONPATH=$PYTHONPATH'
   done
   ```

   Say: "Each service has `/app/shared`; in development it is the same
   host folder mounted three times."

2. **One ticker rule everywhere.**

   ```bash
   docker compose --env-file .env.docker exec -T ai-service python -c "
   from shared.tickers import normalize_ticker
   print(normalize_ticker(' brk.b '))
   try:
       normalize_ticker('NOT A TICKER')
   except ValueError as e:
       print('refused:', e)"
   ```

   Output: `BRK.B`, then the refusal message.

3. **The market calendar.**

   ```bash
   docker compose --env-file .env.docker exec -T core-service python -c "
   from datetime import date
   from shared.market_calendar import market_today, nyse_holidays, is_trading_day
   print('today in New York:', market_today())
   print(sorted(nyse_holidays(2026)))
   print('2026-11-26 trading day?', is_trading_day(date(2026, 11, 26)))"
   ```

   Thanksgiving 2026 (26 November) is not a trading day.

4. **The permission map.**

   ```bash
   docker compose --env-file .env.docker exec -T auth-service python -c "
   from shared.permissions import sorted_permissions
   for role in ('guest', 'user', 'moderator', 'admin'):
       print(role, sorted_permissions(role))"
   ```

5. **Redis databases.** `bash scripts/redis-cli.sh -n 2 --scan --pattern 'auth:user_state:*' | head -3`
   (live state, events database) and
   `bash scripts/redis-cli.sh -n 3 --scan --pattern 'stream:*'` (durable
   streams, queue database).

6. **Type-check the shared package** (on the host, needs `uv sync`):
   `make mypy-shared`. Expected: `Success: no issues found in <N> source
   files` (26 modules today; the number changes when modules are added
   or removed).

---

## 8. Check your understanding

<details>
<summary>1. How does code in <code>/app/main.py</code> import <code>shared.security</code> in a production container?</summary>

The Dockerfile copies `shared/` to `/app/shared` and sets
`PYTHONPATH=/app`, so `shared` is a top-level package (a namespace
package; it has no `__init__.py`).
</details>

<details>
<summary>2. You change <code>shared/tickers.py</code>. What must you run and deploy?</summary>

Type checks and tests for all three services (`make mypy`, `make test`),
then rebuild all three images (or restart all three in development).
</details>

<details>
<summary>3. Why does <code>shared/redis.py</code> strip the database number from <code>REDIS_URL</code>?</summary>

A `/0` in the URL overrode the requested database, so every client used
database 0 and the durable stream ended up in the wrong place.
</details>

<details>
<summary>4. Which module would you change to give moderators a new right?</summary>

`shared/permissions.py` (`ROLE_PERMISSIONS`), then guard the routes with
`require_permission`.
</details>

<details>
<summary>5. Why is the RAG assistant's limit not in <code>shared/rate_limit.py</code>?</summary>

It is used by one service only and has a different shape (per clock
minute plus per UTC day). Shared code is for rules several services need.
</details>

<details>
<summary>6. What does <code>gather_bounded</code> protect against?</summary>

Starting too many concurrent calls at once (to other services or the
connection pool) when a list is resolved in parallel.
</details>

<details>
<summary>7. What two conditions make code belong in <code>shared/</code>?</summary>

Several services need it identical, and it depends on nothing
service-specific (settings come in as parameters).
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `shared/config.py` | `BaseServiceSettings`, `get_settings`, secret checks |
| `shared/database.py` | `Base`, `UUIDPKMixin`, `TimestampColumnsMixin`, engine and session factories, `make_get_db`, `count_query` |
| `shared/logging.py` | `configure_logging` |
| `shared/redis.py` | Four Redis clients and pools |
| `shared/events.py` | Pub/sub and Redis Streams publishers and subscribers |
| `shared/events_registry.py` | `EventName`, `EVENT_OWNERS` |
| `shared/http_client.py` | Pooled httpx clients, internal-token hook |
| `shared/exceptions.py` | `AppError` hierarchy, error handlers |
| `shared/request_id.py` | `RequestIdMiddleware`, `current_request_id`: request id in logs, error and success bodies and `X-Request-ID` header |
| `shared/security.py` | Current user, permission dependencies, internal token |
| `shared/permissions.py` | Role -> permission map |
| `shared/rate_limit.py` | Fixed-window counter, 429 response |
| `shared/storage.py` | S3 client functions |
| `shared/email.py` | SMTP sender and templates |
| `shared/market_calendar.py` | `MARKET_TZ`, `market_today`, NYSE holidays |
| `shared/tickers.py` | `TICKER_RE`, `normalize_ticker` |
| `shared/user_state.py` | Live role/active state |
| `shared/org_membership_cache.py` | Organization roles cache and invalidation |
| `shared/concurrency.py` | `gather_bounded` |
| `shared/e2e_lock.py` | End-to-end run lock |
| `shared/alembic_bootstrap.py`, `shared/alembic_convert.py` | Schema bootstrap and one-time conversion |
| `shared/metrics.py` | `get_or_create` for Prometheus collectors |
| `shared/scheduled_jobs.py` | `job_response`, `all_units_failed` |
| `shared/enums.py` | Shared `StrEnum` vocabularies |
| `shared/schemas.py` | Response envelopes, cross-service refs |

---

## 10. Related chapters

- [01-architecture.md](01-architecture.md): where the services sit.
- [20-postgresql.md](20-postgresql.md): schemas and Alembic bootstrap.
- [22-object-storage.md](22-object-storage.md): `storage.py` in use.
- [23-redis.md](23-redis.md): the four Redis databases.
- [24-events.md](24-events.md): `events.py` and the event registry.
- [28-email.md](28-email.md): `email.py`.
- [29-security.md](29-security.md): `security.py`, `permissions.py`,
  `user_state.py`, `rate_limit.py`.
- [31-testing-quality.md](31-testing-quality.md): how `shared/` is tested
  and type-checked.
- [../PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md).
