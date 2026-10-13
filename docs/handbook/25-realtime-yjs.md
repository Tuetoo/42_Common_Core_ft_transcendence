# 25. Real-time: WebSockets and Yjs collaboration

> A normal HTTP request is "ask once, get one answer". Some screens need
> the server to *push* news the moment it happens: an analysis moved to
> the next step, a document finished processing, a friend came online, a
> colleague typed in a shared note. AlphaMind does this with WebSockets,
> all held by core-service. There are two kinds: JSON channels
> (`/ws/{channel}`) that relay events and notifications, and binary Yjs
> rooms (`/ws/yjs/{note_id}`) that let several people edit one research
> note at the same time. This chapter explains both, how each connection is
> authorized, what the messages look like, how edits are saved, and what
> happens when core-service restarts.

## 1. The big picture

```
 Browser tab                         gateway (nginx)            core-service
 -----------                         ---------------            ------------
 wss://localhost:8443/ws/user:<me>?token=...  --/ws-->  api/websocket.py  -----+
 wss://localhost:8443/ws/analysis:<id>?token=...        (JSON channels)        |
 wss://localhost:8443/ws/document:<id>?token=...                               v
 wss://localhost:8443/ws/presence:<friend>?token=...           websocket_manager.manager
                                                               channel -> set of sockets
 wss://localhost:8443/ws/yjs/<note_id>?token=... --/ws--> api/websocket.py (yjs)
                                                          yjs_relay.relay
                                                          note_id -> YjsRoom (y_py.YDoc)
                                                                    |
                                                  debounced snapshot v
                                                     core.research_notes.yjs_state
                                                     (+ content, search_vector)

 Inputs that cause JSON pushes:
   Redis pub/sub events:*   -> event_listener.py -> manager.broadcast(analysis:/document:/presence:)
   a new notification row    -> deliver_notification -> manager.broadcast(user:, cross_replica)
 Cross-replica relays (Redis pub/sub):
   ws:fanout        (notifications pushed by another replica)
   yjs:note:<id>    (Yjs updates applied on another replica)
```

- A **WebSocket** is one long-lived TCP connection that both sides may
  write to at any time. The browser opens it with an HTTP "upgrade"
  request. The gateway passes `/ws` to core-service with the `Upgrade`
  headers and `proxy_read_timeout 3600s` (`infra/gateway/nginx.conf`).
- **JSON channels** carry small JSON messages ("envelopes"). The server
  only sends; the client only listens (text frames from the client are
  read and ignored).
- **Yjs rooms** carry binary CRDT updates in both directions (explained in
  section 2).

ai-service and auth-service never hold WebSockets. They publish events
(see [24-events.md](24-events.md)); core-service turns those into pushes.

## 2. Concepts you need

| Term | Meaning here |
|---|---|
| WebSocket | A two-way connection over one TCP socket, opened by an HTTP upgrade. Here: `ws://` inside Docker, `wss://` (WebSocket over TLS) through the gateway. |
| Channel | A string `family:target`, e.g. `analysis:3f2c...`. A socket subscribes to exactly one channel (the path). |
| Envelope | The JSON shape of every message on a JSON channel: `{"event", "payload", "emitted_at"}` (`services/core-service/websocket_manager.py::WebSocketEnvelope`). |
| Close code | A number sent when a WebSocket closes. 1000-1015 are standard; 4000-4999 are free for applications. AlphaMind uses 4400, 4401, 4403, 4429, plus 1003, 1008, 1011. |
| Access token | The JWT the user got at login (see [11-auth-service.md](11-auth-service.md)). Browsers cannot set an `Authorization` header on a WebSocket handshake, so it is passed as `?token=...`. |
| Presence | Whether a user is online. auth-service stores `last_seen_at`; core-service reports socket activity to it. |
| CRDT | Conflict-free Replicated Data Type: a data structure that several people can change at the same time, offline or online, and every copy converges to the same result when all changes are merged, in any order, any number of times. |
| Yjs | A popular CRDT library (JavaScript). A Yjs **document** holds shared types; AlphaMind uses one `Y.Text` called `"content"` for the note body. |
| Update | A binary blob that describes changes to a Yjs document. Applying the same update twice is harmless (idempotent). Applying updates in a different order gives the same result (commutative). |
| State | A full encoding of a document (`encode_state_as_update`): one update containing everything. |
| y-py (`y_py`) | Python bindings of Yrs, the Rust port of Yjs. core-service uses it to keep the authoritative copy of each open note. |
| Room | The server's in-memory object for one note being edited: a `y_py.YDoc` plus the set of connected sockets (`yjs_relay.YjsRoom`). |
| Snapshot | Saving a room's full state into PostgreSQL (`research_notes.yjs_state`). Done with a debounce, not on every keystroke. |
| Debounce | Wait until "enough" has happened (20 updates) or "enough time" has passed (30 s) before doing an expensive action. |

## 3. How it works in AlphaMind

### 3.1 The JSON endpoint: `/ws/{channel}`

File: `services/core-service/api/websocket.py::websocket_endpoint`.
Shared helpers: `services/core-service/websocket_manager.py`.

Step by step:

1. **Token check** (`websocket_manager.authenticate`). The token is decoded with
   `shared.security.decode_token`. It must be an access token
   (`type == "access"`), its `sub` must be a UUID, and `exp` must be a
   number. Otherwise: close **4401** "invalid or missing token".
2. **Live account check** (`resolve_live_identity`). With
   `USER_STATE_CHECK_ENABLED` (default `true`) the live user state from
   `shared/user_state.py` replaces the token's role, and a deactivated
   user is refused with **4401** "account deactivated". If the state cannot
   be read, the token's role is kept.
3. **Channel authorization** (`authorize_channel`). The channel is split at
   the first `:`. The family must be in the allowlist
   `_CHANNEL_PREFIXES = ("user", "presence", "analysis", "document")` and
   the target must not be empty, else **4400**. Then per family:

   | Family | Allowed when | How it is checked |
   |---|---|---|
   | `user:<id>` | `<id>` is the caller | string compare |
   | `presence:<id>` | `<id>` is the caller, or an accepted friend | `auth_client.are_users_connected` -> auth `GET /internal/connections/check` |
   | `analysis:<id>` | the caller owns the analysis; for a system analysis (no user), the caller can read its portfolio | `ai_client.get_analysis_ref` (ai-service), then `PortfolioService.get` |
   | `document:<id>` | the caller can read the document (same rule as `GET /research/documents/{id}`) | `ResearchService.get_document` |

   Any exception during the check means "deny" (logged as
   `websocket_channel_auth_denied`), close **4403**.
