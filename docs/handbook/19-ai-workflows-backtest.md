# 19. Workflows, tier monitoring and backtests

> A "workflow" in ai-service is a long-running piece of work that runs
> outside a normal request: an analysis, the daily check that turns
> accepted recommendation tiers into real trades, the nightly review that
> scores old recommendations and user decisions, the nightly document
> ingestion, and backtests that replay real trades against a benchmark.
> They live in `services/ai-service/ai/workflows/`. Some are started by
> the scheduler container at fixed New York times, some by an API call
> that returns at once while the work continues in the background. This
> chapter explains each one, the metrics they compute, two small LLM
> features that sit next to them (the decision review and user-content
> sentiment scoring), and how the evaluation layers measure AI quality.

---

## 1. The big picture

```
 time (New York)   scheduler (infra/scheduler/crontab)     ai-service workflow
 ---------------   ------------------------------------    -----------------------------
 09:30 Mon-Fri     market-open -> core-service       ----> analyses (queue)    ch. 16
 17:00 Mon-Fri     tier-monitoring                   ----> run_tier_monitoring_workflow
 17:30 Mon-Fri     market-close -> core-service            (portfolio NAV snapshots)
 02:00 daily       review-run                        ----> run_review_workflow
 02:30 daily       backup.sh                               (backups, chapter 2)
 03:00 daily       rag-ingest                        ----> auto-ingestion      ch. 17
 04:00 daily       logs-cleanup.sh
 04:10 daily       purge-unverified -> auth-service        (unverified accounts)
 04:20 daily       notification-retention -> core-service  (old read notifications)

 on demand (API, background task)
   POST /api/v1/analyses                     ----> run_analysis_workflow
   POST /api/v1/backtests                    ----> run_backtest_workflow
   POST /api/v1/backtests/recommendation-replay -> run_recommendation_replay_workflow
   POST /api/v1/backtests/portfolio          ----> run_portfolio_backtest_workflow
   POST /api/v1/decisions/{id}/review        ----> one LLM call (synchronous)
   POST /internal/sentiment (core-service)   ----> one LLM call (synchronous)
```

London is 5 hours ahead of New York most of the year, so 17:00 New York is
22:00 in London, 02:00 is 07:00, and 03:00 is 08:00. The scheduler runs in
`America/New_York` (the container's `TZ`), so the jobs follow daylight
saving time automatically.

The life of a recommendation tier across workflows:

```
 analysis workflow           user                 tier monitoring (17:00)       core-service
 -----------------           ----                 -----------------------       ------------
 BUY 3 @ 150  -------------> "followed" -------> today's bar low <= 150? -----> POST /internal/
 pending_acceptance          accepted_awaiting     yes: fill = min(open, 150)     portfolios/{id}/
                             _trigger               idempotency_key tier:<id>     transactions
                                                    |                              |
                                                    +-- 2xx: triggered, event      |
                                                    |   tier.executed  <-----------+
                                                    +-- 4xx: execution_failed,
                                                    |   event tier.execution_failed
                                                    +-- 5xx/timeout: stays accepted,
                                                        tried again next trading day
                 window passed (any open status) --> expired
                 system analysis pending > SYSTEM_TIER_ACCEPTANCE_HOURS --> expired
```

---

## 2. Concepts you need

**Scheduler.** A small container running `crond` with the crontab in
`infra/scheduler/crontab`. Each job is a shell script that calls an
internal HTTP route of a service with `curl`, sending the shared
`X-Internal-Token` header (every `/internal/*` route requires it). On
success the script writes a heartbeat (`/scripts/heartbeat.sh`) that feeds
the `alphamind_job_last_success_timestamp_seconds` metric (chapter 26).

**Background task.** `asyncio.create_task(...)` started by an API route.
The route answers immediately (for example `201` with `status:
"pending"`), and the client polls `GET` until the status is `completed` or
`failed`.

**Trading day.** A weekday on which the NYSE is open.
`shared/market_calendar.py::is_trading_day` knows weekends and market
holidays; "today" is computed in New York (`market_today`).

**Daily bar.** One row of prices for one trading day: open, high, low,
close. The tier job only has daily bars, so it knows *that* the price
touched a level today, not *when*.

**Limit order.** An order to buy at or below a price (or sell at or
above). AlphaMind's tiers behave like limit orders.

**Idempotency key.** A unique key sent with a request so that sending the
same request twice has the effect of sending it once. If the answer to the
first attempt is lost, a retry with the same key returns the already
recorded result instead of trading twice.

**Backtest.** Replaying a strategy over past prices to see how it would
have done. AlphaMind's backtests replay *real* events (triggered tiers or
real portfolio values), not hypothetical signals.

**NAV (net asset value).** The total value of a portfolio on one day:
holdings at that day's close plus cash. The market-close job saves one
NAV snapshot per portfolio per trading day in `core.portfolio_snapshots`.

**Benchmark.** What the strategy is compared to. Default `SPY` (an ETF
that tracks the S&P 500); any symbol can be passed, for example `^GSPC`
(the S&P 500 index itself).

**Return, drawdown, Sharpe ratio.** See 3.7. In short: return is the
percentage gain; maximum drawdown is the worst fall from a peak; the
Sharpe ratio is average daily return divided by its volatility, scaled to
a year.

---

## 3. How it works in AlphaMind

### 3.1 The analysis workflow (summary)

`ai/workflows/analysis_workflow.py::run_analysis_workflow` is covered in
full in chapter 16. In short: the analysis is created as `pending`,
waits in the strictly serial in-process queue (`domains/analysis/queue.py`,
one analysis at a time, `ANALYSIS_TIMEOUT_SECONDS` = 2700 s each), turns
`running`, runs the LangGraph graph, saves everything, sizes tiers, and
ends `completed` or `failed` with a `failure_reason`. It publishes
`analysis.started`, `analysis.completed` or `analysis.failed`,
`recommendation.ready`, and `tier.cancelled` for tiers it replaced. At
service start, `fail_stale_analyses_on_startup` fails any analysis left
`pending` or `running` with `service_restart`.

### 3.2 Tier monitoring: when and what

- **When:** 17:00 New York (22:00 London), Monday-Friday:
  `0 17 * * 1-5 /scripts/call-job.sh tier-monitoring`. The script calls
  `POST /internal/scheduler/tier-monitoring` with a 900 s curl limit.
