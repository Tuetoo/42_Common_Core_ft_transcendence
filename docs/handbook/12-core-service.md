# 12. core-service

> core-service holds the user's own data and the "live" side of the app:
> portfolios with their transaction ledger, holdings, cash and daily value
> snapshots; watchlists; research notes, comments and uploaded research
> documents; notifications; developer API keys and the public API; and the
> WebSocket server (live notifications, presence, analysis and document
> status, and the Yjs collaborative editor). It also hosts the endpoints
> the scheduler calls at market open and market close, and it records when
> each scheduled job last succeeded. It owns the PostgreSQL schema `core`
> and the object-storage bucket `research-documents`.

---

## 1. The big picture

```
                     gateway (https://localhost:8443)
   /api/v1/portfolios /watchlists /research /notifications /api-keys
   /api/v1/public (X-API-Key) /api/v1/moderation       /ws/... (WebSocket)
                                   |
                                   v
 +-----------------------------------------------------------------------+
 | core-service (FastAPI, uvicorn, port 8000)                            |
 |                                                                       |
 |  apikeys.py          X-API-Key check + rate limit for /api/v1/public  |
 |  domains/portfolio   portfolios, ledger (ledger.py), holdings, cash,  |
 |                      snapshots, watchlists, market-open/close jobs    |
 |  domains/research    notes, comments, documents, moderation           |
 |  domains/notification  notification rows + delivery                   |
 |  apikeys.py + api/public.py   public API and /api/v1/api-keys         |
 |  api/websocket.py + websocket_manager.py   JSON WebSocket channels    |
 |  api/websocket.py + yjs_relay.py           collaborative notes        |
 |  api/internal.py     /internal/* (scheduler, ai-service, system docs) |
 |  event_listener.py   events from auth and ai -> notifications, pushes |
 |  job_health.py       last-success gauges of the scheduled jobs        |
 |  clients/            ai_client, auth_client                           |
 +-----------------------------------------------------------------------+
   |            |                 |                     |            |
   v            v                 v                     v            v
 Postgres     Redis            object storage        auth-service  ai-service
 schema core  db0 cache        (SeaweedFS, S3 API,   (/internal)   (/internal:
              db1 rate limits   bucket                             prices, fx,
              db2 pub/sub,      research-documents)                analyses,
                 claims                                            recommendations,
              db3 streams                                          sentiment)
```

Who calls core-service:

- **Users** through the gateway with a JWT access token (chapter 11).
- **Developers' programs** through `/api/v1/public/*` with an `X-API-Key`.
- **The scheduler container** (cron, New York time) through
  `/internal/scheduler/*`.
- **ai-service** through `/internal/portfolios/*` (holdings, cash,
  snapshots, cash flows, executing a tier as a transaction) and
  `/internal/documents*` (system documents).

---

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| **Ledger** | The list of all transactions of a portfolio (buy, sell, deposit, withdrawal, dividend, stock split, adjustment). | It is the source of truth. Holdings and cash are *derived* from it. |
| **Replay** | Computing positions by applying the ledger rows one by one in date order. | `ledger.replay_positions` is used by the live write path, the snapshot recompute script and the checks. |
| **Holding** | Shares of one ticker in one portfolio, with an average cost per share. | Stored in `core.holdings` as a *cache* of the replay. |
| **Average cost** | Total money paid for the shares held divided by the number of shares (weighted average). A sell does not change it. | Used for valuation fallbacks and returns. |
| **Cash balance** | Sum of the cash effect of every ledger row. Not stored; always computed. | No leverage: it may never go below zero. |
| **NAV snapshot** | "Net asset value": the portfolio's total value (holdings at the official close plus cash) on one day. | One row per portfolio per trading day in `core.portfolio_snapshots`, written at 17:30 New York. |
| **Idempotency key** | A unique string sent with a request so that a retry of the same request is applied only once. | ai-service sends `tier:<tier id>` when it executes a tier. |
| **Row lock (`SELECT ... FOR UPDATE`)** | A database lock on one row for the rest of the transaction; other writers wait. | Serializes concurrent writes to one portfolio. |
| **Savepoint (`begin_nested`)** | A sub-transaction that can be rolled back without losing the outer transaction. | Used to recover from unique-key races without losing locks. |
| **Object storage / S3** | A key-value store for files ("objects") in "buckets", spoken to over the S3 HTTP API. | Research document files live in bucket `research-documents` (SeaweedFS in Docker; chapter 22). |
| **Durable event (Redis Stream)** | An event appended to a Redis Stream and read by a *consumer group*; unacknowledged entries are delivered again. At-least-once delivery. | Used where a lost event is a real bug (document uploaded/deleted, portfolio deleted, document status). |
| **Pub/sub event** | Redis `PUBLISH`; only currently connected subscribers get it. At-most-once. | Used for notifications and live pushes. |
| **WebSocket channel** | A name like `user:<uuid>` that a socket subscribes to; pushes are sent to all sockets of a channel. | Live notifications, presence, analysis and document progress. |
| **Prometheus gauge** | A metric that holds one current number. | Job last-success times and the skipped-portfolio count. |
| **API key** | A long random secret given to a program instead of a user token. | Public API, `X-API-Key` header. |

---

## 3. How it works in AlphaMind

### 3.1 Start-up and structure

`services/core-service/main.py`, `lifespan`:

1. `configure_logging(settings)`.
2. `ensure_schema(engine)` (`database.py`): Alembic upgrade of schema
   `core` (same mechanism as auth-service, chapter 11 section 3.20).
3. `ensure_bucket(settings, settings.S3_BUCKET_DOCUMENTS)`: creates the
   documents bucket if missing.
4. `load_job_health()`: re-exports stored job success times and the
   last market-close skip count, and gives every scheduled job that has
   never succeeded a start time, so the stale-job alerts can fire for it
   (3.12).
5. Starts six long-running background tasks, each with a name:
   - `build_subscriber().run()`: Redis pub/sub listener (3.15);
   - `build_durable_subscriber().run()`: Redis Streams listener (3.15);
   - `yjs_relay.run_cross_replica_listener()` and
     `yjs_relay.run_active_room_heartbeat()` (Yjs, chapter 25);
   - `websocket_manager.run_cross_replica_listener()`: the `ws:fanout`
     backplane (3.10);
   - `outbox.run_outbox_dispatcher()`: publishes durable events left in
     `core.event_outbox` (3.15).

   Each of these loops is meant to run until shutdown. They are started
   with `shared.events.start_background_task(coro, name)`, whose
   done-callback logs `background_task_crashed` (task name and traceback)
   or `background_task_ended` (the loop returned) at error level when one
   of them ends on its own, so a loop that died is visible in the logs
   instead of disappearing silently. A cancellation (shutdown) is not
   logged. The three pub/sub listeners share one reconnect loop,
   `shared.events.run_pubsub_listener` (backoff 1, 2, 5, 10, 30 s;
   chapter 24 §3.2).

On shutdown the tasks are cancelled and awaited. Then
`relay.persist_all_dirty()` saves every Yjs room with unsaved edits (log
`yjs_rooms_saved_on_shutdown`; chapter 25), and only after that the Yjs
validator worker, the database engine, Redis, HTTP and S3 clients are
closed.

The app: `docs_url="/api/docs"`, `openapi_url="/api/openapi-core.json"`;
`register_middleware(app)` (the public-API middleware),
`RequestIdMiddleware` (`shared/logging.py`: a request id on every log
line, in the error body, in every `{"data": ..., "request_id"}` success
body and in the `X-Request-ID` header), CORS, shared
error handlers, Prometheus `/metrics`, routers. The public-API middleware
is registered **before** CORS on purpose: Starlette wraps later
middleware around earlier ones, so CORS is the outer layer. It answers
browser preflights itself and also adds CORS headers to the API-key
middleware's own 401 and 429 answers.

Error bodies follow the shared contract ([chapter 01](01-architecture.md),
section 3.6b): a refused read answers 404 `{"error": {"code":
"not_found", ...}}`, an owner-only write by a reader answers 403
`permission_denied`, token and missing-permission errors keep FastAPI's
`{"detail": ...}` shape. The public-API middleware uses the envelope with
its own codes (3.14).

Routers:

| Router | Mounted at |
|---|---|
| `api_router` (built in `main.py`): public API, api-keys, portfolios, watchlists, research, notifications | `/api/v1` |
| `domains/research/api.py` (`moderation_router`) | `/api/v1/moderation` |
| `api/internal.py` | `/internal` |
| `api/websocket.py` (`router`) | `/ws/{channel}` |
| `api/websocket.py` (`yjs_router`) | `/ws/yjs/{note_id}` |

The two WebSocket routers are mounted directly on the app, not inside
`api_router`: the gateway proxies `/ws` to the unversioned path, so a
WebSocket route under `/api/v1` would 404 on every connection. The
internal routers are hidden from the OpenAPI document
(`include_in_schema=False`).

`GET /health` checks `database`, `redis` and `storage` (200 or 503).

As in every service, routes use `Depends(get_db, scope="function")`: the
commit happens before the response, and `BackgroundTasks` run after it.
Notifications and events are always scheduled as background tasks, so
they never describe a change that was rolled back.

### 3.2 Portfolios (`domains/portfolio/`)

Table `core.portfolios`: `id`, `owner_id` (a plain UUID, not a foreign
key: users live in auth-service's schema), `organization_id` (nullable),
`name`, `currency` (`USD`, `EUR` or `GBP`, the `shared/enums.py`
`Currency` enum; anything else is 422), `visibility` (default
`private`), `auto_analyze_at_open` (default false; opt-in because it
costs LLM calls per portfolio per trading day), timestamps. Child tables
inside `core` (holdings, transactions, snapshots) have real foreign keys
to it.

**Visibility** (`PortfolioVisibility = Literal["private", "organization"]`
in `schema.py`; anything else is 422):

| Visibility | Who can read | Who can write |
|---|---|---|
| `private` | the owner only | the owner |
| `organization` (and `organization_id` set) | the owner and every member of that organization (any member role, viewers included) | the owner |

The read rule is `PortfolioService._require_readable`: owner -> allowed;
else, if the portfolio has an organization and is not private, and the
requester is a member (`auth_client.list_user_organization_ids`, cached
60 s) -> allowed; else **404** (never 403, so a stranger cannot even learn
that the id exists). Write operations use `_require_owner`: 404 when
missing or when the requester cannot read it (same check as
`_require_readable`), and 403 "Only the portfolio owner can perform this
action." only for an organization member who can read but not change
it. The list `GET /api/v1/portfolios` applies the same rule in
SQL (`PortfolioRepository.list_for_user`), ordered by `created_at` then
`id` so pages never repeat or skip.

Creating a portfolio *inside* an organization requires the creator to be a
MEMBER, ADMIN or OWNER of it (`auth_client.ensure_organization_writer`;
a non-member or a VIEWER gets 403). `visibility` `organization` without an
`organization_id` is 422 (`PortfolioCreate`). A `PATCH` that changes the
visibility to `organization` runs the same writer check against the
portfolio's organization (403), and is 422 for a portfolio with no
organization. The organization itself never changes after creation.

**Sharing ends when the owner leaves.** On `organization.member.left` and
`organization.member.removed`, `_unshare_member_portfolios` sets that
member's `organization` portfolios of that organization to `private` (the
`organization_id` stays, so the owner keeps the portfolio and can share
it again after rejoining). Log `member_portfolios_unshared` (count), or
`member_portfolios_unshare_failed` when the update fails (then the
portfolio stays shared until the owner changes it). If the event is lost
(Redis down when auth publishes), the portfolio also stays shared until
the owner changes it; no sweep repairs it. A role change to
VIEWER does not unshare: the writer check applies only when the
visibility changes.

Routes (`domains/portfolio/api.py`, prefix `/api/v1/portfolios`):

| Route | Permission | Notes |
|---|---|---|
| `POST ""` | `portfolio:write` | 201, notification "Portfolio created" |
| `GET ""` | logged in | paginated (`page` up to `MAX_PAGE`, `page_size` 1..100) |
| `GET /{id}` | readable | |
| `PATCH /{id}` | `portfolio:write` + owner | name, visibility, `auto_analyze_at_open` |
| `DELETE /{id}` | `portfolio:write` + owner | cascade delete + durable `portfolio.deleted` (3.15) |
| `GET /{id}/snapshots` | readable | `start_date`, `end_date`; display conversion for EUR/GBP (3.5) |
| `POST /{id}/holdings`, `PATCH /{id}/holdings/{hid}`, `DELETE /{id}/holdings/{hid}` | `portfolio:write` + owner | record ADJUSTMENT rows (3.4) |
| `GET /{id}/holdings` | readable | the holdings; `latest_recommendation` is null here (no ai-service call); the public API's list fills it (3.14) |
| `POST /{id}/transactions` | `portfolio:write` + owner | the ledger write path (3.3) |
| `GET /{id}/transactions` | readable | paginated |
| `GET /{id}/history.csv` | readable | daily snapshots with a flow-adjusted daily return |
| `GET /{id}/transactions.csv` | readable | ledger rows, optional date range and type |

Deleting a portfolio deletes its holdings, transactions and snapshots
through `ON DELETE CASCADE` foreign keys (`model.py`).

### 3.3 Transactions and the ledger (`ledger.py`, `service.py`)

Table `core.transactions`: `portfolio_id`, `ticker`, `transaction_type`,
`quantity`, `price`, `dividend_per_share`, `split_ratio`,
`transaction_date`, `idempotency_key` (unique, nullable, max 64 chars).

What each type needs (`TransactionCreate` in `schema.py` plus
`PortfolioService._validate_required_fields`) and what it does:

| Type | Required | Cash effect (`ledger.signed_cash_delta`) | Shares effect |
|---|---|---|---|
| `deposit` | `quantity` = amount | `+quantity` | none (ticker stored as `CASH`) |
| `withdrawal` | `quantity` = amount | `-quantity` | none (ticker `CASH`) |
| `buy` | `quantity`, `price` | `-(quantity * price)` | shares in; average cost moves |
| `sell` | `quantity`, `price` | `+(quantity * price)` | shares out; average cost unchanged; never more than held |
| `dividend` | `quantity`, `dividend_per_share`; ticker must be held | `+(quantity * dividend_per_share)` | none |
| `stock_split` | `split_ratio` > 0; ticker must be held | none | quantity x ratio, average cost / ratio (total cost unchanged). 2-for-1 is 2.0, 1-for-5 is 0.2, a 5% stock dividend is 1.05 |
| `adjustment` | non-zero signed `quantity`; `price` >= 0 for shares in | none | shares in (positive) at `price` as cost basis, or out (negative) |

