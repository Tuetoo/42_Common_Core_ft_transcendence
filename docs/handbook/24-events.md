# 24. Events between services

> AlphaMind has three application services (auth-service, core-service,
> ai-service). Most of the time they talk with HTTP calls. But when
> something *happened* in one service and other services must react
> ("a document was uploaded", "an analysis finished", "a user joined an
> organization"), the service publishes an **event** through Redis and
> does not wait for anybody. This chapter explains the event registry,
> the two delivery styles (fire-and-forget pub/sub and durable Redis
> streams), how duplicates are avoided, why every event is published only
> after the database commit, what happens when a consumer is down, and how
> core-service turns events into notifications and live WebSocket pushes.

## 1. The big picture

An event is a small JSON message with a name (for example
`document.uploaded`) and a payload (a dictionary of fields). The service
that publishes it is the **producer**. The services that react to it are
the **consumers**. The producer does not know who listens. This keeps the
services loosely coupled: ai-service does not need to call core-service
to say "analysis finished"; it publishes `analysis.completed` and moves on.

AlphaMind uses Redis for two different kinds of delivery:

```
                     fire-and-forget (pub/sub)
 auth-service --PUBLISH events:<name>--+
                                        |       +--------------------+
 ai-service ----PUBLISH events:<name>--+------> | Redis pub/sub      | --> core-service
                                                | (no storage)       |     (every replica)
                                                +--------------------+

                     durable (Redis streams, DB 3)
 core-service --XADD stream:document.uploaded --+
              --XADD stream:document.deleted ---+    +------------------+
              --XADD stream:portfolio.deleted --+--> | Redis stream     | --> ai-service
 auth-service --XADD stream:user.deleted -------+    |                  |
                                                     | + consumer group |     (XREADGROUP,
 ai-service ----XADD stream:document.status_changed  | "workers"        |      XACK)
                                                --+-> +------------------+ --> core-service
```

- **Pub/sub** (publish/subscribe) is like a radio broadcast. Whoever is
  listening at that moment hears the message. Nobody stores it. If a
  listener is disconnected, the message is gone for that listener.
- A **stream** is like a mailbox with a log. Every message is stored in
  Redis. A consumer reads it, does the work, and then confirms
  ("acknowledges") it. If the consumer crashes before it confirms, the
  message is still there and is delivered again.

AlphaMind uses pub/sub for notifications and live screen updates (losing
one is annoying but not harmful) and streams for the five events where a
lost message would leave the data wrong (`user.deleted` goes on both:
pub/sub for core, a stream for ai).
core-service's three durable events are first stored in a database
table (the outbox, `core.event_outbox`) in the same transaction as the
change, so they survive a Redis outage until they are published
(section 3.5).

Only core-service holds browser WebSockets. So almost every event ends in
core-service, which (a) writes a notification row and (b) pushes a
message to the browsers that are connected (see
[25-realtime-yjs.md](25-realtime-yjs.md)).

## 2. Concepts you need

| Term | Meaning here |
|---|---|
| Event | A named JSON message saying "this happened". Names are in `shared/events.py::EventName`. |
| Producer / consumer | The service that publishes / the service that reacts. |
| Redis pub/sub | `PUBLISH channel message` sends to every client currently `SUBSCRIBE`d to that channel. No storage, no retry. "At most once" delivery. |
| Channel | The pub/sub "address". AlphaMind uses `events:<event name>`, e.g. `events:analysis.completed`. |
| Redis stream | An append-only log stored in Redis (`XADD` appends, `XRANGE` reads). Each entry has an id like `1727900000000-0` (milliseconds-sequence). |
| Consumer group | A named reader of a stream (here always `workers`). Redis remembers which entries the group has already been given (`last-delivered-id`). Each entry goes to ONE consumer of the group, not to all. |
| Consumer | A named member of the group. AlphaMind uses the service name (`ai-service`, `core-service`). |
| XREADGROUP | "Give me new entries for my group". With id `>` it returns only entries the group has never seen. |
| XACK | "I finished this entry". It removes the entry from the pending list. |
| Pending Entries List (PEL) | Entries delivered to a consumer but not yet acknowledged. Redis counts how many times each was delivered (`times_delivered`). |
| XAUTOCLAIM | "Give me pending entries that have been idle for at least N ms". Used to retry failed or abandoned entries. |
| At-least-once | Every entry is delivered one or more times. The handler must therefore be **idempotent**: running it twice must give the same result as once. |
| Dead letter | An entry that failed too many times. AlphaMind logs it loudly and acknowledges it so it stops blocking. |
| Publish after commit | The event is sent only after the database transaction that made the change has committed. Otherwise a consumer could react to a change that was rolled back. |
| BackgroundTasks | A FastAPI feature: functions added to it run after the response was sent. Routes in AlphaMind use `Depends(get_db, scope="function")`, so the database commit happens before the response, and background tasks run after the commit. |
| Fan-out | Sending one message to many receivers (here: many browser sockets). |
| Replica | One running copy of a service. The code is written so several core-service replicas could run at once; the default compose file runs one. |

## 3. How it works in AlphaMind

### 3.1 The registry: `shared/events.py`

`EventName` is a `StrEnum` with every event name. `EVENT_OWNERS` maps each
name to the one service that publishes it. The map documents ownership;
nothing enforces it at runtime. Its only reader is the contract test
`tests/core-service/test_realtime.py::TestNotificationEvents::test_event_handlers_exist_and_claims_dedupe_replicas`,
which checks that core-service handles every auth and ai event. The payload shapes are
listed in the table below (this table is their only description), and
payload builder functions in the producers
(`services/auth-service/events_out.py`, `services/ai-service/ai_events.py`)
make sure each shape is built in one place only.

Table of all 36 events (producer, transport, consumer and what the
consumer does). "pub/sub" means `publish_cross_service`; "durable" means
`publish_durable_event`. Every pub/sub payload also gets an `event_id`
field on the wire (section 3.4).

| Event | Producer (where) | Transport | Consumer (handler) | Payload fields | Effect |
|---|---|---|---|---|---|
| `document.uploaded` | core (`domains/research/service.py::_publish_uploaded`) | durable | ai (`event_listener.py::_handle_document_uploaded`) | `document_id, owner_id, organization_id, storage_bucket, storage_key, document_type, category, ticker, content_date, filename` | runs RAG ingestion (`run_rag_ingestion_workflow`); `owner_id` and `organization_id` decide who may later retrieve the chunks |
| `document.deleted` | core (`ResearchService.delete_document`, `scripts/delete_documents.py`, `_handle_user_deleted` for a deleted user's private documents, and `_handle_organization_deleted` for a deleted organization's documents, the same two cleanups in the nightly `deleted-user-sweep-core` job, and `scripts/clean_organizations.py`; all through `delete_document_row`) | durable | ai (`_handle_document_deleted`) | `document_id` | `ai/rag/indexing.py::delete_document_completely`: Qdrant points, BM25, `ai.document_chunks`, `ai.embedding_jobs`. `ai.filing_records` is keyed by the filing's filename, not by document id, and is NOT deleted: when the same filing is ingested again, its row is overwritten (upsert by `filename`) |
| `portfolio.deleted` | core (`domains/portfolio/api.py::delete_portfolio`, and `_handle_user_deleted` for a deleted user's private portfolios, and `_handle_organization_deleted` (`_release_portfolios`) for a deleted organization's portfolios whose owner is a deleted account, also from the nightly sweep and `scripts/clean_organizations.py`; all through `delete_portfolio_row`) | durable | ai (`_handle_portfolio_deleted`) | `portfolio_id, user_id` (owner) | closes unfilled tiers (status `expired`), then publishes `tier.cancelled` per tier |
| `document.status_changed` | ai (`rag_ingestion_workflow.py::_publish_status`) | durable AND pub/sub | core: durable `_persist_document_status`; pub/sub `_broadcast_document_status_changed` | `document_id, status` | durable: writes `core.documents.status` and notifies owner on `ready`/`failed`; pub/sub: push to `document:<id>` |
| `analysis.started` | ai (`analysis_workflow.py`) | pub/sub | core `_handle_analysis_started` | `analysis_id, user_id ("" = system), ticker, portfolio_id` | push to `analysis:<id>` (the analysis page refreshes on it), notification |
| `analysis.completed` | ai (`analysis_workflow.py`) | pub/sub | core `_handle_analysis_completed` | same as started | push, notification |
| `analysis.failed` | ai (`mark_analysis_failed`, `domains/analysis/queue.py` at startup) | pub/sub | core `_handle_analysis_failed` | same + `failure_reason` | push, notification with the reason |
| `recommendation.ready` | ai (`analysis_workflow.py`) | pub/sub | core `_handle_recommendation_ready` | `id, analysis_id, ticker, action, confidence, user_id (None = system)` | push, notification |
| `decision.recorded` | ai (`domains/decision/service.py`, published by the route) | pub/sub | core `_handle_decision_recorded` | `decision_id, analysis_id, user_id, ticker, decision` | push, notification |
| `decision.reviewed` | ai (`domains/decision/service.py`, a new or replacing review, published by the route) | pub/sub | core `_handle_decision_reviewed` | same as `decision.recorded` | notification to the user |
| `decision.outcome.evaluated` | ai (`review_workflow.py`) | pub/sub | core `_handle_decision_outcome_evaluated` | `decision_id, analysis_id, user_id, ticker, evaluation_days, alpha, outperformed` | notification |
| `tier.executed` | ai (`tier_monitoring_workflow.py`) | pub/sub | core `_handle_tier_executed` | `tier_id, analysis_id, user_id, portfolio_id, ticker, action, quantity, price, reason` | push, notification |
| `tier.execution_failed` | ai (`tier_monitoring_workflow.py`) | pub/sub | core `_handle_tier_execution_failed` | same | push, notification |
| `tier.cancelled` | ai (decision route: `declined` / `superseded`; analysis workflow: `superseded`; portfolio delete: `portfolio_deleted`) | pub/sub | core `_handle_tier_cancelled` | same, `reason` set | push, notification |
| `backtest.completed` | ai (`backtest_workflow.py::publish_backtest_finished`) | pub/sub | core `_handle_backtest_completed` | `backtest_id, user_id, kind, status, error` | notification |
| `backtest.failed` | ai (same) | pub/sub | core `_handle_backtest_failed` | same | notification |
| `backtest.deleted` | ai (`domains/backtest/api.py::delete_backtest`, after the commit as a background task) | pub/sub | core `_handle_backtest_deleted` | `backtest_id, user_id, kind` | notification to the owner |
| `organization.created` | auth (`domains/organization/service.py`) | pub/sub | core `_handle_organization_created` | `organization_id, organization_name, actor_id, user_id` (the affected member, or `None`), `role` (or `None`), `member_ids` (members to notify, sorted, unique) | notification to members |
| `organization.updated` | auth | pub/sub | core | same | notification to members |
| `organization.deleted` | auth (owner `DELETE /organizations/{id}`; also `AdminService._delete` when the deleted user was the owner and no other member is left, and the nightly purge-unverified job's `clean_deleted_account_leftovers` for such an organization of an account deleted earlier) | pub/sub | core | same | open sockets (Yjs and channels) of every former member are re-checked (section 3.8); notification to former members. core deletes the organization's documents (`document.deleted`) and notes, deletes the shared portfolios of deleted owners (`portfolio.deleted`) and makes the others private again (`delete_organization_content`, chapter 12) |
| `organization.member.joined` | auth (invitation accept) | pub/sub | core | same | welcome to the new member, notice to others |
| `organization.member.left` | auth | pub/sub | core | same | open sockets of the member re-checked; affected member + others notified |
| `organization.member.removed` | auth | pub/sub | core | same | open sockets of the member re-checked; affected member + others notified |
| `organization.role.changed` | auth (also when a deleted owner's organization passes to a successor) | pub/sub | core | same | open sockets of the member re-checked; affected member + others notified |
| `organization.invitation.created` | auth | pub/sub | core | same (`user_id` = the invitee when the email has an account, else `None`) | notification to the actor and to the invited user (when the email has an account) |
| `organization.invitation.revoked` | auth (`revoke_invitation`; also `AdminService._delete` and `clean_deleted_account_leftovers`, one per open invitation the deleted user sent, with that user as `actor_id`) | pub/sub | core | same (`user_id` as above, `role` = the invitation role) | notification to the actor and to the invited user (when the email has an account); a deleted sender (the `AdminService._delete` path) gets no notification, because core skips deleted users |
| `friend.requested` | auth (`domains/social/service.py`) | pub/sub | core `_handle_friend_requested` | `actor_id, user_id, actor_username, other_username` | notification to `user_id` and to the actor |
| `friend.accepted` | auth | pub/sub | core | same | notification to both users |
| `friend.removed` | auth (every `DELETE /connections/{id}`) | pub/sub | core | same + `action` (`removed`, `declined`, `cancelled`, `unblocked`) | open sockets of both users re-checked; notification to the actor, and to the other user only for `removed` |
| `connection.blocked` | auth (`domains/social/service.py`, only when the connection was not already blocked) | pub/sub | core `_handle_connection_blocked` | `actor_id, blocked_user_id, blocked_username` (no `user_id`) | open sockets of both users re-checked; notification to the actor only (the blocked user is never told) |
| `user.updated` | auth (`domains/auth/service.py`, `domains/admin/service.py`) | pub/sub | core `_handle_user_updated` | `user_id, actor_id, username, role, is_active, changed` (sorted field names only, never values) | notification to the user and to the actor (when different) |
| `user.role.changed` | auth (admin) | pub/sub | core | same | open sockets of the user re-checked; notification to the user and the actor |
| `user.deactivated` | auth (admin) | pub/sub | core | same | every open socket of the user closed (4401); notification to the user and the actor |
| `user.reactivated` | auth (admin) | pub/sub | core | same | notification to the user and the actor |
| `user.deleted` | auth (admin delete, nightly unverified purge; `events_out.publish_events`, which also XADDs it to `stream:user.deleted`) | pub/sub AND durable | core `_handle_user_deleted` (pub/sub); ai `_handle_user_deleted` (durable) | same (`username` is the name before the soft delete renamed it) | core: every open socket of the user closed (4401); logged `user_deleted_received`; the user's private data is deleted (`delete_private_user_data`, log `user_data_cleaned`, chapter 12): private documents (`document.deleted`), private notes, private portfolios (`portfolio.deleted`), watchlists, API keys, notifications; notification to the admin who deleted (the purge, where actor = user, notifies nobody). ai: the user's analyses with everything under them, archived reports with their vectors and graph relations, decisions, backtests, episodic and procedural memories (`user_cleanup.delete_user_data`, log `ai_user_data_cleaned`, chapter 13 section 3.8) |
| `user.presence.changed` | auth (`domains/auth/service.py::presence_event`, published after the commit) | pub/sub | core `_handle_user_presence_changed` | `user_id, is_online` | push to `presence:<user_id>` and to `user:<friend_id>` of each accepted friend (friend list from auth `GET /internal/users/{id}/friends`) |

Notes on the table:

- ai-service consumes only durable events: core's three and auth's
  `user.deleted`. It has no pub/sub subscriber; only core-service listens
  to auth's pub/sub events.
- `user.deactivated` and `user.deleted` do not block HTTP requests.
  Blocking is done on every request by the live user-state check in
  `shared/user_state.py` (see [29-security.md](29-security.md)). The events
  create notifications and close the user's open Yjs editor sockets
  (a WebSocket is checked only when it connects, so without the event an
  open editor would keep working until its token expired).
- Deletion policy: a deleted user's personal data is removed completely
  in every service; organization content and system data not keyed to
  the user stay. `user.deleted` is the fast path. core and ai each also
  run a nightly deleted-user sweep (`deleted-user-sweep-core` 04:30,
  `deleted-user-sweep-ai` 04:40 New York, chapter 26). It asks auth
  (`POST /internal/users/deleted`) which owners of leftover data are
  deleted, and runs the same idempotent cleanup. This repairs a lost
  event and data an in-flight request wrote after the cleanup.
  core creates no notification for a deleted user, so a late ai event
  about that user (for example `tier.cancelled`) leaves no row
  (chapter 12, section 3.11).
- auth has no outbox. If its publish of `user.deleted` fails, both the
  pub/sub and the stream copy are lost, and the sweeps clean the data
  the next night. A lost `organization.deleted` is repaired by the same
  core job: it asks auth which referenced organizations no longer exist
  (`POST /internal/organizations/missing`) and runs
  `delete_organization_content` for them; on a lookup error it deletes
  nothing, and a safety brake (`ORGANIZATION_SWEEP_MAX_MISSING`, default
  5) refuses a run where auth reports too many organizations as missing.
  Both core cleanups run
  inline in core's pub/sub loop, so other events wait while they run
  (chapter 12, section 3.15).
- `recommendation.ready` has its own payload shape: the recommendation id
  is in `id`, and `user_id` is `None` (not `""`) for a system analysis.
  core treats both `None` and `""` as "nobody to notify" (a falsy check,
  so an empty string never reaches a UUID column).
- Tier payloads carry `quantity` and `price` as plain decimal text
  (`ai_events.plain_decimal`, `format(value, "f")`). asyncpg can decode a
  NUMERIC with an exponent (`Decimal('1.0E+5')`), and `str()` would keep
  it, so a notification would read "sell 1.0E+5 AMD". `price` is the fill
  price for `tier.executed`, otherwise the tier's target price.
- Backtest `kind` is derived from the strategy name
  (`domains/backtest/schema.py::backtest_kind`): `ai_recommendation_replay` ->
  `recommendation_replay`, `portfolio_snapshot_backtest` -> `portfolio`,
  anything else -> `ticker`. A failed backtest without an error text gets
  `error = "backtest_failed"`.
- `decision.outcome.evaluated`: `alpha` is the decision's return minus the
  benchmark's return, as a fraction (core shows it as percentage points);
  `alpha` and `outperformed` are `None` when there was no price data.
- Tier events of a system analysis have `user_id` `""`. core then looks up
  the portfolio owner in its own database (`_portfolio_owner`). For
  `portfolio.deleted` this lookup is impossible (the portfolio is gone), so
  ai-service puts the owner from the event into the `tier.cancelled`
  payloads (`user_id_fallback`).

### 3.2 Fire-and-forget: `publish_cross_service` and `CrossServiceSubscriber`

Both live in `shared/events.py`.

Publishing:

```python
await client.publish(
    f"events:{event_name}", json.dumps({**payload, "event_id": uuid.uuid4().hex})
)
```

- The client is `get_events_client(settings)`, the Redis connection for
  `REDIS_DB_EVENTS` (2). Note: Redis pub/sub channels are global; the
  database number does not matter for `PUBLISH`/`SUBSCRIBE`. It matters
  for normal keys such as the claim keys below.
- The keyword argument is `event_name`, never `event`. structlog's log
  methods take the message as their first argument, named `event`;
  passing `event=` as well raises `TypeError: got multiple values for
  argument 'event'` on every call. Keep this in mind for every log call in
  the project, not only here.
- A debug log line `cross_service_event_published` is written (pass
  `log=False` to skip it; the durable twin writes `durable_event_published`).
  Debug lines are visible only with `LOG_LEVEL=DEBUG`; in development,
  `docker-compose.override.yml` sets that for ai-service only, so you see
  ai-service's publishes but not core's or auth's.
- `event_id` is added to the message on the wire only; the caller's
  dictionary is not changed.

Subscribing (`CrossServiceSubscriber`):

1. `on(event_name, handler)` registers one async handler per event.
2. `run()` subscribes to `events:<name>` for every registered name and
   loops over `pubsub.listen()`. With no handler registered it returns at
   once.
3. `_dispatch` ignores everything that is not a `message` (for example the
   subscribe confirmations), decodes the JSON and calls the handler. A
   handler exception (or bad JSON) is logged
   (`cross_service_event_handler_failed`) and the loop continues. There is
   no retry: pub/sub has nothing to retry from.
4. If the Redis connection breaks, the loop logs
   `cross_service_subscriber_failed` (with `retry_in_seconds`)
   and reconnects with the backoff
   `RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)` (the last
   value repeats; `reconnect_delay(attempt)`, attempt counted from 0 and
   reset after a successful subscribe). If the server ends the stream
   without an error, `cross_service_subscriber_stream_ended` is logged and
   the same reconnect follows. The old pub/sub object is closed with
   `close_pubsub_quietly` (never raises). Cancellation (service shutdown)
   is not caught and stops the loop. Without the reconnect loop, one Redis
   error would end the coroutine for good and no WebSocket push would
   arrive until the service restarted. The loop itself is
   `shared.events.run_pubsub_listener(settings, name=..., subscribe=...,
   handle=...)`, which logs `<name>_started`, `<name>_stream_ended` and
   `<name>_failed`. core's two other pub/sub listeners use the same
   function: the WebSocket fan-out listener
   (`websocket_fanout_listener`) and the Yjs cross-replica listener
   (`yjs_cross_replica_listener`).

core-service builds its subscriber in
`services/core-service/event_listener.py::build_subscriber()` from the
dictionary `_PUBSUB_HANDLERS` (33 events: 14 from ai-service, 19 from
auth-service) and starts it in `main.py`'s lifespan with
`start_background_task(build_subscriber().run(), "event_subscriber")`.

### 3.3 Durable: `publish_durable_event` and `DurableEventSubscriber`

Also in `shared/events.py`.

Publishing:

```python
await client.xadd(
    f"stream:{event_name}", {"payload": json.dumps(payload)},
    maxlen=_STREAM_MAXLEN, approximate=True,
)
```

- The client is `get_queue_client(settings)`: `REDIS_DB_QUEUE` (3). So the
  streams are in Redis database 3.
- Each entry has one field, `payload`, holding the JSON text.
- `_STREAM_MAXLEN = 10_000` with approximate trimming (`MAXLEN ~`). The
  stream cannot grow without limit. 10 000 is far above any normal backlog.
- Durable payloads do not get an `event_id`; they do not need one
  (section 3.4).

Consuming (`DurableEventSubscriber(settings, consumer_name, group="workers")`):

1. For every registered stream it runs `XGROUP CREATE stream:<name> workers
   0 MKSTREAM`. `id="0"` means "the group starts from the beginning of the
   stream". `MKSTREAM` creates an empty stream if none exists. A
   `BUSYGROUP` error ("group already exists") is ignored. This step runs
   inside the retry loop of step 6: any other error while creating the
   groups (for example Redis not reachable at start) is logged and retried,
   and the subscriber does not read until every group exists.
2. It logs `durable_event_subscriber_started` and calls
   `_reclaim_pending` once (entries left pending by the previous run of this
   consumer). An error in this first reclaim is logged
   (`durable_event_subscriber_reclaim_failed`) and the loop still starts.
3. Main loop: `XREADGROUP GROUP workers <consumer> COUNT 10 BLOCK 5000
   STREAMS ... >`. It waits up to 5 seconds for new entries.
4. Each entry goes to `_handle_one`:
   - no handler registered: `XACK` at once;
   - handler succeeds: `XACK`;
   - handler raises (including bad JSON in `payload`): it reads
     `times_delivered` with `XPENDING`, logs `durable_event_handler_failed`
     with `delivery_count`, and leaves the entry pending. When
     `delivery_count >= _MAX_DELIVERY_ATTEMPTS` (5) it logs
     `durable_event_dead_lettered` at error level and acknowledges it (dead
     letter: it stops blocking the reclaim, and a human must look at it).
5. Every `_RECLAIM_EVERY_N_ITERATIONS = 12` loop iterations (about one
   minute when idle, because each read blocks up to 5 s) it calls
   `_reclaim_pending` again. That runs `XAUTOCLAIM ... min_idle_time=60000
   count=50` (repeated with the returned cursor until it is back at
   `0-0`) and retries every entry that has been pending for at least 60
   seconds. Each reclaim increases `times_delivered`. So a failing entry is
   tried 5 times, about one minute apart, then dead-lettered. The reclaim
   interval matches the 60 s idle threshold on purpose: reclaiming more
   often would find nothing new.
6. Any other exception in the loop (for example a Redis connection error)
   is logged as `durable_event_subscriber_loop_error` (with `groups_ready`
   and `retry_in_seconds`), and the loop retries after the shared backoff
   `reconnect_delay(attempt)`: 1, 2, 5, 10, then 30 seconds, reset to the
   start after a successful read. A `NOGROUP` error
   (`is_missing_group`, for example after the stream was deleted and
   recreated by an `XADD` without the group) marks the groups as missing,
   so the next attempt runs step 1 again and recreates them with id `0`;
   entries added in the meantime are then delivered.

Why step 5 exists: `>` only returns entries the group has never seen. A
failed entry sits in the pending list and `>` never returns it again.
Without the periodic reclaim, a failed entry would only be retried at the
next process restart.

Why step 6 exists: without it, one exception outside the handler (for
example a Redis read timeout) ends the coroutine for good, and it does so
silently. The task object is held by the lifespan for the whole life of the
app, so Python's "Task exception was never retrieved" warning (written only
when the task is garbage-collected) never appears. The symptom is a
stream that shows `entries-read: 1` and then a growing `lag` with no
activity, across restarts and new uploads, and nothing in the logs. One
way to get there is redis-py's default socket timeout (5 s), which equals
the `BLOCK 5000`, so empty reads time out. `shared/redis.py` therefore sets
`socket_timeout=15`, longer than the block time.

Both lifespans (core-service and ai-service) add one more safety net for
their long-running loops, this subscriber included: each loop is started
with `shared.events.start_background_task(coro, name)`, which names the
task and adds the done-callback `log_unexpected_task_end`. If a loop ends
for any reason other than cancellation at shutdown,
`background_task_crashed` (with the task name and the traceback) or
`background_task_ended` (the loop returned) is logged at error level, so
a dead subscriber shows up in the logs instead of only as a growing
`lag`.

Every Redis client is built with `decode_responses=True`, so stream ids,
field names and values are `str`, not `bytes` (the redis-py type stubs do
not know this; `run()` casts the reply once).

ai-service's handlers (`services/ai-service/event_listener.py`) check
their payload before doing any work. `document.uploaded` needs non-empty
strings in `_UPLOADED_REQUIRED_FIELDS` (`document_id`, `storage_bucket`,
`storage_key`, `document_type`, `category`); a malformed event is logged
(`document_uploaded_malformed`, with `missing_fields`) and acknowledged,
instead of failing five times and being dead-lettered. In the same way a
`document.deleted` without a string `document_id` logs
`document_deleted_missing_document_id`, and a `portfolio.deleted` whose
`portfolio_id` is not a UUID logs `portfolio_deleted_bad_portfolio_id`,
and a `user.deleted` whose `user_id` is not a UUID logs
`user_deleted_bad_user_id`.
A database error inside a handler is NOT caught: it propagates, so the
entry stays pending and is retried.

Who consumes what:

| Service | Builder | Consumer name | Streams |
|---|---|---|---|
| ai-service | `services/ai-service/event_listener.py::build_durable_subscriber` | `ai-service` (`settings.SERVICE_NAME`) | `stream:document.uploaded`, `stream:document.deleted`, `stream:portfolio.deleted`, `stream:user.deleted` |
| core-service | `services/core-service/event_listener.py::build_durable_subscriber` | `core-service` | `stream:document.status_changed` |

Both use the group `workers`. The consumer name must stay the same across
restarts, because the pending entries belong to a consumer name; a random
name would leave the previous run's entries orphaned.

Redis must keep the streams across a crash. The `redis` service in
`docker-compose.yaml` runs with `--appendonly yes --appendfsync everysec`
(append-only file, written to disk every second). With periodic RDB
snapshots alone a crash could lose up to an hour of entries (see
[23-redis.md](23-redis.md)).

### 3.4 `event_id` and de-duplication: `_claim_event`

Pub/sub delivers each event to **every** subscriber. With several
core-service replicas, every replica receives `analysis.completed`. Each
replica must push to its own browser sockets (each replica only knows its
own sockets), but only ONE replica may create the notification row.

`services/core-service/event_listener.py::_claim_event(event, payload)`:

```python
event_id = payload.get("event_id")
digest = str(event_id) if event_id else sha256(json.dumps(payload, sort_keys=True))
claimed = await get_events_client(settings).set(
    f"core:event_claim:{event}:{digest}", "1", nx=True, ex=_EVENT_CLAIM_TTL_SECONDS)
```

- `SET ... NX EX 300`: set the key only if it does not exist, with a 300
  second expiry. The first replica wins; the others get `None` and skip the
  notification.
- The key uses the publisher's `event_id`. A key made only from the
  payload content would make two real, identical events within 5 minutes
  (deactivate, reactivate, deactivate the same user) look like one, and the
  second notification would be lost. A payload without `event_id` (for
  example one published by hand) falls back to a SHA-256 hash of the
  payload (`json.dumps(..., sort_keys=True, default=str)`).
- If Redis is down the function logs `event_claim_failed` and returns
  `True` ("fails open"): a duplicate notification is better than a lost
  one.
- Handlers broadcast first and claim second, so every replica still pushes
  to its local sockets.

Durable events do not need a claim: a consumer group gives each entry to
one consumer only. Their handlers are idempotent instead:

- `_persist_document_status` uses `DocumentRepository.transition_status`,
  which locks the row (`SELECT ... FOR UPDATE`) and only moves the status
  forward (`is_forward_status_transition`: rank `uploaded 0 < parsing 1 <
  chunking 2 < embedding 3 < ready 4 = failed 4`; `ready` is final;
  `failed -> parsing` is allowed, for a retry). A redelivered or late event
  is ignored (`document_status_transition_ignored`), and the ready/failed
  notification is sent only when the status really changed. Before the
  database is touched, the payload is checked: a missing `document_id`
  (`document_status_changed_missing_document_id`), one that is not a UUID
  (`document_status_changed_invalid_document_id`) or a missing `status`
  (`document_status_changed_missing_status`) is logged and acknowledged;
  an id that matches no row logs `document_status_changed_unknown_document`.
- Ingestion replaces the previous attempt's rows (`delete_for_document`,
  `replace_existing=True`, chunks wiped before upsert).
- `cancel_unfilled_tiers_for_portfolio` only touches tiers still in
  `pending_acceptance` / `accepted_awaiting_trigger`; a second run finds
  nothing and publishes nothing.
- `user_cleanup.delete_user_data` (ai's `user.deleted` handler) deletes
  only rows that are still there, so a redelivery does nothing. It runs
  with `strict=True`: a failed step raises, the entry stays pending and is
  retried.

### 3.5 Publishing after the commit

Rule: an event is sent only after the transaction that made the change
committed. The reason is the "dual write" problem: Redis and PostgreSQL are
two systems, and a write to both is not atomic. If the event goes first and
the commit then fails, a consumer reacts to something that does not exist.

How each service follows the rule:

- **core-service routes** use a transactional outbox (next
  subsection). The route writes the event as a row of `core.event_outbox`
  in the request's transaction (`outbox.enqueue`), then adds
  `background_tasks.add_task(publish_now, [entry])`. Repositories only
  `flush()`; the commit happens in `get_db`'s teardown, which (with
  `scope="function"`) runs before the response is sent; background tasks
  run after the response. `document.uploaded`, `document.deleted` and
  `portfolio.deleted` all follow this pattern. The `user.deleted`
  cleanup and the nightly deleted-user sweep in core commit each step
  themselves, then call `publish_now`. The operator script
  `services/core-service/scripts/delete_documents.py` uses the outbox
  too: per document it enqueues `document.deleted` in the delete's
  transaction, commits, then calls `outbox.publish_now` (which never
  raises; the dispatcher retries a failed publish).
- **auth-service** (`services/auth-service/events_out.py`): services
  collect `(EventName, payload)` pairs in `self.pending_events` while they
  work; the route calls `schedule_events(background_tasks,
  service.pending_events)`, which adds one background task
  `publish_events(pending)`. `publish_events` first drops cached
  organization memberships (`invalidate_membership_cache`) for every
  member a membership event affects, then publishes each event; a publish
  failure is logged (`event_publish_failed`) and never raised.
  `user.deleted` (`ALSO_DURABLE_EVENTS`) is also XADDed to its stream for
  ai-service (`durable_event_publish_failed` on error). The
  membership events are those in `MEMBERSHIP_EVENTS` (organization
  created, deleted, member joined, left, removed, role changed); the
  affected member is the payload's `user_id`, and for a deleted
  organization every id in `member_ids`. The service already dropped these
  cache entries once before the commit; the second drop here, after the
  commit, removes an entry that another request may have cached from the
  old data in between.
- **ai-service** (`services/ai-service/ai_events.py`): routes use the same
  pattern (`background_tasks.add_task(publish_events,
  list(service.pending_events))` in `domains/analysis/api.py::record_decision`);
  workflows publish right after their own `session.commit()` (tier
  monitoring commits per tier and then publishes that tier's event).
  `publish_event` never raises: it logs `ai_event_publish_failed` (error
  type and the first 200 characters of the message) and returns `False`.
  It is the only pub/sub publisher in ai-service: routes, workflows, the
  analysis workflow and the analysis queue's startup reconciliation all
  use it. The reason: if a publish could raise, a pub/sub outage would
  fail every analysis at `analysis.started`, and a failed
  `analysis.completed` publish would skip `recommendation.ready`. A
  publish problem must never change the outcome of the work. Tests
  capture these publishes by patching `shared.events.publish_cross_service`
  (fixture `mock_cross_service`).

#### The transactional outbox (core-service's durable events)

A **transactional outbox** is a database table that holds events waiting
to be sent. The event row is written in the same transaction as the
change it announces. So either both are committed or neither is. A
separate loop (the "dispatcher") then copies the rows to Redis. This
closes the "dual write" gap: publishing only once, right after the commit,
loses the event if Redis is down at that moment or the process stops in
between, and that could leave a document never indexed or a deleted
portfolio's tiers still open.

core-service uses an outbox for its three durable events
(`document.uploaded`, `document.deleted`, `portfolio.deleted`). The code is
in `services/core-service/outbox.py`, the table model in
the same file and the migration in
`services/core-service/migrations/versions/f1200c000001_event_outbox.py`.

```
 request transaction                         after the response
 -------------------                         ------------------
 change rows (flush)                         publish_now([entry])  (background task)
 outbox.enqueue -> INSERT core.event_outbox    XADD stream:<event> -> UPDATE published_at
 get_db: COMMIT  (both or neither)             failure: log outbox_publish_deferred, stop

 run_outbox_dispatcher (lifespan task, every 5 s)
   SELECT ... WHERE published_at IS NULL AND created_at < now - 10 s
          ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED
   for each row: attempts += 1; XADD
        ok   -> published_at = now, last_error = NULL
        fail -> last_error = error, log outbox_publish_failed, stop this pass
   COMMIT; gauge alphamind_outbox_unpublished_events = count of unpublished rows
   once an hour: DELETE rows published more than 7 days ago (log outbox_purged)
```

Table `core.event_outbox`: `id` (UUID), `event_name`, `payload` (JSONB),
`created_at`, `published_at` (NULL until published), `attempts`,
`last_error`. The partial index `ix_event_outbox_unpublished` covers
`created_at` for rows `WHERE published_at IS NULL`, the rows the dispatcher
looks for. See [../ERD.md](../ERD.md).

Details:

- `enqueue(session, event_name, payload)` adds the row to the caller's
  session and flushes it; it does not commit. The row commits or rolls
  back together with the change it announces.
- `publish_now` publishes the request's events in order and stops at the
  first failure (`outbox_publish_deferred`); the rest stay for the
  dispatcher. It never raises. The published rows are then marked in a
  new session (`published_at = now()`, `attempts + 1`, only rows still
  unpublished). If publishing worked but marking the rows failed, it logs
  `outbox_mark_published_failed`; the dispatcher will publish those rows a
  second time.
- The dispatcher stores at most the first 500 characters of the error in
  `last_error`. An unexpected error in a whole pass is logged
  (`outbox_dispatch_loop_failed`) and the loop goes on; only cancellation
  (shutdown) stops it. The purge runs at most once per
  `_PURGE_EVERY_SECONDS` (3600 s), checked after each pass.
- The dispatcher only takes rows older than 10 seconds
  (`OUTBOX_GRACE_SECONDS`), so it does not race the normal
  `publish_now` path. It goes oldest first, 100 rows at a time
  (`OUTBOX_BATCH_SIZE`), every 5 seconds
  (`OUTBOX_DISPATCH_INTERVAL_SECONDS`). It stops at the first failure,
  because when Redis is down every later row would fail the same way.
- `FOR UPDATE SKIP LOCKED` locks the selected rows and skips rows another
  transaction already locked. So two core-service replicas never publish
  the same row at the same time.
- Delivery is **at least once**: an event can reach the stream twice.
  ai-service's handlers are idempotent (section 3.4), so a duplicate does
  no harm.
- If rows stay unpublished, the alert `OutboxBacklog`
  (`alphamind_outbox_unpublished_events > 0` for 15 minutes, group
  `durable_events` in `infra/prometheus/alert_rules.yml`) fires.

**ai-service's `document.status_changed`** is not in an outbox. It is
published from the ingestion workflow, not from a request transaction.
`_publish_status` in `services/ai-service/ai/workflows/rag_ingestion_workflow.py`
retries the durable publish after 1, 5 and 15 seconds
(`_STATUS_PUBLISH_RETRY_WAITS_SECONDS`, log
`document_status_publish_retrying`), so a short Redis blip does not lose
the status; if the last try fails, it raises. The pub/sub live update that
follows is best effort: a failure is only logged
(`document_status_live_publish_failed`). Both publishes are called with
`log=False` and one combined debug line `document_status_changed_published`
is written instead, so a status change does not print two identical
"published" lines.

For the pub/sub events of all services the gap remains: if the process
dies, or Redis is unreachable, between the commit and the publish, the
event is lost. The effect is a missing notification or a missing live
update.

Presence follows the same rule: `AuthService.set_presence`, `_mark_online`
and `logout` queue the event built by `presence_event()` on
`pending_events`, and the route publishes it after the commit with
`schedule_events`. A Redis failure is only logged and never fails a login.

### 3.6 Sequence: document upload -> ingestion -> status

```
Browser        core-service              Postgres     Redis DB3 streams        ai-service              Qdrant
  |  POST /api/v1/research/documents       |              |                       |                      |
  |---------------->|                      |              |                       |                      |
  |                 | upload object (S3)   |              |                       |                      |
  |                 | INSERT documents     |              |                       |                      |
  |                 | (status=uploaded)    |              |                       |                      |
  |                 | + event_outbox row ->| flush        |                       |                      |
  |                 | get_db commit ------>| COMMIT       |                       |                      |
  |<-- 201 ---------|                      |              |                       |                      |
  |                 | BackgroundTask publish_now: XADD stream:document.uploaded |                      |
  |                 |------------------------------------>|                       |                      |
  |                 |                      |              | XREADGROUP workers    |                      |
  |                 |                      |              |---------------------->| _handle_document_uploaded
  |                 |                      |              |                       | run_rag_ingestion_workflow
  |                 |   status parsing: XADD stream:document.status_changed + PUBLISH events:document.status_changed
  |                 |<------------------------------------|<----------------------|                      |
  |                 | durable: transition_status(parsing) |                       | parse, chunk         |
  |  WS document:<id> <- pub/sub push "document.status_changed"                   |                      |
  |                 |   ... chunking, embedding (same two publishes each) ...     | embed, index ------->|
  |                 |                      |              |                       | commit chunks, job   |
  |                 |   status ready (two publishes)      |<----------------------|                      |
  |                 | transition_status(ready) -> COMMIT  |                       |                      |
  |                 | notify owner "Document ready"       |       XACK stream:document.uploaded <--------|
  |  WS user:<id>  <- "notification"                      |                       |                      |
```

Details:

- Every status (`parsing`, `chunking`, `embedding`, `ready`, `failed`) is
  published twice by `_publish_status`: once durable (core persists it) and
  once pub/sub (core pushes it to `document:<id>` sockets). Persisting must
  not be lost; the live push may be. With pub/sub alone, a status
  published while core's subscriber was reconnecting would be gone, and
  the document would stay at `uploaded` for good although ai-service had
  indexed it; that is why the status write is durable.
- The handler runs the whole ingestion inline (awaited, not a background
  task: the consumer loop is already off the request path, so there is no
  response to unblock). The entry is acknowledged only after the workflow
  returns. Ingestion jobs therefore run one at a time; if the document
  volume ever grows, this handler is the place to add concurrency, not the
  subscriber loop.
- A permanent failure (empty text, zero chunks, a parse error) publishes
  `failed` and returns normally, so the entry is acknowledged. A transient
  failure (provider rate limit, including `EmbeddingRateLimitedError`
  from the Gemini pool, outage, timeout) publishes `failed` and
  raises `TransientIngestionError`; the entry stays pending and is
  retried (core then sees `failed -> parsing`, which is allowed).
- core notifies the owner on every real transition to `ready` or
  `failed`. It cannot tell a transient `failed` from a final one, so after
  a transient failure the owner first gets "Document processing failed"
  and, when the retry succeeds, "Document ready" (chapter 12, section
  3.10).
- System documents (owner `SYSTEM_DOCUMENT_OWNER_ID`) get no
  notification.
- If ai-service stops for good in the middle (the entry is dead-lettered
  after five failed deliveries), the document stays at the last status
  that reached core. A re-upload of the same file publishes
  `document.uploaded` again once the status has not changed for 10
  minutes (`uploaded`) or 60 minutes (`parsing`, `chunking`,
  `embedding`),
  and auto-ingestion's dedup list leaves such documents out, so a stuck
  system document is submitted again on the next nightly run (chapter
  12, section 3.10).

### 3.7 Sequence: portfolio deleted -> tiers cancelled

```
Browser       core-service            Postgres(core)   Redis             ai-service              Postgres(ai)
  | DELETE /api/v1/portfolios/{id}        |              |                    |                       |
  |--------------->| PortfolioService.delete             |                    |                       |
  |                | commit (get_db) ---->| COMMIT       |                    |                       |
  |<-- 204 --------|                      |              |                    |                       |
  |                | BG: XADD stream:portfolio.deleted {portfolio_id,user_id}|                       |
  |                |------------------------------------>|                    |                       |
  |                | BG: notify_users "Portfolio deleted"|                    |                       |
  |                |                      |              | XREADGROUP ------->| _handle_portfolio_deleted
  |                |                      |              |                    | cancel_unfilled_tiers_for_portfolio
  |                |                      |              |                    |   UPDATE tiers status=expired
  |                |                      |              |                    |   append note to recommendation
  |                |                      |              |                    |------------------> COMMIT
  |                |                      |              |                    | log portfolio_deleted_tiers_cancelled
  |                |   PUBLISH events:tier.cancelled (one per tier, reason=portfolio_deleted)
  |                |<------------------------------------|<-------------------|                       |
  |                | _handle_tier_cancelled:             |       XACK <-------|                       |
  |                |   push analysis:<id>                |                    |                       |
  |                |   claim, notify user_id (fallback = owner from the event)|                       |
```

Why durable: if ai-service is down when the portfolio is deleted, the
entry waits in the stream. Without it, the deleted portfolio's accepted
tiers would stay open, and the daily tier-monitoring job could later try to
trade into a portfolio that no longer exists.

`expired` is used because `TierStatus` has no "cancelled" value; it is the
existing "can no longer fill, nothing was traded" state. The note
`Tier cancelled: the portfolio was deleted (portfolio_deleted).` is
appended to the recommendation's `tier_generation_note`.

### 3.8 From event to browser

core-service's handlers in `event_listener.py` do up to three things:

1. **Live push** with `websocket_manager.manager.broadcast(channel,
   WebSocketEnvelope(event=<name>, payload=<payload>))`. Channels:
   `analysis:<analysis_id>` (`_broadcast_analysis`, used by the analysis,
   recommendation, decision and tier events), `document:<document_id>`
   (document status), `presence:<user_id>` and each accepted friend's
   `user:<friend_id>` (presence). These broadcasts run
   on every replica for its own sockets and are not relayed between
   replicas. The browser receives the payload unchanged, including the
   `event_id` field.
2. **Notification** with `domains/notification/service.py::notify_user`
   (after `_claim_event`). It first reads the user's delivery preferences.
   If the user turned in-app notifications off, no row is written and only
   the optional email is sent. Otherwise it writes a row in
   `core.notifications`, commits, then
   `NotificationService.deliver_notification` pushes
   `{"event": "notification", "payload": <row>}` to `user:<user_id>` with
   `cross_replica=True` (relayed to other replicas through the Redis
   channel `ws:fanout`) and sends an email if the user enabled it.
3. **Access re-check of open sockets** (`_recheck_sessions`, which
   calls `api/websocket.py::recheck_user_sessions`). Nine events
   change what a user may do with a note or a channel: `organization.member.removed`,
   `organization.member.left`, `organization.role.changed`,
   `organization.deleted` (for every id in `member_ids`),
   `user.role.changed`, `user.deactivated` and `user.deleted` (the last
   two close every socket of the user), `friend.removed` (every action;
   `actor_id` and `user_id`) and `connection.blocked` (`actor_id` and
   `blocked_user_id`). The last two close an open `presence:<id>` socket
   with 4403 once the two users are no longer connected.
   `auth_client.are_users_connected` answers False on an auth-service
   HTTP error, so during an auth outage this re-check closes presence
   sockets instead of keeping them open. The re-check runs on every replica
   (each replica only knows its own sockets), before the event claim, and
   never raises: a failure is logged (`yjs_session_recheck_failed`) and the
   notification still goes out. auth-service drops the membership cache
   and mirrors the user state before it publishes, so the re-check already
   sees the new rights. The same re-check covers the user's `/ws/{channel}`
   sockets: 4401 "account deactivated" for a deactivated or deleted user,
   4403 "access to this channel was revoked" for a channel the user can no
   longer read (log `websocket_session_closed_on_access_change`); if the
   check itself fails the socket stays open (`websocket_session_recheck_failed`).
   Details in [25-realtime-yjs.md](25-realtime-yjs.md).

Handlers that need a recipient and find none log a warning and stop:
the notify handlers for one recipient (analysis, recommendation,
decision and backtest events) log `event_missing_user_id` (fields
`event_name`, `analysis_id`; for example a system analysis), the
actor-only ones (`connection.blocked`, `friend.removed` other than
`removed`, `user.deleted`) log `event_missing_actor_id`, and the handlers
that notify the user and the actor (`_notify_pair`: friend, user and
invitation events) log `event_missing_recipient` when both are missing;
`organization_member_joined_missing_user_id`,
`user_presence_changed_missing_user_id` and `tier_event_without_recipient`
are logged by their own handlers (`_handle_organization_member_joined`,
`_handle_user_presence_changed`, `_handle_tier_event` in
`event_listener.py`).

The WebSocket side (channels, authorization, message shapes) is in
[25-realtime-yjs.md](25-realtime-yjs.md).

### 3.9 Other Redis pub/sub channels you will see

They are not "events" in the registry sense, but `PUBSUB CHANNELS` shows
them:

| Channel | Who publishes | Who listens | Purpose |
|---|---|---|---|
| `ws:fanout` | core `websocket_manager._publish_fanout` | every core replica (`run_cross_replica_listener`) | relay single-origin pushes (notifications) to other replicas |
| `yjs:note:<note_id>` (pattern `yjs:note:*`) | core `yjs_relay._publish_cross_replica` | every core replica (`PSUBSCRIBE`) | relay Yjs edits between replicas |

## 4. Configuration

| Setting / constant | Default | Meaning | File |
|---|---|---|---|
| `REDIS_DB_EVENTS` | `2` | Redis DB for the events client (claim keys, Yjs active-room keys; pub/sub itself ignores the DB) | `shared/config.py` |
| `REDIS_DB_QUEUE` | `3` | Redis DB of the durable streams | `shared/config.py` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_URL` | `redis` / `6379` / unset | connection; a DB number in `REDIS_URL` is stripped on purpose | `shared/config.py`, `shared/redis.py`, `.env.example` |
| `socket_timeout` | `15` s | longer than the 5 s blocking read | `shared/redis.py::_get_client` |
| `_CONSUMER_GROUP` | `"workers"` | consumer group name | `shared/events.py` |
| `_MAX_DELIVERY_ATTEMPTS` | `5` | deliveries before dead-letter | `shared/events.py` |
| `_RECLAIM_EVERY_N_ITERATIONS` | `12` | loop iterations between reclaims (about 60 s idle) | `shared/events.py` |
| `XAUTOCLAIM min_idle_time` | `60000` ms | how long an entry must be idle before it is retried | `shared/events.py::_reclaim_pending` |
| `XREADGROUP COUNT / BLOCK` | `10` / `5000` ms | batch size and wait | `shared/events.py::DurableEventSubscriber.run` |
| `_STREAM_MAXLEN` | `10_000` (approximate) | maximum entries kept per stream | `shared/events.py` |
| `RECONNECT_BACKOFF_SECONDS` | `(1, 2, 5, 10, 30)` | pub/sub reconnect delays | `shared/events.py` |
| `_EVENT_CLAIM_TTL_SECONDS` | `300` | lifetime of `core:event_claim:*` keys | `services/core-service/event_listener.py` |
| `SERVICE_NAME` | `auth-service` / `core-service` / `ai-service` | also the durable consumer name | `services/*/config.py`, `docker-compose.yaml` |
| Redis persistence | `--appendonly yes --appendfsync everysec` | streams survive a crash (at most about 1 s lost) | `docker-compose.yaml` (`redis`) |
| `STUCK_DOCUMENT_AFTER` | 10 minutes | a document still `uploaded` this long after its last status change (`status_changed_at`): re-uploading the same file re-sends `document.uploaded` | `services/core-service/domains/research/repository.py` |
| `STUCK_PROCESSING_AFTER` | 60 minutes | the same for `parsing`, `chunking` and `embedding`, which can last long for a large filing | same |
| `OUTBOX_DISPATCH_INTERVAL_SECONDS` | `5.0` | how often the outbox dispatcher runs | `services/core-service/outbox.py` |
| `OUTBOX_GRACE_SECONDS` | `10.0` | an outbox row must be at least this old before the dispatcher publishes it | `services/core-service/outbox.py` |
| `OUTBOX_BATCH_SIZE` | `100` | outbox rows per dispatcher pass | `services/core-service/outbox.py` |
| `OUTBOX_RETENTION_DAYS` | `7` | published outbox rows older than this are deleted | `services/core-service/outbox.py` |
| `_PURGE_EVERY_SECONDS` | `3600.0` | how often the dispatcher runs the purge | `services/core-service/outbox.py` |
| `_UPLOADED_REQUIRED_FIELDS` | `document_id, storage_bucket, storage_key, document_type, category` | fields a `document.uploaded` payload must carry as non-empty strings | `services/ai-service/event_listener.py` |
| `_STATUS_PUBLISH_RETRY_WAITS_SECONDS` | `(1.0, 5.0, 15.0)` | waits before re-trying the durable `document.status_changed` publish | `services/ai-service/ai/workflows/rag_ingestion_workflow.py` |

## 5. Design decisions and why

**Two transports instead of one.**
Decision: pub/sub for notifications and live pushes, streams for five
events. Alternative: everything on streams. Reason: a consumer group gives
each entry to ONE consumer, which is the wrong shape for "tell every
replica so it can push to its own sockets". And a missed live update only
costs a refresh. Streams add real machinery (groups, acks, reclaim, dead
letter), so they are used only where a lost event is a real bug.

**Durable streams where a lost event is a silent data bug.**
With a plain `PUBLISH`, a document uploaded while ai-service's subscriber
is reconnecting (during a restart) keeps its row and its file, but the
"process this" message is gone: the document stays at `uploaded` with
zero chunks, with no error anywhere.
`document.deleted` is the durable counterpart: without it, deleting a
document would leave its chunks in Qdrant and BM25, orphaned but still
searchable. `portfolio.deleted` is durable because a missed one would
leave tiers that later try to trade in a portfolio that no longer exists.
`user.deleted` goes on a stream for ai so that a restart of ai-service
does not skip a deleted user's cleanup; the nightly sweep is the
backstop for a publish that never happened.

**`document.status_changed` is published twice.**
Decision: one durable publish (core persists the status) and one pub/sub
publish (core pushes to the browser). Alternative: one handler on one
transport. Reason: the two halves have different needs. The database write
must not be lost; the screen update may be. Splitting them keeps the push
cheap and the write safe.

**Claim keys instead of a single "leader" replica.**
Each replica handles every pub/sub event (for its own sockets), and a
`SET NX EX` key decides which one writes the notification. Alternative:
elect one replica to do all notifications. Reason: the claim is one Redis
command, needs no coordination, and fails open.

**`event_id` per publish.**
A claim keyed by a hash of the payload makes two genuine events with the
same content within the 5-minute claim window (deactivate -> reactivate ->
deactivate) collide, and the second notification is dropped. A random id
per publish tells them apart. It is added on the wire only; the caller's
dictionary is not changed.

**A transactional outbox for core-service's durable events; publish after
commit for the rest.**
Alternative: publish every event once from a background task after the
commit. Reason: that design covers the common failure (the commit fails
after the event was sent), but a Redis outage or a process stop between
the commit and the publish loses the event for good. For the three core-service durable events that meant a
document never indexed or removed, or a deleted portfolio's tiers left
open. The outbox closes that gap with one table and one lifespan task in
core-service, not a separate process: the dispatcher runs inside
core-service. Pub/sub events stay on publish-after-commit, because a lost
one costs only a notification or a live update.

**Dead-letter after 5 attempts.**
Alternative: retry forever. Reason: an entry that fails 5 times about a
minute apart will not start working on attempt 11. Retrying forever would
block the reclaim loop and fill the logs. The dead letter is logged at
error level for a human to inspect.

**Ingestion runs inline in the consumer loop.**
Alternative: start a background task per event. Reason: an inline await
means the entry is acknowledged only after the work really finished, so a
crash mid-ingestion leads to a retry. The cost is that ingestion jobs (and
the other durable events of ai-service) wait behind each other, which
is fine at this project's document volume.

## 6. When things go wrong

| Situation | What the system does | How to diagnose |
|---|---|---|
| ai-service is down when a document is uploaded | the entry waits in `stream:document.uploaded`; on start, `XREADGROUP >` delivers the backlog | `bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded` shows `lag` > 0 |
| ai-service crashes during an ingestion | the entry stays pending for consumer `ai-service`; after restart `_reclaim_pending` retries it once it has been idle 60 s | `XPENDING stream:document.uploaded workers` (DB 3) |
| core-service is down when ai publishes a pub/sub event | that event is lost for core (no notification, no push); the data itself is in ai's database | the user can refresh; nothing to repair |
| core-service is down during ingestion | the `document.status_changed` entries wait in DB 3; core persists them when it starts | `XINFO GROUPS stream:document.status_changed` |
| A handler keeps failing | 5 attempts about 1 min apart, then dead-letter | log events `durable_event_handler_failed` (with `delivery_count`) and `durable_event_dead_lettered` |
| Redis is down at publish time | pub/sub: logged (`event_publish_failed`, `ai_event_publish_failed`), work continues; core-service durable: the row stays in `core.event_outbox` (`outbox_publish_deferred`, then `outbox_publish_failed` every 5 s) and is published when Redis is back; ai-service `document.status_changed`: retried after 1, 5 and 15 s, then the ingestion raises | service logs; `RedisDown` alert; `OutboxBacklog` alert after 15 minutes ([27-observability.md](27-observability.md)) |
| Outbox rows not published | the dispatcher retries every 5 s, oldest first, and stops a pass at the first failure | `bash scripts/psql.sh -c "SELECT event_name, created_at, attempts, last_error FROM core.event_outbox WHERE published_at IS NULL ORDER BY created_at;"`; gauge `alphamind_outbox_unpublished_events` |
| A document stays at `uploaded`, `parsing`, `chunking` or `embedding` | its `document.uploaded` is still in the outbox, or ai-service dead-lettered the entry (before or during the ingestion) | `bash scripts/psql.sh -c "SELECT id, filename, status, status_changed_at FROM core.documents WHERE status NOT IN ('ready', 'failed') ORDER BY status_changed_at"`; re-upload the same file once the status has not changed for 10 minutes (`uploaded`) or 60 minutes (processing statuses): core logs `document_upload_republished` and sends the event again (a stuck system document is resubmitted by the next nightly ingestion) |
| Pub/sub listener lost Redis | it reconnects with backoff (1, 2, 5, 10, 30 s) | `cross_service_subscriber_failed` then `cross_service_subscriber_started` |
| Durable loop error (also while creating the groups at start) | logged, retried with backoff (1, 2, 5, 10, 30 s); a `NOGROUP` error recreates the groups | `durable_event_subscriber_loop_error` (with `groups_ready`, `retry_in_seconds`) |
| A background loop ended (any lifespan task of core-service or ai-service) | logged at error level by `log_unexpected_task_end`; the loop does not restart until the service restarts | `background_task_crashed` (with `task` and the traceback) or `background_task_ended` |
| The owner got "Document processing failed" and then "Document ready" | a transient ingestion error published `failed`, and the retry succeeded | ai-service log for the document id; the final status is the one in `core.documents` |
| A late or repeated status event | ignored (forward-only) | `document_status_transition_ignored` |
| A tier event without a recipient | logged, no notification | `tier_event_without_recipient` |
| Duplicate notification on two replicas | prevented by `core:event_claim:*`; if Redis is down the claim fails open and duplicates are possible | `event_claim_failed` |
| A consumer is down for a very long time | the stream keeps at most about 10 000 entries; older ones are trimmed and lost | `XLEN stream:<name>` |

Useful log events at INFO level (visible by default), by service:

| Service | Events |
|---|---|
| core-service and ai-service, at start | `durable_event_subscriber_started`, `cross_service_subscriber_started` (core only: ai has no pub/sub subscriber) |
| ai-service | `document_uploaded_received`, `document_deleted_received`, `rag_ingestion_complete`, `portfolio_deleted_tiers_cancelled` (with `count`), `user_deleted_received`, `ai_user_data_cleaned` (counts per step), `ai_user_data_cleanup_failed` |
| core-service and ai-service, nightly sweep | `deleted_user_sweep_completed` and `deleted_organization_sweep_completed` (core), each with candidates, deleted, cleaned, failed, lookup_failed and refused; `deleted_user_sweep_item_failed`; `deleted_organization_sweep_item_failed`; `deleted_user_sweep_lookup_failed`; `deleted_organization_sweep_lookup_failed` (core); `deleted_organization_sweep_refused` (core, error: safety brake); `deleted_user_sweep_candidates_failed` (candidate query failed), core also `deleted_organization_sweep_candidates_failed`, `document_file_owners_lookup_failed` (bucket listing failed) and `deleted_user_files_removed` (unreferenced files of a deleted user deleted) |
| core-service | `outbox_published_pending` (the dispatcher published rows the `publish_now` path could not), `outbox_purged`, `websocket_analysis_broadcast` (with `sockets=`, the number of local sockets reached, so "the client missed it" can be told apart from "nobody was connected"), `user_deleted_received`, `user_data_cleaned` and `organization_data_cleaned` (counts per step), `user_data_cleanup_failed` and `organization_data_cleanup_failed` (one step failed), `notification.skipped_deleted_user` |

Warnings worth a look: `document_uploaded_malformed`,
`document_upload_republished`, `outbox_publish_deferred`,
`outbox_publish_failed`, `event_claim_failed`, `yjs_session_recheck_failed`.
`cross_service_event_published`, `durable_event_published` and
`document_status_changed_published` are DEBUG lines (section 3.2).

## 7. Demo walkthrough

Goal: show both transports live. Use three terminals at the repository
root. Everything in steps 1-5 is read-only.

1. **List the live pub/sub channels.**
   ```
   bash scripts/redis-cli.sh PUBSUB CHANNELS
   bash scripts/redis-cli.sh PUBSUB NUMPAT
   ```
   The audience sees the 33 `events:*` channels core-service subscribes
   to, plus `ws:fanout`. `NUMPAT` is at least 1 (the Yjs pattern
   `yjs:note:*`). Say: "These are core-service's ears. ai-service has none,
   it only reads streams."

2. **Look at the durable streams and their consumer group** (database 3).
   ```
   bash scripts/redis-cli.sh -n 3 --scan --pattern 'stream:*'
   bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.uploaded
   bash scripts/redis-cli.sh -n 3 XINFO GROUPS stream:document.status_changed
   ```
   Point at `name workers`, `consumers 1`, `pending 0`, `lag 0`,
   `last-delivered-id`. Say: "pending 0 and lag 0 means the consumer is
   healthy and has nothing waiting."

3. **Read the last entries of a stream.**
   ```
   bash scripts/redis-cli.sh -n 3 XREVRANGE stream:document.status_changed + - COUNT 5
   bash scripts/redis-cli.sh -n 3 XRANGE stream:document.uploaded - + COUNT 5
   ```
   Each entry is an id plus one field `payload` with the JSON. Note that
   the payload has no `event_id` (durable payloads do not need one).

4. **Watch pub/sub events live.** In terminal A:
   ```
   bash scripts/redis-cli.sh PSUBSCRIBE 'events:*'
   ```
   In terminal B, trigger a harmless event: open a WebSocket (this marks
   the user online in auth-service, which publishes `user.presence.changed`
   when the user was offline). Use the client from
   [25-realtime-yjs.md](25-realtime-yjs.md) section 7 step 3, or:
   ```
   TOKEN=$(bash scripts/token.sh owner)
   docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" core-service python -c "
   import asyncio, os, websockets
   from jose import jwt
   async def main():
       uid = jwt.get_unverified_claims(os.environ['TOKEN'])['sub']
       async with websockets.connect(f'ws://localhost:8000/ws/presence:{uid}?token=' + os.environ['TOKEN']) as ws:
           print(await asyncio.wait_for(ws.recv(), 10))
   asyncio.run(main())
   "
   ```
   Terminal A shows `pmessage events:* events:user.presence.changed
   {"user_id": "...", "is_online": true, "event_id": "..."}` and terminal B
   usually prints the same payload wrapped in `{"event": ..., "payload": ...,
   "emitted_at": ...}` (the online call runs in the background while the
   socket subscribes, so the push can in rare cases arrive first and be
   missed). When the script closes its last socket, terminal A also shows
   `is_online: false`. If the user was already online (seen in the last 5
   minutes), no event is published: say so, and wait or use another user.
   (`jose` is python-jose, the JWT library the services use;
   `get_unverified_claims` only reads the user id from the token.)
   Stop terminal A with Ctrl-C.

5. **A short look at MONITOR** (shows every command Redis receives, with
   arguments; costs performance, so keep it to a few seconds):
   ```
   bash scripts/redis-cli.sh MONITOR
   ```
   Within 5 seconds you see core-service's and ai-service's
   `XREADGROUP ... BLOCK 5000` calls repeating. Press Ctrl-C. Say: "This is
   the durable consumer loop waiting for work."

6. **Optional, WRITES DATA (creates a document, uses the embedding
   provider): the full upload sequence.** Keep terminal A on
   `PSUBSCRIBE 'events:*'`. In terminal B:
   ```
   TOKEN=$(bash scripts/token.sh owner)
   F=/tmp/demo-event-$(date +%s).txt
   printf 'Demo note for the event chapter. Apple reported results.\n' > "$F"
   curl -sk -H "Authorization: Bearer $TOKEN" \
     -F file=@"$F" -F category=news -F ticker=AAPL \
     https://localhost:8443/api/v1/research/documents
   ```
   (A new file name each time: the same owner, ticker, category and file
   name again is treated as a duplicate and publishes nothing.)
   Terminal A shows `events:document.status_changed` with `parsing`,
   `chunking`, `embedding`, `ready`. Then:
   ```
   bash scripts/psql.sh -c "SELECT event_name, created_at, published_at, attempts FROM core.event_outbox ORDER BY created_at DESC LIMIT 3"
   bash scripts/redis-cli.sh -n 3 XREVRANGE stream:document.status_changed + - COUNT 5
   bash scripts/logs.sh ai-service grep document_uploaded_received
   bash scripts/psql.sh -c "SELECT id, status FROM core.documents ORDER BY uploaded_at DESC LIMIT 1"
   ```
   The outbox row of `document.uploaded` has a `published_at` a moment
   after its `created_at`: `publish_now` sent it right after the response.
   **DESTRUCTIVE cleanup** (deletes that document; publishes
   `document.deleted`):
   ```
   curl -sk -X DELETE -H "Authorization: Bearer $TOKEN" \
     https://localhost:8443/api/v1/research/documents/<document id>
   ```

What to say at the end: "Pub/sub is a broadcast: fast, no memory. Streams
are a mailbox: stored, acknowledged, retried. AlphaMind uses the mailbox
only where losing a message would leave the data wrong."

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Which events are durable?**
   `document.uploaded`, `document.deleted`, `portfolio.deleted` (core ->
   ai), the stream copy of `user.deleted` (auth -> ai) and the database
   half of `document.status_changed` (ai -> core).

2. **Why not make every event durable?**
   A consumer group gives each entry to one consumer, but live pushes must
   reach every core replica. And a lost notification only costs a refresh,
   not wrong data.

3. **What does `XACK` do and when does AlphaMind call it?**
   It removes the entry from the pending list. After the handler
   succeeded, when no handler exists, or after the 5th failed delivery
   (dead letter).

4. **A handler raises. When is the entry retried?**
   When `_reclaim_pending` runs (about every minute, or at startup) and the
   entry has been idle at least 60 seconds. After 5 deliveries it is
   dead-lettered.

5. **What is `event_id` for?**
   It makes each pub/sub publish unique so `_claim_event` can tell two
   real events with identical content apart; the first replica to set
   `core:event_claim:<event>:<event_id>` writes the notification.

6. **Why is `document.uploaded` published in a background task?**
   So it is sent after `get_db` committed the document row. Otherwise
   ai-service could receive an event for a row that was rolled back.

7. **The document stays at `uploaded`. What happened, and how do you fix it?**
   Either the event is still in `core.event_outbox` (Redis was down; the
   dispatcher publishes it when Redis is back), or ai-service did not
   process it (down, or the entry was dead-lettered). Re-uploading the
   same file 10 minutes after the last status change re-sends
   `document.uploaded`.

8. **What does the outbox guarantee, and what does it cost?**
   The event row is committed in the same transaction as the change, so
   an event exists exactly when its change exists, and the dispatcher
   retries until Redis accepts it. The cost is at-least-once delivery: an
   event can be published twice, so the handlers must be idempotent.

9. **Why does ai-service use the consumer name `ai-service` and not a random one?**
   Pending entries belong to a consumer name. A stable name lets the next
   run reclaim what the previous run left unacknowledged.

10. **Does ai-service listen to auth-service events?**
   Only to one: it reads `user.deleted` from `stream:user.deleted`. It
   has no pub/sub subscriber; only core-service listens to auth's
   pub/sub events.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `shared/events.py` | `EventName` (all event names), `EVENT_OWNERS`, `publish_cross_service`, `CrossServiceSubscriber`, `publish_durable_event`, `DurableEventSubscriber`, reconnect backoff |
| `shared/redis.py` | Redis clients per logical DB, `socket_timeout=15` |
| `services/core-service/event_listener.py` | `_claim_event`, every core handler, `_recheck_sessions`, `_PUBSUB_HANDLERS`, `build_subscriber`, `build_durable_subscriber` |
| `services/core-service/api/websocket.py` | `recheck_user_sessions` (open editor and channel sockets follow access changes) |
| `services/core-service/main.py` | starts the subscribers, listeners and the outbox dispatcher in the lifespan |
| `services/core-service/outbox.py` | transactional outbox: `enqueue`, `publish_now`, `dispatch_pending`, `purge_published`, `run_outbox_dispatcher`, table `core.event_outbox` |
| `services/core-service/domains/research/service.py` | `document.uploaded` / `document.deleted` written to the outbox, stuck-upload republish |
| `services/core-service/domains/research/repository.py` | `transition_status`, `is_forward_status_transition` |
| `services/core-service/domains/portfolio/api.py` | `portfolio.deleted` written to the outbox |
| `services/core-service/domains/notification/service.py` | `notify_user`, `notify_users`, `notify_after_commit` |
| `services/core-service/websocket_manager.py` | channel fan-out, `ws:fanout` relay |
| `services/ai-service/event_listener.py` | the four durable handlers |
| `services/ai-service/ai_events.py` | payload builders, `publish_event`, `publish_events`, tier cancel reasons |
| `services/ai-service/ai/workflows/rag_ingestion_workflow.py` | `_publish_status` (durable with retries + best-effort pub/sub) |
| `services/ai-service/ai/workflows/analysis_workflow.py` | analysis lifecycle publishes (through `ai_events.publish_event`) |
| `services/ai-service/domains/analysis/repository.py` | `cancel_unfilled_tiers_for_portfolio` |
| `services/auth-service/events_out.py` | `schedule_events`, `publish_events`, payload builders |
| `services/auth-service/domains/auth/service.py` | `presence_event` (queued on `pending_events`) |
| `docker-compose.yaml` (`redis`) | append-only persistence |
| `infra/prometheus/alert_rules.yml` (group `durable_events`) | `OutboxBacklog` alert |

## 10. Related chapters

- [23-redis.md](23-redis.md): Redis databases, persistence, keys.
- [25-realtime-yjs.md](25-realtime-yjs.md): WebSockets and the channels events are pushed to.
- [12-core-service.md](12-core-service.md), [13-ai-service.md](13-ai-service.md), [11-auth-service.md](11-auth-service.md): the services that produce and consume.
- [17-ai-rag.md](17-ai-rag.md): what ingestion does after `document.uploaded`.
- [19-ai-workflows-backtest.md](19-ai-workflows-backtest.md): tier monitoring, review and backtests that publish events.
- [27-observability.md](27-observability.md): logs and alerts for event problems.
- [../API_SPEC.md](../API_SPEC.md) section 6 (cross-service events), [../ERD.md](../ERD.md).