4. **Per-user cap** (`manager.acquire_user`). At most
   `MAX_CONNECTIONS_PER_USER = 20` sockets per user in this process, JSON
   and Yjs sockets together. Over the cap: **4429**.
5. `websocket.accept()` and `manager.connect(websocket, channel)`.
6. For `analysis:` and `document:` sockets: log `websocket_opened` (and
   later `websocket_closed`). These are low-volume and help trace a missed
   live event.
7. For `analysis:` only: `send_analysis_status` sends the first message
   (section 3.3).
8. Receive loop under `serve_until_expiry`: the server reads frames only to
   notice a disconnect. A binary frame closes with **1003** ("text frames
   only"). When the token's `exp` is reached, the socket is closed with
   **4401** "token expired" (one `asyncio.timeout` per connection; a token
   that is already expired at this point closes at once). The client must
   reconnect with a fresh token.
9. Any unexpected error: logged `websocket_connection_failed`, close
   **1011**. In `finally`: `manager.disconnect` and `manager.release_user`.

**Accept-then-close.** All refusals call `accept_and_close`: the server
first accepts the handshake, then closes with the code. If the server
closed before accepting, the browser would only see a plain HTTP 403 on the
handshake and never the application code. `accept_and_close` and
`close_quietly` never raise (a failure is a DEBUG line,
`websocket_accept_close_failed` / `websocket_close_failed`), and
`close_quietly` only closes a socket that is still connected.

Close codes in one table (`websocket_manager.py`):

| Code | Name in code | When |
|---|---|---|
| 1003 | `CLOSE_UNSUPPORTED_DATA` | wrong frame type (binary on JSON, text on Yjs) |
| 1008 | `CLOSE_POLICY_VIOLATION` | Yjs update too large or does not decode |
| 1011 | `CLOSE_INTERNAL_ERROR` | unexpected server error; also a socket whose send failed |
| 4400 | `CLOSE_BAD_REQUEST` | unknown channel family or empty target |
| 4401 | `CLOSE_UNAUTHORIZED` | bad/expired token, deactivated account (at connect, or later through `recheck_user_sessions`), and also a malformed note id on `/ws/yjs/` (reason `invalid token or note id`) |
| 4403 | `CLOSE_FORBIDDEN` | not allowed to read that channel or note; also sent to every open Yjs socket of a note that a moderator hid or that was deleted (`close_note_sessions`, reason `note hidden by a moderator`, `note deleted by a moderator` or `note deleted`), to a Yjs socket whose user lost read access (reason `access to this note was revoked`), and to a writable Yjs socket whose user lost write access (reason `write access to this note was revoked`) |
| 4429 | `CLOSE_TOO_MANY_CONNECTIONS` | more than 20 sockets for this user |

### 3.2 Message shapes: `WebSocketEnvelope` (`websocket_manager.py`)

```python
class WebSocketEnvelope(BaseSchema, Generic[T]):
    event: str
    payload: T
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

Every JSON message is `envelope.model_dump_json()`, for example:

```json
{"event": "tier.executed",
 "payload": {"tier_id": "...", "analysis_id": "...", "user_id": "...",
             "portfolio_id": "...", "ticker": "AMD", "action": "buy",
             "quantity": "10", "price": "152.30", "reason": null,
             "event_id": "9c1e..."},
 "emitted_at": "2026-10-02T21:00:04.123456Z"}
```

What arrives on each channel:

| Channel | `event` values | Payload | Source |
|---|---|---|---|
| `user:<id>` | `notification` | the notification row: `id, user_id, type, title, content, read_at, created_at` (pushed only when the user's WebSocket delivery preference is on) | `NotificationService.deliver_notification` |
| `analysis:<id>` | `analysis.status` (first message), `analysis.started`, `analysis.completed`, `analysis.failed`, `recommendation.ready`, `decision.recorded`, `tier.executed`, `tier.execution_failed`, `tier.cancelled` | the event payload (see [24-events.md](24-events.md)), including `event_id` | `event_listener._broadcast_analysis` |
| `document:<id>` | `document.status_changed` | `document_id, status, event_id` | `_broadcast_document_status_changed` |
| `presence:<id>` | `user.presence.changed` | `user_id, is_online, event_id` | `_handle_user_presence_changed` |

### 3.3 The `analysis:<id>` channel: snapshot first, then live events

A client only learns the analysis id from the `POST /api/v1/analyses`
response. By the time it opens the socket, ai-service has usually already
published `analysis.started`. Pub/sub does not store events, so the client
would miss it and show a stale state.

So the first message on an `analysis:` socket is a **snapshot**:

```json
{"event": "analysis.status",
 "payload": {"analysis_id": "...", "ticker": "AAPL", "status": "running",
             "portfolio_id": null},
 "emitted_at": "2026-10-02T14:00:01.000000Z"}
```

`send_analysis_status` reads it from ai-service (`ai_client.get_analysis_ref`)
**after** `manager.connect`. Order matters: once the socket is in the
channel, no live event can fall in the gap between snapshot and stream. At
worst the client sees one state twice. The client should treat
`analysis.completed` and `analysis.failed` as final. If the lookup fails,
the snapshot is skipped (log `websocket_analysis_status_skipped`) and live
events still follow.

Another ordering detail (in ai-service): procedural memory and archival
writes run *after* `analysis.completed` and `recommendation.ready` are
published. These writes can take more than ten seconds. If they ran first,
the REST status would already say `completed` while the two events had not
reached the socket yet, and a client that stops listening at `completed`
would miss them.

### 3.4 The manager: `services/core-service/websocket_manager.py`

`WebSocketManager` keeps, per process:

- `_connections: dict[str, set[WebSocket]]` (channel -> sockets). An empty
  set is removed.
- `_user_counts` (open sockets per user) and one presence task per user.

Delivery rules:

- `broadcast(channel, envelope, cross_replica=False)` delivers through
  `_deliver_local`, which collects the distinct sockets of all target
  channels first (`sockets_for`, keyed by object id), so a socket is
  written at most once per push. It returns the
  number of local sockets reached (logged as `sockets=` in
  `websocket_analysis_broadcast`).
- `_deliver_local` sends to each socket. A socket whose `send_text` fails
  is logged (`websocket_send_failed`), removed and **closed with 1011**.
  Only removing it would leave a connection that is open but receives
  nothing: the client would never notice and never reconnect.
- A user with several sockets (several tabs) gets one copy per socket.
- `cross_replica=True` (used for notifications) also publishes
  `{"origin": <replica id>, "channels": [...], "text": ...}` to the Redis
  channel `ws:fanout`. Every replica's `run_cross_replica_listener`
  delivers it to its own sockets and ignores its own echo (same
  `_REPLICA_ID`, a random id made once per process). Event-driven
  broadcasts (analysis, document, presence) are not relayed: every replica
  already receives the event itself, so relaying would deliver twice. The
  relay publish is best effort (`websocket_fanout_publish_failed`; the
  local sockets already got the push); a `ws:fanout` message that is not
  valid JSON with `channels` and `text` is dropped
  (`websocket_fanout_message_invalid`). The listener reconnects with the
  same backoff as the event subscriber (`websocket_fanout_listener_failed`,
  `websocket_fanout_listener_stream_ended`).
- The manager holds sockets that are already accepted (`connect` does not
  accept; the endpoint accepts first so it can close with its own codes).
  `disconnect` is idempotent and removes a channel whose set is empty.

### 3.5 Presence: telling auth-service who is online

auth-service decides "online" from `users.last_seen_at`: online when it is
newer than `PRESENCE_ONLINE_THRESHOLD_MINUTES` (default 5,
`services/auth-service/config.py`). core-service knows when sockets open
and close, so it reports to auth:

1. The first socket of a user (`acquire_user`) starts `_presence_loop`.
2. The loop calls `auth_client.set_user_presence(user_id, online=True)`
   -> `POST /internal/users/{id}/presence` `{"online": true}`.
3. While the user has at least one socket, it repeats the online call
   every `PRESENCE_HEARTBEAT_SECONDS = 60` seconds (keeps `last_seen_at`
   fresh).
4. When the last socket closes (`release_user` sets the wake-up event), it
   sends `online=False`. If the user reconnected meanwhile, it sends
   `online=True` again.
5. One task per user means the calls reach auth-service in order (online,
   heartbeats, offline). Failures are logged (`auth_presence_update_failed`)
   and never break a socket.

On the auth side (`AuthService.set_presence`): `online=true` bumps
`last_seen_at` and publishes `user.presence.changed {is_online: true}` only
on an offline -> online transition; `online=false` moves `last_seen_at`
just past the threshold and publishes `is_online: false`. Login, token
refresh and logout (last session) also update presence. core-service then
pushes the event to `presence:<user_id>` sockets, i.e. to the user and to
accepted friends watching them.

### 3.6 Yjs collaborative editing: `/ws/yjs/{note_id}`

Files: `services/core-service/api/websocket.py` (`yjs_router`, endpoint),
`services/core-service/yjs_relay.py` (rooms, merge, snapshots, and the
safe decoding in its "Yjs decode/validate layer" section).

**The contract.** The note body is the `Y.Text` named `"content"`
(`ydoc.getText("content")`), markdown or plain text. Title and tags are
not in the Yjs document; they stay REST fields. Frames are raw Yjs updates
in both directions. This is **not** the y-websocket sync protocol (which
wraps updates in typed sync/awareness messages): a client sends raw
updates and receives raw updates.

**Connect** (`yjs_websocket_endpoint`):

1. Token and note id: invalid token or non-UUID note id -> **4401**
   (reason `invalid token or note id`). A malformed note id is answered
   with 4401, not 4400, so a client that gets 4401 on a Yjs socket should
   check the note id before refreshing its token.
2. Live account state: deactivated -> **4401**.
3. Access: `_note_access` calls `ResearchService.note_access(note, user)`,
   the same rule as the REST API. Read: the author, or any member of the
   note's organization (a hidden note only for its author and moderators).
   Write: needs `Permission.NOTE_WRITE` and being the author, or an
   organization MEMBER / ADMIN / OWNER (VIEWER is read-only). An unknown
   note means no access. No read access -> **4403**. An error during the
   check (for example auth-service down) is logged (`yjs_note_auth_failed`)
   and also means no access.
4. Per-user cap (shared with JSON sockets) -> **4429**.
5. `relay.connect(websocket, room_key)` where `room_key = str(note_uuid)`
   (canonical lower-case form, so two spellings share one room):
   - `_mark_room_active(note_id)` first (section 3.8);
   - `get_or_create_room`: if the room is not in memory, `_load_snapshot`
     reads `yjs_state` and `content` with a row lock. A note with content
     but no Yjs state yet is **seeded**: a fresh document whose "content"
     text is the current body is stored in the same locked transaction, so
     every replica starts from the same CRDT state;
   - the socket is added and receives **one binary frame with the room's
     full state** (sync step: the new client catches up at once instead of
     replaying history). If that first send fails, the socket is removed
     from the room again (and the room saved/evicted as on a normal
     disconnect) before the error propagates, so no room leaks.
6. The socket is registered as a session of its user (`_YjsSession`: socket,
   note id, token identity, current `NoteAccess`), so later access changes
   can find it (below).

**Receive loop:**

- A text frame -> close **1003** "binary frames only".
- A read-only participant's update is dropped (never applied or relayed);
  at most one `yjs_read_only_update_dropped` log line per 30 seconds per
  socket. The reader still receives the sync and all live updates. The
  loop reads the session's *current* `access.can_write` for every frame,
  so an upgrade (below) takes effect on the next frame; a downgrade
  closes the socket instead. The frontend
  should open the editor read-only when the REST note says `can_edit`
  is false.
- Otherwise `relay.handle_local_update(room, update, sender)`:
  1. size check: `len(update) > YJS_MAX_UPDATE_BYTES` (default 1 MiB) ->
     `InvalidYjsUpdate`;
  2. `validator.validate(update)` (in `yjs_relay.py`) decodes it in a separate worker
     process (below);
  3. under the room lock: apply to the room document, send to every other
     local socket of the room (not back to the sender), decide whether a
     snapshot is due;
  4. outside the lock: publish to Redis `yjs:note:<note_id>` for other
     replicas. The message is `<replica id>:<base64 of the update>`. Base64
     is needed because every Redis client is built with
     `decode_responses=True` and cannot carry raw bytes; the colon is a
     safe separator because base64 never contains `:`;
  5. if due: persist the snapshot (section 3.7).
- An `InvalidYjsUpdate` is logged (`yjs_invalid_update` with size and
  error) and closes **only that socket** with **1008**. The room and the
  other participants continue.
- The room lock (`YjsRoom.lock`) is not about threads (asyncio runs one
  coroutine at a time). It keeps the sequence "apply, broadcast, decide on
  a snapshot" of one update from being interleaved with another update of
  the same room. The Redis publish and the database write run outside the
  lock, so they never hold up the next update.
- A local socket whose `send_bytes` fails during a broadcast is removed
  from the room and closed (`ws.close()`, errors of the close itself
  ignored). Its own receive loop then ends, and its `finally` block runs
  as for any other disconnect.
- Any other error in the loop is logged (`yjs_connection_failed`) and
  closes the socket with 1011. In every case the `finally` block
  unregisters the session, runs `relay.disconnect` (which may save the
  room) and releases the per-user count.

**Why a separate process for decoding.** y_py is Rust code. Measured with
random byte strings: about 80% raise a normal `EncodingException`, about 4%
raise pyo3's `PanicException` (a `BaseException`), and about 16% abort the
whole process (a huge allocation from a corrupt length prefix, `SIGABRT`)
or segfault. No `except` can catch an abort. So every client update is
first decoded by a pool of `_VALIDATION_WORKERS = 2` small `python -c`
child processes with a `_VALIDATION_TIMEOUT_SECONDS = 5.0` limit
(`yjs_relay.UpdateValidator`, workers used in turn). How it works:

- Each worker is a plain `python -c` child (not `multiprocessing`), so
  nothing of the service is imported into it and a crash only ends that
  child. It runs with `RUST_BACKTRACE=0` and its stderr discarded.
- Protocol over the pipes: the service writes a 4-byte big-endian length
  and the update; the child answers one status byte (0 ok, 1 rejected)
  plus a length-prefixed reason (at most 200 characters).
- The blocking pipe I/O runs in a thread (`asyncio.to_thread`), so the
  child is not tied to an event loop; a lock serialises the requests to
  one child.
- A timeout (`yjs_validator_timeout`) or a dead child
  (`yjs_validator_worker_died`, an end of file or pipe error) kills the
  child and rejects the update ("decoder timed out" / "decoder process
  died"); the next request starts a new child.
- `validate` never raises for bad input; only an OS error while starting
  a child propagates.
- The decoder is deterministic, so bytes that decode in the child decode
  the same way in the service process. The in-process apply is still
  wrapped in `guarded`, which turns every failure (including pyo3's
  `PanicException`) into `YjsDecodeError`, for problems that only appear
  when the update is merged into a non-empty document. `guarded` lets
  `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` and
  `CancelledError` through.
- The workers are stopped at shutdown (`yjs_update_validator.aclose()` in
  the lifespan, run off the event loop because it waits for the
  children).

**Hide and delete end live sessions** (`YjsRelay.close_note_sessions(note_id,
*, reason, discard_edits)`). After the commit, as a background task,
moderation hide (`reason="note hidden by a moderator"`,
`discard_edits=False`), moderation delete (`"note deleted by a
moderator"`, `discard_edits=True`) and the owner's note delete in
`domains/research/api.py` (`"note deleted"`, `discard_edits=True`) close
every local socket of the note's room with **4403** and that reason (log
`yjs_note_sessions_closed`). Without this, a hidden note could still be
edited by sockets that were open before the hide. After a hide, unsaved
edits are saved by each socket's normal disconnect. After a delete, the
room is marked clean and dropped, because the note is gone. A client that
may still read the note (for example the author of a hidden note) can
reconnect. The call only reaches sockets on this replica (one core-service
replica is assumed; an accepted limitation).

