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
GET    /api/v1/portfolios/{portfolio_id}/snapshots?start_date=...&end_date=...
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

**Currency (added 2026-08-20)**: `POST /api/v1/portfolios`' `currency` field
(`Currency` enum: `USD`/`EUR`/`GBP`, defaults to `USD`) is now actually
validated at creation — previously accepted but never enforced or used
anywhere downstream. `GET .../snapshots` is the one place it now has a real
effect: same real `{snapshot_date, total_value, cash_balance}` history as
`GET /internal/portfolios/{portfolio_id}/snapshots` (§4), ownership-checked
(`_require_readable` — same visibility rules as `GET /api/v1/portfolios/
{portfolio_id}`), plus for a non-USD portfolio, a best-effort, display-only
conversion: `total_value_converted`, `cash_balance_converted`, `fx_rate`, and
`display_currency` are populated using the portfolio's CURRENT real USD→
currency rate (see `GET /internal/fx-rate` under §4) applied uniformly to
every row in the response — not a real historical per-date rate, a
deliberate simplification since Jenny's real holdings are all US-listed and
this is display-only, not real multi-currency accounting. If the FX lookup
fails (rate provider unreachable/no data), the four conversion fields come
back `null` and the real USD `total_value`/`cash_balance` are still
returned — a currency-display hiccup never blocks real portfolio data. For a
USD portfolio, all four conversion fields are always `null` (no conversion
needed). No change to `transactions`, `holdings`, or any other real
portfolio accounting — this is a read-only overlay on top of unchanged real
USD bookkeeping.

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
GET    /internal/portfolios/{portfolio_id}/owner
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

`POST .../transactions` now also accepts `transaction_type: "stock_split"`
with a required `split_ratio` (e.g. `2.0` for a 2-for-1 forward split, `0.5`
for a 1-for-2 reverse split, `1.05` for a 5% stock dividend) — one mechanism
covers all three real corporate actions, since each is mechanically
identical (rescale the existing holding's quantity and average cost by
`split_ratio`, zero cash effect). Requires an existing holding for that
ticker — nothing to rescale otherwise. Two more real, core-service-hosted
internal endpoints this portfolio's data feeds — `.../snapshots` and (new
2026-08-20) `.../cash-flows` — are documented under §4 (ai-service)
alongside the backtest workflows that are their only real callers.

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
result" mode. It requires a logged-in user (round111); the owner comes from
the JWT only, and a `portfolio_id` must belong to the caller (owner, or a
member of the portfolio's organization when the portfolio is not private --
checked through core-service `GET /internal/portfolios/{id}/owner`), else
404. System callers (core-service's market-open job, the owner's run scripts)
use `POST /internal/analyses` on the docker network instead. Optional `portfolio_id` in
the request body: when set and the Judge's verdict is BUY/SELL (not HOLD), a
real position-sizing step runs and writes real, pending recommendation tiers
sized against that portfolio's actual cash/holdings; omitted entirely, the
analysis is review-only and produces no tiers regardless of the verdict.

