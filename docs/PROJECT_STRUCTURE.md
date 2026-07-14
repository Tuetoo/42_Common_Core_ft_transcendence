# AlphaMind — Complete Project Structure (v3)

This is the full repository tree after all corrections from the
alignment review: modular-monolith architecture, corrected module
scoring (`docs/MODULE_MAPPING.md`), corrected schema (`docs/ERD.md`),
and the `core -> shared -> ai -> domains -> api` dependency layering.

```
alphamind/
├── docker-compose.yaml            # simplified: 7 real services, no fake microservices
├── .env / .env.example
├── Makefile
├── README.md                      # required by the subject (see README requirements)
│
├── docs/
│   ├── Proposal.md
│   ├── MODULE_MAPPING.md          # v3.0 — corrected scoring
│   ├── ERD.md                     # v3.0 — corrected schema
│   ├── system_design.md
│   ├── technical_design_doc.md
│   ├── API_SPEC.md
│   ├── EVAL_BLUEPRINT.md
│   ├── ROADMAP.md
│   └── Agents.md
│
├── infra/
│   ├── prometheus/prometheus.yml
│   └── grafana/provisioning/...
│
├── frontend/                                   # Next.js — Frontend Lead
│   ├── Dockerfile
│   ├── package.json
│   ├── next.config.js
│   └── src/
│       ├── app/                                # routes (dashboard, portfolio,
│       │                                        #  research, analysis, org, settings,
│       │                                        #  privacy-policy, terms-of-service)
│       ├── components/                         # shared UI components (design system)
│       ├── features/                           # one folder per domain, mirrors backend
│       │   ├── auth/  organization/  portfolio/
│       │   ├── research/  analysis/  notification/
│       │   └── analytics/
│       ├── hooks/                              # e.g. useWebSocket, usePagination
│       ├── services/                           # REST + WebSocket API clients
│       │                                        #   (types generated from OpenAPI)
│       └── types/
│
└── backend/                                     # FastAPI — Backend Lead + AI Lead
    ├── Dockerfile
    ├── pyproject.toml
    ├── migrations/                              # Alembic
    │   ├── env.py
    │   └── versions/
    │       └── 0001_enable_pgcrypto.py           # gen_random_uuid() for core/models.py
    │
    └── app/
        ├── main.py                               # creates FastAPI app, wires
        │                                          #  lifespan + middleware + router
        │
        ├── core/                                 # ── LAYER 1: infrastructure ──
        │   ├── config.py                          # Settings (single source of env vars)
        │   ├── database.py                        # Postgres engine, Base, get_db()
        │   ├── models.py                           # UUIDPKMixin, TimestampMixin,
        │   │                                        #  SoftDeleteMixin (ORM base classes)
        │   ├── redis.py                            # cache / rate-limit / pub-sub / queue
        │   ├── qdrant.py                            # RAG vector store client
        │   ├── security.py                          # hashing, JWT, RBAC deps, TOTP
        │   ├── exceptions.py                        # AppError -> HTTP mapping
        │   ├── logging.py                           # structlog JSON config
        │   ├── middleware.py                        # request-id, public API rate
        │   │                                         #  limit, CORS
        │   ├── events.py                            # event bus (local + Redis pub/sub)
        │   └── lifecycle.py                         # FastAPI startup/shutdown
        │
        ├── shared/                                # ── LAYER 2: pure, generic logic ──
        │   ├── constants.py
        │   ├── exceptions.py                        # business exceptions (HTTP-agnostic)
        │   ├── enums/
        │   │   ├── role.py            market.py
        │   │   ├── recommendation.py  workflow.py
        │   │   ├── analysis.py        notification.py
        │   ├── events/
        │   │   └── registry.py                      # canonical cross-domain event names
        │   ├── helpers/
        │   │   ├── hashing.py   datetime.py
        │   │   ├── json.py      validation.py
        │   └── schemas/
        │       ├── base.py                          # BaseSchema, IDMixin, TimestampMixin,
        │       │                                     #  PaginationParams, PaginatedResponse,
        │       │                                     #  APIResponse, ErrorResponse
        │       ├── refs.py                           # UserRef, OrganizationRef, TickerRef
        │       └── websocket.py                      # WebSocketEnvelope
        │
        ├── ai/                                    # ── LAYER 3: the AI engine (AI Lead) ──
        │   ├── agents/                              # base.py, market.py, news.py,
        │   │                                         #  sentiment.py, filling.py,
        │   │                                         #  technical.py, fundamental.py,
        │   │                                         #  risk.py, macro.py, bull.py,
        │   │                                         #  bear.py, judge.py, reflection.py,
        │   │                                         #  supervisor.py
        │   ├── graph/                                # builder.py, nodes.py, edges.py,
        │   │                                         #  state.py (LangGraph orchestration)
        │   ├── llm/                                  # base.py, openai.py, claude.py,
        │   │                                         #  gemini.py, mistral.py, router.py
        │   ├── memory/                               # episodic.py, manager.py,
        │   │                                         #  qdrant.py, redis.py
        │   ├── rag/                                  # loader.py, parser.py, chunking.py,
        │   │                                         #  embedding.py, indexing.py,
        │   │                                         #  retriever.py, citation.py
        │   ├── recommendation/                       # engine.py, scoring.py,
        │   │                                         #  confidence.py, weights.py
        │   ├── benchmark/                            # comparator.py, evaluator.py,
        │   │                                         #  metrics.py
        │   ├── evaluation/                           # agents.py, confidence.py,
        │   │                                         #  hallucination.py, recommendation.py,
        │   │                                         #  reports.py, retrieval.py
        │   ├── tools/                                # market.py, news.py, filling.py,
        │   │                                         #  search.py
        │   ├── workflows/                            # analysis.py, debate.py, rag.py,
        │   │                                         #  recommendation.py, benchmark.py,
        │   │                                         #  review.py
        │   └── schemas/                              # ★ single home for ALL AI contracts
        │       ├── refs.py                            # NEW: AnalysisRef, RecommendationRef
        │       │                                       #  (what domains/ are allowed to embed)
        │       ├── analysis.py                        # NEW: analysis run/status entity
        │       ├── backtest.py                        # NEW: backtest run/config entity
        │       ├── benchmark.py                       # comparison results (alpha/beta/...)
        │       ├── debate.py                          # bull/bear argument shapes
        │       ├── outputs.py                         # final formatted recommendation output
        │       ├── recommendation.py                  # recommendation entity
        │       └── retrieval.py                       # RAG query/result shapes
        │
        ├── domains/                               # ── LAYER 4: CRUD business logic ──
        │   ├── _template/                            # ★ reference implementation — copy
        │   │   ├── model.py       schema.py           #   this to start any new domain
        │   │   ├── repository.py  permission.py
        │   │   ├── service.py     events.py
        │   │   └── api.py
        │   ├── auth/               (Backend Lead)
        │   │   ├── model.py    schema.py    repository.py
        │   │   ├── permission.py  service.py  events.py  api.py
        │   ├── organization/       (Backend Lead)
        │   ├── portfolio/          (Backend Lead)
        │   ├── research/           (Backend Lead; document upload lives here,
        │   │                        AI reads/updates document status via events)
        │   ├── notification/       (Backend Lead)
        │   ├── market/             (AI Lead — main consumers are the
        │   │                        Market/Technical/Fundamental agents;
        │   │                        portfolio only does read-only lookups)
        │   └── analytics/          (AI Lead — dashboards over AI-produced metrics;
        │                            still a `domains/` folder because it's exposed
        │                            as ordinary CRUD/read endpoints, same 7-file
        │                            pattern as every other domain)
        │
        ├── api/                                   # ── LAYER 5: HTTP composition ──
        │   ├── router.py                            # includes every domain + ai router
        │   ├── deps.py                               # shared FastAPI dependencies
        │   ├── auth.py         organization.py
        │   ├── portfolio.py    research.py
        │   ├── notification.py analytics.py
        │   └── ai.py                                 # exposes ai/workflows/* as endpoints
        │                                              #  (run analysis, get status, get
        │                                              #   recommendation, run backtest);
        │                                              #  also hosts /api/v1/public/* (W4)
        │
        └── workers/                                # background jobs (Redis-queue driven)
            ├── document.py                           # ingestion pipeline trigger
            ├── rag.py                                # embedding jobs
            ├── ai.py                                 # long-running analysis jobs
            ├── backtest.py                           # backtest execution jobs
            └── notifications.py                      # async notification dispatch
```