**Rights change while a socket is open**
(`api/websocket.py::recheck_user_sessions(user_id, *, reason,
deactivated=False)`). A WebSocket is authorized only when it connects. So
that a removed, demoted or deactivated user does not keep editing until
their token expires (up to 15 minutes), core-service re-checks the open Yjs
sockets of a user when one of nine events arrives:
`organization.member.removed`, `organization.member.left`,
`organization.role.changed`, `organization.deleted` (every former member),
`user.role.changed`, `user.deactivated`, `user.deleted`, `friend.removed`
and `connection.blocked` (both users of the connection; see
[24-events.md](24-events.md) section 3.8). For every local socket of the
user:

1. With `deactivated=True` (the two account events), or when the live user
   state now says inactive (`resolve_live_identity` returns `None`), the
   socket is closed with **4401** "account deactivated".
2. Otherwise `_note_access` is run again with the live identity (the same
   rule as at connect). Not readable any more -> closed with **4403**
   "access to this note was revoked".
3. Still readable but no longer writable -> closed with **4403** "write
   access to this note was revoked". The frontend treats 4403 as final:
   the editor stops, says that access to the note changed and asks for a
   reload; the reload opens the editor read-only. A socket that only
   gains rights keeps running with the new rights.
4. If the new rights cannot be determined (the access check raises, for
   example auth-service is down), the socket keeps its current rights and
   `yjs_session_recheck_failed` is logged (fail open, the same rule as
   elsewhere).

