# AlphaMind v4.1
# MODULE_MAPPING.md

Version: 4.1

---

## 1. Team & Ownership (3 developers, 3 services)

| Role | Owns service(s) | Domains inside |
|---|---|---|
| **Backend Lead** | `auth-service`, `core-service` | auth, organization, social (connections/presence) — in auth-service; portfolio, research, notification — in core-service |
| **AI Lead** | `ai-service` | the entire multi-agent/RAG/LLM engine, market data, AI-driven analytics |
| **Frontend Lead** | `frontend/` | consumes all 3 services through the Nginx gateway |

Backend Lead additionally owns: `gateway/nginx.conf`,
`docker-compose.yaml`, `infra/`, CI/CD pipelines for all 3 services
(see 4.5), Prometheus/Grafana setup.

---

## 2. Point Strategy (unchanged targets, updated composition)

| Tier | Points |
|---|---|
| Official minimum | 14 |
| Safe pass | 20 |
| AlphaMind baseline (v4.0) | **33** |
| Officially countable bonus above 14 | capped at +5 per the subject |

---

## 3. Architecture Overview

```
Frontend (Next.js)
        │
        ▼
   Nginx Gateway (path-based routing + WebSocket upgrade; no custom
                   gateway service to build/maintain)
        │
   ┌────┼────────────────┬─────────────────┐
   ▼                      ▼                  ▼
auth-service         core-service        ai-service
(Backend Lead)       (Backend Lead)      (AI Lead)
auth, organization,  portfolio,          agents, LangGraph,
social/connections   research,           RAG, LLM router,
                      notification,       recommendation,
                      WebSocket           backtesting,
                      connections          market, analytics
   │                      │                  │
   └──────────┬───────────┴─────────┬────────┘
              ▼                     ▼
      One Postgres instance      Redis (cache, rate
      (schemas: auth/core/ai)    limit, cross-service
                                  Pub/Sub events)
                                     │
                                     ▼
                              Qdrant (ai-service only)

shared/ — shared Python source (no packaging step), containing:
config base, DB/Redis factories, JWT verification, exceptions,
logging, event bus, ORM mixins, enums, Pydantic base schemas + refs
```

Services never import each other's code. They talk over:
1. **REST** — synchronous, via `app/clients/*.py` (typed, cached,
   raise `UpstreamServiceError` on failure).
2. **Redis Pub/Sub** — asynchronous, via `alphamind_common.events`,
   for events listed in `alphamind_common.events_registry.EventName`.

---

## 4. Scored Modules

### 4.1 Web (`IV.1`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| W1 | Framework for both frontend and backend | Major | 2 | Next.js + FastAPI (×3 services) | All |
| W2 | ORM | Minor | 1 | SQLAlchemy 2.0 async, `alphamind_common.database.Base` | Backend |
| W3 | Real-time features (WebSocket) | Major | 2 | core-service holds connections; ai-service/auth-service push via Redis Pub/Sub → core-service rebroadcasts | Backend + AI |
| W4 | Public API: API key + rate limiting + docs + ≥5 endpoints | Major | 2 | `/api/v1/public/*`, hosted by core-service, `X-API-Key`, Redis sliding window | Backend |
| W5 | Notification system | Minor | 1 | core-service `domains/notification`, triggered by local + cross-service events | Backend |
| W6 | File upload | Minor | 1 | core-service `domains/research`: PDF/TXT/MD upload, validation, storage, preview, progress, delete | Backend |
| W7 | Advanced search | Minor | 1 | Postgres full-text (notes) + Qdrant semantic (documents, via ai-service) | Backend + AI |
| **Subtotal** | | | **10** | | |

### 4.2 User Management (`IV.3`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| U1 | Standard user management (profile, avatar, connections + online status) | Major | 2 | auth-service | Backend |
| U2 | OAuth 2.0 | Minor | 1 | Google + GitHub, auth-service | Backend |
| U3 | 2FA | Minor | 1 | TOTP, auth-service | Backend |
| U4 | Advanced permissions system | Major | 2 | RBAC, auth-service | Backend |
| U5 | Organization system | Major | 2 | auth-service | Backend |
| **Subtotal** | | | **8** | | |