## Dependency direction, restated as a one-line rule for the team

> `core` → `shared` → `ai` → `domains` → `api`
> Never import upward. `ai/` does not know `domains/` exists.
> `domains/` may read `ai/schemas/refs.py` and `shared/schemas/refs.py`,
> nothing deeper into either package.

## What each of the 3 people touches day-to-day

| Person | Primary folders |
|---|---|
| Backend Lead | `domains/auth`, `domains/organization`, `domains/portfolio`, `domains/research`, `domains/notification`, `api/`, `core/`, `infra/`, `docker-compose.yaml` |
| AI Lead | `ai/**` (every subfolder, entirely), `workers/**` (every file, entirely — including `document.py`/`notifications.py`, since they trigger/consume AI pipeline work), `domains/market`, `domains/analytics` |
| Frontend Lead | `frontend/**` entirely |

`domains/market` and `domains/analytics` are owned by the AI Lead, not
the Backend Lead — see `ROADMAP.md v3.0 §3 "Why this split"` for the
reasoning (their main consumers are the AI agents / AI-produced
metrics, not end-user CRUD).

Everyone reads/reviews `shared/**`, `core/**`, and `docs/MODULE_MAPPING.md`
+ `docs/ERD.md`, but changes to those go through review since they're the
shared contract layer.
