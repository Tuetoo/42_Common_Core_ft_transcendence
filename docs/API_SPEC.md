# AlphaMind — API Specification

## 1. Base paths and gateway routing

All three services sit behind an nginx gateway (`infra/gateway/nginx.conf`),
exposed on `http://localhost:8000`. The gateway forwards the raw
`Authorization` header as-is — it does no JWT validation itself; each service
independently decodes and verifies its own tokens (`shared/security.py`).

```
/api/v1/auth/*           -> auth-service
/api/v1/organizations/*  -> auth-service
/api/v1/connections/*    -> auth-service
/api/v1/portfolios/*     -> core-service
/api/v1/watchlists/*     -> core-service
/api/v1/research/*       -> core-service
/api/v1/notifications/*  -> core-service
/api/v1/api-keys/*       -> core-service
/api/v1/public/*         -> core-service (API-key-authenticated, see §5)
/api/v1/analyses/*       -> ai-service
/api/v1/decisions/*      -> ai-service
/api/v1/backtests/*      -> ai-service
/api/v1/market/*         -> ai-service
/api/v1/analytics/*      -> ai-service
/ws/*                    -> core-service (WebSocket upgrade)
```

`/internal/*` exists on every service but is **not** proxied by the gateway —
reachable only on the Docker-internal network, for service-to-service calls
(`clients/*.py`) and the scheduler. `/health` and `/metrics` are likewise
internal-only (Prometheus scrapes them directly).

## 2. auth-service

### Authentication
```
POST   /api/v1/auth/register
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
POST   /api/v1/auth/refresh
POST   /api/v1/auth/verify-email
POST   /api/v1/auth/password-reset/request
POST   /api/v1/auth/password-reset/confirm
GET    /api/v1/auth/oauth/{provider}/authorize
GET    /api/v1/auth/oauth/{provider}/callback
POST   /api/v1/auth/2fa/setup
POST   /api/v1/auth/2fa/verify
POST   /api/v1/auth/2fa/disable
```

### Current user
```
GET    /api/v1/auth/me
PATCH  /api/v1/auth/me
POST   /api/v1/auth/me/avatar
```

Avatar uploads use local disk storage (`AVATAR_STORAGE_DIR`), deliberately
*not* MinIO like research documents — avatars are small, low-volume, and
never consumed by another service the way research documents are read by
ai-service's RAG ingestion.

### Organizations
```
GET    /api/v1/organizations
POST   /api/v1/organizations
GET    /api/v1/organizations/{org_id}
PATCH  /api/v1/organizations/{org_id}
DELETE /api/v1/organizations/{org_id}
GET    /api/v1/organizations/{org_id}/members
POST   /api/v1/organizations/{org_id}/members/{user_id}/remove
PATCH  /api/v1/organizations/{org_id}/members/{user_id}/role
GET    /api/v1/organizations/{org_id}/roles
POST   /api/v1/organizations/{org_id}/roles
POST   /api/v1/organizations/{org_id}/invitations
POST   /api/v1/organizations/invitations/{token}/accept
```

### Social connections
```
GET    /api/v1/connections
POST   /api/v1/connections
POST   /api/v1/connections/{connection_id}/accept
POST   /api/v1/connections/{connection_id}/block
DELETE /api/v1/connections/{connection_id}
```

### Internal (service-to-service only)
```
GET    /internal/users/{user_id}
GET    /internal/users/{user_id}/organizations
GET    /internal/users/{user_id}/notification-settings
GET    /internal/organizations/{org_id}
GET    /internal/connections/check
```

## 3. core-service

