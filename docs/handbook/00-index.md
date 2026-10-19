# 00. Index and reading paths

> This handbook explains every part of AlphaMind: what it is, why it is
> built that way, how it works in the code, how to operate it, and how to
> show it live. It is written for someone who knows Python basics and
> wants to understand the whole system deeply. Every chapter stands on its
> own and points to the files where the real code lives. When the handbook
> and the code disagree, the code is right.

## 1. What AlphaMind is, in one page

AlphaMind is a multi-agent research platform for US stocks. A user keeps
portfolios and research notes. For any ticker, the system can run an
**analysis**: nine "data agents" collect evidence (prices, technical
indicators, fundamentals, SEC filings, news, sentiment, macro data, risk,
portfolio fit), a **Bull** and a **Bear** argue, a **Judge** decides
BUY, HOLD or SELL with a confidence number, and a **Reflection** step
checks the citations. For a BUY or SELL on a portfolio, the system
proposes concrete price **tiers** (for example "buy 10 shares at 101").
If the owner accepts them, a daily job executes the tiers that the real
market price reached.

Around that core there is a normal web backend: accounts, organizations,
friends, portfolios with a transaction ledger, watchlists, research notes
with live collaborative editing, uploaded research documents that become
searchable (RAG), notifications over WebSockets, a public API with API
keys, backtests, scheduled jobs, backups and full monitoring.

```
 browser / scripts --> gateway (nginx, TLS) --> auth-service   (who you are)
                                            --> core-service   (your data, live updates)
                                            --> ai-service     (analyses, RAG, tiers)
 data:   PostgreSQL (auth, core, ai schemas), Redis, Qdrant, object storage
 around: scheduler (cron), Prometheus + Alertmanager + Grafana, vector logs, MailHog
```

Chapter [01](01-architecture.md) draws the full picture.

### Product overview

AlphaMind is meant to work like a research analyst for an individual
investor, not like a chat assistant that gives one unexplained answer. A
question such as "should I buy AMD?" becomes a fixed process, and every
step is stored so it can be inspected later:

1. collect evidence with tools: prices, technical indicators,
   fundamentals, SEC filings, news, sentiment, macro data, risk and
   portfolio fit ([15](15-ai-agents.md));
2. search the document library: uploaded research, filings, news digests
   and earlier agent reports ([17](17-ai-rag.md));
3. let a Bull and a Bear argue, and let a Judge weigh both sides
   ([16](16-ai-analysis-pipeline.md));
4. check the debate's claims against the evidence (Reflection);
5. issue BUY, HOLD or SELL with a confidence, a note that explains every
   confidence adjustment, and, for a portfolio, concrete price tiers;
6. later, measure whether the call was right and whether following it
   paid off ([19](19-ai-workflows-backtest.md), section 3.11).

What a user can do in the web pages (every action is also an API route):

- keep portfolios with a transaction ledger, see holdings, cash and daily
  values, and keep watchlists ([12](12-core-service.md));
- write research notes (tags, full-text search, live collaborative
  editing, comments) and upload research documents (PDF, Markdown, plain
  text) that become searchable ([12](12-core-service.md),
  [17](17-ai-rag.md));
- ask questions that are answered only from the document library, with
  citations ([17](17-ai-rag.md), section 3.12);
- run analyses, answer a recommendation with a decision (followed,
  ignored, partial, declined), and read the decision's outcome and an LLM
  review of it ([16](16-ai-analysis-pipeline.md),
  [19](19-ai-workflows-backtest.md));
- run backtests and read the analytics dashboard
  ([19](19-ai-workflows-backtest.md), [13](13-ai-service.md));
- work in organizations, with friends, presence and notifications
  ([11](11-auth-service.md), [12](12-core-service.md)).

The web pages are the Next.js app in `frontend/`
([01](01-architecture.md) section 3.11).

## 2. How to use this handbook

- **Each chapter has the same shape**: summary, big picture, concepts,
  how it works (with file paths), configuration, design decisions and
  their reasons, failure modes, a demo walkthrough, questions to check
  your understanding, a code map, and related chapters.
- **File paths** are relative to the repository root, for example
  `services/ai-service/ai/recommendation/engine.py`. Function names are
  written as `file.py::function`.
- **Commands** are run from the repository root on your Mac, with the
  stack running. They use the project's conventions:
  - `docker compose --env-file .env.docker ...` (never without the env
    file: some values in the Compose file come from it);
  - the operator scripts in `scripts/` and their `make` targets
    (chapter [02](02-operations.md) explains each one);
  - database queries always go through
    `bash scripts/psql.sh -c "SELECT ..."`;
  - commands that write, delete or restore data are marked as such. Demo
    steps are read-only unless they say otherwise.
