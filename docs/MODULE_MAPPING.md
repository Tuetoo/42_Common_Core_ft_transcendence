# AlphaMind v3.0
# MODULE_MAPPING.md

Version: 3.0 

---

## 1. Team & Ownership (3 developers)

| Role | Owns | Primary domains (see ERD.md) |
|---|---|---|
| **Backend Lead** | Auth, Social (connections), Organizations, Portfolio (CRUD), Research (notes/tags/upload orchestration), Notifications, Public API + rate limiting, RBAC, OAuth, 2FA, Docker/infra, Monitoring, System/Audit | AUTH, SOCIAL, ORGANIZATION, PORTFOLIO, RESEARCH, DOCUMENT (ingest orchestration), NOTIFICATION, SYSTEM, AUDIT |
| **AI Lead** | RAG pipeline, LangGraph agent orchestration, multi-LLM router, debate/reflection engine, decision tracking, backtesting/benchmarking, market data ingestion, AI-specific analytics dashboards, and everything under `backend/app/workers/` | RAG, AI ANALYSIS, AGENT, AI MEMORY, PROMPT MANAGEMENT, DEBATE, DECISION TRACKING, BACKTEST, BENCHMARK, MODEL ROUTING, MARKET, ANALYTICS |
| **Frontend Lead** | Next.js app end-to-end: dashboard, portfolio UI, research UI, AI workflow visualization, analytics dashboards, org/settings UI, WebSocket client | Consumes all backend domains through the REST + WebSocket API |

Every table migration is reviewed by at least one other developer before
merge (see official requirement: "code reviews" + "how each member
contributed").

---

## 2. Point Strategy

| Tier | Points | Meaning |
|---|---|---|
| Mandatory minimum | 14 | Required to pass at all |
| Our baseline plan (Section 4) | **31** | Everything we are actually building, mapped honestly |
| Officially countable bonus | +5 max | Per the subject: bonus only unlocks once the 14-point baseline is fully validated, and is capped at 5 extra points regardless of how much more you build |

**Why build ~31 points of functionality if only ~19 are "countable"?**
Because (a) modules occasionally fail validation at demo time due to a bug or
a missed requirement — building real headroom protects the 14-point pass
line, (b) the AI stack is the actual product, not a school exercise (see
`Proposal.md`), and (c) the evaluator explicitly rewards depth and coherence,
not just a checklist.

---

## 3. Architecture Overview (simplified, corrected)

```
Frontend (Next.js, 1 container)
        │  REST (JSON) + WebSocket
        ▼
Backend (FastAPI, 1 container — modular monolith)
    ├── domains/auth
    ├── domains/organization
    ├── domains/portfolio
    ├── domains/research
    ├── domains/notification
    ├── domains/analytics
    └── ai/  (LangGraph engine: agents, RAG, debate, reflection, router)
        │
   ┌────┼────────────┐
   ▼    ▼             ▼
Postgres  Redis     Qdrant
(system   (cache,   (vector
 of        sessions, store for
 record)   rate     RAG)
           limit,
           pub/sub)

Prometheus + Grafana (observability, separate containers)
```

This is **one deployable backend**, not seven fake microservices. All
`docker-compose.yaml` service definitions that pointed at non-existent
`backend/services/*` directories are removed. The compose file now defines
exactly what is built: `frontend`, `backend`, `postgres`, `redis`, `qdrant`,
`prometheus`, `grafana` — and starts with a single `docker compose up`.

If, after the core plan is done and fully working, the team has spare time
and wants the extra 2 "Microservices" points, `domains/research` + the AI
engine (the most independent, most CPU/IO-heavy part) is the best candidate
to split into a second container communicating over REST/Redis — but this is
an explicit **stretch item**, not part of the baseline plan, and must not be
started before the 14-point baseline is fully working end to end.

---

## 4. Scored Modules (mapped against the official subject)

### 4.1 Web (`IV.1`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| W1 | Use a framework for both frontend and backend | Major | 2 | Next.js (frontend) + FastAPI (backend) | Frontend + Backend |
| W2 | Use an ORM for the database | Minor | 1 | SQLAlchemy 2.0 (async) + Alembic migrations | Backend |
| W3 | Real-time features via WebSocket | Major | 2 | Live workflow progress, notifications, org presence ("who's online") | Backend + Frontend |
| W4 | Public API: secured API key + rate limiting + docs + ≥5 endpoints (GET/POST/PUT/DELETE) | Major | 2 | `/api/v1/public/*` namespace, `X-API-Key` header, Redis-backed sliding-window limiter, OpenAPI/Swagger docs | Backend |
| W5 | Complete notification system for all create/update/delete actions | Minor | 1 | `notifications` domain fires on portfolio/research/org/analysis events | Backend |
| W6 | File upload and management system | Minor | 1 | Research document upload (PDF/TXT/MD), client+server validation, secure storage, preview, progress bar, delete | Backend + Frontend |
| W7 | Advanced search (filters, sorting, pagination) | Minor | 1 | Search across research notes/documents/holdings with filters + pagination | Backend + Frontend |
| **Subtotal** | | | **10** | | |

Not claimed (explicit decision, can be added later as stretch): "Allow users
to interact with other users" Major (would require a full chat system — see
4.2 for the lighter connections/presence feature we build instead),
Real-time collaborative editing, SSR, PWA, custom design system.