### Portfolios
```
GET    /api/v1/portfolios
POST   /api/v1/portfolios
GET    /api/v1/portfolios/{portfolio_id}
PATCH  /api/v1/portfolios/{portfolio_id}
DELETE /api/v1/portfolios/{portfolio_id}
GET    /api/v1/portfolios/{portfolio_id}/holdings
POST   /api/v1/portfolios/{portfolio_id}/holdings
PATCH  /api/v1/portfolios/{portfolio_id}/holdings/{holding_id}
DELETE /api/v1/portfolios/{portfolio_id}/holdings/{holding_id}
GET    /api/v1/portfolios/{portfolio_id}/transactions
POST   /api/v1/portfolios/{portfolio_id}/transactions
```

**Note**: portfolio-scoped decision tracking (`POST .../decisions`) that
originally lived here has been retired — see §4's
`/api/v1/analyses/{id}/decisions` in ai-service. This was a deliberate
migration, not a removed feature; no frontend depended on the core-service
version at the time it moved.

### Watchlists
```
GET    /api/v1/watchlists
POST   /api/v1/watchlists
POST   /api/v1/watchlists/{watchlist_id}/items
DELETE /api/v1/watchlists/{watchlist_id}/items/{ticker}
```

### Research (notes, comments, documents)
```
GET    /api/v1/research/notes
POST   /api/v1/research/notes
GET    /api/v1/research/notes/{note_id}
PATCH  /api/v1/research/notes/{note_id}
DELETE /api/v1/research/notes/{note_id}
POST   /api/v1/research/notes/{note_id}/comments

GET    /api/v1/research/documents
POST   /api/v1/research/documents
GET    /api/v1/research/documents/{document_id}
DELETE /api/v1/research/documents/{document_id}
GET    /api/v1/research/documents/{document_id}/content
```

`POST /api/v1/research/documents` is multipart; `category` and optional
`ticker` are required form fields, not inferred from the file — see
`docs/ERD.md` §3 for why. `GET .../content` streams the raw bytes back out of
MinIO; it's the only way to actually retrieve a document's content —
`storage_url` in the JSON response is an internal object key, not a browsable
URL. `DELETE .../documents/{id}` cascades: deletes the MinIO object, the
`core.documents` row, and publishes `document.deleted` (§6) so ai-service can
clean up the matching Qdrant/BM25 vectors and its own `ai.document_chunks`/
`ai.embedding_jobs` rows — a document delete is never partial across the two
services.

### Notifications, API keys
```
GET    /api/v1/notifications
POST   /api/v1/notifications/{notification_id}/read
GET    /api/v1/api-keys
POST   /api/v1/api-keys
DELETE /api/v1/api-keys/{key_id}
```

### Public API (§5 — API-key authenticated, not unauthenticated)
```
GET    /api/v1/public/portfolios/{portfolio_id}
GET    /api/v1/public/portfolios/{portfolio_id}/holdings
PUT    /api/v1/public/portfolios/{portfolio_id}/holdings/{holding_id}
POST   /api/v1/public/watchlists/{watchlist_id}/items
DELETE /api/v1/public/watchlists/{watchlist_id}/items/{ticker}
```

### WebSocket
```
WS     /ws/{channel}          generic pub/sub relay
WS     /ws/yjs/{note_id}      Yjs CRDT binary relay for collaborative note editing
```

