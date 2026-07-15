# AlphaMind
# AI Investment Research Operating System — Technical Design Document

Version: 4.0 (supersedes 3.0)

---

# Document Information

| Field | Value |
|---|---|
| Project Name | AlphaMind |
| Version | v4.0 |
| Project Type | AI Investment Research Operating System |
| Architecture Style | Microservices + Event-Driven |
| Frontend | Next.js + TypeScript |
| Backend | 3 FastAPI services (`auth-service`, `core-service`, `ai-service`) + Nginx gateway |
| Shared Library | `shared` (shared source, no packaging step) |
| Database | PostgreSQL (1 instance, 1 schema per service) |
| Vector Database | Qdrant (ai-service only) |
| Cache / Events | Redis (cache, rate limiting, cross-service Pub/Sub) |
| AI Framework | LangGraph |
| Team Size | 3 (Backend Lead, AI Lead, Frontend Lead) |
| Development Duration | 6 weeks |
| Target Score | 14 minimum / 34 baseline (`MODULE_MAPPING.md v4.0`) |
| Target Users | Retail Investors / Research Teams |

---

# 1. Executive Summary

AlphaMind is an AI investment research platform built as 3
independently deployable microservices — `auth-service` (identity),
`core-service` (portfolio/research/notification + the real-time
gateway to the frontend), and `ai-service` (the multi-agent AI engine)
— behind an Nginx gateway. The 3 services share exactly one piece of
code, `shared`, a shared Python source package (no packaging/install step) containing
infrastructure glue and wire contracts, never business logic.

The core product experience: a user builds a portfolio, uploads
research documents, and requests an AI analysis of a ticker. That
analysis runs as a LangGraph workflow — 8 specialist agents analyze in
parallel, a Bull and a Bear agent debate, a Judge weighs the debate, and
a Reflection agent checks evidence quality — before a final BUY/HOLD/
SELL recommendation with a confidence score reaches the user, live,
over WebSocket. Every recommendation is tracked against real market
outcomes and can be backtested against a real benchmark index.

---

# 2. Problem Statement

Retail investors and small research teams lack a tool that combines (a)
personal portfolio tracking, (b) a real research workspace with
document-grounded Q&A, and (c) transparent, auditable AI analysis — most
existing "AI investing" tools produce an opaque recommendation with no
visible reasoning trail. AlphaMind's debate/reflection design is a
direct answer to that gap.

---

# 3. Product Objectives

1. Multi-user portfolio management with real-time updates.
2. A research workspace with document upload and RAG-grounded Q&A.
3. Explainable multi-agent AI analysis (not a single opaque LLM call).
4. Measurable AI quality: decision tracking + backtesting against real
   benchmarks, not just self-reported confidence.
5. A genuinely operable microservices deployment: 3 services, each
   independently buildable, testable, and deployable, with its own
   CI/CD pipeline.

---

# 4. User Personas

- **Retail Investor** — manages a personal portfolio, wants
  AI-assisted second opinions before buying/selling.
- **Research Analyst (Organization member)** — collaborates on shared
  research notes and documents within an organization.
- **Organization Admin** — manages membership, roles, and shared
  portfolios/research.

# 5. User Stories (representative sample)

- As a user, I can register, verify my email, and enable 2FA.
- As a user, I can create a portfolio and record buy/sell transactions.
- As a user, I can upload a 10-K filing and ask "what are the main
  risk factors?" and get a cited answer.
- As a user, I can request a full AI analysis of a ticker and watch the
  agent workflow execute live.
- As a user, I can see whether my past AI-assisted decisions actually
  outperformed a benchmark.
- As an organization admin, I can invite a member and control what they
  can create/edit.

---

# 6. Architecture Overview