- **Why 17:00:** the market closes at 16:00; the hour of buffer lets the
  data provider publish the final daily bar. The market-close snapshot job
  runs at 17:30, after tier monitoring, so trades executed at 17:00 are in
  the same day's portfolio snapshot.
- **Holidays:** the route (`api/internal.py::trigger_tier_monitoring`)
  checks `is_trading_day(market_today())`; on a closed day it answers 200
  `{"skipped": true, "reason": "non_trading_day", "date": ...}` and logs
  `tier_monitoring_skipped_non_trading_day`.

`ai/workflows/tier_monitoring_workflow.py::run_tier_monitoring_workflow`:

1. Load the ids of all open tiers (`pending_acceptance` and
   `accepted_awaiting_trigger`, every user, `list_open_tier_ids`).
2. For each id, inside its own `try`: re-load the tier with its
   recommendation and analysis (`get_tier_with_details`), lock its status
   row (`refresh(..., with_for_update=True)`) and skip it if it is no longer
   open (counted as `closed_meanwhile`; a newer recommendation may have
   superseded it while the job ran).
3. Decide with `_check_one_tier` (3.3), commit **per tier**, then publish
   the tier's event.
4. Any exception: roll back only this tier, count `execution_failed`, log
   `tier_monitoring.tier_processing_failed`, continue with the next tier.

The result (also logged as `tier_monitoring.run_complete`) is a count per
outcome: `checked`, `triggered`, `expired`, `no_fresh_data`,
`price_lookup_failed`, `still_open`, `pending`, `execution_failed`,
`closed_meanwhile`, `execution_rejected`. Note the
naming: `execution_failed` in this summary counts tiers whose processing
raised an error (they stay open and are retried); `execution_rejected`
counts tiers that got the terminal *status* `execution_failed` (3.4).

**When a run counts as failed.** A job script reports success to the
heartbeat (and so to the stale-job alerts) only on a 2xx answer. The
route answers with `shared/scheduled_jobs.py::job_response`: when a step
attempted at least one unit and every unit failed, the answer is 500 with
the same counts plus `"all_units_failed": [<step>]`, and
`scheduled_job_all_units_failed` is logged as an error. A step with
nothing to do is a success. For tier monitoring the units are the
checked tiers minus `closed_meanwhile` and minus `pending` (tiers still
awaiting acceptance, for which no price check is made;
`_TIER_NOT_ATTEMPTED_OUTCOMES`), and a tier fails when it ends as
`execution_failed`, `no_fresh_data` or `price_lookup_failed`
(`_TIER_FAILED_OUTCOMES` in `api/internal.py`). So a day on which the
price feed failed or gave no fresh bar for every accepted tier is not
recorded as a success, however many tiers are still pending.

### 3.3 Tier monitoring: the decision for one tier

`_check_one_tier(tier, today)`:

1. **Expiry first.** If the recommendation's `window_expires_at` has
   passed, the tier becomes `expired`, whatever its status. A pending tier
   past its window is as dead as an accepted one: the window governs both
   "can still be accepted" and "can still trigger". This step is the
   only place such a tier is closed: a newer recommendation for the same
   ticker ignores tiers whose window has passed (it does not mark them
   `superseded` and does not count them as kept; chapter 16, section
   3.14).
2. **Only accepted tiers can trigger.** A `pending_acceptance` tier of a
   system analysis (`user_id` null) whose recommendation is older than
   `SYSTEM_TIER_ACCEPTANCE_HOURS` (72) becomes `expired`
   (`acceptance_lapsed`; chapter 16, section 3.15). Any other
   `pending_acceptance` tier inside its window (or a tier whose
   recommendation has no window yet) is left alone (`pending`).
3. **Price data.** `get_bars_cache_first(ticker, interval="1d",
   lookback_days=1)` (`ai/tools/market_data_cache.py`, the same cache the
   agents use, so every check leaves a `price_bars` row behind for
   auditing). A provider error with no cached bars to serve instead gives
   `price_lookup_failed` (log `tier_monitoring.price_lookup_failed`,
   warning); the tier stays open. No bar for today's New
   York date (`_today_bar_matches`) gives `no_fresh_data`.
4. **Accepted during today's session?** If `accepted_at` is after 09:30
   New York on the bar's date, today's bar is skipped (`still_open`, log
   `tier_monitoring.accepted_after_bar_open`): a daily bar cannot tell
   whether the low happened before or after the acceptance. The next
   session's bar is checked on the next run.
5. **Bad data.** A non-finite open, high or low (NaN) gives
   `no_fresh_data` (`tier_monitoring.bar_has_non_finite_price`). Without
   this, a NaN compared with `<=` is always false and a real trigger would
   be missed silently.
6. **Trigger rule.**
   - BUY triggers when `bar.low <= target_price`; fill price
     `min(bar.open, target_price)`.
   - SELL triggers when `bar.high >= target_price`; fill price
     `max(bar.open, target_price)`.

   This is how a limit order fills: at the target price, unless the day
   already *opened* beyond it (a gap), in which case at the better open
   price. Using the day's low as a BUY fill would assume the best price of
   the whole day, which is systematically too optimistic. All prices are
   `Decimal`.

### 3.4 Execution into core-service

`_execute_and_mark_triggered` sends the trade to core-service with
`clients/core_client.py::record_transaction`
(`POST {CORE_SERVICE_URL}/internal/portfolios/{portfolio_id}/transactions`):

```
{"transaction_type": "buy" | "sell", "ticker": "AMD",
 "quantity": 3.0, "price": 150.0,
 "transaction_date": "<now, ISO with time>",
 "idempotency_key": "tier:<tier-id>"}
```

The outcome decides the tier's status:

| core-service answer | Tier status | Event | Retried? |
|---|---|---|---|
| success | `triggered`, with `triggered_at` and `triggered_price` | `tier.executed` (price = fill price) | no |
| 4xx business refusal (not 408/429): insufficient cash, not enough shares, conflict, unknown portfolio | `execution_failed`; a line "Tier ... was not executed: core-service refused the order (status: detail)." is appended to the recommendation's `tier_generation_note` | `tier.execution_failed` (reason = that line) | no |
| 5xx, timeout, network error | unchanged (`accepted_awaiting_trigger`), whole tier rolled back | none | yes, next trading day |

Why the idempotency key: if core-service committed the trade but the
answer timed out, the next day's retry with the same `tier:<id>` key gets
the existing transaction back instead of buying twice. Why
`execution_failed` is terminal: a refusal for insufficient cash will be
refused again every day until the window ends.

