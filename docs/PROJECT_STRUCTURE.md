# AlphaMind — Project Structure

The repository tree: directories and key files with their purpose. Not
listed: `.venv/`, caches (`__pycache__/`, `.mypy_cache/`), editor folders
(`.vscode/`), and the run-time folders `logs/`, `backups/` and `runs/`
(created by the stack and the scripts, ignored by git). The design behind
the tree is in `docs/DESIGN.md`; the handbook explains each part.

## 1. Top level

```
alphamind/
├── services/                the three FastAPI services (§2-§4)
├── frontend/                the Next.js web app, served by the gateway
│                            at / (§10)
├── shared/                  code every service imports (§5)
├── tests/                   one test suite per service + integration (§6)
├── infra/                   gateway, Postgres init, scheduler, object
│                            storage, monitoring, log shipping (§7)
├── scripts/                 host-side operator scripts (§8)
├── docs/                    design, API, ERD, disaster recovery, module
│                            mapping, this file; handbook/ (§9)
├── rag_seed_documents/      seed corpus for
│                            scripts/upload_seed_documents.py:
│   ├── analyst_report/
│   ├── earnings_call/
│   ├── filing/
│   ├── macro_research/
│   └── news/
├── .github/workflows/       one CI workflow per service, frontend CI,
│                            stack smoke and deploy (cloud host,
│                            docs/DEPLOYMENT.md)
├── docker-compose.yaml      the whole stack
├── docker-compose.override.yml  local development (source mounted into the
│                            containers, development image stage)
├── Makefile                 up/down/logs, certs, backup/restore, tests per
│                            service, promtool-test, lint (flake8 + mypy),
│                            clean, and short targets for scripts/
├── pyproject.toml, uv.lock  one dependency list for all services; pytest,
│                            mypy and isort configuration
├── .style.yapf              yapf layout settings (formatting helper)
├── .env.example             template for .env.docker (never committed)
└── .dockerignore, .gitignore
```

## 2. `services/auth-service/`

```
├── Dockerfile, alembic.ini
├── main.py, config.py, deps.py
├── database.py              engine, sessions, schema bootstrap at startup
├── security.py              password hashing, token issuing, TOTP
│                            helpers, Fernet encryption of stored TOTP
│                            secrets; limits for login, 2FA, reset,
│                            registration, invitations (on shared/redis.py)
├── events_out.py            event payload builders; events are published
│                            after the commit
├── user_state_sync.py       writes each user's live role and active flag to
│                            Redis (shared/user_state.py)
├── api/                     internal.py (/internal/*)
├── domains/                 api.py, model.py, repository.py, schema.py,
│   │                        service.py per domain
│   ├── auth/                users, profiles, settings, sessions, OAuth
│   │                        accounts, 2FA, avatar files
│   ├── admin/               /api/v1/admin: users, soft delete, statistics
│   ├── organization/        organizations, members, invitations
│   └── social/              friends (connections), presence
├── data/avatars/            empty folder; in compose the avatar_data
│                            volume is mounted at /app/data/avatars
├── migrations/              Alembic: env.py, script.py.mako,
│                            versions/ (f1160b000001 baseline,
│                            f1190b000001, f1210b000001)
└── scripts/                 set_system_role.py (the first admin)
```

## 3. `services/core-service/`

