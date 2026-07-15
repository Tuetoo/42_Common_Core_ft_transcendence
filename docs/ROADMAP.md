# AlphaMind v4.1

# ROADMAP.md

---

# 1. Project Vision

AlphaMind is a production-grade AI-powered investment research platform,
built as 3 independently deployable microservices behind a shared
gateway — not a school-project simplification, a deliberate,
demonstrable architecture decision.

Success Criteria:

```text
PASS REQUIREMENT
14 Points (official minimum)

SAFE TARGET
20 Points

ALPHAMIND BASELINE
34 Points of fully-built, demoable functionality
(docs/MODULE_MAPPING.md v4.0)

3 real microservices + Nginx gateway
Per-service CI/CD
Multi-Agent AI (RAG, LLM router, debate/reflection)
Backtesting against real benchmarks
```

---

# 2. Team Structure

```text
3 Developers, confirmed acceptable for this cohort
40~50 hours/person/week, ~810 total engineering hours over 6 weeks
```

---

# 3. Team Ownership

| Role | Service(s) | Domains | Also owns |
|---|---|---|---|
| **Backend Lead** | `auth-service`, `core-service` | auth, organization, social — in auth-service; portfolio, research, notification — in core-service | `gateway/nginx.conf`, `docker-compose.yaml`, `infra/`, CI/CD for all 3 services, Prometheus/Grafana |
| **AI Lead** | `ai-service` | agents, RAG, LLM router, debate/reflection, backtesting, market, analytics | — |
| **Frontend Lead** | `frontend/` | consumes all 3 services through the gateway | — |

## Why 2 services for Backend Lead and 1 (large) service for AI Lead

This isn't an even 3-way split by service count because the services
aren't equal in size. `auth-service` is comparatively small and stable
once built (identity doesn't change shape often); `core-service` is
larger (portfolio + research + notification + the WebSocket gateway
logic) but still ordinary CRUD engineering. `ai-service` alone is
roughly as much work as the other two combined (12 agents, LangGraph
orchestration, RAG pipeline, multi-LLM router, backtesting engine) —
giving it a dedicated owner rather than splitting it further avoids
splintering a tightly-interdependent system across two people who'd
constantly need to coordinate on the same `ai/graph/state.py`.

## Everyone reviews

Changes to `shared/` (see `docs/COMMON_LIBRARY_README.md`)
go through review by someone other than the author — it's the one
piece of code that, if broken, breaks all 3 services simultaneously.

---

# 4. Development Strategy

## Rule #1 — Contracts Before Code

Before writing a domain's real logic, agree on:
1. Its `shared.schemas.refs` entry, if other services need to
   reference it.
2. Its `EventName` entries, if it needs to signal other services.
3. Its REST paths, documented in that service's own OpenAPI (FastAPI
   generates this automatically).

## Rule #2 — Database Before Logic

```text
ERD.md v4.0 entry (which schema? auth/core/ai)
  → Alembic migration (that service's own migrations/versions/,
    version_table_schema set to that service's schema)
  → repository.py → service.py → api.py → Frontend
```

## Rule #3 — Every Service Independently Runnable

At all times, `docker compose up auth-service postgres redis` (just
that one service and its infra deps) must work on its own — this is
what "loosely coupled, independently deployable" actually means, and
what the evaluator can be shown directly.

## Rule #4 — Engineering Standards

### Python Environment (one shared environment)

```bash
uv sync              # from the repo root — installs everything shared/
uv add <package>       # + all 3 services need, into the ONE root pyproject.toml
```

### Testing

```bash
make test              # unit tests for all 3 services (see tests/README.md
                         # for why this is 3 separate pytest invocations,
                         # not one — all 3 services share the package
                         # name `app`)
make test-integration    # requires docker compose up first
```

### Backend Standards

```text
Pydantic v2, FastAPI, SQLAlchemy 2.0 (async), AsyncIO — same as before,
now applied independently in 3 services sharing one contract library.
```

---

# 5. CI/CD (per-service, path-filtered)