### 4.2 User Management (`IV.3`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| U1 | Standard user management (profile update, avatar w/ default, connections + online status, profile page) | Major | 2 | Profile CRUD, avatar upload, a lightweight **connections** feature (add/remove, see online status via WebSocket presence) — not a full chat system | Backend + Frontend |
| U2 | OAuth 2.0 | Minor | 1 | Google + GitHub login | Backend |
| U3 | 2FA | Minor | 1 | TOTP (Google Authenticator / Authy compatible) | Backend |
| U4 | Advanced permissions system (CRUD users, roles, role-based views) | Major | 2 | `SystemRole` (admin/user) + `OrganizationRole` (owner/admin/member/viewer) RBAC, admin console | Backend + Frontend |
| U5 | Organization system (CRUD org, add/remove members, scoped actions) | Major | 2 | Organizations own shared portfolios and shared research | Backend + Frontend |
| **Subtotal** | | | **8** | | |

Not claimed: Game statistics (no game in this project), 2FA already counted
above. User activity analytics dashboard (Minor, 1pt) is a good stretch item
once the baseline works — the `episodic_memories`/audit data already
supports it.

### 4.3 Artificial Intelligence (`IV.4`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| A1 | Complete RAG system | Major | 2 | Upload → chunk → embed → Qdrant → retrieve → cite, used by the Filing/News/Research agents | AI |
| A2 | Complete LLM system interface | Major | 2 | Multi-provider router (OpenAI / Anthropic / Gemini / Mistral), streaming responses, error handling + rate limiting | AI |
| A3 | Sentiment analysis for user-generated content | Minor | 1 | Sentiment Agent scores news headlines and user research notes | AI |
| **Subtotal** | | | **5** | | |

Not claimed: "AI Opponent" (requires a game — out of scope here),
"Recommendation system using machine learning" (see 4.5 below — we do not
claim this exact module because our recommendation logic is agent/LLM-driven
reasoning, not a collaborative/content-based filtering model that improves
from behavioral data; claiming it as such would not survive evaluator
questioning). Voice/speech and image recognition are not built.

### 4.4 Devops (`IV.7`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| D1 | Monitoring with Prometheus + Grafana | Major | 2 | Latency, error rate, AI token cost, agent runtime dashboards + alert rules | Backend |
| D2 | Health check / status page | Minor | 1 | `/health` endpoint aggregating Postgres/Redis/Qdrant liveness, simple status page in the frontend | Backend + Frontend |
| **Subtotal** | | | **3** | | |