`GET /api/v1/analyses/{id}` returns full detail: every agent run, the debate
transcript, reflection history, and the final recommendation, in one
response. `POST .../decisions` confirms followed/ignored/partial/declined
against that analysis's recommendation. Since round111 every route in this
section requires login; an analysis is visible to its user, or (for a
system analysis with no user) to whoever may access its portfolio; anything
else returns 404. The same ownership rule applies to decisions (outcome and
review routes) and backtests (`GET /backtests/{id}` and `/metrics` match the
backtest's user; creates require login and portfolio access).

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
POST   /api/v1/backtests/recommendation-replay
POST   /api/v1/backtests/portfolio
GET    /api/v1/backtests/{backtest_id}
GET    /api/v1/backtests/{backtest_id}/metrics
```

Same async-return pattern as analyses — `"pending"` immediately, real work in
a background task. Three distinct strategy types, one shared schema/polling
contract (`GET .../{backtest_id}` + `.../metrics`):

- `POST /api/v1/backtests` (`ai/workflows/backtest_workflow.py`) —
  **redesigned 2026-08-20**, per Jenny's own direct correction: no longer a
  hypothetical day-1-close buy-and-hold. Replays every real
  `RecommendationTier` that actually reached `TRIGGERED` for `ticker` within
  `[start_date, end_date]` (a ticker can have more than one real triggered
  tier — e.g. "buy 10 at $150, buy 15 more at $145" as two tiers of one
  recommendation). Only tiers triggered WITHIN the window count — a tier's
  full real history clipped to a display window is deliberately NOT the
  same thing; once triggered, a tier is considered fully settled right
  there. New optional `portfolio_id` field — `None` (default) means every
  real triggered tier for this ticker across every real portfolio; a real
  `portfolio_id` narrows to just that one portfolio's own triggers. If
  nothing triggered for this ticker (optionally, in this portfolio) in the
  window, the request fails fast with a real `422 ValidationAppError` —
  never a silent fall-back to a hypothetical trade.
- `POST /api/v1/backtests/recommendation-replay`
  (`ai/workflows/recommendation_replay_workflow.py`) — replays every
  `RecommendationTier` that actually reached `TRIGGERED`, across the AI's
  whole real history, as a real chronological multi-ticker trade sequence.
  No request body fields beyond `benchmark_symbol`/`initial_capital` — the
  trade universe and date range are discovered from real trigger history,
  not chosen by the caller.
- `POST /api/v1/backtests/portfolio`
  (`ai/workflows/portfolio_backtest_workflow.py`) — a real backtest of one
  real portfolio's actual composition over time, sourced from
  `core.portfolio_snapshots` (a real daily NAV time series already written
  by the `market-close` scheduled job). Replaces the previous `portfolio_
  evaluation`/`ai.portfolio_evaluations` design below, which was real, dead
  code (built, but never wired to anything and never actually read back) —
  removed 2026-08-19, see `HANDOFF_CONSOLIDATED.md` §32. **Updated
  2026-08-20**: now computes a real time-weighted return (TWR), excluding
  every real DEPOSIT/WITHDRAWAL cash flow in the window from the return
  calculation (a real dividend and every real buy/sell are still correctly
  included — they're genuine investment activity, already reflected in
  `total_value`). `ending_capital`/`strategy_return`/every other metric on
  the response therefore answer "how did the investing itself perform,"
  NOT "how much money is literally in the account," whenever a real
  deposit or withdrawal happened inside the window — see
  `HANDOFF_CONSOLIDATED.md` §34 for the full real formula.

### Market data
```
GET    /api/v1/market/instruments/{ticker}
GET    /api/v1/market/instruments/{ticker}/prices?interval=1d
```

### Analytics
```
GET    /api/v1/analytics/agents
```

`GET /api/v1/analytics/portfolio/{portfolio_id}` removed 2026-08-19 — real,
dead code (the table backing it, `ai.portfolio_metrics`, had zero writers,
ever). Real portfolio-level performance metrics now come from
`POST /api/v1/backtests/portfolio` + `GET /api/v1/backtests/{id}` above.

### Internal (service-to-service / scheduler only)
```
POST   /internal/analyses
GET    /internal/analyses/{analysis_id}
GET    /internal/analyses/{analysis_id}/detail
GET    /internal/recommendations/latest?ticker=...
POST   /internal/review/run
POST   /internal/scheduler/tier-monitoring
POST   /internal/scheduler/rag-ingest
POST   /internal/market/{ticker}/refresh
GET    /internal/portfolios/{portfolio_id}/snapshots?start_date=...&end_date=...
GET    /internal/portfolios/{portfolio_id}/cash-flows?start_date=...&end_date=...
GET    /internal/fx-rate?from_currency=...&to_currency=...
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

`GET /internal/portfolios/{portfolio_id}/snapshots` returns one real
`{snapshot_date, total_value, cash_balance}` row per real trading day this
portfolio has a `core.portfolio_snapshots` row for (written daily by the
`market-close` scheduled job) — the raw data `POST /api/v1/backtests/
portfolio` above is built from. `start_date`/`end_date` query params are
optional; omitted, returns the portfolio's entire real snapshot history.

`GET /internal/portfolios/{portfolio_id}/cash-flows` (added 2026-08-20, both
`start_date`/`end_date` required — unlike `snapshots` above, every real
caller always has a concrete backtest window in hand) returns every real
DEPOSIT/WITHDRAWAL transaction dated in the window as `{transaction_date,
amount}`, with a signed `amount` (positive for a DEPOSIT, negative for a
WITHDRAWAL) — feeds the real TWR fix in `POST /api/v1/backtests/portfolio`
above; both endpoints are hosted by core-service but have no real callers
outside this workflow, which is why they're documented here rather than
under §3.

`GET /internal/fx-rate` (added 2026-08-20) returns `{"rate": <float>}` — the
real, CURRENT amount of `to_currency` per 1 `from_currency`, or a 404 if no
rate is available. `core-service/clients/ai_client.py::get_fx_rate` is the
real (only) caller, feeding `GET /api/v1/portfolios/{id}/snapshots` (§3)'s
real USD→portfolio-currency display conversion. Same-currency requests
(`from_currency == to_currency`) never reach this endpoint — the client
short-circuits to a rate of `1` locally. Implementation reuses the market
data provider's light, non-persisting `get_current_price` method against a
synthetic yfinance FX-pair ticker (`f"{from}{to}=X"`, e.g. `"USDGBP=X"`) —
deliberately NOT the heavier `/api/v1/market/instruments/{ticker}/prices`
route above, which persists a real `Instrument`/`PriceBar` row per call and
would pollute stock-oriented tables with a mislabeled `asset_type="equity"`
row for a synthetic pair. Redis-cached for 1 hour — a real FX rate doesn't
need fresher-than-hourly precision for this project's display-only use case.

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