Why the transaction date carries the time (about 17:00 New York) and not
just the date: a date-only value was stored as midnight UTC, which is the
evening *before* in New York, so the ledger replay put the fill ahead of
the same day's manual buys and refused a later SELL of shares bought that
day.

**Events** (`ai_events.py::tier_event_payload`): `tier_id`, `analysis_id`,
`user_id`, `portfolio_id`, `ticker`, `action` (side), `quantity`, `price`,
`reason`. Other tier events: `tier.cancelled` with reason `superseded`
(analysis workflow or acceptance), `declined` (decision), or
`portfolio_deleted` (the `portfolio.deleted` event handler in
`event_listener.py`, which closes every unfilled tier of the deleted
portfolio with status `expired`).

### 3.5 The review workflow

- **When:** 02:00 New York (07:00 London), every day:
  `0 2 * * * /scripts/call-job.sh review-run` -> `POST /internal/review/run`
  (curl limit 1800 s).
- **Why every day:** recommendations and decisions keep aging on weekends.

`ai/workflows/review_workflow.py::run_review_workflow` runs three passes
and returns `{"recommendations_scored", "recommendations_attempted",
"recommendations_failed", "decisions_evaluated", "decisions_attempted",
"decisions_failed", "agent_metrics_updated"}`.

**1. AI accuracy** (`review_ai_accuracy`), for *every* recommendation at
least `AI_ACCURACY_EVALUATION_DAYS` (30) days old without a metric (up to
200 per run, oldest first), whether or not anybody acted on it:

- return of the ticker from the analysis date to 30 days later
  (`_realized_return`: first to last daily close in the window);
- score 1.0 or 0.0: BUY correct if the return is above 0, SELL if below
  0, HOLD if the absolute return is at most 2% (`_HOLD_FLAT_BAND`);
- one `ai.recommendation_metrics` row (`accuracy_score`, insert ignored if
  already present), and the episodic memory row gets `outcome` and
  `outcome_return` (chapter 18).

Scoring every recommendation avoids survivorship bias: scoring only the
followed ones would hide exactly the calls nobody trusted.

**2. Decision outcomes** (`review_decision_outcomes`), for every
`InvestmentDecision` at each window of 7, 30 and 90 days
(`_DECISION_EVALUATION_WINDOWS`) that has no outcome for that window yet
(up to 200 per window and run, oldest first):

- `strategy_return` = the ticker's return over the window, negated for a
  SELL recommendation (`_directional_return`). The direction is the
  *recommendation's*, also for declined or ignored decisions, so the
  outcome says what following the call would have returned;
- `benchmark_return` = SPY over the same window (`_DEFAULT_BENCHMARK`);
- `alpha` = strategy minus benchmark; `outperformed` = strategy above
  benchmark;
- one `ai.decision_outcomes` row per decision and window, and the event
  `decision.outcome.evaluated`;
- if the ticker's or the benchmark's return is missing (empty price
  series, which the price feed often returns during an outage), nothing is
  stored (log `review.decision_no_price_data`) and the decision is tried
  again on the next run. Storing an outcome with null returns would
  mean the decision is never evaluated again.

Only explicit decisions are scored: silence is not "ignored".

**3. Agent metrics** (`run_agent_metrics_rollup`): for every agent in the
catalog, over all completed and failed runs, the average score and the
success rate (completed divided by attempted), upserted into
`ai.agent_metrics`. This pass is idempotent.

One failing item is logged (`review.recommendation_failed`,
`review.decision_failed`) and skipped; it never aborts the run. An item
counts as failed when it raised an error or had no price data
(`review.no_price_data`, `review.decision_no_price_data`); an outcome
that already existed counts as neither done nor failed.

The route `/internal/review/run` answers through the same scheduled-job
rule as the other jobs (`job_response`, 3.2) with two steps:
`recommendations` (units = `recommendations_attempted`, failed =
`recommendations_failed`) and `decisions` (units = `decisions_attempted`
summed over the three windows, failed = `decisions_failed`). When a step
attempted at least one item and every item failed, the answer is 500
with the counts plus `"all_units_failed": [<step>]`; otherwise 200 with
the counts. The agent metrics pass is not a step. So a night on which
the price feed returned nothing for any pending item is not recorded as
a success. `infra/scheduler/call-job.sh` calls the route with
`curl -sS --fail-with-body`, so a 500 prints the answer body in the
scheduler log and the script exits non-zero without writing the
heartbeat.

### 3.6 Auto-ingestion (summary)

03:00 New York (08:00 London), every day: `call-job.sh rag-ingest` ->
`POST /internal/scheduler/rag-ingest` (curl limit 3600 s, the slowest
job). For every tracked ticker (portfolio holdings and watchlists) it
adds new SEC filings and one news digest per day, then one macro report
per month. Chapter 17, section 3.13, has the details. The answer is
`{"per_ticker": {"tickers_processed", "documents_created",
"failed_tickers", "filings": {...}, "news": {...}}, "macro":
{"documents_created", "failed", "attempted"}}`, where `filings` and
`news` each hold `attempted`, `failed_tickers` and `quota_deferred`
(chapter 26, section 3.5).

Cost and failure rules (`ai/workflows/auto_ingestion_workflow.py`):

- Each new filing costs two HIGHEST-tier calls: the summary and the
  separate records-only call (chapter 18, sections 3.10-3.11). A ticker
  with several new filings therefore spends several of the day's HIGHEST
  requests at 03:00. A records failure does not stop the filing: the
  document is stored and the records can be added later with
  `scripts/backfill_filing_records.py`.
- Filings, news and macro are separate steps. If storing any new filing
  fails, the ticker fails in the filings step
  (`auto_ingestion.filings_not_stored`); its news digest still runs. One
  ticker's failure never stops the other tickers
  (`auto_ingestion.filings_failed`, `auto_ingestion.news_failed`).
- A source that is not configured (empty `SEC_EDGAR_USER_AGENT`,
  `NEWSAPI_API_KEY` or `FRED_API_KEY`) is disabled: its step attempts
  nothing and the run logs `auto_ingestion.source_not_configured` once.
  A NewsAPI quota refusal defers that ticker's news
  (`auto_ingestion.news_quota_deferred`); it is not a failure.
