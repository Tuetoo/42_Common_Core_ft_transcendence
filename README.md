*This project has been created as part of the 42 curriculum by isahmed, yi-yang, jiezhang.*

# AlphaMind

## Description

AlphaMind is a multi-agent AI research platform for US stocks, for
individual investors and for small groups that research together, such
as an investment club or a small team. It works like a research analyst:
instead of one unexplained answer, every analysis follows a fixed process
and every step is stored so it can be inspected later. Groups share
notes, documents and portfolios in an organization, edit notes together
in real time and see each other's activity as it happens.

For a ticker, up to nine specialist agents collect evidence (prices,
technical indicators, fundamentals, SEC filings, news, sentiment, macro
data, risk and portfolio fit). A Bull and a Bear argue over it, a Judge
decides BUY, HOLD or SELL with a confidence, and a Reflection step checks
the debate's claims against the evidence. For a portfolio, the result
includes concrete price tiers that the owner can accept; a daily job then
executes the tiers the market price reached, and later jobs measure
whether the call was right.

Around this core, AlphaMind is a complete multi-user web application:
accounts with 2FA and OAuth, roles, organizations, friends with online
status, portfolios with a transaction ledger, research notes with live
collaborative editing, uploaded research documents that become searchable
(RAG), a research assistant that answers with citations, notifications
in real time, a public API, analytics, monitoring and backups.

