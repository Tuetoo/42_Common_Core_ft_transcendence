# AlphaMind v4.0
# API_SPEC.md

Version: 4.0 

---

# 1. Overview

All external traffic goes through the Nginx gateway at
`http://localhost:8000` (dev) — the frontend never talks to a service's
own port directly. Internally, each service also has its own Swagger
UI, reachable in development via the gateway at `/docs/auth`,
`/docs/core`, `/docs/ai`.

## Base Paths (through the gateway)

```text
/api/v1/auth/*            -> auth-service
/api/v1/organizations/*   -> auth-service
/api/v1/connections/*     -> auth-service
/api/v1/portfolios/*      -> core-service
/api/v1/research/*        -> core-service
/api/v1/notifications/*   -> core-service
/api/v1/public/*          -> core-service (Public API module, W4)
/ws/*                     -> core-service (WebSocket)
/api/v1/analyses/*        -> ai-service
/api/v1/market/*          -> ai-service
/api/v1/analytics/*       -> ai-service
```

`/internal/*` exists on every service but is **not** proxied by the
gateway — reachable only on the Docker-internal network, service to
service.

## Response Format

Success (single resource):

```json
{
  "data": { },
  "request_id": "a1b2c3d4-..."
}
```

Success (list, paginated):

```json
{
  "items": [ ],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

Error:

```json
{
  "error": {
    "code": "invalid_credentials",
    "message": "Email or password incorrect",
    "request_id": "a1b2c3d4-..."
  }
}
```

`code` values are lower_snake_case, matching
`shared.exceptions.AppError.code` subclasses. `request_id` is
present on both success and error responses (set by each service's
request-logging middleware) so a frontend bug report can be correlated
with a specific backend log line, in a specific service.

---

# 2. Authentication (auth-service)

```text
POST   /api/v1/auth/register
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh
POST   /api/v1/auth/logout
GET    /api/v1/auth/oauth/{provider}/authorize      (google | github)
GET    /api/v1/auth/oauth/{provider}/callback
POST   /api/v1/auth/2fa/setup
POST   /api/v1/auth/2fa/verify
POST   /api/v1/auth/2fa/disable
```

Login response includes an access token (15 min) and a refresh token
(30 days) — both issued exclusively by
`services/auth-service/app/security.py`.

# 3. User Profile (auth-service)

```text
GET    /api/v1/auth/me
PATCH  /api/v1/auth/me
POST   /api/v1/auth/me/avatar
```

# 4. Connections / Presence (auth-service)

```text
POST   /api/v1/connections                (send a request)
POST   /api/v1/connections/{id}/accept
POST   /api/v1/connections/{id}/block
GET    /api/v1/connections                (my connections + online status)
```

# 5. Organizations (auth-service)

```text
POST   /api/v1/organizations
GET    /api/v1/organizations/{id}
PATCH  /api/v1/organizations/{id}
DELETE /api/v1/organizations/{id}
POST   /api/v1/organizations/{id}/invitations
POST   /api/v1/organizations/{id}/members/{user_id}/remove
PATCH  /api/v1/organizations/{id}/members/{user_id}/role
```

# 6. Portfolios (core-service)

```text
POST   /api/v1/portfolios
GET    /api/v1/portfolios/{id}
PATCH  /api/v1/portfolios/{id}
DELETE /api/v1/portfolios/{id}
GET    /api/v1/portfolios                 (paginated, mine + org-shared)
```

`PortfolioRead.owner` is populated by core-service calling
`app/clients/auth_client.py::get_user_ref` — not a local join (see
`COMMON_LIBRARY_README.md §4`).

# 7. Holdings & Transactions (core-service)

```text
POST   /api/v1/portfolios/{id}/holdings
PATCH  /api/v1/portfolios/{id}/holdings/{holding_id}
DELETE /api/v1/portfolios/{id}/holdings/{holding_id}
GET    /api/v1/portfolios/{id}/holdings
POST   /api/v1/portfolios/{id}/transactions
GET    /api/v1/portfolios/{id}/transactions
```

`HoldingRead` optionally includes `latest_recommendation:
RecommendationRef | None`, populated by
`app/clients/ai_client.py::get_latest_recommendation`.

# 8. Watchlists (core-service)

```text
POST   /api/v1/watchlists
POST   /api/v1/watchlists/{id}/items
DELETE /api/v1/watchlists/{id}/items/{ticker}
GET    /api/v1/watchlists
```

# 9. Research (core-service)

```text
POST   /api/v1/research/notes
GET    /api/v1/research/notes/{id}
PATCH  /api/v1/research/notes/{id}
DELETE /api/v1/research/notes/{id}
GET    /api/v1/research/notes             (filters, sort, pagination — W7)
POST   /api/v1/research/notes/{id}/comments
POST   /api/v1/research/documents          (multipart upload — W6)
GET    /api/v1/research/documents/{id}
DELETE /api/v1/research/documents/{id}
GET    /api/v1/research/documents          (paginated)
```

Uploading a document publishes `document.uploaded`
(`shared.events_registry.EventName`) — ai-service reacts
asynchronously; `DocumentRead.status` transitions from `uploaded` to
`ready`/`failed` as `document.status_changed` events arrive back.

# 10. Notifications (core-service)

```text
GET    /api/v1/notifications              (paginated)
PATCH  /api/v1/notifications/{id}/read
```

# 11. Public API — Module W4 (core-service)

Every route requires `X-API-Key`; rate-limited to 60 req/min per key by
default (`Settings.PUBLIC_API_RATE_LIMIT_PER_MINUTE`,
`core-service/app/middleware.py`).

```text
GET    /api/v1/public/portfolios/{id}
GET    /api/v1/public/portfolios/{id}/holdings          (paginated)
POST   /api/v1/public/watchlists/{id}/items
PUT    /api/v1/public/portfolios/{id}/holdings/{holding_id}
DELETE /api/v1/public/watchlists/{id}/items/{ticker}
```

Exceeding the limit:

```json
{ "error": { "code": "rate_limit_exceeded", "message": "Too many requests" } }
```
with HTTP 429. API keys are issued via
`POST /api/v1/auth/me/api-keys` (auth-service issues the key hash; the
key record itself lives in `core.api_keys` — see `ERD.md v4.0 PART 2`
for why).

# 12. WebSocket (core-service)

```text
WS /ws/{channel}        e.g. /ws/analysis:<uuid>, /ws/portfolio:<uuid>,
                              /ws/document:<uuid>
