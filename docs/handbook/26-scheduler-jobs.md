# 26. The scheduler and its jobs

> Some work must happen at fixed times, without a user clicking anything:
> analyses at the market open, executing triggered recommendation tiers
> after the close, the daily portfolio value snapshot, the nightly review
> of past recommendations, fetching new SEC filings and news, backups,
> log cleanup, deleting accounts that never verified their email,
> deleting old read notifications, and the nightly sweeps that remove
> what is left of deleted accounts and organizations. AlphaMind runs all of this from one small container,
> `scheduler`, which runs `cron` in New York time and calls internal HTTP
> endpoints of the services (or runs a shell script for backups and logs).
> Every successful job reports a heartbeat, and two alert rules fire when a
> job has not succeeded for too long (section 3.8). A run in which every
> unit of work failed (every ticker, every tier, every analysis request) is
> not a success: the service answers 500 and no heartbeat is sent.
> The cloud host keeps the scheduler stopped, so it runs no scheduled
> job and makes no automatic backup (section 3.11).

## 1. The big picture

```
            scheduler container (alpine, TZ=America/New_York)
  +----------------------------------------------------------------+
  | entrypoint.sh (PID 1) -> writes /scripts/env.sh, starts crond   |
  | crond reads /etc/crontabs/root (infra/scheduler/crontab)         |
  |                                                                  |
  |  09:30 Mon-Fri  call-job.sh market-open     --POST--> core /internal/scheduler/market-open
  |  17:00 Mon-Fri  call-job.sh tier-monitoring --POST--> ai /internal/scheduler/tier-monitoring
  |  17:30 Mon-Fri  call-job.sh market-close    --POST--> core /internal/scheduler/market-close
  |  02:00 daily    call-job.sh review-run      --POST--> ai /internal/review/run
  |  02:30 daily    backup.sh        (pg_dump, Qdrant snapshots, S3 object copy, avatars tar)
  |  03:05 daily    call-job.sh rag-ingest      --POST--> ai /internal/scheduler/rag-ingest
  |  04:00 daily    logs-cleanup.sh  (find ./logs -mtime +14 -delete)
  |  04:10 daily    call-job.sh purge-unverified --POST--> auth /internal/scheduler/purge-unverified
  |  04:20 daily    call-job.sh notification-retention --POST--> core /internal/scheduler/notification-retention
  |  04:30 daily    call-job.sh deleted-user-sweep-core --POST--> core /internal/scheduler/deleted-user-sweep
  |  04:40 daily    call-job.sh deleted-user-sweep-ai   --POST--> ai /internal/scheduler/deleted-user-sweep
  |                                                                  |
  |  on success: heartbeat.sh <job> --POST--> core /internal/scheduler/heartbeat/<job>
  +----------------------------------------------------------------+
        job output -> /proc/1/fd/1,2 -> docker logs -> vector -> ./logs/scheduler/<date>.log

  core-service: heartbeat -> Redis hash core:job_last_success + gauge
                alphamind_job_last_success_timestamp_seconds{scheduled_job}
  Prometheus scrapes core /metrics
                -> alert rules DailyJobStale / TradingDayJobStale
                   (filter on scheduled_job, see section 3.8)
```

The scheduler does no business logic itself. Each HTTP job is a `curl`
call; the services do the work. This keeps the logic in Python (tested,
typed) and the scheduler trivial.

## 2. Concepts you need

| Term | Meaning here |
|---|---|
| cron | A Unix program that runs commands at times written in a `crontab` file. Each line: `minute hour day-of-month month day-of-week command`. `1-5` in the last time field means Monday to Friday. |
| crond / dcron | The cron daemon. The image installs Alpine's `dcron` and runs `crond -f -l 2` (foreground, log level 2). |
| TZ | The time zone environment variable. With `TZ=America/New_York`, cron reads the crontab in New York time, including daylight-saving changes. |
| Internal endpoint | A route under `/internal/*`. It needs the `X-Internal-Token` header (`INTERNAL_API_TOKEN`), and the gateway returns 404 for it, so it is reachable only inside the Docker network. |
| Heartbeat | A "I succeeded" signal sent after a job. Used to detect jobs that silently stopped running. |
| Gauge | A Prometheus metric that holds one current value (it can go up and down). |
| Trading day | A weekday that is not an NYSE holiday (`shared/market_calendar.py`). |
| Busy window | A time range in which a scheduled job runs and the operator scripts refuse restarts (`scripts/lib.sh`). |
| NAV snapshot | Net asset value of a portfolio for one day (cash + holdings at the close), stored in `core.portfolio_snapshots`. |
| Tier | One planned buy or sell level of a recommendation; see [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md). |

New York and London: London is usually 5 hours ahead of New York. For
about two weeks a year (between the US and UK daylight-saving changes in
March and in late October / early November) the gap is 4 hours. The times
below use the usual 5 hours.

## 3. How it works in AlphaMind

### 3.1 The image: `infra/scheduler/Dockerfile`

- Base `alpine:3.20`.
- Packages: `dcron` (cron), `curl` (HTTP jobs), `tzdata` (time zones),
  `jq` (parses Qdrant responses, writes `manifest.json`),
  `postgresql16-client` (`pg_dump`, `pg_restore`, `psql`; its major version
  must match the `postgres:16-alpine` server, because `pg_dump` refuses to
  dump a newer server).
- `ENV TZ=America/New_York`.
- Copies `crontab` to `/etc/crontabs/root`, `entrypoint.sh` to `/`, and the
  job scripts to `/scripts/` (made executable).
- Installs `rclone` (with `dcron`, `curl`, `tzdata`, `jq` and the
  PostgreSQL 16 client) and copies `s3-remote.sh`, which defines the
  rclone remote `objectstore:` from the `S3_*` settings.
- The mount points `/backups` and `/avatar-data` are NOT created in the image on
  purpose: they must come from compose mounts, and `backup.sh` refuses to
  run (or skips avatars) when they are missing, instead of backing up an
  empty directory.

### 3.2 `entrypoint.sh` and `/scripts/env.sh`

cron does not pass the container's environment to the jobs it starts. So
`entrypoint.sh` writes the needed variables to `/scripts/env.sh`, and every
job script starts with `. /scripts/env.sh`.

What it writes (`export NAME='value'`, single-quoted with escaping so any
character survives):

- `CORE_SERVICE_URL` (default `http://core-service:8000`),
  `AI_SERVICE_URL` (default `http://ai-service:8000`) and
  `AUTH_SERVICE_URL` (default `http://auth-service:8000`);