### 4.3 Artificial Intelligence (`IV.4`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| A1 | Complete RAG system | Major | 2 | ai-service: chunk → embed → Qdrant → retrieve → cite | AI |
| A2 | Complete LLM system interface | Major | 2 | ai-service: multi-provider router (OpenAI/Anthropic/Gemini/Mistral), streaming, rate limiting | AI |
| A3 | Sentiment analysis | Minor | 1 | ai-service Sentiment Agent | AI |
| **Subtotal** | | | **5** | | |

### 4.4 Devops (`IV.7`) — corrected: Microservices is now real

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| D1 | Monitoring (Prometheus + Grafana) | Major | 2 | All 3 services expose `/metrics` (`prometheus-fastapi-instrumentator`); Grafana dashboards per service | Backend |
| D2 | **Backend as microservices** | Major | 2 | 3 real services, own Dockerfiles, own schemas, REST + Redis Pub/Sub communication, single responsibility each — see PROJECT_STRUCTURE.md v4.0 | Backend + AI |
| D3 | Health check / status page | Minor | 1 | Each service's `/health`; Nginx exposes `/health/auth`, `/health/core`, `/health/ai` | Backend |
| **Subtotal** | | | **5** | | |

Still **not claimed**: ELK log management Major — the team runs
structured JSON logs (`alphamind_common.logging`) but not a real
Elasticsearch+Logstash+Kibana stack. Claim it only if that stack is
actually stood up.

### 4.5 CI/CD — Modules of Choice (`IV.10`)

CI/CD is not a listed module in categories I–IX, so it's claimed here,
with the justification the subject requires:

| # | Module | Type | Pts | What we build |
|---|---|---|---|---|
| C1 | Multi-Agent Investment Debate & Reflection Engine | Major | 2 | v4.1: dynamic, task-aware agent selection + every data/analysis agent as an autonomous ReAct loop (Thought→Action→Observation→...→Finish) with its own tools + multi-round Bull/Bear/Judge debate with Judge-decided convergence + Reflection that can send specific agents back for more evidence — see `Agents.md v4.1` |
| C2 | Decision Tracking & Backtesting Evaluation Engine | Minor | 1 | (unchanged — see below) |
| C3 | **Per-service CI/CD pipeline** | Minor | 1 | GitHub Actions: on every PR, lint + test + build a Docker image independently for whichever of the 3 services changed (path-filtered triggers); on merge to main, push images to a registry. |

**Draft justification for C1 (Major, 2 pts) — strengthened in v4.1:**
- *Why we chose it*: a single LLM call producing a recommendation is
  unverifiable and non-transparent, and treats every agent as a
  one-shot prompt rather than something responsible for its own
  result. Structuring the reasoning as dynamically-selected, autonomous
  specialist agents (each gathering and validating its own evidence via
  a ReAct loop, not answering blind on the first pass) → adversarial
  multi-round debate → Judge-assessed convergence → self-critique
  produces an explainable, auditable decision trail, and each agent is
  genuinely responsible for its own evidence quality rather than just
  executing a single prompt.
- *Technical challenge*: coordinating up to 9 autonomous agents (8 data/
  analysis + conditionally Portfolio), each running its own bounded
  Thought/Action/Observation loop with tool-calling; a LangGraph with
  real cycles (not a linear pipeline) for multi-round debate and
  Reflection-triggered agent retries, each dynamically decided by an
  LLM but bounded by an explicit hard cap as a safety net; a shared,
  strongly-typed state object every node reads/writes without race
  conditions; per-agent business-specific retrievers over one shared
  knowledge base.
- *Value added*: the debate/reflection output — including exactly which
  agents participated, which didn't and why, and whether the debate
  actually converged or just ran out of rounds — is the core UX of the
  product and the main reason a user would trust, and return to, a
  recommendation (see `Proposal.md`).
- *Why Major (2 pts)*: comparable in scope/complexity to the official
  "Recommendation system using ML" Major, implemented as autonomous
  multi-agent reasoning with tool use rather than a trained model —
  arguably a harder and more novel engineering problem than fitting a
  standard recommender model.

**Draft justification for C2 (Minor, 1 pt) — unchanged from v3.0:**
Closes the feedback loop (recommendation → decision → real outcome);
technical challenge is correct time-windowed evaluation without
look-ahead bias; smaller in scope than C1.