Explicitly **not claimed**: ELK log management Major (we use Loki for logs,
which is not Elasticsearch+Logstash+Kibana — if the team wants this module,
swap Loki for a real ELK stack; otherwise do not claim it). Microservices
Major (architecture simplified to a monolith — see Section 3; revisit only
as a stretch item after the baseline is done).

### 4.5 Modules of Choice (`IV.10`) — where AlphaMind's real engineering lives

> Per the subject, each custom module requires README justification
> covering: why we chose it, what technical challenge it solves, how it adds
> value, and why it deserves its point value. Draft justification below —
> finalize it in `README.md`, not just here.

| # | Module | Type | Pts | What we build |
|---|---|---|---|---|
| C1 | Multi-Agent Investment Debate & Reflection Engine | Major | 2 | A Supervisor agent orchestrates 8 specialist agents (Market, News, Sentiment, Filing, Technical, Fundamental, Risk, Macro) via LangGraph; a Bull agent and a Bear agent independently build opposing cases; a Judge agent weighs both; a Reflection agent checks source quality, evidence sufficiency, and confidence calibration before a recommendation is released. |
| C2 | Decision Tracking & Backtesting Evaluation Engine | Minor | 1 | Every recommendation is persisted; outcomes are graded against real market data after N days; a backtest engine computes Sharpe/Sortino ratio, max drawdown, and win rate against a benchmark (SPY/QQQ/DIA/VT). |
| **Subtotal** | | | **3** | |

**Draft justification for C1 (Major, 2 pts):**
- *Why we chose it*: single-LLM-call answers ("should I buy X?") are
  unverifiable and non-transparent. Structuring the reasoning as parallel
  specialist analysis → adversarial debate → independent judging →
  self-critique produces an explainable, auditable decision trail instead of
  a black-box answer.
- *Technical challenge*: coordinating 12 agents with partial-failure
  handling, bounded latency (parallel fan-out where possible), and a shared,
  strongly-typed state object (LangGraph `state.py`) that every node reads
  and writes without race conditions.
- *Value added*: the debate/reflection output is the core UX of the product
  and the main reason a user would return to it (see `Proposal.md`).
- *Why Major (2 pts)*: comparable in scope/complexity to the official
  "Recommendation system using ML" Major, but implemented as a
  multi-agent reasoning pipeline rather than a trained model.

**Draft justification for C2 (Minor, 1 pt):**
- *Why we chose it*: without measuring whether past recommendations were
  actually good, an "AI recommendation" feature is just an opinion generator.
- *Technical challenge*: correctly time-windowing "outcome" evaluation
  (waiting N days, pulling the real price, computing alpha vs benchmark)
  without look-ahead bias.
- *Value added*: closes the feedback loop described in `Proposal.md`
  ("Recommendation → User Action → Market Outcome").
- *Why Minor (1 pt)*: smaller in scope than C1 — a metrics/reporting layer
  on top of data already produced by the recommendation engine.

### 4.6 Data & Analytics (`IV.8`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| N1 | Advanced analytics dashboard with data visualization | Major | 2 | Interactive charts (portfolio performance, recommendation accuracy, agent performance, workflow timeline), real-time updates over WebSocket, CSV/PDF export, date-range filters | AI (data) + Frontend |
| **Subtotal** | | | **2** | | |

Not claimed as separately scored (kept as engineering quality, not points):
data export/import (Minor) and GDPR features (Minor) are reasonable stretch
adds once the baseline is stable, but are not required to hit our target.

---

## 5. Running Total

| Category | Points |
|---|---|
| Web | 10 |
| User Management | 8 |
| Artificial Intelligence | 5 |
| Devops | 3 |
| Modules of Choice | 3 |
| Data & Analytics | 2 |
| **Total** | **31** |

Mandatory minimum: 14 ✅ (cleared with a 17-point safety margin)
Officially countable bonus is capped at +5 beyond the 14 baseline per the
subject's Bonus chapter — build everything above for quality, redundancy,
and product value, but do not assume the grade scales linearly past that cap.