Closings are logged as `yjs_session_closed_on_access_change` (with `code`
and `reason` = the event name). The same call also re-checks the user's
`/ws/{channel}` sockets: 4401 "account deactivated" for a deactivated or
deleted user, 4403 "access to this channel was revoked" when the channel
check now fails (log `websocket_session_closed_on_access_change` with
`channel`, `code`, `reason`); if the check itself fails, the socket stays
open (`websocket_session_recheck_failed`). auth-service drops the membership cache and
mirrors the new user state before it publishes, so the re-check sees the
new rights. The two connection events matter for `presence:<id>`
sockets: once the two users are no longer connected, the watcher's
socket is closed with 4403. `auth_client.are_users_connected` answers
False on an auth-service HTTP error, so during an auth outage this
re-check closes presence sockets instead of keeping them open. Like
`close_note_sessions`, it reaches only sockets on this replica.

An accepted limitation: `analysis:<id>` and `document:<id>` sockets are
not closed when the resource stops being shared (a portfolio PATCH to
`visibility="private"`, a portfolio delete, a document delete). No user
event is published for these changes, so an organization member's open
socket keeps receiving status payloads until its token expires (15
minutes or less). REST reads of the resource return 404 at once.

**Other replicas** (`run_cross_replica_listener`): `PSUBSCRIBE yjs:note:*`.
A message whose prefix is this replica's id is its own echo and is skipped
(Redis delivers a publish to the publisher too; without this check a single
replica would apply and broadcast every update twice). Otherwise
`apply_remote_update` applies it to the local room, if this replica has
that note open, and broadcasts it to all local sockets. Remote updates do
not count toward the snapshot debounce (the origin replica snapshots its
own edits). The publishing replica already validated the update; if it
still fails here it is dropped (`yjs_remote_update_rejected`). A broken
message is logged (`yjs_cross_replica_update_failed`). The listener
reconnects with the shared backoff (`yjs_cross_replica_listener_failed`,
`yjs_cross_replica_listener_stream_ended`).

### 3.7 Saving: snapshots and `flush_due_snapshots`

Writing the whole document on every keystroke would mean one database write
per keystroke per collaborator. So a room is saved when it is **dirty** and
either:

- `_updates_since_snapshot >= _SNAPSHOT_MIN_UPDATES` (20), or
- `time.monotonic() - _last_snapshot_at >= _SNAPSHOT_DEBOUNCE_SECONDS` (30).

`_persist_snapshot(note_id, state)`:

1. `SELECT yjs_state, content FROM research_notes ... FOR UPDATE`.
2. **Merge, not overwrite**: `merged = merge_states(stored, state)`
   (`_merge_with_stored`). Two replicas (or two call sites of one replica)
   can race; with a plain overwrite, the last writer could replace a newer
   state with an older one. A CRDT merge is commutative and idempotent, so
   the stored result is the union of both, whatever the order. When there
   is no stored state, the room state is stored as it is. When the stored
   state cannot be decoded (a corrupt row), it cannot be merged with
   anything: `yjs_stored_state_replaced` is logged (warning, with the
   decode error) and the room state is stored alone. The room itself
   started empty in that case (`yjs_stored_state_invalid`), so the room
   state is what every connected editor sees. Without this rule every save
   of the room would fail on the same bytes, the room would stay dirty and
   in memory, and the note would stay locked for REST edits (409).
3. Read the merged "content" text. If it cannot be read as text (a client
   that used another shared type), the stored `content` is kept and
   `yjs_text_extract_failed` is logged. If it changed, write it to `content`
   too, in the same transaction. `search_vector` is a generated column
   (`to_tsvector('english', title || ' ' || coalesce(content, ''))`), so
   full-text search follows automatically; `updated_at` moves (onupdate).
   If the text did not change, `updated_at` is kept as it is.
4. Commit. If the text changed, `schedule_note_sentiment(note_id)` asks
   ai-service to refresh the note's sentiment; while a refresh for the
   note is running, further saves only queue one follow-up (single-flight
   per note), so live typing makes at most one call in flight plus one
   queued.
5. Return `False` if the note no longer exists (deleted meanwhile). A real
   database error is not turned into `False`; it raises.

The room is marked clean (`mark_snapshotted`) **only after** a confirmed
write. If it were marked clean first, a failed write would leave the
database behind the room, and `should_snapshot()` would stay false until
enough new updates arrived: no retry, no error. On the update path, a
failed save is logged (`yjs_snapshot_persist_failed`) and the room stays
dirty, so the next update or heartbeat tries again; a note that vanished
logs `yjs_persist_snapshot_note_missing`.

Seeding (`_load_snapshot`) is not an edit: it stores the first `yjs_state`
but keeps `updated_at` as it is.

Four moments trigger a save:

| Trigger | Code |
|---|---|
| an update arrives and the debounce says "due" | `handle_local_update` |
| the heartbeat task, every 30 s, finds a due room, or a dirty room nobody is connected to (saved even before the debounce) | `run_active_room_heartbeat` -> `relay.flush_due_snapshots()` |
| the last socket of a room disconnects and the room is dirty | `relay.disconnect` (the room is removed from memory, unless the save raised an error) |
| core-service shuts down | lifespan -> `relay.persist_all_dirty()` (every dirty room, whatever the debounce; log `yjs_rooms_saved_on_shutdown`) |

**A failed save on disconnect keeps the room.** If the save in
`disconnect` raises (for example the database is down), the error is
logged (`yjs_snapshot_persist_failed_on_disconnect`) and the room stays in
memory with its unsaved edits. It is not evicted, because evicting it would
leave its edits only in the clients. On each heartbeat
`flush_due_snapshots` tries again for a room nobody is connected to (even
before the debounce) and evicts it once its edits are saved
(`yjs_snapshot_flush_failed` when the retry fails). If the note was
deleted meanwhile (`_persist_snapshot` returns `False`), there is nothing
left to save, so the room is marked clean and evicted too.

**Eviction is race-safe.** `_evict_if_empty` re-checks "nobody connected
and this is still the room in the map" under `_creation_lock`, the same
lock `get_or_create_room` takes. Without it, a client reconnecting while
the room was being saved could be handed the room that is about to be
removed, and its new connection would vanish with it. `disconnect` also
re-checks the connections under the room lock before it saves.

**A room with unsaved edits counts as open.** `_in_use(room)` is true
while the room has a connection *or* is dirty. `has_local_room` and
`local_active_note_ids` use it. So a dirty room with nobody connected
keeps its `yjs:active:<note_id>` marker (section 3.8), and a REST body
edit still gets 409 until the edits are saved. Otherwise the later save
would overwrite the REST edit.

The heartbeat flush is needed because the update path only saves when the
*next* update arrives. Without the heartbeat, the last edits before a user
stopped typing (editor still open) would never reach
`research_notes.content`: search and sentiment would be stale, REST body
edits would get 409, and a crash would lose them. An unexpected error in
the flush is logged (`yjs_snapshot_flush_loop_failed`) and the heartbeat
goes on.

Persistence table: `core.research_notes` (model
`services/core-service/domains/research/model.py::ResearchNote`), columns
`yjs_state` (bytea, NULL until the note is first opened collaboratively),
`content` (text), `search_vector` (generated tsvector), `updated_at`.

### 3.8 REST and Yjs together: the active-room marker

While a note is open in a Yjs room, a REST `PATCH` of its body would be
overwritten by the next snapshot. So:

- When a room opens, `_mark_room_active` adds this replica's id to the
  Redis sorted set `yjs:active:<note_id>` (DB 2) with score
  "now + `_ACTIVE_TTL_SECONDS` (90)", and sets the key to expire after 90 s.
- `run_active_room_heartbeat` refreshes the markers every
  `ACTIVE_HEARTBEAT_SECONDS = 30`.
- If loading the room fails (for example a database error),
  `relay.connect` removes this replica's marker at once, so the note does
  not answer a false 409 for up to 90 s.
- When the room is evicted, `_mark_room_inactive` removes the member.
  A room is evicted only when nobody is connected and its edits are
  saved, so a dirty room keeps its marker.
- `is_note_room_active(note_id)` is true when this replica has the room
  locally, or when Redis has a member with a score in the future.
- The marker writes are best effort (`yjs_active_mark_failed`,
  `yjs_active_unmark_failed`). If Redis cannot be read,
  `is_note_room_active` logs `yjs_active_check_failed` and answers from
  the local rooms only, so with one replica the 409 rule still holds.
- `ResearchService` (note update) raises **409** "This note is being
  edited collaboratively..." for a body change while the room is active.
  Title changes are always allowed.
- A REST body edit when no room is open is applied to the stored
  `yjs_state` as one Yjs edit (`yjs_relay.replace_text`), not as a new
  document. It loads the stored state, compares its text with the new
  text, keeps the common start and end, and in one transaction deletes
  the old middle part and inserts the new one. Positions are counted in
  UTF-8 bytes, the unit `y_py` uses for text positions, and the cut is
  always on a character boundary. The encoded result is stored with the
  new `content`. A note that has no stored state yet gets a fresh state
  (`seed_state`); a stored state that cannot be decoded is replaced by a
  fresh one and `note_yjs_state_reseeded` is logged.