```
├── Dockerfile, alembic.ini
├── main.py, config.py, deps.py
│                            (deps.py: current user, from/to date-range
│                            check (422))
├── database.py              engine, sessions, schema bootstrap at startup
├── apikeys.py               API key model and functions; public API
│                            middleware: key check, owner state, per-key
│                            and per-owner rate limits
├── event_listener.py        auth and ai events -> notifications and
│                            WebSocket pushes; document status (durable)
├── websocket_manager.py     WebSocketEnvelope, sockets per channel,
│                            cross-replica fan-out; socket close codes and
│                            the handshake helpers of both endpoints
├── yjs_relay.py             Yjs rooms, body write-back, active-room markers
├── job_health.py            scheduled-job heartbeat and market-close skip
│                            gauges, kept in Redis across restarts
├── outbox.py                transactional outbox (core.event_outbox) for
│                            the durable events, and its dispatcher
├── api/
│   ├── internal.py          /internal/*: scheduler jobs, heartbeats,
│   │                        portfolios, stats, tracked tickers, ingestion
│   │                        and system documents
│   ├── public.py            /api/v1/public/* (API key)
│   ├── apikeys_api.py       /api/v1/api-keys/*
│   └── websocket.py         /ws/{channel} and /ws/yjs/{note_id}
├── clients/                 auth_client.py, ai_client.py
├── domains/
│   ├── portfolio/           portfolios, transactions (ledger), holdings,
│   │                        snapshots, watchlists; ledger.py (pure ledger
│   │                        math)
│   ├── research/            notes, comments, tags, documents;
│   │                        moderation routes (in api.py), sentiment.py,
│   │                        yjs_guard.py (Yjs updates decoded in worker
│   │                        processes)
│   └── notification/        notifications; service.py holds the one path
│                            every core notification goes through
├── migrations/              env.py, script.py.mako, versions/
│                            (f1160c000001 baseline, f1170c000001,
│                            f1200c000001 event_outbox,
│                            f1250c000001 status_changed_at)
└── scripts/                 e2e_check.py (end to end through the gateway),
                             backfill_sentiment.py, delete_documents.py,
                             recompute_snapshots.py
```

## 4. `services/ai-service/`

```
├── Dockerfile, alembic.ini
├── main.py, config.py, deps.py
├── database.py              engine, sessions, schema bootstrap at startup
├── event_listener.py        durable: document uploaded/deleted, portfolio
│                            deleted
├── ai_events.py             event payload builders + best-effort publisher
├── observability.py         Prometheus metrics (analyses, queue, LLM errors)
├── qdrant.py                Qdrant client and collection check
├── api/                     internal.py (/internal/*)
├── clients/                 core_client.py, auth_client.py
├── domains/                 persistence and HTTP, one folder per context
│   ├── analysis/            analyses, agent runs, debate, reflections,
│   │                        recommendations, tiers; queue.py (the serial
│   │                        analysis queue)
│   ├── decision/            investment decisions, outcomes, reviews
│   ├── backtest/            backtests, metrics, benchmarks
│   ├── analytics/           recommendation and agent metrics; dashboard.py
│   ├── rag/                 document_chunks, embedding_jobs; the RAG
│   │                        assistant (api.py, qa.py)
│   ├── memory/              episodic and procedural memory rows
│   ├── knowledge/           agent reports, knowledge graph, ticker names
│   ├── filings/             ai.filing_records (structured filing records)
│   └── market/              instruments, price bars (the cache), prices API
├── ai/                      the AI logic (only domains/ touches the DB)
│   ├── agents/              one file per role (filing, fundamental, macro,
│   │                        market, news, portfolio, risk, sentiment,
│   │                        technical), debate_common.py (Bull and Bear),
│   │                        judge.py, reflection.py, loop.py (the ReAct
│   │                        loop and BaseAgent), finish_checks.py,
│   │                        evidence_grounding.py
│   ├── graph/               LangGraph: state, builder (incl. routing),
│   │                        nodes (incl. the agent registry and selection)
│   ├── llm/                 router.py (incl. capacity), base.py (incl. the
│   │                        concurrency gate), adapters
│   │                        (google, mistral, openai, anthropic),
│   │                        json_fence.py
│   ├── providers/           yahoo_finance, sec_edgar, fred, newsapi,
│   │                        tavily, news_filters, news_relevance,
│   │                        registry, base (incl. retry)
│   ├── tools/               agent tools, indicators.py,
│   │                        market_data_cache.py, web_search_tool.py
│   │                        (incl. the result classifier)
│   ├── rag/                 chunking (incl. parsing), embedding, indexing,
│   │                        retriever (incl. query expansion and fusion),
│   │                        visibility, category_weights.json
│   ├── knowledge/           archival, KG extraction and context, filing
│   │                        summaries and records, ticker names, checks on
│   │                        recycled and prior-note content
│   ├── ingestion/           sec_edgar_filings.py, digests.py (news and
│   │                        macro research digests)
│   ├── memory/              agent_memory.py (L2 episodic, L4 procedural)
│   ├── recommendation/      engine.py (incl. the position limit),
│   │                        position_sizing.py
│   ├── benchmark/           metrics.py (returns, drawdown, Sharpe, ...)
│   ├── evaluation/          rag_recall.py (golden set and recall)
│   ├── user_sentiment.py    sentiment of notes and comments
│   └── workflows/           analysis, RAG ingestion, auto ingestion, tier
│                            monitoring, review, and the three backtests
│                            (backtest_workflow.py)
├── migrations/              env.py, script.py.mako, versions/
│                            (f1160a000001 baseline, f1170a000001,
│                            f1210a000001, f1220a000001, f1230a000001,
│                            f1240a000001, f1250a000001)
└── scripts/                 analysis_cli.py (used by run_analysis.sh),
                             e2e_tier_check.py, smoke_check.py,
                             upload_seed_documents.py, delete_analyses.py,
                             RAG evaluation (generate_rag_golden_set.py,
                             measure_rag_recall.py incl. --sweep),
                             reembed_collection.py, filing tools
                             (backfill_filing_records.py,
                             regenerate_filing_summaries.py),
                             verify_portfolio_backtest.py,
                             probe_model_latency.py,
                             _text.py (output helpers)
```

