# AlphaMind — Design

AlphaMind is an AI investment research platform: a user keeps portfolios
and watchlists, writes and shares research, uploads documents, and asks
for an AI analysis of a ticker that ends in a BUY / HOLD / SELL
recommendation with a confidence, a visible reasoning trail and, when a
portfolio is attached, price tiers that can be executed against it. Every
recommendation is later scored against the real market.

This document describes the system as built. Details live in `API_SPEC.md` (routes, events), `ERD.md`
(tables), the handbook (`handbook/15-ai-agents.md` and
`handbook/16-ai-analysis-pipeline.md` for the agents and the graph),
`DISASTER_RECOVERY.md` (backups and restore) and `PROJECT_STRUCTURE.md`
(the file tree).

## 1. Goals

1. Multi-user portfolio management with live updates.
2. A research workspace: notes (collaborative editing), comments,
   documents, and questions answered from those documents with citations.
3. Explainable AI analysis: several specialist agents, a debate, a judge
   and a reflection step, not one opaque LLM call.
4. Measured AI quality: every recommendation and every user decision is
   scored against the market later; backtests replay them.
5. An operable microservice deployment: independent services, each with
   its own tests, CI and image; backups, monitoring and alerts.

## 2. Architecture

```
Browser --HTTPS/WSS :8443--> nginx gateway (TLS, path routing, WebSocket
                              upgrade, per-IP limit, /status page)
                               |
        +----------------------+-----------------------+
        v                      v                       v
   auth-service           core-service             ai-service
   identity, roles,       portfolios, research,    LangGraph agents, RAG,
   organizations,         documents, notifications, LLM router, tiers,
   friends, presence      WebSockets, public API   backtests, analytics
        |                      |                       |
        +-----------+----------+-----------+-----------+
                    v                      v
   PostgreSQL (one instance,     Redis (cache, rate limits, pub/sub
   schemas auth / core / ai)     events, durable event streams)
                                           |
   SeaweedFS (S3 object storage:  Qdrant (vectors, ai-service only)
   documents)

   scheduler (cron -> internal routes, backups)   mailhog (dev email)
   Prometheus + Alertmanager + Grafana, postgres/redis exporters,
   vector (log shipping)
```

