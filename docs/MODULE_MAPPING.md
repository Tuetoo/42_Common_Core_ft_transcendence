# AlphaMind — MODULE_MAPPING.md

This document maps the code to the modules of the ft_transcendence
subject (v21.1).

Scoring: a Major module is worth 2 points, a Minor module 1 point, and
only fully working modules count at evaluation. "Backend ✅" below means
every backend requirement of the module is implemented and tested and
can be shown through the API (Swagger, curl, WebSocket client). The
frontend is the Next.js app in `frontend/`, served by the gateway at `/`;
"Frontend:" in the evidence names its pages under `frontend/app/`.

---

## 1. Team and ownership (3 developers)

| Member | Role | Owns |
|---|---|---|
| Jenny | **Backend Lead** | `auth-service`, `core-service`, `ai-service`, `shared/`, database, tests, `docker-compose.yaml`, CI/CD, cloud deployment |
| Ishaaq92 | **Infra Lead** | `infra/` (gateway, monitoring, scheduler, backups, log collection) |
| yi-hsiu-yang | **Frontend Lead** | `frontend/` — the Next.js app, served by the gateway at https://localhost:8443/ |

The subject (II.1.1) also requires the roles Product Owner (PO), Project
Manager (PM) / Scrum Master, Technical Lead / Architect and Developers
(all members), and the README must name each member's role(s). With 3
members, people hold several roles.

> **TODO (owner):** assign PO, PM and Tech Lead to members (all three are
> Developers) and add them to the table above and to the README.

> **TODO (owner):** fill the "Worked on by" column of every module table in
> section 4 (the README must say which member(s) worked on each module).

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
(`shared/events.py`).

---

## 3. Mandatory part (the project is rejected if any is missing)

| Requirement | Status |
|---|---|
| Frontend + backend + database, one-command Docker deployment | ✅ (`make up` starts the `frontend` service with the backend; gateway `location /` -> `frontend:3000`) |
| Several users at once, concurrent actions without corruption, real-time updates | ✅ (row locks and idempotency keys on the ledger and tiers, per-user queue limits, WebSocket fan-out) |
| Credentials in an ignored env file, `.env.example` provided | ✅ (`.env.docker` ignored, `.env.example` complete) |
| Clear database schema with relations | ✅ (`docs/ERD.md`, Alembic in every service) |
| Secure sign-up and login, hashed and salted passwords | ✅ (bcrypt, email verification, rate limits) |
| Input validation in the backend | ✅ (Pydantic on every route, 422 instead of 500 on bad input) |
| Input validation in the frontend | ✅ (every form has HTML limits mirroring the backend Pydantic limits; the Server Actions in `frontend/app/lib/actions/` check again) |
| HTTPS for every connection from outside | ✅ (TLS gateway on 8443; HTTP 8000 only redirects; the frontend's server-side calls use the gateway's internal listener on the Docker network) |
| Privacy Policy and Terms of Service pages | ✅ (`frontend/app/(legal)/privacy/page.tsx` and `terms/page.tsx`, linked from the footer on every page, `frontend/app/site-footer.tsx`) |
| README with the required sections | Not written yet |

---

## 4. Modules

### 4.1 Web

