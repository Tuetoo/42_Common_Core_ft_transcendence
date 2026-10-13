# 23. Redis

> Redis is AlphaMind's fast shared memory. It is an in-memory key-value
> server that all three backend services use for short-lived data that
> several processes must agree on: caches of answers from other services,
> rate-limit counters, a live copy of every user's role and active flag,
> one-time markers, a few locks, the pub/sub channels for live updates,
> and the Redis Streams that carry the events that must never be lost.
> Redis 7 runs here with the append-only file turned on (`appendonly yes`,
> `appendfsync everysec`), so those durable events survive a crash. This
> chapter lists every use with its key pattern and lifetime, explains the
> logical databases, persistence and failure behaviour, and shows how to
> inspect Redis in a demo. The meaning of each event is left to
> [24-events.md](24-events.md).

---

## 1. The big picture

```
                          redis:7-alpine  (redis-server --appendonly yes --appendfsync everysec)
                          volume redis_data -> /data   (AOF + RDB files)
   +---------------------------------------------------------------------------------+
   | DB 0  cache        org roles, holdings, FX rates, API key validation,            |
   |                    user/recommendation lookups, OAuth state, job heartbeats,     |
   |                    NewsAPI usage log and source catalog, end-to-end lock         |
   | DB 1  rate limit   auth login/register/reset/2FA counters, public API counters,  |
   |                    RAG-assistant counters                                        |
   | DB 2  events       auth:user_state:* mirror, event claims, Yjs active rooms      |
   |                    (+ PUBLISH on channels events:*, ws:fanout, yjs:note:*)       |
   | DB 3  queue        Redis Streams stream:<event>  (consumer group "workers")      |
   +---------------------------------------------------------------------------------+
        ^              ^                 ^                    ^
   auth-service    core-service       ai-service        redis-exporter -> Prometheus
```

Every service uses `shared/redis.py`, which gives one connection pool per
(Redis URL, database number) and four helpers:
`get_cache_client`, `get_rate_limit_client`, `get_events_client`,
`get_queue_client`.

Rule of thumb: **Redis holds nothing that cannot be rebuilt, except the
durable event streams, the job heartbeats and the NewsAPI usage log**. Caches refill, counters
expire, the user-state mirror is rewritten by auth-service at every
start. The streams are the reason persistence is on.

---

## 2. Concepts you need

| Term | Plain explanation | In AlphaMind |
|---|---|---|
| Key-value store | Data is stored under a name (key). You get, set or delete by name. | All entries below. |
| TTL (time to live) | A lifetime on a key; Redis deletes the key when it runs out. `TTL key` shows the seconds left (-1 = no TTL, -2 = no such key). | Caches and counters all have TTLs. |
| Logical database | Redis has numbered databases (0-15) inside one server. A key in DB 1 is invisible from DB 0. Clients choose with `SELECT n` (`redis-cli -n n`). | 0 cache, 1 rate limit, 2 events, 3 queue. |
| `SET ... NX EX` | "Set only if the key does Not eXist, with an EXpiry". Atomic: of two concurrent callers, exactly one succeeds. | One-time markers, locks, rate-limit windows. |
| Pipeline / `MULTI` transaction | Several commands sent together and executed back to back without other clients' commands in between. | The rate-limit counter. |
| Pub/sub | `PUBLISH channel message` delivers the message to every client subscribed **right now**. Nothing is stored. At-most-once. | Live UI updates, cross-replica fan-out. |
| Stream | An append-only log stored in Redis (`XADD`). Entries stay until trimmed. | Durable events. |
| Consumer group | A named reader of a stream that remembers which entries it has delivered. Each entry goes to one consumer of the group. | Group `workers`; consumers `ai-service` and `core-service`. |
| Pending Entries List (PEL) | Entries delivered to a consumer but not yet acknowledged (`XACK`). | Unacked entries are re-delivered after 60 s idle. |
| RDB | A point-in-time snapshot file of the whole dataset, written periodically. | Redis's default snapshots still run. |
| AOF (append-only file) | A log of every write command. On restart Redis replays it. | `--appendonly yes`. |
| `appendfsync everysec` | The AOF is flushed to disk once per second. At most about one second of writes can be lost in a crash. | Chosen trade-off between safety and speed. |
| Fail open / fail closed | On an error, "open" lets the request through, "closed" refuses it. | Rate limits fail open; visibility and org-role lookups fail closed. |

---

## 3. How it works in AlphaMind

### 3.1 The server and persistence

`docker-compose.yaml`:

```
redis:
  image: redis:7-alpine
  command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
  volumes: redis_data:/data
  healthcheck: redis-cli ping  (every 5s, timeout 5s, 10 retries)
```

There is no host port and no password: only containers on the Docker
network can reach Redis.

