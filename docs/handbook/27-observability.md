# 27. Logs, metrics, alerts and dashboards

> "Observability" means being able to answer "what is the system doing,
> and what went wrong?" without adding new code. AlphaMind has three
> layers. **Logs**: every service writes one JSON line per event with
> structlog; Vector copies the output of every docker compose container
> into daily files under `./logs`, kept 14 days. **Metrics**: every service exposes `/metrics`;
> Prometheus scrapes them (and two exporters for PostgreSQL and Redis)
> every 15 seconds. **Alerts and dashboards**: Prometheus evaluates alert
> rules (with unit tests), Alertmanager emails firing alerts to MailHog,
> and Grafana shows two provisioned dashboards behind the gateway.

## 1. The big picture

```
                        LOGS                                         METRICS
 auth / core / ai  --stdout JSON lines-->  Docker json-file      auth/core/ai  GET /metrics
 (structlog)                               (5 x 20 MB per        postgres-exporter :9187
 every other container --stdout-->          container)           redis-exporter    :9121
                                               |                 alertmanager      :9093
                                     docker.sock (read)          prometheus        :9090
                                               v                        ^
                                           vector                       | scrape every 15 s
                                               |                        |
                          ./logs/<service>/<YYYY-MM-DD>.log         Prometheus (127.0.0.1:9090)
                          (New York date, prefix "<ts> <stream>")   rule_files: alert_rules.yml
                                               |                        |  firing alerts
                         logs-cleanup.sh 04:00 NY, 14 days              v
                                                                 Alertmanager (127.0.0.1:9093)
                                                                        | SMTP mailhog:1025
                                                                        v
                                                                 MailHog UI (127.0.0.1:8025)

                                     Grafana  (https://localhost:8443/grafana/)
                                     datasource "Prometheus" -> http://prometheus:9090
                                     dashboards: Service Overview, Infrastructure & AI pipeline
```

Logs answer "what happened, step by step" (one request, one analysis).
Metrics answer "how much, how fast, how often" (request rate, latency,
failed analyses per hour, last success of each job). Alerts turn metrics
into a message when something is wrong for long enough.

## 2. Concepts you need

| Term | Meaning here |
|---|---|
| Structured logging | Each log line is a set of key/value fields (here JSON), not free text. Easy to grep and to parse. |
| structlog | The Python library that builds these lines. A chain of **processors** adds fields (service, level, timestamp) and a **renderer** turns the dictionary into JSON. |
| Event name | The first argument of a log call, e.g. `logger.info("rag_ingestion_complete", document_id=...)`. It appears as the `"event"` field. AlphaMind uses snake_case names, sometimes with a dot prefix (`tier_monitoring.run_complete`). |
| Docker json-file driver | Docker's default log storage: it keeps each container's stdout/stderr in JSON files on the Docker disk. `docker compose logs` reads them. |
| Vector | A log collector. Here it reads the logs of the docker compose containers from the Docker API and writes them to per-service daily files. |
| Prometheus | A time-series database that **scrapes** (pulls) metrics over HTTP from targets at a fixed interval and evaluates rules. |
| Metric types | **Counter**: only goes up (requests, errors). **Gauge**: a current value (queue length, last success time). **Histogram**: counts observations in buckets (request durations), used for percentiles. |
| Label | A key/value that splits one metric into series, e.g. `status="5xx"`, `service="ai-service"`. |
| PromQL | Prometheus' query language: `rate(x[5m])` (per-second increase over 5 minutes), `increase(x[1h])`, `histogram_quantile(0.95, ...)`, `sum by (label) (...)`. |
| p95 | 95th percentile: 95% of requests were faster than this. |
| Exporter | A small program that reads another system (PostgreSQL, Redis) and exposes its numbers as Prometheus metrics. |
| Alert rule | A PromQL expression plus a `for:` duration. When the expression returns series for longer than `for`, the alert goes from *pending* to *firing*. |
| Alertmanager | Receives firing alerts from Prometheus, groups them, de-duplicates, silences, and sends notifications (here: email). |
| Inhibition | "While alert A fires, do not send alert B" (here: critical silences warnings of the same service). |
| MailHog | A fake SMTP server with a web UI; every email sent to it is caught and shown, nothing leaves the machine. |
| Grafana | A dashboard tool. **Provisioning** means its datasources and dashboards are created from files at startup instead of by clicking. |
| promtool | Prometheus' command-line tool; `promtool test rules` runs unit tests for alert rules with fake time series. |

## 3. How it works in AlphaMind

### 3.1 Structured logging: `shared/logging.py`

Every service calls `configure_logging(settings)` at the start of its
lifespan (`services/*/main.py`). It does four things:

1. `logging.basicConfig(format="%(message)s", stream=sys.stdout,
   level=settings.LOG_LEVEL)`: Python's standard logging (used by uvicorn,
   httpx, SQLAlchemy and others) prints plain messages to stdout.
2. Noisy libraries are set to `WARNING`: `httpx`, `httpcore`, `botocore`,
   `urllib3`, `s3transfer`. Reasons: httpx logs every request URL at INFO,
   and some providers (FRED) put their API key in a query parameter, so
   keys would end up in the logs. botocore/urllib3/s3transfer log every S3
   request at DEBUG with headers (including the S3 access key id), and
   ai-service's health check calls object storage every 15 seconds.
3. structlog processors, in order: `add_service_name` (field `service` =
   `settings.SERVICE_NAME`), `merge_contextvars` (fields bound to the current
   async context, if any), `add_log_level` (`level`), `add_log_level_number`
   (`level_number`), `TimeStamper(fmt="iso")` (`timestamp`, UTC, ISO 8601),
   `StackInfoRenderer`, `format_exc_info` (an exception becomes an
   `exception` field with the traceback text).