- **Times**: the scheduler runs on New York time. London is usually 5
  hours ahead, so 17:00 New York is 22:00 London.
- **Busy windows**: do not restart services or run end-to-end checks
  between 09:00 and 10:30 or between 16:55 and 17:50 New York time.
  `scripts/rebuild.sh`, `scripts/e2e.sh` and `scripts/snapshots.sh` (and
  their `make` targets) refuse inside these windows unless you pass
  `FORCE=1`; a plain `docker compose restart` is not checked, so look at
  the clock yourself. Chapter [26](26-scheduler-jobs.md) explains why.
- **Secrets**: `.env.docker` holds real secrets. Never commit it, paste it
  or print it. `.env.example` documents every setting without secrets.

## 3. Reading paths

### Path A: "I want to understand the whole system" (about a week)

1. [01 Architecture](01-architecture.md): containers, data ownership,
   request path, life of an analysis.
2. [02 Operations](02-operations.md): run it, look at it, keep it healthy.
3. The data stores: [20 PostgreSQL](20-postgresql.md),
   [23 Redis](23-redis.md), [21 Qdrant](21-qdrant.md),
   [22 Object storage](22-object-storage.md).
4. The services: [10 Gateway](10-gateway.md),
   [11 auth-service](11-auth-service.md),
   [12 core-service](12-core-service.md).
5. The AI engine, in this order: [13 ai-service](13-ai-service.md),
   [14 LLM layer](14-ai-llm-layer.md), [15 Agents](15-ai-agents.md),
   [16 Analysis pipeline](16-ai-analysis-pipeline.md),
   [17 RAG](17-ai-rag.md), [18 Knowledge and memory](18-ai-knowledge-memory.md),
   [19 Workflows and backtests](19-ai-workflows-backtest.md).
6. Cross-cutting topics: [24 Events](24-events.md),
   [25 Real-time](25-realtime-yjs.md), [26 Scheduler](26-scheduler-jobs.md),
   [27 Observability](27-observability.md), [28 Email](28-email.md),
   [29 Security](29-security.md), [30 Shared library](30-shared-library.md),
   [31 Testing and quality](31-testing-quality.md),
   [32 CI/CD](32-ci-cd.md).

### Path B: "I am preparing a demo or an interview" (about three hours)

1. Read sections 1 and 3 of [01](01-architecture.md) and section 3 of
   [16](16-ai-analysis-pipeline.md).
2. Read section 5 ("Design decisions and why") of
   [01](01-architecture.md), [14](14-ai-llm-layer.md),
   [16](16-ai-analysis-pipeline.md), [24](24-events.md) and
   [29](29-security.md). Reviewers and interviewers usually ask "why".
3. Rehearse the demo plan in section 5 below once, end to end.
4. Answer the "Check your understanding" questions of the chapters you
   will demo.

### Path C: "Something is broken right now"

1. `bash scripts/health.sh` (containers, when each scheduled job last
   succeeded, portfolios skipped by the last market-close run, firing
   alerts, and how many of the 100 daily NewsAPI requests were used and are
   left in the last 24 hours; a 15-ticker batch needs about 35).
2. The troubleshooting table in [02](02-operations.md) section 6.
3. Section 6 ("When things go wrong") of the chapter for the failing part.
4. Logs: `bash scripts/logs.sh <service>` and
   [27 Observability](27-observability.md).

## 4. All chapters