**Why AOF.** The durable event streams (`document.uploaded`,
`document.deleted`, `portfolio.deleted`, `document.status_changed`) must
survive a crash. Redis's default RDB snapshots are written only every so
often, so with them alone a crash can lose up to an hour of stream
entries: an uploaded document would then never be indexed, a deleted
document's chunks would stay searchable. With AOF and `everysec`, at most about one
second of writes is at risk. `everysec` instead of `always` (fsync after
every write) keeps writes fast; one second is an acceptable window for
this system.

Redis is **not** in the nightly backup (`infra/scheduler/backup.sh`
backs up PostgreSQL, Qdrant, object storage and avatars). Its data lives
in the `redis_data` volume only.

### 3.2 The client: `shared/redis.py`

```python
_get_client(settings, db_index):
    base_url = settings.assemble_redis_url()       # REDIS_URL or redis://<host>:<port>
    url_without_db = <base_url with any "/n" path removed>
    redis.from_url(url_without_db, db=db_index, decode_responses=True,
                   max_connections=20, socket_timeout=15, socket_connect_timeout=15)
```

Two details matter:

- **The database number in `REDIS_URL` is stripped.** `.env.example` has
  `REDIS_URL=redis://redis:6379/0`. `redis.from_url` lets a `/0` in the
  URL win over the `db=` argument, so every helper would silently use
  DB 0: the durable streams would end up in DB 0 while an operator looks
  for them in DB 3, and a working ingestion pipeline would look broken.
  The URL's database part is therefore always removed; the database
  number comes only from the four helpers.
- **`socket_timeout=15`.** The stream reader blocks for up to 5 seconds
  (`XREADGROUP ... BLOCK 5000`). redis-py's default socket timeout is
  also 5 seconds, so the client would give up at the same moment the
  server answers "nothing new", raising `TimeoutError` (which once
  killed the durable consumer loop silently). The timeout is set well
  above the longest block, for all four databases, so a future blocking
  command anywhere cannot hit the same race.

`check_redis_connection` pings DB 0; every service's `/health` includes
`"redis": true/false`. `close_redis_connections` closes all pools at
shutdown.

### 3.3 Caches (DB 0)

A cache stores an answer from a slower source (usually an internal HTTP
call) for a short time. Most caches here treat a Redis error as a cache
miss and go to the source: the org-role cache, the holdings cache, the
user-reference cache, the API key validation cache, the FX-rate cache
(`services/ai-service/api/internal.py`, logs `fx_rate_cache_read_failed` /
`fx_rate_cache_write_failed`) and the latest-recommendation cache
(`services/core-service/clients/ai_client.py`, logs
`latest_recommendation_cache_read_failed` /
`latest_recommendation_cache_write_failed`). A failed cache write is only
logged; the answer is still returned.

| Key | TTL | Holds | Written / read by | File |
|---|---|---|---|---|
| `auth_client:org_roles:<user_id>` | 60 s (`MEMBERSHIP_CACHE_TTL_SECONDS`) | JSON `{org_id: role}` from auth-service `/internal/users/{id}/organizations` | core-service and ai-service (via `organization_roles`); **deleted by auth-service** when a membership changes | `shared/user_state.py` |
| `ai:core_client:holdings:<portfolio_id>` | 30 s | a portfolio's holdings from core-service | ai-service agents | `services/ai-service/clients/core_client.py` |
| `ai:internal:fx_rate:<from>:<to>` | 3600 s | an exchange rate | ai-service internal route | `services/ai-service/api/internal.py` |
| `core:auth_client:user:<user_id>` | 60 s | a user reference from auth-service | core-service | `services/core-service/clients/auth_client.py` |
| `core:ai_client:latest_recommendation:<portfolio_id>:<ticker>` | 10 s | latest recommendation reference | core-service | `services/core-service/clients/ai_client.py` |
| `apikey:valid:<key hash>` | 30 s (`KEY_CACHE_TTL_SECONDS`) | result of validating a public API key | core-service middleware; deleted when a key is renamed or its limit changes; overwritten with a "revoked" marker when the key is revoked (so a racing request cannot re-cache it as valid); a failed write is logged (`api_key_cache_write_failed`) and does not fail the revoke | `services/core-service/apikeys.py` |
| `ai:newsapi:requests` | 25 h (members older than 24 h are removed on every write) | sorted set: one member per request NewsAPI counts against the key (score = unix time). Answers NewsAPI served from its own cache (`X-Cached-Result: true`) and rejected requests are not counted. Not a cache: a usage log. A failed write only logs `newsapi.usage_count_failed` | ai-service NewsAPI provider writes; `scripts/health.sh` reads it (100 per 24 h; a 15-ticker batch needs about 35) | `services/ai-service/ai/providers/newsapi.py` |
| `ai:newsapi:source_catalog` | 7 days; refreshed when older than 24 h (its stored `fetched_at`); each process also keeps a copy for 1 h; after a failed load the next try waits 15 min | NewsAPI's own source catalog (`GET /v2/top-headlines/sources`), used to tell a known outlet from an unknown site in news and web results. A stale copy is served while a refetch is impossible (log `newsapi.source_catalog_stale_served`), because the web fallback runs exactly when NewsAPI is rate-limited. Every failure is open: no catalog means "unknown", never "not in catalog" | ai-service news and web tools | same file |

