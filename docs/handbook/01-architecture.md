# 01. System architecture

> AlphaMind is a set of Docker containers started by one Compose file. Three
> Python services do the real work: `auth-service` (accounts and tokens),
> `core-service` (portfolios, research, notifications, WebSockets) and
> `ai-service` (the analysis agents, RAG, recommendations). An nginx gateway
> is the only door from the outside. Behind it sit the data stores
> (PostgreSQL, Redis, Qdrant, S3 object storage), a cron scheduler, and a
> monitoring stack (Prometheus, Alertmanager, Grafana, exporters, a log
> collector). This chapter shows every container, who owns which data, how
> the parts talk to each other, and how one request and one analysis travel
> through the system. Later chapters go deep into each part.

## 1. The big picture

Think of AlphaMind as a small company building:

- The **gateway** is the reception desk. Every visitor comes through it.
  It checks nothing about who you are; it only decides which office you go
  to (by URL path) and makes sure the connection is encrypted (TLS).
- The three **services** are the offices. Each office has its own filing
  cabinet (its own PostgreSQL schema) and never opens another office's
  cabinet. When it needs something from another office, it phones that
  office on an internal line (HTTP `/internal/*` routes, protected by a
  shared password called the internal token).
- **Redis** is the notice board. An office pins a message ("analysis
  finished", "document uploaded") and whoever cares reads it.
- **Qdrant**, **object storage** and **MailHog** are special rooms:
  vectors for search, uploaded files, and a fake mailbox for development.
- The **scheduler** is the alarm clock. At fixed New York times it calls
  internal routes (market open, tier monitoring, market close, nightly
  review, RAG ingestion) and makes the nightly backup.
- **Prometheus, Alertmanager, Grafana, the exporters and vector** are the
  security cameras and log books.

All containers run on one Docker network created by Compose. Inside that
network every container can reach every other one by its service name
(for example `http://core-service:8000`). Only the gateway is published to
all interfaces of the host; a few developer tools are published on
`127.0.0.1` only.

```
                        Browser / curl / scripts on the host
                                       |
              https://localhost:8443   |   http://localhost:8000 (redirect only)
                                       v
 +-------------------------------- gateway (nginx 1.27) ------------------------------+
 | TLS, path routing, WebSocket upgrade, /status page, /grafana/, 404 for /internal/* |
 +------------------+-----------------------+----------------------+------------------+
                    |                       |                      |            |
      /api/v1/auth  |    /api/v1/portfolios |   /api/v1/analyses   |   /grafana/|
      /organizations|    /research /ws ...  |   /rag /market ...   |            v
                    v                       v                      v         grafana
             +--------------+  internal  +--------------+ internal +--------------+
             | auth-service |<---------->| core-service |<-------->|  ai-service  |
             |  schema auth |  HTTP +    |  schema core |  HTTP +  |  schema ai   |
             +------+-------+  X-Internal+------+-------+  token   +------+-------+
                    |          -Token           |                         |
     +--------------+---------------+-----------+-------------+-----------+------+
     |              |               |                         |                  |
     v              v               v                         v                  v
 +----------+  +---------+  +------------------+       +-------------+    +-----------+
 | postgres |  |  redis  |  | objectstore      |       |   qdrant    |    | mailhog   |
 | 16       |  | 7 (AOF) |  | SeaweedFS, S3    |       | vectors     |    | SMTP 1025 |
 | auth,core|  | cache,  |  | alias "minio"    |       | (ai only)   |    | UI 8025   |
 | ai       |  | events  |  | (core, ai)       |       |             |    |           |
 +----------+  +---------+  +------------------+       +-------------+    +-----------+

 scheduler (cron, America/New_York) --HTTP + token--> core-service, ai-service
           \--> pg_dump, Qdrant snapshots, S3 object copy, avatars tar --> ./backups/

 prometheus --scrapes /metrics--> auth, core, ai, postgres-exporter, redis-exporter,
                                   alertmanager, itself
           --alerts--> alertmanager --email--> mailhog
 grafana --queries--> prometheus
 vector --reads Docker logs--> ./logs/<service>/<YYYY-MM-DD>.log

 Off by default (Compose profiles): restore ("tools"), adminer ("adminer")
```

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| Container | A running, isolated process with its own file system, made from an image. | Every part of AlphaMind is one container. |
| Image | A frozen file system plus a start command. Either pulled (e.g. `postgres:16-alpine`) or built from a `Dockerfile`. | The three services and the scheduler are built from this repository; everything else is pulled. |
| Docker Compose | A tool that starts many containers from one YAML file (`docker-compose.yaml`). | `docker compose --env-file .env.docker up` starts the whole system. |
| Service (Compose) | One entry under `services:` in the Compose file. Its name is also its host name on the internal network. | `core-service` can call `http://ai-service:8000`. |
| Volume | Storage that survives when a container is deleted. | Database files, vectors, uploads, Grafana data live in named volumes (`postgres_data`, `qdrant_data`, ...). |
| Bind mount | A host folder or file mounted into a container. | In development the Python source is bind-mounted, so the container runs the code you edit. Config files (nginx, Prometheus) are bind-mounted read-only. |
| Profile | A Compose label that keeps a service out of a normal `up`. | `restore` (profile `tools`) and `adminer` (profile `adminer`) only start when asked. |
| Health check | A command Compose runs inside a container to decide "healthy" or "unhealthy". | Other containers wait for `service_healthy` before they start. |
| Reverse proxy / gateway | A server that receives every request and forwards it to the right backend. | nginx in `infra/gateway/nginx.conf`. |
| TLS | The encryption behind `https://`. | The gateway holds a self-signed certificate made by `infra/gateway/gen-certs.sh`. |
| Schema (PostgreSQL) | A named group of tables inside one database. | One database `alphamind` with three schemas: `auth`, `core`, `ai`, one per service. |
| JWT | JSON Web Token: a signed string that says "this is user X with role Y until time T". | auth-service issues it; every service checks the signature itself with the shared `JWT_SECRET`. |
| Internal token | A shared secret sent in the `X-Internal-Token` header. | Every `/internal/*` route requires it (`shared/security.py::require_internal_token`). |
| Pub/sub | Redis "publish/subscribe": a message goes to everyone listening right now, and is lost if nobody listens. | Used for live updates and notifications. |
| Stream / consumer group | A Redis log of messages that are kept until a consumer acknowledges them. A consumer group shares the work among its members. | Used for the few events that must never be lost (chapter 24). |
| Alembic | A tool that applies numbered database changes ("migrations"). | Each service runs `alembic upgrade head` on its own schema at startup. |
| ASGI / uvicorn | ASGI is the async Python web server interface; uvicorn is the server program that runs a FastAPI app. | Each service container runs `uvicorn main:app`. |

## 3. How it works in AlphaMind

### 3.1 Every container

The table lists all 18 Compose services (16 start with a normal `up`; two
are behind profiles). Source: `docker-compose.yaml` and
`docker-compose.override.yml`.

| Service | Image or build | Job | Internal port | Host port | Starts after | Health check | Volumes |
|---|---|---|---|---|---|---|---|
| `gateway` | `nginx:1.27-alpine` | TLS, routing, `/status`, `/grafana/` | 443, 80 | `8443:443`, `8000:80` (all interfaces) | auth, core, ai **healthy** | none | `nginx.conf`, `certs/`, `status.html` (all read-only) |
| `auth-service` | built, `services/auth-service/Dockerfile` | users, login, tokens, 2FA, OAuth, organizations, friends, admin | 8000 | none | postgres, redis healthy | `GET /health` = 200 | `avatar_data` at `/app/data/avatars` |
| `core-service` | built, `services/core-service/Dockerfile` | portfolios, watchlists, research notes and documents, notifications, WebSockets, public API, market-open/close jobs | 8000 | none | postgres, redis, objectstore healthy | `GET /health` = 200 | - |
| `ai-service` | built, `services/ai-service/Dockerfile` | analysis graph, LLM router, RAG, memory, tiers, backtests, analytics | 8000 | none | postgres, redis, qdrant, objectstore healthy | `GET /health` = 200 (start period 90 s) | - |
| `objectstore` | `chrislusf/seaweedfs:4.47`, network alias `minio` | S3-compatible file storage for documents | 9000 (S3), 19000 (gRPC) | none | - | signed S3 ListBuckets (`infra/objectstore/healthcheck.sh`) | `objectstore_data` |
| `postgres` | `postgres:16-alpine` | the one database, three schemas | 5432 | none | - | `pg_isready -h 127.0.0.1` | `postgres_data`, `init-schemas.sql` |
| `redis` | `redis:7-alpine`, `--appendonly yes --appendfsync everysec` | cache, rate limits, events, user state | 6379 | none | - | `redis-cli ping` | `redis_data` |
| `qdrant` | `qdrant/qdrant:v1.18.3` | vector database for RAG | 6333 | `127.0.0.1:6333` | - | `GET /readyz` = 200 (via bash `/dev/tcp`) | `qdrant_data` |
| `mailhog` | `mailhog/mailhog:v1.0.1` | catches every email in development | 1025 (SMTP), 8025 (UI) | `127.0.0.1:8025` | - | none | - |
| `prometheus` | `prom/prometheus:v3.13.2` | collects metrics, evaluates alert rules | 9090 | `127.0.0.1:9090` | alertmanager | none | `prometheus.yml`, `alert_rules.yml`, `prometheus_data` |
| `alertmanager` | `prom/alertmanager:v0.28.1` | sends alert emails | 9093 | `127.0.0.1:9093` | mailhog | none | `alertmanager.yml`, `alertmanager_data` |
| `postgres-exporter` | `prometheuscommunity/postgres-exporter:v0.17.1` | PostgreSQL metrics | 9187 | none | postgres healthy | none | - |
| `redis-exporter` | `oliver006/redis_exporter:v1.74.0` | Redis metrics | 9121 | none | redis healthy | none | - |
| `grafana` | `grafana/grafana:13.1.1` | dashboards, served at `/grafana/` | 3000 | none (through the gateway) | prometheus | none | `grafana_data`, provisioning |
| `scheduler` | built, `infra/scheduler/Dockerfile` (Alpine 3.20 + dcron) | cron jobs and nightly backup | - | none | postgres, qdrant, objectstore healthy | none | `./logs`, `./backups`, `avatar_data` (ro); objects are copied through the S3 API |
| `vector` | `timberio/vector:0.46.1-alpine` | copies every compose container's output to `./logs/` | - | none | - | none | Docker socket (ro), `./logs` |
| `restore` (profile `tools`) | same image as scheduler | one-off restore runner | - | none | postgres healthy, qdrant started | none | `./backups`, `avatar_data` **read-write**, `objectstore_data` read-write (only for older tarball backups); objects are restored through the S3 API |
| `adminer` (profile `adminer`) | `adminer:4.8.1` | PostgreSQL web UI | 8080 | `127.0.0.1:8080` | postgres healthy | none | - |

Every container uses the same log settings (`x-logging` anchor at the top
of `docker-compose.yaml`): Docker's `json-file` driver with `max-size: 20m`
and `max-file: 5`, so Docker keeps at most 5 x 20 MB of log per container.
The long-term copy of the logs is vector's job
([chapter 27](27-observability.md)).

A commented-out `frontend` service sits at the top of the Compose file.
It is not built yet (section 3.11). When it exists it is served through
the gateway, with no host port of its own.

Pulled images are pinned to exact tags. Two pins carry a constraint:
`qdrant/qdrant:v1.18.3` matches the minor version of `qdrant-client` in
`uv.lock`, and `chrislusf/seaweedfs:4.47` must not be switched to the
`_large_disk` image variant on the same volume, because that variant uses
a different on-disk format. The scheduler image's `postgresql16-client`
must match the `postgres:16-alpine` server: `pg_dump` refuses to dump a
server newer than itself, so bump both together.

### 3.2 Ports: what is reachable from where

There are three levels of reachability:

1. **Public (all host interfaces):** only the gateway.
   - `https://localhost:8443` (container port 443): the real entry point.
   - `http://localhost:8000` (container port 80): every request gets a
     `301` redirect to `https://<host>:8443<same path>`
     (`infra/gateway/nginx.conf`, first `server` block). The number 8443
     is written twice: in the Compose port mapping and in the nginx `map
     $host $external_https_port`. Change both together. The whole public
     address (scheme, host, port) is also in `OAUTH_REDIRECT_BASE_URL`
     (`.env.docker`: the OAuth callbacks, and Grafana's
     `GF_SERVER_ROOT_URL`, which Compose builds from it), in `GATEWAY_URL`
     of the operator scripts (`scripts/lib.sh`, default
     `https://localhost:8443`), in the `--base-url` default of
     `services/ai-service/scripts/upload_seed_documents.py` and in the
     checks of `.github/workflows/integration-ci.yml` (CI only). For a
     deployment under another host or port, change all of them.
2. **Local only (`127.0.0.1`):** developer UIs, unreachable from other
   machines on your network.
   - Qdrant REST API and dashboard: `http://127.0.0.1:6333/dashboard`
   - MailHog web UI: `http://127.0.0.1:8025`
   - Prometheus: `http://127.0.0.1:9090`
   - Alertmanager: `http://127.0.0.1:9093`
   - Adminer (only when started): `http://127.0.0.1:8080`
3. **Internal only (Docker network):** PostgreSQL 5432, Redis 6379, the
   three services on 8000, SeaweedFS 9000, Grafana 3000, the exporters.
   You reach them with `docker compose exec` (for example
   `bash scripts/psql.sh`) or through the gateway (Grafana).

Inside the gateway, any path that is not listed is a `404`
(`location / { return 404; }`). `/internal/` and `/metrics` have their own
explicit `404` blocks, so service-to-service routes and Prometheus
metrics can never be called from outside.

The public paths, grouped by backend (`infra/gateway/nginx.conf`):

| Backend | Paths |
|---|---|
| auth-service | `/api/v1/auth` (rate limited: 10 req/s per client address, burst 20, then `429`), `/api/v1/organizations`, `/api/v1/connections`, `/api/v1/admin`, `/static/avatars` |
| core-service | `/api/v1/portfolios`, `/api/v1/watchlists`, `/api/v1/api-keys`, `/api/v1/research`, `/api/v1/notifications`, `/api/v1/public`, `/api/v1/moderation`, `/ws` (WebSocket, read timeout 3600 s) |
| ai-service | `/api/v1/analyses`, `/api/v1/market`, `/api/v1/analytics`, `/api/v1/decisions`, `/api/v1/backtests`, `/api/v1/rag` (no buffering, read timeout 180 s, for Server-Sent Events) |
| grafana | `/grafana/` |
| gateway itself | `/status` (static page that polls `/health/auth`, `/health/core`, `/health/ai` every 15 seconds) |
| Swagger | `/docs/auth/`, `/docs/core/`, `/docs/ai/` and their spec files `/api/openapi-auth.json`, `/api/openapi-core.json`, `/api/openapi-ai.json` |
| health | `/health/auth`, `/health/core`, `/health/ai` (each proxied to that service's `/health`) |

Details of the gateway (TLS settings, security headers, the access log
without query strings) are in [chapter 10](10-gateway.md).

### 3.3 Service boundaries and data ownership

Each service owns its data. "Owns" means: only that service writes it,
and other services ask the owner through its internal API.

| Store | Owner(s) | What lives there |
|---|---|---|
| PostgreSQL schema `auth` | auth-service | `users`, `user_profiles`, `user_settings`, `user_sessions`, `oauth_accounts`, `two_factor_secrets`, `connections`, `organizations`, `organization_members`, `organization_invitations` |
| PostgreSQL schema `core` | core-service | `portfolios`, `holdings`, `transactions`, `portfolio_snapshots`, `watchlists`, `watchlist_items`, `research_notes`, `research_comments`, `research_tags`, `note_tags`, `documents`, `notifications`, `api_keys`, `event_outbox` |
| PostgreSQL schema `ai` | ai-service | `analyses`, `agent_runs`, `agent_reports`, `debate_sessions`, `recommendations`, `recommendation_tiers`, `reflections`, `investment_decisions`, `document_chunks`, `filing_records`, `kg_entities`, `kg_relations`, `episodic_memories`, `price_bars`, `backtests`, and more (see [chapter 20](20-postgresql.md) and [../ERD.md](../ERD.md)) |
| Qdrant | ai-service only | the RAG vectors (collection `research_documents_gemini` by default, `services/ai-service/config.py::QDRANT_COLLECTION_RESEARCH`) |
| Object storage (SeaweedFS) | core-service writes uploads; ai-service reads them for ingestion and writes system documents through core | the `research-documents` bucket (`S3_BUCKET_DOCUMENTS`) |
| `avatar_data` volume | auth-service | uploaded profile pictures, served at `/static/avatars` |
| Redis | shared, with fixed logical databases | see below |

**One database, three schemas.** `infra/postgres/init-schemas.sql` runs
once, when the `postgres_data` volume is empty. It creates the extension
`pgcrypto` and the schemas `auth`, `core` and `ai`. Each service then puts
its own schema first on the PostgreSQL `search_path` for every connection
(`shared/database.py`, `"search_path": f"{settings.POSTGRES_SCHEMA},public"`;
`POSTGRES_SCHEMA` is `auth`, `core` or `ai` in each service's
`config.py`). So the code of core-service writes `SELECT ... FROM
portfolios` and gets `core.portfolios`.

**No foreign keys across schemas.** When `ai.analyses` points at a user or
a portfolio, it stores a plain UUID with no database constraint
(`services/ai-service/domains/analysis/model.py`: `user_id` and
`portfolio_id` are plain `UUID` columns without `ForeignKey`). The
reason: if the database enforced links across schemas, the services could
no longer be deployed, migrated or restored on their own. The price is
that the application must keep the links tidy itself, for example with
the `portfolio.deleted` event (section 3.5).

**Each service migrates its own schema.** At startup each service calls
`db_bootstrap.ensure_schema`, which calls
`shared/alembic_bootstrap.py::bootstrap_schema`. It creates the schema if
it is missing and runs `alembic upgrade head`. Each schema has its own
`alembic_version` table, so the three services can start at the same
time without stepping on each other. Chapter [20](20-postgresql.md)
explains the refusal cases.

**Redis logical databases** (`shared/config.py`, `shared/redis.py`):

| DB | Setting | Used for |
|---|---|---|
| 0 | `REDIS_DB_CACHE` | caches (API key validation, organization membership, OAuth state, the FX rate, a few internal lookups), the scheduled-job health times `core:job_last_success` and `core:job_tracked_since` (when core began tracking a job that has no success yet), the NewsAPI usage log |
| 1 | `REDIS_DB_RATE_LIMIT` | rate-limit counters (auth, public API, RAG assistant) |
| 2 | `REDIS_DB_EVENTS` | pub/sub events and the live user state `auth:user_state:<id>` |
| 3 | `REDIS_DB_QUEUE` | durable event streams `stream:<event>` |

`shared/redis.py` strips any database number from `REDIS_URL` on
purpose. A `REDIS_URL` ending in `/0` would otherwise silently move the
durable streams into database 0, and anyone checking database 3 would
think the document pipeline is broken. The full key list is in
[chapter 23](23-redis.md).

Note: the analysis queue is **not** in Redis. It lives in the memory of
the ai-service process (`services/ai-service/domains/analysis/queue.py`),
which is why restarting ai-service stops running analyses (section 3.7).

### 3.4 How services call each other (internal HTTP)

When one service needs data that another service owns, it makes an HTTP
call inside the Docker network to a route under `/internal/`.

```
core-service                                     ai-service
clients/ai_client.py                             api/internal.py
  get_http_client(timeout)  ---- POST http://ai-service:8000/internal/analyses ---->
  (shared/http_client.py)        X-Internal-Token: <INTERNAL_API_TOKEN>
                                                  router = APIRouter(prefix="/internal",
                                                    dependencies=[Depends(require_internal_token)])
```

The pieces:

- **`shared/http_client.py`** gives each service a pooled `httpx`
  client (`get_http_client(timeout)`; up to 100 connections, 20 kept
  alive). A request hook (`_add_internal_token`) adds the
  `X-Internal-Token` header, but only when the path starts with
  `/internal/` and the host and port match `AUTH_SERVICE_URL`,
  `CORE_SERVICE_URL` or `AI_SERVICE_URL`. The token is therefore never
  sent to an outside data provider that uses the same client.
- **`shared/security.py::require_internal_token`** guards every internal
  router. It compares the header with `INTERNAL_API_TOKEN` using
  `hmac.compare_digest` (a comparison whose time does not depend on where
  the strings differ, so an attacker cannot guess the token byte by byte
  from timing). Missing or wrong token: `401`. If the setting is empty,
  the check is skipped only when `APP_ENV` is `development` or `test`;
  in any other environment the service refuses to start without it
  (`shared/config.py`). The internal routers are registered with
  `include_in_schema=False`, so they do not appear in the Swagger pages
  or the OpenAPI files of any service.
- **`services/*/clients/`** hold one module per remote service:

| Caller | Client module | Internal routes it calls |
|---|---|---|
| core-service | `clients/auth_client.py` | `/internal/users/{id}`, `/internal/connections/check`, `/internal/users/{id}/notification-settings`, `/internal/users/{id}/presence` |
| core-service, ai-service | `clients/auth_client.py` -> `shared/org_membership_cache.py` | auth `/internal/users/{id}/organizations` (organization membership and role, cached 60 s in Redis) |
| all three services | `shared/user_state.py` | auth `/internal/users/{id}/state` (live role and active flag, only when the Redis key is missing) |
| core-service | `clients/ai_client.py` | `/internal/recommendations/latest`, `/internal/analyses/{id}`, `/internal/market/instruments/{ticker}/prices`, `/internal/fx-rate`, `/internal/analyses` (create), `/internal/sentiment` |
| ai-service | `clients/core_client.py` | `/internal/portfolios/{id}/holdings`, `/owner`, `/cash-balance`, `/snapshots`, `/cash-flows`, `POST /internal/portfolios/{id}/transactions` (tier execution), `/internal/tracked-tickers`, `/internal/documents/filenames`, `/internal/documents` |
| auth-service | `domains/admin/service.py::_fetch_service_stats` | `/internal/stats` of core and ai (admin statistics page; auth's own numbers are read from its database, it has no `/internal/stats` route) |
| scheduler | `infra/scheduler/*.sh` (curl) | core `/internal/scheduler/market-open`, `/market-close`, `/heartbeat/{job}`; ai `/internal/scheduler/tier-monitoring`, `/internal/scheduler/rag-ingest`, `/internal/review/run` |

Because the gateway never forwards `/internal/`, operator tools that need
an internal route run **inside** a container. For example
`scripts/run_analysis.sh` runs
`services/ai-service/scripts/analysis_cli.py` inside ai-service, which
calls `http://localhost:8000/internal/analyses` with the token from the
container's environment.

### 3.5 Events over Redis (summary)

Internal HTTP is for questions ("who owns this portfolio?"). Events are
for announcements ("analysis X finished"). The publisher does not wait
for, or even know, the listeners. Two transports exist
(`shared/events.py`); every event name and its owner is listed in
`shared/events_registry.py` (`EventName`, `EVENT_OWNERS`).

| Transport | Function | Redis | Guarantee | Used for |
|---|---|---|---|---|
| Pub/sub | `publish_cross_service` / `CrossServiceSubscriber` | channel `events:<name>` | at most once: lost if no listener is connected | notifications and live updates: `analysis.*`, `recommendation.ready`, `tier.*`, `decision.*`, `backtest.*`, organization, friend and user events, presence |
| Durable stream | `publish_durable_event` / `DurableEventSubscriber` | stream `stream:<name>` in DB 3, consumer group `workers` | at least once: kept until acknowledged, retried, capped at about 10,000 entries | `document.uploaded`, `document.deleted`, `portfolio.deleted` (core -> ai, written first to the outbox table `core.event_outbox` in the same transaction as the change, then published), and the database update of `document.status_changed` (ai -> core) |

Who listens:

- **core-service** (`event_listener.py`) subscribes to almost every
  pub/sub event. It turns them into notification rows and pushes them to
  browsers over WebSockets (`/ws/user:<id>`, `/ws/analysis:<id>`,
  `/ws/document:<id>`, `/ws/presence:<id>`). Only core-service holds
  WebSocket connections.
- **ai-service** (`event_listener.py`) consumes the durable streams
  `document.uploaded`, `document.deleted` (index or remove a document in
  RAG) and `portfolio.deleted` (cancel the portfolio's open tiers).

Why durable streams for those four: with pub/sub, an uploaded document
stays at status `uploaded` with zero chunks forever when its message is
published while ai-service's listener happens to be disconnected, and
nothing logs an error. A missed live update only costs a
page refresh; a missed "index this document" is a real bug. Redis runs
with an append-only file (`--appendonly yes --appendfsync everysec`) so
these streams also survive a Redis crash with at most about one second
of loss.

Full details: [chapter 24](24-events.md).

### 3.6 The path of one browser request

Example: the browser loads the user's portfolios with
`GET https://localhost:8443/api/v1/portfolios` and an
`Authorization: Bearer <access token>` header.

```
Browser            gateway (nginx)             core-service (uvicorn+FastAPI)       Redis        PostgreSQL
   |  TLS handshake     |                                  |                          |               |
   |------------------->|                                  |                          |               |
   | GET /api/v1/portfolios                                |                          |               |
   |------------------->| location /api/v1/portfolios      |                          |               |
   |                    | resolve "core-service" via Docker DNS 127.0.0.11 (cached 10 s)                  |
   |                    | add Host, X-Real-IP, X-Forwarded-For/-Proto/-Host                               |
   |                    |--------------------------------->|                          |               |
   |                    |    http://core-service:8000      | --proxy-headers: client  |               |
   |                    |                                  | address and scheme from  |               |
   |                    |                                  | X-Forwarded-*            |               |
   |                    |                                  | API-key middleware: not  |               |
   |                    |                                  | /api/v1/public -> skip   |               |
   |                    |                                  | decode JWT (JWT_SECRET)  |               |
   |                    |                                  | live role/active check ->|               |
   |                    |                                  |    GET auth:user_state:<id> (DB 2)       |
   |                    |                                  |<-------------------------|               |
   |                    |                                  | handler -> repository    |               |
   |                    |                                  |    SELECT ... FROM portfolios (search_path core)
   |                    |                                  |----------------------------------------->|
   |                    |                                  |<-----------------------------------------|
   |                    |                                  | commit, JSON {"data": ...}               |
   |                    |<---------------------------------|                          |               |
   |                    | add HSTS, X-Content-Type-Options,|                          |               |
   |                    | X-Frame-Options, Referrer-Policy |                          |               |
   |<-------------------|                                  |                          |               |
```

Step by step:

1. **TLS.** nginx terminates TLS with `infra/gateway/certs/server.crt`
   (TLS 1.2 and 1.3 only). Behind the gateway, traffic is plain HTTP on
   the Docker network.
2. **Routing.** nginx picks the `location` by path prefix. `proxy_pass`
   uses a variable (`$core_service`) together with `resolver 127.0.0.11
   valid=10s`, so nginx looks up the container's address again every 10
   seconds. A name resolved only once at nginx start goes stale: after a
   backend restart Docker can give the old address to a different
   container, and `/health/ai` then answers with another service's
   health.
3. **Forwarded headers.** nginx **replaces** `X-Forwarded-For` with the
   real peer address (it never appends). uvicorn runs with
   `--proxy-headers --forwarded-allow-ips=*`, so the service sees the
   browser's address. Replacing matters: if a client could add its own
   `X-Forwarded-For`, it could choose the address that per-IP rate limits
   count against.
4. **Authentication in the service.** The gateway does not look at the
   token. core-service decodes the JWT with the shared `JWT_SECRET`
   (`shared/security.py::decode_token`). Then it reads the user's live
   role and active flag from Redis (`shared/user_state.py`, key
   `auth:user_state:<user_id>`), so a user who was deactivated or demoted
   a second ago is already refused. If Redis and auth-service are both
   down, it falls back to the token's own claims and logs it.
5. **Database.** The handler gets a session from `get_db`
   (`shared/database.py::make_get_db`), which commits after the handler
   returns and before the response is sent.
6. **Response.** The service returns JSON (shapes in section 3.6b) and
   the `X-Request-ID` header. nginx adds the security headers on the way
   out.

A WebSocket (`wss://localhost:8443/ws/analysis:<id>?token=<jwt>`) follows
the same path to core-service, with the `Upgrade` headers passed through
and a 3600-second read timeout. The access log format `main_noargs`
leaves out query strings, so the token in the URL is never written to a
log file.

### 3.6b API response shapes (the client contract)

All three services use the same building blocks from `shared/`, so a
client sees the same shapes everywhere. A client (the future frontend,
a script) must be ready for every shape below.

**Request id.** `shared/request_id.py::RequestIdMiddleware` runs in every
service. It takes the caller's `X-Request-ID` header when it is a short
safe token (regex `[A-Za-z0-9._-]{1,64}`), otherwise it makes a new
`uuid4` hex. It stores the id in `request.state.request_id`, binds it to
every log line of the request (structlog context variable `request_id`),
sets it as the request's context variable (`current_request_id()`, which
fills the success body's `request_id`), and returns it in the
`X-Request-ID` response header. It is a pure ASGI
middleware, so streamed and Server-Sent-Events answers are not buffered.
WebSocket connections get no request id. Use the header to find the log
lines of a failed request: `bash scripts/logs.sh <service> | grep <id>`.

**Success bodies** (`shared/schemas.py`):

| Shape | Model | Used by |
|---|---|---|
| `{"data": <object>, "request_id": "<id>"}` | `APIResponse[T]` | most single-object routes under `/api/v1` |
| `{"items": [...], "total": N, "page": P, "page_size": S}` | `PaginatedResponse[T]`, **not** wrapped in `data` | every paginated list: core portfolios, transactions, notes, comments, documents, notifications, public-API holdings; ai analyses and decisions |
| `{"data": {"items": [...], "total", "page", "page_size"}, "request_id": "<id>"}` | `APIResponse[PaginatedResponse[T]]` | only auth `GET /api/v1/admin/users` |
| plain JSON or `204` with no body | route-specific | some routes, and every `/internal/*` route |

`request_id` inside `APIResponse` is filled automatically: its default is
`shared/request_id.py::current_request_id()`, the id the middleware set for
this request. So it is the same id as the `X-Request-ID` header, the log
lines and (for a failed request) the error body. No route sets it. The
shapes without a `data` envelope (paginated lists, plain JSON, 204) carry
the id only in the header.

**Pagination parameters** (`PaginationParams`): `page` from 1 to
`MAX_PAGE` = 100,000 and `page_size` from 1 to `MAX_PAGE_SIZE` = 100
(default `DEFAULT_PAGE_SIZE` = 20). The upper bound on `page` keeps
`(page - 1) * page_size` inside a PostgreSQL bigint `OFFSET`, so a huge
page number is a `422`, not a `500`.

**Error bodies.** There are two shapes, and a client must accept both.

1. The project envelope, built by `shared/exceptions.py::error_response`:
   `{"error": {"code": "...", "message": "...", "request_id": "..."}}`.
   Every `AppError` raised by service code becomes this body. The status
   comes from `EXCEPTION_STATUS_MAP`:

   | Class | `code` | Status |
   |---|---|---|
   | `NotFoundError` | `not_found` | 404 |
   | `ConflictError` | `conflict` | 409 |
   | `ValidationAppError` | `validation_error` | 422 |
   | `PermissionDeniedError` | `permission_denied` | 403 |
   | `InvalidCredentialsError` | `invalid_credentials` | 401 |
   | `RateLimitExceededError` | `rate_limit_exceeded` | 429 |
   | `UpstreamServiceError` | `upstream_service_error` | 502 |

   A subclass keeps its own `code` and inherits the status of its nearest
   registered parent (`_status_for` walks the class hierarchy). For
   example auth's `TwoFactorRequiredError` (code `two_factor_required`)
   is a 401 because it subclasses `InvalidCredentialsError`. An `AppError`
   with no registered ancestor is a `400`. Any other exception is logged
   as `unhandled_exception` (with the request id, because this handler
   runs outside the middleware's logging context) and answered `500`
   with code `internal_server_error`, message "An unexpected error
   occurred." and the id also in `X-Request-ID`. Rate-limit answers
   that are built directly (auth's `RateLimitedError`, code
   `rate_limited`, and core's public-API middleware codes such as
   `missing_api_key`, `invalid_api_key`, `rate_limit_exceeded`) use the
   same envelope plus a `Retry-After` header where it applies.
2. FastAPI's own shape `{"detail": ...}`. No handler converts these:
   - the token checks in `shared/security.py` (`401` "Missing bearer
     token", "Invalid or expired token", "Account is deactivated"),
     missing permissions (`403` "Insufficient permissions") and the
     internal-token check (`401` "Missing or invalid internal token");
   - request validation by FastAPI (`422` with `{"detail": [ ... ]}`, a
     list of field errors);
   - a few explicit `HTTPException`s in route code, for example
     ai-service's `503` "Portfolio ownership check is unavailable, try
     again later" (`deps.py`).

So a `403` can arrive in either shape: a missing permission is
`{"detail": "Insufficient permissions"}`, while an ownership refusal from
a service is `{"error": {"code": "permission_denied", ...}}`.

### 3.7 The life of one analysis (summary)

The full story, with every agent and decision rule, is in
[chapter 16](16-ai-analysis-pipeline.md). Here is the outline.

```
1. create       POST /api/v1/analyses {ticker, portfolio_id?}       (user, through the gateway)
                or POST /internal/analyses                           (market-open job, run_analysis.sh)
                -> admission: global cap ANALYSIS_QUEUE_MAX_PENDING=50 (503),
                   per-user cap ANALYSIS_MAX_ACTIVE_PER_USER=3 (429, public route only)
                -> row ai.analyses status=pending, committed, job put in the in-memory queue
                -> 201 returned at once
2. queue        one worker, first in first out, one analysis at a time,
                ANALYSIS_TIMEOUT_SECONDS=2700 (45 min) per analysis
3. start        status=running, event analysis.started
4. graph        LangGraph: episodic_memory_node -> supervisor_node
                -> selected agents in parallel (market, technical, fundamental, news ->
                   sentiment, filing, macro, risk, portfolio ...) -> agent_join_node
                -> bull_node + bear_node (debate rounds) -> judge_node
                -> reflection_node (may send an agent back once) -> recommendation_node
5. persist      agent outputs, debate, recommendation; for BUY/SELL on a portfolio:
                position sizing -> recommendation_tiers (status pending_acceptance)
                status=completed (or failed with failure_reason)
6. announce     analysis.completed, recommendation.ready (or analysis.failed)
                -> core-service: notification row + push on /ws/analysis:<id>, /ws/user:<id>
7. learn        archival notes, knowledge-graph facts, procedural memory (after the events)
8. later        user accepts or declines tiers (POST /api/v1/analyses/{id}/decisions);
                17:00 New York tier monitoring executes triggered tiers through
                core /internal/portfolios/{id}/transactions; 17:30 market close snapshots NAV
```

Three architecture points to remember:

- **The create call never waits for the LLM.** It returns `201` with
  `status: pending` (`services/ai-service/domains/analysis/api.py`). The
  work happens in the queue worker, with its own database session.
- **Strictly one analysis at a time.** Every source (users, the 09:30
  market-open job, operator scripts) feeds the same in-process FIFO queue
  (`domains/analysis/queue.py`). Analyses running in parallel compete for
  the same small LLM quotas, and then whole batches fail together.
  Serial running removes that competition. One analysis usually takes
  about 1.5 to 4 minutes (longer when a new SEC filing must be summarised
  first), so a 15-ticker batch takes roughly 30 to 40 minutes.
- **A restart of ai-service ends running analyses.** On shutdown the
  worker marks the in-flight analysis `failed` / `cancelled`. On startup,
  `fail_stale_analyses_on_startup` marks every analysis still `pending`
  or `running` as `failed` / `service_restart`, strictly before the worker
  starts (otherwise it would also fail jobs queued after the start).
  Nothing is resumed: the owner restarts ai-service precisely to abort
  runs, so everything left over belongs to the process that ended. This is why the operator scripts refuse restarts in
  the scheduler's busy windows ([chapter 02](02-operations.md)).

### 3.8 Startup order and health checks

Compose starts containers in dependency order. `condition:
service_healthy` means "wait until its health check passes", not just
"wait until it started".

```
level 0 (no dependencies)  postgres  redis  qdrant  objectstore  mailhog  vector
                              |        |       |         |          |
level 1                    auth-service (postgres, redis healthy)
                           core-service (postgres, redis, objectstore healthy)
                           ai-service   (postgres, redis, qdrant, objectstore healthy)
                           scheduler    (postgres, qdrant, objectstore healthy)
                           postgres-exporter (postgres healthy), redis-exporter (redis healthy)
                           alertmanager (mailhog started)
level 2                    gateway      (auth, core, ai ALL healthy)
                           prometheus   (alertmanager started) -> grafana (prometheus started)
```

The health checks:

| Container | Check | Timing |
|---|---|---|
| postgres | `pg_isready -h 127.0.0.1 -U $POSTGRES_USER -d $POSTGRES_DB` | every 5 s, 10 retries |
| redis | `redis-cli ping` | every 5 s, 10 retries |
| qdrant | raw `GET /readyz` over bash `/dev/tcp`, must contain ` 200 ` | every 10 s, 10 retries, start period 20 s |
| objectstore | SigV4-signed S3 `ListBuckets` with the real keys | every 5 s, 12 retries, start period 30 s |
| auth / core / ai | `python -c "urllib.request.urlopen('http://127.0.0.1:8000/health')"` must be 200 | every 15 s, timeout 5 s, 5 retries, start period 60 s (ai: 90 s) |

Why these exact checks:

- **postgres uses TCP (`-h 127.0.0.1`).** Without it `pg_isready` used
  the Unix socket, which the image's temporary setup server already
  answers during first-time initialisation, so the database looked
  healthy too early.
- **qdrant uses `/dev/tcp`** because the image has bash but no curl or
  wget. ai-service waits for it, so a Qdrant restart does not make
  ai-service fail its startup a few times.
- **objectstore uses a real signed request.** It is only healthy when the
  whole SeaweedFS chain (S3 gateway, filer, master, volume server) answers
  and accepts the configured keys.
- **The services' `/health` returns 503 when a dependency is down.**
  Each `main.py` checks its dependencies: auth checks database and Redis;
  core adds object storage; ai adds Qdrant and object storage. Any
  `false` gives HTTP 503 with `"status": "degraded"`. The service images
  have no curl, so the check uses Python's standard library.

What each service does while starting (its `lifespan` in `main.py`),
before it can answer `/health`:

| Service | Startup steps |
|---|---|
| auth-service | configure logging; `ensure_schema` (Alembic upgrade); write every user's live state to Redis (`mirror_all_users_safely`, failure only logged); create the avatar folder. At shutdown: dispose the database engine, close Redis and the HTTP clients |
| core-service | logging; `ensure_schema`; `ensure_bucket` for `S3_BUCKET_DOCUMENTS`; reload scheduled-job health from Redis (`load_job_health`); start six background tasks: pub/sub subscriber, durable subscriber, Yjs cross-replica listener, active-room heartbeat, WebSocket fan-out listener, outbox dispatcher. At shutdown it cancels the six tasks and waits for them (they use the engine and clients that close next), saves Yjs rooms with unsaved edits (`relay.persist_all_dirty`, they live only in memory), stops the Yjs validator workers, then closes the engine, Redis, HTTP and S3 clients |
| ai-service | logging; `ensure_schema`; `check_embedding_configuration` (provider and vector size must match, or startup fails); `ensure_collection` in Qdrant; rebuild the BM25 keyword index from Qdrant (log `bm25_index_rebuilt` with `chunk_count`); start the durable subscriber; fail stale analyses, then stale backtests; start the analysis queue worker. At shutdown it first stops the queue worker (the running analysis becomes `failed` / `cancelled`, which needs the database, so this comes before the engine closes), cancels the background backtests and the durable subscriber and waits for them, then closes the engine, Redis, HTTP, Qdrant, LLM-provider and S3 clients |

Every service also exposes `/metrics` through
`prometheus_fastapi_instrumentator`, with extra latency buckets
(0.1, 0.25, 0.5, 1, 2, 5, 10, 30 s; the library default stops at 1 s) so
the p95 panel and the `HighLatencyP95` alert (2 s) can see slow
requests ([chapter 27](27-observability.md)). Each service serves its
Swagger page at `/api/docs` and its OpenAPI file at a service-specific
path (`/api/openapi-auth.json`, `-core`, `-ai`); [chapter 10](10-gateway.md)
explains why the paths must differ.

Because the gateway waits for all three services to be healthy, **one
service that cannot start keeps the whole public entry point down** on a
fresh `up`. The usual causes are in section 6.

### 3.9 The development override

`docker-compose.override.yml` is read automatically by every `docker
compose` command in the repository folder. It changes the three Python
services:

```yaml
auth-service:
  build: { target: development }
  volumes:
    - ./shared:/app/shared
    - ./services/auth-service:/app
    - avatar_data:/app/data/avatars
  command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000",
            "--proxy-headers", "--forwarded-allow-ips=*"]
# core-service: same pattern; ai-service: same pattern plus LOG_LEVEL=DEBUG
```

What this means:

- **The image's `development` stage** is used (each service
  `Dockerfile` has `base`, `development` and `production` stages). It
  installs the development dependencies too and runs as root. The
  `production` stage installs with `--no-dev` and runs as an unprivileged
  user `app` (uid 10001). In production the code and the virtual
  environment stay owned by root, so the app can read but not change
  them; only the user's home directory is writable, plus `/app/data` in
  auth-service (the avatar volume, which takes this ownership when it is
  first created).
- **The source is bind-mounted.** `/app` in the container *is*
  `services/<name>/` on your disk, and `/app/shared` *is* `shared/`. The
  Python virtual environment is installed in `/opt/venv`
  (`UV_PROJECT_ENVIRONMENT=/opt/venv`), outside `/app`, so the bind mount
  does not hide it. (Docker creates an empty `shared/` folder inside each
  `services/<name>/` as the mount point; it is not a copy.)
- **No `--reload`.** uvicorn's reload mode watches the whole `/app` tree
  and restarts the process when any file changes, even a script the app
  never imports. That kills work in progress: running background
  analyses in ai-service, and every WebSocket and in-memory Yjs editing
  room in core-service (one file delivery restarted core-service four
  times in three seconds). The Dockerfiles' development `CMD` has no
  `--reload` either; the override still sets `command` explicitly so an
  image built from an older Dockerfile is also reload-free.
- **Forwarded headers are trusted.** `--proxy-headers
  --forwarded-allow-ips=*` makes the request's client address and scheme
  come from the gateway's `X-Forwarded-*` headers. Trusting every
  forwarder is safe because the services are reachable only inside the
  Docker network. The production `CMD` in each Dockerfile has the same
  flags.
- **Consequence: a code change needs a restart.** Edit a `.py` file, then
  `docker compose --env-file .env.docker restart <service>` (or
  `RESTART_ONLY=1 bash scripts/rebuild.sh <service>`). A change under
  `shared/` affects all three services, so restart all three. Do it
  outside the busy windows, and never while an analysis runs if you
  restart ai-service. A service that is not restarted can even run a
  mix: modules imported at startup stay old while modules imported later
  for the first time are new.

What still needs an image **rebuild** (`up -d --build`, or
`bash scripts/rebuild.sh <service>`) is described in
[chapter 02](02-operations.md), section 3.6.

### 3.10 How the images are built

- One `pyproject.toml` and one `uv.lock` at the repository root list the
  dependencies of all three services. Each service image installs the
  whole locked set with `uv sync --frozen` (uv pinned to `0.8.17` in the
  Dockerfiles, the same version the lock file is maintained with, so
  builds are reproducible). `--frozen` makes the build fail if `uv.lock`
  is out of date, instead of silently resolving new versions. Because
  `uv.lock` is copied into every service image, a change to it needs a
  rebuild of all three service images (the scheduler image does not use
  it).
- Python is pinned to 3.11 (`requires-python = ">=3.11,<3.12"`). The
  locked `y-py` 0.6.0 (Yjs bindings) only has prebuilt wheels up to
  Python 3.11 in `uv.lock`.
- The build context is the repository root. Each image copies `shared/`
  to `/app/shared` and `services/<name>/` to `/app`. `.dockerignore`
  excludes everything except the paths the Dockerfiles copy
  (`pyproject.toml`, `uv.lock`, `shared/`, `services/`,
  `infra/scheduler/`), and inside those it excludes `.env` files, virtual
  environments and caches.
- The scheduler image (`infra/scheduler/Dockerfile`) is Alpine 3.20 with
  `dcron`, `curl`, `tzdata`, `jq`, `postgresql16-client` and `rclone`,
  with `TZ=America/New_York`. Its job scripts and crontab are **copied**
  into the image, not mounted. `jq` parses Qdrant's REST answers and
  writes the backup manifest; `rclone` copies the object storage's
  objects through its S3 API. The folders `/backups`, `/avatar-data` and
  (for `restore`) `/objectstore-data` are deliberately not created in the
  image: they must
  come from Compose mounts, and `backup.sh` refuses to run (or skips the
  avatars) when one is missing, instead of silently backing up an empty
  folder inside the container.

### 3.11 Frontend status

There is no frontend in this repository yet; the Frontend Lead builds it
separately. The `frontend` service in `docker-compose.yaml` is commented
out (its build context `./frontend` does not exist yet), and nginx's
`location /` returns `404` until a frontend takes it over. The contract
it must follow is in section 3.6b.
Settings that already exist for it: `FRONTEND_BASE_URL` and
`BACKEND_CORS_ORIGINS` (both `http://localhost:3000` in `.env.example`).
Everything in this handbook is driven through the API, Swagger
(`/docs/<service>/`), the `/status` page, Grafana and the operator
scripts.

### 3.12 Tech stack and why each piece

| Piece | Why it was chosen |
|---|---|
| Python 3.11 + FastAPI | Async request handling for many I/O-bound calls (database, LLMs, other services), typed models with Pydantic, automatic OpenAPI/Swagger pages. |
| uvicorn | The standard ASGI server for FastAPI; `--proxy-headers` lets it trust the gateway's forwarded headers. |
| SQLAlchemy 2 (async) + asyncpg | Async ORM and a fast PostgreSQL driver, so a slow query does not block other requests. |
| Alembic (+ psycopg for its sync `env.py`) | Versioned schema changes; one migration chain per service, run automatically at startup. |
| PostgreSQL 16 | Transactions and constraints for financial data (ledger, tiers), JSONB for flexible fields, full-text search (`TSVECTOR`) for notes. |
| Redis 7 | Already needed for caches and rate limits; its pub/sub and streams give events without running a separate message broker. |
| Qdrant | A dedicated vector database for RAG similarity search, owned by ai-service so vector load stays off PostgreSQL. |
| SeaweedFS (S3 API) + boto3 | S3-compatible storage in one container. MinIO's public images were withdrawn from Docker Hub and Quay, so a fresh install could not pull it; SeaweedFS is pullable and speaks the same protocol. The code speaks plain S3 through boto3, so AWS S3 or Cloudflare R2 work by changing `S3_ENDPOINT_URL` and the keys. |
| LangGraph + the vendor SDKs (`google-genai`, `mistralai`, `openai`, `anthropic`) | LangGraph gives the agent graph with parallel fan-out, joins and conditional loops (debate rounds, reflection retries). Each LLM provider is called through its own SDK behind one router (`ai/llm/`); no LangChain wrapper is used (`langgraph` itself pulls in `langchain-core`). Google Gemini is the configured default (`LLM_DEFAULT_PROVIDER=google`). |
| bm25s | Keyword (BM25) search, fused with Qdrant's vector search for hybrid retrieval. |
| y-py | Python bindings for Yrs, the Rust implementation of the Yjs CRDT, for collaborative note editing in core-service. |
| nginx | Mature reverse proxy: TLS, WebSocket upgrade, path routing, rate limiting, static status page. |
| Prometheus + Alertmanager + Grafana + exporters | Standard open-source metrics, alert rules with unit tests, email alerts, dashboards. |
| vector | Copies every compose container's output to dated files on disk, independent of the application code, so logs survive container recreation. |
| MailHog | Catches all email locally, so verification and reset emails can be read without a real mail provider. |
| dcron in Alpine | A small cron for a handful of daily HTTP calls and the backup script. |
| structlog | Structured JSON logs with named events (`LOG_JSON=True` by default), easy to grep and parse. |
| uv | Fast, locked, reproducible Python dependency installs, on the host (tests, lint) and in the images. |

## 4. Configuration

All application settings come from `.env.docker` (created from
`.env.example`, never committed). Compose reads it twice: as the whole
environment of the three services, the scheduler and `restore`
(`env_file: .env.docker`), and, through `--env-file .env.docker`, to fill
`${...}` values inside `docker-compose.yaml`. The infrastructure
containers get only the values they need through those `${...}` values
(`postgres`: `POSTGRES_DB/USER/PASSWORD`; `objectstore`: the S3 keys;
`postgres-exporter`: the database login; `grafana`: its admin password
and root URL), so the application secrets are not in their environment. The settings classes read environment variables
only, never a `.env` file (`shared/config.py` has no `env_file`): a
container gets them from Compose, a host command from `--env-file`, a
test from `os.environ`. So a stray `.env` file in the working directory
cannot leak into a test run.

| Setting | Default | Meaning | Where |
|---|---|---|---|
| `APP_ENV` | `development` | One of `development`, `staging`, `production`, `test`. Outside `development`/`test` a weak `JWT_SECRET`, an empty `INTERNAL_API_TOKEN` or (auth-service) a missing `TOTP_ENCRYPTION_KEY` stops the service at startup. | `.env.example`, `shared/config.py` |
| `INTERNAL_API_TOKEN` | empty | Shared secret for every `/internal/*` route and the scheduler. Empty only allowed in development/test (the check is then skipped and one warning is logged per process). Make one with `openssl rand -hex 32`. | `.env.example`, `shared/config.py`, `shared/security.py` |
| `JWT_SECRET` | `CHANGE_ME` | Signs access and refresh tokens; outside development/test it must not be empty, the placeholder or shorter than 32 characters (in development/test a weak value only logs a warning). | `.env.example`, `shared/config.py` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `20` / `10` | Connections per service process. (pool + overflow) x replicas x 3 services must stay below PostgreSQL's `max_connections` (default 100); the defaults already use 90 with one replica each, so raise `max_connections` before adding replicas. | `shared/config.py` |
| `USER_STATE_CHECK_ENABLED` / `USER_STATE_CACHE_TTL_SECONDS` | `true` / `300` | Check every authenticated request against the live role and active flag in Redis; TTL of a state a reader fetched from auth-service after a Redis miss (auth's own writes have no TTL). The test configurations turn the check off. | `shared/config.py`, [chapter 29](29-security.md) |
| `AUTH_SERVICE_URL` / `CORE_SERVICE_URL` / `AI_SERVICE_URL` | `http://auth-service:8000` etc. | Where the internal clients send requests; also decides which hosts get the internal token. | `shared/config.py` |
| `POSTGRES_SCHEMA` | `auth` / `core` / `ai` | The service's own schema, first on `search_path`. | each service's `config.py` |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `postgres`, `5432`, `alphamind`, `postgres`, `postgres` | Database connection, used by the services, Alembic, the `postgres` container itself, postgres-exporter and the backup scripts. | `.env.example`, `shared/config.py` |
| `DATABASE_URL` | unset | Optional async SQLAlchemy URL that overrides the five values above for the services only (Alembic and the backups still use the parts), so it is not in the example file. | `shared/config.py` |
| `REDIS_HOST`, `REDIS_PORT` / `REDIS_URL` | `redis`, `6379` / unset | Redis connection; `REDIS_URL` overrides host and port. Logical databases come from `REDIS_DB_*` (a database number in the URL is ignored). | `.env.example`, `shared/config.py`, `shared/redis.py` |
| `REDIS_DB_CACHE` / `_RATE_LIMIT` / `_EVENTS` / `_QUEUE` | 0 / 1 / 2 / 3 | Logical Redis databases. | `shared/config.py` |
| `QDRANT_HOST`, `QDRANT_PORT` / `QDRANT_URL` | `qdrant`, `6333` / unset | Vector database (ai-service and the backup scripts); `QDRANT_URL` overrides host and port. | `.env.example`, `services/ai-service/config.py` |
| `QDRANT_API_KEY` | empty | Only for a Qdrant that requires a key: sent by ai-service's Qdrant client and by the backup and restore scripts. Empty = no key. | `.env.example`, `services/ai-service/config.py` |
| `QDRANT_COLLECTION_RESEARCH`, `QDRANT_VECTOR_SIZE`, `EMBEDDING_PROVIDER` | `research_documents_gemini`, `1536`, `google` | Must match each other, or ai-service refuses to start. | `services/ai-service/config.py` |
| `S3_ENDPOINT_URL` | `http://minio:9000` | Object storage endpoint (the SeaweedFS container's alias); also used by the backup and restore scripts. | `.env.example` |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | `alphamind-dev`, `change-me-objectstore-secret` in the example | The only S3 identity; Compose refuses to start without them. | `.env.example`, `docker-compose.yaml` |
| `S3_BUCKET_DOCUMENTS` | `research-documents` | Bucket for uploaded documents. | `.env.example` |
| `GRAFANA_ADMIN_PASSWORD` | empty (required) | Grafana `admin` password; Compose refuses to start without it. | `.env.example`, `docker-compose.yaml` |
| `SMTP_HOST`, `SMTP_PORT` | `mailhog`, `1025` | Where every service sends email. Any standard SMTP relay works by changing the `SMTP_*` values (no vendor SDK, only `aiosmtplib`). | `.env.example`, [chapter 28](28-email.md) |
| `LLM_DEFAULT_PROVIDER` | `google` | Which LLM provider the router uses by default. | `.env.example`, `services/ai-service/config.py` |
| `ANALYSIS_TIMEOUT_SECONDS` | `2700.0` | Wall-clock limit of one analysis in the queue. | `services/ai-service/config.py` |
| `ANALYSIS_QUEUE_MAX_PENDING` | `50` | Global cap of pending (queued, not yet running) analyses; a new create is refused with 503 when it is reached. | `services/ai-service/config.py` |
| `ANALYSIS_MAX_ACTIVE_PER_USER` | `3` | Per-user cap on the public create route (429). | `services/ai-service/config.py` |
| `LOG_LEVEL` | `INFO` (`DEBUG` for ai-service in the override) | Log verbosity. | `.env.example`, `docker-compose.override.yml` |
| `LOG_JSON` | `True` | JSON log lines (structlog). | `shared/config.py` |
| `FRONTEND_BASE_URL`, `BACKEND_CORS_ORIGINS` | `http://localhost:3000` | For the future frontend: the base of user-facing links in emails (verification, password reset, invitations), and the comma-separated list of origins CORS allows. | `.env.example`, `shared/config.py` |
| Gateway external HTTPS port | `8443` | In the port mapping and in nginx's `$external_https_port`. | `docker-compose.yaml`, `infra/gateway/nginx.conf` |
| `OAUTH_REDIRECT_BASE_URL` | `https://localhost:8443` | Public gateway address: OAuth callback base and Grafana's root URL (section 3.2). | `.env.example`, `services/auth-service/config.py`, `docker-compose.yaml` |

## 5. Design decisions and why

**Three services, not one and not ten.** The split follows three kinds of
work: identity (small, security-critical), the product surface (many
small fast requests and the live connections) and the AI engine (long
runs, heavy dependencies, external providers). A multi-minute analysis
never ties up the process that serves portfolio pages, and each service can be
restarted alone. A finer split (for example RAG as its own service)
would add network hops and contracts without a scaling need at this size.

**One PostgreSQL instance with one schema per service.** Alternative: a
database server per service. Three servers would triple backup and
monitoring work. Separate schemas plus "no cross-schema foreign keys"
keep the ownership rule clear, and a schema can still be moved out later
with a dump and restore.

**Only the gateway is public.** Storage and admin tools published on
every interface with default passwords would let anyone on the same
network, for example, read password-reset emails in MailHog and take
over accounts. So developer UIs bind to `127.0.0.1`, Grafana sits behind
the gateway with a required password, and everything else has no host
port.

**Internal routes need a token even though the gateway blocks them.**
The gateway's `404` for `/internal/` protects against the outside world,
but any container on the Docker network could still call them.
`require_internal_token` is the second wall ("defence in depth").

**Events for announcements, HTTP for questions.** A question needs an
answer now, so it is a direct call with a timeout and a clear error. An
announcement should not make the publisher wait for every listener or
fail when a listener is down, so it goes through Redis. Only events whose
loss would corrupt state use durable streams; the rest use cheap pub/sub.

**Only core-service holds WebSockets.** One place handles connection
authorization, channels and fan-out. ai-service and auth-service only
publish events.

**A serial, in-process analysis queue.** Alternative: a distributed task
queue (for example Celery with Redis). The real limit is the shared LLM
quota, not CPU, so running one analysis at a time is the point. An
in-process queue is simple; its known limits are that a restart ends the
running analysis and that it assumes one ai-service replica
(`domains/analysis/queue.py`).

**Bind-mounted source without auto-reload in development.** Fast
edit-and-restart without image builds, but no surprise restarts that kill
analyses, WebSockets or Yjs rooms (section 3.9).

**Pinned image versions.** Every pulled image has a fixed tag, so a
fresh install gets the same versions. Never pin an image to a version
older than the one that already wrote a volume: Grafana and Qdrant cannot
downgrade their stored data. Keep the application on a standard protocol
(S3) and the network name stable (`minio` alias), so a server can be
swapped without touching the services; this is how MinIO was replaced
by SeaweedFS when its public images disappeared.

**Logs to files by a separate collector.** Docker's own logs vanish when
a container is recreated, so a failed overnight batch could not be
investigated the next morning. vector reads from the Docker daemon, so
no service code had to change. Docker's own json-file logs are capped at
5 x 20 MB per container, because without a cap they grow on the same
fixed-size Docker disk that holds PostgreSQL and the backups.

## 6. When things go wrong

| Symptom | What the system does | How to diagnose |
|---|---|---|
| `docker compose up` stops with `set GRAFANA_ADMIN_PASSWORD in .env.docker` (or `S3_ACCESS_KEY`) | Compose refuses to interpolate a required value. | You ran Compose without `--env-file .env.docker`, or the value is empty in `.env.docker`. |
| Gateway does not start: "dependency failed to start" / a service is `unhealthy` | The gateway waits for all three services to be healthy. | `docker compose --env-file .env.docker ps`, then `bash scripts/logs.sh <service>`. Look for `SchemaBootstrapError` (schema in an unknown or archived state), an embedding/vector-size mismatch from `check_embedding_configuration`, or a dependency check failing in `/health`. |
| `/health/<service>` answers 503 with `"status": "degraded"` | One dependency check is `false`. | The JSON body names it: `database`, `redis`, `storage`, `qdrant`. |
| A browser call returns 404 although the route exists | nginx only forwards listed prefixes; everything else (and all `/internal/*`) is 404 at the gateway. | Check the path against the table in section 3.2. Internal routes must be called from inside a container. |
| An internal call fails with 401 `Missing or invalid internal token` | The receiver's `INTERNAL_API_TOKEN` differs from the caller's. | After editing `.env.docker`, recreate the containers (a `restart` keeps the old environment); see [chapter 02](02-operations.md). |
| A code change has no effect | No `--reload`; the old process still runs. | Restart the service; check a log line that only the new code writes. |
| Analyses show `failed` with `failure_reason = service_restart` or `cancelled` | ai-service was stopped or restarted while they were queued or running. | `bash scripts/psql.sh -c "SELECT id, ticker, status, failure_reason, created_at FROM ai.analyses ORDER BY created_at DESC LIMIT 10;"` |
| Live notifications stop | core-service's pub/sub subscriber reconnects with backoff 1, 2, 5, 10, 30 s (`shared/events.py`); a missed pub/sub message is not replayed. | `bash scripts/logs.sh core-service` and look for subscriber reconnect warnings; the data is still in the database. |
| A document stays `uploaded` | The durable stream redelivers until acknowledged. | `bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded` and ai-service logs (chapter 24). |

## 7. Demo walkthrough

Goal: show the evaluators the architecture live, from the outside in.
Run from the repository root with the stack already up (see
[chapter 02](02-operations.md) for bringing it up).

1. **Show the containers.**
   `docker compose --env-file .env.docker ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'`
   The audience sees 16 running containers. The three services and the
   four data stores (postgres, redis, qdrant, objectstore) are marked
   `(healthy)`; only the gateway with
   `0.0.0.0:8443` and `0.0.0.0:8000`; the developer UIs with
   `127.0.0.1:...`. Say: "Only the gateway is a public door."
2. **Show the redirect and TLS.**
   `curl -sI http://localhost:8000/status | head -3`
   They see `301` with `Location: https://localhost:8443/status`.
3. **Show the status page.** Open `https://localhost:8443/status` in the
   browser (accept the self-signed certificate once). Three green
   services. Say: "This page only calls `/health/auth`, `/core`, `/ai`
   through the gateway."
4. **Show one service's health.**
   `curl -sk https://localhost:8443/health/ai`
   They see `{"status":"ok","service":"ai-service","database":true,"redis":true,"qdrant":true,"storage":true}`.
   Say: "If any of these were false, this would be a 503 and the
   container would turn unhealthy."
5. **Show that internal routes are closed.**
   `curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/internal/stats`
   They see `404`. Then show the second wall from inside the network:
   `docker compose --env-file .env.docker exec -T ai-service python -c "import urllib.request,urllib.error; urllib.request.urlopen('http://core-service:8000/internal/stats')" 2>&1 | tail -1`
   With a token set in `.env.docker`, the last line shows
   `HTTP Error 401: Unauthorized`. Say: "Even inside the Docker network,
   no token, no answer."
6. **Show schema ownership.**
   `bash scripts/psql.sh -c "SELECT table_schema, count(*) FROM information_schema.tables WHERE table_schema IN ('auth','core','ai') GROUP BY table_schema ORDER BY 1;"`
   and
   `bash scripts/psql.sh -c "SELECT 'auth' AS schema, version_num FROM auth.alembic_version UNION ALL SELECT 'core', version_num FROM core.alembic_version UNION ALL SELECT 'ai', version_num FROM ai.alembic_version;"`
   Expected at the current code: auth `f1210b000001`, core
   `f1250c000001`, ai `f1250a000001`. Another value means a service has
   not been restarted since a new migration arrived, or its startup
   refused ([chapter 20](20-postgresql.md)).
   Say: "One database, three schemas, three independent migration
   histories."
7. **Show an authenticated request through the gateway.**
   `TOKEN=$(bash scripts/token.sh admin)` then
   `curl -sk https://localhost:8443/api/v1/auth/me -H "Authorization: Bearer $TOKEN"`
   They see the admin user's JSON. Say: "The gateway did not check this
   token; auth-service did, with the shared secret."
8. **Show the events transport.**
   `bash scripts/redis-cli.sh -n 3 --scan --pattern 'stream:*'`
   They see the durable streams (`stream:document.uploaded`, ...). Say:
   "These four events must never be lost; everything else is pub/sub."
9. **Show the Swagger pages.** Open `https://localhost:8443/docs/core/`
   and scroll through the routes. Point out there are no `/internal`
   routes in the gateway paths.

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Which containers can a machine on the same Wi-Fi reach?**
   Only the gateway, on ports 8443 (HTTPS) and 8000 (redirect). The
   developer UIs listen on `127.0.0.1` only.
2. **Why does ai-service store `portfolio_id` without a foreign key?**
   The portfolio lives in the `core` schema, owned by another service.
   Cross-schema foreign keys would tie the services' migrations and
   restores together. Integrity is kept by the application (for example
   the durable `portfolio.deleted` event).
3. **A browser calls `/internal/stats`. What happens?** nginx answers
   404 (`location ^~ /internal/`). Even inside the Docker network the
   route needs `X-Internal-Token`.
4. **Why is the gateway sometimes not running after `up`?** It waits for
   auth, core and ai to be healthy. If one cannot start (for example a
   schema bootstrap refusal), the gateway never starts.
5. **You changed a Python file in `services/core-service/`. Why does
   nothing change?** The source is bind-mounted but uvicorn runs without
   `--reload`. Restart the service.
6. **Where is the analysis queue stored?** In the memory of the
   ai-service process. A restart ends queued and running analyses; the
   next startup marks them `failed` / `service_restart`.
7. **Which events use Redis streams, and why?** `document.uploaded`,
   `document.deleted`, `portfolio.deleted`, and the database part of
   `document.status_changed`. Losing one would leave wrong state (an
   unindexed document, tiers trading in a deleted portfolio).
8. **Why does nginx re-resolve service names every 10 seconds?** A
   restarted container can get a new IP address, and Docker may give the
   old one to another container. A cached address would send requests to
   the wrong service.
9. **Who checks the JWT signature?** Each service, locally, with the
   shared `JWT_SECRET`. The gateway does not.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `docker-compose.yaml` | Every container, ports, volumes, health checks, dependencies, log limits, profiles. |
| `docker-compose.override.yml` | Development stage, bind-mounted source, uvicorn command without `--reload` and with `--proxy-headers`, ai-service `LOG_LEVEL=DEBUG`. |
| `shared/request_id.py`, `shared/exceptions.py`, `shared/schemas.py` | Request id middleware, error envelope and status map, response and pagination models (section 3.6b). |
| `.env.example` | Template and documentation of every environment setting. |
| `services/<name>/Dockerfile` | `base`, `development`, `production` stages; uv install into `/opt/venv`; non-root user in production. |
| `services/<name>/main.py` | FastAPI app, `lifespan` startup/shutdown steps, routers, `/health`, `/metrics`. |
| `services/<name>/db_bootstrap.py` | Calls `shared/alembic_bootstrap.py::bootstrap_schema` for the service's schema. |
| `services/<name>/api/internal.py` | The service's `/internal/*` routes, guarded by `require_internal_token`. |
| `services/core-service/clients/`, `services/ai-service/clients/` | Typed clients for other services' internal routes. |
| `services/<name>/event_listener.py` (core, ai) | Event subscriptions and handlers. |
| `services/ai-service/domains/analysis/queue.py` | The serial in-process analysis queue. |
| `shared/config.py` | `BaseServiceSettings`: shared settings, service URLs, Redis DB numbers, startup checks. |
| `shared/database.py` | Engine with per-service `search_path`, `make_get_db`. |
| `shared/redis.py` | Redis clients per logical database. |
| `shared/http_client.py` | Pooled `httpx` clients that add the internal token. |
| `shared/security.py` | JWT decoding, live user state check, `require_internal_token`. |
| `shared/events.py`, `shared/events_registry.py` | Pub/sub and durable stream transports; event names and owners. |
| `shared/alembic_bootstrap.py` | Startup schema bootstrap with Alembic. |
| `infra/gateway/nginx.conf` | Routing, TLS, headers, rate limit, resolver. |
| `infra/postgres/init-schemas.sql` | Creates `pgcrypto` and the three schemas on first initialisation. |
| `infra/scheduler/` | Scheduler image, crontab, job scripts, backup and restore. |
| `infra/objectstore/` | SeaweedFS entrypoint (writes the S3 identity) and health check. |
| `infra/prometheus/`, `infra/alertmanager/`, `infra/grafana/`, `infra/vector/` | Monitoring and logging configuration. |
| `pyproject.toml`, `uv.lock` | One locked dependency set for all services, Python 3.11. |

## 10. Related chapters

- [00-index.md](00-index.md) - reading paths
- [02-operations.md](02-operations.md) - running the stack day to day
- [10-gateway.md](10-gateway.md) - nginx in detail
- [11-auth-service.md](11-auth-service.md), [12-core-service.md](12-core-service.md), [13-ai-service.md](13-ai-service.md) - the three services
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md) - the full life of an analysis
- [20-postgresql.md](20-postgresql.md), [21-qdrant.md](21-qdrant.md), [22-object-storage.md](22-object-storage.md), [23-redis.md](23-redis.md) - the data stores
- [24-events.md](24-events.md) - events in detail
- [25-realtime-yjs.md](25-realtime-yjs.md) - WebSockets and collaborative editing
- [26-scheduler-jobs.md](26-scheduler-jobs.md) - the scheduled jobs
- [27-observability.md](27-observability.md) - logs, metrics, alerts
- [29-security.md](29-security.md) - security model
- [30-shared-library.md](30-shared-library.md) - the `shared/` package
- [../API_SPEC.md](../API_SPEC.md), [../ERD.md](../ERD.md), [../DESIGN.md](../DESIGN.md), [../PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md)