Other validation: `CASH` is reserved for deposits and withdrawals; tickers
are normalized (`shared/schemas.py`); decimals are bounded to at most 30
digits, 18 of them after the decimal point (so an absurd value is a 422,
not a database overflow);
`transaction_date` may be at most one day in the future; there are no fee
columns, so a trade's cash effect is exactly `quantity * price`.

**The write path** (`PortfolioService.record_transaction`):

```
 POST /api/v1/portfolios/{id}/transactions
   |
   | _require_owner (404 missing or not readable / 403 reader, not owner)
   | _validate_required_fields (422)
   | [internal route only] idempotency_key given?
   |     lock portfolio row; key already used?
   |       same portfolio -> return the ORIGINAL transaction (no new row)
   |       other portfolio -> 409
   | share-moving type (buy, sell, split, adjustment): lock portfolio row
   | _check_current_holding (422; holding row locked after the portfolio):
   |     dividend / split: ticker held?  sell / adjustment out: enough held?
   | buy / withdrawal: _ensure_cash_stays_non_negative (422, portfolio locked)
   | INSERT transaction (savepoint when an idempotency key is set)
   | share-moving type: _sync_holding_with_ledger (the only writer of
   |     the cached holding on this path)
   |     replay this ticker's ledger WITH and WITHOUT the new row;
   |     more shares taken out than held at some date? -> 422 (rolls back)
   |     else set the holding to the replayed quantity and average cost
   v
 201 TransactionRead, then (background) notification "Transaction recorded"
```

**Lock order.** Every writer locks the portfolio row first and the
holding row second. With one fixed order, two requests can never wait for
each other in a circle (no deadlock).

**No leverage** (`_ensure_cash_stays_non_negative`, `lowest_cash_from`).
For a buy or withdrawal the service:

1. locks the portfolio row (two outflows that each fit alone but not
   together would otherwise both pass);
2. groups all existing cash effects by **snapshot day**
   (`ledger.snapshot_day`, the 17:30 New York cutoff that the daily
   snapshots use), adds the new outflow on its own snapshot day;
3. computes the running balance at the end of each day from the new day
   onwards and takes the lowest;