## 5. `shared/`

```
├── config.py                BaseServiceSettings (each service subclasses it)
├── database.py              Base (naming convention), column mixins
│                            (UUIDPKMixin, TimestampColumnsMixin), engine
│                            and session factories, get_db, schema
│                            bootstrap at startup (upgrade only)
├── security.py              CurrentUser, JWT verification, permissions
├── permissions.py           system role -> permissions map
├── user_state.py            live role and active flag (Redis + auth);
│                            organization roles of a user, cached
├── scheduled_jobs.py        job_response: 500 when every unit of a step
│                            failed
├── events.py                pub/sub and durable events, event names and
│                            owners
├── redis.py                 Redis client, fixed-window limiter and 429
│                            response, lock shared by the end-to-end
│                            scripts
├── http_client.py, storage.py (S3), email.py, concurrency.py
├── market_calendar.py       NYSE trading days and holidays
├── exceptions.py            error hierarchy and error body
├── logging.py               structured logs (structlog), request id
│                            middleware (logs, error body, X-Request-ID
│                            header), get_or_create for Prometheus
│                            collectors
├── enums.py                 shared StrEnums (analysis, connection,
│                            document, market, notification, oauth,
│                            recommendation, role, workflow)
└── schemas.py               APIResponse, PaginatedResponse, cross-service
                             refs, the ticker rule
```

## 6. `tests/`

```
├── auth-service/            conftest.py, helpers.py, test_accounts.py,
│                            test_organizations.py, test_platform.py
├── core-service/            conftest.py, helpers.py, test_platform.py,
│                            test_portfolio.py, test_realtime.py,
│                            test_research.py
├── ai-service/              conftest.py, helpers.py and 14 test_*.py files
│                            (agents, API, data agents, debate and Judge,
│                            filings, graph, knowledge, LLM, market data,
│                            news, RAG, scripts, tools, workflows)
├── integration/             conftest.py, test_rag_recall.py (needs the
│                            running stack with the real corpus)
├── fixtures/                rag_golden_set.json
├── alembic_contract.py      migration-versus-model checks, star-imported by
│                            one test file per service
├── auth_tokens.py           access tokens for the tests
└── README.md                how to run each suite
```

The suites run separately (`make test-auth`, `test-core`, `test-ai`): the
services share top-level module names (`config`, `database`, `main`), so one
process cannot import two of them.

## 7. `infra/`