The organization-role cache shows the invalidation pattern. A user
removed from an organization must lose access at once, not after 60
seconds. auth-service therefore deletes the user's key twice: once before
the commit (so the response never comes before the deletion) and once
after (to drop an entry another service cached from the old data during
the commit). The TTL remains as a backstop if Redis is unreachable. An
auth-service failure during a lookup raises `UpstreamServiceError`, so
callers fail closed. A membership without a role is treated as `viewer`
(read only). An entry that cannot be parsed is logged
(`org_membership_cache.corrupt_entry`) and treated as a miss.

**Market data is not cached in Redis.** Prices and instruments are cached
in PostgreSQL (`ai.instruments`, `ai.price_bars`), see
[20-postgresql.md](20-postgresql.md).

### 3.4 The live user-state mirror (DB 2)

`shared/user_state.py`: an access token keeps the role it was issued
with until it expires. To make a demotion or deactivation take effect
immediately, every service checks the user's live state on each
authenticated request (`USER_STATE_CHECK_ENABLED`, default true).

- Key `auth:user_state:<user_id>`, value JSON
  `{"role": "...", "is_active": true|false}`, in the **events** database.
- auth-service is the only writer (`services/auth-service/user_state_sync.py`).
  It writes every user at startup (`mirror_all_users_safely`, batches of
  500) and after each change of role, active flag or deletion. Its own
  writes have **no TTL**. A deleted user is stored as inactive, never
  removed, so their old tokens stay blocked. `commit_with_state` mirrors
  first and commits second, so a mirror failure rolls the change back.
- Readers (`get_user_state`) read the key; on a miss they call
  auth-service `GET /internal/users/{id}/state` (2 s timeout) and cache
  the answer for `USER_STATE_CACHE_TTL_SECONDS` (300) with `SET NX`: the
  write-back only fills a missing key, so it can never overwrite a state
  auth-service mirrored in the meantime. A user unknown to auth-service
  is returned as inactive; a malformed value counts as a Redis error.
- If Redis and auth-service are both unreachable, the caller gets `None`
  and `shared/security.py` falls back to the token's own claims (logged),
  so an auth outage does not lock everyone out.

### 3.5 Rate limits (DB 1)

**The shared fixed-window counter** (`shared/redis.py::fixed_window_hits`):
for each key, in one `MULTI` transaction:

```
SET key 0 EX <window> NX     # start the window at the first hit
INCR key                     # count this hit
TTL key                      # seconds until the window ends
```

Because the three commands run together, a key can never be left without
an expiry. A Redis error returns `None` and the request is **allowed**
(fail open, logged as `rate_limit_unavailable`): an outage of the counter
store must not lock users out. `rate_limited_response` returns 429 in the
project's error envelope with `Retry-After`.

**auth-service** (`services/auth-service/security.py`): keys
`auth:rl:<scope>:<sha256 of the identity, 32 hex>`. E-mails and IPs are
hashed so raw addresses never sit in Redis. Scopes and limits
(`services/auth-service/config.py`):

| Scope | Limit | Window |
|---|---|---|
| `login:email` / `login:ip` | 10 / 30 | 15 min |
| `register:ip` | 10 | 1 h |
| `reset:email` / `reset:ip` | 3 / 10 | 1 h |
| `resend:email` | 3 | 1 h |
| `invite:org` | 20 | 1 h |
| `friend:user` | 30 friend requests per requester | 1 h |
| `2fa:fail` -> `2fa:lock` | 5 failures in 5 min (a correct code resets) | lock for 15 min |

The 2FA lock uses its own keys: failures count in
`auth:rl:2fa:fail:<hash>` (a correct code deletes it); at the limit `auth:rl:2fa:lock:<hash>` is set
with a 15-minute TTL, and every attempt first checks that TTL.
`RATE_LIMIT_ENABLED` turns all of it off (tests).

**core-service public API** (`services/core-service/apikeys.py`):
for requests with `X-API-Key` under `/api/v1/public`, two counters with a
60-second window: `ratelimit:<api key id>` (limit = the key's
`rate_limit_per_minute`, capped by `PUBLIC_API_RATE_LIMIT_PER_MINUTE`,
default 60) and `ratelimit:owner:<user id>` (all of a user's keys
together, limit `PUBLIC_API_RATE_LIMIT_PER_MINUTE`). Also
`apikey:last_used_gate:<key id>` (`SET NX EX 60`, in the rate-limit
database, unlike the `apikey:valid:*` cache in DB 0): at most one
PostgreSQL update of `last_used_at` per key per minute; skipped when
Redis is down.