```
Frontend (Next.js)
        │  HTTPS / WSS
        ▼
Nginx Gateway (path-based routing, WebSocket upgrade — config only)
        │
   ┌────┼─────────────────┬──────────────────┐
   ▼                       ▼                    ▼
auth-service          core-service           ai-service
(Backend Lead)        (Backend Lead)         (AI Lead)
- users               - portfolios            - LangGraph agents
- oauth_accounts      - research/documents      - RAG (Qdrant)
- 2FA                 - notifications            - LLM router
- organizations       - WebSocket connections     - recommendations
- connections         - public API (W4)            - backtesting
                                                     - market data
                                                     - analytics
   │                       │                        │
   └───────────┬───────────┴──────────┬─────────────┘
               ▼                      ▼
        PostgreSQL (1 instance,   Redis (cache, rate
        schemas: auth/core/ai)    limit, cross-service
                                  Pub/Sub events)
                                     │
                                     ▼
                             Qdrant (ai-service only)

shared — shared, installable package: config base,
DB/Redis factories, JWT verification, exceptions, logging, event bus,
ORM mixins, enums, Pydantic base schemas + cross-service refs.

Prometheus + Grafana scrape all 3 services' /metrics.
```

See `PROJECT_STRUCTURE.md v4.0` for the full repository file tree.

---

# 7. Service Boundaries and Responsibilities

## 7.1 auth-service

**Single responsibility**: identity and access management. The only
service that issues JWTs, hashes passwords, or validates TOTP codes.

- User accounts (create/update/delete), login, OAuth (Google/GitHub),
  2FA (TOTP).
- RBAC: `SystemRole` (admin/user), `OrganizationRole`
  (owner/admin/member/viewer).
- Organizations: CRUD, membership, invitations.
- Social: connections (add/accept/block), presence (`last_seen_at`).
- `app/api/internal.py`: `/internal/users/{id}`,
  `/internal/organizations/{id}` — resolved by other services'
  `auth_client.py`, never exposed through the public gateway.

## 7.2 core-service

**Single responsibility**: the everyday product surface + being the
sole holder of live WebSocket connections.

- Portfolio: CRUD, holdings, transactions, watchlists, snapshots.
  `owner_id`/`organization_id` are plain UUIDs resolved via
  `auth_client.py`, never a local join.
- Research: notes, comments, tags, document upload (PDF/TXT/MD) with
  validation/storage/preview/progress/delete. Publishes
  `document.uploaded` on save.
- Notification: generated from local domain events and cross-service
  events (via `event_listener.py`).
- WebSocket: `websocket_manager.py` holds connections;
  `event_listener.py` subscribes to ai-service's/auth-service's
  Redis-published events and rebroadcasts them, wrapped in
  `WebSocketEnvelope`.
- Public API (Web module W4): `/api/v1/public/*`, `X-API-Key` header,
  Redis-backed sliding-window rate limit, 5 endpoints across
  GET/POST/PUT/DELETE.

## 7.3 ai-service

**Single responsibility**: the AI engine, and the only service that
touches Qdrant or the LLM provider SDKs.

- LangGraph orchestration: Supervisor + 12 agents (see `Agents.md v4.0`).
- RAG: chunking, embedding, Qdrant indexing/retrieval, citation —
  triggered by consuming `document.uploaded`, not by a direct call from
  core-service.
- Multi-LLM router: OpenAI, Anthropic, Google, Mistral; streaming;
  cost/usage tracking.
- Recommendation, debate, reflection.
- Decision tracking + backtesting against real benchmarks.
- Market data ingestion/caching (`domains/market`), consumed by the
  Market/Technical/Fundamental agents and, read-only, by core-service's
  portfolio valuation via `ai_client.py`.
- AI-driven analytics (`domains/analytics`).
- `app/api/internal.py`: `/internal/analyses/{id}`,
  `/internal/recommendations/latest` — resolved by core-service's
  `ai_client.py`.

---

# 8. Inter-Service Communication

## 8.1 Synchronous — REST via typed clients