`docker-compose.yaml` runs all of it; `make up` builds and starts it (and
creates the gateway's self-signed certificate on first use).

### 2.1 Services

**auth-service** — the only service that issues tokens, hashes passwords
or checks TOTP codes. Registration with email verification, login with
optional 2FA, refresh-token rotation, password reset, OAuth (Google,
GitHub), avatars, system roles (admin, moderator, user, guest), admin user
management, organizations (members with fixed roles, invitations),
friends and presence.

**core-service** — the everyday product surface and the only holder of
WebSocket connections. Portfolios (a ledger of transactions; holdings are
derived from it), watchlists, snapshots and exports; research notes
(full-text search, Yjs collaborative editing, comments, moderation,
sentiment); documents in object storage; notifications; the public API
(API keys, per-key and per-owner limits); the market-open / market-close
jobs.

**ai-service** — the only service that talks to LLM providers, Qdrant and
the market data providers. The analysis graph, RAG (ingestion, retrieval,
the RAG assistant), memory, recommendation tiers and their execution,
backtests, decision outcomes and reviews, analytics, and the market data
cache.

**shared** — one Python package every service copies into its image:
configuration base, database and Redis factories, JWT verification,
permissions, exceptions and error bodies, logging, the event bus and its
registry, Alembic bootstrap, the rate limiter, enums and cross-service
schemas. Wire contracts and infrastructure only, never business logic.

### 2.2 Why three services

The split follows the three kinds of work: identity (small, security
critical), the product surface (many small, fast requests and the live
connections) and the AI engine (long runs, heavy dependencies, external
providers). Each can be built, tested, deployed and restarted on its own;
a long analysis never holds a request thread of the product API. A finer
split (e.g. notifications or RAG as their own services) would add network
hops and contracts without a scaling need at this size.

## 3. How the services talk

**Internal REST.** A service calls another's `/internal/*` routes through
a typed client (`clients/*.py`): `httpx` with a timeout, the shared
`X-Internal-Token`, the response parsed into a `shared.schemas`
model, a short Redis cache where the data allows it, and any failure
turned into `UpstreamServiceError` (502) so a raw `httpx` error never
reaches domain code. The gateway never proxies `/internal`.

**Events.** "Something happened" signals go through Redis
(`shared/events.py`, names and payloads in `shared/events.py`):
- pub/sub for notifications and live updates (a missed one loses only a
  message a user can still find by refreshing);
- durable streams with consumer groups for the events that must not be
  lost (`document.uploaded`, `document.deleted`, `portfolio.deleted`, and
  the database half of `document.status_changed`): redelivered until
  acknowledged, dead-lettered after repeated failures. core-service's
  three durable events are first written to the outbox table
  `core.event_outbox` in the same transaction as the change, then
  published after the response; a background dispatcher republishes any
  row that is still unpublished (at-least-once delivery, idempotent
  handlers).
Every publisher publishes after its own transaction committed (including
auth-service's `user.presence.changed`); every event has a consumer (the
table is `API_SPEC.md` §6).

**Live updates.** Only core-service holds WebSockets. ai-service and
auth-service publish events; core-service's listener turns them into
notifications and pushes to the `analysis:<id>`, `user:<id>`,
`document:<id>` and `presence:<id>` channels. With several core replicas,
a notification row is created once (a Redis claim per event) and each
replica pushes to its own sockets.

**Example: an analysis.** `POST /api/v1/analyses` returns at once with
`pending`; the serial analysis queue runs the graph (episodic memory ->
Supervisor -> data and analysis agents -> Bull/Bear debate -> Judge ->
Reflection -> recommendation -> position sizing into tiers);
`analysis.started`, `analysis.completed` / `failed` and
`recommendation.ready` reach the browser through core-service; the tier
monitoring job later executes accepted tiers against the portfolio.

## 4. Data

- One Postgres instance, one schema per service; each service's
  connections put its own schema first on `search_path`, so application
  code sees only its own tables.
- No cross-schema foreign keys: a reference to another service's row is a
  plain UUID, resolved through that service's internal API when more than
  the id is needed. Integrity across the boundary is enforced in the
  application (for example `portfolio.deleted` closes the portfolio's open
  tiers).
- Schema changes are Alembic migrations only. Each service has
  a baseline that builds its whole schema, and upgrades itself at startup;
  a database in an unknown state is refused rather than guessed.
- Documents are in S3 object storage (SeaweedFS); avatars are files in
  auth-service's `avatar_data` volume, served at `/static/avatars`; vectors in
  Qdrant; the BM25 index is rebuilt from Qdrant at ai-service startup.

Why these stores: Postgres for relational guarantees on financial data and
JSONB for flexible fields; one instance because three would triple backup
and monitoring work for no benefit at this scale (a schema can move out
later with a dump and restore). Redis because the system already needs it
for caching and rate limits; its streams give durability where it matters
without a separate broker. Qdrant, owned by ai-service, keeps vector search
load off the database the other services use.

## 5. Security

- **Tokens.** Short-lived HS256 access tokens and rotating refresh tokens,
  issued only by auth-service; every service verifies locally (no call to
  auth-service per request for the signature).
- **Live account state.** Every request also checks the user's current
  role and active flag (`shared/user_state.py`: a Redis entry that
  auth-service rewrites on every change, read with a fallback to
  auth-service), so a deactivated, deleted or demoted user loses access
  at once. If neither Redis nor auth-service can answer, the token's own
  claims are used (logged): an auth outage does not lock users out of
  core and ai.
- **Permissions.** One permission map for the system roles
  (`shared/permissions.py`); organization roles (owner, admin, member,
  viewer) are checked per resource; guests are read-only.
- **Public API.** `X-API-Key`; keys are stored hashed; per-key and
  per-owner limits (fixed one-minute windows, 429 with `Retry-After`).
  Rate limits of auth and core share one implementation
  (`shared/redis.py`) that lets requests through when Redis is down.
- **Transport and secrets.** TLS at the gateway; plain HTTP only inside
  the Docker network; `/internal` needs the shared internal token and is
  never exposed; secrets come from `.env.docker` (never committed;
  `.env.example` is the template).
- **Uploads.** Type, size and content checks; PDFs parsed in the ingestion
  worker, not in the request.

## 6. The AI engine

- **Graph.** LangGraph with dynamic fan-out: the Supervisor picks the
  agents; each agent runs a ReAct loop with its own tools and a
  structured final answer that is checked mechanically against the tool
  output before it is accepted. Bull and Bear debate over rounds; the
  Judge weighs the debate with the ticker's track record (episodic
  memory) as calibration only; Reflection checks citations and can send
  an agent back once with targeted feedback. Details:
  `handbook/15-ai-agents.md` and `handbook/16-ai-analysis-pipeline.md`.