| Chapter | What you learn |
|---|---|
| [01 System architecture](01-architecture.md) | Every container and port, who owns which data, internal HTTP vs events, the path of a request, the life of an analysis, startup order, the development override, the tech stack and why. |
| [02 Running and operating the stack](02-operations.md) | Setup on a Mac, `make` targets, every operator script, deploying a change (restart vs rebuild), health checks, real analyses, tokens, end-to-end checks, backups, logs, troubleshooting. |
| [10 The gateway](10-gateway.md) | nginx routing, TLS and certificates, security headers and CSP, WebSocket upgrade, rate limits, the status page, why `/internal/*` is closed, the frontend at `/` and the internal listener for its server side. |
| [11 auth-service](11-auth-service.md) | Registration and email verification, login, access and refresh tokens, sessions and reuse detection, 2FA, Google/GitHub sign-in, roles, organizations, friends, presence, rate limits, account deletion. |
| [12 core-service](12-core-service.md) | Portfolios and the transaction ledger, holdings and cash, NAV snapshots, watchlists, research notes and documents, notifications, API keys and the public API, WebSockets, job heartbeats, cleanup of deleted users and organizations. |
| [13 ai-service overview](13-ai-service.md) | Folder layout, public and internal API, the serial analysis queue, startup, data providers and caches, settings, scripts. |
| [14 The LLM layer](14-ai-llm-layer.md) | The provider adapters (Google, Anthropic, Mistral, OpenAI), Gemini models and their fallback models, the key pool, pacing, 503 and 429 handling, timeouts, retries, thinking budgets, the Anthropic provider, JSON parsing, embeddings. |
| [15 The agents](15-ai-agents.md) | The ReAct loop, each data agent with its tools and validators, Bull, Bear, Judge and Reflection, evidence grounding. |
| [16 The analysis pipeline](16-ai-analysis-pipeline.md) | The LangGraph graph, the debate, the Judge, the recommendation engine's caps and penalties, position sizing, tiers, decisions. |
| [17 RAG](17-ai-rag.md) | Parsing, chunking, embeddings, hybrid search (vectors + BM25 with RRF), visibility, the ask endpoint, ingestion and deletion, auto-ingestion, evaluation. |
| [18 Knowledge graph and memory](18-ai-knowledge-memory.md) | Archived agent notes, near-duplicate handling, knowledge-graph facts, episodic and procedural memory. |
| [19 Workflows, tier monitoring and backtests](19-ai-workflows-backtest.md) | Tier monitoring and execution, the nightly review, auto-ingestion, recommendation replay, backtests and their metrics, how AI quality is measured (the evaluation layers). |
| [20 PostgreSQL](20-postgresql.md) | Schemas, SQLAlchemy sessions, Alembic migrations at startup, locking and idempotency, important tables, backups, Adminer. |
| [21 Qdrant](21-qdrant.md) | Vectors and similarity, the collection, payload filters, writing and reading points, re-embedding. |
| [22 Object storage](22-object-storage.md) | SeaweedFS with the S3 API, why it replaced MinIO, the storage module, keys and buckets, backups. |
| [23 Redis](23-redis.md) | Logical databases, caches, rate limits, the live user state, streams and pub/sub, persistence (AOF). |
| [24 Events between services](24-events.md) | Every event with its payload, producer and consumers, pub/sub vs durable streams, the transactional outbox, deduplication, retries. |
| [25 Real-time: WebSockets and Yjs](25-realtime-yjs.md) | Channels and their authorization, message shapes, collaborative editing with Yjs, persistence of rooms, open editors that follow access changes. |
| [26 The scheduler and its jobs](26-scheduler-jobs.md) | The crontab, each job (including the nightly deleted-user sweeps), when a run counts as failed, holidays, heartbeats and stale-job alerts, busy windows, backups, running one job by hand. |
| [27 Logs, metrics, alerts and dashboards](27-observability.md) | structlog, vector, Prometheus and its metrics, alert rules and their tests, Alertmanager, Grafana. |
| [28 Email](28-email.md) | The email module, every email the system sends, MailHog, production SMTP. |
| [29 Security](29-security.md) | TLS, headers, tokens, hashing, permissions, internal tokens, rate limits, API keys, input validation, secrets, exposure, error-body shapes, known limitations. |
| [30 The shared library](30-shared-library.md) | Every module in `shared/`, who uses it, and the rule for what belongs there. |
| [31 Testing and code quality](31-testing-quality.md) | Test suites and fixtures, integration and end-to-end checks, flake8, mypy `--strict`, the no-comments code style, CI, writing a new test. |
| [32 CI/CD](32-ci-cd.md) | Basic CI/CD ideas, the pull-request checks, the stack smoke gate on `main`, the deploy workflow and script, the cloud host, keys and secrets, rollback, troubleshooting. |

Reference documents next to this handbook:
[API_SPEC](../API_SPEC.md) (every route),
[ERD](../ERD.md) (every table and column),
[DISASTER_RECOVERY](../DISASTER_RECOVERY.md) (backup and restore),
[DEPLOYMENT](../DEPLOYMENT.md) (the cloud host and continuous deployment),
[MODULE_MAPPING](../MODULE_MAPPING.md) (the 42 subject modules and where
each is implemented),
[DESIGN](../DESIGN.md),
[PROJECT_STRUCTURE](../PROJECT_STRUCTURE.md),
[CHANGES_SINCE_EVALUATION](../CHANGES_SINCE_EVALUATION.md) (what changed
after the 42 evaluation, tag `v1.0-42-evaluated`),
[MODEL_COMPARISON](../MODEL_COMPARISON.md) (Gemini and Claude compared
on the same batch runs).
What is not built yet is listed in section 8.