| # | Module | Type | Pts | Status | Evidence | Worked on by |
|---|---|---|---|---|---|---|
| W1 | Framework for frontend and backend | Major | 2 | ✅ | FastAPI in all three services; Next.js 16 (React 19) in `frontend/`. | TODO |
| W2 | ORM | Minor | 1 | ✅ | SQLAlchemy 2.0 async (`shared/database.py`), Alembic per service. | TODO |
| W3 | Real-time features | Major | 2 | ✅ | `core-service` WebSockets: per-channel authorization, reconnect-safe Redis relay across replicas, each message delivered once per socket, presence heartbeats, analysis/tier/decision events on `analysis:<id>`, notification pushes. Frontend: live notification feed and unread count (`(protected)/notifications/live-feed.tsx`, `use-live-unread.ts`), live analysis status (`(protected)/analyses/[id]/live-status.tsx`), live analytics refresh. | TODO |
| W4 | Public API | Major | 2 | ✅ | `/api/v1/public/*` with `X-API-Key`, per-key and per-user rate limits (429), GET/POST/PUT/DELETE, Swagger at `/docs/core/`; keys of deactivated owners stop working immediately. | TODO |
| W5 | Notification system for all create/update/delete actions | Minor | 1 | ✅ | Every create, update and delete in core notifies the actor (and affected users); auth and ai actions arrive as events (`shared/enums.py` lists all types). In-app rows, live WebSocket push by default, optional email. | TODO |
| W6 | File upload and management | Minor | 1 | ✅ | Type, size and content checks (a `.pdf` must really be a PDF), access-controlled S3 storage, download, inline preview, rename, delete with index clean-up. Frontend: `(protected)/research/documents/` (upload with progress bar, preview, download, delete, processing status). | TODO |
| W7 | Advanced search | Minor | 1 | ✅ | Notes: full-text search with filters, sort, pagination. Documents: filters (status, category, ticker, dates), sort, pagination. Frontend: `(protected)/research/page.tsx` and `(protected)/research/documents/page.tsx`, filter values kept in the URL. | TODO |
| W8 | Real-time collaborative features | Minor | 1 | ✅ | Yjs live editing of research notes: the Yjs document is the source of truth and is written back to the note text and search index; REST body edits get 409 during a session; organization viewers and guests are read-only; malformed updates are isolated in worker processes so they cannot crash the service. Frontend: `(protected)/research/[id]/collab-editor.tsx` (two users of one organization edit a shared note live). | TODO |

### 4.2 User management

| # | Module | Type | Pts | Status | Evidence | Worked on by |
|---|---|---|---|---|---|---|
| U1 | Standard user management | Major | 2 | ✅ | Profile update, avatar upload with a default avatar, friends with online status, profile data. Frontend: `(protected)/profile/`, `(protected)/friends/`. | TODO |
| U2 | Remote authentication (OAuth 2.0) | Minor | 1 | ✅ | Google and GitHub, with 2FA enforced on OAuth logins. Both OAuth apps are registered (2026-10-07) with the local and the cloud callback URLs; the credentials are in `.env.docker` (`GOOGLE_CLIENT_*`, `GITHUB_CLIENT_*`). Google's app is in testing mode, so only its listed test users can sign in with Google. Frontend: Google and GitHub buttons on the login and register pages (`(public)/oauth-buttons.tsx`), `(public)/oauth/complete/`. | TODO |
| U3 | Two-factor authentication | Minor | 1 | ✅ | TOTP, enforced on password and OAuth logins, lockout after repeated failures. | TODO |
| U4 | Advanced permissions system | Major | 2 | ✅ | System roles admin / moderator / user / guest (`shared/permissions.py`); admin user CRUD (`/api/v1/admin/users`), deactivate/reactivate/soft delete, site statistics; moderators hide/delete any note or comment and deactivate users; guests are read-only everywhere; `/me` returns `permissions` so the UI can adapt; a demotion or deactivation applies immediately to existing tokens. Frontend: `lib/me.ts` reads the permissions; `(protected)/admin/users/`, `(protected)/admin/moderation/`; guests see read-only views. | TODO |
| U5 | Organization system | Major | 2 | ✅ | Create/edit/delete organizations, invite, join, leave, remove, change roles; OWNER/ADMIN/MEMBER/VIEWER enforced (viewers are read-only in shared notes and documents); shared portfolios. Frontend: `(protected)/organizations/` (edit, delete, leave, members, invitations). | TODO |

Not claimed: "User activity analytics" (Minor) — no dedicated activity
dashboard.

### 4.3 Artificial intelligence

| # | Module | Type | Pts | Status | Evidence | Worked on by |
|---|---|---|---|---|---|---|
| A1 | Complete RAG system | Major | 2 | ✅ | Ingestion of filings, news, macro research and user uploads; category-aware chunking; hybrid BM25 + vector + RRF retrieval with per-user visibility; users ask questions at `POST /api/v1/rag/ask` and get answers grounded in cited sources; the analysis agents also retrieve from it. | TODO |
| A2 | Complete LLM system interface | Major | 2 | ✅ | Text generation from user input (`/rag/ask`, `mode=draft` writes a note paragraph), streaming over SSE (`/rag/ask/stream`), error events instead of broken streams, per-user rate limits, multi-provider router with fallbacks and timeouts. | TODO |
| A3 | Sentiment analysis | Minor | 1 | ✅ | Sentiment of user-generated content: every research note and comment is scored (label and score) after it is saved; `GET /api/v1/research/sentiment/summary` aggregates it per ticker and per day. | TODO |