- **LLM router.** One interface over Google (Gemini, the default),
  Mistral, OpenAI and Anthropic, with tiers per task, per-key pacing,
  cooldowns after rate limits and fallbacks between models.
- **RAG.** Category-aware chunking, hybrid BM25 + vector retrieval fused
  with RRF, per-category weights, and visibility per document (owner,
  organization, system). Filings, news and macro research are ingested
  automatically each night; users upload their own documents.
- **Measuring quality.** Every recommendation is scored after 30 days and
  every user decision after 7, 30 and 90 days; backtests replay
  recommendations and portfolios; a golden set measures retrieval recall;
  Prometheus counts analysis outcomes and LLM errors by kind.

## 7. Operations

- **Scheduling.** A `scheduler` container runs cron: market open and
  close (weekdays, NYSE holidays skipped by the services), tier
  monitoring, the nightly review, RAG ingestion, log cleanup and the
  nightly backup.
- **Observability.** Structured JSON logs (every HTTP request
  gets a request id, taken from a valid `X-Request-ID` or newly made; it is
  on every log line of the request, in error bodies and in the
  `X-Request-ID` response header), shipped by vector; `/metrics` on every service scraped by Prometheus; alert rules
  (with unit tests, `make promtool-test`) routed by Alertmanager; Grafana
  dashboards; `/health` per service and a gateway status page.
- **Backups.** Nightly Postgres dump, object storage and Qdrant snapshots,
  with a dry-run-first restore (`DISASTER_RECOVERY.md`).
- **CI.** One GitHub Actions workflow per service (flake8, mypy, the test
  suite against real Postgres and Redis containers, the image build) on
  changes to that service or `shared`, and a stack smoke workflow that
  starts the whole stack and checks the gateway and the alert rules.

## 8. Testing

- Service test suites run against real Postgres and Redis
  (testcontainers), not mocks: some faults only show up on the real
  database (string vs UUID ownership checks, a `search_path` set per
  connection, frozen timestamp defaults).
- External providers (LLMs, market data, Qdrant where needed) are
  replaced by test doubles; only the integration test (marked
  `integration`) uses the real Qdrant and embedding provider.
- Contract checks: the Alembic tests prove each schema built by the
  migrations equals the models; the event tests prove every event has a
  handler.
- End to end: `scripts/e2e_check.py` (core-service) and
  `scripts/e2e_tier_check.py` (ai-service) drive the running stack
  through the gateway, with an admin preflight and a run lock.
- `tests/integration/test_rag_recall.py` measures retrieval recall on
  the real corpus against fixed thresholds.
- Code quality: `make lint` runs flake8 with its default settings (no
  configuration file, 79-character lines) on `services`, `shared`,
  `tests` and `infra`, then `mypy --strict` on `shared` and each service. The code carries
  no comments or docstrings except tool pragmas; the handbook explains it
  (`handbook/31-testing-quality.md` section 3.0).