- if set: `INTERNAL_API_TOKEN`, `POSTGRES_HOST`, `POSTGRES_PORT`,
  `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `QDRANT_URL`,
  `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_API_KEY`, `S3_ENDPOINT_URL`,
  `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` (the backup reads object
  storage through its S3 API), `AVATAR_DATA_DIR`, `BACKUP_DIR`,
  `BACKUP_RETENTION_DAYS`, `TZ`.

The file is written with `umask 077` and `chmod 600`: only root can read it,
because it holds secrets. `env.sh` is generated at container start; it is
not a file in the repository. If `INTERNAL_API_TOKEN` is empty, the
entrypoint prints a warning: the jobs then only work while the services run
with `APP_ENV=development/test` (which accept an empty token).

When the container is started with arguments, the entrypoint writes
`env.sh` the same way and then runs those arguments with `exec "$@"`
instead of cron. That runs one job by hand, for example on the cloud
host where the scheduler stays stopped (section 3.11):
`docker compose --env-file .env.docker run --rm --no-deps scheduler
/scripts/call-job.sh deleted-user-sweep-core`.

Without arguments it starts `crond -f -l 2 &` as a **child** and waits for it. It does
not `exec` crond: dcron calls `setpgid()` at startup, which fails with
"Operation not permitted" when it is PID 1 (a session leader), so crond
would exit and the container would restart in a loop.
The script traps `TERM`/`INT` and forwards them to crond, so
`docker compose stop` stops it cleanly.

Job output goes to `/proc/1/fd/1` and `/proc/1/fd/2`, the stdout/stderr of
PID 1 (the entrypoint). So `docker compose logs scheduler` shows every job
line, and Vector copies them to `./logs/scheduler/<date>.log` (see
[27-observability.md](27-observability.md)).

### 3.3 The crontab: every job

From `infra/scheduler/crontab`:

| Job | Cron line | New York | London (usual) | Days | Command | Target |
|---|---|---|---|---|---|---|
| market-open | `30 9 * * 1-5` | 09:30 | 14:30 | Mon-Fri | `call-job.sh market-open` | core `POST /internal/scheduler/market-open` |
| tier-monitoring | `0 17 * * 1-5` | 17:00 | 22:00 | Mon-Fri | `call-job.sh tier-monitoring` | ai `POST /internal/scheduler/tier-monitoring` |
| market-close | `30 17 * * 1-5` | 17:30 | 22:30 | Mon-Fri | `call-job.sh market-close` | core `POST /internal/scheduler/market-close` |
| review-run | `0 2 * * *` | 02:00 | 07:00 | every day | `call-job.sh review-run` | ai `POST /internal/review/run` |
| backup | `30 2 * * *` | 02:30 | 07:30 | every day | `backup.sh` | local: pg_dump, Qdrant, S3 object copy, avatars tar |
| rag-ingest | `5 3 * * *` | 03:05 | 08:05 | every day | `call-job.sh rag-ingest` | ai `POST /internal/scheduler/rag-ingest` |
| logs-cleanup | `0 4 * * *` | 04:00 | 09:00 | every day | `logs-cleanup.sh` | local: `./logs` |
| purge-unverified | `10 4 * * *` | 04:10 | 09:10 | every day | `call-job.sh purge-unverified` | auth `POST /internal/scheduler/purge-unverified` |
| notification-retention | `20 4 * * *` | 04:20 | 09:20 | every day | `call-job.sh notification-retention` | core `POST /internal/scheduler/notification-retention` |
| deleted-user-sweep-core | `30 4 * * *` | 04:30 | 09:30 | every day | `call-job.sh deleted-user-sweep-core` | core `POST /internal/scheduler/deleted-user-sweep` |
| deleted-user-sweep-ai | `40 4 * * *` | 04:40 | 09:40 | every day | `call-job.sh deleted-user-sweep-ai` | ai `POST /internal/scheduler/deleted-user-sweep` |

`restore.sh` is copied into the image but has no cron line. It is run by
hand only (see [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md)).

Order of the night: review (02:00) after the previous day's market jobs;
backup (02:30) in the quietest window, between review and ingestion, when
no job writes documents; rag-ingest (03:05, five minutes after Google's
daily quota reset at midnight Pacific, so keys benched until then are
free again); logs-cleanup (04:00), after
New York midnight, so every file it looks at is closed; then the two
retention jobs, purge-unverified (04:10) and notification-retention
(04:20). They run after the backup on purpose: what they delete is still
in that night's backup. Last, the two deleted-user sweeps, core (04:30)
and ai (04:40): after purge-unverified, so an account purged tonight is
swept tonight, and long after the Google quota reset (03:00) and
rag-ingest (03:05). They spend no LLM quota.

### 3.4 The HTTP job script: `call-job.sh`

The nine HTTP jobs (market-open, market-close, tier-monitoring,
review-run, rag-ingest, purge-unverified, notification-retention,
deleted-user-sweep-core, deleted-user-sweep-ai) run
through one script, `call-job.sh <job>`. A
`case` on the job name sets the route (`url`), the time limit
(`max_time`) and the log message; an unknown name exits with code 2.
Then every job runs the same request:

```sh
echo "[$(date)] ${job}: ${message}"
if curl -sS --fail-with-body --max-time "$max_time" -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "X-Internal-Token: ${INTERNAL_API_TOKEN:-}"; then
    echo
    echo "[$(date)] ${job}: done"
    /scripts/heartbeat.sh "$job"
else
    rc=$?
    echo "[$(date)] ${job}: request FAILED (curl exit code $rc)" >&2
    exit "$rc"