**Draft justification for C3 (Minor, 1 pt) — new in v4.0:**
- *Why we chose it*: with 3 independently deployable services, manual
  testing/building doesn't scale — a change to `ai-service` shouldn't
  require manually rebuilding and retesting `auth-service`.
- *Technical challenge*: path-filtered pipeline triggers (only build/test
  the service(s) that actually changed, plus `libs/alphamind_common`
  changes triggering all 3, since every service depends on it), and
  running integration tests that spin up more than one service against
  a shared Postgres+Redis in CI.
- *Value added*: catches a broken inter-service contract (e.g.
  `core-service`'s `auth_client.py` expecting a `UserRef` field
  `auth-service` no longer returns) before it reaches the demo.
- *Why Minor, not Major*: the pipeline itself (lint/test/build/push) is
  standard practice, not a novel technical contribution — it doesn't
  carry the same weight as C1's actual system design work.

### 4.6 Data & Analytics (`IV.8`)

| # | Module | Type | Pts | What we build | Owner |
|---|---|---|---|---|---|
| N1 | Advanced analytics dashboard | Major | 2 | ai-service `domains/analytics`, charts, real-time via WebSocket relay through core-service, CSV export | AI |
| **Subtotal** | | | **2** | | |

---

## 5. Running Total

| Category | Points |
|---|---|
| Web | 10 |
| User Management | 8 |
| Artificial Intelligence | 5 |
| Devops | 5 |
| Modules of Choice | 4 |
| Data & Analytics | 2 |
| **Total** | **34** |

(Corrected arithmetic note: Devops 2+2+1=5, Modules of Choice 2+1+1=4;
total = 10+8+5+5+4+2 = **34**.)

Mandatory minimum: 14 ✅. Officially countable bonus above 14 is capped
at +5 per the subject's Bonus chapter — build everything above for
resilience against demo-day validation failures and because the AI
stack is the actual product, not to inflate the formal score past the
cap.

---

## 6. What NOT to claim (carried over from v3.0, still applies)

| Do not claim | Why |
|---|---|
| "Database" as a standalone module | Mandatory baseline requirement, not a module |
| "Docker" as a standalone module | Mandatory baseline requirement, not a module |
| ELK log management | Only claim if real Elasticsearch+Logstash+Kibana is running, not just structured JSON logs |
| "Recommendation system using ML" (official module) | Our engine is multi-agent/LLM reasoning, not a trained collaborative/content-based filtering model — claimed instead as Modules of Choice C1, which is honest about what was built |
| CI/CD as a standalone Devops/Web module | Not in the official list — claimed via Modules of Choice (C3) with justification instead |

---

## 7. Evaluation Demo Checklist (updated for 3 services)

- [ ] `docker compose up` from a clean clone starts all 9 containers
      (frontend, gateway, 3 services, postgres, redis, qdrant,
      prometheus, grafana) with one command
- [ ] Register/login/OAuth/2FA against auth-service through the gateway
- [ ] Create an organization, invite a member (auth-service)
- [ ] Create a portfolio, add holdings (core-service) — show the
      owner's username was resolved via a live call to auth-service
      (open Grafana/logs to show the actual inter-service HTTP call)
- [ ] Upload a research document (core-service) → show it get
      chunked/embedded (ai-service reacted to a Redis Pub/Sub event, not
      a direct call) → ask a question, get a cited answer
- [ ] Run a full analysis (ai-service) → show the live workflow
      timeline over WebSocket (core-service relaying ai-service's
      Redis-published events)
- [ ] Show a recommendation on the portfolio page (core-service calling
      ai-service's `/internal/recommendations/latest`)
- [ ] Show a backtest report and decision-tracking outcome
- [ ] Show the analytics dashboard
- [ ] Call the public API with `X-API-Key`, trigger a 429
- [ ] Show 3 separate Grafana dashboards (one per service) and each
      service's independent `/health`
- [ ] Show the GitHub Actions run: a PR touching only `ai-service`
      building/testing only `ai-service` (plus `alphamind_common`)


---

# END OF MODULE_MAPPING.md
# Version 4.1