```
├── gateway/                 nginx.conf (HTTPS server and the internal
│                            8080 listener), api_locations.conf (API and
│                            health locations of both), gen-certs.sh
│                            (self-signed TLS),
│                            status.html + csp_hashes.py (its CSP hashes),
│                            certs/ (generated, only README.md tracked)
├── postgres/init-schemas.sql   the three schemas
├── scheduler/               Dockerfile, entrypoint.sh, crontab,
│                            call-job.sh (the seven HTTP jobs:
│                            market-open, market-close, tier-monitoring,
│                            review-run, rag-ingest, purge-unverified,
│                            notification-retention), logs-cleanup.sh,
│                            heartbeat.sh, backup.sh, restore.sh,
│                            s3-remote.sh (rclone remote for the
│                            object storage's S3 API)
├── drill/docker-compose.drill.yml   compose override of the restore drill
├── objectstore/             SeaweedFS entrypoint.sh and healthcheck.sh
├── prometheus/              prometheus.yml, alert_rules.yml,
│                            alert_rules_test.yml
├── alertmanager/alertmanager.yml
├── grafana/provisioning/    datasources/datasource.yml,
│                            dashboards/ (dashboard.yml, two dashboards)
└── vector/vector.toml       log shipping into ./logs
```

## 8. `scripts/`

```
├── lib.sh                   shared helpers (compose wrapper, busy windows)
├── run_analysis.sh          real analyses with a per-run output folder
├── token.sh                 a short-lived access token
├── e2e.sh, smoke.sh         end-to-end and smoke checks (make e2e/smoke)
├── tiers.sh                 recommendation tiers of a portfolio
├── snapshots.sh             recompute portfolio snapshots
├── health.sh, logs.sh, rebuild.sh, probe.sh
├── psql.sh, redis-cli.sh, qdrant.sh, objectstore.sh   data store shells
├── backups.sh               list, make, check and drill backups
└── deploy.sh                deploy main on the cloud host (run there)
```

## 9. `docs/`

```
├── DESIGN.md                the system as built
├── API_SPEC.md              routes and events
├── ERD.md                   tables per schema
├── DISASTER_RECOVERY.md     backups and restore
├── DEPLOYMENT.md            the cloud host and continuous deployment
├── MODULE_MAPPING.md        code mapped to the subject's modules
├── PROJECT_STRUCTURE.md     this file
└── handbook/                00-index.md and one chapter per part
                             (01-02 overview and operations, 10-19
                             services and AI, 20-31 data stores,
                             infrastructure, shared code, testing)
```

## 10. `frontend/`

```
├── Dockerfile               Node 24, Next.js standalone build, port 3000
├── package.json, package-lock.json   Next.js 16, React 19, yjs
├── next.config.ts, tsconfig.json, eslint.config.mjs, postcss.config.mjs
├── .env.example             settings for `npm run dev` (API through
│                            https://localhost:8443)
├── proxy.ts                 refreshes an access token that is about to
│                            expire before a page runs
└── app/
    ├── layout.tsx, page.tsx, globals.css, site-footer.tsx (Privacy
    │                        Policy and Terms links), not-found.tsx,
    │                        global-error.tsx
    ├── healthz/route.ts     GET /healthz: 200 only when the API answers
    ├── lib/                 backend.ts (API calls), session.ts and
    │   │                    session-cookies.ts (httpOnly cookies), me.ts
    │   │                    (permissions), ws-url.ts, api-error.ts,
    │   │                    format.ts
    │   └── actions/         Server Actions per area (auth, profile,
    │                        twofa, portfolios, research, documents,
    │                        organizations, admin, moderation, ...)
    ├── (public)/            pages without login: login, register,
    │                        forgot-password, reset-password,
    │                        verify-email, invitations/accept,
    │                        oauth/complete
    └── (protected)/         pages that need a login (layout.tsx checks the
                             session; sidebar and mobile navigation):
                             portfolios, watchlists, analyses, analytics,
                             research (notes, collaborative editor, ask,
                             documents), notifications, friends,
                             organizations, profile, admin (users,
                             moderation)
```