Every inter-service REST call follows one shape
(`app/clients/*.py`, see `core-service/app/clients/auth_client.py` as
the reference implementation):

1. `httpx` call to the other service's `/internal/*` endpoint.
2. Parse the response into an `shared.schemas.refs` model.
3. Cache in Redis (short TTL — 60s for identity data, 10-30s for
   AI/market data that changes faster).
4. On failure, raise `shared.exceptions.UpstreamServiceError`
   (mapped to HTTP 502) — never let a raw `httpx` exception escape into
   domain code.

`/internal/*` routes are not proxied by the Nginx gateway — reachable
only on the Docker-internal network.

## 8.2 Asynchronous — Redis Pub/Sub

For "something happened, other services may care" signals:
`shared.events.publish_cross_service(settings, event_name,
payload)` publishes to `events:<event_name>`; a
`CrossServiceSubscriber` running as a background task in the
consuming service's `main.py` lifespan reacts.

### 8.2.1 Event Catalog (`shared.events_registry.EventName`)

| Event | Publisher | Consumer(s) |
|---|---|---|
| `document.uploaded` | core-service | ai-service |
| `document.status_changed` | ai-service | core-service |
| `analysis.started` | ai-service | core-service |
| `analysis.completed` | ai-service | core-service |
| `analysis.failed` | ai-service | core-service |
| `recommendation.ready` | ai-service | core-service |
| `decision.outcome.evaluated` | ai-service | core-service |
| `organization.member.joined` | auth-service | core-service, ai-service |
| `user.presence.changed` | auth-service | core-service |
| `portfolio.created` | core-service | auth-service (audit, optional) |

## 8.3 Example: "Analyze Tesla" End to End

```
Frontend --POST /api/v1/analyses {"ticker":"TSLA"}--> Gateway --> ai-service
ai-service: reads price/fundamentals from its own domains/market
            (if portfolio-aware, calls core-service's
            /internal/portfolios/{id}/holdings via core_client.py)
ai-service: LangGraph runs Supervisor -> 8 data/analysis agents (parallel
            where possible) -> Bull/Bear debate -> Judge -> Reflection
ai-service: publishes analysis.started, then periodic
            analysis.agent_step.completed-equivalent progress, then
            analysis.completed + recommendation.ready
core-service: event_listener.py receives these via Redis Pub/Sub,
              rebroadcasts over WebSocket (WebSocketEnvelope) to the
              frontend, and creates a persisted notification
Frontend: renders the live workflow timeline, then the final
          recommendation with confidence and citations
```

---

# 9. Database Design

Full schema in `ERD.md v4.0`. Summary of the strategy:

- **One Postgres instance, one schema per service** (`auth`, `core`,
  `ai`) — each service's connection sets its own `search_path`.
- **No cross-schema foreign keys.** A column referencing another
  service's entity is a plain UUID with a comment; resolved via that
  service's REST client when more than the ID is needed.
- **62 tables total** across the 3 schemas (`auth`: 11, `core`: 14,
  `ai`: 37) — see `ERD.md v4.0` for full definitions.
- Every table inherits `shared.models.{UUIDPKMixin,
  TimestampMixin}` — consistent primary keys and timestamps everywhere.
- Migrations: each service has its own Alembic setup
  (`services/<name>/migrations/`), with `version_table_schema` set to
  that service's schema, so 3 services' migration histories never
  collide even though they share one Postgres instance.

---

# 10. Security Design

- **JWT**: HS256, shared secret across the 3 services (documented
  upgrade path to RS256 in `COMMON_LIBRARY_README.md`). Only
  auth-service issues tokens (`services/auth-service/app/security.py`);
  every service can verify (`shared.security`).
- **OAuth 2.0**: Google, GitHub — auth-service only.
- **2FA**: TOTP, auth-service only.
- **RBAC**: `SystemRole` + `OrganizationRole`
  (`shared.enums.role`), enforced via
  `shared.security.require_roles` at the endpoint level and
  domain-specific `permission.py` checks at the entity level.
