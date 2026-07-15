# AlphaMind — Complete Project Structure (v4.1)


```
alphamind/
├── docker-compose.yaml            # 9 containers, one command
├── pyproject.toml                 # ONE dependency file for shared/ + all 3 services
├── uv.lock
├── Makefile                       # up/down/test-*/lint targets
├── .env / .env.example
├── README.md
│
├── .github/workflows/              # root-level, path-filtered per service
│   ├── auth-service-ci.yml          # triggers on services/auth-service/** or shared/**
│   ├── core-service-ci.yml
│   ├── ai-service-ci.yml
│   └── integration-ci.yml           # full docker-compose stack, runs on push to main
│
├── docs/
│   ├── Proposal.md
│   ├── MODULE_MAPPING.md            # v4.1
│   ├── ERD.md                       # v4.0 (schema-per-service — unaffected by this revision)
│   ├── PROJECT_STRUCTURE.md         # this file
│   ├── ROADMAP.md                   # v4.1
│   ├── COMMON_LIBRARY_README.md     # v4.1
│   ├── system_design.md
│   ├── technical_design_doc.md
│   ├── API_SPEC.md
│   ├── EVAL_BLUEPRINT.md
│   └── Agents.md                    # v4.1 — major rewrite, read this one first
│
├── infra/
│   ├── gateway/
│   │   └── nginx.conf                # moved here from top-level gateway/
│   ├── postgres/init-schemas.sql
│   ├── prometheus/prometheus.yml
│   └── grafana/provisioning/datasources.yml
│
├── shared/                          # was libs/alphamind_common/src/alphamind_common —
│   ├── __init__.py                   # flattened: no libs/ wrapper, no src/ layer, no
│   ├── config.py                     # separate pyproject.toml/pip-install step. Every
│   ├── database.py                   # service's Dockerfile just COPYs this folder in
│   ├── models.py                     # alongside its own app/ — both importable because
│   ├── redis.py                      # the container's WORKDIR contains both as siblings.
│   ├── security.py                   # (JWT verify only — issuing stays auth-service-only)
│   ├── exceptions.py
│   ├── logging.py
│   ├── events.py                     # LocalEventBus + Redis Pub/Sub cross-service transport
│   ├── events_registry.py            # EventName catalog
│   ├── enums/
│   │   ├── role.py  market.py  recommendation.py
│   │   └── workflow.py  analysis.py  notification.py
│   └── schemas/
│       ├── base.py                   # BaseSchema, IDMixin, TimestampMixin, PaginatedResponse, APIResponse
│       ├── refs.py                    # UserRef, OrganizationRef, TickerRef, AnalysisRef, RecommendationRef
│       └── websocket.py                # WebSocketEnvelope
│
├── services/
│   ├── auth-service/                # Backend Lead
│   │   ├── Dockerfile                 # simplified: no path dependency, just
│   │   │                               # COPY shared + COPY this service's app/
│   │   └── app/
│   │       ├── main.py  config.py  database.py  deps.py
│   │       ├── security.py             # ★ token issuing, password hashing, TOTP —
│   │       │                            #   the ONLY place in the whole system
│   │       ├── domains/
│   │       │   ├── _template/            # 7-file reference pattern
│   │       │   ├── auth/  organization/  social/   (Backend Lead builds these)
│   │       └── api/
│   │           ├── router.py  internal.py
│   │
│   ├── core-service/                 # Backend Lead
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── main.py  config.py  database.py  deps.py  middleware.py
│   │       ├── websocket_manager.py    # ★ only service holding live WS connections
│   │       ├── event_listener.py
│   │       ├── clients/
│   │       │   ├── auth_client.py  ai_client.py
│   │       ├── domains/
│   │       │   ├── _template/
│   │       │   ├── portfolio/  research/  notification/
│   │       └── api/
│   │           ├── router.py  public.py  websocket.py
│   │
│   └── ai-service/                    # AI Lead — see Agents.md v4.1 for the design
│       ├── Dockerfile
│       └── app/
│           ├── main.py  config.py  database.py  deps.py  qdrant.py
│           ├── event_listener.py
│           ├── clients/
│           │   ├── auth_client.py  core_client.py
│           ├── ai/
│           │   ├── agents/                       # ★ now fully broken down, one file
│           │   │   ├── base.py                     #   per agent, every file real
│           │   │   ├── loop.py                      # generic ReAct loop (thought ->
│           │   │   │                                 # action -> observation -> ... ->
│           │   │   │                                 # finish) every data/analysis
│           │   │   │                                 # agent below runs on top of
│           │   │   ├── supervisor.py                 # dynamic agent selection
│           │   │   ├── market.py  news.py  sentiment.py  filing.py
│           │   │   ├── technical.py  fundamental.py  risk.py  macro.py
│           │   │   ├── portfolio.py                  # ★ NEW — only runs when the
│           │   │   │                                  #   analysis has 2+ holdings
│           │   │   ├── bull.py  bear.py               # multi-round, not one-shot
│           │   │   ├── judge.py                        # decides convergence each round
│           │   │   └── reflection.py                    # can request specific agents retry
│           │   ├── graph/
│           │   │   ├── state.py                      # participation tracking, debate
│           │   │   │                                  # rounds, reflection retry count
│           │   │   ├── builder.py                       # dynamic graph WITH cycles
│           │   │   ├── nodes.py  edges.py                # hard caps live in edges.py
│           │   ├── llm/
│           │   │   ├── base.py  router.py
│           │   │   └── openai_provider.py  anthropic_provider.py
│           │   │       gemini_provider.py  mistral_provider.py
│           │   ├── memory/
│           │   │   ├── episodic.py  manager.py
│           │   ├── rag/
│           │   │   ├── loader.py  parser.py  chunking.py  embedding.py
│           │   │   ├── indexing.py  citation.py
│           │   │   └── retriever.py                   # ★ BaseRetriever + business-
│           │   │                                        #   specific subclasses
│           │   │                                        #   (FilingRetriever, ...) —
│           │   │                                        #   ONE Qdrant collection,
│           │   │                                        #   many tailored retrievers
│           │   ├── recommendation/engine.py
│           │   ├── benchmark/
│           │   │   ├── comparator.py  evaluator.py
│           │   ├── evaluation/
│           │   │   ├── confidence.py  hallucination.py
│           │   ├── tools/                             # ★ Tool subclasses each agent's
│           │   │   ├── base.py                          #   loop calls into
│           │   │   ├── market_tool.py  news_tool.py
│           │   │   ├── filing_tool.py  macro_tool.py
│           │   │   └── portfolio_tool.py
│           │   ├── workflows/
│           │   │   └── analysis_workflow.py             # top-level entry point
│           │   └── schemas/
│           │       ├── refs.py  analysis.py  backtest.py
│           │       └── benchmark.py  debate.py  outputs.py  recommendation.py  retrieval.py
│           └── domains/
│               ├── _template/
│               ├── market/                              # ★ real fields now (Instrument,
│               │                                          #   PriceBar, TechnicalIndicatorCache)
│               └── analytics/                             # ★ real fields (PortfolioMetric,
│                                                            #   RecommendationMetric, ...)
│
├── tests/                             # ★ NEW — root-level, 4 independent pytest runs
│   ├── README.md                        # explains why 4 invocations, not 1
│   ├── auth_service/  core_service/  ai_service/    # unit tests, mocked clients/DB
│   └── integration/                                  # black-box HTTP tests against
│                                                        # a running docker-compose stack
│
└── frontend/                          # Frontend Lead — unchanged from v4.0
    └── ...
```


## Who owns what

| Person | Service(s) | Also owns |
|---|---|---|
| Backend Lead | `auth-service`, `core-service` | `infra/`, `docker-compose.yaml`, `.github/workflows/*-service-ci.yml`, Prometheus/Grafana |
| AI Lead | `ai-service` | — |
| Frontend Lead | `frontend/` | — |

Everyone reviews changes to `shared/` and `docs/MODULE_MAPPING.md` / `docs/ERD.md` / `Agents.md`.