4. Renderer: `JSONRenderer()` when `LOG_JSON` is true (the default),
   otherwise the coloured console renderer. Filtering by
   `make_filtering_bound_logger(LOG_LEVEL)`; output by `PrintLoggerFactory`
   (stdout); `cache_logger_on_first_use=True`, so the configuration must
   run before the first log call (it is the first line of every lifespan).

One rule for every log call in the project: never pass a keyword argument
called `event`. structlog's methods take the event name as their first
argument, which is named `event`, so `logger.info("x", event=...)` raises
`TypeError: got multiple values for argument 'event'`. Use `event_name=`
instead (the event code does).

A typical line from core-service:

```json
{"event": "market_close_job_completed", "...": "...", "service": "core-service", "level": "info", "level_number": 20, "timestamp": "2026-10-01T21:30:04.512345Z"}
```

Modules get their logger with `logger = structlog.get_logger(__name__)`
and log an **event name** plus keyword fields. Event names are stable
identifiers you can grep for; the other chapters list the important ones
(for example `durable_event_dead_lettered`, `websocket_send_failed`,
`scheduled_job_skipped`).

Two kinds of context fields are added automatically through
`merge_contextvars`:

- `request_id` on every line logged while an HTTP request is handled, in
  all three services (`shared/logging.py::RequestIdMiddleware`). It is
  the caller's `X-Request-ID` when that matches `[A-Za-z0-9._-]{1,64}`
  (the gateway or a client may set one), otherwise a new `uuid4` hex. The
  middleware stores it in `request.state.request_id`, binds it as a
  structlog context variable for the whole request, and adds it as the
  `X-Request-ID` response header. The same id is in the error body
  (`{"error": {"code", "message", "request_id"}}`), so a user's error
  report leads to the log lines: `grep '"request_id": "<id>"'`. A
  success body wrapped in `data` (`APIResponse`) carries it too, in its
  top-level `request_id`; other success shapes have it only in the
  header. The middleware is pure ASGI (not
  `BaseHTTPMiddleware`), so streaming and SSE responses are not buffered.
  WebSocket connections get no request id.
- `analysis_id` (for the whole run) and `ticker` (once known) on every
  line of an analysis run in ai-service
  (`ai/workflows/analysis_workflow.py::run_analysis_workflow`): agents,
  LLM calls, tools and archival. asyncio copies the context into the
  graph's parallel tasks. One analysis's lines:
  `grep '"analysis_id": "<id>"'`.