4. refuses with 422 if that lowest balance is below zero ("Insufficient
   cash to buy ... Leverage is not allowed -- record a deposit first.").

Checking every later day, not just today, matters for back-dated
outflows: a withdrawal dated last week could make last Tuesday negative
even if today's balance is fine. Days *before* the new row are not
examined, so old messy history cannot block an unrelated new trade. Day
granularity (not exact time) is deliberate: a date-only entry is stored
as midnight UTC (the evening before in New York, so it belongs to its own
calendar date's snapshot), which would otherwise sort before a deposit
recorded later the same day. Using the
snapshot day, not the UTC day, keeps this check and the snapshots in
step: a deposit made after 17:30 New York counts for the next day, so a
buy earlier that day cannot rely on it and the day's snapshot never
shows negative cash.

**Replay** (`ledger.replay_positions`) and **same-timestamp ordering**
(`ledger.ledger_order`, `_phase`). Rows are sorted by
`(transaction_date, phase)` with a stable sort:

| Phase | Rows |
|---|---|
| 0 | shares in: buy, positive adjustment (and every row without a share effect: deposit, withdrawal, dividend) |
| 1 | stock split |
| 2 | shares out: sell, negative adjustment |

So when rows share one timestamp, shares come in first, then splits apply,
then shares go out. A sell can never fail just because a buy with the same
timestamp happened to be stored after it. The replay is *lenient*: a row
that takes out more than is held empties the position and is reported as
a `Shortfall` instead of raising; a split of a ticker not held is skipped.
The live write path uses this to refuse only a row that *increases* the
shortfall (old shortfalls already in the history do not block new rows).

Why the holding is rebuilt from the replay after every share-moving row:
updating the holding *on top of its current state* is only right for a
row dated after every other row of that ticker.
Example: buy 10 at
100 in January, 2-for-1 split in June, then a forgotten buy of 5 at 120
recorded later but dated March. Applied on top of the current state this
gives 25 shares at 64.00. Replayed in date order it gives the correct 30
shares at 53.33 (10 at 100, plus 5 at 120 = 15 at 106.67, split 2:1 = 30
at 53.33).

Two columns exist only for one type each, so no field carries two
meanings: `dividend_per_share` (a dividend's cash is `quantity *
dividend_per_share`; reusing the stock's trade `price` there would
overstate cash by orders of magnitude) and `split_ratio` (a multiplier,
unlike `quantity`). The required fields of BUY/SELL, DEPOSIT/WITHDRAWAL,
DIVIDEND and STOCK_SPLIT are checked in the service
(`_validate_required_fields`; a sell without a price would otherwise
remove shares with no proceeds); signs, the CASH ticker rule and the
ADJUSTMENT rules are checked in the schema (`TransactionCreate`). Quantities and costs
that differ by less than a tiny relative tolerance count as equal in the
replay, because Decimal division in the weighted average can leave a
last-digit difference.

**Cash balance** (`PortfolioService.compute_cash_balance`): the sum of
`ledger.cash_delta` (`signed_cash_delta`) over all transactions of the portfolio. It is never
stored as a running counter: simpler and always consistent, at the cost of
reading all transactions per call. There is no public route for it; it is
returned by `GET /internal/portfolios/{id}/cash-balance` (used by
ai-service to size buy recommendations) and stored in each snapshot.

**Idempotency keys.** Only `POST /internal/portfolios/{id}/transactions`
accepts `idempotency_key` (`InternalTransactionCreate`). ai-service sends
`tier:<tier id>` when it executes a triggered recommendation tier. If the
HTTP answer is lost and ai-service retries, the second call returns the
original transaction (same body, nothing applied twice). The same key on
another portfolio is a 409. The portfolio row is locked before the key is
looked up, so two concurrent retries on one portfolio are serialized; a
race across portfolios is caught by the unique index inside a savepoint.
The internal route also refuses (403) to trade for an owner who is now
deactivated or read-only (a guest), except to return an already-recorded
key (`_ensure_owner_may_trade`).

### 3.4 Holdings are written only through the ledger

`core.holdings` (`portfolio_id`, `ticker`, `quantity`, `average_cost`,
unique per portfolio and ticker) is a cache. The three holding endpoints
do not write it directly; they record ADJUSTMENT transactions
(`_adjust_holding_to`, `ledger.plan_adjustments`):

| Request | ADJUSTMENT rows written |
|---|---|
| add a holding (ticker not held) | one row: `+quantity` at `average_cost` (a ticker already held is 422) |
| change only the quantity (same cost) | one row for the difference |
| change the average cost | two rows: all shares out, then the target quantity in at the target cost (shares out never change the average, so this is the only way to land exactly on the target) |
| delete a holding | one row: all shares out |

The rows are dated one microsecond apart, so their replay order is
fixed. The first row is dated now, or one microsecond after the ticker's
latest ledger row when that row is dated later than now
(`TransactionRepository.latest_date_for_ticker`). The cached holding is
the replay of *all* rows, including rows dated up to one day ahead (a
date-only entry from a user east of UTC is midnight UTC of a day that has
not started yet in UTC). Rows dated before such a future row would be
replayed before it, and the replay would end at a different quantity
than the target. Example: 10 shares bought yesterday and 5 dated
tomorrow give a holding of 15; a cost change dated now would replay as
10 - 15 (shortfall) + 15 + 5 = 20, while the holding says 15. Dated after
the future row, the replay ends exactly at the target, so the holding and
the replay agree. The answer (`HoldingAdjustmentRead`) is the resulting holding plus
`transaction_id` and `transaction_ids`. The public API's
`PUT /api/v1/public/portfolios/{id}/holdings/{hid}` uses the same path.
ADJUSTMENT has no cash effect: it means "shares moved in from or out to
outside" (a transfer, an opening position, a correction).

### 3.5 NAV snapshots and the market-close job

Table `core.portfolio_snapshots`: `portfolio_id`, `snapshot_date`,
`total_value`, `cash_balance` (unique per portfolio and date; written with
an atomic upsert, `PortfolioSnapshotRepository.upsert`).

The scheduler calls `POST /internal/scheduler/market-close` at **17:30
New York** Monday to Friday (about 22:30 London), from
`infra/scheduler/crontab` via `call-job.sh market-close`
(`curl --max-time 600`).
It runs after tier monitoring (17:00 New York), because tier monitoring
can add trades to the ledger that must be in the day's snapshot.

```
 market_close (api/internal.py)
   today = market_today()  (New York date)
   not a NYSE trading day? -> 200 {"skipped": true, "reason": "weekend" |
                                   "market holiday", "date",
                                   "portfolios_processed": 0}
   run_market_close_job(today):
     for every portfolio: read its whole ledger (transactions) and
        positions_at_cutoff(txns, today): shares and cash from the rows
        dated at or before today's 17:30 New York cutoff
     every distinct ticker: ai_client.get_daily_closes(ticker)   (concurrently,
        at most 20 at once; 15 s timeout each; closes keyed by NYSE session date)
        (the ledger reads and the upserts below are sequential: they share
         one AsyncSession, which is not safe for concurrent use; only the
         network-bound price fetch is parallel)
     for every portfolio:
        any held ticker WITHOUT a close dated today?
           -> skip it, log market_close_snapshot_skipped (tickers listed)
        else total_value = sum(quantity * today's close) + cash -> upsert
   record_market_close_skipped(n): gauge + Redis core:market_close_skipped
   -> {"skipped": false, "date", "portfolios_processed", "portfolios_skipped"}
 call-job.sh then calls /scripts/heartbeat.sh market-close (3.12)
```

Why a portfolio is skipped instead of valued anyway: valuing it with
whatever price is at hand stores wrong values silently. When the newest
price bar is a day old, the snapshot repeats the previous day's value;
when no price comes back, every holding is valued at cost. Neither looks
wrong in the chart. So a holding without a close *for that session*
means no snapshot that day, a warning log, and a non-zero
`alphamind_market_close_portfolios_skipped` gauge, which fires the alert
`MarketCloseSkippedPortfolios` (`infra/prometheus/alert_rules.yml`). The
missing day is filled later with the recompute script. (The private
`_compute_snapshot` still falls back to the average cost when a price is
missing; only the end-to-end script uses it directly to seed test
snapshots.)

Shares and cash come from one read of the ledger, *before* the price
fetch (which can take up to 15 s), so they always match each other
(`positions_at_cutoff` in `domains/portfolio/service.py`, which calls
`ledger.rows_through` and `ledger.replay_positions`). The job does not
read the `holdings` table. A ledger row dated after the cutoff (a
transaction may be dated up to one day ahead) is left out and counts in
the next day's snapshot.

**Which ledger rows belong to day D's snapshot** (`ledger.snapshot_day`,
`rows_through`, `SNAPSHOT_TIME_ET = 17:30`): rows dated at or before 17:30
New York on D. A date-only entry is stored as midnight UTC (20:00 New York
the evening before), so it counts on its own calendar date; a deposit at
19:00 New York counts toward the *next* day. The CSV history (its
flow-adjusted daily return attributes a flow to the snapshot it is in),
the `/internal/.../cash-flows` route (field `snapshot_date` and its
`start_date`/`end_date` window), the recompute script (through
`service.positions_at_cutoff`) and the market-close job all use this
rule, so they agree.

**History export daily return** (`export_history`, `external_flow`):
`(value_t - flows) / value_{t-1} - 1`, where the flows are the deposits,
withdrawals and ADJUSTMENT rows whose snapshot day falls after the
previous snapshot and at or before this one. An ADJUSTMENT is valued at
the ticker's session close on or before its snapshot day (signed quantity
times close, from `ai_client.get_daily_closes`), so deleting a holding or
changing its average cost is not shown as a loss or gain. Only without a
close is it valued at its stated `price` (shares out written by the
holding routes have no price and then count as 0), with
the warning `history_adjustment_valued_at_cost_basis`. This is the same
rule as the portfolio backtest's time-weighted return (chapter 19), so the
two returns agree. The export calls ai-service (and through it possibly
the market provider) once per adjusted ticker; each call looks back only
to that ticker's earliest ADJUSTMENT snapshot day in the export (plus a
10-day pad, `_CLOSE_LOOKBACK_PAD_DAYS`), so a ticker listed later than
the first snapshot does not fetch years of bars it does not need.

**Recompute script** (`scripts/recompute_snapshots.py`, wrapper
`scripts/snapshots.sh`, `make snapshots`):

- For each stored snapshot day, and each trading day without one between
  the first snapshot and `--to` (never before the portfolio's first
  ledger row): replay the ledger rows up to that day's 17:30 cutoff, value
  the shares at that session's official close (on a non-trading day, the
  latest close at most 6 days earlier), add the replayed cash.
- Flags a day when the total differs by more than `--threshold` (default
  1.00) or the cash by more than 0.01. Actions: `ok`, `fix`, `add`, or
  `no close` (a close is missing; never written).
- **Preview by default.** `--confirm` writes the `fix` and `add` rows,
  each portfolio committed on its own. Options: `--portfolio <uuid>`
  (repeatable) or `--all`, `--from`, `--to`, `--threshold`.
- Do not run it around 17:30 New York; `scripts/snapshots.sh` refuses
  inside the scheduler's busy windows unless `FORCE=1`. Without
  `--portfolio`/`--all` the wrapper uses the test portfolio
  `PORTFOLIO_ID` from `scripts/lib.sh`.

**Reading snapshots.** `GET /api/v1/portfolios/{id}/snapshots`
(readable portfolios) returns the stored USD values. For a EUR or GBP
portfolio it adds `total_value_converted`, `cash_balance_converted`,
`fx_rate` and `display_currency`, using one *current* rate from ai-service
(`ai_client.get_fx_rate`) for every row. This is a display convenience,
not multi-currency accounting; on an FX failure the USD data is still
returned. `GET /internal/portfolios/{id}/snapshots` (no ownership check)
feeds ai-service's portfolio backtest.

### 3.6 The market-open job

`POST /internal/scheduler/market-open` at **09:30 New York** Monday to
Friday (about 14:30 London). Skipped on weekends and NYSE holidays (200
`{"skipped": true, "reason", "date", "analyses_requested": 0}`). Otherwise
`run_market_open_job`: for every portfolio with `auto_analyze_at_open =
true` whose owner is active, for each holding, `ai_client.request_analysis(ticker,
portfolio_id, user_id=owner_id)` (`POST` ai-service
`/internal/analyses`, 15 s timeout, at most 10 at once). The analysis
belongs to the portfolio owner, so they get its notification and see it
in their analyses list. A failed request is logged
(`market_open_analysis_request_failed`) and does not stop the rest. The
requests touch no database state, so they run concurrently, bounded to
10 so a portfolio-heavy day does not fire hundreds of LLM-backed
requests at once. Before that, the owner's live state is read once per
owner (`shared/user_state.get_user_state`); a portfolio whose owner is
deactivated or deleted is skipped and logged
(`market_open_owner_inactive_skipped`), because ai-service would refuse
each request with 403. An unknown state (Redis and auth-service both
unreachable, `None`) does not skip. The answer contains
`analyses_requested` and `analyses_failed` (`MarketOpenResult`). When at
least one request was made and every one failed, the route answers
**500** with the same body (`shared/scheduled_jobs.py::job_response`), so
the job script sends no heartbeat and the stale-job alert can fire. The
analyses then wait in ai-service's serial queue (usually 1.5 to 4
minutes each, up to 10 minutes more for a ticker with a new SEC filing
to summarise).

### 3.7 Market calendar

`shared/market_calendar.py`: `MARKET_TZ = America/New_York`,
`market_today()` (the New York date), `is_trading_day(day)` (not a weekend
and not an NYSE holiday computed from fixed rules: New Year, MLK Day,
Washington's Birthday, Good Friday, Memorial Day, Juneteenth, Independence
Day, Labor Day, Thanksgiving, Christmas, with the weekend "observed"
rules, plus `EXTRA_CLOSED_DATES`). Early-close days count as normal
trading days. core-service uses it in the two scheduler endpoints, in the
snapshot-day rule of the ledger, in the recompute script, and to key
daily closes by session date (`ai_client.bar_session_date`).

### 3.8 Watchlists

`core.watchlists` (`owner_id`, `name`) and `core.watchlist_items`
(`watchlist_id`, `ticker`, unique per watchlist). Owner-only, and never
shared: `_require_watchlist_owner` answers 404 "Watchlist not found." to
anyone but the owner (no 403, so a stranger cannot learn the id exists).
Routes under
`/api/v1/watchlists`: `POST ""`, `PATCH /{id}` (rename), `DELETE /{id}`,
`GET ""`, `POST /{id}/items` (`{"ticker": ...}`; duplicate is 422/409),
`DELETE /{id}/items/{ticker}`. The union of all holdings and watchlist
tickers is what ai-service's nightly ingestion keeps fresh
(`GET /internal/tracked-tickers`).

### 3.9 Research: notes, comments, moderation (summary)

`domains/research/` (prefix `/api/v1/research`):

- **Notes** (`core.research_notes`): `author_id`, `organization_id`,
  `title`, `content`, a generated full-text `search_vector`
  (`to_tsvector('english', title || ' ' || content)` with a GIN index),
  `yjs_state` (binary CRDT state), moderation and sentiment columns.
  Routes: `POST /notes`, `GET /notes` (search: `q`, `tag`, `author_id`,
  `organization_id`, `sort` `created_at`/`title` with optional `-`),
  `GET/PATCH/DELETE /notes/{id}`. Tags via `research_tags` and
  `note_tags`. A `PATCH` that changes the body updates `content` and
  applies the same change to the stored `yjs_state` as one Yjs edit
  (`yjs_relay.replace_text`: the common start and end of the old and new
  text are kept, only the part in between is deleted and inserted). The
  CRDT history stays, so an editor that still holds the old Yjs document
  merges the server state without duplicating the text (chapter 25,
  section 3.8). A note without a stored state gets a fresh one
  (`seed_state`); a stored state that cannot be decoded is replaced by a
  fresh one and `note_yjs_state_reseeded` is logged.
- **Access** (`ResearchService.note_access`, the one rule for REST and
  the Yjs socket): read = the author or any member of the note's
  organization (a hidden note only for its author and moderators); write
  = `note:write` permission and author, or for an organization note a
  MEMBER/ADMIN/OWNER (VIEWER is read-only). Only the author deletes.
- **Comments** (`core.research_comments`, cascade with the note):
  `POST/GET /notes/{id}/comments`, `PATCH/DELETE
  /notes/{id}/comments/{cid}`. Commenting needs to be the note's author
  or an organization writer. A comment's author edits it; the comment's or
  the note's author deletes it. A comment hidden by a moderator can only
  be deleted by its own author (the note author gets 404 for it). The
  note author is notified of new comments.
- **Moderation** (`moderation_router` in `domains/research/api.py`, permission `content:moderate`):
  `POST /api/v1/moderation/notes/{id}/hide|unhide`,
  `DELETE /api/v1/moderation/notes/{id}`, the same for comments. Hidden
  rows disappear for everyone except their author and moderators. Hiding
  or deleting a note (by a moderator, or the owner's own delete) also
  ends its live editing sessions after the commit:
  `YjsRelay.close_note_sessions` closes every local Yjs socket of the note
  with 4403 (edits are saved after a hide, discarded after a delete;
  chapter 25).
- **Sentiment** (`sentiment.py`): after a note or comment is created or
  its text changes (REST, or the Yjs write-back), core starts an asyncio
  task (`schedule_note_sentiment` / `schedule_comment_sentiment`) that
  calls ai-service `POST /internal/sentiment` (`clients/ai_client.py`)
  and stores the label and score, only if the text has not changed
  meanwhile, so a slow answer for an older version never overwrites the
  newer one's. The REST routes start it from a `BackgroundTasks` entry
  (`schedule_after_commit`), so it starts after the commit, but the
  response (and its latency metric) does not wait for the LLM. Refreshes
  are single-flight per note or comment: at most one call runs, and
  further saves meanwhile queue one follow-up that re-reads the latest
  text. The task opens its own session and never raises; storing a
  result does not move `updated_at` (it is not an edit). A failed call
  stores nothing. A new note or comment then stays without sentiment
  (NULL) until the next edit or `python -m scripts.backfill_sentiment
  --apply` (preview without `--apply`, `--rate` calls per minute, default
  30) fills it. After an edit, a failed refresh keeps the previous label
  until the next successful refresh; the backfill fills rows that have
  no label and also re-analyses rows whose label is older than the last
  edit. `GET /api/v1/research/sentiment/summary` aggregates it
  (counts per label, average score, a per-day series by UTC creation
  day; `ticker` selects notes tagged with it or mentioning it, and their
  comments). It counts the caller's own notes and the notes of the
  caller's organizations, with the same hidden-row rule as every other
  read: a hidden note or comment counts for its author and for
  moderators, not for anyone else.
- **Yjs collaborative editing** (summary; details in chapter 25): a note
  body can be edited live by several users over `/ws/yjs/{note_id}`
  (binary Yjs updates). `yjs_relay.py` keeps one in-memory room per note,
  rebroadcasts updates, relays them to other replicas through Redis, and
  saves `yjs_state` plus the plain text back to `content` after 20 updates
  or 30 seconds, whichever first. Updates larger than
  `YJS_MAX_UPDATE_BYTES` (1 MiB) or that fail to decode (checked in a
  separate worker process, `yjs_relay.py`) close only that socket (1008).
  While a room is open on any replica (Redis sorted set
  `yjs:active:<note_id>`), a REST edit of the body is refused with 409.
  A room whose edits are not saved yet also counts as open, even with
  nobody connected, so the 409 holds until the save succeeds. Rooms with
  unsaved edits are saved at shutdown. Open sockets follow access
  changes: when an organization membership or role, an account role, an
  account state or a connection between two users changes (events
  `organization.member.removed`, `.member.left`, `.role.changed`,
  `organization.deleted`, `user.role.changed`, `user.deactivated`,
  `user.deleted`, `friend.removed`, `connection.blocked`), every replica
  re-checks its local Yjs sockets of the affected users
  (`api/websocket.py::recheck_user_sessions`) with the same access
  rule as the connect path: a note no longer readable is closed with 4403
  (4401 for a deactivated or deleted account), a socket that may no
  longer write is closed with 4403 "write access to this note was
  revoked" (the editor shows its reload state; the reload opens it
  read-only).
  If the new rights cannot be determined (auth-service down), the socket
  keeps its rights and `yjs_session_recheck_failed` is logged. The same
  re-check also covers the user's `/ws/{channel}` sockets (section 3.16).

### 3.10 Research documents and the lifecycle with ai-service

Table `core.documents`: `owner_id`, `organization_id`, `filename`,
`document_type` (the extension: `pdf`, `txt`, `md`), `category`, `ticker`,
`storage_url` (the object key), `status` (default `uploaded`),
`uploaded_at`, `content_date`, `title`, `source_url`. Two unique indexes
(`NULLS NOT DISTINCT`, so a document without a ticker counts too): one
per (organization, ticker, category, filename) for organization
documents, one per (owner, ticker, category, filename) for personal ones.
The indexes are per tenant on purpose: one global key would let two
organizations' "AAPL 10-K.pdf" collide, and the race-recovery path would
then hand one tenant the other tenant's document. `category` drives RAG
chunking (a PDF 10-K and a PDF analyst report need different chunking),
is chosen by the uploader, and is stored as plain text (not a PostgreSQL
enum) so a new `DocumentCategory` member needs no core migration.
`ticker` feeds the ticker filter of RAG retrieval. On every upload path
it goes through `shared/schemas.normalize_ticker` (spaces around it
removed, uppercased, then 1 to 10 characters of `A-Z`, `0-9`, `.` or
`-`); anything else is a 422 `validation_error` before any byte is
stored, and an empty value means "no ticker". Without this check a value
longer than the `String(10)` column failed in PostgreSQL (500), and a
value with a trailing space was stored and never matched the RAG ticker
filter. List filters uppercase the ticker too. `content_date` is the real
date of the content (a filing's SEC filing date), unlike `uploaded_at`.

**Upload** (`POST /api/v1/research/documents`, multipart: `file`,
`category`, optional `ticker`, `organization_id`, `title`,
`content_date`; permission `document:upload`; `ResearchService.upload`):

```
 client             core-service                       object storage     Redis db3        ai-service
   | POST documents   |                                      |               (streams)          |
   |----------------->| extension in .pdf/.txt/.md (422)     |                  |               |
   |                  | size <= 20 MB (422)                  |                  |               |
   |                  | category is a DocumentCategory (422) |                  |               |
   |                  | ticker normalized, else 422          |                  |               |
   |                  | bytes match type: PDF starts "%PDF-",|                  |               |
   |                  |  text is UTF-8 without NUL (422)     |                  |               |
   |                  | organization_id? caller must be an   |                  |               |
   |                  |  organization writer (403)           |                  |               |
   |                  | same (tenant, ticker, category,      |                  |               |
   |                  |  filename) exists? -> see below      |                  |               |
   |                  | PUT documents/<owner>/<hex>.<ext> -->|                  |               |
   |                  |  (the owner folder is for people     |                  |               |
   |                  |   browsing the bucket, not access    |                  |               |
   |                  |   control: that is in PostgreSQL)    |                  |               |
   |                  | INSERT row (status uploaded);        |                  |               |
   |                  |  insert fails -> delete the object   |                  |               |
   |<-- 201 document  | COMMIT                               |                  |               |
   |                  | BackgroundTask: XADD stream:document.uploaded --------->|               |
   |                  |   {document_id, owner_id, organization_id, storage_bucket,             |
   |                  |    storage_key, document_type, category, ticker, content_date,         |
   |                  |    filename}                                             |-- consumer -->|
   |                  |                                                           |  group reads  |
   |                  |                         ai-service: parsing -> chunking -> embedding ->|
   |                  |                         ready | failed; each step publishes          |
   |                  |<--------- document.status_changed (durable stream AND pub/sub) --------|
   |                  | durable handler: UPDATE status (forward only, row locked)            |
   |                  |   on a real change to ready/failed: notify the owner                 |
   |                  |   (not for system documents)                                         |
   |                  | pub/sub handler: push to WebSocket channel document:<id>              |
```

**Statuses and forward-only transitions**
(`domains/research/repository.py`, `_DOCUMENT_STATUS_RANK`,
`is_forward_status_transition`): `uploaded` (0) -> `parsing` (1) ->
`chunking` (2) -> `embedding` (3) -> `ready` (4) or `failed` (4). A status
may only move to a higher rank, so a late or redelivered `parsing` cannot
overwrite `ready`. The one allowed backward move is `failed -> parsing`
(ai-service retries after a transient error). `ready` is final.
core-service itself only ever writes `uploaded`.

**Owner notifications.** The durable handler notifies the owner only on a
real change to `ready` ("Document ready") or `failed` ("Document
processing failed"), and never for system documents. A transient
ingestion error (for example a rate limit of the embedding provider) is
reported by ai-service as `failed` before it retries (chapter 24,
section 3.6). core cannot tell that failure from a final one, so the
owner first gets "could not be processed" and, when the retry succeeds,
"has been processed". The second notification is the one that counts;
the document list always shows the current status.

**Duplicates and retries.** A second upload with the same (tenant,
ticker, category, filename):

- status `failed` -> retried in place when the uploader owns the
  document: the object is overwritten, status reset to `uploaded`,
  `document.uploaded` published again, notification "Document
  re-uploaded" (auto-ingestion relies on this); another member of the
  organization gets 409 ("A document with this name already exists in
  the organization.");
- status not final and unchanged for too long, measured from the last
  status change (`status_changed_at`, set on create, on every applied
  `document.status_changed` and on a failed-upload retry): `uploaded` for
  more than **10 minutes** (`STUCK_DOCUMENT_AFTER`; nothing has picked the
  document up), or `parsing`, `chunking` or `embedding` for more than
  **60 minutes** (`STUCK_PROCESSING_AFTER`; a large filing can stay in
  `embedding` for many minutes under provider rate limits, so a shorter
  limit would start a second ingestion of a document that is still in
  progress). Both live in `repository.py` (`stuck_after(status)`); the
  final statuses are `FINAL_DOCUMENT_STATUSES`, `ready` and `failed` -> the
  `document.uploaded` event is published again (log
  `document_upload_republished`) and the existing document is returned
  (201);
- anything else -> nothing is uploaded or published. The public upload
  route answers 409 `conflict` ("A document with this name already
  exists. Delete it or rename the file before uploading again."). The
  internal route for system documents (`POST /internal/documents/system`)
  still returns the existing document with `duplicate: true`.

The "stuck" rule is an extra repair path. A document can stay `uploaded`
when its event was published but ai-service never processed it (for
example, the stream entry was dead-lettered), or stay at `parsing`,
`chunking` or `embedding` when ai-service stopped in the middle and its
last status event was the last thing it managed to publish. Re-uploading
the same file after that limit locks the row, checks again that it is
still stuck, resets it to `uploaded` with `status_changed_at` = now (as a
retried failed upload does) and publishes
the event again; ai-service re-indexes idempotently (it deletes the
document's old chunks first), and the forward-only rule lets the statuses
that follow move the row on to `ready` or `failed`. Because the stuck
clock restarts, the document is not republished again while that
re-ingestion runs. A retried failed upload restarts the stuck clock
too, so the stuck limit is measured from the retry. The event cannot be
lost between the commit and the publish: it is stored in the outbox
first (next paragraph). The duplicate check runs before anything is written to
object storage, so a duplicate leaves no orphan object and does not make
ai-service re-index the old id with new content.

Auto-ingestion's dedup list (`GET /internal/documents/filenames`,
`DocumentRepository.list_filenames`) covers every owner by default; with
`system_only=true` it covers only system documents (`SYSTEM_DOCUMENT_OWNER_ID`).
ai-service's nightly filing, news and macro ingestion passes
`system_only=true`, so a user's private upload with the same filename does
not stop the system copy; the seed upload script (`--as-system`) uses the
every-owner list. Both leave out
`failed` documents first uploaded within the last
`FAILED_DOCUMENT_RETRY_DAYS` (default 7) days, so a filing whose ingestion
failed (for example on a transient embedding error) is fetched and
submitted again by each nightly run for about a week, which retries it in
place. An older `failed` document counts as present and is no longer
retried; `0` turns the nightly retry off. A retry keeps the document's
first `uploaded_at`, so this age is measured from the first upload (a
re-uploaded failed document therefore keeps its first upload date in the
document list and its date sort). The list also leaves out documents
stuck in a non-final status, by the stuck limits above, so the next
ingestion run submits them again
and the duplicate path above republishes their event. A document that is
`ready`, or whose status changed within the limit for its status, counts
as present, so a document that is still being processed is never
fetched and ingested a second time.

**The outbox (`outbox.py`).** The three durable events
core-service sends (`document.uploaded`, `document.deleted`,
`portfolio.deleted`) are not published directly. The route writes the
event as a row of `core.event_outbox` in the same transaction as the
change (`outbox.enqueue`, flushed with the document or portfolio rows).
So the event exists exactly when the change exists. After the response a
background task (`outbox.publish_now`) publishes it and marks the row
published; if Redis is down then, it logs `outbox_publish_deferred` and
the row stays unpublished. A publish from a background task alone would
lose the event for good whenever Redis is down at that moment or the
process stops in between.

The dispatcher (`run_outbox_dispatcher`, a lifespan task) runs every
`OUTBOX_DISPATCH_INTERVAL_SECONDS` = 5 s. It takes rows that are still
unpublished and older than `OUTBOX_GRACE_SECONDS` = 10 s (so it does not
race the request's own `publish_now`), at most `OUTBOX_BATCH_SIZE` = 100
per pass, oldest first, locked with `SKIP LOCKED` so two replicas never
publish the same row. Each try increments `attempts`; a failure stores
`last_error` (first 500 characters), logs `outbox_publish_failed` and
stops the pass (Redis is down, the rest would fail the same way). It
logs `outbox_published_pending` with the count, sets the gauge
`alphamind_outbox_unpublished_events` every pass (alert `OutboxBacklog`
after 15 minutes above 0), and once an hour deletes rows published more
than `OUTBOX_RETENTION_DAYS` = 7 days ago (log `outbox_purged`). A failure
of the whole pass is logged as `outbox_dispatch_loop_failed` and the loop
goes on. If the "published" mark itself fails after a publish
(`outbox_mark_published_failed`), the event is published again later:
delivery is at least once, and the ai-service handlers are idempotent.
Details in chapter 24, section 3.5.

**Why durable events here.** With a plain pub/sub `PUBLISH`, a document
uploaded while ai-service's subscriber was reconnecting is never
processed, with no error anywhere; status updates have the same problem
(documents stay `uploaded` although ai-service finished them). Both
directions therefore use Redis Streams with a consumer group (`shared/events.py`, `DurableEventSubscriber`;
chapter 24). The live WebSocket push of the status stays on pub/sub,
because a missed live update only means the page refreshes later.

**Why the publish waits for the commit.** The row is only flushed during
the request; the commit runs after the handler returns. An event sent
before the commit could reach ai-service for a document that a failed
commit then never stored. `BackgroundTasks` run after the commit, and
the outbox row is part of the same commit.

**Other document routes:** `GET /documents` (filters `ticker`,
`category`, `status` (a `DocumentStatus` value, else 422),
`created_from/to`, `content_from/to` as inclusive UTC days, `sort` with a
`-` prefix for descending, ties broken by id; `include_system` adds the
shared system documents, off by default so a user's own uploads are not
buried), `GET /documents/{id}`, `GET /documents/{id}/content`
(download with `nosniff`; the `Content-Disposition` header follows RFC
6266/5987, an ASCII fallback plus `filename*`, so a non-ASCII filename
does not break the latin-1 header),
`GET /documents/{id}/preview` (inline for pdf, txt, md -- the upload types;
text as `text/plain` with `Content-Security-Policy: default-src 'none';
sandbox`; any other type answers 415 with the shared error envelope, code
`unsupported_media_type`, which only happens if
`ALLOWED_DOCUMENT_EXTENSIONS` is widened without a preview type),
`PATCH /documents/{id}` (title, owner only),
`DELETE /documents/{id}` (owner only: delete the row and write durable
`document.deleted` to the outbox in one transaction; after the commit,
background tasks publish the event, so ai-service removes the indexed
chunks, and delete the stored file. A failed file delete only logs
`document_orphan_cleanup_failed` and leaves an orphan object; the nightly
sweep removes it only once its owner is a deleted account, 3.15). The row
delete and the outbox entry are one helper,
`service.delete_document_row`, which the operator script
`scripts/delete_documents.py` uses too, so both paths stay the same.

`/content` and `/preview` are not streamed from object storage:
`shared/storage.download_bytes` reads the whole object into memory (at
most `MAX_UPLOAD_SIZE_BYTES`, 20 MB, per request). `/content` returns it
as one response; `/preview` sends the in-memory bytes in 64 KiB chunks
with an explicit `Content-Length`.

**Read rule:** the owner; members of the document's organization; and
everyone for *system documents* (owner id
`00000000-0000-0000-0000-0000000005ec`, `shared.schemas.SYSTEM_DOCUMENT_OWNER_ID`, the
documents the nightly ingestion adds and the RAG assistant cites).
Otherwise 404.

**System documents** come in through `api/internal.py`:
`GET /internal/documents/filenames` (dedup check for auto-ingestion;
optional `system_only=true`),
`POST /internal/documents` (raw text from ingestion), `POST
/internal/documents/system` (multipart, used by ai-service's seed upload
script). They use the same `ResearchService.upload`.

### 3.11 Notifications (`domains/notification/`)

Table `core.notifications`: `user_id`, `type`, `title`, `content`,
`read_at`, `created_at`. Routes under `/api/v1/notifications`: `GET ""`
(`unread_only`, paginated), `POST /{id}/read`, `POST /read`
(`{notification_ids: [...]}`, bulk). Someone else's notification is a 404.

Mark-read: `POST /{id}/read` keeps the first `read_at`; the bulk route
(at most `MAX_BULK_MARK_READ_IDS` = 500 ids per request) updates only the caller's
own unread rows, silently skips ids that do not exist or belong to
someone else, and answers only the count. Lists are ordered with the id
as tie-breaker, so pages never repeat or skip.

How a notification is made (`domains/notification/service.py`):

```
 route: notify_after_commit(background_tasks, [actor, other affected users], type, title, content)
   (runs after the commit, de-duplicates ids)
 event handler (another service acted): notify_user / notify_users directly
     |
     v
 delivery_preferences(user)  -> auth-service /internal/users/{id}/notification-settings
     (defaults when unreachable: in_app on, websocket on, email off)
 deleted account? -> nothing (log notification.skipped_deleted_user)
 in_app off? -> only the optional email, no row
 else: own session -> INSERT notification -> COMMIT
       deliver: websocket on -> push {"event": "notification", "payload": row}
                to channel user:<id>, cross_replica=True (every replica's sockets)
                email on and address known -> send_notification_email
                in a background task (the handler does not wait for SMTP)
 never raises (a failed notification never fails the action)
```

The user's channels come from `user_settings.notification_preferences` in
auth-service (keys `in_app`, `email`, `websocket`). `in_app` and
`websocket` default to on (so a user who never opened their settings
still gets live notifications); email is opt-in because it leaves the
app. The same answer carries `deleted`: for a deleted account
`notify_user` creates no row, pushes nothing and sends no email (log
`notification.skipped_deleted_user`). This one check covers every event,
for example a `tier.cancelled` that ai sends after the user's
portfolios were deleted. It needs no extra call. The Redis user-state
mirror is not used here, because it does not tell a deleted account from
a deactivated one, and a deactivated user still gets notifications. If
the lookup fails, the defaults apply and the notification is created
(fail open, log `notification_settings_lookup_failed`). With `in_app` off no row is written and nothing is pushed; email
follows its own switch. The preferences are read without a cache (they
can change and notifications are rare). Delivery runs only after the
row is committed, so a client is never told about a row a failed commit
rolled back.

Typical notifications: portfolio created/updated/deleted, transaction
recorded, holding adjusted, watchlist item added/removed, note and comment
actions, document uploaded/ready/failed, API key created/updated/revoked,
analysis started/completed/failed, recommendation ready, decision recorded,
reviewed or evaluated, tier executed/failed/cancelled, backtest
completed/failed/deleted, organization, invitation, friend and block
events, account changes (the full mapping is in
`event_listener.py` and the route files).

**Retention.** Read notifications do not stay forever. The nightly
scheduler job `notification-retention` (04:20 New York, chapter 26) calls
`POST /internal/scheduler/notification-retention`, which runs
`NotificationService.purge_old_read_notifications()`: it deletes rows
with `read_at` set and `created_at` older than
`NOTIFICATION_READ_RETENTION_DAYS` (default 90) days. Unread rows are
kept whatever their age, so nothing the user has not seen disappears.
The age is the notification's own age (`created_at`), not the time it
was read. Rows are deleted in batches of `NOTIFICATION_PURGE_BATCH_SIZE`
(default 1000; `NotificationRepository.delete_read_before`, a `DELETE
... WHERE id IN (SELECT id ... LIMIT n)`), each batch committed on its
own, so a large first run never holds one long transaction. A failed
batch is rolled back and logged (`notification_retention_failed`) and
ends the run; the answer `{"retention_days", "deleted", "batches",
"failed_batches"}` goes through `job_response` with the step `batches`,
so it is a 500 (and no heartbeat) only when every attempted batch failed.
Log at the end: `notification_retention_done`.

### 3.12 Job health heartbeats (`job_health.py`)

Problem: a failed backup, ingestion run or market close leaves one log
line and nothing else, and nobody notices. So every job script in the
scheduler reports success:

```
 scheduler container (cron, New York time)
   call-job.sh market-close: curl POST core /internal/scheduler/market-close
       success -> /scripts/heartbeat.sh market-close
   heartbeat.sh: curl -f --max-time 10 POST core /internal/scheduler/heartbeat/market-close
                 (X-Internal-Token; never fails the job itself)
 core-service: scheduler_heartbeat(job)
   job not in SCHEDULED_JOBS -> 404
   record_job_success: gauge alphamind_job_last_success_timestamp_seconds{scheduled_job} = now
                       HSET core:job_last_success <job> <unix time>   (Redis db 0)
   -> {"job", "recorded_at"}
 market-close route: record_market_close_skipped(n)
                     gauge alphamind_market_close_portfolios_skipped = n
                     SET core:market_close_skipped <n>                 (Redis db 0)
 core-service start: load_job_health() re-exports the stored times and skip count;
                     a job with no stored success:
                       HSETNX core:job_tracked_since <job> <start time>  (Redis db 0)
                       gauge = that "tracked since" time
 Prometheus scrapes core /metrics; alert rules (group scheduled_jobs):
   DailyJobStale        backup|review-run|rag-ingest|logs-cleanup|
                        purge-unverified|notification-retention|
                        deleted-user-sweep-core|deleted-user-sweep-ai > 26 h  (for 10m)
   TradingDayJobStale   market-open|market-close|tier-monitoring    > 26 h, 76 h after a Friday (for 10m)
   MarketCloseSkippedPortfolios   alphamind_market_close_portfolios_skipped > 0
```

`SCHEDULED_JOBS`: `market-open`, `market-close`, `tier-monitoring`,
`review-run`, `rag-ingest`, `logs-cleanup`, `backup`, `purge-unverified`
(auth-service), `notification-retention`, `deleted-user-sweep-core`,
`deleted-user-sweep-ai` (ai-service). The trading-day
jobs still report success on a closed-market weekday (a holiday): the
route answers "skipped" and the script sends the heartbeat. So a
success is due every weekday: the limit is 26 hours after a Monday to
Thursday success, 76 hours after a Friday (or Saturday) success, which
covers the weekend (Friday 17:30 to Monday 17:30, 71-73 hours), and 50
hours after a Sunday success.

**A job that has never succeeded still alerts.** An alert rule compares
`time()` with the gauge; a label that has no series compares with nothing
and never fires. Without a start value, a job that fails from its very
first run (a fresh install with a wrong backup mount, or the hash lost
with the Redis volume) would never report and never alert. So at start
`load_job_health` gives every name in `SCHEDULED_JOBS` without a stored
success a "tracked since" time: the first core start that saw the job
without a success, kept in the Redis hash `core:job_tracked_since` with
`HSETNX`, so a later restart keeps the first time and does not push the
alert further away. The gauge holds that time until the first real
heartbeat replaces it, so `DailyJobStale` fires 26 hours, and
`TradingDayJobStale` 26, 76 or 50 hours (by the weekday tracking started on), after tracking started. The log line
`scheduled_job_no_success_yet` (info, `job`, `since`) names each such
job at start. If Redis cannot be read at start, the gauge of every job
is set to the process start time (nothing is written), so the series
exist in any case. While a job has not succeeded yet, the gauge value is
therefore the tracking start, not a success time; the hash
`core:job_last_success` is the record of real successes.

`tests/core-service/test_platform.py::TestJobHealth` checks that the
crontab, `call-job.sh`, the scripts that call `heartbeat.sh` and the two
`scheduled_job=~` regexes in `infra/prometheus/alert_rules.yml` name
exactly the jobs of `SCHEDULED_JOBS`, so a job added in one place and
forgotten in another fails the tests.

The label is called `scheduled_job`, not `job`. Prometheus sets its own
`job` label on every scraped series (the scrape job, here
`core-service`). A metric label with the same name would be renamed to
`exported_job`, and alert rules that filter on `job` would never match.

The Redis hash keeps the times, and the key `core:market_close_skipped`
keeps the skip count, across a core-service restart. Without the stored
count a restart would reset it to 0 and silently clear the
`MarketCloseSkippedPortfolios` alert. If Redis fails the gauge is still
set and only the persistence is lost. `make health`
(`scripts/health.sh`) prints the last success of every job and the skip
count.

### 3.13 Internal routes (`api/internal.py`)

All need `X-Internal-Token` and are never proxied by the gateway. Several
have **no ownership check on purpose**: the caller is a trusted service
acting for a request it already authorized.

| Route | Caller | Purpose |
|---|---|---|
| `POST /internal/scheduler/market-close` | scheduler 17:30 NY | snapshots (3.5) |
| `POST /internal/scheduler/market-open` | scheduler 09:30 NY | analyses (3.6) |
| `POST /internal/scheduler/heartbeat/{job}` | scheduler scripts | job health (3.12) |
| `POST /internal/scheduler/notification-retention` | scheduler 04:20 NY | deletes read notifications older than 90 days (3.11) |
| `POST /internal/scheduler/deleted-user-sweep` | scheduler 04:30 NY | runs `delete_private_user_data` and `delete_unreferenced_files` for every deleted account that still owns private core data or an unreferenced document file, then (always) `delete_organization_content` for every referenced organization auth no longer has (3.15); `{candidates, deleted, cleaned, failed, lookup_failed, refused, organizations: {same}}`; 500 when every deleted user or every missing organization failed, when either auth lookup failed, or when the organization safety brake refused |
| `GET /internal/portfolios/{id}/holdings` | ai-service | holdings as floats |
| `GET /internal/portfolios/{id}/owner` | ai-service access checks | owner, organization, visibility |
| `GET /internal/portfolios/{id}/cash-balance` | ai-service position sizing | `{cash_balance}` |
| `GET /internal/portfolios/{id}/snapshots` | ai-service backtests | NAV history |
| `GET /internal/portfolios/{id}/cash-flows?start_date&end_date` | ai-service time-weighted return | deposits/withdrawals (signed `amount`), adjustments (amount 0), with `snapshot_date` |
| `POST /internal/portfolios/{id}/transactions` | ai-service tier execution | ledger write with `idempotency_key` (3.3) |
| `GET /internal/stats` | auth-service admin stats | a flat object of integer row counts (auth shows `null` for core when this call fails) |
| `GET /internal/tracked-tickers` | ai-service ingestion | every held and watched ticker of owners whose account is active (one live-state lookup per owner; a deactivated or deleted owner's tickers are left out, logged `tracked_tickers_inactive_owners_skipped`; an unknown state is kept) |
| `GET /internal/documents/filenames`, `POST /internal/documents`, `POST /internal/documents/system` | ai-service | system documents (3.10) |

### 3.14 API keys and the public API (`apikeys.py`)

Swagger (`/docs/core/`) declares the `X-API-Key` header as a security
scheme of the five `/api/v1/public/*` routes, so its "Authorize" button
sends the key with "Try it out". The middleware in `apikeys.py` is still
the only place that checks the key.

Keys: `amk_` + 32 random bytes (URL-safe base64). The prefix lets a leaked
key be recognized in logs. Only `sha256(key)` is stored
(`core.api_keys.key_hash`, unique); the raw key is shown **once**, in the
create response. A plain SHA-256 is enough because the key is high-entropy
random data (unlike a password), and it keeps lookup a single indexed
query.

Management (JWT, `apikeys_router` in `api/public.py`, prefix `/api/v1/api-keys`):
`POST ""` (`{name, rate_limit_per_minute?}`), `GET ""`, `PATCH /{id}`,
`DELETE /{id}` (revoke). Creating, changing and revoking need
`api_key:manage` (guests get 403). At most
`MAX_ACTIVE_API_KEYS_PER_USER` = **10** non-revoked keys per user: the
count runs after a transaction-scoped advisory lock on the owner
(`pg_advisory_xact_lock(hashtext('apikeys:<owner>'))`), so two concurrent
creates cannot both pass; the 11th is 409.

The public API (`api/public.py`, prefix `/api/v1/public`):

| Route | Effect |
|---|---|
| `GET /portfolios/{id}` | portfolio (same read rule as the owner's token) |
| `GET /portfolios/{id}/holdings` | paginated holdings |
| `PUT /portfolios/{id}/holdings/{hid}` | ADJUSTMENT rows (3.4) |
| `POST /watchlists/{id}/items`, `DELETE /watchlists/{id}/items/{ticker}` | watchlist items |

`PublicAPIRateLimitMiddleware` runs for every path starting with
`/api/v1/public` (CORS preflight `OPTIONS` is let through):

```
 X-API-Key missing -> 401 missing_api_key
 key_hash = sha256(key)
 Redis db0 apikey:valid:<hash> (cached 30 s) else Postgres lookup, then cache
   unknown or revoked -> 401 invalid_api_key
 owner live state (shared/user_state.py):
   deactivated/deleted -> 401 account_deactivated
   non-GET/HEAD and the owner's role lacks portfolio:write (guest) -> 403
 two fixed one-minute counters in Redis db1 (shared/redis.py):
   ratelimit:<key id>          limit = min(key's rate_limit_per_minute, 60)
   ratelimit:owner:<owner id>  limit = 60 (shared by ALL of the owner's keys)
   over -> 429 rate_limit_exceeded + Retry-After (Redis down -> allowed, logged)
 last_used_at written at most once per 60 s per key (SET NX gate)
 request.state.api_key_owner_user_id = owner -> the route acts as that user
```

The per-owner counter stops "create more keys to get more requests". The
owner's live state is checked before the counters, so a refused request
does not use the owner's budget. The cache and the `last_used_at` gate
exist because validating a key and writing `last_used_at` on every
request would be a database round trip plus a contended `UPDATE` of the
same row for a busy key. The holdings list of the public API is
paginated in memory with the same `PaginationParams` arithmetic, because
the service returns the small per-portfolio list unpaginated. It is the
only holdings list that asks ai-service for each holding's
`latest_recommendation` (only for the rows of the page); the web app's
list leaves it null because the page does not show it.
Revoking a key overwrites its cached entry with a revoked marker
(`mark_revoked_in_cache`); changing a key deletes its cache entry
(`forget_cached_key`). Each write happens twice: once in the request,
before the commit, and once more in a background task after the commit.
The first write makes the change visible to other requests at once; the
second closes a race. A public-API request that missed the cache can
read the key row before the revoke commits (still valid) and write its
"valid" entry after the first marker; without the second write that
stale entry would let the revoked key work, or keep the old rate limit,
for up to 30 s. After the commit every database read sees the new row,
so the second write is final. The marker is used rather than a delete
because a delete would let such a racing request cache the key as valid
again. Both writes go through `_write_cache_safely`: a Redis error is
logged (`api_key_cache_write_failed`) and the revoke or update still
commits, instead of a 500 and a rollback. The middleware treats a Redis error as a
cache miss and reads the database, so the revoke still applies; a "valid"
entry that survives the outage expires within 30 s.

### 3.15 Events consumed and published (`event_listener.py`)

**Published by core-service** (all durable, Redis Streams
`stream:<event>` in db 3; each is written to `core.event_outbox` in the
request's transaction and published after the commit, with the outbox
dispatcher as the retry path):

| Event | When | Consumer |
|---|---|---|
| `document.uploaded` | upload, failed-retry, stuck republish (`uploaded` unchanged for 10 minutes, `parsing`/`chunking`/`embedding` unchanged for 60 minutes) | ai-service ingestion |
| `document.deleted` | document delete, or cleanup of a deleted user's private documents or a deleted organization's documents | ai-service removes chunks |
| `portfolio.deleted` | portfolio delete, cleanup of a deleted user's private portfolios, or cleanup of a deleted organization's portfolios whose owner is a deleted account | ai-service cancels not-yet-executed tiers |

**Consumed** (two subscribers started in `lifespan`):

1. **Durable** (`build_durable_subscriber`, consumer name
   `core-service`, group `workers`): only `document.status_changed`
   -> `_persist_document_status`. One consumer per entry, so the
   ready/failed notification is created once, and only on a real
   transition (a redelivered event does not notify again). The
   `document_id` is converted to a UUID explicitly, so a malformed id
   logs a clear warning; an unknown id is ignored instead of breaking
   the handler.
2. **Pub/sub** (`build_subscriber`, channels `events:<name>`): every
   replica gets every event.

| Events | Live push (WebSocket channel) | Notification |
|---|---|---|
| `analysis.started`, `analysis.completed`, `analysis.failed`, `recommendation.ready`, `decision.recorded` | `analysis:<analysis_id>` | the analysis user |
| `decision.reviewed`, `decision.outcome.evaluated`, `backtest.completed`, `backtest.failed`, `backtest.deleted` | none | the user |
| `tier.executed`, `tier.execution_failed`, `tier.cancelled` | `analysis:<analysis_id>` | the user, or the portfolio owner when `user_id` is empty (system analysis) |
| `document.status_changed` | `document:<document_id>` | (by the durable handler) |
| `organization.*` (created, updated, deleted, member joined/left/removed, role changed) | none | members and the affected user (own wording) |
| `organization.invitation.created`, `organization.invitation.revoked` | none | the actor, and the invitee when the invited email has an account |
| `friend.requested`, `friend.accepted`, `friend.removed` | none | the actor; the other side for a request, an accept and an unfriend (`friend.removed` with `action` `removed`, or no `action`) |
| `connection.blocked` | none | the actor only (never the blocked user) |
| `user.updated`, `user.role.changed`, `user.deactivated`, `user.reactivated` | none | the user, and the admin or moderator who acted (when `actor_id` differs) |
| `user.deleted` | none | the admin who deleted (the unverified-account purge, where the actor is the user: nobody, only logged); the user's private data is deleted first (see below) |
| `user.presence.changed` | `presence:<user_id>` | none |

Before the notification work, the handlers of
`organization.member.removed`, `organization.member.left`,
`organization.role.changed`, `organization.deleted` (all `member_ids`),
`user.role.changed`, `user.deactivated`, `user.deleted`, `friend.removed`
(`actor_id` and `user_id`, for every `action`) and `connection.blocked`
(`actor_id` and `blocked_user_id`) re-check the
affected users' open Yjs sockets on every replica (no claim; a failure is
logged and never stops the notification; see 3.9). auth-service drops the
membership cache and writes the live state before it publishes, so the
re-check sees the new rights. The `member.left` and `member.removed`
handlers then make the member's shared portfolios of that organization
private (3.2; also on every replica, the update is idempotent). An analysis or tier event whose `user_id`
is empty (`""`, a system analysis) never creates a notification for an
empty id; tier events then notify the portfolio owner.

`user.updated` gives the user an "Account updated" notification
(`account_updated`, listing the changed field names) for every change
auth-service publishes, including the user's own profile edits (`PATCH
/me`, avatar upload, password reset, 2FA enable/disable), not only changes
made by an administrator. It works as a security notice: an edit the user
did not make is visible. When an admin or moderator made the change, that
person gets a notification too ("You updated the account of X (fields).").

Recipient helpers: `_notify_payload_user` notifies `user_id`,
`_notify_actor` notifies `actor_id`, and `_notify_pair` claims the event
once and then notifies `user_id` and `actor_id` (when they differ), each
with its own text. `friend.removed` with an `action` of `cancelled`,
`declined`, `unblocked` (or an unknown value, with the text "Your
connection with <name> changed.") notifies only the actor, so the other
user never learns about an unblock or a declined request.

**Deleted users** (`delete_private_user_data`). Policy: a deleted
user's personal data is removed in every service. Data that
belongs to that user alone is deleted; content other people use
(organization documents and notes, comments on organization notes) stays
and shows the anonymized username. A portfolio the user shared with an
organization also stays, readable by that organization's members, until
the organization is deleted (then it is deleted with the organization's
other data). System data not keyed to a user
stays. core's part runs on `user.deleted`; each step runs in its own
session and commits:

- private documents (`organization_id` is null), through
  `delete_document_row`: the row goes, `document.deleted` is queued in
  the outbox and published, and the S3 object is deleted (a failed S3
  delete logs `document_orphan_cleanup_failed`; the nightly sweep below
  deletes the file later);
- private notes (`organization_id` is null); their comments and tags go
  by cascade;
- portfolios that no one else can read (no organization, or visibility
  `private`), through `delete_portfolio_row`, which queues
  `portfolio.deleted`; holdings, transactions and snapshots go by
  cascade;
- watchlists with their items, API keys and notifications.

A failed step is logged (`user_data_cleanup_failed`, with the step
name) and the other steps still run. The result is logged as
`user_data_cleaned` with a count per step. The cleanup only deletes what
is still there, so a repeated run does nothing. The step runner is
`shared/deleted_users.py::run_cleanup_steps`, shared with ai-service,
which deletes its own part on the same event (chapter 13, section 3.8).

**Nightly deleted-user sweep** (`sweep_deleted_users`, job
`deleted-user-sweep-core`, 04:30 New York, chapter 26). The event is the
fast path; the sweep is the guarantee. It lists every user id that still
owns private core data (the same filters as the steps above: private
documents and notes, unshared portfolios, watchlists, API keys,
notifications; the system document owner is left out), asks auth which
of them are deleted (`POST /internal/users/deleted`, batches of 500),
and runs `delete_private_user_data(..., strict=True)` for each one,
then `delete_unreferenced_files`. Document files are stored under
`documents/<owner_id>/` in the documents bucket. So the sweep also adds
to its candidates every owner id with a file there that no document row
references (`users_with_unreferenced_files`; a failed listing is logged
as `document_file_owners_lookup_failed` and the sweep goes on with the
database candidates). A deleted user whose folder holds only files of
kept organization documents is therefore not a candidate, so it is not
counted again every night. For each deleted user it deletes every object
under that user's folder that no document row references any more (log
`deleted_user_files_removed`). A file of a kept organization document
is still referenced, so it stays. This repairs an S3 delete that failed
during the event cleanup: the row was already gone, but the file is
found again by its folder. A
user with a failed step counts as failed (`deleted_user_sweep_item_failed`)
and the others still run; the next night retries it, because its rows
are still there. This catches a lost `user.deleted` (failed publish,
core down) and a document or portfolio that an in-flight request
committed after the cleanup. New requests of a deleted account are
already refused by the live user-state check (chapter 29). If the
candidate query or the auth lookup fails, nothing is deleted (`deleted_user_sweep_candidates_failed`
or `deleted_user_sweep_lookup_failed`, `lookup_failed: true`, step `users_lookup`), and the organization sweep
below still runs. The sweep logs `deleted_user_sweep_completed`.

**Deleted organizations** (`delete_organization_content`). On
`organization.deleted` (an owner delete, or auth deleting an
organization whose sole owner was deleted), core cleans that
organization's content with the same steps and rules, after the socket
re-check and before the notifications:

- its documents, through `delete_document_row` (S3 object and ai chunks
  go too);
- its notes, with all their comments and tags by cascade;
- its portfolios. A portfolio always belongs to a user; the
  organization only shares it. For each owner core asks auth
  (`get_notification_settings`, field `deleted`). A portfolio whose
  owner is a deleted account is deleted through `delete_portfolio_row`
  (`portfolio.deleted`). Any other portfolio is made private again
  (`organization_id` null, visibility `private`), so a live owner keeps
  the ledger and history. If the lookup fails, the portfolio is made
  private (data kept) and `organization_portfolio_owner_lookup_failed`
  is logged.

Documents and notes shared with an organization are organization
content, written for the group, so they go with the organization. A
portfolio is personal financial data that the organization only shares,
so a living owner keeps it as a private portfolio.

Logs: `organization_data_cleaned` and `organization_data_cleanup_failed`.
auth deletes the organization's members and invitations itself, so
nothing is done twice. The owner check also covers a user who was
deleted earlier while the organization survived (a plain member, or an
owner with a successor): their shared portfolios are deleted when the
organization is deleted later.

**Nightly organization sweep** (`sweep_deleted_organizations`, run by
the same `deleted-user-sweep-core` job after the user sweep, whatever
the user sweep's outcome). It
lists every organization id still referenced by a document, note or
portfolio, asks auth which of them no longer exist (`POST
/internal/organizations/missing`, batches of 500), and runs
`delete_organization_content(..., strict=True)` for each missing one.
This repairs a lost `organization.deleted`. An id counts as missing only
when auth answered successfully; if the lookup fails, nothing is deleted
(log `deleted_organization_sweep_lookup_failed`), the route answers 500
(step `organizations_lookup`) and the next night retries.
**Safety brake:** an organization is hard-deleted in auth, so "missing"
means "delete its content", and a wrong or reset auth would report every
organization as missing. So if auth reports more missing organizations
than `ORGANIZATION_SWEEP_MAX_MISSING` (default 5), or reports every
referenced organization as missing while there is more than one (the
typical sign of a wrong environment), the sweep deletes nothing, logs the error
`deleted_organization_sweep_refused` with the counts, reports
`refused: true` and the job answers 500 (step
`organizations_safety_brake`), so the stale-job alert fires. An operator
checks auth and, if the deletions are real, raises the setting for one
run. When every referenced organization was really deleted, no setting
passes the brake; the operator then runs
`docker compose exec core-service python -m scripts.clean_organizations
--id <org id> [--id ...]` (a preview), then the same with `--confirm`.
The script asks auth first and cleans only the ids auth reports missing,
with the same `delete_organization_content(..., strict=True)` as the
sweep; an id auth still has is printed as skipped. On a small deployment a
single lost event (for example 1 of 3 organizations) is still repaired.
The brake cannot protect a deployment with exactly one referenced
organization: a wrong auth there still deletes that organization's
content, so check `AUTH_SERVICE_URL` before running the job by hand.
One failing
organization is logged (`deleted_organization_sweep_item_failed`) and the
others still run. Log `deleted_organization_sweep_completed`.

**Known limitations.**

- Both cleanups run inline in the single pub/sub dispatch loop, with
  sequential S3 deletes. While one runs, the other cross-service events
  (socket re-checks, notifications) wait. This is acceptable at the
  current scale.
- Both cleanups run before any claim (`_claim_event`). The stack runs one
  core replica; with more than one, every replica would run them: the
  data still ends correct (each step deletes only what is still there),
  but S3 deletes are repeated and `document.deleted` / `portfolio.deleted`
  can be queued twice.

**Replica de-duplication** (`_claim_event`). With several core replicas,
each one receives every pub/sub event. Pushes must happen on every
replica (each has its own sockets), but a notification row must be
created once. The first replica to `SET core:event_claim:<event>:<id> NX
EX 300` (db 2) creates it. `<id>` is the publisher's `event_id` (a fresh
one per publish), so two real events with identical content (deactivate,
reactivate, deactivate within five minutes) are not merged. Every
publisher adds `event_id` (`shared/events.py`); a payload without one is
not claimed, so each replica would act on it. If Redis is
down the claim fails open: a duplicate notification is better than a lost
one.

### 3.16 WebSockets (`api/websocket.py`, `websocket_manager.py`)

URL: `wss://localhost:8443/ws/<channel>?token=<access token>`. The token
goes in the query string because browsers cannot set an `Authorization`
header on the WebSocket handshake (the gateway does not log query
strings, chapter 10).

**Channel allow-list and authorization** (`_CHANNEL_PREFIXES = ("user",
"presence", "analysis", "document")`, `authorize_channel`):

| Channel | Allowed when |
|---|---|
| `user:<uuid>` | the uuid is the caller (own notifications) |
| `presence:<uuid>` | the caller, or an accepted connection (friend) per auth-service `/internal/connections/check` |
| `analysis:<uuid>` | the caller owns the analysis; for a system analysis (no user), the caller can read its portfolio |
| `document:<uuid>` | the caller can read the document (same rule as `GET /documents/{id}`) |

Any other prefix, or a malformed name, is refused. Any error during the
check means deny.

**Connection sequence:**

```
 client                         core-service
   | GET /ws/<channel>?token=...  (Upgrade)
   |------------------------------>| authenticate(token): valid access JWT,
   |                               |   UUID sub, numeric exp      else 4401
   |                               | resolve_live_identity: deactivated -> 4401
   |                               | authorize_channel            else 4400 / 4403
   |                               | manager.acquire_user: > 20 sockets -> 4429
   |                               |   first socket of the user -> presence task:
   |                               |   POST auth /internal/users/{id}/presence online
   |<------ accept ----------------| (refusals also accept first, then close
   |                               |  with the code, so the browser sees it)
   | analysis channel: snapshot {"event": "analysis.status", payload: {...}}
   |<------ pushes: {"event", "payload", "emitted_at"} ---------------------
   | (client sends only text frames; a binary frame -> close 1003)
   |                               | token exp reached -> close 4401 "token expired"
   | disconnect                    | release_user; last socket -> presence offline
```

Close codes: 1003 unsupported data, 1008 policy violation (Yjs), 1011
internal error, 4400 bad channel, 4401 unauthorized or token expired,
4403 forbidden, 4429 too many connections (`websocket_manager.py`). The
`analysis.status` snapshot exists because ai-service usually
publishes `analysis.started` before the client has subscribed; the
snapshot makes sure the client is not left without a state. It is sent
after the socket joins the channel, so a live event can precede it.

Open channel sockets follow access changes too: on the same events as
the Yjs re-check (section 3.9), `recheck_user_sessions` runs the channel
check again for every `/ws/{channel}` socket of the user. A deactivated or
deleted account gets 4401 "account deactivated", a channel the user can
no longer read gets 4403 "access to this channel was revoked" (log
`websocket_session_closed_on_access_change`). After `friend.removed` or
`connection.blocked`, an open `presence:<id>` socket is closed with 4403
once the two users are no longer connected. If the check itself fails,
the socket stays open (log `websocket_session_recheck_failed`). The
presence check is an exception: `auth_client.are_users_connected`
answers False on an auth-service HTTP error, so during an auth-service
outage this re-check closes presence sockets instead of keeping them
open.

Accepted limitation: `analysis:<id>` and `document:<id>` sockets are not
closed when the resource stops being shared (a portfolio PATCH to
`visibility="private"`, a portfolio delete, a document delete), because
no event triggers a re-check for them. An organization member's open
socket keeps receiving status payloads until its token expires (15
minutes or less); REST reads answer 404 at once.

**Fan-out rules** (`websocket_manager.py`):

- `MAX_CONNECTIONS_PER_USER = 20` per process (JSON channels and Yjs rooms
  together), so one token cannot exhaust memory.
- Each push reaches each socket at most once (`sockets_for`
  de-duplicates), and every socket of a user gets its own copy.
- A socket whose send fails is logged (`websocket_send_failed`) and closed
  with 1011, so the client notices and reconnects.
- Pushes produced by exactly one replica (a notification) are sent with
  `cross_replica=True`: delivered locally and published to Redis channel
  `ws:fanout` (db 2) with this replica's random id; other replicas deliver
  them to their sockets and ignore their own echo. Pushes that every
  replica produces anyway (from pub/sub events) stay local.
- Presence: one task per user with any socket; online at the first socket,
  a heartbeat every `PRESENCE_HEARTBEAT_SECONDS = 60`, offline when the
  last socket closes. All calls of one user come from that one task, so
  they reach auth-service in order.

### 3.17 Clients (`clients/`)

| Client | Calls | Behaviour on failure |
|---|---|---|
| `auth_client.get_user_ref` | auth `/internal/users/{id}` | cached 60 s in Redis db 0 (a Redis error is a cache miss, because every portfolio, note and comment read builds one); error -> 502 |
| `auth_client.list_user_organization_roles` / `_ids` | auth `/internal/users/{id}/organizations` (via `shared/user_state.py`) | cached 60 s, invalidated by auth on membership changes; a missing role counts as `viewer` |
| `auth_client.ensure_organization_writer` | (uses the roles above) | 403 for non-members and viewers |
| `auth_client.are_users_connected` | auth `/internal/connections/check` | False (fail closed) |
| `auth_client.get_notification_settings` | auth `/internal/users/{id}/notification-settings` | not cached; error -> caller uses defaults |
| `auth_client.set_user_presence` | auth `/internal/users/{id}/presence` | logged, never raises |
| `ai_client.get_latest_recommendation` | ai `/internal/recommendations/latest` (ticker + portfolio) | cached 10 s; a Redis error on the cache read or write is logged (`latest_recommendation_cache_read_failed` / `_write_failed`) and treated as a cache miss; holdings still returned without it |
| `ai_client.get_analysis_ref` | ai `/internal/analyses/{id}` | raises (WebSocket check then denies) |
| `ai_client.get_daily_closes` | ai `/internal/market/instruments/{ticker}/prices?interval=1d` (market-close job, history export, recompute script) | None; 15 s timeout; `lookback_days` capped at 3650 (ai's limit); closes keyed by New York session date |
| `ai_client.get_fx_rate` | ai `/internal/fx-rate` (ai-service treats a Redis error on its own rate cache as a cache miss, not a 5xx) | None (USD to USD is 1 without a call) |
| `ai_client.request_analysis` | ai `POST /internal/analyses` | None; 15 s timeout |
| `ai_client.analyze_sentiment` | ai `POST /internal/sentiment` | None |

All use the shared pooled client (`shared/http_client.py`), which adds
`X-Internal-Token` to `/internal/` calls. Timeouts: 3 s (`_TIMEOUT`) for
`auth_client` and `ai_client` calls, except `get_daily_closes` and
`request_analysis` with 15 s (a close may trigger a provider refresh in
ai-service, and a create can involve more work than a cached read);
`ai_client.analyze_sentiment` waits up to 270 s (`_SENTIMENT_TIMEOUT`), because the sentiment call is an
LLM call that runs in a fire-and-forget asyncio task (the HTTP response does not wait for it); the wait is longer than ai-service's own
limit (`SENTIMENT_TIMEOUT_SECONDS`, 240 s), so a slow answer or its 503 arrives. The sentiment contract: body
`{"text", "kind": "note" | "comment"}` -> `{"label", "score" in [-1, 1],
"model"}`; 422 for empty text, 503 when the LLM is unavailable; any
failure or an answer outside the contract returns `None`.

### 3.18 Migrations

Four revisions in `migrations/versions/`:

- `f1160c000001` (baseline, every table);
- `f1170c000001` (recreates the two document unique indexes with `NULLS
  NOT DISTINCT` after checking that no duplicates exist, drops the
  never-used `api_keys.owner_org_id` and `api_keys.scopes`, makes
  `owner_user_id` NOT NULL; it refuses and lists the rows when
  duplicates or ownerless keys exist);
- `f1200c000001` (creates `core.event_outbox`: `event_name`, `payload`,
  `created_at`, `published_at`, `attempts`, `last_error`, with the
  partial index `ix_event_outbox_unpublished` on `created_at` where
  `published_at IS NULL`, the dispatcher's query);
- `f1250c000001` (adds `core.documents.status_changed_at`, NOT NULL,
  default `now()`, filled from `uploaded_at` for existing rows; the stuck
  rule measures from it).

Head: `f1250c000001`. Bootstrap runs at start
([chapter 20](20-postgresql.md) describes the shared mechanism).

---

## 4. Configuration

`services/core-service/config.py` unless noted.

| Setting / constant | Default | Meaning |
|---|---|---|
| `PUBLIC_API_RATE_LIMIT_PER_MINUTE` | `60` | default and maximum per-key limit, and the per-owner budget |
| `MAX_ACTIVE_API_KEYS_PER_USER` | `10` | non-revoked keys per user |
| `FAILED_DOCUMENT_RETRY_DAYS` | `7` | nightly auto-ingestion resubmits a `failed` document only while its first upload is newer than this many days; `0` disables the retry |
| `NOTIFICATION_READ_RETENTION_DAYS` | `90` | read notifications created longer ago are deleted by the nightly `notification-retention` job; unread ones are kept |
| `NOTIFICATION_PURGE_BATCH_SIZE` | `1000` | rows per delete batch (one commit each) of that job |
| `ORGANIZATION_SWEEP_MAX_MISSING` | `5` | safety brake of the nightly organization sweep: more missing organizations than this (or all of them, when more than one is referenced), and it deletes nothing and fails (3.15) |
| `MAX_UPLOAD_SIZE_BYTES` | `20 * 1024 * 1024` | document size limit (gateway allows 25 MB) |
| `ALLOWED_DOCUMENT_EXTENSIONS` | `.pdf`, `.txt`, `.md` | |
| `ALLOWED_DOCUMENT_CATEGORIES` | all `DocumentCategory` values | `filing`, `analyst_report`, `earnings_call`, `news`, `macro_research`, `risk_analysis` |
| `YJS_MAX_UPDATE_BYTES` | `1024 * 1024` | largest Yjs update |
| `S3_BUCKET_DOCUMENTS` | `research-documents` (`shared/config.py`) | |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | `http://minio:9000`, ... (`shared/config.py`) | object storage (alias `minio` points at SeaweedFS) |
| `USER_STATE_CHECK_ENABLED` | `true` (`shared/config.py`) | live owner check for tokens, API keys, WebSockets, internal trades |
| `KEY_CACHE_TTL_SECONDS` | `30` (`apikeys.py`) | API key validation cache |
| `_LAST_USED_UPDATE_INTERVAL_SECONDS` | `60` (`apikeys.py`) | `last_used_at` write throttle |
| `STUCK_DOCUMENT_AFTER` | 10 minutes (`domains/research/repository.py`) | a document still `uploaded` this long after its last status change is republished on re-upload and left out of the auto-ingestion dedup list |
| `STUCK_PROCESSING_AFTER` | 60 minutes (same file) | the same for `parsing`, `chunking` and `embedding` |
| `SNAPSHOT_TIME_ET` | 17:30 (`domains/portfolio/ledger.py`) | snapshot cutoff |
| `MAX_CONNECTIONS_PER_USER` | `20` (`websocket_manager.py`) | sockets per user per process |
| `PRESENCE_HEARTBEAT_SECONDS` | `60` (`websocket_manager.py`) | |
| `_EVENT_CLAIM_TTL_SECONDS` | `300` (`event_listener.py`) | replica de-duplication window |
| `OUTBOX_DISPATCH_INTERVAL_SECONDS` / `OUTBOX_GRACE_SECONDS` / `OUTBOX_BATCH_SIZE` / `OUTBOX_RETENTION_DAYS` | `5` s / `10` s / `100` / `7` days (`outbox.py`) | dispatcher period; age before the dispatcher takes over a row; rows per pass, oldest first; how long published rows are kept (purged once an hour) |
| `_CLOSES_TIMEOUT` / `_TIMEOUT` | `15.0` / `3.0` s (`clients/ai_client.py`) | price fetch (analysis create uses a literal `15.0`) / other ai calls |
| `_SENTIMENT_TIMEOUT` | `270.0` s (`clients/ai_client.py`) | sentiment (LLM) call |
| `_CACHE_TTL_SECONDS` | `60` s (`clients/auth_client.py`), `10` s (`clients/ai_client.py`) | user-ref cache / latest-recommendation cache |
| `DEFAULT_THRESHOLD` / `CASH_TOLERANCE` | `1.00` / `0.01` (`scripts/recompute_snapshots.py`) | recompute flags |
| cron times | market-open `30 9 * * 1-5`, market-close `30 17 * * 1-5`, notification-retention `20 4 * * *`, deleted-user-sweep-core `30 4 * * *` (New York) | `infra/scheduler/crontab` |
| `INTERNAL_API_TOKEN` | empty (`shared/config.py`) | `/internal/*` secret |

---

## 5. Design decisions and why

**The ledger is the source of truth; holdings are a cache.**
*Alternative:* let users edit holdings directly.
*Reason:* a direct edit leaves no history, breaks cash and return
calculations, and cannot be audited. Every change is a ledger row
(ADJUSTMENT for manual edits), and backtests can tell external flows from
investment returns.

**Rebuild the holding from a full replay after every share-moving row.**
*Alternative:* apply each row on top of the current holding.
*Reason:* back-dated rows (a forgotten buy, a late split) gave wrong
quantities and costs (the 25 at 64 versus 30 at 53.33 example).

**Same-timestamp phases: in, split, out.**
*Reason:* rows with identical timestamps (two-step adjustments, imported
data) must replay deterministically, and an outflow must not fail because
of storage order.

**No leverage, checked on every later day.**
*Alternative:* check only the current balance.
*Reason:* back-dated outflows could make a past day negative while today
looks fine. The project rule is that cash is never negative; the user
records a deposit first.

**Cash computed, never stored.**
*Reason:* always consistent with the ledger; the cost (read all rows) is
acceptable at this scale.

**Skip a snapshot instead of storing a guess.**
*Reason:* real wrong values (stale bars, valuation at cost) poisoned
backtests. A missing day is visible (alert) and can be filled by the
recompute script; a wrong day was silent.

**Market close at 17:30 New York, after tier monitoring at 17:00.**
*Reason:* trades executed by tier monitoring must be in the day's
snapshot, and the provider's final daily bar is settled by then.

**Durable events for document and portfolio lifecycle, pub/sub for live
pushes.**
*Reason:* a lost "document uploaded" or "status ready" would leave a
document stuck for good, with no error anywhere. A lost live push only
delays a screen update. Streams' consumer groups deliver each entry to one consumer, which
is wrong for "tell every browser", so pushes stay on pub/sub.

**Events and notifications only after the commit.**
*Reason:* no message may describe a change that a failed commit rolled
back.

**A transactional outbox for the three durable events.**
*Alternative:* publish once from a background task after the commit.
*Reason:* with that design, Redis being down at that moment, or the
process stopping between the commit and the publish, loses the event for
good: a document was never indexed, or a deleted portfolio kept its open
tiers. With the outbox row in the same transaction, the event exists
exactly when the change exists, and the dispatcher retries until Redis
accepts it. The cost is at-least-once delivery (an event can be published
twice), which the ai-service handlers accept because they are
idempotent.

**Idempotency keys on the automated trade path.**
*Reason:* a lost HTTP response followed by a retry must not buy twice.

**404 instead of 403 for unreadable resources.**
*Reason:* a stranger cannot learn which ids exist.

**API keys: hashed, shown once, capped, rate-limited per key and per
owner, checked against the owner's live state.**
*Reason:* a leaked database does not leak keys; more keys do not mean more
traffic; a deactivated owner's keys stop at once.

**WebSocket refusals accept first, then close with a code.**
*Reason:* closing before accepting becomes a plain HTTP 403 on the
handshake, and the browser never sees why.

**Job heartbeats with alerts.**
*Alternative:* read the logs.
*Reason:* failed backups and ingestion runs otherwise leave only one log
line and no alert.

---

## 6. When things go wrong

| Symptom | Cause / meaning | How to diagnose |
|---|---|---|
| buy or withdrawal 422 "Insufficient cash ..." | no leverage; the lowest future balance would be negative | log `transaction_rejected_insufficient_cash` (required, available, shortfall day); record a deposit first |
| 422 "with it, the ledger would take out more X shares than were held at some date" | a back-dated sell or negative adjustment before the shares existed | list the ledger: `bash scripts/psql.sh -c "SELECT transaction_date, transaction_type, ticker, quantity, price FROM core.transactions WHERE portfolio_id = '<id>' ORDER BY transaction_date;"` |
| holdings look wrong after old imports | holdings are a cache of the replay | `make snapshots` previews the replayed values per day; compare with `core.holdings` |
| no snapshot for a trading day; alert `MarketCloseSkippedPortfolios` | a held ticker had no close for the session | `bash scripts/logs.sh core-service grep market_close_snapshot_skipped`; fill later: `bash scripts/snapshots.sh --portfolio <id> --to <YYYY-MM-DD>` (preview), then add `--confirm` (writes snapshots) |
| alert `TradingDayJobStale` / `DailyJobStale` | a job did not report success | `make health`; `make service-logs S=scheduler`; was the machine awake at the scheduled time? |
| `deleted-user-sweep-core` answers 500 with `all_units_failed: ["organizations_safety_brake"]` | auth reported too many organizations as missing (3.15) | core log `deleted_organization_sweep_refused` (counts); check `AUTH_SERVICE_URL`; if the deletions are real, `python -m scripts.clean_organizations --id <org id>` (preview, then `--confirm`) |
| market-close answered `{"skipped": true, "reason": "market holiday"}` | NYSE closed | expected |
| document stays `uploaded` (or stops at `parsing`, `chunking`, `embedding`) | event still in the outbox (Redis down), ai-service not consuming, or ai-service dead-lettered the entry mid-ingestion | `bash scripts/psql.sh -c "SELECT event_name, created_at, attempts, last_error FROM core.event_outbox WHERE published_at IS NULL ORDER BY created_at;"`; `bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded`; ai-service logs; re-upload the same file after the stuck limit (10 minutes for `uploaded`, 60 for the processing statuses; republish) |
| alert `OutboxBacklog` | durable events waited in `core.event_outbox` for 15 minutes | core logs `outbox_publish_failed` (with `last_error`); is Redis up? The dispatcher publishes them once it is |
| document status never reaches `ready` in core but ai-service finished | `document.status_changed` durable consumer stuck | core logs `durable_event_handler_failed`, `durable_event_dead_lettered`; `bash scripts/redis-cli.sh -n 3 XPENDING stream:document.status_changed workers` |
| upload 422 "The file is not a valid PDF." | bytes do not start with `%PDF-` | the file is not really a PDF |
| upload 409 "A document with this name already exists" | a document with the same ticker, category and file name exists and is not failed | delete it or rename the file |
| public API 401 `invalid_api_key` right after revoking | expected; the cache was overwritten | |
| a revoked key still works for up to 30 s | Redis failed during the revoke, so the old "valid" cache entry was not overwritten (the revoke itself committed) | log `api_key_cache_write_failed` (`action`, `api_key_id`) |
| public API 429 `rate_limit_exceeded` | per-key or per-owner minute limit | `Retry-After`; counters `bash scripts/redis-cli.sh -n 1 --scan --pattern 'ratelimit:*'` |
| WebSocket closes immediately with 4401 | bad or expired token, or deactivated user | make a fresh token |
| WebSocket 4403 | channel not allowed for this user | check the table in 3.16 |
| WebSocket 4429 | more than 20 sockets for this user in this process | close old tabs |
| live notifications missing on one replica | `ws:fanout` listener down | logs `websocket_fanout_listener_failed`, `websocket_send_failed` |
| duplicate notifications | event claim failed open (Redis error) | log `event_claim_failed` |
| `/health/core` 503 | database, Redis or object storage down | the JSON body names the failing dependency |

---

## 7. Demo walkthrough

**This demo writes data.** It creates one portfolio, a few transactions,
an API key and one small document, all owned by a demo user. It does not
touch other users' data. Run it outside the scheduler's busy windows
(09:00-10:30 and 16:55-17:50 New York, about 14:00-15:30 and 21:55-22:50
London). Uploading a document makes ai-service call the embedding API.

Use the demo user from chapter 11 (or any non-admin user you created):

```
BASE=https://localhost:8443
USER_ID=<the demo user's id from /me>
TOKEN=$(bash scripts/token.sh "$USER_ID")
AUTH="Authorization: Bearer $TOKEN"
NOW=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
YDAY=$(python3 -c "from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
json() { python3 -c "import sys, json; print(json.load(sys.stdin)['data']['$1'])"; }
```

**Step 1 (second terminal): open a WebSocket on your notification
channel.** The `websockets` library is installed in the core-service
image, and the container can reach the gateway as `gateway`:

```
docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" -e CHANNEL="user:$USER_ID" \
  core-service python - <<'PY'
import asyncio, os, ssl
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

async def main():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"wss://gateway/ws/{os.environ['CHANNEL']}?token={os.environ['TOKEN']}"
    async with connect(url, ssl=ctx, ping_interval=20) as ws:
        print("connected to", os.environ["CHANNEL"], flush=True)
        try:
            async for message in ws:
                print(message, flush=True)
        except ConnectionClosed:
            pass
        print("closed:", ws.close_code, ws.close_reason, flush=True)

asyncio.run(main())
PY
```

(With `websocat` on the host instead:
`websocat -k "wss://localhost:8443/ws/user:$USER_ID?token=$TOKEN"`.)
Leave it running; notifications from the next steps appear here as
`{"event": "notification", "payload": {...}}`.

Show a refusal too: run the same script with
`CHANNEL="user:00000000-0000-0000-0000-000000000001"`. It prints
`closed: 4403 not authorized for this channel`. With `CHANNEL="foo:bar"`
it prints `4400`.

**Step 2: create a portfolio.**

```
PID=$(curl -k -sS -X POST $BASE/api/v1/portfolios -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "Demo portfolio", "currency": "USD"}' | json id)
echo $PID
```

The WebSocket terminal shows "Portfolio created".

**Step 3: deposit, then buy.**

```
curl -k -sS -X POST $BASE/api/v1/portfolios/$PID/transactions -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"ticker\": \"CASH\", \"transaction_type\": \"deposit\", \"quantity\": \"10000\", \"transaction_date\": \"$NOW\"}"
curl -k -sS -X POST $BASE/api/v1/portfolios/$PID/transactions -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"ticker\": \"AAPL\", \"transaction_type\": \"buy\", \"quantity\": \"10\", \"price\": \"200\", \"transaction_date\": \"$NOW\"}"
```

Both answer 201. Now try to buy more than the cash allows:

```
curl -k -sS -X POST $BASE/api/v1/portfolios/$PID/transactions -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"ticker\": \"AAPL\", \"transaction_type\": \"buy\", \"quantity\": \"100\", \"price\": \"200\", \"transaction_date\": \"$NOW\"}"
```

Expected 422: "Insufficient cash to buy 100 AAPL at 200 on <date>:
required 20000, available cash 8000. Leverage is not allowed -- record a
deposit first."

**Step 4: the ledger protects history.** A sell dated *yesterday*, before
the buy existed:

```
curl -k -sS -X POST $BASE/api/v1/portfolios/$PID/transactions -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"ticker\": \"AAPL\", \"transaction_type\": \"sell\", \"quantity\": \"5\", \"price\": \"210\", \"transaction_date\": \"$YDAY\"}"
```

Expected 422: "... with it, the ledger would take out more AAPL shares
than were held at some date." Say: "Today we do hold 10 shares, so a naive
check would accept it. The replay in date order sees the sell before the
buy."

**Step 5: ledger-derived holdings and cash.**

```
curl -k -sS $BASE/api/v1/portfolios/$PID/holdings -H "$AUTH" | python3 -m json.tool
bash scripts/psql.sh -c "SELECT transaction_date, transaction_type, ticker, quantity, price FROM core.transactions WHERE portfolio_id = '$PID' ORDER BY transaction_date;"
docker compose --env-file .env.docker exec -T core-service python -c "
import httpx
from shared.http_client import internal_headers
r = httpx.get('http://core-service:8000/internal/portfolios/$PID/cash-balance', headers=internal_headers())
print(r.json())"
```

Expected: one holding AAPL, quantity 10, average_cost 200
(`latest_recommendation` null); two ledger rows
(deposit with ticker `CASH`, buy); cash balance 8000.0. Say: "There is no
cash column anywhere. 10000 minus 10 times 200 is computed from the
ledger every time."

**Step 6: idempotency on the internal trade path.** This records one real
buy of 1 share (a write):

```
docker compose --env-file .env.docker exec -T core-service python -c "
import httpx
from shared.http_client import internal_headers
body = {'ticker': 'AAPL', 'transaction_type': 'buy', 'quantity': '1', 'price': '200',
        'transaction_date': '$NOW', 'idempotency_key': 'demo:$PID'}
for attempt in (1, 2):
    r = httpx.post('http://core-service:8000/internal/portfolios/$PID/transactions',
                   json=body, headers=internal_headers())
    print(attempt, r.status_code, r.json())"
```

Both attempts print the same `id`. Count the rows:
`bash scripts/psql.sh -c "SELECT count(*) FROM core.transactions WHERE idempotency_key = 'demo:$PID';"`
prints `1`.

**Step 7: a snapshot.** The market-close job touches every portfolio, so
do not trigger it by hand. Show the stored snapshots of the test
portfolio instead (read-only):

```
OWNER=$(bash scripts/token.sh owner)
PORTFOLIO_ID=d72855ea-2776-41c3-bc77-ce1a4c1435d5
curl -k -sS "$BASE/api/v1/portfolios/$PORTFOLIO_ID/snapshots?start_date=$(python3 -c "from datetime import date, timedelta; print(date.today() - timedelta(days=10))")" \
  -H "Authorization: Bearer $OWNER" | python3 -m json.tool
bash scripts/snapshots.sh
```

The first command lists the last days' `total_value` and `cash_balance`.
`scripts/snapshots.sh` (preview only, no `--confirm`) replays the ledger
for each day, values it at the official closes and prints `ok`, `fix` or
`add` per day with the difference. Say: "If a stored day disagrees with
the ledger by more than a dollar, this is how we find it and fix it."
After 17:30 New York (22:30 London) on a trading day, the demo portfolio
gets its own row; check with
`bash scripts/psql.sh -c "SELECT * FROM core.portfolio_snapshots WHERE portfolio_id = '$PID';"`.

**Step 8: upload a document and watch its status.**

```
printf '# Demo note on AAPL\n\nServices revenue grew in the last quarter.\n' > demo-aapl-note.md
DOC=$(curl -k -sS -X POST $BASE/api/v1/research/documents -H "$AUTH" \
  -F file=@demo-aapl-note.md -F category=news -F ticker=AAPL | json id)
for i in $(seq 1 12); do
  curl -k -sS $BASE/api/v1/research/documents/$DOC -H "$AUTH" | json status
  sleep 5
done
```

Expected: `uploaded`, then `parsing`, `chunking`, `embedding` (some may
pass too fast to see) and finally `ready`. The WebSocket terminal shows
"Document uploaded" and later "Document ready". To see the raw status
events, start the WebSocket script with `CHANNEL="document:$DOC"` before
the status reaches `ready`; it prints `document.status_changed` events.
Show the stored object:
`bash scripts/objectstore.sh ls documents/$USER_ID/`. Upload the same file
again: the answer is 409 "A document with this name already exists"
(nothing new is processed).

**Step 9: an API key and the public API.**

```
KEY=$(curl -k -sS -X POST $BASE/api/v1/api-keys -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "demo key"}' | json key)
echo "$KEY" | cut -c1-8          # amk_....
curl -k -sS $BASE/api/v1/public/portfolios/$PID/holdings -H "X-API-Key: $KEY" | python3 -m json.tool
curl -k -sS -o /dev/null -w 'no key: %{http_code}\n' $BASE/api/v1/public/portfolios/$PID/holdings
bash scripts/psql.sh -c "SELECT name, left(key_hash, 12) AS hash_prefix, rate_limit_per_minute, last_used_at FROM core.api_keys WHERE owner_user_id = '$USER_ID';"
```

Say: "The raw key was shown once. The database only has its SHA-256."

**Step 10: job health.**

```
make health
bash scripts/redis-cli.sh HGETALL core:job_last_success
bash scripts/redis-cli.sh HGETALL core:job_tracked_since
```

`make health` shows each scheduled job's gauge value and the skipped
portfolio count from Prometheus; the first Redis hash is where
core-service keeps the real success times across restarts. A job listed
only in the second hash has not succeeded yet: its gauge holds the time
tracking started, so its stale-job alert still fires (3.12). (Do not call the heartbeat endpoint by
hand: it would record a success that did not happen.)

**Clean-up (optional, deletes the demo data):**
`curl -k -sS -X DELETE $BASE/api/v1/research/documents/$DOC -H "$AUTH"`
and `curl -k -sS -X DELETE $BASE/api/v1/portfolios/$PID -H "$AUTH"` (the
portfolio delete cascades to its transactions and holdings and publishes
`portfolio.deleted`). Revoke the key with `DELETE /api/v1/api-keys/<id>`.

---

## 8. Check your understanding

1. Where is a portfolio's cash balance stored?
2. A user records a buy dated March after a split dated June already
   exists. Why is the holding rebuilt by replay instead of updated in
   place?
3. Three rows share one timestamp: a sell, a split and a buy. In which
   order are they replayed, and why?
4. Why does the no-leverage check look at every later day and not only
   at today?
5. ai-service executes a tier, the HTTP response is lost, and it retries.
   What prevents a double buy?
6. On a trading day one held ticker has no close for the session. What
   does the market-close job do, and how is the day fixed later?
7. A document is still `uploaded` after an hour. What are two possible
   causes and one repair?
8. Why is `document.status_changed` consumed both from a stream and from
   pub/sub?
9. What stops a user from getting 600 public API requests per minute with
   ten keys?
10. A client connects to `/ws/presence:<uuid>` of a stranger. What
    happens?
11. Which Redis structure and which metric make a scheduled job's success
    visible to alerts?

<details>
<summary>Answers</summary>

1. Nowhere. It is computed from the ledger (`compute_cash_balance`) each
   time; snapshots store the value of their day.
2. Applying a back-dated row on top of the current state gives wrong
   quantity and average cost (25 at 64 instead of 30 at 53.33 in the code's
   example). `_sync_holding_with_ledger` replays the ticker's ledger in
   date order and stores that.
3. Buy (phase 0), then split (phase 1), then sell (phase 2): shares in,
   then splits, then shares out, so an outflow never fails because of
   storage order.
4. A back-dated outflow can push a past day below zero even when today is
   fine; the rule is that cash is never negative at any point.
5. The `idempotency_key` `tier:<tier id>`: the second call finds the key
   and returns the original transaction; the portfolio row lock serializes
   concurrent retries.
6. It skips that portfolio, logs `market_close_snapshot_skipped`, sets the
   skipped gauge (alert). Later `bash scripts/snapshots.sh --all --to
   <date>` adds the day (preview, then `--confirm`).
7. The event is still waiting in `core.event_outbox` (Redis has been
   down; the dispatcher publishes it once Redis is back), or ai-service is
   not consuming the stream. Re-uploading the same file 10 minutes after
   the last status change republishes the event.
8. The stream (durable) is for persisting the status and creating one
   notification; pub/sub is for the live push on every replica, where a
   miss is harmless.
9. The per-owner counter `ratelimit:owner:<id>` shares one budget of
   `PUBLIC_API_RATE_LIMIT_PER_MINUTE` (60) across all keys.
10. `authorize_channel` asks auth-service whether they are accepted
    connections; not friends -> accept, then close with 4403.
11. The Redis hash `core:job_last_success` (db 0) and the gauge
    `alphamind_job_last_success_timestamp_seconds{scheduled_job}` on core's
    `/metrics`, fed by `heartbeat.sh` through
    `POST /internal/scheduler/heartbeat/{job}`.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/core-service/main.py` | app, lifespan (schema, bucket, job health, background listeners, outbox dispatcher, Yjs save at shutdown), routers, `/health` |
| `services/core-service/config.py` | core settings (API keys, notification retention, organization sweep brake, failed-document retry, uploads, Yjs) |
| `services/core-service/apikeys.py` | `ApiKey` model, hashing, create, revoke, cache key, the cache writes `mark_revoked_in_cache` and `forget_cached_key`; the public-API middleware: key check, owner state, rate limits, `last_used_at` throttle |
| `services/core-service/api/public.py` | `/api/v1/public` routes and the `/api/v1/api-keys` routes (`apikeys_router`) |
| `services/core-service/api/internal.py` | scheduler, portfolio, stats, tracked-tickers and system document internal routes |
| `services/core-service/api/websocket.py` | JSON WebSocket endpoint and channel authorization, the Yjs endpoint, `recheck_user_sessions` |
| `services/core-service/yjs_relay.py` | collaborative note editing, and the Yjs decode/validate layer (`validator`, `UpdateValidator` worker processes, `replace_text`, `seed_state`) (chapter 25) |
| `services/core-service/websocket_manager.py` | channel registry, fan-out, per-user cap, presence task, `ws:fanout`; token check, close codes, accept-then-close, expiry timer |
| `services/core-service/event_listener.py` | pub/sub and durable handlers, replica claims, `delete_private_user_data`, `delete_organization_content`, `sweep_deleted_users` (with `delete_unreferenced_files`), `sweep_deleted_organizations` |
| `shared/deleted_users.py` | `run_cleanup_steps`, `deleted_user_ids`, `missing_organization_ids`, `run_deleted_user_sweep`, `clean_each`, `sweep_steps` (job answer steps; `brake=True` adds the safety-brake step) (shared with ai) |
| `services/core-service/job_health.py` | job success gauges, the "tracked since" start value for jobs without a success, market-close skip count, and their Redis persistence |
| `services/core-service/outbox.py` | transactional outbox: `enqueue`, `publish_now`, `run_outbox_dispatcher`, table `core.event_outbox` |
| `services/core-service/domains/portfolio/ledger.py` | replay, phases, cash rules, adjustment planning, snapshot day |
| `services/core-service/domains/portfolio/service.py` | portfolios, transactions, holdings, cash, snapshots, jobs, CSV exports |
| `services/core-service/domains/portfolio/model.py`, `schema.py`, `repository.py`, `api.py` | tables, validation, data access, routes and their notification texts |
| `services/core-service/domains/research/*` | notes, comments, documents, moderation, sentiment |
| `services/core-service/domains/notification/*` | notification rows, delivery, `notify_after_commit` |
| `services/core-service/clients/ai_client.py`, `auth_client.py` | calls to the other services |
| `services/core-service/scripts/recompute_snapshots.py` | snapshot check and repair |
| `services/core-service/scripts/clean_organizations.py` | operator cleanup of organizations the sweep's safety brake refused: asks auth which ids are missing, previews, then with `--confirm` runs `delete_organization_content(..., strict=True)` for the missing ones only |
| `services/core-service/scripts/delete_documents.py` | operator delete of documents by id (preview, `--confirm`); per document: delete the row and enqueue `document.deleted` in the outbox in one transaction (`service.delete_document_row`, shared with the API), commit, publish with `outbox.publish_now`, then delete the stored file (a failure is reported as an orphan object) |
| `services/core-service/scripts/backfill_sentiment.py` | fill missing or outdated note/comment sentiment (preview, `--apply`) |
| `services/core-service/scripts/e2e_check.py` | real-environment end-to-end check (`make e2e`) |
| `services/core-service/migrations/` | Alembic `env.py`, `versions/` (`f1160c000001` -> `f1170c000001` -> `f1200c000001` -> `f1250c000001`, head), `script.py.mako` |
| `services/core-service/deps.py` | `get_current_user`, the one `from <= to` check (422) for date-range query parameters (`check_date_range`), `day_start` |
| `shared/market_calendar.py` | NYSE calendar, New York date |
| `shared/events.py` | pub/sub and durable events, event names |
| `shared/redis.py`, `shared/user_state.py` | shared counters, live state, membership cache |
| `infra/scheduler/crontab`, `call-job.sh`, `heartbeat.sh` | the callers of the scheduler endpoints |
| `infra/prometheus/alert_rules.yml` (groups `scheduled_jobs`, `durable_events`) | stale-job, skipped-portfolio and outbox-backlog alerts |
| `scripts/snapshots.sh`, `scripts/health.sh`, `scripts/objectstore.sh` | operator wrappers |

---

## 10. Related chapters

- [10-gateway.md](10-gateway.md): routing of `/api/v1/*` and `/ws`.
- [11-auth-service.md](11-auth-service.md): tokens, roles, the live state
  mirror, organizations, presence.
- [13-ai-service.md](13-ai-service.md) and
  [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md): analyses,
  tiers executed through the internal transaction route, portfolio
  backtests reading snapshots and cash flows.
- [17-ai-rag.md](17-ai-rag.md): what ai-service does with an uploaded
  document.
- [20-postgresql.md](20-postgresql.md), [22-object-storage.md](22-object-storage.md),
  [23-redis.md](23-redis.md): the stores behind this service.
- [24-events.md](24-events.md): pub/sub and Redis Streams in detail.
- [25-realtime-yjs.md](25-realtime-yjs.md): WebSockets and Yjs in depth.
- [26-scheduler-jobs.md](26-scheduler-jobs.md): the cron schedule and job
  scripts.
- [27-observability.md](27-observability.md): metrics and alerts.
- [../API_SPEC.md](../API_SPEC.md) sections 3 to 5, [../ERD.md](../ERD.md)
  part 2 (the `core` schema).