fi
```

- `-sS`: no progress bar, but errors are printed.
- `--fail-with-body`: an HTTP error status (400 or higher) makes curl
  exit with code 22, so the script exits non-zero, prints the reason and
  sends no heartbeat. It also prints the answer body on an error, so the
  counts of an "every unit failed" 500 (section 3.5) appear in the log.
- `--max-time`: the time limit of the whole request. The response body (the
  service's JSON summary) is printed into the log.
- The heartbeat is sent only after success.

Time limits (`--max-time`):

| Job | Limit | Why |
|---|---|---|
| market-open | 600 s | one ai-service create call (up to 15 s each) per portfolio and ticker |
| market-close | 600 s | recomputes every portfolio snapshot inline |
| tier-monitoring | 900 s | checks every open tier inline |
| review-run | 1800 s | runs the whole review workflow inline |
| rag-ingest | 3600 s | fetches filings and news for every tracked ticker (slowest job) |
| purge-unverified | 300 s | deletes the candidate accounts one by one (normally a handful) |
| notification-retention | 300 s | deletes old read notifications in batches of 1000 |
| deleted-user-sweep-core, deleted-user-sweep-ai | 900 s each | one cleanup per deleted account that still owns data (normally none or a handful) |
| heartbeat | 10 s | one small POST |

When curl hits its limit, the script reports a failure and sends no
heartbeat. Whether the service then finishes the request on its own is not
controlled by the script.

**When a run counts as failed: `shared/scheduled_jobs.py`.** A service
can do its work "successfully" in the HTTP sense while every single unit
of work failed (every ticker's ingestion, every tier check, every analysis
request). Such a run must not send a heartbeat, or the stale-job alerts
would never fire for a job that always fails. So the scheduler routes of
market-open, tier-monitoring, review-run, rag-ingest, purge-unverified,
notification-retention and the two deleted-user sweeps build their answer
with
`job_response(job, result, steps={step: (units, failed)})`:

- For every step, `units` is how many units were attempted and `failed`
  how many failed. A step "failed completely" when
  `all_units_failed(units, failed)`: `units > 0 and failed >= units`.
- If any step failed completely, the answer is **500**
  (`ALL_UNITS_FAILED_STATUS`) with the same body plus
  `"all_units_failed": [<step names>]`, and the error
  `scheduled_job_all_units_failed` (with the counts) is logged.
- Otherwise the answer is 200 with the body.
- A step with zero units is a success. So an empty run (no tracked
  tickers, no open tiers, no opted-in portfolios) and a non-trading-day
  skip still answer 200 and send the heartbeat.

| Job | Steps and units |
|---|---|
| market-open | `analyses`: requests attempted, requests that failed (ai-service unreachable or refused) |
| tier-monitoring | `tiers`: `checked - closed_meanwhile - pending` (a tier still awaiting acceptance is not checked, so it is not a unit); failed = `execution_failed + no_fresh_data + price_lookup_failed` (a price feed that fails or returns nothing for every accepted tier counts as a failure) |
| review-run | `recommendations`: recommendations due for scoring; failed = no usable price history, or an error. `decisions`: (decision, window) pairs due for evaluation; failed = no ticker or benchmark price, or an error. The agent metrics rollup is not a step |
| rag-ingest | `filings` and `news`: one unit per ticker the step attempted, failed when the ticker's step raised or one of its sources failed ("nothing new" is a success); a source that is not configured gives the step 0 units, and a ticker whose news hit the NewsAPI quota is deferred, not a unit; `macro`: 1 unit (0 when `FRED_API_KEY` is empty), failed when no FRED indicator could be fetched, the report's LLM call failed, or storing the report failed. So a failed macro step alone makes the run a 500; a month whose report already exists is a success |
| purge-unverified | `accounts`: accounts the job tried to delete (deleted + failed); failed = the deletion raised (rolled back, logged `unverified_account_purge_failed`). An account that was verified or signed in meanwhile, and the last active admin, are skipped, not units. `deleted_account_leftovers`: deleted accounts that still had sessions, open sent invitations, memberships or owned organizations; failed = that account's cleanup raised (rolled back, logged `deleted_account_leftovers_failed`). `deleted_account_leftovers_lookup`: 1 unit, failed when the candidate query raised (`deleted_account_leftovers_lookup_failed`) |
| notification-retention | `batches`: delete batches attempted (one more than the full batches, the last one finds fewer rows than the batch size); failed = the batch raised (rolled back, logged `notification_retention_failed`; the run stops there). So only a run whose very first batch fails is a 500 |
| deleted-user-sweep-core, deleted-user-sweep-ai | `users`: deleted accounts that still own data; failed = at least one cleanup step failed for that user (logged `deleted_user_sweep_item_failed`; the others still run). `users_lookup`: 1 unit, failed when auth could not be asked or the service's own candidate query failed (`deleted_user_sweep_candidates_failed`); nothing is deleted then. Core only: the same two for `organizations` (`organizations_lookup`: auth could not be asked or the organization candidate query failed, `deleted_organization_sweep_candidates_failed`), plus `organizations_safety_brake`, failed when too many organizations are missing and nothing was deleted. The organization part runs even if the user part failed |

market-close does not use it: its partial failures have their own gauge
and alert, `alphamind_market_close_portfolios_skipped`.

### 3.5 What each endpoint does in the services

**market-open** (`services/core-service/api/internal.py::market_open`):

1. `today = market_today()` (date in New York).
2. Not a trading day -> `200 {"skipped": true, "reason": "weekend" | "market holiday", "date": ..., "analyses_requested": 0}` and a log line `scheduled_job_skipped`.
3. Otherwise `PortfolioService.run_market_open_job()`: for every portfolio
   with `auto_analyze_at_open=True` whose owner is active, one ai-service
   analysis request per held ticker (`ai_client.request_analysis(ticker, portfolio_id,
   user_id=owner_id)`), at most 10 at once (`gather_bounded(limit=10)`).
   The analysis belongs to the portfolio owner, so they get the
   notifications. A failed request is logged
   (`market_open_analysis_request_failed`) and does not stop the others.
   A portfolio whose owner's live state
   (`shared/user_state.get_user_state`) is inactive (deactivated or
   deleted) is skipped with log `market_open_owner_inactive_skipped`; an
   unknown state (`None`) does not skip (fail open).
   Log `market_open_job_completed` (`requests_attempted`,
   `requests_failed`). The job returns `MarketOpenResult(requested,
   failed)`. Answer: `{"skipped": false, "date": ..., "analyses_requested":
   N, "analyses_failed": F}`, with status 500 when N > 0 and every request
   failed (`job_response`, step `analyses`).

The analyses then run in ai-service's serial queue (one at a time, usually
about 1.5 to 4 minutes each, longer when a new filing must be summarised
first; hard limit 45 minutes, `ANALYSIS_TIMEOUT_SECONDS`), so a 15-ticker
market-open batch keeps ai-service busy for about 30 to 40 minutes. An
analysis whose owner was deactivated after it was queued is not run: the
queue checks the owner again before it starts and marks the job failed
(see [13-ai-service.md](13-ai-service.md)).

**tier-monitoring** (`services/ai-service/api/internal.py::trigger_tier_monitoring`):

1. Not a trading day -> `200 {"skipped": true, "reason": "non_trading_day", "date": ...}`, log `tier_monitoring_skipped_non_trading_day`.
2. Otherwise `run_tier_monitoring_workflow()`: for every open tier
   (pending acceptance or accepted awaiting trigger), re-read it under a row
   lock, check today's high/low against the target price and the tier's
   window, and then trigger (execute the trade into the portfolio through
   core-service), expire, or leave it open. A tier expires when its window
   has passed, or when it is a pending tier of a system analysis that was
   not accepted within `SYSTEM_TIER_ACCEPTANCE_HOURS` (72). Each tier is
   committed on its own and its event (`tier.executed`, `tier.execution_failed`) is
   published after its commit. Log `tier_monitoring.run_complete` with
   counts: `checked, triggered, expired, no_fresh_data,
   price_lookup_failed, still_open, pending, execution_failed,
   closed_meanwhile, execution_rejected`. `pending` counts tiers still
   awaiting acceptance (no price check is made for them);
   `price_lookup_failed` counts accepted tiers whose price lookup raised a
   provider error with no cached bars to fall back on (log
   `tier_monitoring.price_lookup_failed`, warning). The answer is these
   counts, with status 500 when every attempted tier (checked minus
   `closed_meanwhile` and `pending`) failed, had no fresh price or had a
   failed price lookup (`job_response`, step `tiers`). Details in
   [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md).
3. The skip reason here is `non_trading_day`, while core's two jobs say
   `weekend` or `market holiday` (the wording is not unified yet).

**market-close** (`services/core-service/api/internal.py::market_close`):

1. Not a trading day -> `200 {"skipped": true, "reason": ..., "date": ..., "portfolios_processed": 0}`.
2. Otherwise `PortfolioService.run_market_close_job(today)`: first work
   out every portfolio's shares and cash from its ledger, using only the
   rows dated at or before today's 17:30 New York cutoff
   (`positions_at_cutoff`, the same rule as
   `scripts/recompute_snapshots.py`; a row dated later counts in the next
   day's snapshot). Then fetch the daily closes of every
   distinct ticker from ai-service concurrently (`gather_bounded(limit=20)`),
   then write one NAV snapshot per portfolio for `today`. A portfolio gets a
   snapshot only when **every** held ticker has a close dated today;
   otherwise it is skipped and logged (`market_close_snapshot_skipped`),
   instead of storing a wrong value. (Without this rule, a day whose newest
   bar is a day old repeats the previous day's value, and a day with no
   price at all is valued at cost.) Log `market_close_job_completed`.
3. Sets the gauge `alphamind_market_close_portfolios_skipped` to the
   skipped count and stores it in Redis (`core:market_close_skipped`,
   `record_market_close_skipped`), so a core-service restart keeps it.
   Answer: `{"skipped": false, "date": ..., "portfolios_processed": W, "portfolios_skipped": S}`.

A skipped day can be filled later with
`services/core-service/scripts/recompute_snapshots.py` (operator wrapper:
`bash scripts/snapshots.sh --portfolio=<id> --to <date>` to preview, then
the same with `--confirm`). `--to` matters: by default the script stops at
the last stored snapshot, so it would not add the skipped day.

**review-run** (`services/ai-service/api/internal.py::trigger_review` ->
`ai/workflows/review_workflow.py::run_review_workflow`): three passes,
`review_ai_accuracy()` (one `ai.recommendation_metrics` row per
recommendation at a fixed checkpoint), `review_decision_outcomes()` (one
`ai.decision_outcomes` row per decision and evaluation window, each new
row publishes `decision.outcome.evaluated`; a missing ticker or benchmark
price leaves the window for the next run, log
`review.decision_no_price_data`) and `run_agent_metrics_rollup()`. Answer:
`{"recommendations_scored", "recommendations_attempted",
"recommendations_failed", "decisions_evaluated", "decisions_attempted",
"decisions_failed", "agent_metrics_updated"}`, with status 500 when every
attempted recommendation or every attempted decision window failed
(`job_response`, steps `recommendations` and `decisions`). An item that
raises an error is logged (`review.recommendation_failed`,
`review.decision_failed`) and the others go on. An item without price
data (`review.no_price_data`, `review.decision_no_price_data`) also counts
as failed; it writes no row, so the next run tries it again.
It runs every day, because recommendations and decisions keep aging on
weekends. Rows are created only if absent, so a second run on the same day
writes nothing new.

**rag-ingest** (`trigger_rag_ingest`): `run_auto_ingestion_workflow()`
(for every tracked ticker from core's `GET /internal/tracked-tickers`, i.e.
portfolio holdings and watchlists: new SEC EDGAR 10-K/10-Q filings and a
NewsAPI news digest) and `run_macro_auto_ingestion_workflow()` (macro
research report). The macro step is a separate function because it is not
ticker-scoped and its sources (Fed, BLS, BEA) are structured numbers, not
ready-made text, so it needs an LLM call to turn them into a report; it
runs right after the per-ticker step in the same call, not on its own
schedule.

`GET /internal/tracked-tickers` returns the sorted union of every
portfolio's holdings and every watchlist's tickers, grouped by owner,
leaving out owners whose live state is inactive (deactivated or deleted;
one `get_user_state` per owner, cached in Redis; log
`tracked_tickers_inactive_owners_skipped` with the number of owners). An
owner whose state is unknown (auth and Redis both down) is kept.

The run has three steps, each judged on its own: **filings** (one unit
per ticker), **news** (one unit per ticker) and **macro** (one unit). The
rule of every scheduled job applies to each step: the job answers 500
only when a step attempted at least one item and every attempted item
failed. Three outcomes are kept apart:

- **Disabled source.** A source whose setting is empty is not configured:
  `SEC_EDGAR_USER_AGENT` (filings), `NEWSAPI_API_KEY` (news) or
  `FRED_API_KEY` (macro). Its step attempts nothing (0 units), so it can
  never fail the run, and the run logs `auto_ingestion.source_not_configured`
  (with `source` and `setting`) once. `.env.example` leaves these keys
  optional, so a fresh setup gets a 200 and a heartbeat.
- **Quota deferral.** NewsAPI refusing a request because the daily request
  limit is used up (HTTP 429 or the codes `rateLimited`/`apiKeyExhausted`,
  and the cooldown that follows; `ProviderQuotaError`) is a planned limit
  shared with the analyses, not an outage. The ticker's news is deferred:
  it is listed in `news.quota_deferred`, is not counted as an attempted
  unit, and the run logs `auto_ingestion.news_quota_deferred` once with
  the tickers. The next run fetches the last 28 days again, so nothing is
  lost.
- **Failure.** Any other error, or a reported failed source (below). An
  LLM that is out of quota (the news relevance classifier, the filing
  summary, the macro narrative) is a failure, not a deferral: only the
  NewsAPI quota defers. The ticker is retried the next night.

Per ticker and step, an error (malformed answer, network error, a
core-service call that fails) is logged (`auto_ingestion.filings_failed`
or `auto_ingestion.news_failed`) and the run goes on; the failed ticker
gets another chance tomorrow. If some filings of a ticker could not be
stored, the others are stored, and the ticker counts as failed in the
filings step (`auto_ingestion.filings_not_stored`). The news step of the
same ticker still runs.

The ingestion helpers catch provider errors themselves (they return "no
document" so that an analysis that calls them is not interrupted). To keep
such an outage visible, each helper reports a failed source
(`ai/ingestion/digests.py::report_source_failure`), and the
workflow collects the reports of one ticker and step with
`track_source_failures()`. A ticker with at least one failed source counts
as failed in that step, even when some of its documents were stored (log
`auto_ingestion.ticker_source_failed` with `step` and the source names).
The failed sources are:

| Source | Step | When |
|---|---|---|
| `sec_edgar` | filings | the company lookup or filing listing raised (not "this ticker is not an SEC filer": `CompanyNotFoundError`, for example an ETF, is "nothing new"), a form listing failed, or a filing's text download failed |
| `filing_summary` | filings | the summary LLM call failed (an LLM out of quota included), so only a fallback excerpt exists (the filing is not stored and is tried again tomorrow) |
| `sec_ticker_map` | news | the company name for the news query could not be looked up because the SEC ticker map could not be loaded (a ticker that is simply not in the map is not a failure) |
| `newsapi` | news | the NewsAPI search failed (network error, server error, malformed answer); a quota refusal is a deferral, not this |
| `news_relevance` | news | the relevance classifier (an LLM call) could not check every article, for example because the LLM is out of quota, so nothing is stored |

"Nothing new" (no new filing, today's digest already exists, no relevant
article) is a success. The macro step counts as failed when every FRED
indicator fetch raised a request error (source `fred`), when the report's
LLM call failed (source `llm`, log `auto_ingestion.macro_source_failed`),
or when storing the report failed (`auto_ingestion.macro_workflow_failed`);
it is reported as `{"failed": true}`, so the per-ticker result is not
lost. A month whose report already exists, or a month in which FRED
returned no data without an error, is not a failure. Log at the end:
`auto_ingestion.complete`.

The macro report reads five FRED series (fed funds rate, CPI, unemployment
rate, GDP, 10-year Treasury yield) over the last 430 days
(`YOY_MIN_LOOKBACK_DAYS` of the macro tool). The window is long enough for
quarterly GDP, whose newest published observation is dated at the start of
a quarter that ended months ago. CPI is given as year-over-year inflation
(the latest index against the observation one year earlier), the same
calculation as the Macro agent's `get_macro_indicators`; when no
observation one year back exists, the line states that the value is an
index level (log `auto_ingestion.macro_year_over_year_unavailable`). An
indicator with no observations in the window is left out and logged
(`auto_ingestion.macro_indicator_empty`).

Before it creates a document, it asks
core which filenames already exist (`GET /internal/documents/filenames`),
so a filing is not added twice. For each new filing a separate
records-only LLM call (`extract_filing_records`) extracts structured
records in four categories (one-off items, guidance, transactions, legal
matters); they are stored in `ai.filing_records` (one row per filing, keyed
by filename). Each filing is extracted on its own, without the previous
filing's records. When the records call fails, the filing document is
still stored and the gap is listed by the `filing_records` smoke check
(`make health`); `scripts/backfill_filing_records.py --filename <file>
--apply` fills it. The raw filing text is kept in object storage under
`raw_filings/` (chapters [18](18-ai-knowledge-memory.md) and
[22](22-object-storage.md)). New documents are created in core through
`POST /internal/documents` (JSON, `services/core-service/api/internal.py`),
which publishes `document.uploaded` after the commit, so ingestion then
runs through the durable event path
([24-events.md](24-events.md)). Every day: SEC filings can appear on any
day, and a day with nothing new is a harmless no-op. Answer:
`{"per_ticker": {"tickers_processed", "documents_created",
"failed_tickers", "filings": {"attempted", "failed_tickers",
"quota_deferred"}, "news": {"attempted", "failed_tickers",
"quota_deferred"}}, "macro": {"documents_created", "failed",
"attempted"}}` (`failed_tickers` at the top is the union of both steps),
with status 500 when a step's attempted items all failed (`job_response`,
steps `filings`, `news` and `macro`; `macro` has 0 units when FRED is not
configured). Two concurrent filing checks of the same ticker (the nightly
job and an analysis's fresh-filing check) share one run: the second
caller waits for the first and gets the same result
(`sec_filings.ingest_joined`), so a filing is summarised and stored
once.

**purge-unverified** (`services/auth-service/api/internal.py::purge_unverified_accounts`
-> `domains/admin/service.py::AdminService.purge_unverified_accounts`):
deletes every account that registered more than
`UNVERIFIED_ACCOUNT_RETENTION_DAYS` (auth setting, default 7) days ago,
is not verified, has never signed in (`last_seen_at` is null; login needs
a verified email, so this means "never verified") and has no OAuth link.
Each account goes through the same code as an admin's
`DELETE /api/v1/admin/users/{id}` (`AdminService._delete`: anonymized
email and username, profile cleared, sessions deleted, `user.deleted`
published after the commit with the user as `actor_id`), in its own
transaction, after locking the row again with the same conditions. So the
email can be registered again, and core-service handles the event like
any deletion. Then it cleans the auth leftovers of accounts deleted
before the full deletion existed
(`AdminService.clean_deleted_account_leftovers`: sessions, open sent
invitations, memberships, owned organizations; a second run finds
nothing). Answer `{"retention_days", "candidates", "deleted",
"skipped", "failed", "deleted_account_leftovers": {"candidates",
"cleaned", "failed", "lookup_failed"}}`, 500 when every attempted account
failed, every leftover cleanup failed, or the leftover candidate query
failed (`job_response`, steps `accounts`, `deleted_account_leftovers`
and `deleted_account_leftovers_lookup`). A failed leftover query is
caught, so the purge's events are still published. Logs `unverified_accounts_purged` and
`deleted_account_leftovers_cleaned`.
Details: [11-auth-service.md](11-auth-service.md) sections 3.4 and 3.19.

**notification-retention** (`services/core-service/api/internal.py::notification_retention`
-> `domains/notification/service.py::NotificationService.purge_old_read_notifications`):
deletes read notifications created more than
`NOTIFICATION_READ_RETENTION_DAYS` (core setting, default 90) days ago,
in batches of `NOTIFICATION_PURGE_BATCH_SIZE` (1000) rows with one commit
each; unread notifications are never deleted. Answer `{"retention_days",
"deleted", "batches", "failed_batches"}`, 500 when every attempted batch
failed (`job_response`, step `batches`). Log
`notification_retention_done`. Details:
[12-core-service.md](12-core-service.md) section 3.11.

**deleted-user-sweep-core** (`services/core-service/api/internal.py::deleted_user_sweep`
-> `event_listener.py::sweep_deleted_users`) and **deleted-user-sweep-ai**
(`services/ai-service/api/internal.py::trigger_deleted_user_sweep` ->
`user_cleanup.py::sweep_deleted_users`). Deletion policy: a deleted
user's personal data is removed completely in every service, and
`user.deleted` is only the fast path. Each sweep lists the user ids that
still own private data in its own tables, asks auth which of them are
deleted (`POST /internal/users/deleted`, batches of 500; an unknown id
is never treated as deleted), and runs the same idempotent cleanup as its
`user.deleted` handler for each one (`shared/deleted_users.py::
run_deleted_user_sweep`). This repairs a lost event and data an in-flight
request wrote after the deletion. core also adds to its candidates every
owner folder `documents/<user_id>/` in the documents bucket that holds a
file no `core.documents` row references (one listing of `documents/` per
run), and for each deleted user deletes those unreferenced files
(`delete_unreferenced_files`, log `deleted_user_files_removed`). Files of
documents kept by an organization are still referenced, so they stay. One user's failure is logged
(`deleted_user_sweep_item_failed`) and the others still run. The core job
then also sweeps organizations, whatever the user sweep's outcome: every
organization id still referenced by a core document, note or portfolio
is checked with auth (`POST /internal/organizations/missing`), and
`delete_organization_content` runs for each one auth no longer has.
Safety brake: if auth reports more than `ORGANIZATION_SWEEP_MAX_MISSING`
(default 5) missing organizations, or every referenced organization as
missing while there is more than one, nothing is
deleted and `deleted_organization_sweep_refused` is logged (a wrong or
reset auth must not wipe all organization content). If an auth lookup
fails, that part deletes nothing. Answer `{"candidates", "deleted",
"cleaned", "failed", "lookup_failed", "refused"}` (core adds the same for
`"organizations"`); 500 on any failed step (see the steps table). Log
`deleted_user_sweep_completed` (and `deleted_organization_sweep_completed`
in core). Details:
[12-core-service.md](12-core-service.md) section 3.15 and
[13-ai-service.md](13-ai-service.md) section 3.8.

**Why two HTTP jobs and not part of logs-cleanup or backup.** Both
retention rules delete database rows owned by one service, through that
service's code (the deletion path that publishes `user.deleted`, the
notification repository). The local shell jobs have no Python, no
database models and no event publisher, and they have no "units": they
either finish or stop with an error. Putting the deletions into
`logs-cleanup.sh` or `backup.sh` would mean raw SQL from the shell
(bypassing the deletion path and its events), and one heartbeat would
then cover two unrelated tasks, so a failing purge would hide behind a
successful log cleanup (or the reverse). As separate `call-job.sh` jobs
they follow every job rule unchanged: the service answers through
`job_response` (500 only when every attempted unit failed), the script
sends the heartbeat only on a 2xx, each has its own name in
`SCHEDULED_JOBS`, and `DailyJobStale` covers it after 26 hours. Each job
calls the service that owns the data (auth-service owns accounts,
core-service owns notifications).

### 3.6 The local jobs

**backup.sh** (02:30): writes `/backups/<YYYY-MM-DD_HHMM>/` (host
`./backups`) with `postgres.dump` (`pg_dump -Fc` of the whole database,
which holds the auth, core and ai schemas), one Qdrant snapshot per
collection, every object of every bucket under
`objectstore/<bucket>/<key>`, `avatars.tar.gz` (when mounted),
`SHA256SUMS` and `manifest.json` (format 3). It builds in `.incomplete-<stamp>` and
renames only on success; a failed run is kept as `<stamp>-FAILED`. Old
backups (older than `BACKUP_RETENTION_DAYS`, default 7) are deleted only
after today's backup succeeded. The newest 3 complete backups are always
kept, whatever their age (`KEEP_MIN=3`). This protects you when the host
was asleep for longer than the retention window. A leftover
`.incomplete-<stamp>` folder (from a run that was killed) is deleted after
one day. One run at a time (lock directory `/backups/.lock`). `--dry-run`
prints the plan and changes nothing. The heartbeat is sent at the end of a
successful run.

How each part is made and checked:

- PostgreSQL: `pg_dump -Fc`, then `pg_restore -l` reads the dump's table of
  contents, which proves the archive is complete and readable. The
  manifest records the server version and each schema's Alembic revision
  (a schema without an `alembic_version` row is logged as a warning and
  recorded as `null`).
- Qdrant: for every collection a snapshot is created through the REST API
  (`?wait=true`), downloaded, and then deleted from Qdrant again, so
  snapshots do not pile up inside Qdrant. The `api-key` header is sent only
  when `QDRANT_API_KEY` is set. No collections is logged, not an error.
- Qdrant point counts: the manifest records each collection's exact
  point count (`points/count` with `exact: true`) when it is the same
  before and after the snapshot, otherwise `null`.
- Object storage: every bucket is listed and copied with
  `rclone copy objectstore:<bucket> objectstore/<bucket>` through the S3
  API, with the services' endpoint and keys (`S3_*`, passed to rclone as
  environment variables, never on a command line). rclone checks each
  object's MD5; `SHA256SUMS` lists every object and the manifest records
  the object count and bytes per bucket. Each S3 `GET` returns a whole
  object, so the copy is consistent per object even if a document changes
  during the backup, and it does not depend on SeaweedFS's on-disk
  format.
- Avatars: a `tar.gz` of the volume, mounted read-only, then `tar -tzf`
  checks the archive.
- A run killed outright (the container recreated mid-backup) skips the
  clean-up trap and leaves `.incomplete-<stamp>` behind; that is why such
  folders are removed after a day.
- Only folder names that look like a backup run are ever pruned, never
  anything else under the backup root.

Full details, and how `restore.sh` restores each part (PostgreSQL in one
transaction that drops the three schemas with CASCADE, Qdrant through
snapshot upload, object storage through the S3 API with
`rclone sync --checksum` while `objectstore` runs, and `--verify`, which
compares the live data with the backup):
[../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md).

**logs-cleanup.sh** (04:00): deletes `*.log` files under `LOGS_DIR`
(default `/logs`, host `./logs`) not modified for more than
`RETENTION_DAYS` (default 14) days (`find -mtime +14 -delete`), then empty
directories (`-mindepth 1`, so the mount point itself stays). `-mtime +N`
means "at least N+1 whole days old", so today's file and the previous 14
days' files are always kept. Vector closes idle files after 60 s, so an old
file is never still open. If `/logs` does not exist it reports "nothing to
do" and still sends the heartbeat. A `RETENTION_DAYS` that is not a whole
number stops the script with an error (no heartbeat). It uses only POSIX
`sh` and BusyBox `find` options (Alpine). It can also be run on the host
from the repository root with `LOGS_DIR=./logs sh
infra/scheduler/logs-cleanup.sh`; the deletion works, but the final
heartbeat call (`/scripts/heartbeat.sh`) does not exist on the host, so
the script prints `... not found, success not reported` and exits 0.

**restore.sh** (manual only): dry run by default (verifies checksums and
the dump, prints what it would do); `--yes` really restores, which replaces
current data, after a preflight that checks every chosen part's
preconditions (databases answer, object storage running or stopped as the
backup format needs, target volumes writable) so that nothing is replaced
when one part could not run; `--verify` changes nothing and exits 1 when the live data
differs from the backup (Alembic revisions, table and Qdrant point
counts, every object by size and MD5, avatars). Through the Makefile:
`make restore BACKUP=<name>` (dry run) or `make restore BACKUP=<name>
CONFIRM=yes` (**DESTRUCTIVE**: stops auth-service, core-service,
ai-service and scheduler, makes sure objectstore runs, restores, runs `--verify`, starts everything
again). That last step also starts the scheduler, so on the cloud host
run `docker compose --env-file .env.docker stop scheduler` right after
the restore. Both forms run the one-off `restore` compose service with
`docker compose run --rm --build`. `--build` makes sure the restore image
is rebuilt, because `make up` does not rebuild it, and an old image would
run an old `restore.sh`.

**Restore drill** (manual, monthly): `make restore-drill
[BACKUP=<name>]` (`bash scripts/backups.sh drill`) restores a backup into
a separate, temporary compose project `alphamind-drill` (its own empty
volumes, no published ports), runs `restore.sh --yes` and
`restore.sh --verify`, prints how long it took and removes the project
with its volumes, also on failure. It does not touch the live stack.
Details: `../DISASTER_RECOVERY.md` section 6.

### 3.7 Holiday handling: `shared/market_calendar.py`

cron only knows weekdays. Holidays are checked by the services, so every
service uses the same calendar and "today" is the New York date:

- `MARKET_TZ = ZoneInfo("America/New_York")`; `market_today()` converts
  "now" (UTC) to New York and takes the date. A UTC date would be wrong
  for a manual run after 20:00 New York.
- `nyse_holidays(year)`: rules-based, no third-party package. New Year's
  Day (Sunday -> Monday; a Saturday is not moved to the Friday before),
  Martin Luther King Jr. Day (3rd Monday of January), Washington's
  Birthday (3rd Monday of February), Good Friday (2 days before Easter,
  computed with the Meeus/Jones/Butcher algorithm), Memorial Day (last
  Monday of May), Juneteenth (June 19, from 2022), Independence Day
  (July 4), Labor Day (1st Monday of September), Thanksgiving (4th
  Thursday of November), Christmas (December 25). Saturday holidays move to
  Friday, Sunday holidays to Monday (`_observed`), except New Year's Day.
- `EXTRA_CLOSED_DATES`: one-off closures (2018-12-05, 2025-01-09, national
  days of mourning). New ones must be added by hand when the exchange
  announces them.
- `is_trading_day(day)`: weekday and not a holiday.

Which jobs skip: market-open, market-close (core) and tier-monitoring (ai)
answer `200 {"skipped": true, ...}` on a non-trading day. Because the
answer is 200, the script prints it, counts it as success and sends the
heartbeat. review-run, rag-ingest, backup, logs-cleanup,
purge-unverified, notification-retention and the deleted-user sweeps run
every day.
Early-close days (13:00 close) are treated as normal trading days; the
after-close jobs simply run later than needed.

### 3.8 Heartbeats and the stale-job alerts

`infra/scheduler/heartbeat.sh <job>`:

```sh
curl -sS -f --max-time 10 -X POST "${CORE_SERVICE_URL}/internal/scheduler/heartbeat/${job}" \
    -H "X-Internal-Token: ${INTERNAL_API_TOKEN:-}" > /dev/null