A running copy is deployed at https://alphamind-research.duckdns.org:8443
(status page: https://alphamind-research.duckdns.org:8443/status).

### Key features

- **Analyses**: multi-agent analysis with debate, Judge and Reflection;
  live status while it runs; recommendations with price tiers that the
  user accepts or declines.
- **Research workspace**: notes with tags, full-text search, comments and
  live collaborative editing; document upload (PDF, Markdown, text) with
  preview; sentiment scores on notes and comments.
- **Research assistant**: questions answered from the document library,
  streamed token by token, with cited sources.
- **Portfolios**: transaction ledger, holdings, cash, daily values,
  watchlists, and backtests of a portfolio against a benchmark.
- **Users and teams**: registration with email verification, 2FA,
  Google and GitHub login, profile and avatar, friends and online
  status, organizations with member roles, admin and moderator pages.
- **Real time**: notifications, unread counts, analysis status and
  analytics refresh over WebSockets.
- **Analytics**: interactive charts, date ranges and filters, CSV export
  and a print view for PDF.
- **Public API**: five endpoints with API keys, rate limits and Swagger
  documentation.
- **Operations**: one-command Docker deployment, HTTPS gateway, status
  page, Prometheus and Grafana with alerting, daily backups with a
  verified restore, CI/CD to a cloud host.

### Architecture

```
Browser ──HTTPS 8443──> nginx gateway ──> frontend (Next.js)
                             │
                             ├──> auth-service  (users, roles, organizations, friends, 2FA, OAuth)
                             ├──> core-service  (portfolios, research, documents, notifications,
                             │                   WebSockets, public API)
                             └──> ai-service    (analyses, recommendations, RAG, analytics)

Data: PostgreSQL (schemas auth, core, ai) · Redis · Qdrant · S3-compatible object storage
Around it: scheduler (cron jobs, backups) · Prometheus, Alertmanager, Grafana · MailHog
```

- **Frontend.** A Next.js app. Pages are rendered on the frontend
  server, which calls the backend for the data; the browser receives
  finished HTML and only talks to the gateway.
- **Backend.** Three FastAPI services, each responsible for one area and
  owning its own database schema. They call each other over REST
  (internal routes protected by a service token) and publish events
  through Redis, for example to create notifications.
- **Database.** PostgreSQL with one schema per service; Redis for events,
  rate limits and live user state; Qdrant for the vector index of the
  research library; S3-compatible object storage for uploaded files.
- **Gateway.** nginx terminates TLS, sends pages to the frontend and API
  calls to the services, applies rate limits on login routes, serves the
  status page and Grafana, and blocks internal routes and metrics from
  outside.

AlphaMind is a research and learning tool. It does not trade real money
and its output is not financial advice.

## Instructions

### Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Docker with the Compose plugin | Docker Desktop 4.x or Docker Engine 24+ with `docker compose` v2 | Runs every container |
| `make` | any | The short commands in the `Makefile` |
| `openssl` | any (LibreSSL on macOS works) | Creates the local TLS certificate |
| `bash`, `curl`, `python3` | system versions | Operator scripts in `scripts/` |
| A browser | latest Google Chrome, Microsoft Edge or Safari | The web application |
| `uv` (optional) | any | Running the tests and linters on the host (Python 3.11) |
| Node.js (optional) | 20.9 or newer (the image uses 24), with `npm` | Installing the frontend packages on the host, for the editor and the frontend checks |

The stack runs 17 containers and uses about 2 GB of memory when idle; give Docker at least 4 GB so analyses and image builds have room.

### Configuration (`.env.docker`)

All credentials live in a local `.env.docker` file that git ignores.
`.env.example` lists every variable with a comment.

```sh
cp .env.example .env.docker
```

Then fill in at least:

- `GRAFANA_ADMIN_PASSWORD`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (required);
- `JWT_SECRET` (at least 32 random characters), `INTERNAL_API_TOKEN`
  (for example `openssl rand -hex 32`), `TOTP_ENCRYPTION_KEY` (the
  command to generate it is in `.env.example`);
- an LLM key for `LLM_DEFAULT_PROVIDER` (`GOOGLE_API_KEY` in the
  example) and, optionally, the market-data keys (`FRED_API_KEY`,
  `NEWSAPI_API_KEY`, `TAVILY_API_KEY`, `SEC_EDGAR_USER_AGENT`);
- optionally `GOOGLE_CLIENT_ID`/`_SECRET` and `GITHUB_CLIENT_ID`/`_SECRET`
  for OAuth login, with the callback
  `https://localhost:8443/api/v1/auth/oauth/<google|github>/callback`
  registered at each provider.

### Run

```sh
make up
```

`make up` creates the self-signed TLS certificate if it is missing,
builds every image and starts all 17 containers; the data stores, the
three API services and the frontend have health checks, and the gateway
starts only after the three API services report healthy. Each service
runs its own database migrations when it starts, and the first start
creates the database schemas, the storage bucket and the vector
collection. `make up` stays in the foreground and shows the logs; run
the next commands in a second terminal. Then open:

| Address | What |
|---|---|
| https://localhost:8443 | The application (accept the self-signed certificate once) |
| https://localhost:8443/status | Status page of every service |
| https://localhost:8443/docs/auth/, `/docs/core/`, `/docs/ai/` | Swagger documentation of each service |
| https://localhost:8443/grafana/ | Grafana (user `admin`, password from `.env.docker`) |
| http://127.0.0.1:8025 | MailHog: verification, invitation and alert emails |
| http://127.0.0.1:9090 | Prometheus: scrape targets (`/targets`) and alert rules with their state (`/alerts`) |
| http://127.0.0.1:9093 | Alertmanager: alerts that are firing and being sent by email |

HTTP on port 8000 only redirects to HTTPS.

The only manual step before `make up` is the local `.env.docker`, which holds
the secrets and is never committed.

### First steps

1. Load the data. A fresh clone starts with empty databases: users, the
   research library and the analysis history are data, not code, and
   the backups are not in git. Copy a backup folder into `backups/`,
   then run `make restore BACKUP=<YYYY-MM-DD_HHMM>` as a dry run and
   again with `CONFIRM=yes`, the same procedure used for disaster
   recovery (`docs/DISASTER_RECOVERY.md`). A restore replaces every
   account, so do it before you register.
2. Register at https://localhost:8443/register and open the verification
   link in MailHog, or sign in with an account from the backup.
3. Give the first account the admin role:
   ```sh
   docker compose --env-file .env.docker exec auth-service python -m scripts.set_system_role --email <email> --role admin
   ```
4. Without a backup, fill the research library by uploading the seed
   documents instead (`docs/handbook/17-ai-rag.md`); the seed documents
   are not in git either, so first copy the seed folder into
   `rag_seed_documents/`.

### Code editor setup (optional)

The containers install their own dependencies, so `make up` does not need
anything installed on the host. To open the code in an editor such as
VS Code without import errors, install the packages on the host once,
from the repository root:

```sh
uv sync
npm ci --prefix frontend
```

`uv sync` creates `.venv/` with the Python packages (choose
`.venv/bin/python` as the editor's interpreter) and `npm ci --prefix
frontend` creates `frontend/node_modules/`. Both folders are ignored by
git. In VS Code, the Tailwind CSS IntelliSense extension recognises the
Tailwind rules in `frontend/app/globals.css`.

### Everyday commands

| Command | What it does |
|---|---|
| `make down` | Stop the stack (data volumes stay) |
| `make logs` | Follow every container's log |
| `make backup` | Back up the database, vectors, stored files and avatars now |
| `make restore BACKUP=<stamp>` | Check a backup (dry run); add `CONFIRM=yes` to restore |
| `make test` | Run the three services' test suites (needs `uv` and a running Docker) |
| `make lint` / `make mypy` | flake8 and `mypy --strict` |

The full operations guide is `docs/handbook/02-operations.md`; the cloud
deployment is described in `docs/DEPLOYMENT.md`.

## Team Information

| Member | Role(s) | Responsibilities |
|---|---|---|
| **isahmed** | Tech Lead (infrastructure), Developer | The infrastructure in `infra/`: the HTTPS gateway, object storage, the scheduler, backups and restore, monitoring and alerting |
| **yi-yang** | Tech Lead (frontend), Developer | The web application in `frontend/`: pages, components, styling, the session handling on the frontend server, real-time and collaborative editing in the browser, the Privacy Policy and Terms of Service pages |
| **jiezhang** | Product Owner, Project Manager, Tech Lead (backend), Developer | Product vision and module choice; backlog and priorities; planning and progress tracking; architecture of the backend; the three backend services (auth, core, ai), the database, the AI pipeline, tests, CI/CD and the cloud deployment; review and integration of all changes |

## Project Management

- **Organization of the work.** The work was split by area, with one
  lead per area: backend and AI, infrastructure, frontend. The backend's
  API contract (`docs/API_SPEC.md` and Swagger) was the interface
  between the areas, so they could be built in parallel. Only one
  member worked on the project full time and the team worked across
  different time zones, so there were no fixed meetings: progress and
  blockers were discussed in calls arranged when needed, and in person
  when possible.
- **Tools.** GitHub Issues and a GitHub Project board (Backlog, Ready,
  In progress, In review, Done) tracked the tasks, each with an
  assignee. Changes reached `main` mainly through pull requests, checked
  by the CI workflows.
- **Communication.** Slack and WhatsApp for day-to-day messages, calls
  for progress updates, and meetings in person to plan the work and solve
  problems together.
- **Git.** Each member committed their own work. The full history is in
  the repository (`git log`, or Insights → Contributors on GitHub).

## Technical Stack

| Area | Technologies | Why |
|---|---|---|
| Frontend | Next.js 16 (App Router, React 19, TypeScript), Tailwind CSS 4, Yjs, Recharts (charts) | Server-rendered pages and Server Actions keep the refresh token in an httpOnly cookie on the frontend server; Tailwind gives one consistent, responsive style; Yjs merges concurrent edits |
| Backend | Python 3.11, FastAPI, Pydantic, SQLAlchemy 2.0 (async), Alembic | Typed request validation and automatic OpenAPI documentation; one framework for three services |
| AI | LangGraph, a multi-provider LLM router (Google Gemini by default; OpenAI, Anthropic and Mistral adapters), Google embeddings, BM25 + vector hybrid retrieval | A cyclic agent graph with retries and failure states; fallback models when a provider is overloaded |
| Database | PostgreSQL 16 | Relational data with foreign keys, row locks for the ledger and the trade tiers, full-text search, JSONB for agent evidence; one schema per service |
| Other data stores | Redis 7 (events, pub/sub, rate limits, live roles), Qdrant (vectors), SeaweedFS (S3-compatible object storage) | Events between services, the vector index for RAG, stored documents and backups |
| Infrastructure | Docker Compose, nginx (TLS gateway), Prometheus, Alertmanager, Grafana, exporters, Vector (logs), MailHog, a cron scheduler | One-command deployment, HTTPS everywhere, monitoring with alerts, automated jobs |
| CI/CD | GitHub Actions, an SSH forced-command deploy to an AWS virtual machine | Every change checked before it reaches the cloud host |
| Quality | pytest with real PostgreSQL and Redis (testcontainers), flake8, `mypy --strict`, ESLint, TypeScript checks | The same checks locally and in CI |

### Styling

The frontend uses Tailwind CSS 4. `frontend/app/globals.css` starts with
`@import "tailwindcss";`, and its `@theme` block registers the project's
colours and fonts with Tailwind, so every page uses one palette. Layout,
spacing and responsive behaviour are utility classes written directly in
`className`: for example `flex flex-col md:flex-row` in
`app/(protected)/layout.tsx` stacks the page on phones and places the
sidebar beside the content on screens 768 px and wider, and
`hidden md:flex` in `sidebar-nav.tsx` hides the sidebar on phones, where
a Menu button takes its place. Elements that repeat everywhere, such as
buttons and form fields, share a few component classes (`btn`, `field`)
defined in `globals.css`.

## Technical Decisions

- **Why FastAPI.** Native async I/O for the many slow LLM and
  market-data calls, with WebSockets and streaming in the same framework;
  typed request validation with Pydantic, with models shared across
  services; automatic OpenAPI documentation, which is also the public API
  documentation; a small app per service with dependency injection for
  login and permission checks, which suits microservices; and Python, the
  language of the AI stack (LangGraph, the Gemini SDK), so the AI code
  runs inside ai-service.
- **Why Next.js.** Server components fetch data on the server, and
  Server Actions keep the refresh token in an httpOnly cookie on the
  frontend server, out of reach of browser JavaScript.
- **Why PostgreSQL.** Relational data with real relations, row locks for
  the ledger and the trade tiers, built-in full-text search, and JSONB
  for agent evidence.
- **Why microservices.** A slow or failing AI job must not take down
  login or the portfolio ledger; each service can be rebuilt, tested and
  scaled on its own, and owns its data.
- **How live updates stay private.** Every WebSocket channel is
  authorized when it is opened, and a change of role or membership
  re-checks the open sockets and closes them (code 4401 when the login
  expired or the account was deactivated, 4403 when access was revoked).
- **How the services talk.** REST with typed clients when an answer is
  needed now; Redis events for things that happened, with an outbox in
  core-service so an event is not lost if Redis is briefly down.
- **Why LangGraph for the analysis.** The analysis is a graph with
  branches, retries and a loop back from Reflection to the agents;
  LangGraph keeps that state explicit and lets every step be stored.
- **Biggest technical challenge.** LLM providers rate-limit and overload
  during long analyses. The LLM layer pools several keys, paces calls,
  cools down a key after errors and falls back to other models, and a
  failed step is recorded as failed instead of being filled with a
  made-up answer.
- **Trade-offs.** Only US stocks; only the Gemini free tier is
  configured; the cloud host runs no scheduled jobs so that it does not
  share the LLM quota with the local stack (see Known Limitations).

## Database Schema

PostgreSQL holds one schema per service. A service only reads and writes
its own schema; references to another service's rows are plain UUIDs,
checked through that service's API. The full schema with every column
is in `docs/ERD.md`.

| Schema | Tables | Key relations |
|---|---|---|
| `auth` | `users`, `user_profiles`, `user_settings`, `user_sessions`, `oauth_accounts`, `two_factor_secrets`, `connections`, `organizations`, `organization_members`, `organization_invitations` | A user has one profile and settings row, many sessions (refresh-token rotation chains) and OAuth links; `connections` links two users (pending, accepted, blocked); an organization has an owner and members with a role |
| `core` | `portfolios`, `holdings`, `transactions`, `portfolio_snapshots`, `watchlists`, `watchlist_items`, `research_notes`, `research_comments`, `research_tags`, `note_tags`, `documents`, `notifications`, `api_keys`, `event_outbox` | A portfolio has holdings, a transaction ledger and daily snapshots; a note has comments and tags; notes, documents and portfolios belong to a user and optionally an organization |
| `ai` | `analyses`, `recommendations`, `recommendation_tiers`, `agent_runs`, `agent_results`, `debate_sessions`, `debate_arguments`, `reflections`, `investment_decisions`, `decision_outcomes`, `decision_reviews`, `document_chunks`, `embedding_jobs`, `kg_entities`, `kg_relations`, `episodic_memories`, `agent_memories`, `agents`, `agent_metrics`, `agent_reports`, `recommendation_metrics`, `backtests`, `backtest_metrics`, `benchmarks`, `benchmark_results`, `price_bars`, `instruments`, `ticker_names`, `filing_records` (full list with columns in `docs/ERD.md`) | An analysis has agent runs and results, a debate, reflections and recommendations; a recommendation has price tiers; a decision links a user to a recommendation and has measured outcomes; a backtest has metrics and benchmark results |

Key field types: UUID primary keys, `timestamptz` times, `numeric` for
money and quantities, `jsonb` for agent evidence, `tsvector` for note
search. Vectors live in Qdrant and files in object storage
(`docs/ERD.md` sections 5 and 6).

`docs/ERD.md` section 7 draws every table with its columns and foreign
keys (Mermaid diagrams, generated from the models), and section 8 shows
how to explore the live schema and its relations in Adminer
(`make adminer`, then **Database schema** for a diagram of each schema).

Why one schema per service, without foreign keys between schemas: each
service must be able to change its own tables without breaking the
others, and the services could be moved to separate databases later.
Inside a schema, relations are real foreign keys; across schemas, a row
stores only the other service's UUID (for example `core.portfolios.owner_id`
points to `auth.users.id`), and the application keeps them consistent
through internal API checks and events such as `user.deleted` and
`portfolio.deleted`.

## Security

- **Secrets.** Every credential (API keys, database and Grafana
  passwords, JWT and encryption keys) is in `.env.docker`, which
  `.gitignore` excludes; `.env.example` lists every variable with
  placeholder values. No secret is in the repository.
- **Passwords.** Hashed with bcrypt (passlib). A stored hash looks like
  `$2b$12$...`: `2b` is the bcrypt version, `12` the cost factor
  (2^12 rounds), and the next 22 characters are a random salt, unique per
  password and stored inside the hash. The plain password is never
  stored; at login the input is hashed again with the stored salt and
  compared.
- **Sessions.** Access tokens live 15 minutes; refresh tokens rotate on
  every use and are stored only as hashes. Reusing an old refresh token
  more than 30 seconds after its rotation revokes that session chain.
  Optional TOTP 2FA, with the secret encrypted at rest.
- **Input validation, twice.** Every form has the same limits in the
  browser as on the server, and the Server Action checks again; every
  backend route validates its input with Pydantic and answers 422 with
  one error per field. Calling the API directly (Swagger or curl) with
  an invalid email, a `<script>` username or a short password is
  rejected the same way.
- **SQL injection.** User text is never concatenated into SQL.
  SQLAlchemy sends it as a bound parameter, so the database treats it as
  data, never as code: searching notes for `' OR 1=1 --` looks for that
  literal text and does not return every note.
- **XSS.** React escapes all text it renders, and the frontend never
  inserts user content as raw HTML. A note titled
  `<img src=x onerror=alert(1)>` is shown as those characters and no
  script runs.
- **HTTPS.** Browsers, scripts and API clients reach the application only
  through the gateway's TLS listener on 8443; port 8000 only redirects to
  HTTPS, and HSTS is set. Locally the certificate is self-signed, so the
  browser warns once while the connection is still encrypted; the cloud
  host uses a Let's Encrypt certificate. Traffic inside the Docker
  network (gateway to services, services to databases) is plain HTTP,
  which never leaves the host.
- **Access control.** Every request carries a token checked by the
  service; roles and organization membership decide what a user may read
  or change, and a role change or deactivation applies to open sessions
  at once.

The known security trade-offs are listed in
`docs/handbook/29-security.md` section 9.

## Multi-user Support

- Many users can be logged in at once, in several browsers and tabs;
  each session has its own refresh token.
- Concurrent writes are safe: the portfolio ledger and the trade tiers
  use row locks and idempotency keys, so two requests cannot spend the
  same cash or execute the same tier twice.
- Concurrent note editing uses Yjs, which merges the edits of several
  users instead of letting one overwrite another; the server validates
  every update and saves the merged text.
- Changes reach other users at once over WebSockets: notifications,
  unread counts, online status, analysis status and analytics refresh.

## Frontend Quality

- **Responsive layout.** Desktop, tablet and phone widths: below 768 px
  the sidebar becomes a Menu button and wide tables scroll horizontally
  (see Styling).
- **Clean browser console.** Every page was checked with the developer
  console open in Chrome, Edge and Safari: no errors or warnings. Form
  fields carry `autocomplete` attributes, local times are rendered only
  after hydration, and WebSocket clients close cleanly. The separate
  Issues panel of Chrome and Edge (not the console) may show
  informational navigation notices after the redirects of a login or
  the OAuth redirect chain, for example that a history entry created by
  an automatic redirect is skipped on Back; they are browser notices,
  not errors or warnings.
- **Browsers.** The latest Google Chrome, Microsoft Edge and Safari.
- **Privacy Policy and Terms of Service.** Linked from the footer of
  every page, also before login. The privacy policy describes the data
  AlphaMind actually stores (account, portfolios, notes, uploaded
  documents, login sessions), the third parties that receive data (the
  LLM and market-data providers), cookies, how long each kind of data is
  kept, and how users change or delete it. The terms state that
  AlphaMind is a research tool and not investment advice, and set the
  rules for accounts, acceptable use, user content and moderation.

## Features List

| Feature | What it does | Member(s) |
|---|---|---|
| Registration and login | Email and password (bcrypt), email verification, password reset, refresh-token rotation | yi-yang (pages), jiezhang (backend) |
| 2FA and OAuth | TOTP 2FA, Google and GitHub login | yi-yang, jiezhang |
| Profiles and friends | Profile, avatar with a default, friends, blocks, online status | yi-yang, jiezhang |
| Roles and administration | Admin, moderator, user and guest; user management and moderation pages | yi-yang, jiezhang |
| Organizations | Create, invite, roles, share notes, documents and portfolios | yi-yang, jiezhang |
| Portfolios | Ledger, holdings, cash, daily values, watchlists | yi-yang, jiezhang |
| Research notes | Tags, full-text search, comments, live collaborative editing | yi-yang, jiezhang |
| Documents | Upload with progress, preview, delete, access control | yi-yang, isahmed (object storage), jiezhang |
| Research assistant (RAG) | Streamed answers with cited sources | yi-yang, jiezhang |
| Sentiment | Scores for notes and comments | yi-yang, jiezhang |
| Analyses and tiers | Multi-agent analysis, debate, Judge, Reflection, price tiers, decisions | yi-yang, jiezhang |
| Backtests and reviews | Recommendation replay, tier and portfolio backtests, accuracy reviews | jiezhang |
| Notifications | For every create, update and delete action, live over WebSockets, optional email | yi-yang, jiezhang |
| Analytics | Charts, filters, date ranges, CSV and PDF | yi-yang, jiezhang |
| Public API | API keys, rate limits, Swagger | jiezhang |
| HTTPS gateway | TLS, routing, rate limits, status page | isahmed |
| Monitoring | Prometheus, exporters, Grafana dashboards, alert rules and email | isahmed, jiezhang (service metrics) |
| Backups and restore | Daily backups of Postgres, Qdrant, object storage and avatars, verified restore | isahmed |
| Scheduled jobs | Market-open analyses, tier execution, snapshots, reviews, retention | isahmed (scheduler), jiezhang (jobs) |
| CI/CD | Per-service CI, stack smoke test, gated deployment | jiezhang |
| Legal pages | Privacy Policy and Terms of Service | yi-yang |

## Modules

The evidence for each module (files, pages, how to show it) is in
`docs/MODULE_MAPPING.md`.

| # | Module | Type | Pts | How it is implemented | Why | Member(s) |
|---|---|---|---|---|---|---|
| W1 | Framework for frontend and backend | Major | 2 | Next.js on the frontend, FastAPI for the three services | Next.js renders pages on the server; FastAPI gives typed validation and OpenAPI docs. | yi-yang, jiezhang |
| W2 | ORM | Minor | 1 | SQLAlchemy 2.0 models and repositories, Alembic migrations | Three schemas that change often need typed models and versioned migrations. | jiezhang |
| W3 | Real-time features with WebSockets | Major | 2 | Authorized channels, a Redis relay across replicas, presence, reconnection | Analyses run for minutes, so status, notifications and presence must arrive live. | yi-yang, jiezhang |
| W4 | Public API | Major | 2 | Five endpoints, API keys, per-key rate limits, Swagger | Members can read portfolios and update holdings and watchlists from their own scripts. | jiezhang |
| W5 | Notification system | Minor | 1 | Every create, update and delete action notifies its actor and the users it affects | Group members must know when shared data or their analyses change. | yi-yang, jiezhang |
| W6 | File upload and management | Minor | 1 | Documents and avatars, validation in browser and server, progress, preview, delete | Research starts from documents such as filings and analyst reports. | yi-yang, isahmed, jiezhang |
| W7 | Advanced search | Minor | 1 | Filters, sorting and pagination for notes and documents | A growing library of notes and documents must stay easy to find. | yi-yang, jiezhang |
| W8 | Real-time collaboration | Minor | 1 | Yjs editing of shared notes, validated in isolated workers | Groups write their research notes together. | yi-yang, jiezhang |
| W9 | Server-Side Rendering | Minor | 1 | Server components fetch data and send finished HTML | Pages arrive with their data, and the backend calls stay on the frontend server. | yi-yang |
| B1 | Support for additional browsers | Minor | 1 | Microsoft Edge and Safari tested next to Chrome | Members of a group use different browsers. | yi-yang, jiezhang |
| U1 | Standard user management | Major | 2 | Profile, avatar with default, friends with online status | Team members need profiles and need to see who is online. | yi-yang, jiezhang |
| U2 | OAuth 2.0 | Minor | 1 | Google and GitHub | Users can sign in with an account they already have. | yi-yang, jiezhang |
| U3 | 2FA | Minor | 1 | TOTP with encrypted secrets and lockout | Accounts hold portfolios and private research, so they need a second factor. | yi-yang, jiezhang |
| U4 | Advanced permissions | Major | 2 | Four system roles, admin and moderation pages, live role changes | A shared platform needs admins and moderators for users and content. | yi-yang, jiezhang |
| U5 | Organization system | Major | 2 | Organizations with member roles and shared content | Investment clubs and teams share notes, documents and portfolios. | yi-yang, jiezhang |
| A1 | RAG system | Major | 2 | Hybrid BM25 and vector retrieval over filings, news and research | Answers must come from filings and research, with sources. | yi-yang, jiezhang |
| A2 | LLM system interface | Major | 2 | Streamed answers, error events, rate limits, provider fallback | The assistant and the analyses depend on LLM calls that can be slow or fail. | yi-yang, jiezhang |
| A3 | Sentiment analysis | Minor | 1 | Scores for notes and comments | The tone of members' notes and comments is a signal next to the research. | yi-yang, jiezhang |
| D1 | Prometheus and Grafana | Major | 2 | Exporters, dashboards, alert rules, secured Grafana | Long AI runs across several services need metrics and alerts. | isahmed, jiezhang |
| D2 | Backend as microservices | Major | 2 | Auth, core and ai services with their own schemas, REST and events | Slow or failing AI work must not take down login or the ledger. | jiezhang |
| D3 | Health checks, status page, backups, disaster recovery | Minor | 1 | `/health`, `/status`, daily backups, dry-run restore | User data and the research library must survive a lost volume or a bad migration. | isahmed, jiezhang |
| N1 | Advanced analytics dashboard | Major | 2 | Interactive charts, filters, live refresh, CSV and PDF | Users need to see how analyses and decisions perform over time. | yi-yang, jiezhang |
| | **Total** | 11 Major, 11 Minor | **33** | | | |

**Why these modules.** AlphaMind is a collaborative research tool, so
the modules follow from the product: teams need organizations,
permissions, live collaboration and notifications; research needs
document upload, search, RAG and an LLM interface; a platform used by
several people needs monitoring, backups and microservices that can fail
independently; members of a group use different browsers, so Edge and
Safari are supported next to Chrome. The analysis engine (agents, debate,
Judge, Reflection), decision tracking with backtests, and the CI/CD
pipeline are part of the product (see the Features List) but are not
counted as modules.

## Individual Contributions

**isahmed**
- The HTTPS gateway (nginx): TLS, routing to the services and the
  frontend, an internal listener for the frontend server, rate limits,
  the status page.
- Object storage (SeaweedFS), the cron scheduler, backups of Postgres,
  Qdrant, object storage and avatars with checksums and retention, and
  the restore procedure.
- Monitoring: Prometheus, exporters, Alertmanager with email, Grafana
  dashboards behind a login.
- Challenges: backing up four different data stores consistently and
  restoring them with a dry run first, solved with one folder per backup
  that holds checksums (`SHA256SUMS`) and a manifest, a restore that only
  checks the backup unless it is confirmed, and a `--verify` step that
  compares the restored data with the manifest; keeping internal routes
  and metrics unreachable from outside the gateway, solved with nginx
  answering 404 for `/internal` and `/metrics` and a separate internal
  listener for the frontend server that only the Docker network can
  reach.

**yi-yang**
- The Next.js frontend: every page, the layout for desktop and mobile,
  the forms with validation that mirrors the backend, and the Privacy
  Policy and Terms of Service pages linked from the footer.
- Session handling on the frontend server (httpOnly cookies, token
  refresh), WebSocket clients with reconnection, the live notification
  feed and analysis status, the collaborative note editor, file upload
  with progress, the analytics charts.
- Challenges: keeping tokens out of the browser while live connections
  still need one, solved with Server Actions and server components that
  keep the refresh token (and the access token) in httpOnly cookies and
  pass the short-lived access token only to the components that open a
  live connection; connecting the editor to the backend's Yjs protocol,
  solved with an editor that sends and receives the raw Yjs updates the
  backend expects and turns each text change into a small Yjs edit; a
  clean browser console on every page, solved with `autocomplete`
  attributes on the form fields and hydration-safe rendering (local
  times are shown only after the page has hydrated).

**jiezhang**
- Product and project: vision, module choice, backlog, GitHub Issues and
  the Project board, planning, reviews and merges.
- Backend: the auth, core and ai services, the shared library, the
  database schemas and migrations, events between services, WebSockets,
  the public API, notifications.
- AI: the analysis pipeline (agents, debate, Judge, Reflection), the LLM
  router, RAG, sentiment, recommendations, tiers, backtests and reviews.
- Quality and delivery: the test suites, CI/CD, the cloud deployment,
  integration of the frontend and the infrastructure, the final review
  and the end-to-end tests in three browsers.
- Challenges: LLM providers that rate-limit or overload during long
  analyses, solved with pooled keys, pacing, cooldowns and fallback
  models; concurrent refresh-token use from several tabs and browsers,
  solved with a short grace window and chain-only revocation; keeping
  three services consistent, solved with an outbox and typed events.

## Resources

### References

Learning material used before and during the project:

- Corey Schafer, *FastAPI Tutorials* (video series): https://www.youtube.com/playlist?list=PL-osiE80TeTsak-c-QsVeg0YYG_0TeyXI
- Antonio Gullí, *Agentic Design Patterns: A Hands-On Guide to Building
  Intelligent Systems*, Springer, 2025: https://link.springer.com/doi/10.1007/978-3-032-01402-3

Official documentation:

- Backend: FastAPI https://fastapi.tiangolo.com/, Pydantic https://docs.pydantic.dev/,
  SQLAlchemy 2.0 https://docs.sqlalchemy.org/en/20/, Alembic https://alembic.sqlalchemy.org/,
  pytest https://docs.pytest.org/, Testcontainers https://testcontainers.com/
- Frontend: Next.js https://nextjs.org/docs, React https://react.dev/,
  TypeScript https://www.typescriptlang.org/docs/, Tailwind CSS https://tailwindcss.com/docs,
  Yjs https://docs.yjs.dev/, MDN WebSocket API https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API
- AI: LangGraph https://langchain-ai.github.io/langgraph/, Gemini API https://ai.google.dev/gemini-api/docs,
  Qdrant https://qdrant.tech/documentation/
- Data and infrastructure: PostgreSQL https://www.postgresql.org/docs/16/, Redis https://redis.io/docs/,
  SeaweedFS https://github.com/seaweedfs/seaweedfs/wiki, Docker Compose https://docs.docker.com/compose/,
  nginx https://nginx.org/en/docs/, Prometheus https://prometheus.io/docs/, Grafana https://grafana.com/docs/,
  GitHub Actions https://docs.github.com/en/actions
- Security: OAuth 2.0 (RFC 6749) https://datatracker.ietf.org/doc/html/rfc6749,
  JSON Web Token (RFC 7519) https://datatracker.ietf.org/doc/html/rfc7519,
  TOTP (RFC 6238) https://datatracker.ietf.org/doc/html/rfc6238,
  OWASP Cheat Sheet Series https://cheatsheetseries.owasp.org/
- Market data: SEC EDGAR https://www.sec.gov/edgar/sec-api-documentation,
  FRED API https://fred.stlouisfed.org/docs/api/fred/

Papers behind the retrieval and the agents:

- Robertson and Zaragoza, *The Probabilistic Relevance Framework: BM25
  and Beyond* (2009)
- Lewis et al., *Retrieval-Augmented Generation for Knowledge-Intensive
  NLP Tasks* (2020): https://arxiv.org/abs/2005.11401
- Yao et al., *ReAct: Synergizing Reasoning and Acting in Language
  Models* (2022): https://arxiv.org/abs/2210.03629

### Use of AI

AI tools were used throughout the project, as the subject allows. The
team decided what to build, made the design decisions, and ran, tested
and reviewed every change before it was merged.

- **Infrastructure (isahmed) and frontend (yi-yang).** Written by each
  member with Claude as a guide: explanations, review of their code and
  help with testing.
- **Backend (jiezhang).** The FastAPI microservice structure was built
  step by step following Corey Schafer's FastAPI video series (see
  References); ChatGPT was used to explain unfamiliar concepts. The
  agent design was informed by the book *Agentic Design Patterns*.
  Claude (Claude Code and Claude Cowork) then reviewed the backend,
  found bugs and helped fix and refactor it.
- **AI service (jiezhang).** Large parts of the AI-service code were
  written with Claude as a pair programmer. jiezhang defined the
  behaviour and the architecture, made the decisions, ran every
  iteration on real market data from August onwards, reviewed the
  results ticker by ticker, and directed each round of fixes,
  refactoring and tests.
- **Review and testing.** The final review of the whole repository and
  the end-to-end tests were carried out with Claude's guidance; each
  finding was checked against the code and the running application
  before it was fixed.
- **Documentation.** The documents in `docs/` (including the handbook)
  and this README were drafted with Claude from the code and checked by
  the team.

## Further documentation

| Document | Content |
|---|---|
| [docs/MODULE_MAPPING.md](docs/MODULE_MAPPING.md) | Every claimed module with its evidence and a demo checklist |
| [docs/handbook/](docs/handbook/) | How every part works, how to operate it and how to show it (start at `00-index.md`) |
| [docs/API_SPEC.md](docs/API_SPEC.md) | The API contract of the three services |
| [docs/ERD.md](docs/ERD.md) | The database schema |
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture and design decisions |
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | The repository layout |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | The cloud deployment and CI/CD |
| [docs/DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) | Backups and restore |
| [tests/README.md](tests/README.md) | How the tests are organized and run |

## Known Limitations

- **Market and costs.** Only US stocks are covered, and the ledger is
  kept in US dollars. A portfolio in EUR or GBP only shows its daily
  values converted at the current exchange rate. The simulated trades
  ignore fees, commissions and taxes.
- **LLM providers.** Only Google Gemini on the free tier is configured and
  tested. The router also has adapters for OpenAI, Anthropic and Mistral,
  but they are not in use, and users cannot yet choose a provider or a
  paid tier themselves.
- **Market sentiment.** The sentiment agent reads market sentiment from
  news. Social media sources such as X and Reddit were planned but are
  not included, because of the limits and costs of their developer APIs.
- **Data providers.** Market data, news and filings come from free API
  tiers. When a provider limits requests, an analysis takes longer or
  works with less evidence; an analysis takes a few minutes in normal
  conditions.
- **LLM output.** The Judge's written reasoning can occasionally misstate
  a figure (for example count a number that is already part of a total).
  Reflection checks the debate's claims against the evidence, but not
  every sentence of the final summary. The recommended action does not
  depend on that wording.
- **Confidence rules.** The penalties and caps that adjust a
  recommendation's confidence are fixed constants. They are not yet
  derived from measured outcomes, because that needs months of scored
  recommendations.
- **Trade tiers.** Tiers are executed once a day from daily prices into
  the simulated ledger. There is no intraday execution and no connection
  to a real broker.
- **Analyses list.** The Analyses page lists the analyses a user owns,
  including the market-open analyses of the user's portfolios. Analyses
  that the operator's batch scripts start without an owner (system
  analyses) on the user's portfolios are counted in the analytics
  dashboard but are not in that list.
