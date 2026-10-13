# AlphaMind — MODULE_MAPPING.md

This document maps the code to the modules of the ft_transcendence
subject (v21.1, chapter IV).

Scoring: a Major module is worth 2 points and a Minor module 1 point;
only fully working modules count at evaluation. Each row names the
module as the subject lists it and the evidence: the backend code, the
pages of the frontend (the Next.js app in `frontend/`, served by the
gateway at `/`; page paths are under `frontend/app/`, and route groups in
parentheses such as `(protected)` are not part of the URL) and, where a
requirement has no page, how to show it (Swagger at
`/docs/{auth,core,ai}/`, Grafana, GitHub Actions).

---

## 1. Architecture in one picture

```
Browser / scripts / external API clients
        │  HTTPS 8443 (HTTP 8000 only redirects), WSS for WebSockets
        ▼
   nginx gateway ── / (frontend), /status page, /grafana/ (login), rate limit on /api/v1/auth
        │
   ┌────┴───────────────┬─────────────────────┬──────────────────┐
   ▼                    ▼                     ▼                  ▼
frontend           auth-service          core-service        ai-service
Next.js 16         users, roles,         portfolios + ledger, 9-agent LangGraph pipeline,
(React 19)         admin, orgs,          research + Yjs,      debate/Judge/Reflection,
server-rendered    friends, 2FA,         notifications,       RAG + ask/stream, LLM router,
pages              OAuth                 public API,          decisions, backtests,
                                         WebSockets           analytics, sentiment
                        │                     │                  │
                        └── REST (/internal/*, X-Internal-Token) + Redis pub/sub and streams ──┘
              │                  │                 │
        Postgres (auth/core/ai schemas)   Redis   Qdrant (ai only)
        S3-compatible object storage (SeaweedFS)
        Prometheus + exporters, Alertmanager, Grafana; scheduler (cron jobs, backups)
```

The frontend's server-side code calls the backend through the gateway's
internal listener on the Docker network; browsers reach everything only
through HTTPS. Services never import each other's code. They talk over
REST (`services/*/clients/*.py`, service-to-service routes under
`/internal/*` protected by a shared token) and over Redis events
(`shared/events.py`).

---

## 2. Mandatory part

| Requirement | Evidence |
|---|---|
| Frontend + backend + database, one-command Docker deployment | `make up` builds and starts the whole stack (frontend, three services, Postgres, Redis, Qdrant, object storage, monitoring, scheduler); the gateway serves the frontend at `/` |
| Several users at once, concurrent actions without corruption, real-time updates | Row locks and idempotency keys on the ledger and trade tiers, per-user queue limits, WebSocket fan-out to every connected client, Yjs for concurrent note editing |
| Credentials in an ignored env file, `.env.example` provided | `.env.docker` is ignored by git; `.env.example` lists every variable |
| Clear database schema with relations | `docs/ERD.md`; Alembic migrations in every service |
| Secure sign-up and login, hashed and salted passwords | bcrypt, email verification, rate limits on the auth routes |
| Every form and input validated in the frontend and the backend | Every form, and the live note editor, has HTML limits that mirror the backend limits and the Server Actions in `frontend/app/lib/actions/` check again; every route validates with Pydantic and answers 422 instead of 500 on bad input |
| HTTPS for every connection from outside | TLS gateway on 8443; HTTP 8000 only redirects; WebSockets use WSS |
| Privacy Policy and Terms of Service pages | `(legal)/privacy/page.tsx` and `(legal)/terms/page.tsx`, linked from the footer of every page (`site-footer.tsx`) |
| Responsive frontend, CSS framework | Tailwind CSS 4; layouts for desktop and mobile, with a menu for small screens |

---

## 3. Modules