Each service has its own workflow file at the repo root
(`.github/workflows/auth-service-ci.yml`, `core-service-ci.yml`,
`ai-service-ci.yml` — root-level, not nested inside each service's own
folder, since GitHub Actions only reads workflows from `.github/
workflows/` at the repository root regardless). Each triggers on:
- changes to `services/<name>/**`
- changes to `shared/**` or the root `pyproject.toml`/`uv.lock` (since
  every service depends on them — a common-library change must
  re-validate all 3 services)

Pipeline steps: `uv sync` → lint (`ruff`) → test (`make test-<name>`,
spinning up throwaway Postgres+Redis via GitHub Actions services) →
build the Docker image. A 4th workflow
(`.github/workflows/integration-ci.yml`) runs the full `docker compose
up` stack + `make test-integration` on every push to `main`.

This is claimed as Modules of Choice C3 (Minor, 1 pt) — see
`MODULE_MAPPING.md v4.1 §4.5` for the justification.

---

# 6. Milestones

```text
Week 1 — Foundation: shared + all 3 service skeletons + gateway
Week 2 — auth-service complete
Week 3 — core-service complete
Week 4 — ai-service: multi-agent engine
Week 5 — ai-service: evaluation, cross-service integration hardening
Week 6 — Hardening + demo prep
```

---

# WEEK 1 — FOUNDATION

## Deliverables

### Repo layout

```text
shared/
services/auth-service/  services/core-service/  services/ai-service/
gateway/nginx.conf
frontend/
docs/  infra/
docker-compose.yaml
```

### `shared/` (Backend Lead builds, all 3 review)

```text
config.py, database.py, models.py, redis.py, security.py,
exceptions.py, logging.py, events.py, events_registry.py,
enums/, schemas/{base,refs,websocket}.py
```

This is exactly the package delivered in this alignment review —
`docs/COMMON_LIBRARY_README.md` is the reference.

### 3 service skeletons

Each with: `Dockerfile`, `pyproject.toml` (path-depends on
`shared`), `app/main.py` (health check, `/metrics`, CORS,
exception handlers), `app/config.py`, `app/database.py`,
`app/domains/_template/` (or `app/ai/schemas/` for ai-service).

### Gateway + Docker Compose

```text
gateway/nginx.conf — path-based routing to the 3 services + WebSocket
  upgrade for /ws/
docker-compose.yaml — 9 containers total
infra/postgres/init-schemas.sql — auth/core/ai schemas + pgcrypto
```

### CI skeleton (3 pipelines)

Lint + test + build for each service, path-filtered as described in §5.

## Week 1 Success Criteria

```text
docker compose up starts all 9 containers from a clean clone
Each service's /health and /metrics respond
Gateway correctly routes a request to each of the 3 services
CI passes on all 3 pipelines (even with near-empty test suites)
```

---

# WEEK 2 — AUTH-SERVICE

Goal: complete identity, fully independent of the other 2 services.

## Backend Lead

```text
domains/auth: register, login, JWT issuing (app/security.py),
  refresh tokens
OAuth: Google + GitHub
2FA: TOTP
domains/organization: CRUD, invite/remove member, RBAC roles
domains/social: connections (add/accept/block), presence (last_seen_at)
api/internal.py: /internal/users/{id}, /internal/organizations/{id}
```

## AI Lead

```text
ai-service skeleton: app/ai/schemas/{analysis,backtest}.py finalized
domains/market: client contract for external price data (no live
  wiring yet)
```

## Frontend Lead

```text
Auth UI, Profile UI, Organization UI — calling auth-service through
  the gateway at /api/v1/auth/*, /api/v1/organizations/*
```

## Week 2 Success Criteria

```text
auth-service is fully independently testable:
  docker compose up auth-service postgres redis
Full auth flow works end to end through the gateway
```

Modules unlocked: U1–U5.

---

# WEEK 3 — CORE-SERVICE

Goal: portfolio, research, and the WebSocket gateway to the frontend.

## Backend Lead

```text
domains/portfolio: CRUD, holdings, transactions, watchlists — owner_id
  resolved via app/clients/auth_client.py (NOT a local join)
domains/research: notes, comments, tags, document upload; publishes
  document.uploaded on save
app/websocket_manager.py + app/api/websocket.py: /ws/{channel}
app/api/public.py: the 5 Public API (W4) endpoints, behind
  PublicAPIRateLimitMiddleware
```

## AI Lead