- **Settings without a page.** Email notification preferences and the
  portfolio history and transactions CSV exports are available through
  the API only.
- **Deployment.** The cloud deployment is one virtual machine with one
  instance of each service and no automatic failover. Its scheduler is
  kept stopped (the cloud and the local stack share the LLM and news
  quotas), so the cloud host runs no scheduled jobs and makes no
  backups; backups are made on the local stack, nightly or with
  `make backup`.
- **Dependency advisories.** `npm audit` reports published advisories
  for the pinned Next.js 16.3.3 (fixed in 16.4.0) and for two indirect
  dependencies (`sharp`, `source-map-js`). The application does not use
  most of the affected features: no `next/og` image responses, no
  `use cache` or Draft Mode, no metadata image routes, no `next/image`,
  and it runs the production server, not the development server. The
  versions stay pinned until the upgrade can be tested end to end.
- **Security trade-offs.** The services share one signing secret for
  tokens, Redis and Qdrant rely on the private Docker network instead of
  passwords, and a few sockets end only at token expiry when access
  changes. The full list with the reasons is in
  `docs/handbook/29-security.md` section 9.

## Future Improvements

- Calibrate the confidence rules from scored outcomes, and turn the
  debate and Reflection signals into quality metrics.
- Let each user choose the LLM provider (Google, OpenAI, Anthropic,
  Mistral) and whether to use a paid tier, with their own API keys.
- Add social media sentiment (X, Reddit) next to news sentiment.
- Cover more markets and instruments (other exchanges, ETFs, bonds,
  crypto), with realistic fees and multi-currency portfolios.
- Learn from each user's decisions to rank and personalize
  recommendations.
- Paper trading through a broker API, with intraday execution of tiers.
- A settings page for notification preferences, self-service data export
  and account deletion (GDPR), and chat inside organizations.
- More languages and an installable mobile app (PWA).
- Backups copied off the host, a managed database and more than one
  replica per service.
- Asymmetric token signing (RS256), so only auth-service can issue tokens.
- Upgrade Next.js to 16.4.0 or later and refresh the other dependencies
  flagged by `npm audit`, with a full rebuild and end-to-end test.

The analysis output is for research and learning, not investment advice.