```

It prints `<job>: success reported` or an error, and always exits 0: a
heartbeat problem never fails the job itself. If it cannot be sent, the
stale-job alert fires later, which is the point (core-service or the
network is not working).

core-service (`api/internal.py::scheduler_heartbeat` and
`services/core-service/job_health.py`):

- `job` must be in `SCHEDULED_JOBS = {market-open, market-close,
  tier-monitoring, review-run, rag-ingest, logs-cleanup, backup,
  purge-unverified, notification-retention, deleted-user-sweep-core,
  deleted-user-sweep-ai}`, else 404. The heartbeat always goes to
  core-service, also for the auth-service job purge-unverified and the
  ai-service jobs.
- `record_job_success(job)` sets the gauge
  `alphamind_job_last_success_timestamp_seconds{scheduled_job="<job>"}`
  to the current Unix time and stores it in the Redis hash
  `core:job_last_success` (cache DB 0). Answer: `{"job": ...,
  "recorded_at": <ISO time>}`.
- At startup `load_job_health()` reloads the hash and the market-close
  skip count (`core:market_close_skipped`), so the gauges survive a
  core-service restart (without this, a restart would reset the skip
  count to 0 and silently clear the `MarketCloseSkippedPortfolios`
  alert). Unknown job names and unreadable values in the hash are
  ignored.
- Redis problems never fail a heartbeat: a failed store is logged
  (`job_health_store_failed`, `market_close_skipped_store_failed`) and the
  gauge is still set; a failed reload is logged (`job_health_load_failed`,
  `market_close_skipped_load_failed`). The only loss is the value across a
  restart.
- A job in `SCHEDULED_JOBS` that has no stored success yet gets a
  "tracked since" value: at core start `load_job_health()` runs
  `HSETNX core:job_tracked_since <job> <now>` (so the first start is kept
  across restarts) and sets the gauge to that time, logging
  `scheduled_job_no_success_yet`. The stale-job alerts therefore fire 26 h
  (`DailyJobStale`) or 100 h (`TradingDayJobStale`) after tracking began
  for a job that never succeeds. `core:job_last_success` holds only real
  successes. If Redis cannot be read at start, every job gets the process
  start time (nothing is written).

**Why the label is called `scheduled_job`.** Prometheus adds a `job`
label to everything it scrapes: the name of the scrape job in
`infra/prometheus/prometheus.yml`, here `job="core-service"`. Two labels
cannot have the same name. If the service also exported a label `job`,
Prometheus (without `honor_labels: true`) would keep its own and rename
the service's label to `exported_job`, and alert rules that filtered on
`job` would never match anything. So the gauge's label is `scheduled_job`.
The series that Prometheus stores looks like this:

```
alphamind_job_last_success_timestamp_seconds{scheduled_job="backup", job="core-service", instance="core-service:8000", service="core-service"}
```

When you query Prometheus for one scheduled job, filter on
`scheduled_job`. `scripts/health.sh` reads `scheduled_job`, and in its
"firing alerts" block it names a stale-job alert by its `scheduled_job`
(for example `DailyJobStale  backup`), not by the scrape job.

Alert rules (`infra/prometheus/alert_rules.yml`, group `scheduled_jobs`):

| Alert | Expression (simplified) | For | Meaning |
|---|---|---|---|
| `DailyJobStale` | `time() - alphamind_job_last_success_timestamp_seconds{scheduled_job=~"backup\|review-run\|rag-ingest\|logs-cleanup\|purge-unverified\|notification-retention\|deleted-user-sweep-core\|deleted-user-sweep-ai"} > 26 * 3600` | 10m | a daily job has not succeeded for 26 h (one run plus 2 h slack) |
| `TradingDayJobStale` | same for `market-open\|market-close\|tier-monitoring`, `> 100 * 3600` | 10m | a trading-day job has not succeeded for 100 h (the longest normal gap is a weekend, 72 h, because a closed-market weekday still reports success) |
| `MarketCloseSkippedPortfolios` | `alphamind_market_close_portfolios_skipped > 0` | (none) | the last market-close skipped portfolios |

`DailyJobStale` and `TradingDayJobStale` filter on
`scheduled_job=~"..."`. Their promtool unit tests
(`infra/prometheus/alert_rules_test.yml`, run by `make promtool-test`)
feed series that carry both `job="core-service"` and `scheduled_job`,
like a real scrape, so the tests would catch a rule that filtered on
`job` again.

Alerts go to Alertmanager and are emailed to MailHog (see
[27-observability.md](27-observability.md)). The `DailyJobStale` text also
reminds you to check that the Mac was awake at the scheduled time: cron
does not catch up on a run missed while the host slept.

Without heartbeats, a failed backup, ingestion or market-close would only
print one log line, and nothing would alert.

### 3.9 Why market-close (17:30) runs after tier monitoring (17:00)

Tier monitoring can **execute** triggered tiers, which writes buy/sell
transactions into the portfolio ledger. The NAV snapshot must include those
trades. A market-close at the 16:00 close would miss the day's tier trades
until the next day's snapshot. So:

- 16:00 the market closes.
- 17:00 tier monitoring: one hour of buffer, because the data provider's
  final end-of-day bar is not always available right at the close. Its time
  limit is 900 s, so it has finished by 17:15.
- 17:30 market-close: all trades are in, and the final bar is settled.

### 3.10 Busy windows the operator scripts respect

`scripts/lib.sh::in_busy_window` (New York time):

| Window | Why |
|---|---|
| 09:00-10:30 | market-open (09:30) starts analyses for opted-in portfolios |
| 16:55-17:50 | tier monitoring (17:00) and the market-close snapshot (17:30) |

`guard_busy_window` makes `scripts/rebuild.sh`, `scripts/e2e.sh` and
`scripts/snapshots.sh` refuse to restart ai-service/core-service or run an
end-to-end check in these windows, because that could break a job. Override
with `FORCE=1`. The night jobs (02:00-04:40) are not guarded; inside
01:55-04:55 the scripts only print a warning.

### 3.11 The compose service

`docker-compose.yaml`, service `scheduler`:

- `env_file: .env.docker` (the token, backup settings and the `S3_*`
  object storage settings the backup uses) plus `CORE_SERVICE_URL`,
  `AI_SERVICE_URL`, `AUTH_SERVICE_URL`, `BACKUP_DIR=/backups`,
  `AVATAR_DATA_DIR=/avatar-data`.
- Volumes: `./logs:/logs`, `./backups:/backups`,
  `avatar_data:/avatar-data:ro` (read-only: backups only read it; restores
  use the separate `restore` service). The object storage volume is not
  mounted: objects are read through the S3 API.
- `depends_on`: only `postgres`, `qdrant`, `objectstore` (healthy). It does
  not wait for core-service or ai-service: if it did, an ai-service that
  failed to start would also stop the scheduler, and with it the nightly
  backup. The HTTP jobs report a down service themselves, and the
  stale-job alerts fire.
- Logging capped like every container (json-file, 5 x 20 MB).
- On the cloud host the scheduler is kept stopped: its jobs spend the
  LLM and news quotas that the cloud shares with the local stack, and
  `scripts/deploy.sh` keeps a stopped scheduler stopped (chapter
  [32](32-ci-cd.md) section 3.5). So the host runs no scheduled job and
  makes no automatic backup. `make backup` does not work there either:
  it runs `backup.sh` inside the running scheduler container. Backups
  are made on the local stack (nightly, or by hand with `make backup`)
  and copied to the host when its data should be replaced. Because no
  job succeeds on the host, its stale-job alerts fire after 26 h / 100 h
  (section 3.8).
- The quota-free jobs still matter on the host, because real users are
  deleted there: purge-unverified, notification-retention and the two
  deleted-user sweeps (the sweeps catch whatever a lost `user.deleted`
  or `organization.deleted` event missed). Run them by hand on the host,
  in this order, while the scheduler stays stopped (do not start it):

  ```bash
  for job in purge-unverified notification-retention \
      deleted-user-sweep-core deleted-user-sweep-ai; do
    docker compose --env-file .env.docker run --rm --no-deps scheduler \
      /scripts/call-job.sh "$job"
  done
  ```

  `run` starts a one-off container from the scheduler image; with
  arguments, `entrypoint.sh` runs that command instead of cron (section
  3.2). Each job prints its answer and sends its heartbeat as usual.

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `TZ` | `America/New_York` | time zone of the crontab | `infra/scheduler/Dockerfile` |
| `CORE_SERVICE_URL` | `http://core-service:8000` | target of core jobs and heartbeats | `docker-compose.yaml`, `entrypoint.sh` |
| `AI_SERVICE_URL` | `http://ai-service:8000` | target of ai jobs | same |
| `AUTH_SERVICE_URL` | `http://auth-service:8000` | target of the auth job (purge-unverified) | same |
| `UNVERIFIED_ACCOUNT_RETENTION_DAYS` | `7` | age after which a never-verified account is deleted | `services/auth-service/config.py`, `.env.example` |
| `NOTIFICATION_READ_RETENTION_DAYS` | `90` | age after which a read notification is deleted | `services/core-service/config.py`, `.env.example` |
| `NOTIFICATION_PURGE_BATCH_SIZE` | `1000` | rows per delete batch | `services/core-service/config.py` |
| `INTERNAL_API_TOKEN` | empty (must be set) | `X-Internal-Token` for `/internal/*` | `.env.example`, `shared/config.py` |
| `BACKUP_DIR` | `/backups` | backup root | `docker-compose.yaml`, `backup.sh` |
| `BACKUP_RETENTION_DAYS` | `7` | days of backups kept (the newest 3 are always kept) | `.env.example`, `backup.sh` |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | from `.env.docker` | object storage the backup copies through its S3 API (rclone remote `objectstore:`) | `.env.example`, `s3-remote.sh` |
| `AVATAR_DATA_DIR` | `/avatar-data` | avatars to archive | `docker-compose.yaml`, `backup.sh` |
| `LOGS_DIR` | `/logs` | log root for cleanup | `logs-cleanup.sh` |
| `RETENTION_DAYS` (logs) | `14` | days of logs kept | `logs-cleanup.sh` |
| curl `--max-time` | 600 / 900 / 600 / 1800 / 3600 / 300 / 300 / 900 / 900 / 10 s | per-job time limit (open / tiers / close / review / ingest / purge-unverified / notification-retention / deleted-user-sweep-core / deleted-user-sweep-ai / heartbeat) | `call-job.sh`, `heartbeat.sh` |
| `ORGANIZATION_SWEEP_MAX_MISSING` (core) | `5` | safety brake of the organization sweep | `services/core-service/config.py`, `.env.example` |
| `ALL_UNITS_FAILED_STATUS` | `500` | answer of a scheduler route when every unit of a step failed | `shared/scheduled_jobs.py` |
| `SCHEDULED_JOBS` | 11 job names | accepted heartbeat names | `services/core-service/job_health.py` |
| `EXTRA_CLOSED_DATES` | 2 dates | one-off market closures | `shared/market_calendar.py` |
| busy windows | 09:00-10:30, 16:55-17:50 NY | operator scripts refuse restarts | `scripts/lib.sh` |