```

Every message on the socket has this shape
(`shared.schemas.websocket.WebSocketEnvelope`):

```json
{
  "event": "analysis.completed",
  "payload": { "...": "event-specific fields" },
  "emitted_at": "2026-07-04T10:00:00Z"
}
```

Example — a recommendation becoming ready (originally published by
ai-service over Redis, relayed here by core-service):

```json
{
  "event": "recommendation.ready",
  "payload": {
    "id": "uuid", "analysis_id": "uuid", "ticker": "TSLA",
    "action": "buy", "confidence": 0.78
  },
  "emitted_at": "2026-07-04T10:00:42Z"
}
```

# 13. Analyses (ai-service)

```text
POST   /api/v1/analyses                   {"ticker": "TSLA", "portfolio_id": null}
GET    /api/v1/analyses/{id}              (status + steps so far)
GET    /api/v1/analyses/{id}/recommendation
GET    /api/v1/analyses                   (paginated, mine)
```

Starting an analysis returns immediately with `status: pending`; the
live workflow timeline is delivered over WebSocket
(`/ws/analysis:<id>`), not by polling.

# 14. RAG / Document Q&A (ai-service)

```text
POST   /api/v1/analyses/{id}/ask          {"question": "..."}
```

Retrieves relevant chunks from Qdrant (payload only: `document_id`,
`chunk_id`), fetches the actual text from core-service's
`core.document_chunks`-equivalent store (or ai-service's own
`ai.document_chunks` cache of the same, per `ERD.md v4.0 PART 3` — see
that document for exactly which service holds chunk text vs.
metadata), and returns a cited answer.

# 15. Debate / Reflection (ai-service)

```text
GET    /api/v1/analyses/{id}/debate
GET    /api/v1/analyses/{id}/reflection
```

# 16. Decision Tracking & Backtesting (ai-service)

```text
POST   /api/v1/analyses/{id}/decisions    {"action_taken": "followed"}
GET    /api/v1/decisions/{id}/outcome
POST   /api/v1/backtests                  {"strategy_name": "...", "benchmark_symbol": "SPY", ...}
GET    /api/v1/backtests/{id}
GET    /api/v1/backtests/{id}/metrics
```

# 17. Market Data (ai-service)

```text
GET    /api/v1/market/instruments/{ticker}
GET    /api/v1/market/instruments/{ticker}/prices?interval=1d
```

# 18. Analytics (ai-service)

```text
GET    /api/v1/analytics/portfolio/{id}
GET    /api/v1/analytics/recommendations
GET    /api/v1/analytics/agents
GET    /api/v1/analytics/export?format=csv
```

# 19. Internal (all 3 services — not gateway-proxied)

```text
GET    /internal/users/{id}                        (auth-service)
GET    /internal/organizations/{id}                (auth-service)
GET    /internal/analyses/{id}                      (ai-service)
GET    /internal/recommendations/latest?ticker=...  (ai-service)
```

Called only by another service's `app/clients/*.py` — see
`COMMON_LIBRARY_README.md §4` for the request/cache/error-handling
pattern every one of these calls follows on the caller's side.

# 20. Health & Metrics (all 3 services)

```text
GET /health          (each service; also aggregated at the gateway as
                       /health/auth, /health/core, /health/ai)
GET /metrics          (Prometheus scrape target, each service)
```

---

# END OF API_SPEC.md
# Version 4.0