## 5. A 30-minute demo plan

Each step uses the demo walkthrough of one chapter. Rehearse it once the
day before, outside the busy windows, and run
`bash scripts/health.sh` just before you start.

| Minutes | Show | Chapter demo |
|---|---|---|
| 0-3 | The containers, the public door and the status page. | [01](01-architecture.md) steps 1-4 |
| 3-6 | Internal routes are closed; schema ownership. | [01](01-architecture.md) steps 5-6, [10](10-gateway.md) |
| 6-10 | Register a user, read the verification email in MailHog, log in, refresh, log out, look at the session rows. | [11](11-auth-service.md), [28](28-email.md) |
| 10-14 | A portfolio: deposit, buy, the ledger-derived holdings and cash, a snapshot. | [12](12-core-service.md) |
| 14-20 | A finished analysis: open a run folder's result, walk through agents, debate, Judge, the confidence notes and the tiers. Start a new one-ticker analysis and follow its log while you talk. | [13](13-ai-service.md), [15](15-ai-agents.md), [16](16-ai-analysis-pipeline.md) |
| 20-23 | Upload a document, watch it become searchable, ask a question about it. | [17](17-ai-rag.md), [24](24-events.md) |
| 23-26 | Live updates over a WebSocket. | [25](25-realtime-yjs.md) |
| 26-30 | Monitoring: Prometheus queries, alert rules, Grafana, the scheduled jobs and backups. | [26](26-scheduler-jobs.md), [27](27-observability.md) |

One analysis usually takes about 1.5 to 4 minutes (up to 10 minutes more when a new SEC filing must be summarised first) and uses LLM quota. For a tight time
slot, show a finished run folder and start a new analysis only as a live
"it really runs" moment.

## 6. Glossary of the most used terms

| Term | Meaning in AlphaMind | Chapter |
|---|---|---|
| Analysis | One run of the agent graph for one ticker, stored in `ai.analyses`. | [16](16-ai-analysis-pipeline.md) |
| Data agent | An LLM agent that collects one kind of evidence with tools (Market, Technical, Fundamental, Filing, News, Sentiment, Macro, Risk, Portfolio). | [15](15-ai-agents.md) |
| ReAct loop | Thought -> action (tool call) -> observation, repeated until the agent finishes. | [15](15-ai-agents.md) |
| Bull / Bear | The two debate sides that argue for and against the stock. | [16](16-ai-analysis-pipeline.md) |
| Judge | The step that weighs the debate and returns BUY, HOLD or SELL with a confidence. | [16](16-ai-analysis-pipeline.md) |
| Reflection | The step that checks the debate's claims against the evidence. | [16](16-ai-analysis-pipeline.md) |
| Recommendation | The saved verdict: action, confidence, reasoning and notes that explain every adjustment. | [16](16-ai-analysis-pipeline.md) |
| Tier | A concrete order proposal (side, target price, quantity, expiry window) attached to a recommendation. | [16](16-ai-analysis-pipeline.md), [19](19-ai-workflows-backtest.md) |
| Decision | The portfolio owner's answer to a recommendation: followed, ignored, partial or declined. | [16](16-ai-analysis-pipeline.md) |
| Tier monitoring | The daily job that executes accepted tiers whose price was reached and expires old ones. | [19](19-ai-workflows-backtest.md) |
| Ledger | The list of a portfolio's transactions; holdings and cash are computed by replaying it. | [12](12-core-service.md) |
| NAV snapshot | The portfolio's total value at the end of a trading day. | [12](12-core-service.md) |
| HIGHEST / CHEAP tier | The two LLM quality levels. With Google: two Gemini Flash models (a primary and a fallback), and two Lite models; with Anthropic: Sonnet and Haiku, without a fallback. | [14](14-ai-llm-layer.md) |
| RAG | Retrieval-augmented generation: search the document library first, then let the LLM answer from what was found. | [17](17-ai-rag.md) |
| Embedding | A list of numbers that represents the meaning of a text; similar texts get similar lists. | [21](21-qdrant.md) |
| BM25 | A classic keyword-ranking formula, used next to vectors for hybrid search. | [17](17-ai-rag.md) |
| Knowledge graph (KG) | Facts extracted from agent notes as (subject, relation, object) rows. | [18](18-ai-knowledge-memory.md) |
| Episodic memory | The track record of earlier calls on the same ticker and how they turned out. | [18](18-ai-knowledge-memory.md) |
| Durable event | An event kept in a Redis stream until a consumer acknowledges it. | [24](24-events.md) |
| Pub/sub event | A Redis message delivered to whoever listens right now, then gone. | [24](24-events.md) |
| Transactional outbox | A table (`core.event_outbox`) where an event is written in the same transaction as the change it announces, then published to Redis until it gets through. | [24](24-events.md) |
| Internal route | A `/internal/*` route for service-to-service calls, closed at the gateway and protected by `X-Internal-Token`. | [01](01-architecture.md), [29](29-security.md) |
| Access / refresh token | Short-lived JWT for requests; long-lived token to get a new access token. | [11](11-auth-service.md) |
| Heartbeat | A scheduled job's "I succeeded" report, exported as a Prometheus metric. | [26](26-scheduler-jobs.md) |
| Busy window | A time range when the scheduler's daily jobs run and restarts are refused. | [26](26-scheduler-jobs.md) |
| Development override | `docker-compose.override.yml`: development image stage (its Dockerfile `CMD` runs uvicorn without `--reload`, so a code change needs `docker compose restart <service>`), the service's source and `shared/` bind-mounted, client address and scheme taken from the gateway's `X-Forwarded-*` headers, and ai-service at `LOG_LEVEL=DEBUG`. | [01](01-architecture.md) |