### 3.1 Web (IV.1)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| W1 | Use a framework for both the frontend and backend | Major | 2 | Next.js 16 (React 19) for the frontend; FastAPI for all three backend services. |
| W2 | Use an ORM for the database | Minor | 1 | SQLAlchemy 2.0 async models and repositories (`shared/database.py`, `services/*/domains/*/model.py`), Alembic migrations per service. |
| W3 | Real-time features using WebSockets | Major | 2 | `core-service` WebSockets at `/ws/{channel}` over WSS: per-channel authorization, a Redis relay so every replica delivers each message once per socket, presence heartbeats, close codes for expired sessions and lost access, and client reconnection with backoff. Shown in the app: the live notification feed and unread badge (`(protected)/notifications/live-feed.tsx`, `use-live-unread.ts`), the live status of a running analysis (`(protected)/analyses/[id]/live-status.tsx`) and the live refresh of the analytics page. |
| W4 | Public API with a secured API key, rate limiting, documentation and at least 5 endpoints | Major | 2 | Five endpoints under `/api/v1/public/*` with GET, POST, PUT and DELETE (`services/core-service/api/public.py`); `X-API-Key` checked on every call, with an "Authorize" field in Swagger at `/docs/core/`; per-key and per-user rate limits (429); the keys of a deactivated owner stop working at once. |
| W5 | A complete notification system for all creation, update and deletion actions | Minor | 1 | Every create, update and delete action notifies the user who made it, and the users it affects: portfolios and the ledger, notes, comments and documents, organizations, members and invitations, friend requests and blocks (the blocked user is never told), analyses, decisions, trade tiers and backtests. Auth and ai actions reach core-service as events (`shared/events.py`, types in `shared/enums.py`). In-app notifications at `/notifications` with a live unread count, optional email. |
| W6 | File upload and management system | Minor | 1 | Documents (PDF, text, Markdown) at `/research/documents`: type and size checked in the browser and on the server, where a `.pdf` must really be a PDF; upload progress bar; inline preview; download; delete with search-index clean-up; access control by owner and organization on S3-compatible storage. Images (PNG, JPEG, WebP, GIF) as avatars at `/profile`: type and size checked in the browser and on the server, a default avatar when none is set. |
| W7 | Advanced search with filters, sorting and pagination | Minor | 1 | Notes at `/research`: full-text search, tag, organization and owner filters, sort, pagination. Documents at `/research/documents`: ticker, category, status and date filters, sort, pagination. Filter values are kept in the URL. |
| W8 | Real-time collaborative features | Minor | 1 | Live editing of research notes with Yjs (`(protected)/research/[id]/collab-editor.tsx`, `services/core-service/yjs_relay.py`): two users of one organization edit a shared note at the same time; the Yjs document is written back to the note text and search index; REST body edits get 409 during a session; organization viewers and guests see the note read-only (a user downgraded while the editor is open gets a reload prompt and then the read-only editor); malformed updates are isolated in worker processes so they cannot crash the service. |
| W9 | Server-Side Rendering (SSR) | Minor | 1 | The pages are React server components that fetch their data on the server (`backendFetch` in `frontend/app/lib/backend.ts`) and send finished HTML, so the first paint needs no client-side requests; client components are used only where a page is interactive (forms, live updates, the editor). Page titles and descriptions come from the `metadata` exports (`app/layout.tsx`, legal pages). |

### 3.2 Accessibility and Internationalization (IV.2)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| B1 | Support for additional browsers | Minor | 1 | Besides Google Chrome, the latest stable Microsoft Edge and Safari are supported. Every page and feature was tested in each of them: registration and email verification, password, GitHub OAuth and 2FA logins, profile and avatar, friends and online status, organizations, notes with live editing between two browsers (Chrome and Edge), documents, search, the research assistant, analyses with their live status, analytics, notifications, admin and moderation pages, the legal pages and the narrow-window menu. The console stays clean in all three browsers, and the pages look and behave the same, because the layout uses only standard CSS (Tailwind CSS 4) and standard browser APIs (WebSocket, `fetch` with a streamed response body), with no browser-specific code. |

**Browser-specific limitations.** No feature differs between the three
browsers. Two points to know when testing locally:

- The local gateway uses a self-signed certificate, so each browser asks
  once to accept it for `https://localhost:8443` (Safari: "Show Details",
  then "visit this website"). The cloud host has a real certificate.
- Edge's *Issues* panel (not the console) can list informational
  navigation notices after the OAuth redirect chain; they come from the
  browser's own checks of the redirects, not from the application, and
  nothing fails.