## 5. Design decisions and why

**cron in a container that calls HTTP endpoints.** Alternatives: an
in-process scheduler (APScheduler) inside a service, or a task queue
(Celery beat). Reason: the logic stays in the services, where it is tested
and uses the same database sessions and clients; the scheduler has no
Python and no dependencies; a job can be run by hand with one curl. A
service restart does not lose the schedule.

**Times in New York, not UTC.** The jobs follow the US market. With
`TZ=America/New_York` the crontab stays right across daylight-saving
changes. A UTC crontab would drift by an hour twice a year.

**Holidays checked in the services, not in cron.** cron cannot express the
NYSE calendar. Checking in the service puts one calendar in shared code,
used by core and ai alike, and lets a skipped run still answer 200 (and
send its heartbeat).

**Market-close after tier monitoring.** Explained in section 3.9: snapshots
must include the day's tier trades and the settled end-of-day bar.

**Heartbeats into Prometheus.** Alternative: parse the scheduler log.
Reason: an alert on "last success too old" catches every failure kind at
once: curl error, service down, cron not running, host asleep, script
broken. Storing the time in Redis keeps it across core-service restarts.

**A run where every unit failed answers 500.** Alternative: always answer
200 and let the operator read the counts. Reason: the heartbeat is sent on
any 2xx answer, so a job that fails completely every day would look
healthy forever. Answering 500 (with the counts in the body, printed by
`--fail-with-body`) skips the heartbeat, and the existing stale-job alerts
fire. A partial failure stays a success: some tickers failing on one day
is normal (provider hiccups) and they are retried the next day.

