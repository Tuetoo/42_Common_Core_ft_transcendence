# AlphaMind — Project Structure

Rewritten from scratch against the actual repository tree (verified via a full
file listing, not carried over from an earlier plan). Directories that don't
exist yet, or exist but are empty, are called out explicitly rather than
described as if built.

## 1. Top level

```
alphamind/
├── services/           three independently deployable FastAPI services
├── shared/             code imported by all three services (see §5)
├── tests/              one subdirectory per service + integration (see §6)
├── infra/              nginx gateway, Postgres init, scheduler, monitoring
├── frontend/           
├── docs/               this document and its siblings
├── docker-compose.yaml
├── docker-compose.override.yml   local-dev-only overrides
├── pyproject.toml      single root dependency list + pytest config, shared
│                       by all three services (see docs/ROADMAP.md for why)
├── uv.lock
├── Makefile            make test / test-auth / test-core / test-ai /
│                       test-integration -- see tests/README.md for why
│                       each is a separate invocation
└── alphamind.code-workspace   VSCode multi-root workspace; see its sibling
                        per-folder .vscode/settings.json files -- needed
                        because the three services share top-level module
                        names (config.py, database.py, main.py, ...) and a
                        single flat editor view can't resolve imports
                        correctly across all of them at once
```

## 2. `services/ai-service/`

```
services/ai-service/
├── main.py                 FastAPI app, lifespan (schema bootstrap, Qdrant
│                           bucket check), CORS, exception handlers, metrics
├── config.py, database.py, db_bootstrap.py, deps.py
├── event_listener.py       subscribes to DOCUMENT_UPLOADED (from
│                           core-service) -- drives RAG ingestion
├── qdrant.py                Qdrant client singleton
├── clients/                 core_client.py -- sync
│                           service-to-service HTTP calls
├── scripts/
│   ├── smoke_test_mistral.py       run manually, not in CI
│   └── generate_rag_golden_set.py  run manually, not in CI (needs real
│                                   indexed Qdrant data + an LLM key)
├── api/
│   ├── router.py            mounts every domain's router (see below)
│   └── internal.py          /internal/* -- service-to-service + scheduler
│                           hooks only, not proxied by the gateway
├── domains/                 persistence + HTTP layer, one folder per
│   │                       bounded context, each with the same 5-file
│   │                       shape (model.py, schema.py, repository.py,
│   │                       service.py, api.py) except where noted
│   ├── analysis/            analyses, recommendations, debate, reflections,
│   │                       agent run/result records
│   ├── decision/            investment_decisions, decision_outcomes,
│   │                       decision_reviews (singular name -- every domain
│   │                       here is singular; this one was briefly plural
│   │                       and got renamed for consistency)
│   ├── backtest/            backtests, positions, trades, metrics,
│   │                       benchmarks, benchmark_results
│   ├── rag/                 document_chunks, embedding_jobs, rag_collections,
│   │                       retrieval_logs (no service.py/api.py -- this
│   │                       domain is Postgres bookkeeping only; the actual
│   │                       RAG pipeline lives in ai/rag/, not here)
│   ├── memory/              episodic_memories, agent_memories,
│   │                       memory_references (no service.py/api.py --
│   │                       written and read by ai/memory/ and the graph
│   │                       nodes directly, not exposed as its own API)
│   ├── analytics/            portfolio_metrics, recommendation_metrics,
│   │                       agent_metrics, portfolio_evaluations -- mostly
│   │                       read-side; only recommendation_metrics is
│   │                       actually written to (by review_workflow.py)
│   └── market/               instruments, price_bars,
│                             technical_indicator_cache -- modeled, but the
│                             write path (refresh from the provider) isn't
│                             implemented yet; agents call
│                             ai/providers/yahoo_finance.py directly instead
│                             of through this cache layer
└── ai/                       the actual AI/agent logic -- no persistence
    │                        code lives here, only domains/ touches the DB
    ├── agents/               one file per agent role (market, news,
    │                        sentiment, filing, technical, fundamental,
    │                        risk, macro, portfolio, bull, bear, judge,
    │                        reflection) + supervisor.py (dynamic fan-out)
    │                        + loop.py (shared ReAct loop) + registry.py
    ├── graph/                LangGraph wiring: state.py (AnalysisState),
    │                        builder.py, nodes.py, edges.py, naming.py
    ├── llm/                  provider-agnostic router + one adapter per
    │                        vendor (anthropic, openai, google, mistral)
    ├── providers/            external data sources: yahoo_finance.py,
    │                        sec_edgar.py, fred.py, newsapi.py,
    │                        tavily.py, news_relevance.py (the
    │                        Bloomberg/TradingView/Finviz/Macrotrends
    │                        stubs were removed in round111: no
    │                        free/official API, never wired in)
    ├── tools/                agent-callable wrappers around providers +
    │                        indicators.py (SMA/EMA/RSI/MACD/Bollinger/ATR,
    │                        pure functions, no provider dependency)
    ├── rag/                  chunking, embedding, indexing, fusion (RRF),
    │                        query_expansion, retriever, loader, parser --
    │                        the actual retrieval pipeline
    ├── memory/               episodic.py (L2 read/write), procedural.py
    │                        (L4 read/write + combine_context helper)
    ├── benchmark/            metrics.py -- pure Sharpe/Sortino/drawdown/
    │                        win-rate/alpha/beta math, no DB, no network;
    │                        shared by backtest_workflow.py and (once built)
    │                        a live-portfolio-evaluation workflow
    ├── evaluation/           golden_set.py + recall.py -- RAG retrieval
    │                        quality measurement, see docs/ERD.md's note on
    │                        tests/integration/test_rag_recall.py
    ├── recommendation/       engine.py -- final buy/hold/sell + confidence
    │                        assembly from the debate + reflection outcome
    └── workflows/            the actual background-task entry points HTTP
                             handlers hand off to: analysis_workflow.py,
                             rag_ingestion_workflow.py, backtest_workflow.py,
                             review_workflow.py
```