Two kinds of lines therefore appear in a service's output: structlog JSON
lines (application events) and plain text lines from standard logging
(for example uvicorn's access log `INFO: 172.18.0.5:41234 - "GET /health
HTTP/1.1" 200 OK`).

`LOG_LEVEL` defaults to `INFO` (`shared/config.py`, `.env.example`). The
development override `docker-compose.override.yml` sets `LOG_LEVEL: "DEBUG"`
for ai-service only, so its debug events (for example
`document_status_changed_published`, `cross_service_event_published`) are
visible there.

### 3.2 Docker log caps

`docker-compose.yaml` defines one anchor and every service uses it:

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"
```

So Docker keeps at most 5 files of 20 MB per container (100 MB). Without
the cap, the json-file logs would grow until the container was recreated,
on the same fixed-size Docker disk that also holds PostgreSQL and the
backups.
`docker compose logs` therefore only shows recent history; the long-term
copy is Vector's.

### 3.3 Vector: `infra/vector/vector.toml`

Why: `docker compose logs` shows only what Docker still holds, and that is
lost when a container is recreated (`up -d --build`, `--force-recreate`).
A failing overnight batch could not be investigated the next morning. Vector
reads straight from the Docker daemon, independent of the application.

Pipeline:

1. `[sources.docker]` `type = "docker_logs"`, `docker_host =
   "unix:///var/run/docker.sock"` (mounted read-only),
   `auto_partial_merge = true` (re-joins lines Docker split into 16 KiB
   pieces, so one original line stays one line).
2. `[transforms.prepare]` (VRL remap, `drop_on_abort = true`):
   - `service` = the compose label `com.docker.compose.service`; lines of
     a container without that label are dropped (`abort`). Compose
     services keep their lines, including `docker compose run` containers
     (they carry the label); containers not started by compose
     (testcontainers from `make test`, `docker run` one-offs with random
     names) get no folder under `./logs`;
     characters outside `[A-Za-z0-9_.-]` replaced by `_`; `.` or `..`
     becomes `unknown` (so the name can never point outside the log
     folder);
   - lines from `vector` itself are dropped (`abort`), so Vector's own
     warnings cannot loop back as new events;
   - lines of another compose project on the same Docker daemon are
     dropped: the label `com.docker.compose.project` must equal the
     environment variable `COMPOSE_PROJECT`, which `docker-compose.yaml`
     sets to the project's own name (`${COMPOSE_PROJECT_NAME}`). The
     restore drill (project `alphamind-drill`) has services with the same
     names and would otherwise write into the live files. When the
     variable is empty the check is skipped;
   - the new message is `"<timestamp> <stream> <original line>"`, where the
     timestamp is Docker's recorded time (the current time if missing)
     formatted in **America/New_York** (`%Y-%m-%dT%H:%M:%S%.3f%:z`) and
     `stream` is `stdout` or `stderr` (`-` if unknown);
   - `log_date` = the New York calendar date of the line.
3. `[sinks.files]` `type = "file"`, `path =
   "/logs/{{ log_service }}/{{ log_date }}.log"`, `encoding.codec = "text"`
   (only the message, byte for byte; JSON encoding would escape every quote
   of the services' own JSON lines), `idle_timeout_secs = 60` (yesterday's
   file is closed after 60 s without writes, so the cleanup never races an
   open file). The sink creates the folders on the first write.

On the host: `./logs/<service>/<YYYY-MM-DD>.log`, for example
`./logs/core-service/2026-10-01.log`. A line looks like:

```
2026-10-01T17:30:04.513-04:00 stdout {"event": "market_close_job_completed", ..., "service": "core-service", "level": "info", ...}
```

Why the prefix: some processes (uvicorn access logs, the scheduler's shell
scripts) print lines without a timestamp. Why New York dates: the same
zone as the scheduler, so "today's file" matches the trading day.

Retention: `infra/scheduler/logs-cleanup.sh` at 04:00 New York deletes
`*.log` files older than 14 days (see
[26-scheduler-jobs.md](26-scheduler-jobs.md)).

`api.enabled = false`: Vector's own HTTP API is off. `VECTOR_LOG: warn`
keeps Vector itself quiet.

### 3.4 Prometheus: `infra/prometheus/prometheus.yml`

- `scrape_interval: 15s`, `evaluation_interval: 15s` (rules are
  evaluated every 15 s).
- `rule_files: [/etc/prometheus/alert_rules.yml]` (mounted read-only by
  compose).
- `alerting.alertmanagers: alertmanager:9093`.
- Image `prom/prometheus:v3.13.2`, UI only on `127.0.0.1:9090`, data in the
  `prometheus_data` volume (no retention flag is set, so Prometheus' default
  applies).

Scrape jobs (each adds a `service` label used by dashboards and alerts):

| Job | Target | `service` label | What it exposes |
|---|---|---|---|
| `auth-service` | `auth-service:8000/metrics` | `auth-service` | HTTP metrics |
| `core-service` | `core-service:8000/metrics` | `core-service` | HTTP metrics + job health gauges |
| `ai-service` | `ai-service:8000/metrics` | `ai-service` | HTTP metrics + analysis/LLM metrics |
| `postgres` | `postgres-exporter:9187` | `postgres` | PostgreSQL (`pg_up`, `pg_stat_database_*`, ...) |
| `redis` | `redis-exporter:9121` | `redis` | Redis (`redis_up`, memory, commands, keys) |
| `alertmanager` | `alertmanager:9093` | `alertmanager` | Alertmanager itself |
| `prometheus` | `localhost:9090` | `prometheus` | Prometheus itself |

`/metrics` is not reachable from outside: the gateway answers 404 for
`location = /metrics` and for anything it does not route
(`infra/gateway/nginx.conf`).

### 3.5 HTTP metrics: prometheus-fastapi-instrumentator

Each service's `main.py`:

```python
Instrumentator().instrument(
    app, latency_lowr_buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30)
).expose(app, endpoint="/metrics")
```

The library's default metrics:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `http_requests_total` | counter | `handler`, `status`, `method` | requests; `status` is grouped (`2xx`, `4xx`, `5xx`) |
| `http_request_duration_seconds` | histogram | `handler`, `method` | latency with the buckets above |
| `http_request_duration_highr_seconds` | histogram | none | latency with many buckets |
| `http_request_size_bytes` / `http_response_size_bytes` | summary | `handler` | body sizes |

`handler` is the route template (for example `/api/v1/analyses/{analysis_id}`),
so ids do not create new series. The custom buckets exist because the
library's default (0.1, 0.5, 1 s) cannot show anything slower than 1 s,
and the p95 panel and the `HighLatencyP95` alert use a 2 s threshold.

### 3.6 Custom AlphaMind metrics

Searched in the code (`Gauge`, `Counter`): all custom metrics live in
three files (`services/ai-service/observability.py`,
`services/core-service/job_health.py`, `services/core-service/outbox.py`)
and use the default `prometheus_client` registry, so they appear on the
same `/metrics` endpoint that the instrumentator exposes; no extra
endpoint is needed.

`services/ai-service/observability.py`:

| Metric | Type | Labels | Set by |
|---|---|---|---|
| `alphamind_analyses_total` | counter | `status` (`completed`/`failed`), `failure_reason` (an `AnalysisFailureReason` value, `none` for completed, `other` for unknown) | `record_analysis_completed`, `record_analysis_failed` (analysis workflow and queue) |
| `alphamind_analysis_queue_pending` | gauge | none | read at scrape time from the serial queue (`bind_analysis_queue`, `set_function`) |
| `alphamind_analysis_queue_running` | gauge | none | 1 while an analysis runs, else 0 |
| `alphamind_llm_errors_total` | counter | `provider`, `kind` (`rate_limit`, `server_overload`, `timeout`, `other`) | `record_llm_error`, one per failed router-level attempt |

Details:

- The two counters are registered as `alphamind_analyses` and
  `alphamind_llm_errors`; `prometheus_client` adds `_total` to a counter's
  name.
- `alphamind_analyses_total` counts analyses that reached a terminal state
  *in this process*, so it starts at 0 after a restart (PromQL's
  `increase` handles the reset). The list of known failure reasons is
  built from the `AnalysisFailureReason` enum (`KNOWN_FAILURE_REASONS`),
  so a new reason automatically gets its own label value instead of
  being counted as `other`.
- The queue gauges use `set_function`: the value is read from the queue
  object at scrape time, so there is no bookkeeping that could drift.
- `alphamind_llm_errors_total` is counted by the LLM router, once per
  router-level attempt that raised; the adapters' own inner retries over
  API keys are not counted separately. `llm_error_kind` maps the
  exception: request or provider timeout -> `timeout`, server overloaded
  (503) -> `server_overload`, rate limit (429) -> `rate_limit`, anything
  else -> `other`. The order of the checks matters, because the timeout
  types are subclasses of the overload and request error types.
  `record_llm_error` never raises: a metrics problem must not change LLM
  routing.
- The label values are bounded on purpose (failure reasons from an enum,
  four error kinds): unbounded labels would create unlimited series.

`services/core-service/job_health.py`:

| Metric | Type | Labels | Set by |
|---|---|---|---|
| `alphamind_job_last_success_timestamp_seconds` | gauge | `scheduled_job` (the seven scheduler jobs; see below) | `record_job_success` on each heartbeat; reloaded from Redis `core:job_last_success` at startup; a job with no success yet gets its tracking start from `core:job_tracked_since` |
| `alphamind_market_close_portfolios_skipped` | gauge | none | `record_market_close_skipped`, called by the `market_close` endpoint after each trading-day run; stored in Redis `core:market_close_skipped` and reloaded at startup |

`services/core-service/outbox.py`:

| Metric | Type | Labels | Set by |
|---|---|---|---|
| `alphamind_outbox_unpublished_events` | gauge | none | the outbox dispatcher, every 5 s: rows of `core.event_outbox` with `published_at IS NULL` ([24-events.md](24-events.md), section 3.5) |

These files create a collector only if one with that name does not exist
yet, so importing a module twice (as tests do) does not raise "Duplicated
timeseries". core-service and ai-service both use
`shared/logging.py::get_or_create(cls, name, doc, labels)`. It looks the
name up in the default registry, which lists a counter under both
`<name>` and `<name>_total`.

**Why the label is `scheduled_job` and not `job`.** Prometheus adds a
`job` label to every series it scrapes. Its value is the scrape job's name
from `prometheus.yml`, here `core-service`. When a scraped metric has its
own label with the same name, Prometheus keeps its own by default and
renames the scraped one to `exported_job` (only the scrape option
`honor_labels: true` would keep the service's value, and `prometheus.yml`
does not set it). Alert rules that filtered on `job` would then never
match. So the heartbeat gauge's label is `scheduled_job`. The stored
series is:

```
alphamind_job_last_success_timestamp_seconds{scheduled_job="backup", job="core-service", instance="core-service:8000", service="core-service"}
```

To select one scheduled job in a query, filter on `scheduled_job`.
`scripts/health.sh` reads `scheduled_job`.

### 3.7 Exporters

- `postgres-exporter` (`prometheuscommunity/postgres-exporter:v0.17.1`):
  connects with `DATA_SOURCE_URI=postgres:5432/<db>?sslmode=disable` and
  the user/password from `.env.docker` (split form, so special characters
  in the password need no URL escaping). No host port. Key metrics:
  `pg_up`, `pg_stat_database_numbackends`, `pg_stat_database_xact_commit`,
  `pg_database_size_bytes`, `pg_stat_database_deadlocks`.
- `redis-exporter` (`oliver006/redis_exporter:v1.74.0`):
  `REDIS_ADDR=redis://redis:6379` (Redis has no password in this stack). No
  host port. Key metrics: `redis_up`, `redis_memory_used_bytes`,
  `redis_commands_processed_total`, `redis_connected_clients`,
  `redis_db_keys`.