### 3.3 User Management (IV.3)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| U1 | Standard user management and authentication | Major | 2 | Users update their profile and upload an avatar with a default avatar (`/profile`); add, accept and remove friends and see their online status (`/friends`); the profile page shows the user's information. |
| U2 | Remote authentication with OAuth 2.0 | Minor | 1 | Google and GitHub sign-in (`services/auth-service/domains/auth/`), with 2FA required on OAuth logins when it is enabled; buttons on the login and register pages (`(public)/oauth-buttons.tsx`, `(public)/oauth/complete/`). Both apps are registered with the local and the cloud callback URLs; the credentials come from `.env.docker`. |
| U3 | Complete 2FA system | Minor | 1 | TOTP set-up and confirmation at `/profile`, the second step on password and OAuth logins, lockout after repeated failures, a recent login required to enable it, secrets encrypted at rest. |
| U4 | Advanced permissions system | Major | 2 | System roles admin, moderator, user and guest (`shared/permissions.py`). Admins view, edit, deactivate, reactivate and delete users and change their roles (`/admin/users`); moderators hide and delete any note or comment and deactivate users (`/admin/moderation`); guests cannot create or change shared content. `/auth/me` returns the user's permissions and the pages show the actions each role may use. A role change or deactivation applies to existing sessions at once. |
| U5 | Organization system | Major | 2 | Create, edit and delete organizations; invite users by email, accept, leave, remove members and change their roles; roles owner, admin, member and viewer decide what each member may do; notes, documents and portfolios can be shared with an organization (`/organizations`, `/organizations/[id]`). |

### 3.4 Artificial Intelligence (IV.4)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| A1 | Complete RAG system | Major | 2 | A corpus of SEC filings, news, earnings calls, analyst and macro research and user uploads; category-aware chunking; hybrid BM25 + vector retrieval with reciprocal rank fusion and per-user visibility (`services/ai-service/ai/rag/`). Users ask questions at `/research/ask` and get answers grounded in cited sources; the analysis agents retrieve from the same index. |
| A2 | Complete LLM system interface | Major | 2 | Text generated from user input at `/research/ask` (answers, and draft note paragraphs); the answer streams token by token over SSE (`/api/v1/rag/ask/stream`); errors arrive as error events instead of a broken stream; per-user rate limits; a multi-provider router with fallback models and timeouts (`services/ai-service/ai/llm/`). |
| A3 | Sentiment analysis for user-generated content | Minor | 1 | Every research note and comment is scored (label and score) after it is saved, including notes written in the live editor; the score is shown next to each note and comment; `GET /api/v1/research/sentiment/summary` aggregates it per ticker and per day. |

**RAG data.** The indexed corpus is data, not code: the seed documents
(third-party texts) and the backups are not committed, so a fresh clone
starts with an empty index. Before showing A1, copy a backup folder into
`backups/` and run `make restore BACKUP=<YYYY-MM-DD_HHMM>` (dry run), then
the same command with `CONFIRM=yes`; this restores the database, the
indexed chunks and the stored documents (`docs/DISASTER_RECOVERY.md`).
Without a backup, upload `rag_seed_documents/` with
`services/ai-service/scripts/upload_seed_documents.py`
(`docs/handbook/17-ai-rag.md`); the daily `rag-ingest` job then adds
filings and news.

### 3.5 Devops (IV.7)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| D1 | Monitoring system with Prometheus and Grafana | Major | 2 | `/metrics` on every service plus Postgres and Redis exporters; custom metrics for analyses, the queue and LLM errors; two provisioned Grafana dashboards; 12 alert rules (9 with promtool unit tests) and Alertmanager email; Grafana behind the gateway with a required admin password and no anonymous access. |
| D2 | Backend as microservices | Major | 2 | Three services (auth, core, ai), each with a single responsibility, its own database schema, image and migrations; REST with typed clients and Redis events between them; internal routes protected by a service token. |
| D3 | Health check and status page system with automated backups and disaster recovery | Minor | 1 | `/health` on every service (503 when a dependency is down); the `/status` page; daily automated backups of Postgres, Qdrant, object storage and avatars with checksums and 7-day retention (made by the scheduler on the local stack; the cloud host keeps its scheduler stopped and makes no backups); a restore that runs as a dry run first; `docs/DISASTER_RECOVERY.md`. |

### 3.6 Data and Analytics (IV.8)

