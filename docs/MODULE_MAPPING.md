# AlphaMind — MODULE_MAPPING.md

This document maps the code to the modules of the ft_transcendence
subject (v21.1).

Scoring: a Major module is worth 2 points, a Minor module 1 point, and
only fully working modules count at evaluation. "Backend ✅" below means
every backend requirement of the module is implemented and tested and
can be shown through the API (Swagger, curl, WebSocket client). Modules
whose requirement is inherently something a user sees in the browser are
marked "needs frontend" even when their backend is complete.

---

## 1. Team and ownership (3 developers)

| Role | Owns |
|---|---|
| **Backend Lead** | `auth-service` (auth, users, administration, organizations, social), `core-service` (portfolios and ledger, research notes/comments/documents, notifications, WebSocket and Yjs relay, public API), gateway, compose, CI |
| **AI Lead** (Jenny) | `ai-service` (multi-agent analysis, RAG, LLM router, recommendations, decisions, backtests, analytics), monitoring, backups |
| **Frontend Lead** | `frontend/` — consumes all three services through the gateway at https://localhost:8443 |

---

## 2. Architecture in one picture

```
Browser / scripts / external API clients
        │  HTTPS 8443 (HTTP 8000 only redirects), WSS for WebSockets
        ▼
   nginx gateway ── /status page, /grafana/ (login), rate limit on /api/v1/auth
        │
   ┌────┴───────────────┬─────────────────────┐
   ▼                    ▼                     ▼
auth-service       core-service           ai-service
users, roles,      portfolios + ledger,   9-agent LangGraph pipeline,
admin, orgs,       research + Yjs,        debate/Judge/Reflection,
friends, 2FA,      notifications,         RAG + ask/stream, LLM router,
OAuth              public API (API keys), decisions, backtests,
                   WebSockets             analytics, sentiment
   │                    │                     │
   └─── REST (/internal/*, X-Internal-Token) + Redis pub/sub and streams ───┘
              │                  │                 │
        Postgres (auth/core/ai schemas)   Redis   Qdrant (ai only)
        S3-compatible object storage (SeaweedFS)
        Prometheus + exporters, Alertmanager, Grafana; scheduler (cron jobs, backups)
```

Services never import each other's code. They talk over REST
(`services/*/clients/*.py`, service-to-service routes under `/internal/*`
protected by a shared token) and over Redis events
(`shared/events_registry.py`).

---

## 3. Mandatory part (the project is rejected if any is missing)

| Requirement | Status |
|---|---|
| Frontend + backend + database, one-command Docker deployment | Backend ✅ (`make up`); frontend in progress |
| Several users at once, concurrent actions without corruption, real-time updates | ✅ (row locks and idempotency keys on the ledger and tiers, per-user queue limits, WebSocket fan-out) |
| Credentials in an ignored env file, `.env.example` provided | ✅ (`.env.docker` ignored, `.env.example` complete) |
| Clear database schema with relations | ✅ (`docs/ERD.md`, Alembic in every service) |
| Secure sign-up and login, hashed and salted passwords | ✅ (bcrypt, email verification, rate limits) |
| Input validation in the backend | ✅ (Pydantic on every route, 422 instead of 500 on bad input) |
| Input validation in the frontend | Frontend |
| HTTPS for every connection from outside | ✅ (TLS gateway on 8443; HTTP 8000 only redirects) |
| Privacy Policy and Terms of Service pages | Frontend |
| README with the required sections | Not written yet |

---

## 4. Modules

### 4.1 Web

| # | Module | Type | Pts | Status | Evidence |
|---|---|---|---|---|---|
| W1 | Framework for frontend and backend | Major | 2 | Needs frontend | FastAPI in all three services; the frontend framework is the Frontend Lead's part. |
| W2 | ORM | Minor | 1 | ✅ | SQLAlchemy 2.0 async (`shared/database.py`), Alembic per service. |
| W3 | Real-time features | Major | 2 | Backend ✅, needs frontend | `core-service` WebSockets: per-channel authorization, reconnect-safe Redis relay across replicas, each message delivered once per socket, presence heartbeats, analysis/tier/decision events on `analysis:<id>`, notification pushes. |
| W4 | Public API | Major | 2 | ✅ | `/api/v1/public/*` with `X-API-Key`, per-key and per-user rate limits (429), GET/POST/PUT/DELETE, Swagger at `/docs/core/`; keys of deactivated owners stop working immediately. |
| W5 | Notification system for all create/update/delete actions | Minor | 1 | ✅ | Every create, update and delete in core notifies the actor (and affected users); auth and ai actions arrive as events (`shared/enums.py` lists all types). In-app rows, live WebSocket push by default, optional email. |
| W6 | File upload and management | Minor | 1 | Backend ✅ (upload progress is frontend) | Type, size and content checks (a `.pdf` must really be a PDF), access-controlled S3 storage, download, inline preview, rename, delete with index clean-up. |
| W7 | Advanced search | Minor | 1 | ✅ | Notes: full-text search with filters, sort, pagination. Documents: filters (status, category, ticker, dates), sort, pagination. |
| W8 | Real-time collaborative features | Minor | 1 | Backend ✅, needs frontend | Yjs live editing of research notes: the Yjs document is the source of truth and is written back to the note text and search index; REST body edits get 409 during a session; organization viewers and guests are read-only; malformed updates are isolated in worker processes so they cannot crash the service. |