### 3.8 Alert rules: `infra/prometheus/alert_rules.yml`

Every alert has `severity` (`critical` or `warning`) and a `service`
label (from the scrape or set in the rule).

**Group `availability`**

| Alert | Expression | For | Severity |
|---|---|---|---|
| `ServiceDown` | `up == 0` | 1m | critical |
| `PostgresDown` | `pg_up == 0` | 1m | critical (`service: postgres`) |
| `RedisDown` | `redis_up == 0` | 1m | critical (`service: redis`) |

`up` is 0 when Prometheus cannot scrape a target. `pg_up`/`redis_up` are 0
when the exporter runs but cannot reach the database.

**Group `http`**

| Alert | Expression (summary) | For | Severity |
|---|---|---|---|
| `High5xxRate` | per service, 5xx share of `rate(http_requests_total{handler!~"/metrics\|/health"}[5m])` > 5%, AND that rate > 0.05 req/s | 5m | warning |
| `HighLatencyP95` | `histogram_quantile(0.95, ...)` of `http_request_duration_seconds_bucket` over 10m > 2 s, excluding handlers `/internal/.*`, `/metrics`, `/health` | 10m | warning |

Both HTTP alerts leave out the `/metrics` scrapes and `/health` checks:
Prometheus scrapes every 15 s and the Docker healthcheck calls `/health`
every 15 s, about 0.13 req/s per idle service. Counted as traffic, they
would always meet the minimum and dilute the 5xx share. The minimum of
0.05 real requests per second stops one failed request on an idle service
from firing. Internal scheduler jobs are slow by design and excluded from
the latency alert. Creating an analysis or a backtest only stores the row
and starts the work in the background, so both stay included.

**Group `ai-pipeline`**

| Alert | Expression (summary) | For | Severity |
|---|---|---|---|
| `AnalysisFailureRateHigh` | failed / all `alphamind_analyses_total` increase over 1h > 0.5, AND at least 3 finished | 5m | warning |
| `AnalysisQueueBacklog` | `alphamind_analysis_queue_pending > 20`, OR pending > 0 `unless` any analysis finished in 30m | 30m | warning |
| `LLMProviderErrorsHigh` | `sum by (provider, kind) (increase(alphamind_llm_errors_total[15m])) > 30` | 5m | warning |

The queue is serial and one analysis takes about 1.5 to 4 minutes (up to
the 45-minute hard limit when a filing must be summarised first), so a
15-ticker batch legitimately keeps jobs queued for 30-40 minutes. The
backlog alert therefore fires only for a very large queue (more than 20)
or a queue that has not finished a single analysis in 30 minutes.
`unless` also covers a counter with no samples yet. `AnalysisFailureRateHigh`
needs at least 3 finished analyses in the hour, so one failure alone never
fires it.