```text
ai-service: real RAG pipeline (chunking, embedding, Qdrant indexing),
  triggered by app/event_listener.py reacting to document.uploaded
domains/market: real price-fetching + Redis caching
```

## Frontend Lead

```text
Portfolio dashboard, Research dashboard + upload page, WebSocket
  connection wired to /ws/{channel} through the gateway
```

## Week 3 Success Criteria

```text
Portfolio/Research CRUD works through the gateway
Uploading a document in core-service visibly triggers processing in
  ai-service (show it in each service's logs / Grafana — this is the
  cross-service event flow, worth demonstrating explicitly)
Semantic search returns cited results
```

Modules unlocked: W2, W4, W6, W7, A1.

---

# WEEK 4 — AI-SERVICE: MULTI-AGENT ENGINE

Goal: the AlphaMind brain. Entirely AI Lead.

```text
LangGraph with dynamic agent selection (Supervisor decides which of
  the 9 agents run per request — task type + optional user override +
  portfolio context, see Agents.md v4.1 §3) and real cycles (multi-
  round debate, Reflection-triggered retries), not a fixed linear
  pipeline
9 agents, each an autonomous ReAct loop (Thought -> Action ->
  Observation -> ... -> Finish) with its own tools: Market, News,
  Sentiment, Filing, Technical, Fundamental, Risk, Macro, and the new
  Portfolio agent (only runs when the analysis has 2+ holdings)
Bull/Bear/Judge: multi-round debate, Judge decides convergence each
  round, hard-capped at 3 rounds
Reflection: can send specific agents back for more evidence, hard-
  capped at 2 retries total
Multi-LLM router (OpenAI/Anthropic/Gemini/Mistral), streaming
app/api/internal.py: /internal/analyses/{id},
  /internal/recommendations/latest
Publishes analysis.started/completed/failed, recommendation.ready
  (shared.events_registry.EventName)
```

## Backend Lead (integration only)

```text
core-service's event_listener.py: subscribe to analysis.completed /
  recommendation.ready, rebroadcast over WebSocket, create a
  notification
```

## Frontend Lead

```text
Analysis page: live workflow timeline over the gateway's /ws/ endpoint
```

## Week 4 Success Criteria

```text
Run an analysis end to end; watch it live over WebSocket even though
  the analysis is running in a completely different process
  (ai-service) than the one holding the WebSocket connection
  (core-service) — this cross-service real-time flow is the single
  most impressive thing to demo
```

Modules unlocked: A2, A3, C1.

---

# WEEK 5 — AI-SERVICE: EVALUATION + INTEGRATION HARDENING

## AI Lead

```text
Decision tracking, backtesting engine (Sharpe/Sortino/drawdown/win-rate
  vs SPY/QQQ/DIA/VT)
domains/analytics: dashboard-ready aggregates
```

## Backend Lead

```text
Prometheus scrape config verified for all 3 services; Grafana
  dashboards per service (latency, error rate, request volume)
Inter-service failure handling review: what happens if auth-service is
  down when core-service needs a UserRef? (Should degrade gracefully —
  e.g. show "user" as a placeholder — not 500 the whole request.)
```

## Frontend Lead

```text
Analytics dashboard, backtest report UI
```

## Week 5 Success Criteria

```text
Backtests produce real metrics
Killing auth-service does not crash core-service — it degrades
  (demonstrate this explicitly; it's a legitimate "why microservices"
  evaluator question and this is the answer, live)
```

Modules unlocked: W3, N1, C2.

---

# WEEK 6 — HARDENING + DEMO PREP

## All three developers

```text
Testing: unit + integration tests per service; at least one true
  cross-service integration test (e.g. spin up auth-service +
  core-service together in CI, verify a real HTTP call between them)
Security review: JWT verification on all 3 services, OAuth, 2FA, Public
  API rate limiting
Monitoring review: all 3 Grafana dashboards + all 3 /health endpoints
CI/CD review: confirm path-filtering actually skips unrelated services
Docker review: docker compose up from a clean clone, single command,
  all 9 containers healthy
Demo rehearsal against docs/EVAL_BLUEPRINT.md v4.0, including the
  cross-service failure-mode demo from Week 5
```

## Week 6 Success Criteria