**Jobs write to PID 1's stdout.** A file inside the container would be
invisible to `docker compose logs` and lost on recreate. On PID 1's
stdout, Vector collects the job lines with every other container's logs.

**Scheduler does not depend on the app services.** So a broken ai-service
cannot also stop the nightly backup.

## 6. When things go wrong

| Problem | What you see | What to do |
|---|---|---|
| A service is down at job time | `<job>: request FAILED (curl exit code 7)` (connection refused) or 22 (HTTP error) in the scheduler log; no heartbeat | fix the service; run the job by hand (section 7) if it is still useful |
| Every unit of a run failed | the answer body with `"all_units_failed": [...]` followed by `request FAILED (curl exit code 22)`; `scheduled_job_all_units_failed` (error) in the service log; no heartbeat | look at the per-unit errors (`market_open_analysis_request_failed`, `auto_ingestion.filings_failed`, `auto_ingestion.news_failed`, `auto_ingestion.ticker_source_failed`, `auto_ingestion.macro_source_failed`, `auto_ingestion.macro_workflow_failed`, `tier_monitoring.price_lookup_failed`, tier outcomes in `tier_monitoring.run_complete`); usually one provider or service is down |
| Token wrong or empty | curl exit code 22, the service answered 401 | check `INTERNAL_API_TOKEN` in `.env.docker` for the scheduler and the services (do not print it) |
| Job too slow | curl exit code 28 (timeout) | look at the service log for the job's progress; the stale alert fires later |
| Heartbeat failed | `<job>: could not report success to core-service` | core-service problem; the job itself succeeded |
| Host asleep (laptop) | no job lines at all for that time | the job's age grows in `make health`; `DailyJobStale` / `TradingDayJobStale` fire after 26 h / 100 h |
| purge-unverified answers 500 | `"all_units_failed": ["accounts"]` or `["deleted_account_leftovers"]` / `["deleted_account_leftovers_lookup"]` (`deleted_account_leftovers_failed` / `deleted_account_leftovers_lookup_failed`); `unverified_account_purge_failed` (error, with `user_id`) in the auth-service log | read the error; usually the database or Redis (the user-state mirror) was down; the next night retries |
| notification-retention answers 500 | `"all_units_failed": ["batches"]`; `notification_retention_failed` in the core-service log | database problem; the next night retries |
| A deleted-user sweep answers 500 or reports `failed` > 0 | `"all_units_failed"` names the step: `users`/`organizations` (cleanups failed), `users_lookup`/`organizations_lookup` (auth unreachable; `deleted_*_sweep_lookup_failed`), `organizations_safety_brake` (`deleted_organization_sweep_refused`: check that core talks to the right, intact auth; if the deletions are real, raise `ORGANIZATION_SWEEP_MAX_MISSING` for one run; when every organization with content is missing, no setting passes the brake, so clean them with `docker compose exec core-service python -m scripts.clean_organizations --id <org id> [--id ...]`, then again with `--confirm`: it asks auth first and skips any id auth still has); `deleted_user_sweep_item_failed` and `user_data_cleanup_failed` (core) or `ai_user_data_cleanup_failed` (ai) with the step name | read the step error (database, S3, Qdrant or auth down); the next night retries, because the rows are still there |
| A user says their unverified account vanished | `unverified_accounts_purged` in auth-service; the account was older than 7 days, unverified and never signed in | expected; they register again |
| Holiday | `{"skipped": true, "reason": "market holiday", ...}` and `scheduled_job_skipped` in core, `tier_monitoring_skipped_non_trading_day` in ai | nothing; this is correct |
| A portfolio got no market-open analyses | `market_open_owner_inactive_skipped` in core: the owner is deactivated or deleted | nothing; reactivate the owner if the skip is wrong |
| A portfolio got no snapshot | `market_close_snapshot_skipped` in core; `MarketCloseSkippedPortfolios` alert | fill the day later: `bash scripts/snapshots.sh --portfolio=<id> --to <date>`, then again with `--confirm` |
| Backup failed | `backup: ERROR: ...`, a `<stamp>-FAILED` folder, no heartbeat | read the error; `make backups`; `make backup-check` |
| Stale-job alerts on the cloud host | `DailyJobStale` / `TradingDayJobStale` for the jobs, no backups in the host's `backups/` | expected: the host keeps the scheduler stopped (section 3.11); show jobs, backups and alerts on the local stack |
| A new exchange closure not in the calendar | the jobs run as on a trading day, without a fresh price bar for that date (expect `no_fresh_data` tiers and skipped portfolios) | add the date to `EXTRA_CLOSED_DATES` |
| Gauge shows nothing after a fresh install | no series until the first success | expected |
| Your query for one job returns nothing | you filtered on `job="backup"` | filter on `scheduled_job="backup"` (section 3.8) |