**Not present**: `ai/schema/` was an empty leftover directory (never used --
every domain's Pydantic schemas live in its own `domains/*/schema.py`
instead) and has been deleted rather than filled in.

## 3. `services/core-service/`

```
services/core-service/
├── main.py, config.py, database.py, db_bootstrap.py, deps.py
├── middleware.py            PublicAPIRateLimitMiddleware -- the only piece
│                           of request handling that runs outside the
│                           normal dependency-injected DB session (see
│                           docs/ERD.md's note on why this matters for tests)
├── apikeys.py                plain functions (create/get/list/revoke), not
│                           a domains/ package -- deliberately small,
│                           doesn't need the full 5-file shape
├── event_listener.py          publishes DOCUMENT_UPLOADED; subscribes to
│                           DOCUMENT_STATUS_CHANGED (from ai-service)
├── websocket_manager.py, yjs_relay.py
├── clients/                  ai_client.py, auth_client.py
├── api/
│   ├── router.py, internal.py
│   ├── public.py             /api/v1/public/* -- API-key-authenticated
│   │                        developer surface, see docs/API_SPEC.md §5
│   ├── apikeys_api.py         /api/v1/api-keys/* -- issuing/managing keys
│   │                        (distinct from public.py, which *uses* them)
│   ├── websocket.py           generic /ws/{channel} relay
│   └── yjs_websocket.py       /ws/yjs/{note_id} -- Yjs CRDT binary relay
└── domains/
    ├── portfolio/             portfolios, holdings, transactions, snapshots,
    │                        watchlists -- decision tracking that used to
    │                        live here has moved to ai-service's decision/
    │                        domain, see docs/ERD.md §3
    ├── research/               notes, comments, tags, documents (MinIO-backed
    │                        storage, category/ticker fields for RAG)
    └── notification/           in-app notifications
```

## 4. `services/auth-service/`

```
services/auth-service/
├── main.py, config.py, database.py, db_bootstrap.py, deps.py
├── security.py               password hashing (passlib/bcrypt -- pinned
│                           below bcrypt 4.1, see pyproject.toml) + TOTP
├── api/router.py, api/internal.py
└── domains/
    ├── auth/                  users, profiles, settings, sessions, OAuth
    │                        accounts, 2FA secrets -- registration, login,
    │                        token refresh, password reset, avatar upload
    ├── organization/           organizations, members, custom roles,
    │                        invitations
    └── social/                 connections (friend/follow-style requests)
```

## 5. `shared/`

Code imported by all three services -- none of it is itself deployable.