`LLMProviderErrorsHigh` (more than 30 errors of one provider and kind in
15 minutes) is expected during waves of Google 503 answers on the
HIGHEST-tier models (on bad days about 70% of first attempts on the newest
Flash model fail). The fallback model and the step retries usually still
complete every analysis, so first check whether analyses complete
(`alphamind_analyses_total{status="completed"}`) before treating it as an
outage. The fallback model and the retries are described in
[14-ai-llm-layer.md](14-ai-llm-layer.md).

`ServiceDown` names the target by its scrape `job` and `instance`;
`PostgresDown` and `RedisDown` mean the exporter runs but cannot reach the
database. `MarketCloseSkippedPortfolios` has no `for:` and fires at the
next evaluation.

**Group `scheduled_jobs`** (see [26-scheduler-jobs.md](26-scheduler-jobs.md))

| Alert | Expression (summary) | For | Severity |
|---|---|---|---|
| `DailyJobStale` | `time() - alphamind_job_last_success_timestamp_seconds{scheduled_job=~"backup\|review-run\|rag-ingest\|logs-cleanup\|purge-unverified\|notification-retention"} > 26 * 3600` | 10m | warning (`service: scheduler`) |
| `TradingDayJobStale` | same for `market-open\|market-close\|tier-monitoring`, `> 100 * 3600` | 10m | warning (`service: scheduler`) |
| `MarketCloseSkippedPortfolios` | `alphamind_market_close_portfolios_skipped > 0` | none | warning (`service: core-service`) |

The two stale-job alerts filter on `scheduled_job` and name the job with
`{{ $labels.scheduled_job }}` in their text. A scheduler run in which every
unit of work failed answers 500 and sends no heartbeat, so it also ends in
these alerts ([26-scheduler-jobs.md](26-scheduler-jobs.md) section 3.4). The trading-day limit is 100
hours because a closed-market weekday still reports success (the job
answers "skipped"), so the longest normal gap is a weekend (Friday 17:30
to Monday 17:30, 72 hours).

**Group `durable_events`** (see [24-events.md](24-events.md))

| Alert | Expression | For | Severity |
|---|---|---|---|
| `OutboxBacklog` | `alphamind_outbox_unpublished_events > 0` | 15m | warning (`service: core-service`) |

The outbox dispatcher retries every 5 seconds, so events that stay
unpublished for 15 minutes mean Redis (or the dispatcher) has been failing
for a while. The text points to the `outbox_publish_failed` log lines.

Annotations (`summary`, `description`) use templates such as
`{{ $labels.service }}` and `{{ $value | humanizePercentage }}`; they become
the email text.

### 3.9 Alert rule tests: `infra/prometheus/alert_rules_test.yml`

`promtool test rules` feeds fake series, evaluates the rules at given times
and compares the alerts (labels and rendered annotations) with the
expected ones. The file (`evaluation_interval: 1m`) has seven test groups:

1. `up` of core-service goes to 0 and `pg_up` to 0 -> at 5m `ServiceDown`
   and `PostgresDown` fire with the expected labels and text.
2. ai-service at 10 req/s with 20% 5xx -> at 12m `High5xxRate` fires with
   "answered 20% of its requests".
3. An idle core-service: `/metrics` and `/health` at 4 req/min each plus
   one real request per minute that fails -> no `High5xxRate` at 12m
   (the probes are not traffic, 1 req/min is below the minimum).
4. Every `POST /api/v1/backtests` of ai-service takes 2-5 s, and so does
   core-service's `/internal/scheduler/market-close` -> at 25m
   `HighLatencyP95` fires for ai-service only, with "has been 4.85s"
   (2 + 3 x 0.95, linear interpolation inside the 2-5 s bucket).
5. 3 jobs pending, nothing finished -> no `AnalysisQueueBacklog` at 20m
   (still within `for: 30m`), firing at 65m.
6. Job gauges stuck at time 0: `DailyJobStale` for `backup` silent at 20h
   and firing at 30h ("last succeeded 1d 6h 0m 0s ago");
   `TradingDayJobStale` for `market-close` silent at 30h (below 100h);
   `MarketCloseSkippedPortfolios` firing at 100m with value 2. The input
   series carry the labels a real scrape stores: `job="core-service"` and
   the job name in `scheduled_job`.