Note: `alphamind_market_close_portfolios_skipped` is also stored in Redis
(`core:market_close_skipped`). After a core-service restart it is reloaded,
so the `MarketCloseSkippedPortfolios` alert keeps firing until the next
market-close run with no skipped portfolio.

## 7. Demo walkthrough

All steps except the marked ones are read-only. Run them on the local
stack: the cloud host keeps the scheduler stopped.

1. **Show the schedule and the running cron.**
   ```
   docker compose --env-file .env.docker exec -T scheduler cat /etc/crontabs/root | grep -v '^#' | grep .
   docker compose --env-file .env.docker exec -T scheduler date
   docker compose --env-file .env.docker exec -T scheduler ps
   ```
   The audience sees the eleven lines, the New York time (`EDT`/`EST`), and
   `crond -f -l 2` as a child of `/entrypoint.sh`. Say: "Times are New York
   time; in London add five hours."

2. **Show recent job output.**
   ```
   docker compose --env-file .env.docker logs --since 24h scheduler | tail -40
   ls logs/scheduler/
   ```
   Point at a `done` line, the JSON summary printed by curl, and
   `success reported`. On a weekend you see `{"skipped":true,...}`.

3. **Show the health view.**
   ```
   make health        # = bash scripts/health.sh
   ```
   Sections: containers; "scheduled jobs (last success)" with each job and
   its age in hours (from the gauge); portfolios skipped by the last
   market-close; firing alerts; NewsAPI requests in the last 24 hours.
   Say: "If a job silently stops, its age
   grows here." (`health.sh` asks Prometheus at `http://127.0.0.1:9090`,
   so the `prometheus` container must be running.)

