# 13. ai-service overview

> ai-service is the "brain" of AlphaMind. It runs the multi-agent analysis
> of a US stock (nine data agents, a Bull/Bear debate, a Judge, a
> Reflection check, a recommendation with buy/sell tiers), answers
> questions over the research documents (RAG), stores market data,
> scores past recommendations, and runs backtests. It is a FastAPI
> application in `services/ai-service/`. This chapter is the map: the
> folder layout, the HTTP API, the analysis queue, what happens at
> startup, the external data providers, the configuration groups and the
> operator scripts. The next chapters go deeper: the LLM layer
> ([14](14-ai-llm-layer.md)), the agents ([15](15-ai-agents.md)) and the
> analysis pipeline ([16](16-ai-analysis-pipeline.md)).

## 1. The big picture

ai-service sits behind the gateway like the other two Python services.
Browsers reach its public routes under `/api/v1/...`. Other services
(core-service, the scheduler) reach its internal routes under
`/internal/...` on the Docker network only.

```
 Browser / frontend                          scheduler container (cron, New York time)
        |                                            |
        v                                            |  POST /internal/review/run
   gateway (nginx)                                   |  POST /internal/scheduler/tier-monitoring
        |  /api/v1/analyses, /market, /analytics,    |  POST /internal/scheduler/rag-ingest
        |  /decisions, /backtests, /rag              |
        v                                            v
 +------------------------------------------------------------------+
 | ai-service (FastAPI, uvicorn, one process, port 8000)            |
 |                                                                  |
 |  main.py        -> domains/*/api.py   (public, JWT)              |
 |  api/internal.py                      (internal, X-Internal-Token)|
 |                                                                  |
 |  domains/analysis/queue.py: ONE serial FIFO worker               |
 |        |                                                         |
 |        v                                                         |
 |  ai/workflows/analysis_workflow.py -> ai/graph (LangGraph)       |
 |        agents (ai/agents) -> tools (ai/tools) -> providers       |
 |        LLM calls -> ai/llm (router, Google key pool)             |
 |                                                                  |
 |  event_listener.py: durable Redis consumer (document.uploaded,   |
 |                     document.deleted, portfolio.deleted)         |
 +------------------------------------------------------------------+
      |            |            |             |               |
      v            v            v             v               v
  PostgreSQL     Redis       Qdrant     object storage   external APIs
  (schema ai)  (events,    (vectors)   (S3 API,         Gemini, Yahoo Finance,
               cache)                   documents)      FRED, NewsAPI, SEC EDGAR,
                                                        Tavily
```

Three ideas explain most of the design:

1. **Analyses are slow and run in the background.** One analysis takes
   several minutes because it makes dozens of LLM calls. The create
   request returns at once with `status: "pending"`. A single in-process
   worker then runs the analyses one at a time.
2. **The LLM provider is the scarce resource.** AlphaMind uses Google
   Gemini on free-tier quotas (a few requests per minute and per day per
   Google Cloud project). Most of the reliability code (chapter 14)
   exists because of that.
3. **Every agent must prove its numbers.** The agents run a ReAct loop
   (chapter 15) and each final answer is checked in code against the
   tool results of the same run.

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| FastAPI | A Python web framework for HTTP APIs built on `async` functions. | Every route in ai-service is a FastAPI route. |
| uvicorn | The ASGI server that runs a FastAPI app. | The container runs `uvicorn main:app --port 8000` (`services/ai-service/Dockerfile`), one process. |
| Lifespan | A FastAPI hook that runs code once at startup (before `yield`) and once at shutdown (after `yield`). | `main.py::lifespan` prepares the database, Qdrant, the event listener and the queue. |
| JWT | JSON Web Token: a signed token that says who the user is. auth-service issues it. | Public routes read the user from the JWT (`shared.security`). |
| Internal token | A shared secret sent as the `X-Internal-Token` header. | Every `/internal/*` route requires it (`api/internal.py`, `require_internal_token`). |
| Serial queue | A first-in, first-out (FIFO) list of jobs handled by exactly one worker. | All analyses run strictly one after another (`domains/analysis/queue.py`). |
| Advisory lock | A PostgreSQL lock on a number, not on a row. `pg_advisory_xact_lock` holds it until the transaction ends. | Makes the "is the queue full?" check and the insert one atomic step. |
| LangGraph | A library that runs a graph of async steps ("nodes") over a shared state. | The analysis is a LangGraph graph (`ai/graph/builder.py`). |
| ReAct loop | Reason + Act: the model writes a thought, picks a tool (action), reads the result (observation), and repeats until it gives a final answer. | Every data agent is a ReAct loop (`ai/agents/loop.py`). |
| Tool | A small Python class an agent can call by name, with JSON arguments. It returns plain text. | `ai/tools/*.py`. |
| Provider | A client for one external data source (Yahoo, FRED, ...). | `ai/providers/*.py`; tools call providers, never the reverse. |
| RAG | Retrieval-augmented generation: find relevant text chunks first, then give them to the LLM. | Filing search, prior agent notes, the RAG assistant (chapter 17). |
| Qdrant | A vector database. It stores one embedding vector per text chunk. | Collection `research_documents_gemini` by default (`config.py`). |
| BM25 | A classic keyword ranking formula. | ai-service keeps an in-memory BM25 index next to Qdrant and rebuilds it at startup. |
| Durable subscriber | A Redis Streams consumer group: messages stay in Redis until the consumer acknowledges them. | `event_listener.py` uses it so an upload event is not lost during a restart. |
| Tier | One priced, sized buy or sell order suggestion of a recommendation (side, target price, quantity). | Created after a BUY/SELL verdict when the analysis has a portfolio. |

## 3. How it works in AlphaMind

### 3.1 Folder layout

All paths are relative to `services/ai-service/`.