**ai-service research assistant** (`services/ai-service/domains/rag/qa.py::check_rate_limit`):
keys `ai:rag_ask:rate:<user>:m:<YYYYMMDDHHMM>` (TTL 120 s) and
`ai:rag_ask:rate:<user>:d:<YYYYMMDD>` (TTL 2 days) in the **rate-limit**
database (DB 1, `get_rate_limit_client`), with
`RAG_ASK_RATE_LIMIT_PER_MINUTE` (10) and `RAG_ASK_RATE_LIMIT_PER_DAY`
(200). Each count is one call to the shared `fixed_window_hits` (one key
per call, so a request refused by the minute limit does not count toward
the day). The window is also part of the key name (the minute or the
UTC day), and the day limit's `Retry-After` is computed from the next UTC
midnight. Fails open too: a Redis error logs
`rag_ask.rate_limit_unavailable` and the question is allowed.

### 3.6 One-time markers and locks

| Key | DB | How | TTL | Purpose | File |
|---|---|---|---|---|---|
| `oauth:state:<state>` | 0 | `SET` (JSON: provider, nonce hash, PKCE verifier) then `GETDEL` | 300 s | the OAuth `state` value can be used once and only by the browser holding the matching nonce cookie | `services/auth-service/domains/auth/service.py` |
| `oauth:2fa:used:<jti>` | 0 | `SET NX EX` | until the challenge token expires | an OAuth 2FA challenge can be used once | same |
| `core:event_claim:<event>:<event_id>` | 2 | `SET NX EX` | 300 s | each core replica receives every pub/sub event; only the first creates the notification row (fails open: a duplicate is better than a lost notification) | `services/core-service/event_listener.py::_claim_event` |
| `e2e:run_lock` | 0 | `SET NX EX`; release with a Lua script that deletes only if the value is still ours | 45 min | only one end-to-end check run at a time | `shared/redis.py` (`acquire_e2e_lock`) |
| `yjs:active:<note_id>` | 2 | sorted set: member = replica id, score = expiry time | 90 s, refreshed | "is any replica editing this note right now?" | `services/core-service/yjs_relay.py` |

**Why the end-to-end lock exists.** The end-to-end checks compare
site-wide counters before and after their own changes, so two
overlapping runs disturb each other (one soft-deletes its three test
users while the other registers three, and a "user count increased"
check fails). Both e2e scripts take `e2e:run_lock` first and refuse to
start while another run holds it. The TTL frees the lock if a run is
killed; `--break-lock` removes it at once.

**About `gather_bounded` (`shared/http_client.py`).** It does not use
Redis. `gather_bounded(coros, limit=10)` runs coroutines concurrently with
an `asyncio.Semaphore`, so at most `limit` run at once inside one
process. Database locks (row and advisory locks) are described in
[20-postgresql.md](20-postgresql.md).

### 3.7 Pub/sub and streams (overview)

`shared/events.py` has two mechanisms. The semantics (which event, who
publishes, who handles, retries) are in [24-events.md](24-events.md);
here is what they look like in Redis.