`/ws/{channel}` is a generic relay — the channel string itself carries the
real meaning, not a fixed enum of endpoints. Real channel names currently in
use: `analysis:{analysis_id}` (agent-run/recommendation progress for one
analysis), `user:{user_id}` (a user's own notification stream — analysis
completions, recommendations ready), `presence:{user_id}` (online/offline
status changes for a user's connections).

### Internal (service-to-service / scheduler only)
```
GET    /internal/portfolios/{portfolio_id}/holdings
GET    /internal/portfolios/{portfolio_id}/cash-balance
POST   /internal/portfolios/{portfolio_id}/transactions
POST   /internal/scheduler/market-open
POST   /internal/scheduler/market-close
GET    /internal/tracked-tickers
GET    /internal/documents/filenames
POST   /internal/documents
```

The `portfolios/{id}/holdings`, `cash-balance`, and `transactions` endpoints
are ai-service's real callers (`clients/core_client.py`) into a portfolio's
real state — used by the Portfolio agent's own tool, by position-sizing
(needs real available cash to size a BUY against), and by the daily
tier-monitoring job (executes a real transaction once a tier's price
condition is met). No per-call ownership check on any of these — internal,
Docker-network-only, service-to-service, not user-facing.

`tracked-tickers` returns the union of every portfolio's holdings and every
watchlist's items across all users — what ai-service's RAG auto-ingestion
job should keep fresh. `documents/filenames` / `POST documents` are the
automated-ingestion counterparts of the real, interactive
`GET/POST /api/v1/research/documents` — used by the daily filing/news
ingestion job (owned by a well-known system user, not a real logged-in one)
rather than a multipart upload, since the job already has the raw text in
memory.

## 4. ai-service

### Analyses
```
POST   /api/v1/analyses
GET    /api/v1/analyses
GET    /api/v1/analyses/{analysis_id}
GET    /api/v1/analyses/{analysis_id}/recommendation
GET    /api/v1/analyses/{analysis_id}/debate
GET    /api/v1/analyses/{analysis_id}/reflection
POST   /api/v1/analyses/{analysis_id}/decisions
```

`POST /api/v1/analyses` returns immediately with `status: "pending"` — the
actual agent graph run happens in a detached background task
(`ai/workflows/analysis_workflow.py`). There is no synchronous "wait for the
result" mode. It's callable two ways: an authenticated frontend request
(JWT-derived user), or core-service's scheduled market-open job calling
directly with no auth header at all (a batch analysis with no single owning
user — see `deps.py::get_current_user_optional`). Optional `portfolio_id` in
the request body: when set and the Judge's verdict is BUY/SELL (not HOLD), a
real position-sizing step runs and writes real, pending recommendation tiers
sized against that portfolio's actual cash/holdings; omitted entirely, the
analysis is review-only and produces no tiers regardless of the verdict.

`GET /api/v1/analyses/{id}` returns full detail: every agent run, the debate
transcript, reflection history, and the final recommendation, in one
response. `POST .../decisions` confirms followed/ignored/partial against
that analysis's recommendation.

### Decisions
```
GET    /api/v1/decisions
GET    /api/v1/decisions/{decision_id}/outcome
POST   /api/v1/decisions/{decision_id}/review
GET    /api/v1/decisions/{decision_id}/review
```

`POST .../review` generates a fresh, LLM-written critique of one past
decision (was it a good call given what was actually knowable at the time,
not just whether it made money) — user-triggered, not automatic, and
generates a new one on every call rather than returning a cached result.
`GET .../review` retrieves the most recently generated one.

### Backtests
```
POST   /api/v1/backtests
GET    /api/v1/backtests/{backtest_id}
GET    /api/v1/backtests/{backtest_id}/metrics
```

Same async-return pattern as analyses — `"pending"` immediately, real work in
a background task (`ai/workflows/backtest_workflow.py`). Current engine
scope: single-ticker buy-and-hold vs. a benchmark, not a multi-trade strategy
simulator (see `docs/ERD.md`'s `backtest_trades` note). Distinct from
`portfolio_evaluation` (§4's internal section) — this simulates a
hypothetical position, that evaluates a real portfolio's actual holdings.

### Market data
```
GET    /api/v1/market/instruments/{ticker}
GET    /api/v1/market/instruments/{ticker}/prices?interval=1d
```

### Analytics
```
GET    /api/v1/analytics/agents
GET    /api/v1/analytics/portfolio/{portfolio_id}
```

`GET .../portfolio/{id}` is the read side of `POST /internal/portfolios/
{id}/evaluate` (real Sharpe/Sortino/drawdown, computed against a portfolio's
actual holdings) — the write only happens via the internal, scheduler-driven
path below; there's no user-facing "evaluate now" trigger yet.

### Internal (service-to-service / scheduler only)
```
GET    /internal/analyses/{analysis_id}
GET    /internal/recommendations/latest?ticker=...
POST   /internal/review/run
POST   /internal/scheduler/tier-monitoring
POST   /internal/scheduler/rag-ingest
POST   /internal/market/{ticker}/refresh
POST   /internal/portfolios/{portfolio_id}/evaluate
```

`POST /internal/review/run` runs `ai/workflows/review_workflow.py`'s two
evaluation passes (AI-accuracy scoring for all recommendations, decision-
outcome evaluation for confirmed user decisions) — wired to
`infra/scheduler/review-run.sh`, daily at 2:00 AM ET.

`POST /internal/scheduler/tier-monitoring` runs the daily check of every
accepted (or still-acceptable) recommendation tier for a real trigger
(today's actual high/low crossing the target price) or expiry — the hook
exists and works but is not yet wired into `infra/scheduler/crontab`.

`POST /internal/scheduler/rag-ingest` runs both the per-ticker (filing +
news) and the separate macro-research auto-ingestion passes in one call —
wired to `infra/scheduler/rag-ingest.sh`, daily.

`POST /internal/market/{ticker}/refresh` proactively warms
`domains/market`'s price-bar cache for one ticker ahead of real agent
demand — not the only way the cache gets populated (agents themselves
trigger a refresh on a cache miss/stale read via `ai/tools/market_data_
cache.py`), and not yet wired to a cron entry.

`POST /internal/portfolios/{portfolio_id}/evaluate` computes a real
portfolio's actual performance (vs. a benchmark) from its real transaction
history, writing to `ai.portfolio_evaluations` — read back via
`GET /api/v1/analytics/portfolio/{id}` above. Not yet on a schedule.

## 5. The "public API" (developer API keys)

core-service's `/api/v1/public/*` routes are authenticated by an `X-API-Key`
header, not a JWT — keys are issued/managed via `/api/v1/api-keys`. "Public"
here means "the developer-facing programmatic API surface," not
"unauthenticated." `PublicAPIRateLimitMiddleware` validates the key and
resolves its owning user/organization before the request reaches the route
handler.

## 6. Cross-service events (Redis pub/sub, not HTTP)

Defined in `shared/events_registry.py::EventName`, published via
`shared/events.py::publish_cross_service`, consumed via
`CrossServiceSubscriber`. Not HTTP endpoints; listed here because they're as
much a part of the API surface between services as the internal REST calls.

| Event | Published by | Consumed by | Status |
|---|---|---|---|
| `document.uploaded` | core-service | ai-service | wired, drives RAG ingestion |
| `document.deleted` | core-service | ai-service | wired, cleans up Qdrant/BM25 + `ai.document_chunks`/`ai.embedding_jobs` |
| `document.status_changed` | ai-service | core-service | wired — two independent subscribers: a real DB write (`Document.status`, via the durable subscriber) and a separate WebSocket broadcast (plain pub/sub, deliberately not durable — see `event_listener.py`'s own note on why broadcast-only doesn't need delivery guarantees the way a DB write does) |
| `analysis.completed` | ai-service | core-service | wired — WebSocket broadcast (`analysis:{id}`/`user:{id}`) + a real notification |
| `recommendation.ready` | ai-service | core-service | wired — same as `analysis.completed` (WebSocket + notification) |
| `analysis.started` / `.failed` | ai-service | — | published, no subscriber yet |
| `decision.outcome.evaluated` | ai-service | — | published, no subscriber yet |
| `organization.member.joined` | auth-service | core-service | wired — creates a real welcome notification |
| `user.presence.changed` | auth-service | core-service | wired (WebSocket relay, `presence:{user_id}`) |
| `portfolio.created` | core-service | — | published, no subscriber yet |

"No subscriber yet" means exactly that — the event fires correctly and can be
subscribed to at any time; nothing currently listens for it.