The marker is set **before** the snapshot is loaded, so a REST edit racing
a room opening either sees the room (409) or commits first and is read by
`_load_snapshot` (row lock).

**Why the REST edit keeps the history.** A fresh state for the new text
would carry new item ids from a new Yjs client id. An editor that still
holds the old document (its socket closed, for example on token expiry or
a core restart, and the room was saved and evicted) merges what the server
sends after it reconnects. With a fresh state it would keep the old text
and add the new text next to it, and its next updates would refer to
items the server never had, so the server would keep them pending and
never apply them. Applied as an edit of the existing state, the REST
change is just one more CRDT change: the reconnecting editor converges to
the new text, and its later updates apply normally.

### 3.9 What happens on restart

Rooms live only in the process memory (`YjsRelay._rooms`). On a restart:

- Every socket is closed. The server never reopens a socket; the client
  must reconnect (with a fresh token if the old one expired). A plain
  `WebSocket` object in the browser does not reconnect on its own, so the
  frontend must do it (most Yjs provider libraries already include a
  reconnect loop).
- A clean stop normally closes the sockets first, and each socket's
  `finally` runs `relay.disconnect`, which saves a dirty room before
  evicting it. After the background tasks stop, the lifespan also calls
  `relay.persist_all_dirty()`, which saves every room that is still dirty
  (for example one whose earlier save failed), before the database engine
  is closed. A hard kill (crash, out-of-memory, `docker kill`) skips
  both: edits made since the last snapshot (at most 20 updates or about 30
  seconds, plus up to one heartbeat interval) exist only in the clients'
  copies.
- After reconnect, the first frame is the state loaded from PostgreSQL. A
  Yjs client that still holds edits the server lost can send them again
  (for example its full state); the CRDT merge makes that safe. The server
  does not request it; that is the client's choice. This also holds when
  the body was changed through REST while the editor was disconnected,
  because a REST body edit is stored as a change of the existing Yjs state
  (section 3.8).
- The `yjs:active:*` markers of the dead process expire within 90 seconds;
  until then REST body edits get 409.
- JSON channel subscriptions are also in memory. Events published while
  the socket was down are not replayed. The `analysis.status` snapshot on
  reconnect covers the analysis channel; for notifications the client can
  read `GET /api/v1/notifications`.
- This is also why the development image stage (the Dockerfile
  development `CMD`) runs uvicorn without `--reload`: copying
  a set of changed files makes an auto-reloading server restart several
  times in a few seconds, and every reload drops every WebSocket and
  in-memory Yjs room. A code change therefore needs
  `docker compose restart core-service`.
- If `disconnect`'s save fails (database down), the error is logged
  (`yjs_snapshot_persist_failed_on_disconnect`) and the room stays in
  memory; the heartbeat retries the save (section 3.7). A save that still
  fails at shutdown is logged as `yjs_snapshot_shutdown_save_failed`.

## 4. Configuration

| Setting / constant | Default | Meaning | File |
|---|---|---|---|
| `YJS_MAX_UPDATE_BYTES` | `1048576` (1 MiB) | largest accepted client update | `services/core-service/config.py`, `.env.example` |
| `USER_STATE_CHECK_ENABLED` | `true` | live account check on connect | `shared/config.py`, `.env.example` |
| `PRESENCE_ONLINE_THRESHOLD_MINUTES` | `5` | "online" window in auth-service | `services/auth-service/config.py` |
| `MAX_CONNECTIONS_PER_USER` | `20` | sockets per user per process (JSON + Yjs) | `websocket_manager.py` |
| `PRESENCE_HEARTBEAT_SECONDS` | `60.0` | online heartbeat while connected | `websocket_manager.py` |
| `FANOUT_CHANNEL` | `"ws:fanout"` | cross-replica relay for notifications | `websocket_manager.py` |
| `_CHANNEL_PREFIXES` | `user, presence, analysis, document` | allowed channel families | `api/websocket.py` |
| `_SNAPSHOT_MIN_UPDATES` | `20` | updates before a snapshot | `yjs_relay.py` |
| `_SNAPSHOT_DEBOUNCE_SECONDS` | `30` | seconds before a snapshot | `yjs_relay.py` |
| `_ACTIVE_TTL_SECONDS` | `90` | lifetime of an active-room marker | `yjs_relay.py` |
| `ACTIVE_HEARTBEAT_SECONDS` | `30.0` | marker refresh and snapshot flush interval | `yjs_relay.py` |
| `_REDIS_CHANNEL_PREFIX` | `"yjs:note:"` | cross-replica Yjs channel | `yjs_relay.py` |
| `_VALIDATION_WORKERS` | `2` | decoder child processes | `yjs_relay.py` |
| `_VALIDATION_TIMEOUT_SECONDS` | `5.0` | decode time limit per update | `yjs_relay.py` |
| `_READ_ONLY_LOG_INTERVAL_SECONDS` | `30.0` | log throttle for dropped reader updates | `api/websocket.py` |
| `proxy_read_timeout` (`/ws`) | `3600s` | gateway idle limit for a socket | `infra/gateway/nginx.conf` |

## 5. Design decisions and why

**Only core-service holds sockets.** Alternative: each service pushes its
own updates. Reason: one place for authorization, one per-user cap, one
presence source, and the gateway routes only `/ws` to one service.
ai-service and auth-service stay plain HTTP + events.

**Token in the query string.** Browsers cannot set headers on the
WebSocket handshake. The token is short-lived, and the socket is closed
with 4401 when it expires, so a stolen URL stops working soon.

**Allowlist of channel families.** Every family must be listed in
`_CHANNEL_PREFIXES` with its own authorization rule; anything else is
4400. Accepting any channel string would let a client subscribe to
channels that no rule protects.

**Snapshot message on `analysis:`.** Alternative: store events and replay
them. Reason: pub/sub stores nothing, and storing per-analysis histories
would add machinery. One status read at subscribe time solves the real
problem (the first event is usually already gone).

**Local broadcast plus a relay only for single-origin pushes.** Event
pushes happen on every replica already (each replica receives the event).
Notifications are created by exactly one replica (the claim winner), so
only they need `ws:fanout`.

**Yjs with a server-side document.** Alternative: a pure relay that only
forwards bytes. Reason: a newly joined client needs the current merged
state, not a replay of all history; the server needs the text for search
and sentiment. Keeping a `YDoc` per open note gives both.

**Debounced, merged snapshots.** One write per 20 updates or 30 s instead
of per keystroke. Merging instead of overwriting removes the "older state
wins the race" problem between replicas.