4. **Show where the heartbeat lives.**
   ```
   bash scripts/redis-cli.sh -n 0 HGETALL core:job_last_success
   curl -sS --get http://127.0.0.1:9090/api/v1/query \
     --data-urlencode 'query=alphamind_job_last_success_timestamp_seconds{scheduled_job="backup"}'
   ```
   The first command shows Unix timestamps per job in Redis. The second
   shows the same value for `backup` as Prometheus stores it. Point at the
   labels in the answer: `scheduled_job="backup"` and `job="core-service"`.
   Say: "`job` is the scrape job Prometheus adds; the scheduled job has
   its own label, so the stale-job alerts can match it."

5. **Run one harmless job by hand: the backup dry run.** It prints the plan
   and writes, dumps and deletes nothing, and it sends no heartbeat:
   ```
   docker compose --env-file .env.docker exec scheduler /scripts/backup.sh --dry-run
   ```
   Also harmless: the restore dry run of the newest backup
   (`make backup-check`), which only verifies checksums and the dump. It
   uses `docker compose run --build`, so the first run may spend a minute
   building the restore image.

6. **Commands for real job runs (shown, not run by default).**
   - **WRITES DATA:** review run (creates evaluation rows that are not yet
     present, may send `decision.outcome.evaluated` notifications; a second
     run the same day adds nothing):
     ```
     docker compose --env-file .env.docker exec scheduler /scripts/call-job.sh review-run
     ```
   - **WRITES DATA:** a real backup (`make backup`), several minutes,
     prunes backups older than 7 days after success (the newest 3 are
     always kept).
   - **DELETES FILES:** `docker compose --env-file .env.docker exec scheduler /scripts/logs-cleanup.sh`
     (log files older than 14 days).
   - **DELETES DATA:** `call-job.sh purge-unverified` (accounts older than
     7 days that never verified their email) and `call-job.sh
     notification-retention` (read notifications older than 90 days), and
     `call-job.sh deleted-user-sweep-core` / `deleted-user-sweep-ai`
     (leftover data of deleted accounts). All are safe to repeat: a second
     run finds nothing more to delete.
   - **WRITES DATA (LLM, NewsAPI and embedding quota):** `call-job.sh
     rag-ingest` (new filings, news digests, the monthly macro report).
   - **WRITES DATA / STARTS ANALYSES (LLM cost):** `call-job.sh market-open`
     on a trading day. **EXECUTES TRADES:** `call-job.sh tier-monitoring`.
     Do not run these in a demo, and never inside the busy windows.

What to say at the end: "cron only knows times; the services know the
market calendar; the heartbeats tell Prometheus when a job stopped
succeeding."

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Why does the scheduler write `/scripts/env.sh`?**
   cron does not pass the container's environment to jobs; the scripts
   source this file to get the URLs, the internal token and the backup
   settings.

2. **What time does tier monitoring run in London?**
   17:00 New York = usually 22:00 London (21:00 during the short periods
   when the clocks change on different dates).

3. **Who decides that today is a holiday?**
   The services, with `shared/market_calendar.py::is_trading_day` on the
   New York date. cron only filters weekdays.

4. **On Thanksgiving, does market-close send a heartbeat?**
   Yes. The endpoint answers 200 `{"skipped": true, ...}`, curl succeeds,
   and the script reports success.

5. **Why 17:30 for market-close?**
   After tier monitoring (17:00, at most 15 minutes) executed the day's
   triggered tiers, and after the final end-of-day bar is available.

6. **What does `DailyJobStale` measure?**
   `time()` minus the last success time of a daily job, alerting above 26
   hours for 10 minutes. It filters on the `scheduled_job` label.

7. **Why does `alphamind_job_last_success_timestamp_seconds{job="backup"}`
   return nothing in Prometheus?**
   On every scraped series `job` is the scrape job, here `core-service`.
   The scheduled job's name is in the label `scheduled_job`; it was given
   that name because a metric label called `job` would be renamed to
   `exported_job`. Query `{scheduled_job="backup"}` instead.

8. **Why does the scheduler not depend on ai-service being healthy?**
   So an ai-service that fails to start does not stop the nightly backup.

9. **Which operator scripts refuse to run at 17:10 New York, and how do you override?**
   Restarts (`scripts/rebuild.sh`), end-to-end runs (`scripts/e2e.sh`) and
   snapshot recomputation (`scripts/snapshots.sh`); override with `FORCE=1`.

10. **The NewsAPI and SEC providers are down all night. Does rag-ingest send a heartbeat?**
    No. The filing listing and the news search each report a failed
    source (`sec_edgar`, `newsapi`), so every attempted ticker fails in
    both steps and `job_response` answers 500 with
    `"all_units_failed": ["filings", "news"]`; curl exits with 22 and the
    script sends no heartbeat. After 26 hours `DailyJobStale` fires. If
    only some tickers fail, the run is still a success. A night with
    nothing new, a source that is not configured (empty key) and a
    NewsAPI daily quota that is used up are successes too: none of them is
    an outage. An LLM out of quota is different: the relevance classifier,
    the filing summary and the macro narrative then fail, so those tickers
    count as failed.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `infra/scheduler/Dockerfile` | image, packages, `TZ=America/New_York` |
| `infra/scheduler/entrypoint.sh` | writes `/scripts/env.sh`, then runs its arguments (one job by hand) or crond as a child |
| `infra/scheduler/crontab` | the eleven job lines with their reasons |
| `infra/scheduler/call-job.sh` | the nine curl jobs with their routes and time limits |
| `infra/scheduler/backup.sh`, `restore.sh`, `s3-remote.sh` | backup, manual restore and `--verify`; the rclone S3 remote |
| `scripts/backups.sh`, `infra/drill/docker-compose.drill.yml` | list and check backups; the restore drill (`make restore-drill`) |
| `infra/scheduler/logs-cleanup.sh` | 14-day log retention |
| `infra/scheduler/heartbeat.sh` | success report to core-service |
| `shared/scheduled_jobs.py` | `job_response`, `all_units_failed`: the 500 rule for a run where every unit failed |
| `services/core-service/api/internal.py` | `market_open`, `market_close`, `notification_retention`, `deleted_user_sweep`, `scheduler_heartbeat`, `_skipped`, `get_tracked_tickers` |
| `services/auth-service/api/internal.py`, `domains/admin/service.py` | `purge_unverified_accounts` (route and `AdminService` method, which reuses the admin deletion `_delete`) and `clean_deleted_account_leftovers` |
| `services/core-service/domains/notification/service.py`, `repository.py` | `purge_old_read_notifications`, `delete_read_before` |
| `services/core-service/job_health.py` | the two gauges, Redis persistence |
| `services/core-service/domains/portfolio/service.py` | `run_market_open_job`, `run_market_close_job` |
| `services/ai-service/api/internal.py` | `trigger_review`, `trigger_tier_monitoring`, `trigger_rag_ingest`, `trigger_deleted_user_sweep` |
| `shared/deleted_users.py`, `services/core-service/event_listener.py`, `services/ai-service/user_cleanup.py` | the deleted-user sweeps: `run_deleted_user_sweep`, `deleted_user_ids`, and each service's `sweep_deleted_users`; core also `sweep_deleted_organizations` and `delete_unreferenced_files` |
| `services/core-service/scripts/clean_organizations.py` | the way out of the organization sweep's safety brake (section 6) |
| `services/ai-service/ai/workflows/tier_monitoring_workflow.py`, `review_workflow.py`, `auto_ingestion_workflow.py` | the job workflows |
| `shared/market_calendar.py` | `market_today`, `nyse_holidays`, `is_trading_day` |
| `infra/prometheus/alert_rules.yml` | group `scheduled_jobs` |
| `scripts/lib.sh` | busy windows, `guard_busy_window` |
| `scripts/health.sh` | job ages and firing alerts |
| `docker-compose.yaml` | the `scheduler` and `restore` services |

## 10. Related chapters

- [02-operations.md](02-operations.md): running the stack and the operator scripts.
- [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md): tier monitoring and review in depth.
- [17-ai-rag.md](17-ai-rag.md): what happens to the documents rag-ingest creates.
- [24-events.md](24-events.md): events published by the jobs.
- [27-observability.md](27-observability.md): logs, the gauges and the alert pipeline.
- [20-postgresql.md](20-postgresql.md), [21-qdrant.md](21-qdrant.md), [22-object-storage.md](22-object-storage.md): what the backup copies.
- [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md): backup and restore procedure.