```
shared/
├── config.py                BaseServiceSettings -- each service's own
│                           config.py subclasses this
├── database.py               Base, create_service_engine, create_session_
│                           factory, make_get_db -- see docs/ERD.md §1 for
│                           the cross-schema-FK rule this enforces, and the
│                           search_path fix (server_settings, not a connect
│                           event) this file's docstring explains
├── security.py               CurrentUser + JWT verification -- every
│                           service decodes/verifies its own tokens; only
│                           auth-service issues them
├── events.py, events_registry.py    Redis pub/sub cross-service events,
│                           see docs/API_SPEC.md §6 for the full event table
├── redis.py, http_client.py, concurrency.py, storage.py (MinIO/S3), email.py
├── exceptions.py              AppError hierarchy + EXCEPTION_STATUS_MAP,
│                           looked up via MRO walk (a subclass inherits its
│                           nearest registered ancestor's HTTP status) --
│                           not an exact-type-only dict lookup
├── lifecycle.py, logging.py, models.py (UUIDPKMixin etc.)
├── enums/                    one file per enum family (analysis, connection,
│                           document, market, notification, oauth,
│                           recommendation, role, workflow)
└── schemas/                  base.py (APIResponse, PaginatedResponse --
                             note PaginatedResponse's field is `items`, not
                             `data`; only single-object APIResponse uses
                             `data`), refs.py (cross-service reference DTOs),
                             websocket.py
```

## 6. `tests/`

```
tests/
├── auth-service/    conftest.py (testcontainers Postgres+Redis, session-
│                   scoped schema fixture) + test_auth.py, test_organization.py,
│                   test_social.py
├── core-service/     conftest.py (same pattern) + test_apikeys.py,
│                   test_portfolio.py, test_research.py, test_notification.py
├── ai-service/       conftest.py (same pattern) + test_agent_loop.py,
│                   test_agents.py, test_analysis_domain.py, test_graph.py,
│                   test_llm_router.py, test_providers.py, test_rag.py,
│                   test_tools.py
└── integration/      conftest.py (assumes `docker compose up` already
                     running, no testcontainers) + test_rag_recall.py --
                     the RAG retrieval-quality gate, see docs/ERD.md;
                     currently skips (no golden set generated yet)
```

Each service's tests are a separate `pytest` invocation (`make test-auth`,
`make test-core`, `make test-ai`) rather than one combined run -- the three
services share top-level module names (`config`, `database`, `main`, ...),
so collecting them together in one process causes import collisions.
`make test` runs all three sequentially. `make test-integration` is separate
again, and requires real infrastructure already running.

## 7. `infra/`

```
infra/
├── gateway/nginx.conf         routes /api/v1/* to the right service by
│                            path prefix, proxies /ws/* for WebSocket
│                            upgrade -- does not touch JWTs itself
├── postgres/init-schemas.sql  creates the three schemas (auth/core/ai) on
│                            first container start
├── scheduler/                a small standalone container: crontab +
│                            market-open.sh / market-close.sh, hits
│                            core-service's /internal/scheduler/* endpoints
├── prometheus/prometheus.yml
└── grafana/provisioning/       datasource + one dashboard
```

## 8. What's genuinely not built yet

Listed explicitly rather than left to be discovered by absence:

- **`frontend/`** — directory scaffolding only, no code in any of it.
- **`domains/market/service.py`'s cache-refresh path** (ai-service) — models
  exist, nothing populates them from the provider yet.
- **A live-portfolio-evaluation workflow** writing to
  `ai.portfolio_evaluations` — `ai/benchmark/metrics.py` is written to serve
  it, but the workflow itself doesn't exist; only backtesting
  (`ai.backtests`) does.
- **`ai.agent_metrics` rollup** — table exists, nothing aggregates into it.
- **`ai.decision_reviews`** — table exists, nothing writes `review_text`/
  `lessons_learned` into it (would need an LLM call to generate that text, a
  design decision not yet made).
- **A cron entry actually calling `/internal/review/run`** — the endpoint
  exists (see docs/API_SPEC.md §4), no schedule is wired up yet.
- **A real, measured RAG recall baseline** — the framework
  (`ai/evaluation/`, `tests/integration/test_rag_recall.py`) is built and
  its logic is verified, but the actual baseline numbers need a real run
  against indexed data (`scripts/generate_rag_golden_set.py`) which hasn't
  happened yet.