### 4.2 User management

| # | Module | Type | Pts | Status | Evidence |
|---|---|---|---|---|---|
| U1 | Standard user management | Major | 2 | Backend ✅, needs frontend | Profile update, avatar upload with a default avatar, friends with online status, profile data. |
| U2 | Remote authentication (OAuth 2.0) | Minor | 1 | Code ✅, not configured | Google and GitHub, with 2FA enforced on OAuth logins. No OAuth app is registered yet: register both with the real domain before the evaluation. |
| U3 | Two-factor authentication | Minor | 1 | ✅ | TOTP, enforced on password and OAuth logins, lockout after repeated failures. |
| U4 | Advanced permissions system | Major | 2 | ✅ (role-based UI is frontend) | System roles admin / moderator / user / guest (`shared/permissions.py`); admin user CRUD (`/api/v1/admin/users`), deactivate/reactivate/soft delete, site statistics; moderators hide/delete any note or comment and deactivate users; guests are read-only everywhere; `/me` returns `permissions` so the UI can adapt; a demotion or deactivation applies immediately to existing tokens. |
| U5 | Organization system | Major | 2 | ✅ | Create/edit/delete organizations, invite, join, leave, remove, change roles; OWNER/ADMIN/MEMBER/VIEWER enforced (viewers are read-only in shared notes and documents); shared portfolios. |

Not claimed: "User activity analytics" (Minor) — no dedicated activity
dashboard.

### 4.3 Artificial intelligence

| # | Module | Type | Pts | Status | Evidence |
|---|---|---|---|---|---|
| A1 | Complete RAG system | Major | 2 | ✅ | Ingestion of filings, news, macro research and user uploads; category-aware chunking; hybrid BM25 + vector + RRF retrieval with per-user visibility; users ask questions at `POST /api/v1/rag/ask` and get answers grounded in cited sources; the analysis agents also retrieve from it. |
| A2 | Complete LLM system interface | Major | 2 | ✅ | Text generation from user input (`/rag/ask`, `mode=draft` writes a note paragraph), streaming over SSE (`/rag/ask/stream`), error events instead of broken streams, per-user rate limits, multi-provider router with fallbacks and timeouts. |
| A3 | Sentiment analysis | Minor | 1 | ✅ | Sentiment of user-generated content: every research note and comment is scored (label and score) after it is saved; `GET /api/v1/research/sentiment/summary` aggregates it per ticker and per day. |

Not claimed: "Recommendation system using machine learning" (Major) —
recommendations come from multi-agent LLM reasoning, not a trained
model (claimed as C1 instead).

### 4.4 DevOps

| # | Module | Type | Pts | Status | Evidence |
|---|---|---|---|---|---|
| D1 | Monitoring with Prometheus and Grafana | Major | 2 | ✅ | `/metrics` on every service plus postgres/redis exporters, custom metrics for analyses, queue and LLM errors, two provisioned dashboards, 12 alert rules (9 of them with promtool unit tests), Alertmanager email, Grafana behind the gateway with a required admin password and no anonymous access. |
| D2 | Backend as microservices | Major | 2 | ✅ | Three services with their own schemas, images and migrations; REST with typed clients and Redis events; internal routes protected by a service token. |
| D3 | Health check, status page, backups and disaster recovery | Minor | 1 | ✅ | `/health` returns 503 when a dependency is down, `/status` page, daily backups of Postgres, Qdrant, object storage and avatars with checksums and 7-day retention, dry-run-first restore, `docs/DISASTER_RECOVERY.md`. |

Not claimed: ELK log management — logs are structured JSON collected by
Vector into files, not an Elasticsearch/Logstash/Kibana stack.

### 4.5 Data and analytics

| # | Module | Type | Pts | Status | Evidence |
|---|---|---|---|---|---|
| N1 | Advanced analytics dashboard | Major | 2 | Backend ✅, needs frontend | Overview and time series with date ranges and filters (`/api/v1/analytics/overview`, `/timeseries`), CSV export of analyses/recommendations/decisions, portfolio history and transaction CSVs; live updates can use the existing WebSocket events. Charts and PDF export are frontend work. |

Not claimed: "Data export and import" (Minor) — export exists, import
does not. GDPR (Minor) — not implemented.

### 4.6 Modules of choice