---

## 6. Modules explicitly excluded (do not present these at evaluation)

| Claimed in v2.3 | Why it's invalid | What to do instead |
|---|---|---|
| "Web Framework" (2pt) + "Frontend Framework" (2pt) as two separate majors | The subject has exactly ONE major for "framework for both frontend and backend" (2 pts total) | Counted once as W1 above |
| "Database" (2pt) | Not a module in the subject — a schema with clear relations is a mandatory baseline requirement, not a scored module | Removed from the point count entirely |
| "Docker" (1pt) | Containerized single-command deployment is a mandatory general requirement, not a scored module | Removed from the point count entirely |
| "CI/CD" (1pt) | Not a listed module anywhere in the subject | Either drop it, or move it into `Modules of choice` with full justification if the team wants to claim it |
| "ELK log management" (2pt) using Loki | The official module requires Elasticsearch + Logstash + Kibana specifically; Loki is not ELK | Either stand up real ELK, or don't claim this module |
| "Microservices" (2pt) | `docker-compose.yaml` pointed at services that don't exist in the codebase; the actual code is one FastAPI app | Architecture simplified to a monolith (Section 3); revisit only as a stretch item |
| "Recommendation system using ML" (2pt) | Our engine is multi-agent/LLM reasoning, not a trained collaborative/content-based filtering model that improves from usage data — an evaluator can and will ask to see the model | Reframed as Modules of Choice C1 (Section 4.5), which honestly describes what we built |

---

## 7. Evaluation Demo Checklist

- [ ] Register, login, OAuth login, 2FA setup and login
- [ ] Update profile, upload avatar, add a connection, see their online status
- [ ] RBAC: show admin vs regular user views
- [ ] Create an organization, invite a member, show scoped portfolio/research
- [ ] Create a portfolio, add holdings, record a transaction
- [ ] Upload a research document (PDF), show it get chunked/embedded, ask a
      question and get a cited answer (RAG)
- [ ] Run a full analysis: watch the agent workflow execute live over
      WebSocket, see the Bull/Bear debate, see the Judge verdict, see the
      Reflection self-check, receive the final BUY/HOLD/SELL recommendation
- [ ] Show a past decision being graded against real market outcome
      (Decision Tracking) and a backtest report (Sharpe/Sortino/drawdown vs
      SPY)
- [ ] Show the analytics dashboard (recommendation accuracy, agent
      performance) with live updates and a CSV export
- [ ] Call the public API with `X-API-Key`, show a 429 once the rate limit
      is hit, show the Swagger docs
- [ ] Show Grafana dashboards and the `/health` status page
- [ ] `docker compose up` from a clean clone, single command, no manual steps

---

## 8. Change Log vs v2.3

1. Removed double-counted Web framework claim (was 4pts, now 2pts, correctly
   as one Major).
2. Removed "Database," "Docker," and "CI/CD" as scored modules — none exist
   in the official module list; they are mandatory baseline requirements.
3. Removed the ELK claim (Loki ≠ ELK); documented as a stretch item requiring
   a real ELK stack.
4. Removed the "Microservices" claim; simplified architecture to a working
   modular monolith with an honest `docker-compose.yaml`.
5. Added the AI stack (RAG, LLM interface, sentiment) as properly scored
   `IV.4 Artificial Intelligence` modules — previously excluded from the
   point count entirely.
6. Reframed the debate/reflection engine and decision-tracking/backtesting
   as justified `Modules of Choice` (Major + Minor) instead of an
   unscored "AlphaMind Core AI Modules" appendix.
7. Added a small "connections + online status" feature to satisfy the
   `User Management` Major's own requirement (independent of, and lighter
   than, the Web category's full chat system, which we do not build).
8. Corrected baseline total from an inflated 22–24 down to an honestly
   mapped **31**, with an explicit note on the official +5 bonus cap.

---

# END OF MODULE_MAPPING.md
# Version 3.0