**Decoding in child processes.** Measured: about 16% of random byte
strings crash y_py hard. Isolating the first decode protects the whole
core-service (all users, all routes) from one bad client.

**Rooms evicted when empty.** Keeping rooms forever would hold one `YDoc`
per note ever opened for the life of the process. The last disconnect
saves and evicts; reopening reloads from PostgreSQL (a small one-time
cost). A room whose save failed is the exception: it stays in memory
until the heartbeat saves it, because evicting it would throw away edits
that exist nowhere else on the server.

## 6. When things go wrong

| Symptom | Likely cause | How to check |
|---|---|---|
| Client closes immediately with 4401 | expired/invalid token, refresh token used instead of access, deactivated user | make a new token: `bash scripts/token.sh owner` |
| 4403 on `analysis:` | not the owner; ai-service unreachable (any error means deny) | core log `websocket_channel_auth_denied` |
| 4403 on an open Yjs socket | a moderator hid the note, it was deleted, the user lost access (removed from the organization), or lost write access (made organization VIEWER or guest; reason "write access to this note was revoked") | core log `yjs_note_sessions_closed` with `note_id` and `reason`, or `yjs_session_closed_on_access_change` |
| 4401 or 4403 on an open `/ws/{channel}` socket | the account was deactivated or deleted, or the user lost access to the channel | `websocket_session_closed_on_access_change` with `channel`, `code` and `reason` |
| 4401 on an open Yjs socket | token expired, or the account was deactivated or deleted | `yjs_session_closed_on_access_change` with `code=4401`; otherwise refresh the token |
| 4400 | channel family not in the allowlist, or `analysis:` with empty id | check the URL |
| 4429 | more than 20 sockets for that user | `WebSocketManager._user_counts` (in code only); close old tabs |
| 1011 after a while | a send to the socket failed; the server closed it | core log `websocket_send_failed` |
| Client sees no live event | event published before subscribe (use the `analysis.status` snapshot), event lost on pub/sub, or broadcast reached 0 sockets | core log `websocket_analysis_broadcast ... sockets=0`; [24-events.md](24-events.md) |
| 1008 on a Yjs socket | update larger than 1 MiB, or undecodable | core log `yjs_invalid_update` with `size` and `error` |
| Edits not in search results | snapshot not yet taken (up to 30 s + heartbeat) or persist failing | `yjs_snapshot_persist_failed`, `yjs_snapshot_persist_failed_on_disconnect`, `yjs_snapshot_flush_failed`; query below |
| REST edit returns 409 | a Yjs room is open somewhere, a room with unsaved edits is waiting for its save (nobody connected), or a dead replica's marker has not expired yet (90 s) | `bash scripts/redis-cli.sh -n 2 ZRANGE yjs:active:<note_id> 0 -1 WITHSCORES` |
| Stored Yjs state cannot be decoded | corrupt `yjs_state` | `yjs_stored_state_invalid` when the room opens: the room starts empty; at the first save `yjs_stored_state_replaced`: the room state replaces the corrupt row. A REST body edit of such a note logs `note_yjs_state_reseeded` and stores a fresh state |
| Friend's online dot never changes | presence calls to auth failing | `auth_presence_update_failed`, `presence_loop_failed` |

Check a note's saved state:

```
bash scripts/psql.sh -c "SELECT id, title, length(yjs_state) AS yjs_bytes, left(content, 60) AS content_start, updated_at FROM core.research_notes ORDER BY updated_at DESC LIMIT 5"
```

## 7. Demo walkthrough

Run the client inside the core-service container (it has Python,
`websockets` and `y_py`), talking to `ws://localhost:8000` directly. Steps
1-4 are read-only.

1. **Get a token and the ids you need.**
   ```
   TOKEN=$(bash scripts/token.sh owner)
   bash scripts/psql.sh -c "SELECT id, ticker, status FROM ai.analyses ORDER BY created_at DESC LIMIT 3"
   bash scripts/psql.sh -c "SELECT id, title FROM core.research_notes ORDER BY updated_at DESC LIMIT 3"
   ```
   Pick an analysis that belongs to the owner and a note the owner can read.

2. **Show a refusal (wrong channel family).**
   ```
   docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" core-service python -c "
   import asyncio, os, websockets
   async def main():
       async with websockets.connect('ws://localhost:8000/ws/secret:1?token=' + os.environ['TOKEN']) as ws:
           try:
               await ws.recv()
           except websockets.ConnectionClosed as e:
               print('closed', e.rcvd.code, e.rcvd.reason)
   asyncio.run(main())
   "
   ```
   Expected: `closed 4400 not authorized for this channel`. Say: "The
   server accepted and then closed with an application code, so the client
   learns why."

3. **Subscribe to an analysis channel.** Replace `<ANALYSIS_ID>`:
   ```
   docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" -e A=<ANALYSIS_ID> core-service python -c "
   import asyncio, os, websockets
   async def main():
       url = f\"ws://localhost:8000/ws/analysis:{os.environ['A']}?token={os.environ['TOKEN']}\"
       async with websockets.connect(url) as ws:
           print(await ws.recv())
           try:
               while True:
                   print(await asyncio.wait_for(ws.recv(), 30))
           except asyncio.TimeoutError:
               print('no more events in 30 s')
   asyncio.run(main())
   "
   ```
   The first line is `{"event":"analysis.status","payload":{...,"status":"completed",...}}`.
   For a finished analysis nothing else arrives. Then show the server side:
   ```
   bash scripts/logs.sh core-service grep websocket_opened
   ```

4. **Join a Yjs room read-only and print the note body.** Replace `<NOTE_ID>`:
   ```
   docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" -e N=<NOTE_ID> core-service python -c "
   import asyncio, os, websockets, y_py as Y
   async def main():
       url = f\"ws://localhost:8000/ws/yjs/{os.environ['N']}?token={os.environ['TOKEN']}\"
       async with websockets.connect(url) as ws:
           state = await ws.recv()
           doc = Y.YDoc(); Y.apply_update(doc, state)
           print(len(state), 'bytes of state')
           print(str(doc.get_text('content'))[:300])
   asyncio.run(main())
   "
   ```
   Say: "The first binary frame is the whole document. The client merges
   it into an empty YDoc and reads the shared text called content."
   While a room for a note is open, a REST body edit of that note gets
   409. (This script closes its socket right after printing, so the room
   closes again at once.)