## 7. Conventions this handbook follows

- Settings are named exactly as in the code, with their default and the
  file they live in. Change a setting in `.env.docker`, then recreate the
  container (chapter [02](02-operations.md) explains restart vs
  recreate vs rebuild).
- "Why" sections give the reason for a design, often a real failure that
  the design prevents. They describe the current code, not its history;
  there is no change log in the handbook.
- Known issues that are not fixed yet are written as such, in the chapter
  where they matter.

## 8. Status and open items

Everything this handbook describes is built and runs on the real stack
(PostgreSQL, Redis, Qdrant, object storage, the real LLM and market-data
providers). The test suites and the end-to-end checks are in chapter
[31](31-testing-quality.md). This is the one list of what is not built or
not finished; the chapters give the details.

| Item | State | Chapter |
|---|---|---|
| Frontend | Built: the Next.js app in `frontend/`, served by the gateway at `/`; its server side calls the API on the gateway's internal listener (port 8080). The Privacy Policy and Terms of Service pages are written (`frontend/app/(legal)/privacy`, `terms`; linked from the footer). Every route can also be tried in Swagger (`/docs/<service>/`). | [01](01-architecture.md) section 3.11, [10](10-gateway.md) section 3.3 |
| Cloud deployment | One AWS virtual machine runs the same Docker Compose stack at `https://alphamind-research.duckdns.org:8443`, with `APP_ENV=production` and continuous deployment from `main` (the head of `main` is deployed only after every workflow run of that exact commit succeeded; per-service checks run only for the services a commit touches). The host keeps its scheduler stopped, so it makes no automatic backups and runs no scheduled jobs; backups are made on the local stack, and the jobs that spend no quota (purge-unverified, notification-retention, the deleted-user sweeps) can be run there by hand from the scheduler image (DEPLOYMENT section 3). | [32](32-ci-cd.md), [../DEPLOYMENT.md](../DEPLOYMENT.md) |
| Top-level README | `README.md` at the repository root: the project, how to run it, the team, the modules and the use of AI. Other READMEs: `tests/README.md`, `infra/gateway/certs/README.md` and `frontend/README.md` (how to run the frontend, its environment variables and its structure). | - |
| Calibration of the confidence rules | The penalties and caps in `ai/recommendation/engine.py`, the HOLD flat band of the accuracy score (`review_workflow.py::_HOLD_FLAT_BAND`) and the procedural-memory score bar (`_MIN_SCORE_TO_REMEMBER`) are fixed constants. Nothing derives them from scored outcomes yet; that needs enough rows in `ai.recommendation_metrics`. | [16](16-ai-analysis-pipeline.md) section 3.12, [19](19-ai-workflows-backtest.md) section 3.11 |
| Debate and Reflection quality signals | Debate convergence and Reflection retries are stored per analysis but not aggregated into a metric. | [19](19-ai-workflows-backtest.md) section 3.11 |
| Restore drill | `make restore-drill` restores a backup into a throwaway compose project and verifies every part, but it has not yet been run against a real nightly backup of the object-storage format written by the S3 copy. | [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) section 6 |
| Known security limitations | Listed with their reasons. | [29](29-security.md) section 9 |