| # | Module | Type | Pts | Status |
|---|---|---|---|---|
| C1 | Multi-agent investment debate and reflection engine | Major | 2 | ✅ |
| C2 | Decision tracking and backtesting engine | Minor | 1 | ✅ |
| C3 | Per-service CI pipeline | Minor | 1 | ✅ |

**C1 (Major).** *Why:* one LLM call producing a recommendation cannot be
checked. The engine selects up to 9 specialist agents, each running its
own tool-using ReAct loop with evidence gates, then runs a multi-round
Bull/Bear debate, a Judge that decides convergence and weighs its own
past track record on the ticker (L2 episodic memory with measured
outcomes), and a Reflection stage that verifies claims against tool
evidence and re-runs the agents that failed. *Challenge:* a cyclic
LangGraph with concurrent agent writes, retries and failure states that
are recorded honestly (no placeholder verdicts); a production LLM layer
that survives provider rate limits and overloads (pooled keys, pacing,
escalating cooldowns, fallback chains). *Why Major:* comparable in scope
to the official ML recommendation Major, implemented as auditable
multi-agent reasoning.

**C2 (Minor).** Decision tracking (one decision per user and
recommendation, accept/decline of trade tiers that the scheduler then
executes into the ledger), three backtests (recommendation replay,
single-ticker triggered tiers, whole-portfolio time-weighted return with
external flows handled correctly), review workflow scoring AI accuracy
after 30 days and user decisions at 7, 30 and 90 days. A live portfolio evaluation is not
part of it and is not claimed.

**C3 (Minor).** GitHub Actions per service (path filters, flake8 and
mypy with errors failing the job, the service's test suite, the
production image build) plus an integration workflow.

---

## 5. Point total

| Category | Shown through the API today | Needs the frontend to be shown |
|---|---|---|
| Web | 6 (W2, W4, W5, W6, W7) | 5 (W1, W3, W8) |
| User management | 6 (U2 after OAuth apps are registered, U3, U4, U5) | 2 (U1) |
| Artificial intelligence | 5 (A1, A2, A3) | 0 |
| DevOps | 5 (D1, D2, D3) | 0 |
| Data and analytics | 0 | 2 (N1) |
| Modules of choice | 4 (C1, C2, C3) | 0 |
| **Total** | **26** | **9** |

Every row was checked against the module list of the subject (v21.1):
each module above exists there with the type shown. The minimum of 14
points is covered by the API-demonstrable modules alone. With the
frontend parts done the claim is 35 points. The subject counts at most 5
bonus points above the required 14 (each extra module must be fully
working and justified in the README), so the extra modules are a margin
for modules that fail at the evaluation, not a higher grade. Evaluators
count only modules that fully work, so U2 needs its OAuth apps
registered and every "needs frontend" module needs its screens before
the evaluation.

---

## 6. What not to claim

| Do not claim | Why |
|---|---|
| Database or Docker as modules | Mandatory baseline, not modules |
| ELK log management | No Elasticsearch/Logstash/Kibana |
| Official ML recommendation system | No trained model — claimed as C1 |
| User interaction (chat) | No chat |
| Data export/import, GDPR | Import and GDPR flows not implemented |
| User activity analytics | No activity dashboard |

---

## 7. Evaluation demo checklist

Items marked (frontend) need the frontend; everything else can be shown
with Swagger (https://localhost:8443/docs/{auth,core,ai}/), curl or a
WebSocket client.

- [ ] Fresh clone: `cp .env.example .env.docker`, fill the secrets, `make up` (creates the certificate and starts everything with one command)
- [ ] https://localhost:8443/status all green; http://localhost:8000 redirects to HTTPS
- [ ] Register, verify email (MailHog), log in, enable 2FA, log in with 2FA; OAuth login (after registering the apps)
- [ ] First admin with `scripts.set_system_role`; list/edit users, make someone a guest and show a 403 on a write, deactivate a user and show their token rejected at once
- [ ] Organization: create, invite, accept, change a member to viewer and show read-only access to a shared note
- [ ] Portfolio: deposit, buy, sell, transfer shares in (adjustment), CSV export of history and transactions; notifications for each action
- [ ] Upload a PDF, preview it, see it indexed, ask a question at `/api/v1/rag/ask`, watch `/rag/ask/stream` stream tokens with citations
- [ ] Write a note and a comment, show their sentiment and the ticker sentiment summary; moderator hides a comment
- [ ] Two clients editing one note live (frontend); REST body edit gets 409 during the session
- [ ] Run an analysis, follow `analysis:<id>` events over WSS, inspect agents, debate, Judge track record and Reflection; accept a tier and show its execution notification
- [ ] Backtests and analytics overview/time series/CSV export; dashboard charts (frontend)
- [ ] Public API with `X-API-Key`, trigger a 429
- [ ] Grafana dashboards behind login; stop an exporter and show the alert email in MailHog
- [ ] `make backup` and a dry-run `make restore`
- [ ] A GitHub Actions run testing only the changed service

---

# END OF MODULE_MAPPING.md