| Folder / file | What lives there |
|---|---|
| `main.py` | The FastAPI app, the lifespan (startup/shutdown), `/health`, `/metrics`, router mounting (`api_router`: the public domain routers under `/api/v1`). |
| `config.py` | `AIServiceSettings`: every ai-service setting with its default (section 4). |
| `api/internal.py` | The `/internal/*` routes (unversioned, not proxied by the gateway). |
| `domains/` | Classic web layers per domain: `api.py` (routes), `schema.py` (Pydantic request/response models), `model.py` (SQLAlchemy tables), `repository.py` (queries), `service.py` (business logic). Domains: `analysis`, `analytics`, `backtest`, `decision`, `filings` (filing records table), `knowledge`, `market`, `memory`, `rag`. |
| `domains/analysis/queue.py` | The serial analysis queue and the admission check. |
| `ai/agents/` | The nine data agents, Bull, Bear, Judge, Reflection, the ReAct loop and the evidence checks (chapter 15). |
| `ai/graph/` | The LangGraph graph: `builder.py` (wiring and routing decisions), `nodes.py` (node functions, the agent registry and the Supervisor's agent selection), `state.py` (the shared `AnalysisState`, node names per role, the debate and reflection caps). Chapter 16. |
| `ai/llm/` | LLM adapters (Google, Mistral, OpenAI, Anthropic), the router with tiers and one fallback model per tier, request pacing, the helpers that parse JSON out of LLM replies (`base.py`). Chapter 14. |
| `ai/providers/` | Clients for external data: Yahoo Finance, FRED, NewsAPI (with its source catalog), SEC EDGAR (fundamentals, filing periods, the XBRL latest-quarter line), Tavily; a small retry helper; `news_relevance.py` (query building, company aliases, structural pre-drops, consent-link relinking, and the LLM relevance classifier with its subject/kind enums). |
| `ai/tools/` | Agent tools that wrap providers, the RAG retriever, core-service and the knowledge graph; the cache-first price store (`market_data_cache.py`); indicator math and the backtest metrics (returns, drawdown, Sharpe, ...) shared by the backtests (`indicators.py`). |
| `ai/rag/` | Chunking, embeddings, Qdrant + BM25 indexing, hybrid retrieval with Reciprocal Rank Fusion, visibility rules. Chapter 17. |
| `ai/knowledge/` | Archival of agent reports into RAG, knowledge-graph extraction and KG context, filing summaries and structured filing records (`filing_records.py`, table `ai.filing_records`), recycled-content and prior-note-claim detection, ticker names. Chapter 18. |
| `ai/memory/` | Episodic memory (track record per ticker) and procedural memory (good tool-call patterns per agent role). Chapter 18. |
| `ai/recommendation/` | The final recommendation and the single-position limit (`engine.py`), position sizing into tiers (`position_sizing.py`). Chapter 16. |
| `ai/ingestion/` | Daily ingestion helpers: SEC filings (via `edgartools`; summary and records calls), a news digest, a monthly macro note. |
| `ai/workflows/` | Long-running jobs with their own DB session: the analysis workflow, RAG ingestion, auto-ingestion, review, tier monitoring, three backtest workflows. Chapter 19. |
| `ai/evaluation/` | One module, `rag_recall.py`: the RAG golden set model and loader, and the one Recall@K / MRR scoring code (`rank_of`, `Score`, `embed_golden_questions` with the `query` embedding purpose) shared by `scripts/measure_rag_recall.py` and the integration-test floor. |
| `ai/user_sentiment.py` | Sentiment of a user's research note or comment, for core-service. |
| `clients/` | HTTP clients for core-service (`core_client.py`: holdings, cash, snapshots, documents, transactions) and auth-service (`auth_client.py`: organization membership). |
| `deps.py` | Access checks: `ensure_portfolio_access`, `ensure_portfolio_owner`, `ensure_analysis_access`. |
| `event_listener.py` | Handlers for the durable Redis events. |
| `ai_events.py` | Payload builders and a best-effort publisher for ai-service events. |
| `observability.py` | ai-service Prometheus metrics. |
| `qdrant.py`, `database.py` | Qdrant client and collection check, SQLAlchemy engine/session, Alembic upgrade at startup. |
| `migrations/` | Alembic revisions for the `ai` schema (chapter 20). |
| `scripts/` | Operator and maintenance scripts, run inside the container (section 3.7). |

### 3.2 The public API

`main.py` mounts six domain routers on `api_router` and adds it under
`settings.API_V1_PREFIX` (`/api/v1`, from `shared/config.py`). The
gateway forwards these prefixes to ai-service (`infra/gateway/nginx.conf`:
`location /api/v1/analyses`, `/api/v1/market`, `/api/v1/analytics`,
`/api/v1/decisions`, `/api/v1/backtests`, `/api/v1/rag`). ai-service
mounts the market router at `/api/v1/market/instruments`; the gateway
forwards everything under `/api/v1/market`, which includes that path.

| Prefix | Router file | Routes (from the decorators) |
|---|---|---|
| `/api/v1/analyses` | `domains/analysis/api.py` | `POST ""` (create, 201), `GET ""` (my analyses, paginated: own analyses only, newest first; system analyses on the caller's portfolio appear in analytics `scope=mine` (CSV `analysis_id`) and can be opened by id, not here), `GET /{analysis_id}` (full detail), `GET /{analysis_id}/recommendation`, `GET /{analysis_id}/tiers`, `GET /{analysis_id}/debate`, `GET /{analysis_id}/reflection`, `POST /{analysis_id}/decisions` (201) |
| `/api/v1/decisions` | `domains/decision/api.py` | `GET ""`, `GET /{decision_id}/outcome`, `POST /{decision_id}/review` (201, LLM critique that replaces the stored review and publishes `decision.reviewed`, a notification to the user; 503 with `Retry-After: 60` and nothing stored or published when the LLM call fails), `GET /{decision_id}/review` |
| `/api/v1/backtests` | `domains/backtest/api.py` | `POST ""` (a `strategy_name` reserved for the other two kinds is a 422), `POST /recommendation-replay`, `POST /portfolio`, `GET ""` (the caller's own backtests, newest first, `limit`/`offset`), `GET /{backtest_id}`, `GET /{backtest_id}/metrics`, `DELETE /{backtest_id}` (204; publishes `backtest.deleted`, a notification to the owner). The three create routes answer 429 when the caller already has `BACKTEST_MAX_ACTIVE_PER_USER` (2, a module constant) backtests pending or running (log `backtest_rejected_limit`; counted under a per-user advisory lock in the creating transaction, so two parallel creates cannot both pass). |
| `/api/v1/market/instruments` | `domains/market/api.py` | `GET /{ticker}/prices` (JWT required; `interval` `1d`, `1wk` or `1mo`, default `1d`; `lookback_days` default 5, 1..3650 (`MAX_PRICE_LOOKBACK_DAYS` in `domains/market/repository.py`); newest bar first). There is no instrument-metadata route. |
| `/api/v1/analytics` | `domains/analytics/api.py` | `GET /agents`, `GET /overview`, `GET /timeseries`, `GET /export.csv` |
| `/api/v1/rag` | `domains/rag/api.py` | `POST /ask`, `POST /ask/stream` (Server-Sent Events) |

The public route and its internal twin
(`/internal/market/instruments/{ticker}/prices`, section 3.3) share the
path shape and one implementation (`price_bars_response`). Both read the
price cache (section 3.6), which reads up to `CACHED_BARS_READ_LIMIT`
(3652) bars per series, enough for the longest allowed daily window.

The create route (`create_analysis` in `domains/analysis/api.py`):

1. Requires a logged-in user with permission to create analyses
   (`get_analysis_writer`). The owner always comes from the token; a
   `user_id` in the body is ignored.
2. If a `portfolio_id` is given, the caller must own that portfolio
   (`deps.ensure_portfolio_owner`, which asks core-service). An analysis
   on a portfolio can lead to trades there, so reading access is not
   enough.
3. Calls `create_and_enqueue(..., enforce_user_cap=True)`: admission
   check, insert the row, commit, enqueue the job (section 3.4).

The request body is `AnalysisCreate` (`domains/analysis/schema.py`):
`ticker` (1-10 characters, normalised by `shared/schemas.py`),
`portfolio_id` (optional), `analysis_type` (default `full`) and
`requested_agents` (optional list; when given it must name at least one
research agent, `RESEARCH_ROLES` = every role except `portfolio`, `bull`
and `bear`, otherwise 422, because a run without a research agent has no
evidence for the Judge). The response is the new row with
`status: "pending"`.

`GET /{analysis_id}` returns `AnalysisDetailRead`: the analysis fields
plus `agent_runs`, `debate`, `reflections` and `recommendation` in one
response (section 7 walks through it). Access is checked by
`ensure_analysis_access`; an analysis you may not see is a 404.

The API contract with request and response shapes is in
[../API_SPEC.md](../API_SPEC.md), section 4.

### 3.3 The internal API

`api/internal.py` defines one router with `prefix="/internal"`, the
dependency `require_internal_token` and `include_in_schema=False` (the
internal routes are not in the published OpenAPI document). It is mounted
directly on the app in `main.py`, not under `/api/v1`, so its paths are
exactly `/internal/*`.

| Route | Caller | What it does |
|---|---|---|
| `POST /internal/analyses` | core-service market-open job; `scripts/analysis_cli.py` | Creates an analysis like the public route, but without a JWT. `user_id` is optional (none = a "system analysis"). If a user is given, that user must be active and allowed to create analyses (checked when `USER_STATE_CHECK_ENABLED` is on and the user's state can be read); with a user and a portfolio, the user must own the portfolio. Respects the global queue cap, not the per-user cap. |
| `GET /internal/analyses/{id}` | core-service (WebSocket channel authorisation); `analysis_cli.py status` | Short reference: id, ticker, status, created_at, user_id, portfolio_id. |
| `GET /internal/analyses/{id}/detail` | `analysis_cli.py get` | Same body as the public detail route. |
| `GET /internal/recommendations/latest?ticker=&portfolio_id=` | core-service | Newest recommendation of an analysis on that portfolio; 404 when none. |
| `GET /internal/market/instruments/{ticker}/prices` | core-service (`get_daily_closes`) | Twin of the public prices route. |
| `GET /internal/fx-rate?from_currency=&to_currency=` | core-service | Current FX rate via Yahoo (`USDGBP=X` style ticker), cached in Redis for 3600 s. A Redis error on the cache read or write is logged (`fx_rate_cache_read_failed` / `fx_rate_cache_write_failed`) and treated as a cache miss, not a 5xx. |
| `POST /internal/review/run` | scheduler, daily 02:00 New York | Scores past recommendations and decision outcomes and rolls up agent metrics (`ai/workflows/review_workflow.py`); returns done, attempted and failed counts per step. Answers 500 with the same counts when every attempted recommendation or every attempted decision outcome failed. |
| `POST /internal/scheduler/tier-monitoring` | scheduler, 17:00 New York on weekdays | Checks open tiers against the day's bar; skipped with 200 and `{"skipped": true, "reason": "non_trading_day"}` on a weekend or NYSE holiday (the only ai-service job tied to the trading session). Answers 500 with the same counts when every attempted tier (checked minus `closed_meanwhile` and `pending`) failed to execute, had no fresh price or had a failed price lookup. |
| `POST /internal/scheduler/rag-ingest` | scheduler, daily 03:00 New York | Per-ticker filing and news ingestion plus the macro ingestion, as three steps (`filings`, `news`, `macro`). Answers 500 with the same body when every ticker a step attempted failed (an error, or a provider outage reported by the ingestion helpers), or the macro step failed. A source without its key or user agent is disabled (0 units, logged once) and a NewsAPI quota refusal defers that ticker's news; neither is a failure (chapter 26, section 3.5). |
| `GET /internal/stats` | auth-service admin page | Counts of analyses (total/completed/failed), recommendations, backtests. |
| `POST /internal/sentiment` | core-service, after a note or comment is saved | Lite-tier sentiment label; 503 with `Retry-After: 60` when the LLM is unavailable. |

The scheduler times come from `infra/scheduler/crontab` (New York time;
02:00 New York is usually 07:00 London, 03:00 is 08:00, 17:00 is 22:00).
The 500 rule is `shared/scheduled_jobs.py::job_response`: a step that
attempted at least one unit and failed every one answers 500 (log
`scheduled_job_all_units_failed`, body plus `"all_units_failed": [steps]`),
so the job script does not record a heartbeat and the stale-job alert can
fire; a step with nothing to do is a success.

### 3.4 The analysis queue

`domains/analysis/queue.py` holds one module-level object,
`analysis_queue = AnalysisQueue()`. It is an `asyncio.Queue` plus exactly
one worker task named `analysis-queue-worker`.

```
 POST /api/v1/analyses            POST /internal/analyses
          \                              /
           v                            v
     create_and_enqueue(db, user_id, payload, enforce_user_cap)
          |
          |  1. check_admission  (same transaction as the insert)
          |       SELECT pg_advisory_xact_lock(1120015)
          |       pending >= ANALYSIS_QUEUE_MAX_PENDING (50)?  -> 503, Retry-After: 60
          |       public only: user's pending+running >= ANALYSIS_MAX_ACTIVE_PER_USER (3)? -> 429
          |  2. INSERT ai.analyses (status "pending"), COMMIT
          |  3. analysis_queue.enqueue(AnalysisJob(id, analysis_type, requested_agents))
          v
   asyncio.Queue  [job][job][job] ...      (in memory, FIFO)
          |
          v
   worker _work(): for each job
       _run_one(job):
          task = create_task(run_analysis_workflow(id, ...))
          wait(task, timeout = ANALYSIS_TIMEOUT_SECONDS = 2700 s)
             finished          -> log analysis_queue_job_finished
             timed out         -> cancel, wait up to 30 s, mark failed "timeout"
             raised            -> mark failed "worker_error" (or "cancelled")
             worker cancelled  -> cancel run, mark failed "cancelled", re-raise
```

Key facts, all from `queue.py`:

- **Strictly one at a time.** Every source funnels into the same queue:
  the market-open job, the run scripts and API users. The reason: if
  each analysis were its own `asyncio.create_task`, a market-open batch
  of 13 tickers plus a manual script plus API users would all run at
  once, compete for the same small per-project LLM quotas, and whole
  batches would fail together.
- **Timeout.** `ANALYSIS_TIMEOUT_SECONDS` is `2700.0` (45 minutes) in
  `config.py`. On expiry the run task is cancelled, gets up to
  `_CANCEL_GRACE_SECONDS = 30.0` to unwind, and only then is the row
  marked `failed` with `failure_reason = "timeout"`. Writing the reason
  after the unwind means no late write from the cancelled run can land
  after it.
- **Admission caps.** `check_admission` counts rows in `ai.analyses`, so
  the caps survive restarts. The global cap (`ANALYSIS_QUEUE_MAX_PENDING
  = 50` pending rows) applies to every create and answers HTTP 503. The
  per-user cap (`ANALYSIS_MAX_ACTIVE_PER_USER = 3` pending or running)
  applies to the public create only and answers HTTP 429. Both run under
  the advisory lock `1120015`, so two parallel requests cannot both slip
  under a cap.
- **The worker never dies.** `_run_one` catches everything a job raises;
  `_work` has a last `except Exception` around it; `enqueue` calls
  `start()`, which replaces a worker that somehow finished (logged as
  `analysis_queue_worker_restarted`).
- **Inactive owners are skipped.** Before a job starts, `_run_one` checks
  the owner's live state (`shared.user_state.get_user_state`, the cached
  check the create routes use, gated by `USER_STATE_CHECK_ENABLED`). An
  owner known to be deactivated or deleted: the job does not run, the row
  is marked `failed` with `cancelled` (there is no dedicated reason yet),
  log `analysis_queue_job_skipped_owner_inactive`. No owner (a system
  analysis), an unknown state or a failing check: it runs (fail open,
  `analysis_queue_owner_check_failed`).
- **Restart means abort.** At startup, `fail_stale_analyses_on_startup`
  marks every row still `pending` or `running` as `failed` with
  `failure_reason = "service_restart"` and publishes `analysis.failed`
  for each. Nothing is resumed: the queue lives only in memory.
- **Single instance.** Two ai-service replicas would give two queues
  (not globally serial), and one replica's
  startup reconciliation would fail the other's live runs.

The failure reasons a row can carry are listed in
`shared/enums.py::AnalysisFailureReason`: `timeout`,
`cancelled`, `service_restart`, `worker_error`, `workflow_error`, and the
"no verdict" endings `judge_unavailable`, `no_evidence`,
`debate_unavailable`, `insufficient_evidence` (chapter 16).

### 3.5 Startup and shutdown

`main.py::lifespan` runs these steps in order:

1. `configure_logging(settings)`: structured JSON logs to stdout
   (`shared/logging.py`, `LOG_JSON` default true). Logs
   `service_startup`.
2. `ensure_schema(engine)` (`database.py`): brings the `ai` schema to
   the Alembic head on every start (chapter 20).
3. `check_embedding_configuration()` (`ai/rag/embedding.py`): fails
   startup if the embedding provider and the vector size do not match.
4. `ensure_collection()` (`qdrant.py`): creates the Qdrant collection
   `QDRANT_COLLECTION_RESEARCH` with cosine distance if it is missing. If
   it exists with a different vector size, startup stops with a
   `RuntimeError` that points to `scripts/reembed_collection.py`.
5. `rebuild_bm25_from_qdrant()` (`ai/rag/indexing.py`): the BM25 index is
   in memory, so it is rebuilt from the Qdrant payloads. Logs
   `bm25_index_rebuilt` with `chunk_count`.
6. Starts the durable event listener as a background task
   (`build_durable_subscriber().run()` in `event_listener.py`). It
   handles `document.uploaded` (RAG ingestion), `document.deleted`
   (remove from Qdrant, BM25 and Postgres) and `portfolio.deleted`
   (cancel the portfolio's unfilled tiers). A transient ingestion error
   is re-raised so the message stays unacknowledged and is delivered
   again (chapter 24). It is started with
   `shared.events.start_background_task(..., "durable_event_subscriber")`,
   so an unexpected end of the loop is logged (`background_task_crashed`
   or `background_task_ended`).
7. `fail_stale_analyses_on_startup()` and
   `fail_stale_backtests_on_startup()`: strictly before the worker
   starts, or a freshly queued job would be failed too.
8. `analysis_queue.start()`.

Shutdown (after `yield`) stops the queue first (the in-flight analysis is
marked `cancelled` while the database is still open), then the background
backtests and the event listener, then disposes the engine and closes
Redis, HTTP, Qdrant and storage clients.

`GET /health` checks the database, Redis, Qdrant and object storage. It
returns 200 with `"status": "ok"` or 503 with `"status": "degraded"`; the
Docker healthcheck in `docker-compose.yaml` calls it every 15 s.
`GET /metrics` exposes Prometheus metrics (chapter 27). The ai-service
specific ones (`observability.py`) are `alphamind_analyses_total{status,
failure_reason}`, `alphamind_analysis_queue_pending`,
`alphamind_analysis_queue_running` and `alphamind_llm_errors_total{provider,
kind}`. The HTTP latency histogram has extra buckets (beyond the library's
0.1/0.5/1 s) so the p95 panel and the `HighLatencyP95` alert (2 s) can see
slow requests. ai-service builds its collectors in `observability.py`
with `shared/logging.py::get_or_create`, like core-service.
Every response and error envelope carries the request id
(`shared/logging.py`). Swagger UI is at `/api/docs` with the schema at
`/api/openapi-ai.json` (behind the gateway: `/docs/ai/`). The schema path
is unique per service because Swagger UI fetches it as an absolute URL
from the gateway's root, and the gateway must know which backend to route
it to.

### 3.6 Data providers and caching

A provider is a client for one external data source. Tools never call
the vendor directly; they go through the singletons in
`ai/providers/registry.py`:

| Accessor | Provider | Source |
|---|---|---|
| `get_market_data_provider()` | `YahooFinanceProvider` | Yahoo Finance through the `yfinance` library: current price, price history, instrument metadata, fundamentals from `.info`. No API key. `yfinance` is synchronous, so every call runs in `asyncio.to_thread`. |
| `get_fundamental_data_providers()` | `[SecEdgarProvider, YahooFinanceProvider]` | SEC EDGAR (`data.sec.gov`, no key, needs `SEC_EDGAR_USER_AGENT`: SEC requires a descriptive User-Agent). Three uses: (1) annual fundamentals from XBRL "company facts" (`SecEdgarProvider.get_fundamentals`); `get_fundamentals` (`ai/tools/fundamentals_tool.py`) merges them with Yahoo: SEC first, Yahoo fills only the fields still empty. (2) The latest quarter from the same company facts (`latest_quarter_results`; chapter 18, section 3.12). (3) Filing periods: `filing_period(ticker, form, filing_date)` reads the EDGAR submissions JSON and returns the filer's own `reportDate`, used to label every filing passage and record. Every SEC request waits until `1 / SEC_EDGAR_MAX_REQUESTS_PER_SECOND` seconds (default 8 per second; SEC allows 10) have passed since the previous one, process-wide. Network errors and 5xx are retried, 4xx is final. |
| `get_macro_data_provider()` | `FredProvider` | FRED (St. Louis Fed) series observations. Needs `FRED_API_KEY`. Aliases: `fed_funds_rate` (DFF), `cpi` (CPIAUCSL), `unemployment_rate` (UNRATE), `gdp` (GDP), `10y_treasury_yield` (DGS10), `vix` (VIXCLS); any other string is passed through as a series id. 15 s timeout per request. |
| `get_news_provider()` | `NewsApiProvider` | NewsAPI `/v2/everything`. Needs `NEWSAPI_API_KEY`. The free tier gives about one month of history and 100 requests per rolling 24 hours, not per calendar day (as observed on the account page in October 2026). Every agent `search_news` sends 2 requests (relevancy plus `NEWSAPI_RECENT_EXTRA_LIMIT` newest), so one 15-ticker batch uses about 30-35: check `make health` (section 5) before a batch. Successful requests are counted in Redis `ai:newsapi:requests` (rolling 24 h, read by `make health`). An HTTP error is raised with its status and NewsAPI's `code`/`message` and logged as `newsapi.request_failed`; HTTP 429 or code `rateLimited`/`apiKeyExhausted` stops all NewsAPI requests of the process for `NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS` (3600; log `newsapi.rate_limited_cooldown_started`), searches failing at once with "request limit reached ... use web_search". Credibility of a news or web result comes from NewsAPI's own source catalog (`source_catalog()`, `GET /v2/top-headlines/sources`, language `en`): an item is a catalog outlet when NewsAPI gave it a `source.id`, or its URL host is (a subdomain of) a catalog source host (`catalog_flag`). The catalog costs one counted request when refreshed; it is kept in Redis `ai:newsapi:source_catalog` for 7 days and refreshed after 24 h (from its stored `fetched_at`), with a 1 h process copy and a 15 min back-off after a failed load. A stale copy is served while a refresh is impossible (rate-limit cooldown, error; `newsapi.source_catalog_stale_served`). It fails open: no catalog means "unknown", never "not a catalog outlet". Logs: `newsapi.source_catalog_fetched`, `newsapi.source_catalog_fetch_failed`. |
| `get_web_search_provider()` | `TavilyProvider` | Tavily web search, "basic" depth, short snippets only (never raw page content), at most 10 results (`max_results` clamped to 1..10). With `news_days` it searches Tavily's news index for the last N days (`topic: "news"`); the News agent's fallback uses `NEWS_WEB_FALLBACK_DAYS` (7). Needs `TAVILY_API_KEY`; free tier 1,000 credits per month. |

SEC filing documents (the 10-K/10-Q text that goes into RAG) are fetched
separately by `ai/ingestion/sec_edgar_filings.py` with the `edgartools`
library (`from edgar import Company, set_identity`). The filing listings
(recent filings, older submission pages, 10-K/A amendments) run in worker
threads with a 15 s timeout each (`_EDGAR_API_TIMEOUT_SECONDS`); the
company lookup and the document downloads (`filing.text()`) rely on the
library's own per-request HTTP timeout. Each filing is then summarised by
an LLM, with a
second LLM call for the structured filing records
(`ai/knowledge/filing_summary.py`, chapter 18).

Provider errors follow one rule (`ai/providers/base.py`): a provider
raises `ProviderUnavailableError` when its key is missing (permanent) or
`ProviderRequestError` when a call fails (transient). A refusal because
a request quota is used up (NewsAPI's daily limit and its cooldown) is
the subclass `ProviderQuotaError`, so code that only needs "the call
failed" still sees a `ProviderRequestError`, while the nightly ingestion
can treat it as a deferral instead of an outage (chapter 26, section
3.5). Tools catch all of them and return a readable failure text to the
agent instead of crashing it.
`ai/providers/base.py::call_with_retry` retries Yahoo, SEC EDGAR and
FRED calls. It makes at most 3 attempts in total
(`DEFAULT_MAX_ATTEMPTS = 3`). The waits are about 1 s and then 2 s, each
with up to 30 % random jitter. For SEC EDGAR and FRED it retries only
network errors and 5xx answers; a 4xx answer is final. That
schedule is deliberately much shorter than the LLM rate-limit schedule,
because a market-data blip clears in seconds.

**Price caching.** `ai/tools/market_data_cache.py::get_bars_cache_first`
serves daily price bars from the `ai.price_bars` table (with
`ai.instruments`) before asking Yahoo. It is used by the price history,
technical indicator and volatility tools. The rules:

- A `1d` cache entry is fresh if it was fetched after the most recent
  NYSE session close (16:00 New York, holidays from
  `shared/market_calendar.py`), or within a 15-minute grace window
  (`_INTRADAY_REUSE_GRACE`) so several agents in one run share one fetch.
- Fresh is not enough: the cached bars must also cover the requested
  window without holes (`_covers_window`), and after the close the series
  must contain the last completed session.
- Weekly and monthly bars use plain age limits (6 and 27 days); other
  intervals 20 hours.
- On a miss, exactly one live refresh runs and is written back
  (`MarketService.refresh_from_external_provider`). If Yahoo fails and
  some cached bars exist, the stale bars are served (logged as
  `market_cache.refresh_failed_serving_stale_cache`), because yesterday's
  close is still more useful to an agent than nothing.

Other caches: the FX rate (Redis, 1 h); SEC's ticker-to-CIK map and
company names (process memory, re-downloaded once per UTC day); SEC
submissions and the XBRL latest-quarter result per ticker per day (process
memory; a cached quarter is re-checked for a newer 10-Q/10-K, and both are
dropped when ingestion stores a new filing); NewsAPI's source catalog
(Redis 7 days, refreshed after 24 h, process 1 h); the NewsAPI usage
counter `ai:newsapi:requests` (rolling 24 h); company names for news
relevance (process); the portfolio tool's holdings snapshot for one agent
run. Process caches are lost on restart; that is safe because each one is
rebuilt by one request.

### 3.7 The scripts folder

`services/ai-service/scripts/` holds Python scripts that run inside the
container (`docker compose --env-file .env.docker exec -T ai-service
python -m scripts.<name>`). Most are owner tools, not part of the running
service. The host-side operator scripts in the repository's `scripts/`
folder call some of them for you.

| Script | Purpose | Writes data? |
|---|---|---|
| `analysis_cli.py` | Small client for the internal analysis routes: `create TICKER [--portfolio ID] [--user ID]`, `status ID`, `get ID`; and `committed PORTFOLIO_ID`, which reads the database and prints the BUY cash held by the portfolio's open tiers, per analysis, as the position-sizing budget counts it. Used by `scripts/run_analysis.sh` and `scripts/tiers.sh`. | `create` queues an analysis |
| `probe_model_latency.py` | Sends Judge-sized requests to one Gemini model (default: the configured HIGHEST primary model) per pooled key and reports latency and what a 30/60/90 s timeout would keep. Used by `scripts/probe.sh`. `--health` instead sends one tiny request per key and per configured model ID. | No, but spends daily quota |
| `smoke_check.py` | One line per non-analysis feature check (RAG, KG, filing records, market, FX, FRED, core reads, auth, backtests). Its top also holds the `flatten`/`one_line` output helpers that `e2e_tier_check.py` and `delete_analyses.py` import. The `filing_records` check fails and lists the filing documents without an `ai.filing_records` row (a records call that failed at ingestion), with the backfill command. Used by `scripts/smoke.sh`. | Small, deliberate writes only: refreshes AAPL price bars and creates then deletes one scratch backtest (see its header) |
| `e2e_tier_check.py` | Deterministic real-environment test of the tier chain with a synthetic recommendation. Used by `scripts/e2e.sh`. | Yes (test data, cleaned up unless `--keep`) |
| `delete_analyses.py` | Deletes whole analyses and everything they wrote, then sweeps orphans (including knowledge-graph entities left without relations). Walks the live foreign-key tree first. | **Yes, destructive** |
| `reembed_collection.py` | Copies the RAG collection into a new Qdrant collection with vectors from another embedding model. Preview by default; `--confirm` writes. | Yes with `--confirm` |
| `generate_rag_golden_set.py`, `measure_rag_recall.py` | RAG evaluation: build questions, measure Recall@K per category; `measure_rag_recall.py --sweep` tunes the BM25/vector weights. | `--sweep --apply` writes `ai/rag/category_weights.json`; everything else is read-only |
| `backfill_filing_records.py` | Extracts (or with `--redo` re-extracts) `ai.filing_records` for filing documents already stored, oldest first, one records-only call each. Flags `--ticker` (repeatable; default every ticker with FILING documents), `--latest N`, `--filename NAME` (repeatable; tickers taken from the names when no `--ticker`), `--tier` (default `FILING_RECORDS_BACKFILL_LLM_TIER`), `--apply` (default dry run: no LLM, SEC or database writes). Waits 65 s before a filing above 500,000 characters; a failed filing stores nothing and the exit code is 1. Chapter 18, 3.13. | Yes with `--apply` |
| `regenerate_filing_summaries.py` | Two steps for a stored summary that is bad (for example cut off): `--filename F` (repeatable) makes one HIGHEST summary call from the archived raw text and saves it to `/app/_regenerated/` (no database writes; prints the current core document id); after the owner deletes the old document with core-service `scripts.delete_documents`, `--filename F --apply` stores the new document (normal RAG ingestion). Summary only; records are redone with the backfill. Chapter 18, 3.13. | Yes with `--apply` |
| `upload_seed_documents.py` | Uploads the RAG seed corpus from the repo-root `rag_seed_documents/` folder. | Writes documents |
| `verify_portfolio_backtest.py` | Recomputes a portfolio backtest independently and compares. | Only with `--run`: it runs the production backtest, which creates a backtest row; the row is deleted unless `--keep` |

## 4. Configuration

Settings live in `services/ai-service/config.py` (class
`AIServiceSettings`, which extends `shared/config.py::BaseServiceSettings`).
Values come from the environment; `config.py` holds every default.
`.env.example` lists the settings an operator usually sets (keys, URLs,
the embedding model); the tables in this handbook list all of them. The
groups below summarise them. Chapter 14 has the full LLM table.

| Group | Settings (default) | Meaning |
|---|---|---|
| Service | `SERVICE_NAME` (`ai-service`), `POSTGRES_SCHEMA` (`ai`), `API_V1_PREFIX` (`/api/v1`, shared) | Identity and the database schema. |
| Qdrant and embeddings | `QDRANT_HOST` (`qdrant`), `QDRANT_PORT` (6333), `QDRANT_URL` (None), `QDRANT_API_KEY` (empty; passed to the Qdrant client when set, also used by backup and restore), `QDRANT_COLLECTION_RESEARCH` (`research_documents_gemini`), `QDRANT_VECTOR_SIZE` (1536), `EMBEDDING_PROVIDER` (`google`), `GOOGLE_EMBEDDING_MODEL` (`gemini-embedding-2`), `GOOGLE_EMBEDDING_DIMENSIONS` (1536), `EMBEDDING_REQUEST_TIMEOUT_SECONDS` (30.0) | Where vectors live and which model makes them. Chapters 17 and 21. |
| Archival | `ARCHIVAL_NEAR_DUPLICATE_THRESHOLD` (None = 0.90), `ARCHIVAL_RECYCLED_SHARE_MAX` (0.5), `KG_EXTRACTION_LLM_TIER` (`cheap`) | When an agent note is stored or superseded. Chapters 15 and 18. |
| Prior notes | `PRIOR_NOTE_MAX_SIMILARITY` (0.9) | Word-sequence similarity at which a Risk or News answer is a copy of a prior note (News: sent back once; both: recycled, not archived). Chapter 15. |
| Filings | `SEC_EDGAR_MAX_REQUESTS_PER_SECOND` (8.0), `FILING_RECORDS_MAX_FILINGS` (5), `FILING_RECORDS_BACKFILL_LLM_TIER` (`cheap`) | SEC request spacing; how many filings' records agents see; default tier of the records backfill. Chapter 18. |
| News | `NEWS_RELEVANCE_BATCH_SIZE` (8), `NEWS_CONSUMER_MAX_ITEMS` (2), `NEWS_WEB_FALLBACK_DAYS` (7) | Items per relevance-classifier call; consumer-product items kept per run (never evidence); look-back of the Tavily news fallback. Chapters 15 and 17. |
| LLM keys | `GOOGLE_API_KEY`, `GOOGLE_API_KEY_2` .. `_10`, `MISTRAL_API_KEY`, `MISTRAL_API_KEY_2` .. `_7`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (all empty) | Provider credentials. Chapter 14. |
| LLM routing and resilience | `LLM_DEFAULT_PROVIDER` (`google`), `LLM_GOOGLE_MODEL_*`, pacing, timeouts, cooldowns | Chapter 14, section 4. |
| Analysis queue | `ANALYSIS_TIMEOUT_SECONDS` (2700.0), `ANALYSIS_MAX_ACTIVE_PER_USER` (3), `ANALYSIS_QUEUE_MAX_PENDING` (50) | Section 3.4. |
| Debate and verdict resilience | `JUDGE_RETRY_BUDGET_SECONDS` (240.0), `JUDGE_RETRY_MAX_ATTEMPTS` (4), `DEBATE_SIDE_REPAIR_DELAY_SECONDS` (60.0), `REFLECTION_REVIEW_DELAYED_RETRY_SECONDS` (60.0), `MIN_USABLE_AGENTS_FOR_DEBATE` (4), `JUDGE_STATE_QUARTER_DECLINE_PCT` (15.0) | Chapter 16. |
| Agents | `AGENT_PREFETCH_ENABLED` (True), `FALLBACK_ANCHOR_MIN_WORD_SHARE` (0.3), `SENTIMENT_THINKING_BUDGET` (1024) | Fixed first tool calls run in code; word-share similarity of the fallback evidence anchor; thinking budget of Sentiment's ReAct turns on Lite models (0 = off). Chapters 14 and 15. |
| Recommendation and tiers | `TIER_MIN_NOTIONAL_USD` (100.0), `TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION` (0.01), `MAX_POSITION_WEIGHT_PCT` (50.0), `MIN_BUY_TIER_CONFIDENCE` (0.5), `TIER_WINDOW_DAYS_MAX` (30), `SYSTEM_TIER_ACCEPTANCE_HOURS` (72: how long a pending tier of a system analysis may wait for acceptance) | Chapter 16. There is no per-recommendation BUY cap (owner decision). |
| Memory and review | `EPISODIC_CONTEXT_LIMIT` (3), `AI_ACCURACY_EVALUATION_DAYS` (30) | Chapters 18 and 19. |
| Data providers | `SEC_EDGAR_USER_AGENT`, `FRED_API_KEY`, `NEWSAPI_API_KEY`, `TAVILY_API_KEY` (empty), `NEWSAPI_SEARCH_IN` (`title,description`), `NEWSAPI_SORT_BY` (`relevancy`), `NEWSAPI_RECENT_EXTRA_LIMIT` (10), `NEWSAPI_RATE_LIMIT_COOLDOWN_SECONDS` (3600.0) | Section 3.6. |
| RAG assistant | `RAG_TOP_K_RESULTS` (5), `RAG_ASK_RATE_LIMIT_PER_MINUTE` (10), `RAG_ASK_RATE_LIMIT_PER_DAY` (200), `RAG_ASK_LLM_TIER` (`cheap`), `RAG_ASK_MAX_OUTPUT_TOKENS` (1024), `RAG_ASK_SNIPPET_CHARS` (300), `RAG_ASK_STREAM_TIMEOUT_SECONDS` (120.0) | Chapter 17. |
| User sentiment | `SENTIMENT_MAX_INPUT_CHARS` (6000), `SENTIMENT_TIMEOUT_SECONDS` (240) | `POST /internal/sentiment`. |
| Shared (from `shared/config.py`) | `LOG_LEVEL` (`INFO`), `LOG_JSON` (True), `INTERNAL_API_TOKEN` (empty), `USER_STATE_CHECK_ENABLED` (True), database/Redis/storage URLs | Chapter 30. |

## 5. Design decisions and why

**One serial queue instead of concurrent analyses.**
Alternative: run each analysis as its own task, or a bounded pool of N
workers. Reason: the LLM quota is per Google Cloud project and small.
Concurrent analyses exhaust it together and fail together. One at a time
makes each analysis slower to start but far more likely to finish. The
cost is visible: one analysis usually takes about 1.5 to 4 minutes (up to 10
minutes more when a new SEC filing must be summarised first), so a batch of
15-16 tickers takes about 30-40 minutes.

**A hard 45-minute timeout per analysis.**
Alternative: no timeout. Reason: with a serial queue, one hung run (for
example a provider that keeps answering 503) would block every analysis
behind it. `ANALYSIS_TIMEOUT_SECONDS = 2700.0` bounds that.

**Admission caps counted from the database.**
Alternative: count in memory. Reason: the rows survive a restart and the
count is the truth the user sees. The advisory lock makes the count plus
insert atomic.

**Restart aborts, it does not resume.**
Alternative: re-queue pending rows at startup. Reason: the owner restarts precisely to abort runs, and a half-run
analysis cannot be resumed from the middle of a LangGraph run anyway.

**Internal routes outside `/api/v1`, protected by a token.**
Alternative: one router tree. Reason: the gateway never proxies
`/internal`, callers already used unversioned paths, and the token is
defence in depth on the Docker network.

**Cache-first prices with session-aware freshness.**
Alternative: a plain "younger than N hours" rule. Reason: with such a
rule a market-open snapshot fetched hours before the close looks "fresh"
for the whole day, and a tier fill price would be computed from it.
Freshness is therefore tied to the NYSE session close.

**Providers return text failures to agents.**
Alternative: let exceptions propagate. Reason: "the data source did not
answer" is information an agent can reason about (note the gap, lower its
confidence), not a reason to crash the analysis.

## 6. When things go wrong

| Symptom | What the system does | How to diagnose |
|---|---|---|
| Create returns 503 "The analysis queue is full" | Global cap of 50 pending rows reached. | `bash scripts/psql.sh -c "SELECT status, count(*) FROM ai.analyses GROUP BY status;"`; log event `analysis_queue_full`. |
| Create returns 429 | The user already has 3 pending or running analyses. | Wait; check the user's rows in `ai.analyses`. |
| Analysis stays `pending` for a long time | It is waiting behind other jobs (FIFO). | Log `analysis_enqueued` shows `jobs_ahead`; `analysis_queue_job_started` shows `queued_behind`; metric `alphamind_analysis_queue_pending`. |
| Analysis `failed`, reason `timeout` | The run passed 2700 s and was cancelled. | Log `analysis_queue_job_timed_out`; then look for long LLM waits in the same window (chapter 14). |
| Analysis `failed`, reason `service_restart` | ai-service restarted while it was pending or running. | Log `analysis_failed_on_startup`, `analysis_startup_reconciliation_complete`. |
| Analysis `failed`, reason `worker_error` | Something outside the workflow's own handler raised. | Log `analysis_queue_job_errored` with `error_type`. |
| Analysis `failed` with a no-verdict reason | The Judge or the debate could not produce a verdict, or too few agents had evidence. | Chapter 16. |
| ai-service does not start | Alembic, embedding configuration or Qdrant vector size mismatch. | `bash scripts/logs.sh ai-service`; look for the `RuntimeError` from `qdrant.py::ensure_collection`. |
| `/health` is 503 | One dependency check failed. | The JSON body names which (`database`, `redis`, `qdrant`, `storage`). |
| An agent says "price lookup failed" | Yahoo failed after 3 attempts and no cached bars existed. | Log `provider_retry.retrying`, `market_cache.refresh_failed_serving_stale_cache`. |
| A document upload never becomes searchable | The durable listener did not process it, or ingestion failed. | Chapter 17 and chapter 24. |
| Analysis `failed`, reason `cancelled`, never started | The owner was deactivated or deleted while the job waited. | Log `analysis_queue_job_skipped_owner_inactive`. |
| Backtest create returns 429 | The user already has 2 backtests pending or running. | Log `backtest_rejected_limit`. |
| Scheduler job shows no fresh heartbeat after a run | The job answered 500 because every attempted item failed. | Log `scheduled_job_all_units_failed` (`job`, `steps`); then the job's own log lines. |
| Fundamental or Judge has no "Latest quarter" line | SEC companyfacts has no usable us-gaap quarter (IFRS filer) or the lookup failed. | Log `sec_edgar.latest_quarter_unavailable`, `sec_edgar.quarter_results_failed`; a missing year-to-date line logs `sec_edgar.year_to_date_unavailable` (its `metrics` field says why per metric). |
| Filing passages have no report period | EDGAR has no `reportDate` for that filing (XOM's filings before its new CIK). | Log `sec_edgar.filing_period_unavailable`. |
| News agent says "request limit reached ... use web_search" | NewsAPI rate limit; one-hour cooldown. | Log `newsapi.request_failed` with status and code; `make health` shows the rolling 24 h usage. |

## 7. Demo walkthrough

Goal: run one real analysis, follow it live in the log, and read the
result. The analysis itself usually takes about 1.5 to 4 minutes (up to
10 minutes more when a new SEC filing must be summarised first); plan
about 15 minutes for the whole walkthrough. Avoid the scheduler's busy
windows (09:00-10:30 and 16:55-17:50 New York, usually 14:00-15:30 and
21:55-22:50 London): at 09:30 New York the market-open job queues
analyses into the same serial queue, so your run would wait behind them.

**This demo writes data.** It creates a row in `ai.analyses` (plus agent
runs, debate and reflections). The run script binds the analysis to the
development portfolio (`PORTFOLIO_ID`, default in `scripts/lib.sh`), so a
BUY or SELL verdict also writes recommendation tiers with status pending
acceptance. They never execute unless someone accepts them. The run
script makes a system analysis (no user), so its pending tiers expire
after `SYSTEM_TIER_ACCEPTANCE_HOURS` (72 hours). Step 7 shows how to
decline them earlier.

1. **Check the stack is healthy.**
   ```bash
   make health
   ```
   Say: "ai-service, Postgres, Redis and Qdrant are up; the scheduler's
   job heartbeats are recent."

2. **Open a second terminal and follow the ai-service log.**
   ```bash
   bash scripts/logs.sh ai-service        # or: make service-logs S=ai-service
   ```
   It shows the last 10 minutes and then follows. Each line is one JSON
   object with `event`, `timestamp`, `level` and fields.

3. **Start the analysis (writes data).**
   ```bash
   make analyze T=AAPL                    # same as: bash scripts/run_analysis.sh AAPL
   ```
   On a Mac wrap long batches in `caffeinate -i`. The script prints
   `Output directory: runs/run_<YYYYMMDD_HHMMSS>`, then `AAPL ->
   analysis_id=<uuid>`, then a status line every 5 seconds
   (`[AAPL] elapsed=30s status=running`).
   Say: "The script calls `POST /internal/analyses` from inside the
   container through `scripts/analysis_cli.py`; the row is created as
   pending and the queue picks it up."

4. **Watch the log events in order.** In the second terminal you should
   see:
   - `analysis_enqueued` with `jobs_ahead` (0 if the queue was empty);
   - `analysis_queue_job_started` with `timeout_seconds: 2700.0`;
   - many `google.completion_succeeded` lines with `model`, `key`,
     `latency_ms` and token counts (the agents' LLM calls);
   - maybe a few `react_loop.*` warnings from the agents, for example
     `react_loop.finish_gate_rejected` when an agent tried to finish too
     early (chapter 15). The loop logs only these exceptions, not every
     step, so a clean run can have none;
   - debate, Judge and Reflection events (chapter 16);
   - finally `analysis_queue_job_finished`.
   Say: "Every LLM call is one line. Lite models serve the nine data
   agents; the two Flash models serve Bull, Bear, Judge and Reflection."

5. **Check the row in the database (read-only).**
   ```bash
   bash scripts/psql.sh -c "SELECT id, ticker, status, failure_reason, created_at FROM ai.analyses ORDER BY created_at DESC LIMIT 3;"
   ```
   The newest row should be `completed` (or `failed` with a reason).

6. **Read the result JSON.** When the script ends, open the run folder.
   ```bash
   ls runs/                                # newest folder last
   python3 - <<'EOF'
   import glob, json
   path = sorted(glob.glob("runs/run_*/AAPL_result.json"))[-1]
   d = json.load(open(path))["data"]
   print("status:", d["status"], "failure_reason:", d.get("failure_reason"))
   for run in d["agent_runs"]:
       res = run["result"] or {}
       out = res.get("output") or {}
       print(f'{run["agent_name"]:12} {run["status"]:10} {run["duration_ms"]} ms '
             f'score={res.get("score")} well_supported={out.get("is_well_supported")}')
   rec = d["recommendation"]
   if rec:
       print(rec["recommendation"], rec["confidence"], "converged:", rec["debate_converged"])
       print("tiers:", len(rec["tiers"]), "|", rec.get("tier_generation_note"))
   EOF
   ```
   Explain the fields (they come from `domains/analysis/schema.py`):
   - top level: `id`, `ticker`, `status`, `user_id` (null for a system
     analysis), `portfolio_id`, `created_at`, `failure_reason`;
   - `agent_runs[]`: `agent_name`, `status`, `started_at`,
     `completed_at`, `duration_ms`, and `result` with `score`, `summary`
     (the agent's final text) and `output` (its raw output: the
     scratchpad, `finish_meta`, `verification_warnings`, `key_facts`,
     plus `is_well_supported`);
   - `debate.arguments[]`: `round_number`, `side` (bull/bear), `content`,
     `citations`;
   - `reflections[]`: `round_number`, `verified`, `issues`,
     `retry_agents`;
   - `recommendation`: `recommendation` (buy/hold/sell), `confidence`,
     `reasoning`, `confidence_adjustment_note`, `tier_generation_note`,
     `debate_converged`, `window_expires_at`, `accepted_at`, `tiers[]`
     (side, target_price, quantity, status).
   The same folder also has `AAPL_agent_reports.txt` (archived notes),
   `AAPL_tiers.txt`, `AAPL_filings.txt`, `AAPL_ai-service.log` (the log
   window of this run) and `_summary.txt`.

7. **Clean up the pending tiers if there are any (writes data).**
   ```bash
   make tiers                              # BUY cash held by open tiers, per analysis
   make tiers-decline A=<analysis-uuid>    # decline that analysis' recommendation
   ```
   Say: "Tiers wait for the owner's decision; declining closes them."

Alternative without tiers: create a review-only analysis with no
portfolio (still writes the analysis rows):
`docker compose --env-file .env.docker exec -T ai-service python -m scripts.analysis_cli create AAPL`,
then `... analysis_cli status <id>` and `... analysis_cli get <id>`.

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Why does `POST /api/v1/analyses` return before any agent runs?**
   The run takes minutes. The route inserts a `pending` row, commits,
   and puts a job on the in-memory queue; the single worker runs it
   later.

2. **What are the two queue caps and which HTTP codes do they give?**
   `ANALYSIS_QUEUE_MAX_PENDING` (50 pending rows, every create, 503) and
   `ANALYSIS_MAX_ACTIVE_PER_USER` (3 pending or running for one user,
   public create only, 429).

3. **What happens to a running analysis when ai-service restarts?**
   On shutdown it is cancelled and marked `failed`/`cancelled`. Anything
   still pending or running at the next start is marked
   `failed`/`service_restart`. Nothing resumes.

4. **Why can ai-service not simply be scaled to two replicas?**
   The queue is in process memory, so two replicas would run two
   analyses at once, and one replica's startup reconciliation would fail
   the other's live runs.

5. **Why is the internal router not under `/api/v1`?**
   Callers use unversioned `/internal/...` paths and the gateway never
   proxies `/internal`. Nesting it caused real 404s.

6. **When does the price cache call Yahoo for daily bars?**
   When the cached series is older than the last NYSE close (and
   outside the 15-minute grace), or does not cover the requested window,
   or lacks the last completed session.

7. **Which provider needs no API key?**
   Yahoo Finance (and SEC EDGAR, which needs only a User-Agent).

8. **What does `GET /health` return when Qdrant is down?**
   HTTP 503 with `"status": "degraded"` and `"qdrant": false`.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/main.py` | App, lifespan, `/health`, `/metrics`, router mounting and public router prefixes. |
| `services/ai-service/config.py` | `AIServiceSettings` and every default. |
| `services/ai-service/api/internal.py` | Internal routes. |
| `services/ai-service/domains/analysis/api.py` | Analysis routes, `create_and_enqueue`. |
| `services/ai-service/domains/analysis/queue.py` | `AnalysisQueue`, `check_admission`, `fail_stale_analyses_on_startup`. |
| `services/ai-service/domains/analysis/schema.py` | `AnalysisCreate`, `AnalysisDetailRead` and nested read models. |
| `services/ai-service/ai/workflows/analysis_workflow.py` | `run_analysis_workflow`, persistence, `mark_analysis_failed`. |
| `services/ai-service/event_listener.py` | Durable event handlers. |
| `services/ai-service/qdrant.py` | Qdrant client and `ensure_collection`. |
| `services/ai-service/database.py` | `ensure_schema` (Alembic at startup). |
| `services/ai-service/observability.py` | Prometheus metrics. |
| `services/ai-service/ai/providers/registry.py` | Provider singletons. |
| `services/ai-service/ai/providers/yahoo_finance.py`, `fred.py`, `newsapi.py`, `sec_edgar.py`, `tavily.py` | The five data providers. |
| `services/ai-service/ai/providers/base.py` | Provider error rule; `call_with_retry` for market data. |
| `services/ai-service/ai/ingestion/sec_edgar_filings.py` | Filing download through `edgartools`, summary and records calls, `ingest_new_filings`. |
| `shared/scheduled_jobs.py` | `job_response`: 200, or 500 when every attempted item of a step failed. |
| `services/ai-service/scripts/backfill_filing_records.py`, `regenerate_filing_summaries.py` | Filing maintenance (chapter 18). |
| `services/ai-service/ai/tools/market_data_cache.py` | Cache-first price bars. |
| `services/ai-service/scripts/analysis_cli.py` | Internal-route client used by the run script; `committed` (BUY cash held by open tiers) used by `scripts/tiers.sh`. |
| `scripts/run_analysis.sh` | Host script: run analyses and save a run folder. |
| `scripts/logs.sh` | Follow or search service logs. |

## 10. Related chapters

- [01-architecture.md](01-architecture.md) and
  [02-operations.md](02-operations.md): where ai-service fits and how to
  run the stack.
- [14-ai-llm-layer.md](14-ai-llm-layer.md): providers, router, keys,
  retries.
- [15-ai-agents.md](15-ai-agents.md): the agents and the ReAct loop.
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): the graph,
  debate, Judge, recommendation and tiers.
- [17-ai-rag.md](17-ai-rag.md), [18-ai-knowledge-memory.md](18-ai-knowledge-memory.md),
  [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md).
- [20-postgresql.md](20-postgresql.md), [21-qdrant.md](21-qdrant.md),
  [24-events.md](24-events.md), [26-scheduler-jobs.md](26-scheduler-jobs.md),
  [27-observability.md](27-observability.md).
- [../API_SPEC.md](../API_SPEC.md) (section 4), [../ERD.md](../ERD.md).