- Each news digest is one NewsAPI request (newest first). It counts in
  the same 100-requests-per-24-hours quota as the agents' `search_news`
  calls, so check `make health` before a batch that follows the
  ingestion.
- The digest stores only news items the relevance classifier confirmed
  as company business or stock market. When the classifier is
  unavailable, nothing is stored for that ticker
  (`auto_ingestion.news_unverified_skipped`).
- A provider outage counts as a failure even though the ingestion
  helpers catch it: a failed SEC listing or download, a fallback filing
  summary, a failed NewsAPI search, an unavailable relevance classifier
  or an unloadable SEC ticker map marks the ticker as failed
  (`auto_ingestion.ticker_source_failed`). "Nothing new" is a success.
  The macro step fails when no FRED indicator could be fetched, its LLM
  call failed or the report could not be stored. Chapter 26, section 3.5,
  lists every source.
- The route answers 500 (`job_response`, 3.2) when every ticker the
  filings step or the news step attempted failed, or when the macro step
  failed (steps `filings`, `news`, `macro`).

### 3.7 Metrics: `ai/tools/indicators.py`

Pure functions, no database, no network, shared by the three backtests and
the review. They form the section "Backtest and performance metrics" of
`ai/tools/indicators.py`, next to the technical indicators. Each returns `None` (not 0.0) when there is too little data,
so "not enough data" never looks like "zero risk".

| Function | Formula | Notes |
|---|---|---|
| `simple_returns(prices)` | `(p[i] - p[i-1]) / p[i-1]` | one return per day; a day whose previous price is 0 is skipped |
| `paired_simple_returns(a, b)` | `simple_returns` of two series, day by day | a day is skipped in both series when either previous value is 0, so strategy and benchmark returns stay aligned for alpha/beta, IR and TE |
| `total_return(prices)` | `(last - first) / first` | |
| `sharpe_ratio(returns)` | `mean(excess) / stdev(excess) x sqrt(252)` | risk-free rate 0 by default; sample standard deviation; `None` below 2 returns or zero volatility |
| `sortino_ratio(returns)` | like Sharpe, but divides by the downside deviation only | |
| `max_drawdown(prices)` | largest `(peak - price) / peak` | positive fraction: 0.23 means a 23% fall |
| `win_rate(trade_returns)` | share of closed trades with a positive return | per trade, not per day |
| `alpha_beta(s, b)` | regression of strategy on benchmark returns: beta = cov/var, alpha = daily intercept x 252 | |
| `information_ratio(s, b)` | `mean(s - b) / stdev(s - b) x sqrt(252)` | |
| `tracking_error(s, b)` | `stdev(s - b) x sqrt(252)` | |

252 is the usual number of trading days in a year (`_TRADING_DAYS_PER_YEAR`).

A worked example: an equity curve 100, 110, 99, 120 has daily returns
+10%, -10%, +21.2%; total return +20%; maximum drawdown (110 - 99) / 110 =
10%.

### 3.8 The three backtests

All three share one table set (`ai.backtests`, `ai.backtest_metrics`,
`ai.benchmark_results`, `ai.benchmarks`; trades and positions are not
stored, because nothing read them), one engine in
`backtest_workflow.py` (`replay_trade_events`, `compute_backtest_metrics`,
`persist_backtest_result`), one status lifecycle (`pending -> running ->
completed | failed`), and one polling contract (`GET
/api/v1/backtests/{id}` and `/metrics`). `strategy_name` tells them apart.
Creates need the `analysis:create` permission; reads and deletes are for
the backtest's own user only (another user's is a 404).