**Pub/sub (at-most-once).** `publish_cross_service` does
`PUBLISH events:<event name> <json>` and adds a fresh `event_id` to each
message. `CrossServiceSubscriber` subscribes to `events:<name>` channels
and reconnects with backoff (1, 2, 5, 10, 30 s) after an error (the
shared `run_pubsub_listener`, also used by core's two listeners below). Other
channels: `ws:fanout` (core-service's WebSocket pushes across replicas,
`websocket_manager.py`) and `yjs:note:<note_id>` (collaborative editing
updates across replicas, `yjs_relay.py`). Pub/sub channels belong to the
whole server, not to a numbered database, even though the publishing
client is the events client.

**Streams (at-least-once).** `publish_durable_event` does
`XADD stream:<event name> MAXLEN ~ 10000 * payload <json>` in the queue
database (DB 3). `DurableEventSubscriber`:

- creates the consumer group `workers` on each stream
  (`XGROUP CREATE ... 0 MKSTREAM`, ignoring `BUSYGROUP`) inside its retry
  loop, and creates it again after a `NOGROUP` error (stream deleted and
  recreated without the group);
- reads with `XREADGROUP GROUP workers <consumer> COUNT 10 BLOCK 5000
  STREAMS ... >`;
- acknowledges with `XACK` after the handler succeeds;
- every 12 loops (about a minute) and at startup, takes over entries
  idle for 60 s with `XAUTOCLAIM` and retries them;
- after 5 deliveries (`_MAX_DELIVERY_ATTEMPTS`) acknowledges the entry
  anyway and logs `durable_event_dead_lettered`;
- never dies on an error: the loop logs
  `durable_event_subscriber_loop_error` and retries with backoff (1, 2, 5,
  10, 30 s).

The consumer name is `settings.SERVICE_NAME`, stable across restarts, so
a restarted service finds its own pending entries.

| Stream (DB 3) | Consumer | Handler |
|---|---|---|
| `stream:document.uploaded` | `ai-service` | RAG ingestion |
| `stream:document.deleted` | `ai-service` | remove chunks |
| `stream:portfolio.deleted` | `ai-service` | close the portfolio's open tiers |
| `stream:document.status_changed` | `core-service` | persist `core.documents.status` |

### 3.8 What is not in Redis

- **The analysis queue.** It is an in-process `asyncio.Queue` in
  ai-service (`domains/analysis/queue.py`), with admission counted from
  `ai.analyses` in PostgreSQL. Analyses still pending or running at a
  restart are marked failed with reason `service_restart`.
- **LLM pacing and cooldowns.** They live in ai-service's process memory
  (`ai/llm/google_provider.py`, timestamps from `time.monotonic()`); a
  restart resets them.
- **Market data cache.** PostgreSQL (`ai.price_bars`, `ai.instruments`).

### 3.9 Key naming

Prefixes say who owns a key:

| Prefix | Owner |
|---|---|
| `auth:` (`auth:rl:*`, `auth:user_state:*`) | auth-service (user state read by all) |
| `auth_client:` | cache of auth-service answers, shared by core and ai |
| `core:` | core-service (`core:auth_client:*`, `core:ai_client:*`, `core:event_claim:*`, `core:job_last_success`, `core:job_tracked_since`, `core:market_close_skipped`) |
| `ai:` | ai-service (`ai:core_client:*`, `ai:internal:*`, `ai:rag_ask:*` in DB 1, `ai:newsapi:*`) |
| `apikey:`, `ratelimit:` | core-service public API (`apikey:valid:*` in DB 0; `apikey:last_used_gate:*` and `ratelimit:*` in DB 1) |
| `oauth:` | auth-service OAuth flow |
| `yjs:`, `ws:` | core-service real-time (`yjs:active:*` keys; `yjs:note:*` and `ws:fanout` channels) |
| `events:` | pub/sub channels |
| `stream:` | durable streams |
| `e2e:` | end-to-end test lock |

### 3.10 Job heartbeats (DB 0)

`core:job_last_success` is a hash with one field per scheduled job
(`market-open`, `market-close`, `tier-monitoring`, `review-run`,
`rag-ingest`, `logs-cleanup`, `backup`, `purge-unverified`,
`notification-retention`) holding the Unix time of its last
success (`services/core-service/job_health.py`). Each job script reports
to `POST /internal/scheduler/heartbeat/{job}`. core-service exports it as
the gauge `alphamind_job_last_success_timestamp_seconds{scheduled_job}`,
and alert rules fire when a job is overdue. The plain key
`core:market_close_skipped` holds the number of portfolios the last
market-close run skipped (gauge `alphamind_market_close_portfolios_skipped`,
written by `record_market_close_skipped`). Neither has a TTL, so the
values survive a core-service restart (`load_job_health` reloads both at
startup). A third hash, `core:job_tracked_since`, holds for each job
without a stored success the time core-service first began tracking it
(`HSETNX`, so the first start is kept); the gauge is set to that value, so
the stale-job alerts fire for a job that never succeeds. See
[26-scheduler-jobs.md](26-scheduler-jobs.md).

### 3.11 Metrics: redis-exporter

The compose service `redis-exporter` (`oliver006/redis_exporter:v1.74.0`,
`REDIS_ADDR=redis://redis:6379`) is scraped by Prometheus as job `redis`
on port 9121.

- Alert `RedisDown`: `redis_up == 0` for 1 minute (critical)
  (`infra/prometheus/alert_rules.yml`).
- Grafana "AlphaMind - Infrastructure & AI pipeline" panels: Redis up
  (`redis_up`), Memory used (`redis_memory_used_bytes`), Commands per
  second (`rate(redis_commands_processed_total[5m])`), Connected clients
  (`redis_connected_clients`), Keys per database (`redis_db_keys > 0`),
  Keyspace hit ratio (hits / (hits + misses)).

---

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` | `redis` / `6379` | server address | `shared/config.py` |
| `REDIS_URL` | unset (`.env.example`: `redis://redis:6379/0`) | full URL; any `/n` database part is ignored | `shared/config.py`, `shared/redis.py` |
| `REDIS_DB_CACHE` | `0` | caches, markers, heartbeats | `shared/config.py` |
| `REDIS_DB_RATE_LIMIT` | `1` | rate-limit counters | `shared/config.py` |
| `REDIS_DB_EVENTS` | `2` | user state, event claims, Yjs markers (and the pub/sub client) | `shared/config.py` |
| `REDIS_DB_QUEUE` | `3` | durable streams | `shared/config.py` |
| `USER_STATE_CHECK_ENABLED` | `True` | check live role/active state per request | `shared/config.py` |
| `USER_STATE_CACHE_TTL_SECONDS` | `300` | TTL of a state a reader fetched from auth-service | `shared/config.py` |
| `RATE_LIMIT_ENABLED` and `RATE_LIMIT_*` | see section 3.5 | auth-service rate limits | `services/auth-service/config.py` |
| `PUBLIC_API_RATE_LIMIT_PER_MINUTE` | `60` | public API cap per key and per owner | `services/core-service/config.py` |
| `RAG_ASK_RATE_LIMIT_PER_MINUTE` / `_PER_DAY` | `10` / `200` | research assistant limits | `services/ai-service/config.py` |
| `MEMBERSHIP_CACHE_TTL_SECONDS` | `60` (constant) | org-role cache lifetime | `shared/user_state.py` |
| pool | `max_connections=20`, `socket_timeout=15` | per (URL, DB) pool | `shared/redis.py` |
| stream cap | `MAXLEN ~ 10000` (constant) | approximate length limit per stream | `shared/events.py` |
| server | `redis:7-alpine`, `--appendonly yes --appendfsync everysec` | version and persistence | `docker-compose.yaml` |

---

## 5. Design decisions and why

**Redis for these jobs, not something new.**
The system needs a cache and rate limits anyway; Redis also offers
pub/sub and streams. One component covers live updates and durable
events without adding a message broker.

**Four logical databases.**
They keep different kinds of data apart, so a `SCAN` of the queue
database shows only streams, and a cache flush in development (`FLUSHDB`
on DB 0) would not touch streams or user state. The database number is
taken only from the code, never from `REDIS_URL`, because a number in the
URL would silently move everything to DB 0.

**Streams for events that must not be lost; pub/sub for the rest.**
With a plain `PUBLISH`, a `document.uploaded` sent while ai-service is
restarting reaches nobody, and the document stays `uploaded` with no
chunks and no error anywhere. Streams keep entries until acknowledged. Broadcast-style
updates (WebSocket pushes, Yjs) stay on pub/sub: a consumer group gives
each entry to only one consumer, which is the wrong shape for "tell every
replica", and a missed live update is not a correctness problem.

**AOF with `everysec`.**
Once streams carry durable events, Redis's data matters. RDB snapshots
alone can lose up to an hour of entries; AOF reduces that to about a
second without the cost of syncing every write.

**Fail open for rate limits, fail closed for access.**
A broken counter store must not stop logins or the API; a broken
membership lookup must not grant access. Each module documents which way
it fails.

**Atomic counters.**
`SET NX EX` + `INCR` + `TTL` in one transaction. A plain `INCR` followed
by a separate `EXPIRE` can leave a key without expiry if the process dies
in between, and the user would be limited forever.

**A mirror of user state instead of short tokens only.**
Shorter tokens would only shrink the window in which a demoted user keeps
old rights. The mirror closes it, and the fallback to token claims keeps
the system usable when both Redis and auth-service are down.

---

## 6. When things go wrong

| Symptom | Likely cause | What the system does | How to diagnose |
|---|---|---|---|
| `/health` shows `"redis": false`; alert `RedisDown` | Redis down | caches miss, rate limits allow, user state falls back to token claims, publishes fail (core-service's durable events wait in `core.event_outbox` and are published when Redis is back; alert `OutboxBacklog` after 15 minutes) | `docker compose --env-file .env.docker ps redis`; `bash scripts/redis-cli.sh PING` |
| Uploaded documents stay `uploaded` | stream not consumed (ai-service down), or Redis was down when core-service tried to publish | entries wait in the stream; events core-service could not publish wait in `core.event_outbox` and the outbox dispatcher publishes them when Redis is back; a re-upload re-publishes once the status has not changed for 10 minutes (`uploaded`) or 60 minutes (processing statuses) | `bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded` (look at `lag` and `pending`) |
| Log `durable_event_dead_lettered` | a handler failed 5 times | the entry is acknowledged and needs a human | the preceding `durable_event_handler_failed` log lines; [24-events.md](24-events.md) |
| Log `durable_event_subscriber_loop_error` repeating | Redis unreachable from the subscriber | loop retries with backoff (1, 2, 5, 10, 30 s; field `retry_in_seconds`) | Redis health; network |
| Streams seem empty in DB 3 but data exists in DB 0 | a client that does not use `shared/redis.py` took the URL's `/0` | `shared/redis.py` always strips it | `bash scripts/redis-cli.sh -n 0 --scan --pattern 'stream:*'` |
| A removed organization member still sees org data briefly | invalidation failed (Redis error) | TTL ends it within 60 s | log `org_membership_cache.invalidate_failed` |
| 429 on login | rate limit reached | `Retry-After` header tells when | `bash scripts/redis-cli.sh -n 1 --scan --pattern 'auth:rl:login:*'` then `TTL <key>` |
| e2e script refuses to start | another run holds `e2e:run_lock` | refuses | `bash scripts/redis-cli.sh GET e2e:run_lock`, `TTL e2e:run_lock`; the script's `--break-lock` removes it |
| Memory grows | many keys without TTL, or long streams | streams are capped at about 10,000 entries each | Grafana "Memory used", "Keys per database"; `bash scripts/redis-cli.sh INFO memory` |

---

## 7. Demo walkthrough

All commands are read-only. `bash scripts/redis-cli.sh` passes its
arguments to `redis-cli` inside the container (`make redis-cli` opens an
interactive session). Use `-n <db>` to choose the database.

1. **Health and persistence.**
   ```
   bash scripts/redis-cli.sh PING
   bash scripts/redis-cli.sh INFO persistence
   bash scripts/redis-cli.sh CONFIG GET appendfsync
   bash scripts/redis-cli.sh CONFIG GET save
   ```
   Point at `aof_enabled:1`, `aof_last_write_status:ok`,
   `aof_last_bgrewrite_status:ok`, and `appendfsync everysec`. Say: "Every
   write is logged to the append-only file and flushed once a second, so
   at most about a second of durable events can be lost. RDB snapshots
   (the `save` schedule) still run as well."

2. **Key counts per database.**
   ```
   bash scripts/redis-cli.sh INFO keyspace
   ```
   Expected lines like `db0:keys=...`, `db1:keys=...`, `db2:keys=...`,
   `db3:keys=...`. Say: "0 cache, 1 rate limits, 2 events, 3 queue."

3. **Key patterns with SCAN.** `SCAN` walks the keyspace in small steps
   without blocking the server (unlike `KEYS *`).
   ```
   bash scripts/redis-cli.sh -n 0 --scan --pattern 'auth_client:org_roles:*'
   bash scripts/redis-cli.sh -n 0 --scan --pattern 'ai:core_client:*'
   bash scripts/redis-cli.sh -n 1 --scan --pattern 'auth:rl:*'
   bash scripts/redis-cli.sh -n 2 --scan --pattern 'auth:user_state:*' | head
   bash scripts/redis-cli.sh -n 3 --scan --pattern 'stream:*'
   ```
   Caches may be empty if nothing happened in the last minute. To fill
   one, open a portfolio page or log in, then scan again.

4. **TTL of a cache key, and a key without TTL.**
   ```
   bash scripts/redis-cli.sh -n 0 TTL auth_client:org_roles:<user id from step 3>
   bash scripts/redis-cli.sh -n 2 GET auth:user_state:<user id>
   bash scripts/redis-cli.sh -n 2 TTL auth:user_state:<user id>
   bash scripts/redis-cli.sh -n 0 HGETALL core:job_last_success
   bash scripts/redis-cli.sh -n 0 GET core:market_close_skipped
   ```
   Say: "The org-role cache dies within 60 seconds. The user-state entry
   written by auth-service has no TTL (-1), because auth-service keeps it
   current. Job heartbeats have no TTL either."

5. **Streams and consumer groups.**
   ```
   bash scripts/redis-cli.sh -n 3 XINFO STREAM stream:document.uploaded
   bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded
   bash scripts/redis-cli.sh -n 3 XINFO CONSUMERS stream:document.uploaded workers
   bash scripts/redis-cli.sh -n 3 XPENDING stream:document.uploaded workers
   bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.status_changed
   ```
   Point at `length`, the group `workers`, `lag` (entries not yet
   delivered, should be 0), `pending` (delivered but not acknowledged,
   normally 0), and the consumer name `ai-service` (or `core-service` for
   `document.status_changed`). Say: "Entries stay in the stream until
   they are acknowledged. If ai-service is down, they wait and are
   delivered when it is back."

6. **Live pub/sub channels.**
   ```
   bash scripts/redis-cli.sh PUBSUB CHANNELS '*'
   bash scripts/redis-cli.sh PUBSUB NUMPAT
   ```
   Expected: the `events:...` channels core-service subscribes to, and
   `ws:fanout`. `yjs:note:<id>` channels do not appear in this list:
   core-service listens to them with one pattern subscription
   (`PSUBSCRIBE yjs:note:*`), and `PUBSUB CHANNELS` lists only plain
   subscriptions. `PUBSUB NUMPAT` (number of pattern subscriptions) shows
   that pattern; it is at least 1. Say: "Pub/sub keeps nothing; it only
   reaches whoever is listening now."

7. **Optional: watch pub/sub live.** In a second terminal:
   ```
   bash scripts/redis-cli.sh PSUBSCRIBE 'events:*'
   ```
   Then upload a small document in the UI and watch
   `events:document.status_changed` messages arrive. Stop with Ctrl-C.

8. **Metrics.** In Grafana (`https://localhost:8443/grafana/`), open
   "AlphaMind - Infrastructure & AI pipeline" and show the Redis row.

---

## 8. Check your understanding

<details>
<summary>1. Why is `appendonly yes` needed when Redis is "only a cache"?</summary>

Redis is not only a cache here: the durable event streams must survive a
crash. With RDB snapshots alone up to an hour of entries could be lost;
AOF with `everysec` limits it to about a second.
</details>

<details>
<summary>2. Why does `shared/redis.py` strip the database number from `REDIS_URL`?</summary>

`redis.from_url` lets the URL's `/0` override the `db=` argument, so every
client would use DB 0. The code always chooses the database explicitly.
</details>

<details>
<summary>3. What is the difference between `events:document.status_changed` and `stream:document.status_changed`?</summary>

The first is a pub/sub channel: delivered to whoever listens now, nothing
stored (used for live WebSocket updates). The second is a stream entry
kept until core-service's consumer group acknowledges it (used to persist
the status).
</details>

<details>
<summary>4. How does the fixed-window counter guarantee every key gets an expiry?</summary>

`SET key 0 EX window NX`, `INCR key` and `TTL key` run in one `MULTI`
transaction, so the key is created with its TTL before it is counted.
</details>

<details>
<summary>5. What happens to rate limits and to organization access when Redis is down?</summary>

Rate limits fail open (requests are allowed, logged). The org-role cache
treats Redis errors as misses and asks auth-service; if auth-service also
fails, the lookup raises and access is refused (fail closed).
</details>

<details>
<summary>6. Why does `auth:user_state:<id>` have no TTL when auth-service writes it?</summary>

auth-service is the only writer and updates it on every change (and at
startup), so it is always current. A deleted user stays stored as
inactive to keep blocking old tokens.
</details>

<details>
<summary>7. Is the analysis queue in Redis?</summary>

No. It is an in-process `asyncio.Queue` in ai-service; admission is
counted from `ai.analyses` in PostgreSQL.
</details>

<details>
<summary>8. What do `lag` and `pending` mean in `XINFO GROUPS`?</summary>

`lag`: entries added to the stream that the group has not delivered yet.
`pending`: entries delivered to a consumer but not acknowledged yet.
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `docker-compose.yaml` (`redis`, `redis-exporter`) | server command (AOF), volume, healthcheck, exporter |
| `shared/config.py` | `REDIS_*`, `USER_STATE_*` settings |
| `shared/redis.py` | pools per database, URL fix, timeouts, health check; fixed-window counter, 429 response; end-to-end run lock |
| `shared/user_state.py` | live role/active state readers and writer; org-role cache and its invalidation |
| `services/auth-service/user_state_sync.py` | auth-service side of the mirror |
| `services/ai-service/ai/providers/newsapi.py` | NewsAPI usage log and source catalog keys |
| `services/auth-service/security.py` | auth scopes, 2FA lock |
| `services/core-service/apikeys.py` | public API counters, key validation cache |
| `services/ai-service/domains/rag/qa.py` | research assistant counters |
| `shared/events.py` | pub/sub publish/subscribe, streams, consumer group |
| `shared/http_client.py` | `gather_bounded` (in-process semaphore, not Redis) |
| `services/core-service/event_listener.py` | event claims |
| `services/core-service/yjs_relay.py`, `websocket_manager.py` | Yjs and WebSocket channels, active-room markers |
| `services/core-service/job_health.py` | job heartbeat hash |
| `services/ai-service/clients/core_client.py`, `services/core-service/clients/*.py` | short HTTP-answer caches |
| `infra/prometheus/alert_rules.yml` | `RedisDown` |
| `scripts/redis-cli.sh` | redis-cli in the container |

---

## 10. Related chapters

- [24-events.md](24-events.md): event names, publishers, handlers, retries
- [25-realtime-yjs.md](25-realtime-yjs.md): `ws:fanout`, `yjs:note:*`, active rooms
- [11-auth-service.md](11-auth-service.md): rate limits, OAuth, user-state mirror
- [12-core-service.md](12-core-service.md): public API keys and limits
- [17-ai-rag.md](17-ai-rag.md): research assistant limits, visibility (uses the org-role cache)
- [20-postgresql.md](20-postgresql.md): database locks and the analysis queue admission
- [26-scheduler-jobs.md](26-scheduler-jobs.md): job heartbeats
- [27-observability.md](27-observability.md): exporter, alerts, dashboards
- [29-security.md](29-security.md): live user state and fail-open/fail-closed choices
- [30-shared-library.md](30-shared-library.md): `shared/redis.py` and friends
- [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md) section 7 (what is not backed up)