```text
Stable build, passing tests, evaluation-day checklist fully green
```

---

# 7. Risk Management

## Risk #1 — Inter-Service Contract Drift

The single biggest new risk vs the monolith plan: `core-service`'s
`auth_client.py` expects a `UserRef` shape that `auth-service` might
change without core-service knowing, until it breaks at runtime.
Mitigation: `shared.schemas.refs` is the enforced single
source of truth for that shape — a breaking change there is visible in
a PR diff to `shared/`, reviewed by someone other than the author (see §3).

## Risk #2 — Debugging Across 3 Processes

A bug that only reproduces "when a document is uploaded and then an
analysis is run" now spans 2 services' logs. Mitigation:
`shared.logging` tags every line with `service` and every
request with a `request_id`; make it standard practice to log the
`request_id` into cross-service event payloads too, so one id can be
grepped across both services' logs.

## Risk #3 — AI Engine Scope, Now Also an Isolation Risk

Same underlying risk as v3.0 (AI Lead's workload is the largest share),
compounded by the fact that `ai-service` is now a separate deployable —
if it's unstable, it can be restarted/rolled back independently without
touching auth-service or core-service. Mitigation: Week 4's success
criteria stay narrow (4 data agents + debate + reflection + one
recommendation) before adding scope.

## Risk #4 — Frontend Depending on 3 Independently-Evolving APIs

Mitigation: the gateway (`gateway/nginx.conf`) gives the frontend one
base URL; each service's OpenAPI schema is still generated and typed
independently, so a breaking change in one service's API is caught at
the type level in the frontend build, not at runtime.

## Risk #5 — CI Path-Filtering Misconfigured, Wasting Time or Missing Bugs

If `shared/**` changes don't correctly trigger all 3
pipelines, a breaking common-library change could pass CI and only
break at `docker compose up`. Mitigation: test the path-filter
configuration itself in Week 1, deliberately, with a throwaway commit
touching only `shared/`.

---

# 8. Evaluation Readiness Checklist

See `docs/EVAL_BLUEPRINT.md v4.0 §26` for the full list. Summary:

```text
Core: Register, Login, OAuth, 2FA, Organizations, Portfolio, Research,
  Search, Real-time (cross-service)
AI: RAG, Multi-Agent, Debate, Reflection, Recommendation, Backtesting
Infrastructure: Docker (9 containers, 1 command), 3× Monitoring
  dashboards, 3× Swagger, 3× Health check, 3× CI/CD pipeline
Resilience: demonstrate one service being killed without crashing
  the others
```

---

# 9. Expected Point Progression

| After | Points unlocked (cumulative) |
|---|---|
| Week 2 | ~11 (U1–U5, auth-service complete) |
| Week 3 | ~21 (+ W2, W4, W6, W7, A1) |
| Week 4 | ~28 (+ A2, A3, C1) |
| Week 5 | ~34 (+ W3, N1, C2) |
| Week 6 | 34 + D1, D2, D3, C3 confirmed working = final |

Mandatory minimum 14 is cleared after Week 2. Officially countable
bonus above 14 is capped at +5 per the subject — build the full 34 for
demo resilience and product quality, not because the formal score
scales linearly past the cap.

---

# 10. Final Deliverables

Documentation: `README.md` + everything in `docs/` (`ERD.md`,
`API_SPEC.md`, `Agents.md`, `MODULE_MAPPING.md`, `ROADMAP.md`,
`EVAL_BLUEPRINT.md`, `system_design.md`, `technical_design_doc.md`,
`COMMON_LIBRARY_README.md`).

Deployment: `docker-compose.yaml` (9 containers), 3 independent CI/CD
pipelines, Prometheus + Grafana.

Application: Frontend, 3 microservices (auth/core/ai), Nginx gateway,
shared `shared` library, multi-agent AI engine, backtesting
engine, analytics dashboards.

---

# Final Target

A working, honestly-scoped, 3-service microservices platform — real
service boundaries, real inter-service communication (REST + Redis
Pub/Sub), per-service CI/CD, workflow-driven AI — achievable by 3
developers in 6 weeks (~810 engineering hours), with the architecture
itself being one of the strongest things to demonstrate to an evaluator.

---

# END OF ROADMAP.md
# Version 4.1