Each backtest runs as a background task in the ai-service process. A
user may have at most `BACKTEST_MAX_ACTIVE_PER_USER` (2, a constant in
`domains/backtest/api.py`) backtests `pending` or `running`; a further
create answers 429 ("You already have 2 backtests pending or running
...", log `backtest_rejected_limit`). The count runs under a
transaction-scoped advisory lock on the user
(`BacktestRepository.count_active_for_user(lock=True)`), held until the
new row is committed, so two concurrent creates cannot both pass the
cap.

**A. Single-ticker backtest** -- `POST /api/v1/backtests`,
`ai/workflows/backtest_workflow.py::run_backtest_workflow`.

Body (`BacktestCreate`): `strategy_name` (any name except the two
reserved for the other kinds, `ai_recommendation_replay` and
`portfolio_snapshot_backtest`, which are refused with 422 so that the
`backtest.*` event's `kind` stays correct), `ticker`, `benchmark_symbol`
(default `SPY`), `start_date`, `end_date`, `initial_capital` (> 0),
optional `portfolio_id`.

- It replays every tier of this ticker that really *triggered* between
  the two dates, at its real fill price and time. Without a portfolio,
  only tiers from the caller's own analyses count; with a portfolio (which
  the caller must be allowed to use), all tiers on that portfolio count,
  system analyses included.
- The route checks first and refuses with 422 if there is no such tier
  ("No triggered recommendation tiers for ..."); it never falls back to a
  hypothetical buy-and-hold.
- The workflow walks the benchmark's trading days. Each trade adjusts cash
  and quantity; SELLs are matched first-in-first-out (FIFO) against earlier
  BUY lots to get per-trade returns for the win rate. Each day's equity is
  cash plus quantity times the last close.
- Cash never goes below zero: when the replayed BUYs need more cash than
  `initial_capital` at any point, the backtest fails with
  `InsufficientCapitalError` ("the replayed buys need N of cash but
  initial_capital is M; run the backtest again with an initial_capital of
  at least N"), instead of computing returns on a negative cash balance.
  The same rule applies to the recommendation replay (B).
- Metrics from the equity curve and the benchmark closes; metrics and
  benchmark result are saved; the backtest is finalized with `ending_capital`, `strategy_return` and
  `benchmark_return`.
- Known gap: a stock split inside the window is
  not adjusted for (fill prices are unadjusted, provider closes are
  adjusted).

**B. Recommendation replay** -- `POST /api/v1/backtests/recommendation-replay`,
`ai/workflows/backtest_workflow.py::run_recommendation_replay_workflow`. Body: `benchmark_symbol`
(default `SPY`), `initial_capital` (default 100000). It replays *all*
triggered tiers of the caller's own analyses, across tickers, in time
order, from the first trigger to today (`strategy_name =
ai_recommendation_replay`). The route stores that window on the backtest
row; the workflow (`_load_replay_trade_events`) then passes `start_date` and
`end_date` to `RecommendationTierRepository.list_triggered_tiers`, so the
replay counts only fills whose `triggered_at` is inside the window (UTC
days, both ends inclusive). A fill that lands after the
window, while the replay waits to run, is left out instead of being
replayed past `end_date`. Open positions are marked to market at each
day's close; the win rate counts closed round trips only (FIFO). A SELL
larger than anything bought in the replay changes cash and quantity but
gives no win-rate observation. Without any triggered tier of the caller:
422.

**C. Portfolio backtest** -- `POST /api/v1/backtests/portfolio`,
`ai/workflows/backtest_workflow.py::run_portfolio_backtest_workflow`. Body: `portfolio_id`,
`benchmark_symbol` (default `SPY`), optional `start_date`/`end_date`.
It reads the portfolio's real daily values from `core.portfolio_snapshots`
(written by the 17:30 market-close job). Leading snapshots with a total
value of 0 or less (a portfolio created before it was funded) are
skipped: it needs at least 2 snapshots in the window, counted from the
first one with a positive value (422 otherwise). Without `start_date` the
backtest starts on that first funded day, and `initial_capital` is that
day's total value (`strategy_name = portfolio_snapshot_backtest`). The
workflow also drops leading aligned days with a value of 0 or less before
the TWR (log `portfolio_backtest.skipped_unfunded_leading_days`) and fails
with a clear error if fewer than 2 days remain.

- Snapshot days are aligned with benchmark days by date; a day without a
  usable benchmark close is dropped from both (and named in the error if
  fewer than 2 aligned days remain; the newest daily bar can be incomplete
  for hours after the close).
- Returns are **time-weighted** (TWR): for each day, external cash flows
  (deposits minus withdrawals, and share adjustments valued at that day's
  close) are removed: `r = (v_curr - flow - v_prev) / v_prev`. The daily
  returns are compounded into a synthetic curve that every metric uses.
  So a deposit in the middle of the window does not count as profit, and
  `ending_capital` answers "how did the investing perform", not "how much
  money is in the account".
- `win_rate` is `None`: a NAV curve has no discrete trades.

**Common robustness rules:** the route commits the `Backtest` row before
starting the task; each workflow marks its own row `running` and then
`completed` or `failed`, and publishes `backtest.completed` or
`backtest.failed`. Non-finite closes are dropped (`closes_by_date`). At
shutdown, `stop_background_backtests` cancels the running tasks and waits
for them before the database engine and clients are closed; a cancelled
backtest stays `running`. At startup, `fail_stale_backtests_on_startup`
fails backtests left `pending` or `running` by a stopped process. `DELETE /api/v1/backtests/{id}` removes
a finished backtest (409 while pending or running) and, after the commit,
publishes `backtest.deleted` (`backtest_id`, `user_id`, `kind`) as a
background task, which notifies the owner.

**The response.** `GET /api/v1/backtests/{id}` returns
`BacktestDetailRead`: the backtest fields (`strategy_name`,
`benchmark_symbol`, dates, `initial_capital`, `ending_capital`,
`strategy_return`, `benchmark_return`, `status`) plus `metrics`
(`sharpe_ratio`, `sortino_ratio`, `drawdown`, `win_rate`) and
`benchmark_result` (`strategy_return`, `benchmark_return`, `alpha`,
`beta`, `information_ratio`, `tracking_error`, `outperformed`).
`GET .../metrics` returns only the four metrics (404 while not computed).

### 3.9 The decision review (LLM)

`POST /api/v1/decisions/{decision_id}/review`
(`domains/decision/service.py::DecisionService.generate_review`) writes a
short coaching review of one past decision. It is pressed by the user, not
automatic, and generates a new review on every call
(`GET .../review` returns the stored one). Re-posting the decision deletes
its reviews together with its outcomes
(`DecisionReviewRepository.delete_for_decision`; log `decision.updated`
with `reviews_removed`), because a review describes the old action.

- It needs an evaluated outcome first (otherwise 422: "has no evaluated
  outcome yet").
- A new review replaces the stored one (the old rows of the decision are
  deleted in the same transaction), so a decision has at most one review.
- One Lite-tier call (`LLMTier.CHEAP`) with: ticker, the AI's
  recommendation, confidence and reasoning, the user's action and note,
  and the outcome (window, strategy return, benchmark return, out- or
  underperformed). The system prompt asks for a plain review and one
  concrete lesson, and says to name a miss plainly instead of softening it.
- The answer must be JSON `{"review_text", "lessons_learned"}`. If the
  LLM fails, the JSON is unusable, or `review_text` is missing, not a
  string or blank, the route answers **503** with `Retry-After: 60` in the
  shared error envelope (`ReviewUnavailableError`, a
  `ServiceUnavailableError`; code `review_unavailable`, message "Review
  generation is temporarily unavailable; try again shortly.",
  log `decision_review.generation_failed` with the error type) and stores
  nothing: a failure is never stored as a review, and the previous review
  (if any) stays readable. `lessons_learned` is kept only when it is a
  string; otherwise it is empty.
- Only the decision's own user may generate or read it (another user's
  decision is a 404); generating needs `analysis:create`.
- A stored review publishes `decision.reviewed` (same payload as
  `decision.recorded`) after the response; core-service turns it into a
  `decision_reviewed` notification for the user ("A new review of your
  decision on T is ready."). A failed generation (503) publishes
  nothing.

### 3.10 User sentiment scoring

`ai/user_sentiment.py::analyze_user_sentiment`, served at
`POST /internal/sentiment` for core-service. After a research note or a
comment is created or its body changes, core-service calls it in the
background (`services/core-service/clients/ai_client.py::analyze_sentiment`) and
stores `sentiment_label`, `sentiment_score`, `sentiment_model` and
`sentiment_analyzed_at` on the row.

- Request `{"text", "kind": "note" | "comment"}`; the text is cut at a
  word boundary to `SENTIMENT_MAX_INPUT_CHARS` (6000).
- One Lite call, temperature 0, JSON mode, at most 128 tokens. The text
  is passed inside `<text>...</text>` and the prompt says to ignore any
  instructions in it (protection against prompt injection).
- Only the numeric `score` is taken from the model, clamped to -1..1. The
  label is derived in code: `>= 0.15` positive, `<= -0.15` negative,
  otherwise neutral. Label and score therefore never disagree.
- An unusable answer is retried once; any LLM failure gives 503 with
  `Retry-After: 60`, and core-service leaves the fields empty (they can be
  filled later by `services/core-service/scripts/backfill_sentiment.py`).
- The text is never logged, only its length.

core-service aggregates the scores at `GET /api/v1/research/sentiment/summary`.

### 3.11 How AI quality is measured: the evaluation layers

There is no single "eval score". Quality is measured at four separate
layers, each answering its own question with its own mechanism, plus a
per-agent rollup:

| Layer | Question it answers | Mechanism | Details |
|---|---|---|---|
| RAG retrieval | Does search find the chunk that holds the answer? | golden set of (question, chunk) pairs; Recall@K and MRR for BM25, vector and fused search; an integration-test floor | chapter 17, section 3.15; chapter 31, section 3.6 |
| Recommendation accuracy | Was the BUY, HOLD or SELL call right in direction? | review pass 1: every recommendation, 30 days later, into `ai.recommendation_metrics` | 3.5 |
| Decision outcome | Did following (or not following) the call pay off against the benchmark? | review pass 2: only explicit decisions, at 7, 30 and 90 days, into `ai.decision_outcomes` | 3.5 |
| Backtest | How did the real triggered trades, or the real portfolio, do over a period against a benchmark? | the three backtests and the metrics in `ai/tools/indicators.py` | 3.7, 3.8 |
| Agent rollup | Is one agent role pulling its weight? | review pass 3: average score and success rate per agent, into `ai.agent_metrics` | 3.5 |

**Why the layers stay separate.** A low recall points at retrieval; a low
accuracy points at reasoning. One blended score would hide which part to
fix. The two review layers also measure different things on purpose:
accuracy scores every recommendation (it measures the AI, including the
calls nobody trusted enough to follow), while decision outcomes score only
what a user explicitly recorded (they measure what the user did with the
AI's advice).

**Where to read the numbers.** `GET /api/v1/analytics/overview` reports
the accuracy of the evaluated recommendations (overall and per ticker)
and per-agent run counts, average score and duration;
`GET /api/v1/analytics/agents` returns the `ai.agent_metrics` rows; a
backtest's metrics come from `GET /api/v1/backtests/{id}`; RAG recall
comes from `scripts/measure_rag_recall.py` (chapter 17, section 3.15).
The script and the integration-test floor
(`tests/integration/test_rag_recall.py`) share one scoring module,
`ai/evaluation/rag_recall.py`: `rank_of`, `Score` (Recall@K and MRR) and
`embed_golden_questions`, which embeds the golden questions with the
`query` purpose, exactly as production searches embed a question.

**Limits of the current measures.**

- The accuracy score is binary (1.0 or 0.0): it is not risk-adjusted and
  not weighted by the confidence, and the HOLD flat band (2%) is a fixed
  constant.
- Debate convergence and Reflection retries are not measured as quality
  signals. `debate_converged` is stored on every recommendation (and
  appears in `GET /api/v1/analytics/export.csv?dataset=recommendations`),
  and every Reflection
  round is stored in `ai.reflections` with `verified` and `retry_agents`,
  but nothing aggregates them into a rate. Whether convergence predicts
  accuracy is therefore not known yet; they are used only one analysis at
  a time (the confidence penalties in chapter 16, section 3.12, and the
  retry routing).

**Tests.** `tests/ai-service/test_rag.py` checks Recall@1/@3 and MRR
against hand-computed values. In `tests/ai-service/test_workflows.py`,
`TestReviewAiAccuracy`, `TestReviewDecisionOutcomes`, `TestReviewRollup`,
`TestBacktestWorkflow` and `TestPortfolioBacktestWorkflow` run the other
layers on synthetic price series from a fake market-data provider.

---

## 4. Configuration

| Setting / constant | Default | File | Meaning |
|---|---|---|---|
| crontab: tier monitoring | `0 17 * * 1-5` | `infra/scheduler/crontab` | 17:00 New York, weekdays |
| crontab: review | `0 2 * * *` | same | 02:00 New York, daily |
| crontab: rag-ingest | `0 3 * * *` | same | 03:00 New York, daily |
| crontab: market-close | `30 17 * * 1-5` | same | snapshots after tier monitoring |
| curl limit tier monitoring | 900 s | `infra/scheduler/call-job.sh` | |
| curl limit review | 1800 s | same | |
| curl limit rag-ingest | 3600 s | same | |
| `AI_ACCURACY_EVALUATION_DAYS` | 30 | `services/ai-service/config.py` | accuracy checkpoint |
| `ANALYSIS_TIMEOUT_SECONDS` | 2700.0 | same | per analysis |
| `SYSTEM_TIER_ACCEPTANCE_HOURS` | 72 | same | pending tier of a system analysis waits this long for acceptance (3.3) |
| `SENTIMENT_MAX_INPUT_CHARS` | 6000 | same | text cut for sentiment |
| `_DECISION_EVALUATION_WINDOWS` | 7, 30, 90 | `ai/workflows/review_workflow.py` | decision outcome windows |
| `_DEFAULT_BENCHMARK` | `SPY` | same | decision outcome benchmark |
| `_HOLD_FLAT_BAND` | 0.02 | same | HOLD correct within +-2% |
| `benchmark_symbol` default | `SPY` | `domains/backtest/schema.py` | backtests; `^GSPC` allowed |
| `initial_capital` default (replay) | 100000.0 | same | |
| `_TRADING_DAYS_PER_YEAR` | 252 | `ai/tools/indicators.py` | annualization |
| `POSITIVE_THRESHOLD` / `NEGATIVE_THRESHOLD` | 0.15 / -0.15 | `ai/user_sentiment.py` | sentiment labels |
| `BACKTEST_MAX_ACTIVE_PER_USER` | 2 | `domains/backtest/api.py` | backtests pending or running per user |
| `_TIER_FAILED_OUTCOMES` | `execution_failed`, `no_fresh_data`, `price_lookup_failed` | `api/internal.py` | tier outcomes counted as failed for the 500 rule |

---

## 5. Design decisions and why

**Tier monitoring once a day, after the close.** Alternative: watch
prices during the day. Reason: the project uses daily bars from a free
provider; one settled bar per day is reliable and cheap.

**Limit-order fill price.** Alternative: fill at the day's low (BUY) or
high (SELL). Reason: that assumes the best price of the day and makes
every result look better than reality.

**Skip today's bar for tiers accepted after the open.** Reason: a daily
bar cannot prove the price crossed *after* acceptance. Missing a fill by
one day is better than inventing one.

**Commit per tier.** Alternative: one commit at the end. Reason: one bad
tier must not undo the correct changes of every other tier in the run.

**Business refusal is terminal, technical failure is retried.** Reason:
retrying "insufficient cash" daily only repeats the refusal; a timeout may
well succeed tomorrow, and the idempotency key makes that retry safe.

**Score every recommendation, but only explicit decisions.** Reason:
recommendation accuracy must include ignored calls (no survivorship
bias); a decision outcome needs a real decision.

**Backtests replay real events.** Alternative: a hypothetical
buy-and-hold from the start date. Reason: the question is how AlphaMind's
real trades did; a hypothetical trade answers a different question. So a
backtest with no real trigger is refused instead of silently simulated.

**Time-weighted returns for portfolios.** Reason: deposits and
withdrawals are not performance; TWR is the standard way to remove them.

**Metrics return `None` on too little data.** Reason: 0.0 would read as a
real "zero" result.

**A job with every unit failed answers 500.** Alternative: always 200
with the counts. Reason: the heartbeat and the stale-job alerts only see
the HTTP status; a run that checked nothing successfully must not look
like a success.

**A failed decision review is a 503, not a stored review.** Alternative:
store a "temporarily unavailable" text as the review. Reason: the GET
route would then serve that text as the review until the user pressed
again; a failure must be reported as a failure.

**A replay never runs on negative cash.** Alternative: let cash go
negative. Reason: returns, Sharpe and drawdown computed on borrowed money
the user never had are meaningless; the error names the capital needed.

**At most two active backtests per user.** Reason: each backtest is a
background task in the same process as the analyses; the cap keeps one
user from filling the process.

**Sentiment label derived in code.** Reason: a model can return
`"positive"` with a score of -0.4; deriving the label from the score makes
that impossible.

---

## 6. When things go wrong

| Symptom | Cause | Diagnose |
|---|---|---|
| an accepted tier never triggers | price did not cross, accepted after the open, no fresh bar, price lookup failed | log `tier_monitoring.accepted_after_bar_open`, `tier_monitoring.price_lookup_failed`; the run's `no_fresh_data` and `price_lookup_failed` counts; `make tiers` |
| tier in `execution_failed` | core-service refused the order | the tier's recommendation `tier_generation_note`; log `tier_monitoring.execution_rejected` |
| `execution_failed` count > 0 in the run summary | technical error while processing; tier stays open | `tier_monitoring.tier_processing_failed` with a traceback |
| tier monitoring did nothing on a weekday | market holiday | the job answer `{"skipped": true, ...}` in the scheduler log |
| alert `TradingDayJobStale` (tier-monitoring) | job has not succeeded for over 100 hours | `bash scripts/logs.sh scheduler grep tier-monitoring`; `make health` |
| alert `DailyJobStale` (review-run, rag-ingest) | daily job has not succeeded for over 26 hours | scheduler log; was the machine awake at the scheduled time? |
| decision has no outcome | younger than 7 days, or no price data for the ticker or the benchmark (retried on the next run) | `GET /api/v1/decisions/{id}/outcome` (404 with "hasn't been evaluated yet"); log `review.decision_no_price_data` (`ticker_missing`, `benchmark_missing`) |
| tier-monitoring, review-run or rag-ingest answers 500 | every attempted unit failed (all accepted tiers without fresh data, with a failed price lookup or failed; all pending recommendations or decisions without price data or failed; every attempted ticker of the filings or the news step failed, including by a provider outage; or the macro step failed; a missing key or a used-up NewsAPI quota is not a failure) | `bash scripts/logs.sh ai-service grep scheduled_job_all_units_failed`; the body's `all_units_failed` names the step |
| no news digest for a ticker | classifier unavailable, company name lookup failed, or no confirmed item | `auto_ingestion.news_unverified_skipped`, `auto_ingestion.no_news_articles` |
| backtest create returns 429 | the user already has 2 backtests pending or running | `backtest_rejected_limit`; wait for one to finish |
| backtest create returns 422 | no triggered tier in the window / fewer than 2 snapshots | the error message names the reason |
| decision review returns 503 | the Lite-tier call failed or its answer was unusable; nothing was stored | log `decision_review.generation_failed`; retry after `Retry-After` |
| backtest create returns 422 with "reserved" | `strategy_name` is `ai_recommendation_replay` or `portfolio_snapshot_backtest` (names of the other two kinds) | choose another name |
| backtest `failed` with `InsufficientCapitalError` | the replayed buys need more cash than `initial_capital` | run it again with the capital the error names |
| backtest `failed` | alignment or price problem | log `backtest_failed`, `recommendation_replay_failed` or `portfolio_backtest_failed` (with a traceback); event `backtest.failed` carries the error text |

---

## 7. Demo walkthrough

Nothing in steps 1-3 writes data. Step 4 creates a backtest row (not
destructive; it can be deleted afterwards).

1. **Show the schedule.**

   ```
   docker compose --env-file .env.docker exec -T scheduler cat /etc/crontabs/root 2>/dev/null || sed -n '1,200p' infra/scheduler/crontab
   ```

   (The second command prints the repository copy.) Say: "Times are New
   York time; 17:00 New York is 22:00 in London."

2. **Show the last tier-monitoring runs from the logs.** The daily log
   files are named by New York date; pass the date of the last trading
   day if today's run has not happened yet:

   ```
   bash scripts/logs.sh scheduler grep tier-monitoring
   bash scripts/logs.sh ai-service grep tier_monitoring.run_complete
   bash scripts/logs.sh scheduler grep tier-monitoring 2026-10-01
   ```

   The scheduler lines show "checking recommendation tiers for triggers
   and expiry", the JSON answer with the counts (or `"skipped": true` on a
   holiday), and "tier-monitoring: done". The ai-service line shows the
   same counts. Explain each count with 3.2.

3. **Show job health and the tier states.**

   ```
   make health
   bash scripts/psql.sh -c "SELECT status, count(*) FROM ai.recommendation_tiers GROUP BY 1 ORDER BY 2 DESC;"
   bash scripts/psql.sh -c "SELECT a.ticker, t.side, t.target_price, t.triggered_price, t.triggered_at FROM ai.recommendation_tiers t JOIN ai.recommendations r ON r.id = t.recommendation_id JOIN ai.analyses a ON a.id = r.analysis_id WHERE t.status = 'triggered' ORDER BY t.triggered_at DESC LIMIT 10;"
   ```

4. **Create a backtest through the API and read its metrics (writes one
   backtest row).** A backtest belongs to its creator, and the route checks
   portfolio access, so use a token of a user who may use the portfolio.
   For the test portfolio that is its owner (an admin token gets 404 for a
   portfolio it does not own or share):

   ```
   TOKEN=$(bash scripts/token.sh owner)
   curl -sk -X POST https://localhost:8443/api/v1/backtests/portfolio \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"portfolio_id": "d72855ea-2776-41c3-bc77-ce1a4c1435d5", "benchmark_symbol": "^GSPC"}' \
     | python3 -m json.tool
   ```

   The answer is `201` with `"status": "pending"` and the resolved dates
   and starting value. Copy the `id`, wait a few seconds, then:

   ```
   curl -sk https://localhost:8443/api/v1/backtests/<backtest-id> \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   curl -sk https://localhost:8443/api/v1/backtests/<backtest-id>/metrics \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   ```

   Point at `strategy_return` against `benchmark_return`, `drawdown` (a
   positive fraction), `sharpe_ratio`, `alpha`/`beta`, and `win_rate:
   null` (a portfolio NAV curve has no discrete trades). Say: "Deposits
   and withdrawals are removed, so this is a time-weighted return."

   The single-ticker version works the same way. Because this body names
   the test portfolio, every triggered AMD tier on that portfolio counts,
   including tiers from system analyses:

   ```
   curl -sk -X POST https://localhost:8443/api/v1/backtests \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"strategy_name": "demo", "ticker": "AMD", "start_date": "2026-09-01", "end_date": "2026-10-01", "initial_capital": 10000, "portfolio_id": "d72855ea-2776-41c3-bc77-ce1a4c1435d5"}'
   ```

   Without a triggered AMD tier in that window the answer is 422 "No
   triggered recommendation tiers for AMD ...": a good moment to explain
   that backtests replay only real fills.

5. **Optional clean-up (deletes the demo backtest).**

   ```
   curl -sk -X DELETE https://localhost:8443/api/v1/backtests/<backtest-id> \
     -H "Authorization: Bearer $TOKEN" -o /dev/null -w "%{http_code}\n"
   ```

   Expected: `204`.

Do not call `/internal/scheduler/tier-monitoring` in a demo: it can execute
real accepted tiers into the portfolio.

---

## 8. Check your understanding

1. Why does tier monitoring run at 17:00 New York and the snapshot job at
   17:30?
2. An accepted BUY tier at 150: today's bar is open 148, low 145. What is
   the fill price?
3. The same tier was accepted at 11:00 New York today. What happens?
4. core-service answers 422 "insufficient cash". What is the tier's
   status, and will it be tried again?
5. core-service times out. What protects against a double trade tomorrow?
6. Why is a HOLD recommendation scored as correct when the ticker moved
   +1.5% in 30 days?
7. Why does a portfolio backtest have no `win_rate`?
8. A user deposits 10,000 into a 100,000 portfolio in the middle of a
   backtest window. Does the backtest show a 10% gain from that?

<details>
<summary>Answers</summary>

1. The final daily bar needs time after the 16:00 close; and trades
   executed by tier monitoring must be in the same day's NAV snapshot.
2. `min(open, target) = min(148, 150) = 148`: the day opened below the
   limit, so the order fills at the open.
3. Today's bar is skipped (`accepted_after_bar_open`), the tier stays
   open, and the next trading day's bar is checked.
4. `execution_failed` (terminal), with the reason appended to the
   recommendation's `tier_generation_note`; it is not retried.
5. The idempotency key `tier:<tier-id>`: core-service returns the already
   recorded transaction instead of recording a second one.
6. HOLD is correct when the absolute 30-day return is at most 2%
   (`_HOLD_FLAT_BAND`).
7. It works on a daily NAV curve, not on discrete trades; there are no
   closed round trips to count.
8. No. The time-weighted return subtracts the day's external flow before
   computing that day's return.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/ai-service/ai/workflows/analysis_workflow.py` | analysis run and persistence (chapter 16) |
| `services/ai-service/ai/workflows/tier_monitoring_workflow.py` | daily trigger/expiry check and execution |
| `services/ai-service/ai/workflows/review_workflow.py` | accuracy, decision outcomes, agent metrics |
| `services/ai-service/ai/workflows/auto_ingestion_workflow.py` | nightly ingestion (chapter 17) |
| `services/ai-service/ai/workflows/backtest_workflow.py` | all three backtests: single-ticker replay and replay of all the user's fills (one runner, `_replay_backtest`), the snapshot-based TWR backtest; shared helpers |
| `services/ai-service/ai/tools/indicators.py` | backtest and performance metrics: return, drawdown, Sharpe, Sortino, alpha/beta, IR, TE, win rate (the technical indicators are in chapter 15) |
| `services/ai-service/api/internal.py` | scheduler routes, `/internal/sentiment` |
| `services/ai-service/domains/backtest/` | backtest API, schemas, repository, models |
| `services/ai-service/domains/decision/service.py` | decisions and the decision review |
| `services/ai-service/ai/user_sentiment.py` | note/comment sentiment |
| `services/ai-service/clients/core_client.py` | `record_transaction`, snapshots, cash flows |
| `services/ai-service/ai_events.py` | event payloads (`tier.*`, `backtest.*`, `decision.*`) |
| `infra/scheduler/crontab`, `infra/scheduler/*.sh` | schedule and job scripts |
| `shared/market_calendar.py` | trading days in New York |

---

## 10. Related chapters

- [16. The analysis pipeline](16-ai-analysis-pipeline.md) (tiers, decisions)
- [17. Retrieval-augmented generation](17-ai-rag.md) (auto-ingestion)
- [18. Knowledge graph and memory](18-ai-knowledge-memory.md) (episodic outcomes)
- [12. core-service](12-core-service.md) (transactions, snapshots, notes)
- [24. Events between services](24-events.md)
- [26. The scheduler and its jobs](26-scheduler-jobs.md)
- [27. Logs, metrics, alerts and dashboards](27-observability.md)
- [00. Index](00-index.md), section 8 (open items, including the calibration of the confidence rules)
- [31. Testing and code quality](31-testing-quality.md) (the RAG recall gate)
- [../API_SPEC.md](../API_SPEC.md), [../ERD.md](../ERD.md)