7. `alphamind_outbox_unpublished_events` at 3 from minute 11 to minute 41:
   no `OutboxBacklog` at 20m, firing at 30m ("3 durable event(s) not yet
   published"), silent again at 50m.

`RedisDown`, `AnalysisFailureRateHigh` and `LLMProviderErrorsHigh` have
no test case in this file.

Run both checks with `make promtool-test`. It uses the same image as
compose (`PROMETHEUS_IMAGE := prom/prometheus:v3.13.2`) with `promtool` as
the entrypoint, mounts `infra/prometheus` read-only, and runs
`promtool check rules alert_rules.yml` (syntax) and
`promtool test rules alert_rules_test.yml` (behaviour).

### 3.10 Alertmanager -> MailHog: `infra/alertmanager/alertmanager.yml`

- `global`: `smtp_smarthost: "mailhog:1025"`, `smtp_from: "AlphaMind
  Alertmanager <alertmanager@alphamind.local>"`, `smtp_require_tls: false`,
  `resolve_timeout: 5m`.
- `route`: one receiver `email`; `group_by: ["alertname", "service"]`;
  `group_wait: 30s` (alerts that fire together arrive in one email);
  `group_interval: 5m`; `repeat_interval: 4h` (a still-firing alert is
  re-sent every 4 hours).
- `receivers`: `email` to `alerts@alphamind.local` with
  `send_resolved: true` (an email also when the alert clears).
- `inhibit_rules`: while a `severity="critical"` alert fires for a
  `service`, `severity="warning"` alerts of the same `service` are not
  sent (a down service also causes latency and error warnings).

Alertmanager does not expand environment variables in this file, so the
addresses are fixed values. For a real inbox, edit
`receivers[0].email_configs[0].to` (several addresses: comma separated)
and restart the container
(`docker compose --env-file .env.docker restart alertmanager`). For a real
SMTP provider, change `smarthost`, `auth_username` and `auth_password` (or
`auth_password_file`) and set `require_tls: true`; never commit real
credentials. Check the file with `amtool check-config
infra/alertmanager/alertmanager.yml`. Image `prom/alertmanager:v0.28.1`, UI on `127.0.0.1:9093`.
MailHog (`mailhog/mailhog:v1.0.1`) shows the emails at
`http://127.0.0.1:8025`.

### 3.11 Grafana: `infra/grafana/provisioning`

- Image `grafana/grafana:13.1.1`. No host port: served only through the
  gateway at `https://localhost:8443/grafana/` (`GF_SERVER_ROOT_URL`,
  built by Compose from `OAUTH_REDIRECT_BASE_URL` plus `/grafana/`, so it
  follows the public address; `GF_SERVER_SERVE_FROM_SUB_PATH: "true"`; nginx `location /grafana/`
  passes the prefix through and forwards the `Upgrade` headers for Grafana
  Live). `/grafana` redirects to `/grafana/`.
- Login required: `GF_AUTH_ANONYMOUS_ENABLED: "false"`,
  `GF_USERS_ALLOW_SIGN_UP: "false"`, admin password from
  `GRAFANA_ADMIN_PASSWORD` in `.env.docker` (compose refuses to start
  without it). The password applies only when the `grafana_data` volume is
  first created; afterwards reset it with `grafana cli admin
  reset-admin-password`.
- `datasources/datasource.yml`: one datasource `Prometheus`, fixed
  `uid: Prometheus`, `url: http://prometheus:9090`, `access: proxy`,
  default, not editable. A same-named datasource from an older volume is
  deleted first (`deleteDatasources`) so it is recreated with the fixed uid
  that the dashboards reference.
- `dashboards/dashboard.yml`: provider `AlphaMind`, reads JSON files from
  `/etc/grafana/provisioning/dashboards` every 30 s (`allowUiUpdates:
  true`: a change made in the UI is possible but is overwritten by the
  file on the next reload, so change the JSON file instead).

The two dashboards:

| Dashboard (uid) | Panels |
|---|---|
| AlphaMind - Service Overview (`alphamind-overview`) | request rate by service; p95 latency by service; 5xx rate by service |
| AlphaMind - Infrastructure & AI pipeline (`alphamind-infra`) | scrape targets up; firing alerts table (`ALERTS{alertstate="firing"}`); analysis queue (pending, running); analyses finished in 24h by status; failed analyses by reason per hour; LLM errors by provider and kind; ai-service p95 by handler; PostgreSQL up, connections, transactions/s, database size, deadlocks, cache hit ratio; Redis up, memory, commands/s, clients, keys per DB, keyspace hit ratio |

There is no dashboard panel for the job heartbeat gauges; use
`scripts/health.sh` or the Prometheus UI for them.

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` (ai-service `DEBUG` in development) | minimum log level | `shared/config.py`, `.env.example`, `docker-compose.override.yml` |
| `LOG_JSON` | `true` | JSON renderer vs console renderer | `shared/config.py` |
| `SERVICE_NAME` | per service | `service` field in every line | `services/*/config.py` |
| json-file `max-size` / `max-file` | `20m` / `5` | Docker log cap per container | `docker-compose.yaml` (`x-logging`) |
| Vector file path | `/logs/<service>/<YYYY-MM-DD>.log` | daily files, New York date | `infra/vector/vector.toml` |
| `idle_timeout_secs` | `60` | close idle log files | `infra/vector/vector.toml` |
| log retention (`RETENTION_DAYS`) | `14` | days kept by logs-cleanup | `infra/scheduler/logs-cleanup.sh` |
| `scrape_interval` / `evaluation_interval` | `15s` / `15s` | Prometheus timing | `infra/prometheus/prometheus.yml` |
| latency buckets | `0.1, 0.25, 0.5, 1, 2, 5, 10, 30` s | `http_request_duration_seconds` | `services/*/main.py` |
| `group_wait` / `group_interval` / `repeat_interval` | `30s` / `5m` / `4h` | Alertmanager timing | `infra/alertmanager/alertmanager.yml` |
| alert recipient | `alerts@alphamind.local` | MailHog only | `infra/alertmanager/alertmanager.yml` |
| `GRAFANA_ADMIN_PASSWORD` | must be set | Grafana admin login | `.env.example`, `docker-compose.yaml` |
| Prometheus / Alertmanager / MailHog UIs | `127.0.0.1:9090` / `:9093` / `:8025` | local-only ports | `docker-compose.yaml` |

## 5. Design decisions and why

**JSON lines with event names.** Alternative: free-text messages.
Reason: a stable event name plus fields can be grepped exactly
(`grep '"event": "durable_event_dead_lettered"'`) and parsed later, and
fields like `analysis_id` connect lines across services.

**Vector to files, not a log database.** Alternatives: Loki or
Elasticsearch. Reason: files on the host survive container recreation,
need no extra service with its own storage and queries, and `grep` is
enough at this scale. The text codec keeps the original JSON readable.

**New York dates in file names.** The scheduler runs in New York time;
one file per trading day makes "what happened at market-close yesterday"
one file.

**Docker log caps plus Vector.** The cap protects the shared Docker disk;
Vector keeps the long history outside Docker.

**Silencing noisy libraries.** Not only volume: those libraries leaked
secrets (API keys in URLs, S3 key ids in headers). Setting them to WARNING
removes the leak at the source.

**Bounded metric labels.** Failure reasons come from an enum and error
kinds are four fixed strings, so the number of series stays small.

**Heartbeat gauges in core-service.** The scheduler has no metrics
endpoint; core-service already is a scrape target and has Redis to keep
the values across restarts.

**Alert thresholds with minimum volume and long `for`.** A single failed
request or a normal market-open queue must not page anybody; alerts aim at
sustained problems.

**Unit-tested alert rules.** A wrong PromQL expression fails silently (it
just never fires). `promtool test rules` proves each tested rule fires
when it should and stays quiet when it should. Its limit: the test only
sees the series you write into it. A test series with a label that a real
scrape renames (a metric label `job` becomes `exported_job`, section 3.6)
lets a rule pass its test and still match no live data. So the test series
carry the labels a real scrape stores (`job="core-service"` plus
`scheduled_job`). A query in the live Prometheus UI is still the check
that catches this kind of mistake.

**Everything local and behind the gateway.** Prometheus, Alertmanager and
MailHog bind to `127.0.0.1`; Grafana is only reachable through the TLS
gateway with a login; `/metrics` is 404 at the gateway.

## 6. When things go wrong

| Problem | Where to look |
|---|---|
| A service is down | `ServiceDown` firing; `docker compose --env-file .env.docker ps`; `bash scripts/logs.sh <service>` |
| No `./logs` files for today | Vector not running or cannot read the Docker socket: `docker compose --env-file .env.docker logs vector`; validate: `docker compose --env-file .env.docker run --rm --no-deps vector validate --no-environment /etc/vector/vector.toml` |
| Old logs gone | expected after 14 days (logs-cleanup); `docker compose logs` keeps at most 100 MB per container |
| A target shows DOWN in Prometheus | `http://127.0.0.1:9090/targets` shows the scrape error |
| Grafana panels say "No data" | datasource uid must be `Prometheus`; check the target is up; check the time range |
| No alert email | Alertmanager UI `http://127.0.0.1:9093` (is the alert there?), MailHog `http://127.0.0.1:8025`; an inhibited warning is not sent while a critical alert of the same service fires |
| An alert never fires although it should | `make promtool-test`; then paste the expression into the Prometheus query page: if it returns no series even with the threshold removed, a label filter is wrong (for example a filter on `job` where the series carries the name in another label, section 3.6) |
| Logs contain secrets | check the logger is in the noisy list; never log full URLs of providers that put keys in query strings |
| Job heartbeat missing | `bash scripts/health.sh`; `bash scripts/redis-cli.sh -n 0 HGETALL core:job_last_success`; in Prometheus filter on `scheduled_job`, not `job` |
| Durable events not reaching ai-service; alert `OutboxBacklog` | core logs `outbox_publish_failed`; `bash scripts/psql.sh -c "SELECT event_name, created_at, attempts, last_error FROM core.event_outbox WHERE published_at IS NULL ORDER BY created_at;"`; is Redis up ([24-events.md](24-events.md)) |

## 7. Demo walkthrough

All steps are read-only.

1. **Open Prometheus** at `http://127.0.0.1:9090` (local only). Show
   *Status -> Targets*: seven jobs, all UP. Then run three queries in the
   query page:
   ```
   up
   ```
   One series per scrape target with value 1. Say: "0 here for one minute
   is the ServiceDown alert."
   ```
   sum by (service) (rate(http_requests_total[5m]))
   ```
   Requests per second per service (Graph tab).
   ```
   (time() - alphamind_job_last_success_timestamp_seconds) / 3600
   ```
   Hours since each scheduled job last succeeded. Point at the labels:
   the job name is in `scheduled_job`, and `job` is `core-service`. One job
   only:
   ```
   (time() - alphamind_job_last_success_timestamp_seconds{scheduled_job="backup"}) / 3600
   ```
   Say: "The rules alert above 26 hours for a daily job and 100 hours for
   a trading-day job."
   Optional fourth: `sum by (status, failure_reason) (increase(alphamind_analyses_total[24h]))`.

2. **Show alert states.** Open *Alerts* (`http://127.0.0.1:9090/alerts`).
   Each rule is *inactive* (green), *pending* (condition true, waiting for
   `for`) or *firing*. Usually all are inactive. If one fires, open
   Alertmanager at `http://127.0.0.1:9093` and MailHog at
   `http://127.0.0.1:8025` to show the email. From the terminal:
   ```
   make health
   ```
   The last section lists firing alerts ("none" when healthy).

3. **Run the alert rule tests** (needs Docker; read-only):
   ```
   make promtool-test
   ```
   Output: `SUCCESS` for the syntax check and for the unit tests.

4. **Open Grafana** at `https://localhost:8443/grafana/` (log in as
   `admin` with the password from `.env.docker`; do not show it on
   screen). Open *Dashboards -> AlphaMind - Infrastructure & AI pipeline*
   (direct link `https://localhost:8443/grafana/d/alphamind-infra`). Show:
   scrape targets up, firing alerts table, analysis queue, failed analyses
   by reason, PostgreSQL and Redis rows. Then *AlphaMind - Service
   Overview* for request rate, p95 and 5xx per service.

5. **Grep a log event in `./logs`.**
   ```
   ls logs/
   ls logs/core-service/ | tail -3
   bash scripts/logs.sh core-service grep durable_event_subscriber_started
   bash scripts/logs.sh scheduler grep 'success reported'
   grep -h '"event": "market_close_job_completed"' logs/core-service/*.log | tail -2
   ```
   Point at the New York timestamp prefix, `stdout`, and the JSON body
   with `event`, `service`, `level`, `timestamp` (UTC). `scripts/logs.sh
   <service> grep <pattern>` searches only today's file (New York date).
   `durable_event_subscriber_started` is logged when a service starts, so
   if the service has not restarted today, add the date of its last start
   as a fourth argument (for example `... grep durable_event_subscriber_started 2026-10-01`).
   Say: "The prefix is
   Vector's (New York time), the JSON is the service's own line (UTC)."
   To follow live: `bash scripts/logs.sh ai-service` (Ctrl-C to stop).

6. **Show a raw metrics endpoint from inside the network** (the gateway
   blocks it):
   ```
   docker compose --env-file .env.docker exec -T core-service python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())" | grep '^alphamind_'
   curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/metrics
   ```
   The first prints AlphaMind's own metrics, among them the job gauges
   (label `scheduled_job`, for example `scheduled_job="backup"`) and
   `alphamind_outbox_unpublished_events`. Compare with step 1: Prometheus
   adds `job="core-service"` and `instance` to the same series. The
   second command prints `404`.

What to say at the end: "Logs tell the story of one event; metrics tell
the trend; alerts tell me when the trend is wrong for long enough. All of
it is configured in files under infra/ and tested where it can be."

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Where do the files in `./logs` come from?**
   Vector reads the output of every docker compose container from the
   Docker API and writes `./logs/<service>/<New York date>.log` (other
   containers, e.g. testcontainers from `make test` or the restore
   drill's project, are skipped); logs-cleanup removes files older
   than 14 days.

2. **Why are `httpx` and `botocore` set to WARNING?**
   At lower levels they log request URLs and headers that contain an API
   key and the S3 access key id, and they are very noisy.

3. **What is the difference between `http_requests_total` and `alphamind_analyses_total`?**
   The first is the library's per-request counter (handler, method,
   grouped status); the second is AlphaMind's counter of finished analyses
   by status and failure reason.

4. **Why does `High5xxRate` also require more than 0.05 req/s?**
   So a single 5xx on an idle service does not fire an alert.

5. **What does inhibition do here?**
   While a critical alert for a service fires, warnings of the same
   service are not emailed.

6. **How are alert rules tested?**
   `make promtool-test`: `promtool check rules` and `promtool test rules`
   with fake series in `alert_rules_test.yml`.

7. **Why does Grafana's datasource have a fixed uid?**
   The provisioned dashboards reference the datasource by uid
   `Prometheus`; a fixed uid keeps them working on every install.

8. **How do you see `/metrics` of core-service?**
   From inside the Docker network (Prometheus, or a request from inside a
   container); the gateway returns 404.

9. **Why is the heartbeat gauge's label called `scheduled_job` and not
   `job`?**
   A real scrape adds `job="core-service"` to every series and, without
   `honor_labels`, renames a metric's own `job` label to `exported_job`.
   Rules filtering on `job` would never match live data, and unit tests
   whose series still carry `job` would not notice. With `scheduled_job`
   there is no clash, and the tests carry both labels.

10. **`LLMProviderErrorsHigh` fires during a batch. Is it an outage?**
    Not necessarily. Google's HIGHEST-tier models often answer 503 on
    first attempts, and the router's fallback model and the step retries
    usually still finish every analysis. Check `alphamind_analyses_total{status="completed"}`
    and `AnalysisFailureRateHigh` first.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `shared/logging.py` | `configure_logging`: structlog processors, JSON renderer, noisy loggers; `RequestIdMiddleware`, `X-Request-ID`; `get_or_create` for Prometheus collectors |
| `shared/config.py` | `LOG_LEVEL`, `LOG_JSON` |
| `docker-compose.yaml` | `x-logging` caps; prometheus, alertmanager, exporters, grafana, mailhog, vector services |
| `docker-compose.override.yml` | `LOG_LEVEL: "DEBUG"` for ai-service in development |
| `infra/vector/vector.toml` | Docker log source, remap, daily file sink |
| `infra/scheduler/logs-cleanup.sh` | 14-day retention |
| `infra/prometheus/prometheus.yml` | scrape jobs, rule file, Alertmanager target |
| `infra/prometheus/alert_rules.yml` | all alert groups and rules |
| `infra/prometheus/alert_rules_test.yml` | promtool unit tests |
| `infra/alertmanager/alertmanager.yml` | SMTP to MailHog, routing, inhibition |
| `infra/grafana/provisioning/datasources/datasource.yml` | Prometheus datasource |
| `infra/grafana/provisioning/dashboards/*.json`, `dashboard.yml` | the two dashboards and their provider |
| `services/*/main.py` | `Instrumentator().instrument(...).expose(app, endpoint="/metrics")` |
| `services/ai-service/observability.py` | analysis and LLM metrics |
| `services/core-service/job_health.py` | job heartbeat and market-close gauges |
| `services/core-service/outbox.py` | `alphamind_outbox_unpublished_events` |
| `scripts/health.sh`, `scripts/logs.sh` | operator views of alerts, jobs and logs |
| `Makefile` (`promtool-test`) | rule checks |

## 10. Related chapters

- [02-operations.md](02-operations.md): daily operation and the operator scripts.
- [26-scheduler-jobs.md](26-scheduler-jobs.md): heartbeats and logs-cleanup.
- [24-events.md](24-events.md) and [25-realtime-yjs.md](25-realtime-yjs.md): the log events to grep for event and WebSocket problems.
- [14-ai-llm-layer.md](14-ai-llm-layer.md): where LLM errors are counted.
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): analysis outcomes and failure reasons.
- [10-gateway.md](10-gateway.md): `/grafana/` and the blocked `/metrics`.
- [28-email.md](28-email.md): MailHog.
- [31-testing-quality.md](31-testing-quality.md): `make promtool-test` among the other checks.