| # | Module | Type | Pts | Evidence |
|---|---|---|---|---|
| N1 | Advanced analytics dashboard with data visualization | Major | 2 | `/analytics`: interactive charts with tooltips (area chart over time, bar chart of recommended actions, donut chart of statuses); date ranges and filters; refresh from WebSocket events; CSV export and PDF through the print view. The data comes from `/api/v1/analytics/overview` and `/timeseries`; the API also exports portfolio history and transactions as CSV. |

---

## 4. Point total

| Category | Modules | Points |
|---|---|---|
| Web | W1, W3, W4 (Major); W2, W5, W6, W7, W8, W9 (Minor) | 12 |
| Accessibility and Internationalization | B1 (Minor) | 1 |
| User Management | U1, U4, U5 (Major); U2, U3 (Minor) | 8 |
| Artificial Intelligence | A1, A2 (Major); A3 (Minor) | 5 |
| Devops | D1, D2 (Major); D3 (Minor) | 5 |
| Data and Analytics | N1 (Major) | 2 |
| **Total** | 11 Major, 11 Minor | **33** |

---

## 5. Subject modules not chosen

| Module | Reason |
|---|---|
| Separate frontend or backend framework (Minor) | Covered by W1 |
| User interaction with chat (Major), advanced chat (Minor) | No chat |
| PWA (Minor), custom design system (Minor) | Not implemented |
| Accessibility WCAG 2.1 AA (Major), multiple languages (Minor), RTL (Minor) | Not implemented |
| Game, statistics and match history, AI opponent, tournament, gamification and other gaming modules | No game |
| Recommendation system using machine learning (Major) | Recommendations come from multi-agent LLM reasoning, not a trained model |
| Content moderation AI, voice, image recognition (Minor) | Moderation is done by moderators |
| User activity analytics dashboard (Minor) | No activity dashboard |
| WAF/ModSecurity + Vault (Major) | Not implemented |
| ELK log management (Major) | Logs are structured JSON collected by Vector into files |
| Data export and import (Minor), GDPR (Minor) | Export only; no import or GDPR flows |
| Blockchain modules | Not implemented |
| Modules of choice (Major, Minor) | Not claimed |

---

## 6. Evaluation demo checklist

- [ ] Fresh clone into an empty folder (not named `alphamind`, so its volumes are separate; `make down` the existing stack first, the ports are the same); copy the saved `.env.docker` into it (or `cp .env.example .env.docker` and fill the secrets); `make up` (creates the certificate and starts everything with one command)
- [ ] https://localhost:8443/status all green; http://localhost:8000 redirects to HTTPS; Chrome console clean
- [ ] Copy a backup folder into `backups/`, then `make restore BACKUP=<stamp>` (dry run) and `make restore BACKUP=<stamp> CONFIRM=yes` (the RAG index and the data come from this step)
- [ ] Footer links to the Privacy Policy and Terms of Service; the layout on a phone-sized window
- [ ] Register with invalid inputs (empty, wrong format, script tags), verify the email (MailHog), log in, enable 2FA, log in with 2FA; OAuth login with GitHub or Google
- [ ] Profile and avatar, friends and online status in two browsers
- [ ] Admin users page: change a role, make someone a guest and show what the guest cannot do, deactivate a user and show their session ends; moderation page hides a comment
- [ ] Organization: create, invite, accept, change a member to viewer, share a note and edit it live in two browsers
- [ ] Upload a document with the progress bar, preview, delete; avatar upload with a wrong type or size
- [ ] Search, filter, sort and page notes and documents
- [ ] Ask a question at `/research/ask` and watch the streamed, cited answer
- [ ] Write a note and a comment and show their sentiment
- [ ] Run an analysis and follow its live status; open the agents, debate, Judge and Reflection; accept a trade tier and see its notification
- [ ] Analytics page: charts, date range, filters, CSV export
- [ ] Notifications for create, update and delete actions
- [ ] Public API in Swagger with an API key, then trigger a 429
- [ ] Grafana dashboards behind login; stop an exporter and show the alert email in MailHog
- [ ] `make backup` and a dry-run `make restore`
- [ ] The same pages in Microsoft Edge and Safari, console clean in each
- [ ] GitHub Actions history: a pull request that ran only the changed service's workflow, a stack smoke run on `main` and the deploy run that followed

---

# END OF MODULE_MAPPING.md