- **Public API**: `X-API-Key` header, Redis-backed rate limiting
  (`core-service/app/middleware.py`), scoped per key
  (`core.api_keys.scopes`).
- **Transport**: HTTPS/WSS at the gateway for external traffic;
  plaintext between containers on the internal Docker network (per the
  subject's own carve-out for internal traffic).
- **Secrets**: `.env` per environment, never committed; `.env.example`
  provided; each service only receives the env vars it actually needs
  (documented in that service's `app/config.py`).

---

# 11. Observability

- **Logging**: `shared.logging` — structured JSON, every line
  tagged with `service` and (via middleware / manual context) a
  `request_id`, so a single request's path across 2 services can be
  correlated by grepping that id.
- **Metrics**: every service exposes `/metrics`
  (`prometheus-fastapi-instrumentator`); Prometheus scrapes all 3
  (`infra/prometheus/prometheus.yml`); Grafana (`infra/grafana/`)
  visualizes per-service latency, error rate, and request volume, plus
  ai-service-specific panels for LLM token cost.
- **Health**: each service exposes `/health` (checks its own DB/Redis/
  Qdrant-if-applicable connectivity); the gateway exposes
  `/health/auth`, `/health/core`, `/health/ai` as a convenience
  aggregation (still 3 independent checks, not a combined one).

---

# 12. Deployment

`docker-compose.yaml` — 9 containers: `frontend`, `gateway`
(Nginx), `auth-service`, `core-service`, `ai-service`, `postgres`,
`redis`, `qdrant`, `prometheus`, `grafana`. Single command:
`docker compose up`.

Each service's `Dockerfile` builds with the repository root as build
context (needed to `COPY` both `shared` and that
service's own folder into the image), producing an independent image
per service — these could be pushed to 3 separate registry repositories
and deployed/scaled/rolled-back independently in a real production
setting.

---

# 13. CI/CD

Each service has its own GitHub Actions workflow
(`services/<name>/.github/workflows/ci.yml`), triggered on changes to
that service's own folder OR to `shared` (since every
service depends on it). Pipeline: `uv sync` → `ruff` (lint) → `mypy`
(types) → `pytest` (with throwaway Postgres+Redis service containers in
CI) → Docker build → (on `main`) push to a registry.

Claimed as `MODULE_MAPPING.md v4.0`'s Modules of Choice C3 (Minor,
1 pt) — see that document for the full justification. Not claimed as a
standalone Devops/Web module, since no such module exists in the
official subject.

---

# 14. Testing Strategy

- **Unit tests** per service: `service.py`/`repository.py` logic,
  mocking `app/clients/*.py` calls to other services.
- **Contract tests**: verify each service's `/internal/*` response
  actually matches the `shared.schemas.refs` model the
  consuming service expects — the automated defense against the
  "inter-service contract drift" risk in `ROADMAP.md v4.0 §7`.
- **Integration tests**: at least one test suite that boots 2+ services
  together against real (test) Postgres/Redis and exercises a real
  cross-service call.
- **End-to-end**: Playwright/Cypress against the full `docker compose
  up` stack for the critical user journeys (register → portfolio →
  upload → analyze → recommendation).

---

# 15. Disaster Recovery / Resilience Notes

- Postgres: scheduled `pg_dump` of the single instance (all 3 schemas
  backed up together, since they're one physical database).
- Redis: used for cache/rate-limit/events, none of which need
  durability across a restart — acceptable to lose on a Redis restart;
  no backup strategy needed for it.
- Service failure isolation: JWT verification being stateless means
  auth-service being briefly down doesn't invalidate existing sessions
  in core-service/ai-service (see `system_design.md v4.0` Q15) — this
  is deliberately demonstrated during evaluation.

---

# END OF technical_design_doc.md
# Version 4.0