5. **Optional, WRITES DATA (changes the note body): send one edit.**
   ```
   docker compose --env-file .env.docker exec -T -e TOKEN="$TOKEN" -e N=<NOTE_ID> core-service python -c "
   import asyncio, os, websockets, y_py as Y
   async def main():
       url = f\"ws://localhost:8000/ws/yjs/{os.environ['N']}?token={os.environ['TOKEN']}\"
       async with websockets.connect(url) as ws:
           doc = Y.YDoc(); Y.apply_update(doc, await ws.recv())
           before = Y.encode_state_vector(doc)
           text = doc.get_text('content')
           doc.transact(lambda txn: text.extend(txn, ' [demo edit]'))
           await ws.send(bytes(Y.encode_state_as_update(doc, before)))
           await asyncio.sleep(1)
   asyncio.run(main())
   "
   bash scripts/psql.sh -c "SELECT right(content, 40), updated_at FROM core.research_notes WHERE id = '<NOTE_ID>'"
   ```
   The token's user must be allowed to edit the note (its author, or an
   organization MEMBER or above); a read-only user's update is dropped
   without an error. The update is sent, the socket closes, `disconnect`
   saves the dirty room (when no other socket is still in the room;
   otherwise the save follows within the 30 s debounce), and the query
   shows the text ending with `[demo edit]`. Say: "Only
   the difference since the state vector is sent; the server merged it and
   wrote the text back to content."

6. **The same through the gateway (from the host).** Needs `pip install
   websockets` on the host; the gateway's self-signed certificate is
   `infra/gateway/certs/server.crt`:
   ```
   python3 -c "
   import asyncio, ssl, sys, websockets
   ctx = ssl.create_default_context(cafile='infra/gateway/certs/server.crt')
   async def main():
       url = 'wss://localhost:8443/ws/analysis:' + sys.argv[1] + '?token=' + sys.argv[2]
       async with websockets.connect(url, ssl=ctx) as ws:
           print(await ws.recv())
   asyncio.run(main())
   " <ANALYSIS_ID> "$TOKEN"
   ```

What to say at the end: "JSON channels are one-way news feeds, authorized
per channel. Yjs rooms are shared documents: the server keeps the merged
copy in memory, saves a merged snapshot every 20 updates or 30 seconds,
and isolates the risky decoding in child processes."

## 8. Check your understanding

<details>
<summary>Questions and answers</summary>

1. **Why is the token in the URL instead of a header?**
   Browsers cannot set an `Authorization` header on the WebSocket
   handshake.

2. **Which four channel families exist and who may read `presence:<id>`?**
   `user`, `presence`, `analysis`, `document`. `presence:<id>` is allowed
   for the user themselves and for accepted friends.

3. **Why does an `analysis:` socket start with `analysis.status`?**
   The client usually subscribes after `analysis.started` was published,
   and pub/sub does not store events. The snapshot gives the current state;
   live events follow.

4. **Why does the server accept the handshake before refusing?**
   Closing before accept becomes a plain HTTP 403 and the client never
   sees the application close code.

5. **When is a Yjs room saved to PostgreSQL?**
   When dirty and either 20 updates or 30 seconds have passed (on the next
   update or by the 30 s heartbeat), and when the last socket disconnects.

6. **Why merge instead of overwrite when saving?**
   Two writers can race; merging CRDT states gives the union regardless of
   order, so an older state can never replace a newer one.

7. **Why are client updates decoded in a child process first?**
   Malformed bytes can abort or segfault the y_py process; a child crash
   only rejects that update.

8. **What happens to an open note when core-service is killed?**
   The in-memory room is lost; edits since the last snapshot exist only in
   the clients. After reconnect the room is reloaded from `yjs_state`; the
   active-room marker expires within 90 seconds. (A clean stop is
   different: the lifespan saves every dirty room with
   `persist_all_dirty` before the database engine closes.)

9. **Why does a REST body edit sometimes return 409?**
   A Yjs room for the note is open on some replica, or a room still holds
   edits that are not saved yet; the next snapshot would overwrite the
   REST change.

10. **An organization admin removes a member who has the shared note open in the editor. What happens?**
    auth-service publishes `organization.member.removed`. core-service's
    handler calls `recheck_user_sessions` for that member: the note is no
    longer readable, so the socket is closed with 4403 "access to this note
    was revoked" at once, not at token expiry. A member who was only
    demoted to VIEWER also gets 4403 ("write access to this note was
    revoked"); after a reload the editor opens read-only.

</details>

## 9. Code map

| File | What it holds |
|---|---|
| `services/core-service/api/websocket.py` | `/ws/{channel}`, channel allowlist, `authorize_channel`, `send_analysis_status` |
| `services/core-service/websocket_manager.py` | `WebSocketEnvelope`, `WebSocketManager`, per-user cap, presence loop, `ws:fanout` relay; close codes, `authenticate`, `resolve_live_identity`, `open_identity`, `acquire_or_close`, `accept_and_close`, `serve_until_expiry` |
| `services/core-service/event_listener.py` | event handlers that call `manager.broadcast` |
| `services/core-service/domains/notification/service.py` | `deliver_notification` (push to `user:<id>`) |
| `services/core-service/clients/auth_client.py` | `set_user_presence`, `are_users_connected` |
| `services/auth-service/api/internal.py` | `POST /internal/users/{id}/presence`, `GET /internal/connections/check` |
| `services/auth-service/domains/auth/service.py` | `set_presence`, `_mark_online`, `presence_event` |
| `services/core-service/api/websocket.py` (`yjs_router`) | `/ws/yjs/{note_id}`, read-only handling, `_YjsSession` registry, `recheck_user_sessions` |
| `services/core-service/yjs_relay.py` | `YjsRoom`, `YjsRelay` (`_in_use`, `flush_due_snapshots`, `persist_all_dirty`), snapshots, cross-replica listener, active-room markers; decoder worker pool (`UpdateValidator`, `validator`), `guarded`, `seed_state`, `replace_text` (REST body edits), `merge_states`, `extract_text` |
| `services/core-service/domains/research/model.py` | `ResearchNote` (`yjs_state`, `content`, `search_vector`) |
| `services/core-service/domains/research/service.py` | `note_access`, the 409 rule |
| `services/core-service/main.py` | starts the listeners and the room heartbeat; saves dirty rooms at shutdown (`persist_all_dirty`) and stops the decoder workers |
| `infra/gateway/nginx.conf` | `location /ws` with upgrade headers |

## 10. Related chapters

- [24-events.md](24-events.md): where the pushed events come from.
- [12-core-service.md](12-core-service.md): core-service as a whole.
- [10-gateway.md](10-gateway.md): the `/ws` route and TLS.
- [11-auth-service.md](11-auth-service.md): tokens and presence storage.
- [23-redis.md](23-redis.md): the Redis channels and keys used here.
- [29-security.md](29-security.md): token checks and live user state.
- [../API_SPEC.md](../API_SPEC.md) (WebSocket section), [../ERD.md](../ERD.md) (`research_notes`).