**RAG data on a fresh clone.** The indexed corpus is data, not code: the
seed documents (`rag_seed_documents/`, third-party texts) and the backups
are not committed. A fresh clone therefore starts with an empty Qdrant
collection. Before showing A1, copy a backup folder into `backups/` and
run `make restore BACKUP=<YYYY-MM-DD_HHMM>` (dry run), then the same
command with `CONFIRM=yes`; this brings back the database, the about
1,178 indexed chunks and the stored documents
(`docs/DISASTER_RECOVERY.md` section 4). Without a backup, copy
`rag_seed_documents/` into the clone and upload it with
`services/ai-service/scripts/upload_seed_documents.py`
(`docs/handbook/17-ai-rag.md` section 3.14); the daily `rag-ingest` job
then adds filings and news.

Not claimed: "Recommendation system using machine learning" (Major) —
recommendations come from multi-agent LLM reasoning, not a trained
model (claimed as C1 instead).

### 4.4 DevOps

| # | Module | Type | Pts | Status | Evidence | Worked on by |
|---|---|---|---|---|---|---|
| D1 | Monitoring with Prometheus and Grafana | Major | 2 | ✅ | `/metrics` on every service plus postgres/redis exporters, custom metrics for analyses, queue and LLM errors, two provisioned dashboards, 12 alert rules (9 of them with promtool unit tests), Alertmanager email, Grafana behind the gateway with a required admin password and no anonymous access. | TODO |
| D2 | Backend as microservices | Major | 2 | ✅ | Three services with their own schemas, images and migrations; REST with typed clients and Redis events; internal routes protected by a service token. | TODO |
| D3 | Health check, status page, backups and disaster recovery | Minor | 1 | ✅ | `/health` returns 503 when a dependency is down, `/status` page, daily backups of Postgres, Qdrant, object storage and avatars with checksums and 7-day retention, dry-run-first restore, `docs/DISASTER_RECOVERY.md`. | TODO |

Not claimed: ELK log management — logs are structured JSON collected by
Vector into files, not an Elasticsearch/Logstash/Kibana stack.

### 4.5 Data and analytics

| # | Module | Type | Pts | Status | Evidence | Worked on by |
|---|---|---|---|---|---|---|
| N1 | Advanced analytics dashboard | Major | 2 | ✅ | Overview and time series with date ranges and filters (`/api/v1/analytics/overview`, `/timeseries`), CSV export of analyses/recommendations/decisions, portfolio history and transaction CSVs. Frontend: `(protected)/analytics/` (charts, live refresh from WebSocket events, CSV export, "Save as PDF" through the print dialog). | TODO |

Not claimed: "Data export and import" (Minor) — export exists, import
does not. GDPR (Minor) — not implemented.

### 4.6 Modules of choice

| # | Module | Type | Pts | Status | Worked on by |
|---|---|---|---|---|---|
| C1 | Multi-agent investment debate and reflection engine | Major | 2 | ✅ | TODO |
| C2 | Decision tracking and backtesting engine | Minor | 1 | ✅ | TODO |
| C3 | CI/CD pipeline | Minor | 1 | ✅ | TODO |

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
escalating cooldowns, fallback models). *Why Major:* comparable in scope
to the official ML recommendation Major, implemented as auditable
multi-agent reasoning.

**C2 (Minor).** Decision tracking (one decision per user and
recommendation, accept/decline of trade tiers that the scheduler then
executes into the ledger), three backtests (recommendation replay,
single-ticker triggered tiers, whole-portfolio time-weighted return with
external flows handled correctly), review workflow scoring AI accuracy
after 30 days and user decisions at 7, 30 and 90 days. A live portfolio evaluation is not
part of it and is not claimed.

**C3 (Minor).** A CI/CD pipeline in GitHub Actions
(`.github/workflows/`) that takes every change from a pull request to the
running cloud host:

- *Per-service CI* (`auth-service-ci.yml`, `core-service-ci.yml`,
  `ai-service-ci.yml`): path filters run only the workflow of the service
  a change touches (plus `shared/`, the test helpers and the repository
  files its tests read), on pull requests and on pushes to `main`. Each
  runs flake8, `mypy --strict` and the service's test suite (real
  PostgreSQL and Redis through testcontainers), then builds the
  service's production image. Any error fails the job.
- *Frontend CI* (`frontend-ci.yml`): lint, type check and production
  build of the Next.js app.
- *Stack smoke CI* (`integration-ci.yml`): configuration checks (compose
  files, shell syntax, Prometheus alert rules with their promtool unit
  tests, the status page's CSP hashes); on `main` it builds every
  production image, starts the whole stack, waits until all services are
  healthy and checks the gateway (HTTP redirect, `/status`, blocked
  `/internal` and `/metrics`, the frontend's `/login` and `/healthz`).
- *Continuous deployment* (`deploy.yml`): runs only after a successful
  stack smoke run for a push to `main`. It opens an SSH connection whose
  key can only run one forced command, `scripts/deploy.sh`, on the cloud
  host; that script fast-forwards the checkout, rebuilds, restarts what
  changed and waits until every service is healthy, and fails otherwise.
  It keeps the scheduler in the state it had before the deploy.
  Setup and history: `docs/DEPLOYMENT.md`.

*Why it is not trivial:* it is not a single "run the tests" workflow. A
monorepo with three Python services and a frontend needs path filters
that are exact (each filter lists every file its tests read, so a change
cannot skip the checks that cover it); the smoke job tests the
production images and the real gateway, not the development setup; and
deployment is gated on that smoke run, uses a restricted SSH key with a
forced command, and only reports success when the new version is
healthy, so a broken `main` is never deployed and a failed deploy is
visible in the Actions history.

---

## 5. Point total

| Category | Shown through the API | Shown through the frontend only |
|---|---|---|
| Web | 5 (W2, W4, W5, W7) | 6 (W1, W3, W6, W8) |
| User management | 4 (U2, U3, U5) | 4 (U1, U4) |
| Artificial intelligence | 5 (A1, A2, A3) | 0 |
| DevOps | 5 (D1, D2, D3) | 0 |
| Data and analytics | 0 | 2 (N1) |
| Modules of choice | 4 (C1, C2, C3) | 0 |
| **Total** | **23** | **12** |

Every row was checked against the module list of the subject (v21.1):
each module above exists there with the type shown. The minimum of 14
points is covered by the API-demonstrable modules alone. With the
frontend modules the claim is 35 points. The subject counts at most 5
bonus points above the required 14 (each extra module must be fully
working and justified in the README), so the extra modules are a margin
for modules that fail at the evaluation, not a higher grade. Evaluators
count only modules that fully work, so the frontend screens named in
section 4 must work at the evaluation. W6 (client-side validation,
progress and preview) and U4 (different views per role) are counted in
the frontend column because their subject requirements include the user
interface. C3 is the module of choice most likely to be questioned as
standard tooling; the total stays above 14 without it.

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

Items marked (frontend) are shown in the frontend; everything else can also be shown
with Swagger (https://localhost:8443/docs/{auth,core,ai}/), curl or a
WebSocket client.

- [ ] Fresh clone into an empty folder (not named `alphamind`, so its volumes are separate; `make down` the existing stack first, the ports are the same); copy the saved `.env.docker` into it (or `cp .env.example .env.docker` and fill the secrets); `make up` (creates the certificate and starts everything with one command)
- [ ] https://localhost:8443/status all green; http://localhost:8000 redirects to HTTPS
- [ ] Copy a backup folder into `backups/`, then `make restore BACKUP=<stamp>` (dry run) and `make restore BACKUP=<stamp> CONFIRM=yes`: the fresh clone has an empty RAG index and no data until this step (see the RAG note in section 4.3)
- [ ] Register, verify email (MailHog), log in, enable 2FA, log in with 2FA; OAuth login with Google or GitHub
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
- [ ] GitHub Actions history: a pull request that ran only the changed service's workflow, a stack smoke run on `main` and the deploy run it triggered

---

# END OF MODULE_MAPPING.md
