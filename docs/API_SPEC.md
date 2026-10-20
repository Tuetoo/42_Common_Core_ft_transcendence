# AlphaMind — API Specification

This document lists the routes of all three services as they exist in
the code (the `@router.*` decorators of each service's `api.py`,
`internal.py` and `public.py` files). Endpoints that don't exist are not
listed here; `docs/handbook/00-index.md` section 8 lists what is not
built.

## 1. Base paths and gateway routing

All three services sit behind an nginx gateway (`infra/gateway/nginx.conf`),
exposed on **`https://localhost:8443`** (TLS 1.2/1.3, self-signed development
certificate from `infra/gateway/gen-certs.sh`, created by `make certs` /
`make up`; use `curl -k` or trust the certificate). Plain
`http://localhost:8000` only answers `301` to the same path on
`https://localhost:8443`. WebSocket URLs are `wss://localhost:8443/ws/...`.
The gateway forwards the raw
`Authorization` header as-is — it does no JWT validation itself; each service
independently decodes and verifies its own tokens (`shared/security.py`).
It sets `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Real-IP` / `Host` on
every proxied request (the services run uvicorn with `--proxy-headers`), adds
security headers (HSTS, `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`) to every response, and rate-limits `/api/v1/auth/*` to
10 requests/second per client address (burst 20, then `429`) in addition to
auth-service's own limits.

```
/api/v1/auth/*           -> auth-service
/api/v1/organizations/*  -> auth-service
/api/v1/connections/*    -> auth-service
/api/v1/admin/*          -> auth-service (user administration, site stats)
/api/v1/portfolios/*     -> core-service
/api/v1/watchlists/*     -> core-service
/api/v1/research/*       -> core-service
/api/v1/notifications/*  -> core-service
/api/v1/api-keys/*       -> core-service
/api/v1/moderation/*     -> core-service (hide/unhide/delete notes and comments)
/api/v1/public/*         -> core-service (API-key-authenticated, see §5)
/api/v1/analyses/*       -> ai-service
/api/v1/decisions/*      -> ai-service
/api/v1/backtests/*      -> ai-service
/api/v1/market/*         -> ai-service
/api/v1/analytics/*      -> ai-service
/api/v1/rag/*            -> ai-service (RAG assistant; no buffering, see below)
/ws/*                    -> core-service (WebSocket upgrade)
/health/{auth,core,ai}   -> each service's /health
/docs/{auth,core,ai}/    -> each service's Swagger UI
/grafana/*               -> Grafana (login required)
/status                  -> static status page (infra/gateway/status.html)
/static/avatars/*        -> auth-service (uploaded avatar images)
/*                       -> frontend (every other path is a page)
```

`/api/v1/rag` is proxied with `proxy_buffering off`,
`proxy_cache off` and a 180 s read timeout, because `POST
/api/v1/rag/ask/stream` is a Server-Sent Events stream whose tokens must
reach the client as they arrive (ai-service also sends
`X-Accel-Buffering: no`).

`/internal/*` exists on every service but is **not** proxied by the gateway —
reachable only on the Docker-internal network, for service-to-service calls
(`clients/*.py`) and the scheduler; the gateway answers `404` for it.
`/metrics` is likewise internal-only (Prometheus scrapes it directly), and
each service's `/health` is exposed only through the `/health/<service>`
aliases above. Any other path goes to the frontend, which shows its own
404 page for an unknown page.

Errors raised by the services use the body
`{"error": {"code", "message", "request_id"}}`; codes are snake_case
(a missing resource is `not_found`). Authentication and permission errors
(401/403 from `shared/security.py`) and request-validation errors (422)
keep FastAPI's `{"detail": ...}` body. Every HTTP
response (not WebSockets) carries an `X-Request-ID` header. A client may
send its own `X-Request-ID` (kept when it matches `[A-Za-z0-9._-]{1,64}`,
otherwise replaced by a new `uuid4` hex). The same id is the error body's
`request_id`, the `request_id` of every success body built as
`{"data": ..., "request_id": "<id>"}` (`shared/schemas.py::APIResponse`, all
three services), and is on every service log line of that request, so any
answer can be traced in the logs. Answers that are not wrapped in `data`
(204, paginated lists returned directly, `/internal/*` plain objects,
`/health`) carry the id only in the header.

## 2. auth-service

### System roles and permissions (all services)

Every user has one site-wide `system_role` (`shared/enums.py`). What a
role may do is defined once in `shared/permissions.py` and enforced by every
service with `shared.security.require_permission(...)` /
`ensure_permission(...)` (403 `"Insufficient permissions"`); route code
never compares role names (auth-service's admin rules about who may act on
whom are the one exception).

| Permission | What it covers | guest | user | moderator | admin |
|---|---|---|---|---|---|
| `portfolio:write` | portfolios, ledger, holdings, watchlists (JWT and public API) | - | yes | yes | yes |
| `analysis:create` | analyses, decisions, decision reviews, backtests | - | yes | yes | yes |
| `document:upload` | document upload, rename, delete | - | yes | yes | yes |
| `note:write` | notes, collaborative editing | - | yes | yes | yes |
| `comment:write` | comments | - | yes | yes | yes |
| `organization:create` | create, change and delete organizations, manage members, send and revoke invitations | - | yes | yes | yes |
| `connection:write` | send and accept friend requests | - | yes | yes | yes |
| `api_key:manage` | create, change, revoke API keys | - | yes | yes | yes |
| `rag:ask` | RAG assistant (`/api/v1/rag/*`) | - | yes | yes | yes |
| `content:moderate` | hide/unhide/delete anyone's notes and comments | - | - | yes | yes |
| `user:deactivate` | deactivate/reactivate users (moderators: only users and guests) | - | - | yes | yes |
| `user:manage` | list, view, edit, re-role, delete users | - | - | - | yes |
| `site:stats` | site statistics, analytics `scope=all` | - | - | - | yes |

A guest is read-only: they can still read everything shared with them,
manage their own account (profile, 2FA, and the password through the
emailed reset link), remove connections,
leave organizations and accept an organization invitation. An unknown role has no permissions (fail closed).
The first admin is created on the command line (see Administration below).

**Live role and active check** (`shared/user_state.py`): an access token
carries the role it was issued with, so every authenticated request in
every service (HTTP, both WebSocket endpoints, and API-key requests via the
key owner) re-reads the user's live state from Redis (`auth:user_state:{id}`,
JSON `{"role", "is_active"}`, written only by auth-service after every
role/active/delete change and for every user at startup). On a Redis miss
the reader asks auth-service `GET /internal/users/{id}/state` and caches the
answer for `USER_STATE_CACHE_TTL_SECONDS` (300). Effects are immediate: a
deactivated or deleted user gets **401 `"Account is deactivated"`** on the
next request (WebSockets close with 4401), and a demotion applies to tokens
already issued (the live role replaces the token's `role` claim). If Redis
and auth-service are both unreachable the check fails open: the token's own
claims are used and a warning is logged, so an auth outage does not lock
everyone out of core and ai. Setting: `USER_STATE_CHECK_ENABLED` (default
true; the test conftests turn it off).

### Authentication
```
POST   /api/v1/auth/register
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
POST   /api/v1/auth/refresh
POST   /api/v1/auth/verify-email
POST   /api/v1/auth/verify-email/resend
POST   /api/v1/auth/password-reset/request
POST   /api/v1/auth/password-reset/confirm
GET    /api/v1/auth/oauth/{provider}/authorize
GET    /api/v1/auth/oauth/{provider}/callback
POST   /api/v1/auth/oauth/2fa
POST   /api/v1/auth/2fa/setup
POST   /api/v1/auth/2fa/verify
POST   /api/v1/auth/2fa/disable
```

Rules: emails are case-insensitive (stored lowercase; usernames are
unique ignoring case). Login, 2FA codes, password-reset requests, register,
resend-verification and invitations are rate limited (429 with
`Retry-After`; limits in auth-service `config.py`); 5 wrong 2FA codes
within 5 minutes, with no correct code between them, lock 2FA for 15
minutes. `/2fa/setup` returns 409 while 2FA is enabled.
`/2fa/setup` and `/2fa/verify` (the step that enables 2FA) need a **recent
login**: the access token's `auth_time` claim (the Unix time of the
password or OAuth login that started the session; a refresh keeps it, it
does not renew it) must be at most `TWO_FACTOR_RECENT_LOGIN_MINUTES`
(default 10) minutes old, for password and OAuth users alike. Otherwise
they answer **403** `recent_login_required` ("This action needs a recent
login. Log in again (password or OAuth) and retry within the next few
minutes."); a token without an `auth_time` claim gets the same 403. The frontend should send the user through login again
and then retry. `/2fa/disable` is not affected (it needs a valid code). The OAuth
callback applies the same checks as password login: a deactivated account
is refused, and when 2FA is enabled it gives a `challenge_token` instead of
tokens (see OAuth callback result below); `POST /auth/oauth/2fa`
exchanges `{challenge_token, code}` for the token pair (single use, 5 min).
OAuth links to an existing account only when the provider verified the email
and the local account is verified (else 409 `oauth_link_refused`). Password
reset links are single-use and revoke every session. Refresh tokens are
stored hashed. A refresh token revoked by rotation (reason `rotated`) that
comes back within 30 seconds of the rotation (`REFRESH_REUSE_GRACE`) gets
**401** `refresh_token_superseded` and nothing else is revoked (two
requests raced with the same token; the client keeps its cookie and
tries later). After 30 seconds it is treated as reuse: 401
`invalid_credentials`, and the sessions descending from that token (its
rotation chain) are revoked with reason `reuse`; the user's other logins
stay valid. A token revoked by logout, revoke-all or for any other reason
only gets 401. `/verify-email/resend` always returns 200.
An account that never verifies its email (and never signs in, and has no
OAuth link) is deleted by a nightly job 7 days after registration
(`UNVERIFIED_ACCOUNT_RETENTION_DAYS`), which frees the email for a new
registration (see Internal below).

OAuth browser binding: `/auth/oauth/{provider}/authorize` must be opened as
a browser navigation. It takes two optional query parameters: `next`, a
same-site path to open after the sign-in (it must start with one `/`,
not `//`, contain no backslash or control characters, and be at most 512
characters), and `marker`, a random value of 16 to 128 characters from
`A-Za-z0-9_-`. An invalid value is dropped silently, not refused. It sets
the cookie `alphamind_oauth_nonce` (HttpOnly,
Secure, SameSite=Lax, Path `/api/v1/auth/oauth`, 5 minutes) and redirects to
the provider with a single-use `state` and a PKCE challenge (S256). The
callback requires that cookie: a missing or foreign nonce burns the state
and fails with `oauth_failed` without calling the provider.

OAuth callback result: `GET /auth/oauth/{provider}/callback` never answers
JSON. It always answers **303 See Other** with
`Location: ${FRONTEND_BASE_URL}/oauth/complete#<fragment>`, and deletes the
nonce cookie on every outcome. The fragment is URL-encoded
(`application/x-www-form-urlencoded`) and is one of:

| Outcome | Fragment |
|---|---|
| signed in | `access_token=...&refresh_token=...` |
| the account has 2FA | `challenge_token=...&expires_in=300`; then call `POST /auth/oauth/2fa` with `{challenge_token, code}` |
| any failure | `error=oauth_failed&message=<error message>` |

When the browser-binding check passed, the fragment also carries the
`next` and `marker` values given at the authorize step (for every
outcome above, including a failure after that check).

"Any failure" covers every error of the sign-in: the provider reported an
error or the user said no, a missing `code` or `state`, an invalid,
expired, replayed or other-provider state, a missing or foreign nonce
cookie, a deactivated account, a refused account link, provider token or
user-info errors, and an unknown provider. The `message` is the same text
the error would carry in a JSON error body (for example "Invalid or
expired OAuth state."). On failure the request's database work is rolled
back and auth-service logs `oauth_callback_failed` with the error code.
A fragment (the part after `#`) is never sent by the browser to any
server and never appears in the `Referer` header. The frontend page
`/oauth/complete` reads `window.location.hash`, removes it from the
address bar with `history.replaceState`, and then stores the tokens,
asks for the 2FA code, or shows the message. Before the authorize
navigation, the frontend stores a random marker for the tab in
`sessionStorage`; `/oauth/complete` accepts tokens or a 2FA challenge
only when the fragment's `marker` equals that stored value, otherwise it
shows an error and stores nothing (protection against login CSRF). After
the sign-in, including the 2FA step, the user lands on `next` (checked
again by the frontend) instead of `/portfolios`, so an invitation link
survives a Google or GitHub sign-in. `FRONTEND_BASE_URL` must be
the frontend origin (for example `https://localhost:8443` locally, the
cloud URL on the server).

A soft-deleted account (see Administration) cannot log in
(401 invalid credentials), refresh a token, or reset its password (the
reset request is silently ignored, a reset link already sent is refused
with 422).

2FA secrets are stored encrypted. If a stored secret cannot be
decrypted (the encryption keys changed, or the value is not encrypted),
every 2FA check (login,
`/auth/oauth/2fa`, `/2fa/verify`, `/2fa/disable`) answers **409** "Your 2FA
settings cannot be read. Ask an administrator to reset your 2FA, then set
it up again." The frontend should show that message; an admin fixes it
with `DELETE /api/v1/admin/users/{user_id}/2fa` (Administration).

### Current user
```
GET    /api/v1/auth/me
PATCH  /api/v1/auth/me
POST   /api/v1/auth/me/avatar
```

Avatar uploads use local disk storage (`AVATAR_STORAGE_DIR`), deliberately
*not* S3 object storage like research documents — avatars are small, low-volume, and
never consumed by another service the way research documents are read by
ai-service's RAG ingestion.

`GET /me` returns `system_role` (`admin`/`moderator`/`user`/
`guest`) and `permissions`, the sorted list of permission strings of that
role (`shared.permissions.sorted_permissions`, e.g. `["analysis:create",
"api_key:manage", ...]`; `[]` for a guest), so the frontend shows only the
actions the user may take. Its `settings` object holds only
`notification_preferences`. `PATCH /me` changes `username`, the profile
fields (`first_name`, `last_name`, `bio`, `timezone`, `country`) and
`notification_preferences`; other fields (for example the old `theme`,
`preferred_currency` or `default_model`) are ignored. `PATCH /me`, avatar upload, password reset and
2FA enable/disable publish `user.updated` (field names only) after the
commit, which gives the user an `account_updated` notification (§6).

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
POST   /api/v1/organizations/{org_id}/invitations
GET    /api/v1/organizations/{org_id}/invitations
DELETE /api/v1/organizations/{org_id}/invitations/{invitation_id}
POST   /api/v1/organizations/invitations/accept
POST   /api/v1/organizations/{org_id}/leave
```

Nobody can assign the OWNER role through a role update or an
invitation (422); only the owner can promote to, change or remove admins;
any non-owner member can leave (the owner gets 422).

Invitations: `POST .../invitations/accept` takes the token in the body
(`{"token"}`), never in the URL, so it does not reach access logs. The
invitation list (`GET`) shows the open invitations (not accepted, not
expired) to the owner and admins; `DELETE` revokes one (owner/admin;
an invitation with the admin role only by the owner; 404 for an unknown,
accepted or foreign invitation) and publishes
`organization.invitation.revoked`. A new invitation replaces the
organization's open invitations for the same email and removes its
expired open invitations; accepted rows are kept. Replacing an open admin
invitation is owner-only, like revoking it (an admin gets 403).

Creating, changing and deleting an organization, removing a member,
changing a member's role, and sending or revoking an invitation need
`organization:create` (a guest gets 403); a guest may still leave an
organization and accept an invitation. Organization names are stripped of
leading and trailing spaces and must not be blank (422). Create, update,
delete, member removed/left, role change, invitation and accept publish
`organization.*` events after the commit, which core-service turns into
notifications (§6).

### Social connections
```
GET    /api/v1/connections
POST   /api/v1/connections
POST   /api/v1/connections/{connection_id}/accept
POST   /api/v1/connections/{connection_id}/block
DELETE /api/v1/connections/{connection_id}
```

A block records who blocked; only the blocker can remove it, and
the blocked user can neither delete it nor send a new request. `GET` hides
a block from the user who was blocked: only the blocker sees the `blocked`
row. Online status
is shown only for accepted connections. Sending and accepting need
`connection:write` (a guest gets 403); blocking and `DELETE` (remove,
decline, cancel, unblock) stay open to guests.

Sending a request and accepting one publish `friend.requested` /
`friend.accepted`; the actor and the other side are notified ("You sent a
friend request to X.", "You are now friends with X."). Every `DELETE`
publishes `friend.removed` with an `action`: `removed` (an accepted friend),
`declined`, `cancelled` or `unblocked`. The actor is always notified ("You
removed X from your friends.", "You declined the friend request from X.",
"You cancelled your friend request to X.", "You unblocked X."); only
`removed` also notifies the other side. Blocking publishes
`connection.blocked` (only when the connection was not already blocked),
which notifies only the user who blocked. The blocked user is never told
about a block or an unblock.

### Administration
```
GET    /api/v1/admin/users                          user:manage
GET    /api/v1/admin/users/{user_id}                user:manage
PATCH  /api/v1/admin/users/{user_id}                user:manage
DELETE /api/v1/admin/users/{user_id}                user:manage       -> 204
DELETE /api/v1/admin/users/{user_id}/2fa            user:manage       -> 204
POST   /api/v1/admin/users/{user_id}/deactivate     user:deactivate
POST   /api/v1/admin/users/{user_id}/reactivate     user:deactivate
GET    /api/v1/admin/stats                          site:stats
```

`GET /users` is paginated (`{items, total, page, page_size}`) with query
parameters `page`, `page_size` (1-100, default 20), `q` (case-insensitive
match on email or username), `role`, `is_active`, `include_deleted`
(default false), `sort` (`created_at` (default) / `email` / `username` /
`last_seen_at`) and `order` (`asc` / `desc`, default `desc`). Items:
`id, email, username, system_role, is_active, is_verified, is_online,
avatar_url, first_name, last_name, created_at, last_seen_at, deleted_at`.
`GET /users/{id}` (also the body of PATCH, deactivate and reactivate) adds
`updated_at, permissions, profile, two_factor_enabled, oauth_providers,
organizations_count`; it also works for a deleted user (404 only for an
unknown id).

`PATCH /users/{id}` changes only the fields present: `username`, `email`,
`is_verified`, `first_name`, `last_name`, `bio`, `timezone`, `country`,
`role`, `is_active` (null is 422 for the account fields; null clears a
profile field). A new email without `is_verified` makes the account
unverified and sends a verification link to the new address. A taken
username or email is 409.

Rules (`domains/admin/service.py`):
- **Self-action**: an admin cannot change their own role, deactivate or
  delete themselves, and nobody can deactivate/reactivate themselves (403).
- **Last admin**: the last active admin cannot be demoted, deactivated or
  deleted (409, "promote another admin first").
- **Moderator limits**: a caller without `user:manage` (a moderator) may
  deactivate/reactivate only users whose role is `user` or `guest`; an
  admin or another moderator is 403. Moderators cannot edit, re-role or
  delete anyone.
- A deleted user cannot be changed, deactivated or reactivated (409).
  Deactivating an already inactive user (or reactivating an active one)
  returns 200 without changes.
- Deactivation revokes all of the user's sessions (refresh tokens).
- **2FA reset** (`DELETE /users/{id}/2fa`): removes the user's
  2FA (enabled or pending setup), so they log in with the password alone
  and can set 2FA up again; for a lost authenticator, or when the stored
  secret cannot be decrypted. 204 also when no 2FA was set up;
  403 on the admin's own account (use `POST /auth/2fa/disable` with a
  code); 404 for an unknown id, 409 for a deleted user. Sessions are kept;
  `user.updated` with `changed: ["two_factor_enabled"]` notifies the user
  and the admin.
- **Soft delete** (`DELETE`): the row stays so references from core and ai
  still resolve. `deleted_at` is set, `is_active` becomes false, the email
  becomes `deleted-<id>@deleted.alphamind.local`, the username
  `deleted_<id>`, the password is replaced by a random one, profile fields
  and the avatar file are removed, OAuth links, the 2FA secret, friend
  connections, organization memberships and every session row are
  deleted. Open invitations the user sent are deleted
  (`organization.invitation.revoked` for each unexpired one), and so are
  invitations to the old address of a verified account. An organization
  the user owned passes to its longest-standing active admin, else its
  longest-standing active member or viewer, else a deactivated member in
  the same order (`organization.role.changed` for the new owner); an organization with
  no other member is deleted (`organization.deleted`). core and ai then
  delete the user's private data on `user.deleted`, and their nightly
  sweeps catch what an event missed (handbook chapters 12 and 13).
- Every role/active/delete change is written to the live-state mirror in
  Redis before the commit (a mirror failure rolls the change back), so it
  applies to existing tokens at once. The route then publishes
  `user.role.changed`, `user.deactivated`, `user.reactivated`,
  `user.deleted` and/or `user.updated` (§6). The user and the admin or
  moderator who acted are both notified (for a delete, only the admin).

`GET /stats` returns `{"auth": {...}, "core": {...} | null, "ai": {...} |
null}`: auth's counts (`users_total`, `users_active`, `users_deleted`,
`users_by_role.{admin,moderator,user,guest}`; deleted users excluded from
the others) plus core's and ai's `GET /internal/stats` objects. A service
that cannot be reached gives `null` for its part, never an error.

The first admin is set on the command line (updates the database and the
Redis mirror; refuses an unknown email, a deleted account and demoting the
last active admin; `--dry-run` previews):
```
docker compose --env-file .env.docker exec auth-service \
    python -m scripts.set_system_role --email you@example.com --role admin
```

### Internal (service-to-service only)
```
GET    /internal/users/{user_id}
GET    /internal/users/{user_id}/state          {"role", "is_active"}
GET    /internal/users/{user_id}/organizations
GET    /internal/users/{user_id}/notification-settings
GET    /internal/connections/check
GET    /internal/users/{user_id}/friends        {"friend_ids": [uuid, ...]}, accepted connections only
POST   /internal/users/{user_id}/presence      body {"online": bool} (called by core-service WebSockets)
POST   /internal/organizations/missing   body {"organization_ids": [uuid, ...]} (at most 500) -> {"missing_ids": [...]} (core's nightly sweep)
POST   /internal/users/deleted               body {"user_ids": [uuid, ...]} (at most 500) -> {"deleted_ids": [...]}, the deleted accounts among them (the nightly deleted-user sweeps)
POST   /internal/scheduler/purge-unverified     (scheduler, daily 04:10 New York)
```

`POST /internal/scheduler/purge-unverified` deletes every account that
registered more than `UNVERIFIED_ACCOUNT_RETENTION_DAYS` (default 7) days
ago, never verified its email, never signed in (`last_seen_at` is null) and
has no OAuth link. Each account goes through the same path as
`DELETE /api/v1/admin/users/{id}` (soft delete: anonymized email and
username, sessions revoked, `user.deleted` published after the commit,
with the user as `actor_id`, so nobody is notified), one account per
transaction, so the email can register again. Then it cleans the auth
leftovers of accounts deleted before the full deletion existed: for each
deleted account that still has sessions, open sent invitations,
memberships or owned organizations, it removes them as a delete does
(owned organizations pass to a successor or are deleted), one account per
transaction. Answer `{"retention_days", "candidates", "deleted",
"skipped", "failed", "deleted_account_leftovers": {"candidates",
"cleaned", "failed", "lookup_failed"}}`; 500 with `"all_units_failed"`
naming the step (`accounts`, `deleted_account_leftovers`, or
`deleted_account_leftovers_lookup` when the leftover query failed) when
every unit of that step failed (`shared/scheduled_jobs.py`); the events
of the accounts purged in the same run are still published.
`skipped` counts an account that changed meanwhile (verified, signed in)
and the last active admin, which is never deleted.

Internal routes are excluded from the public OpenAPI document.

`/users/{id}/state` returns the live role and active flag from
the database (a deleted user is `is_active: false`; 404 only for an id that
never existed) -- the fallback `shared/user_state.py` uses on a Redis miss.
auth-service has no `/internal/stats`; `GET /api/v1/admin/stats` reads
auth's counts from its own database. Each item of `/users/{id}/organizations`
also carries the member's organization `role` (`owner`/`admin`/
`member`/`viewer`); core-service uses it to make VIEWER read-only.

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
GET    /api/v1/portfolios/{portfolio_id}/history.csv?from=...&to=...
GET    /api/v1/portfolios/{portfolio_id}/transactions.csv?from=...&to=...&type=...
```

**Note**: decision tracking is not a portfolio route; it is §4's
`/api/v1/analyses/{id}/decisions` in ai-service.

**Currency**: `POST /api/v1/portfolios`' `currency` field
(`Currency` enum: `USD`/`EUR`/`GBP`, defaults to `USD`) is validated at
creation. `GET .../snapshots` is the one place it has an effect: same real `{snapshot_date, total_value, cash_balance}` history as
`GET /internal/portfolios/{portfolio_id}/snapshots` (§4), ownership-checked
(`_require_readable` — same visibility rules as `GET /api/v1/portfolios/
{portfolio_id}`), plus for a non-USD portfolio, a best-effort, display-only
conversion: `total_value_converted`, `cash_balance_converted`, `fx_rate`, and
`display_currency` are populated using the portfolio's CURRENT real USD→
currency rate (see `GET /internal/fx-rate` under §4) applied uniformly to
every row in the response — not a historical per-date rate, a
deliberate simplification because the holdings are US-listed and this is
display-only, not multi-currency accounting. If the FX lookup
fails (rate provider unreachable/no data), the four conversion fields come
back `null` and the real USD `total_value`/`cash_balance` are still
returned — a currency-display hiccup never blocks real portfolio data. For a
USD portfolio, all four conversion fields are always `null` (no conversion
needed). No change to `transactions`, `holdings`, or any other real
portfolio accounting — this is a read-only overlay on top of unchanged real
USD bookkeeping.

Portfolio rules: `visibility` is `private` or `organization` (422
otherwise); creating a portfolio in an organization requires membership
(403); the list shows your own portfolios plus non-private portfolios of your
organizations. Deleting a portfolio deletes its holdings, transactions and
snapshots (cascade). Transactions are validated per type (BUY/SELL need
quantity and price, DEPOSIT/WITHDRAWAL need quantity, DIVIDEND needs a held
ticker and dividend_per_share, STOCK_SPLIT needs split_ratio); tickers must
match `^[A-Z0-9.-]{1,10}$` and `CASH` is reserved; dates more than one day in
the future are rejected; `page` is capped at 100000. Holdings show the
latest recommendation of an analysis on the same portfolio only.

**Ledger-only holdings.** Holdings are derived only from the
transaction ledger; the `holdings` table is a cache of the replayed result.
Every write route of this section needs `portfolio:write` (a guest gets
403; reads stay open to anyone who may read the portfolio). A
write by someone who is not the owner answers 404 when that user cannot
read the portfolio, and 403 only for an organization member who can read
it.

- Transaction type **`adjustment`**: shares moved in (positive
  `quantity`) or out (negative `quantity`) at a stated per-share cost basis
  (`price`, 0 or more, required for shares in), with **no cash movement**
  (a transfer from another broker, a correction, an opening position).
  Shares in update the average cost like a buy; shares out leave it
  unchanged and cannot exceed the held quantity (422). `dividend_per_share`
  and `split_ratio` are rejected for it. Every other type needs
  `quantity > 0` and `price > 0` when given. `POST .../transactions`
  accepts adjustments directly.
- The holding routes do not write the holding directly:
  `POST .../holdings` (201), `PATCH .../holdings/{id}` (200) and
  `DELETE .../holdings/{id}` (**200 with a body**) record the
  `adjustment` rows that bring the position to the requested state and
  return `HoldingAdjustmentRead` = the holding fields plus
  `transaction_id` (the last recorded row, `null` when nothing changed) and
  `transaction_ids` (all rows recorded by the request). Same average cost:
  one row for the quantity difference. A different average cost: two rows
  (all shares out, then the target quantity in at the new cost), the only
  way to land on the exact target average. After a delete `quantity` is 0
  and the holding does not exist any more. `POST` for a ticker already held is
  422; a concurrent first position for the same ticker is 409. The
  holding keeps its id across a PATCH.
- `DELETE /portfolios/{id}` writes the durable `portfolio.deleted`
  event to `core.event_outbox` in the delete's transaction and publishes
  it after the commit; the outbox dispatcher retries it until it reaches
  Redis (ai-service cancels the portfolio's unexecuted tiers, §6).
- Every create/update/delete here notifies the actor (and the portfolio
  owner when different): `portfolio_created`, `portfolio_updated`,
  `portfolio_deleted`, `transaction_created` (one per request, listing the
  recorded rows, e.g. `adjustment +5`).

**CSV exports.** Both need read access to the portfolio (same
rule as the JSON reads), take optional inclusive ISO dates `from`/`to`
(422 when `from` > `to`), and answer `text/csv; charset=utf-8` as an
attachment (`portfolio-<id>-history.csv` / `portfolio-<id>-transactions.csv`).
Text a spreadsheet would run as a formula is prefixed with `'`.
- `history.csv`: `date, total_value, cash_balance, holdings_value,
  daily_return` from the daily snapshots. `daily_return` is flow-adjusted:
  `(value_t - external flows since the previous snapshot) / value_{t-1} - 1`,
  where external flows are deposits, withdrawals and adjustments valued at
  their stated `price` (shares out without a price count as 0); empty when
  there is no previous positive value.
- `transactions.csv`: every ledger field (`id, transaction_date,
  transaction_type, ticker, quantity, price, dividend_per_share,
  split_ratio, idempotency_key`), oldest first; `from`/`to` are UTC days,
  optional `type` is one `TransactionType`.

### Watchlists
```
GET    /api/v1/watchlists
POST   /api/v1/watchlists
PATCH  /api/v1/watchlists/{watchlist_id}                  body {"name"}
DELETE /api/v1/watchlists/{watchlist_id}                  -> 204
POST   /api/v1/watchlists/{watchlist_id}/items
DELETE /api/v1/watchlists/{watchlist_id}/items/{ticker}
```

Every write needs `portfolio:write`. `PATCH` renames a
watchlist (owner only; `name` 1-128 characters); `DELETE` removes the
watchlist and its items (owner only). Create, rename, delete and item
add/remove notify the owner (`watchlist_created`, `watchlist_updated`,
`watchlist_deleted`). Watchlists are never shared, so another
user's watchlist id answers 404 (not 403) on every route.

### Research (notes, comments, documents)
```
GET    /api/v1/research/notes
POST   /api/v1/research/notes
GET    /api/v1/research/notes/{note_id}
PATCH  /api/v1/research/notes/{note_id}
DELETE /api/v1/research/notes/{note_id}
POST   /api/v1/research/notes/{note_id}/comments
GET    /api/v1/research/notes/{note_id}/comments
PATCH  /api/v1/research/notes/{note_id}/comments/{comment_id}
DELETE /api/v1/research/notes/{note_id}/comments/{comment_id}

GET    /api/v1/research/sentiment/summary?ticker=...&from=...&to=...

GET    /api/v1/research/documents
POST   /api/v1/research/documents
GET    /api/v1/research/documents/{document_id}
PATCH  /api/v1/research/documents/{document_id}                   body {"title"}
DELETE /api/v1/research/documents/{document_id}
GET    /api/v1/research/documents/{document_id}/content
GET    /api/v1/research/documents/{document_id}/preview
```

`POST /api/v1/research/documents` is multipart; `category` and optional
`ticker` are required form fields, not inferred from the file — see
`docs/ERD.md` §3 for why. `GET .../content` streams the raw bytes back out of
object storage (S3 API; SeaweedFS in docker compose); it's the
only way to retrieve a document's content —
`storage_url` in the JSON response is an internal object key, not a browsable
URL. `DELETE .../documents/{id}` cascades: deletes the
`core.documents` row, and publishes `document.deleted` (§6) so ai-service can
clean up the matching Qdrant/BM25 vectors and its own `ai.document_chunks`/
`ai.embedding_jobs` rows — a document delete is never partial across the two
services. The row delete and the outbox entry commit together; the
stored object is deleted only after the commit (background task), so a failed
file delete leaves at most an orphan object, never a row without its file.

Re-uploading a document with the same name (same ticker, category and
filename) returns 409 `conflict` "A document with this name already
exists. Delete it or rename the file before uploading again." Two cases
differ: a `failed` duplicate is re-ingested in place when the caller owns
it (another organization member gets 409), and a duplicate that looks
stuck in processing is published again and its existing row is returned
(201); download sends an RFC 5987 `filename*` so non-ASCII names work;
`sort` on the notes list is validated (422 on unknown values); another
user's document or notification returns 404. Comments can be listed by
anyone who can read the note and deleted by the comment or note author; a
comment hidden by a moderator can be deleted only by its own author (the
note author gets 404 for it).

**System documents.** Documents owned by the system (filing
summaries and news digests from ai-service's ingestion, `upload_seed_documents
--as-system`, `POST /internal/documents/system`) are readable by every
logged-in user: detail, `content` and `preview` work for them (they are
what RAG answers and analyses cite). They are read-only: rename and delete
return 403. `GET /api/v1/research/documents` lists them only with
`include_system=true` (default false, so a user's own uploads are not
buried under the shared corpus); `is_system` is true on those rows.

**Notes and comments.**
- Permissions: note create/update/delete need `note:write`, comment
  create/update/delete need `comment:write`, document upload/rename/delete
  need `document:upload` (a guest gets 403 for all of them).
- One read/write rule for REST and the Yjs socket
  (`ResearchService.note_access`). Read: the author, or any member (any
  organization role) of the note's organization. Write: a caller with
  `note:write` who is the author or, for an organization note, a MEMBER /
  ADMIN / OWNER of that organization. An organization **VIEWER** (and every
  guest) is read-only: cannot edit the note, comment on it, create a note
  in the organization, or upload a document into it (403).
- **404 instead of 403**: a note the caller cannot read answers 404 on
  every route (get, update, delete, comments), so its existence does not
  leak; a note the caller can read but not change gives 403. Only the
  author can delete a note (403 for other readers).
- Note fields beyond the basics: `can_edit` (whether the caller may edit it -- open the
  editor read-only when false), `is_hidden`, `hidden_at` (moderation) and
  `sentiment_label` (`positive`/`neutral`/`negative`), `sentiment_score`
  (-1..1), `sentiment_model`, `sentiment_analyzed_at` (null until
  analysed). Comments carry the same moderation and sentiment fields plus
  `updated_at` (null if never edited).
- **409 during a collaborative session**: `PATCH /notes/{id}` that changes
  `content` returns 409 while a Yjs room for the note is open on any
  replica (the body then belongs to the live editor); title and tag edits
  are always allowed. A REST body edit is applied as one Yjs edit to the
  stored Yjs state (common prefix and suffix kept, the middle replaced),
  so clients holding the old document converge without duplicated text.
- `PATCH .../comments/{comment_id}` (body `{"content"}`, same validation as
  create): the comment's author only (403 for others); sets `updated_at`.
- Hidden notes and comments are left out of every list, search, detail and
  summary for everyone except their author (who sees `is_hidden: true`)
  and moderators; a hidden comment is 404 for other readers.
- Sentiment (A3): after a note or comment is created or its text changes
  (REST or the Yjs write-back), a separate asyncio task asks ai-service
  `POST /internal/sentiment` and stores the result, only if the text is
  still the same; the response does not wait for it. At most one call
  per note or comment runs at a time; saves made meanwhile queue one
  follow-up that reads the latest text. A failed refresh keeps the
  previous label until the next successful refresh (a new row stays
  null); `scripts/backfill_sentiment.py` (preview, `--apply`) fills rows
  that have no label and also re-analyses rows whose label is older than
  the last edit.
- `GET /sentiment/summary`: the analysed notes and comments the caller may
  read, optionally about `ticker` (tagged with it or mentioning it, plus
  their comments) and created within `from`..`to` (UTC days, 422 when
  `from` > `to`). Response (not wrapped in `data`): `ticker, from, to,
  total, notes, comments, counts {positive, neutral, negative},
  average_score, series[{date, count, counts, average_score}]`.
- Notifications: the actor gets `note_created` / `note_updated` /
  `note_deleted` / `comment_created` / `comment_updated` /
  `comment_deleted`; the note's author is also notified when an
  organization member edits the note or someone comments on it, and the
  comment's author when the note's author deletes their comment.

**Documents.**
- `GET /documents` filters: `ticker`, `category`, `status`
  (`uploaded`/`parsing`/`chunking`/`embedding`/`ready`/`failed`),
  `created_from`/`created_to` (upload time) and `content_from`/`content_to`
  (inclusive UTC days, 422 when reversed); `sort`: `created_at`,
  `content_date`, `title` (sorts by title, else filename), `status`, each
  with a `-` prefix for descending (default `-created_at`).
- Upload accepts an optional `title` form field (max 256). Content checks
  on top of the extension (`.pdf`, `.txt`, `.md`) and size checks: an
  empty file, a PDF that does not start with `%PDF-`, or a text file that
  is not UTF-8 or contains NUL bytes is 422.
- `DocumentRead` adds `title`, `source_url` and `is_system` (true for a
  system document, owner `00000000-0000-0000-0000-0000000005ec`).
- `PATCH /documents/{id}` sets or clears the display title (owner only,
  403 for other readers); nothing else can change (file, category and
  ticker drive RAG indexing).
- `GET /documents/{id}/preview` serves the file with
  `Content-Disposition: inline` for an in-page viewer, same read rule as
  `/content`. PDF is `application/pdf`; `.txt`/`.md` are `text/plain` with
  `Content-Security-Policy: default-src 'none'; sandbox` (Markdown is never
  rendered as HTML); any other type is 415. `Cache-Control: private,
  no-store`.
- Notifications: `document_uploaded` (not for a duplicate),
  `document_updated` (rename, or re-upload of a failed document),
  `document_deleted`, and after ingestion `document_ready` /
  `document_failed` (from `document.status_changed`, not for system
  documents).

### Moderation
```
POST   /api/v1/moderation/notes/{note_id}/hide          -> 200 {"data": note}
POST   /api/v1/moderation/notes/{note_id}/unhide        -> 200 {"data": note}
DELETE /api/v1/moderation/notes/{note_id}               -> 204
POST   /api/v1/moderation/comments/{comment_id}/hide    -> 200 {"data": comment}
POST   /api/v1/moderation/comments/{comment_id}/unhide  -> 200 {"data": comment}
DELETE /api/v1/moderation/comments/{comment_id}         -> 204
```

All need `content:moderate` (moderator, admin; others 403) and act on any
note or comment by id, not only ones the moderator could read (404 for an
unknown id). Hide stores `is_hidden`, `hidden_by`, `hidden_at` (unhide
clears them) without touching the note's `updated_at`. The author is
notified (`note_hidden` / `comment_hidden`; `note_updated` /
`comment_updated` on unhide; `note_deleted` / `comment_deleted`) and the
moderator gets the same type for their own action. Mounted by core
`main.py` directly (not inside `api_router`). Hiding or deleting a note ends
its open `/ws/yjs/{note_id}` sessions (close 4403, see WebSocket below).

### Notifications, API keys
```
GET    /api/v1/notifications?unread_only=...       (read rows older than 90 days are deleted nightly)
POST   /api/v1/notifications/{notification_id}/read
POST   /api/v1/notifications/read            body {"notification_ids": [...]} -> {"marked_count"} (unread rows only)
GET    /api/v1/api-keys
POST   /api/v1/api-keys
PATCH  /api/v1/api-keys/{key_id}             body {"name"?, "rate_limit_per_minute"?}
DELETE /api/v1/api-keys/{key_id}
```

Creating, changing and revoking keys needs `api_key:manage` (a
guest gets 403); listing stays open. `PATCH` renames a key and/or changes
its per-key limit (1 to `PUBLIC_API_RATE_LIMIT_PER_MINUTE`; at least one
field, else 422; 404 for another user's key; 409 for a revoked key); the
new limit applies on the next request. Create, change and revoke notify
the owner (`api_key_created`, `api_key_updated`, `api_key_revoked`;
revoking an already revoked key sends nothing).

#### Notification types

Every notification is a row in `core.notifications` (`type` is a plain
string, `shared/enums.py::NotificationType`) and is pushed to
the user's `user:{id}` WebSocket channel. Rule: every create, update and
delete a user performs notifies that user, and an action that affects
someone else also notifies that person (never twice for one action).
core-owned actions notify in-process after the commit
(`domains/notification/service.py::notify_after_commit` from a route,
`notify_user(s)` from an event handler; commits, delivers and never
raises); auth and ai actions arrive as events (§6).

Channels follow the user's `notification_preferences` (user settings,
`{"in_app": bool, "websocket": bool, "email": bool}`):
- `in_app` (default on): off means no notification row is
  written at all, so nothing is pushed live either;
- `websocket` (default on): the live push of the row to `user:{id}`;
- `email` (default off): an email with the same title and text,
  independent of `in_app`.
If auth-service cannot be reached, the defaults apply (nothing is lost).
A deleted account gets no new notification (no row, push or email).

| Domain | Type | When |
|---|---|---|
| portfolio | `portfolio_created`, `portfolio_updated`, `portfolio_deleted` | portfolio create / PATCH / delete (actor and owner) |
| ledger | `transaction_created` | a transaction, or the adjustments recorded by a holding route (JWT or public API) |
| watchlist | `watchlist_created`, `watchlist_updated`, `watchlist_deleted` | create; rename or item added/removed (JWT or public API); delete |
| API keys | `api_key_created`, `api_key_updated`, `api_key_revoked` | create; PATCH; first revoke |
| research | `note_created`, `note_updated`, `note_deleted` | own actions; author when an org member edits; moderator unhide (`note_updated`) or delete |
| research | `note_hidden` | a moderator hid the note (author and moderator) |
| research | `comment_created`, `comment_updated`, `comment_deleted` | own actions; note author on a new comment; comment author when the note author or a moderator deletes it, or on unhide |
| research | `comment_hidden` | a moderator hid the comment |
| documents | `document_uploaded`, `document_updated`, `document_deleted` | upload (not a duplicate); rename or re-upload of a failed document; delete |
| documents | `document_ready`, `document_failed` | ingestion finished (`document.status_changed`; not for system documents) |
| organizations | `organization_created`, `organization_updated`, `organization_deleted` | the members (for delete: the members before the delete) |
| organizations | `organization_member_joined`, `organization_member_left`, `organization_member_removed`, `organization_role_changed` | the affected member (own wording) and the `member_ids` in the event |
| organizations | `organization_invite` | an invitation was sent: the inviter (also when the invitee has no account) and the invitee when the email already has an account |
| organizations | `organization_invitation_revoked` | an invitation was revoked: the user who revoked, and the invitee when the email has an account ("Your invitation to the organization X was withdrawn.") |
| friends | `friend_request`, `friend_accepted`, `friend_removed` | the actor, and the other side (not for decline, cancel or unblock) |
| friends | `connection_blocked` | the user who blocked (never the blocked user) |
| account | `account_updated` | own profile/settings/password/2FA/avatar change; admin edit, 2FA reset, deactivation, reactivation (the user and the admin or moderator who acted); admin delete (the admin) |
| account | `account_role_changed` | an admin changed the system role (the user and the admin) |
| analyses | `analysis_started`, `analysis_completed`, `analysis_failed`, `recommendation_ready` | the analysis owner (system analyses: nobody) |
| decisions | `decision_recorded` | a decision was posted or changed |
| decisions | `decision_reviewed` | a new or replacing review was generated (`POST /decisions/{id}/review`; a failed generation sends nothing) |
| decisions | `decision_evaluated` | the review scored the decision after 7, 30 or 90 days (outperformed the benchmark or not, by how much) |
| tiers | `tier_executed`, `tier_execution_failed`, `tier_cancelled` | the tier's user, or the portfolio owner for a system analysis |
| backtests | `backtest_completed`, `backtest_failed`, `backtest_deleted` | the backtest owner |

### Public API (§5 — API-key authenticated, not unauthenticated)
```
GET    /api/v1/public/portfolios/{portfolio_id}
GET    /api/v1/public/portfolios/{portfolio_id}/holdings
PUT    /api/v1/public/portfolios/{portfolio_id}/holdings/{holding_id}
POST   /api/v1/public/watchlists/{watchlist_id}/items
DELETE /api/v1/public/watchlists/{watchlist_id}/items/{ticker}
```

POST returns 201 and DELETE returns 204 (same as the JWT routes).
Swagger (`/docs/core/`) declares the `X-API-Key` header as a security
scheme, so its "Authorize" button sends the key with "Try it out"; the
middleware in `apikeys.py` still does the actual check.
At most 10 active keys per user; a key's per-minute limit defaults to and is
capped by `PUBLIC_API_RATE_LIMIT_PER_MINUTE`, and the limit also applies per
owner across all of their keys (fixed one-minute windows). Revoking a key
takes effect immediately. A Redis error while updating the key cache does
not fail the revoke or update (it commits, logged
`api_key_cache_write_failed`); only a cached "valid" entry left over from
before the revoke can then keep the key working, for at most 30 seconds.
Over the limit is **429**
`rate_limited` with `Retry-After` (seconds to the next window); if Redis
is unavailable the limiter lets the request through (logged), the same
shared implementation as auth-service's limits (`shared/redis.py`).
In front of that, the gateway limits `/api/v1/public` to 5 requests per
second per client address (burst 20); beyond that nginx answers 429 with
its own HTML body, not the JSON error envelope.

`PUT .../holdings/{holding_id}` records `adjustment`
transactions like the JWT route and returns `HoldingAdjustmentRead`
(holding fields plus `transaction_id`/`transaction_ids`). The key owner's
live state (`shared/user_state.py`) is checked on every request before the
rate-limit counters: a deactivated or deleted owner gives **401**
`{"error": {"code": "account_deactivated"}}`, and a key of a guest owner
is read-only (**403** `insufficient_permissions` for anything but
GET/HEAD). When the live state cannot be determined the request is
allowed (logged). Watchlist item add/remove and holding updates notify the
owner as in the JWT routes.

### WebSocket
```
WS     /ws/{channel}          generic pub/sub relay
WS     /ws/yjs/{note_id}      Yjs CRDT binary relay for collaborative note editing
```

`/ws/{channel}` is a generic relay — the channel string itself carries the
real meaning, not a fixed enum of endpoints. Real channel names currently in
use: `analysis:{analysis_id}` (completion / recommendation-ready events for
one analysis), `user:{user_id}` (a user's own stream: each new in-app
notification, and `user.presence.changed` when one of the user's accepted
friends comes online or goes offline), `presence:{user_id}` (online/offline
status changes for a user's connections), `document:{document_id}`
(ingestion status). The web app opens `user:` sockets (one per browser
tab, shared by the unread badge, the notifications feed, the analytics
page and the friends page) and `analysis:` sockets;
`presence:` and `document:` are used only by API clients (the web app
gets presence over `user:` and polls the documents table).

Only these four prefixes are accepted (others close with 4400).
`user:` must be the caller; `presence:` needs an accepted connection;
`analysis:` needs ownership of the analysis (or, for a system analysis, read
access to its portfolio); `document:` needs read access to the document.
The server accepts first and then closes with 4401 (bad/expired token --
sockets are also closed when the token expires), 4403 (forbidden), 4400
(bad channel) or 4429 (more than 20 sockets per user). Connecting marks the
user online in auth-service; closing their last socket marks them offline.

A deactivated or deleted account is refused
with 4401 (live account state, same rule as HTTP). Open sockets follow
access changes: on a role, deactivation, deletion, organization-membership,
`friend.removed` or `connection.blocked` event, a socket of a deactivated
or deleted account is closed with 4401 and a socket whose channel the user
can no longer read with 4403 (a failed check leaves it open; for a
`presence:` socket the connection check answers "not connected" when
auth-service cannot be reached, so the socket is closed). An
`analysis:` or `document:` socket is not closed when its resource stops
being shared (portfolio made private or deleted, document deleted): it
keeps receiving status payloads until the token expires (at most 15
minutes), while REST reads answer 404 at once. Every push reaches each
socket at most once; a user's notifications arrive on every one of their
`user:` sockets on any replica. Messages are `{"event": <event name>,
"payload": <event payload>}`; the `analysis:{analysis_id}` channel
carries `analysis.started`, `analysis.completed`, `analysis.failed`,
`recommendation.ready`, `decision.recorded`, `tier.executed`,
`tier.execution_failed` and `tier.cancelled` (payloads in §6).

The first message on an `analysis:{analysis_id}` socket is normally
`{"event": "analysis.status", "payload": {"analysis_id", "ticker", "status",
"portfolio_id"}}` -- the analysis state when the socket subscribed. A client
learns the id only from the POST response, and `analysis.started` is usually
published before it can subscribe, so the live event alone would nearly
always be missed. Live events follow the snapshot, except that one published
while the status is read can arrive just before it; a client may see a state twice and
should treat `analysis.completed` / `analysis.failed` as final.

`/ws/yjs/{note_id}` -- collaborative editing contract:
- Yjs is the source of truth for the note **body**: the body is the
  `Y.Text` named **`"content"`** (`ydoc.getText("content")`, markdown or
  plain text). Title and tags are not part of the Yjs document (REST only).
- Binary frames are raw Yjs updates in both directions; the first server
  frame is the room's full state. A note whose Yjs state is empty is seeded
  from its `content` when the first room opens.
- Every persist (debounced) writes the "content" text back to
  `research_notes.content` (and so to the full-text index) in the same
  transaction; `updated_at` moves and a sentiment refresh is scheduled only
  when the text changed.
- Access uses the REST rule (`can_edit` in the note response): a reader
  who may not edit (organization VIEWER, guest) joins and receives the
  sync and every live update, but their updates are dropped -- open the
  editor read-only. No read access: 4403. An open writable socket whose
  user loses write access (made organization VIEWER or guest) is closed
  with 4403 "write access to this note was revoked"; reconnecting opens
  it read-only. Bad token or note id, or a deactivated account: 4401.
  Text frames: 1003.
- An update larger than `YJS_MAX_UPDATE_BYTES` (1 MiB) or one that does not
  decode closes only that socket with **1008**; the room and every other
  connection continue. Updates are first decoded in separate worker
  processes (`yjs_relay.py`), because malformed bytes can
  abort the y_py process.
- While any replica has the room open, REST body edits of the note get 409
  (see Research above).
- After a moderator hides the note, or a moderator or the owner
  deletes it, every open socket of the note is closed with **4403** and the
  reason `note hidden by a moderator`, `note deleted by a moderator` or
  `note deleted`. Unsaved edits are kept after a hide and dropped after a
  delete. A client that may still read the note can reconnect.

### Internal (service-to-service / scheduler only)
```
GET    /internal/portfolios/{portfolio_id}/holdings
GET    /internal/portfolios/{portfolio_id}/cash-balance
GET    /internal/portfolios/{portfolio_id}/owner
GET    /internal/portfolios/{portfolio_id}/snapshots?start_date=...&end_date=...
GET    /internal/portfolios/{portfolio_id}/cash-flows?start_date=...&end_date=...
POST   /internal/portfolios/{portfolio_id}/transactions   (optional body idempotency_key)
POST   /internal/scheduler/market-open
POST   /internal/scheduler/market-close
POST   /internal/scheduler/heartbeat/{job}
POST   /internal/scheduler/notification-retention
POST   /internal/scheduler/deleted-user-sweep               (daily 04:30 New York)
GET    /internal/tracked-tickers
GET    /internal/documents/filenames
POST   /internal/documents
POST   /internal/documents/system                          (multipart)
GET    /internal/stats
```

The `portfolios/{id}/holdings`, `cash-balance`, and `transactions` endpoints
are ai-service's real callers (`clients/core_client.py`) into a portfolio's
real state — used by the Portfolio agent's own tool, by position-sizing
(needs real available cash to size a BUY against), and by the daily
tier-monitoring job (executes a real transaction once a tier's price
condition is met). No per-call ownership check on any of these — internal,
Docker-network-only, service-to-service, not user-facing.

`POST .../transactions` accepts an optional `idempotency_key`
(ai-service tier monitoring sends `tier:<tier id>`): a repeat with the same key
returns the original transaction (200) without applying it again; the same
key for another portfolio is 409. It also accepts `transaction_type: "stock_split"`
with a required `split_ratio` (e.g. `2.0` for a 2-for-1 forward split, `0.5`
for a 1-for-2 reverse split, `1.05` for a 5% stock dividend) — one mechanism
covers all three real corporate actions, since each is mechanically
identical (rescale the existing holding's quantity and average cost by
`split_ratio`, zero cash effect). Requires an existing holding for that
ticker — nothing to rescale otherwise. Two more real, core-service-hosted
internal endpoints this portfolio's data feeds — `.../snapshots` and
`.../cash-flows` — are listed above and described under §4
(ai-service) alongside the backtest workflows that are their only real
callers.

`tracked-tickers` returns the union of every portfolio's holdings and every
watchlist's items across all users — what ai-service's RAG auto-ingestion
job should keep fresh. `documents/filenames` / `POST documents` are the
automated-ingestion counterparts of the real, interactive
`GET/POST /api/v1/research/documents` — used by the daily filing/news
ingestion job (owned by a well-known system user, not a real logged-in one)
rather than a multipart upload, since the job already has the raw text in
memory.

Scheduler hooks: "today" is the US market date
(America/New_York, not the container's UTC date). On a weekend or NYSE
holiday (`shared/market_calendar.py`) nothing runs and both answer 200
with `{"skipped": true, "reason": "weekend" | "market holiday", "date":
"YYYY-MM-DD", "portfolios_processed": 0}` (market-close)
or `{..., "analyses_requested": 0}` (market-open). On a trading day the
bodies are `{"skipped": false, "date", "portfolios_processed",
"portfolios_skipped"}` (a portfolio whose holding has no close for the
session gets no snapshot and is counted as skipped) and
`{"skipped": false, "date", "analyses_requested", "analyses_failed"}`
(status 500 when requests were attempted and every one failed). Market
close runs at 17:30 ET, after tier execution at 17:00.

`POST /internal/scheduler/heartbeat/{job}`: a scheduler job
script reports that it succeeded (`infra/scheduler/heartbeat.sh`); `job` is
one of `market-open`, `market-close`, `tier-monitoring`, `review-run`,
`rag-ingest`, `logs-cleanup`, `backup`, `purge-unverified`,
`notification-retention`, `deleted-user-sweep-core`,
`deleted-user-sweep-ai` (404 otherwise). Answers `{"job",
"recorded_at"}`; the time is exported on core-service's `/metrics` as
`alphamind_job_last_success_timestamp_seconds{scheduled_job}` for the
`scheduled_jobs` alerts (the label is not `job`, which Prometheus sets to
the scrape job). A job with no success yet is exported with the time
core-service began tracking it, so its stale alert still fires.
Market-close also exports `alphamind_market_close_portfolios_skipped`,
stored in Redis
(`core:market_close_skipped`) so it survives a core-service restart.

`POST /internal/scheduler/notification-retention` (daily 04:20 New York)
deletes READ notifications created more than
`NOTIFICATION_READ_RETENTION_DAYS` (default 90) days ago; unread ones are
kept whatever their age. It deletes in batches of
`NOTIFICATION_PURGE_BATCH_SIZE` (default 1000) rows, each batch committed
on its own. Answer `{"retention_days", "deleted", "batches",
"failed_batches"}`; a failed batch stops the run, and the answer is 500
with `"all_units_failed": ["batches"]` only when every attempted batch
failed (in practice: the first one).

`POST /internal/scheduler/deleted-user-sweep` (core, daily 04:30 New
York) repeats the `user.deleted` cleanup for every deleted account that
still owns private data in core: it lists the owners of private
documents, notes and portfolios, watchlists, API keys and notifications,
plus the owners of files under `documents/<owner_id>/` that no document
row references, asks auth `POST /internal/users/deleted` which of them are
deleted, and for each one deletes that data and the unreferenced files.
Then it runs the organization sweep: it asks auth
`POST /internal/organizations/missing` about every organization that
still has documents, notes or portfolios in core and cleans the content
of the missing ones as `organization.deleted` does. As a safety brake it
cleans nothing and fails when more than `ORGANIZATION_SWEEP_MAX_MISSING`
(5) organizations, or every one of at least two, are missing
(`python -m scripts.clean_organizations --id <org id> [--confirm]` cleans
named ones by hand). Answer `{"candidates", "deleted", "cleaned",
"failed", "lookup_failed", "refused", "organizations": {same fields}}`;
500 with `"all_units_failed"` naming the step (`users`, `users_lookup`,
`organizations`, `organizations_lookup`, `organizations_safety_brake`)
when every unit of that step failed.

`POST /internal/portfolios/{id}/transactions` accepts
`transaction_type: "adjustment"` with the same rules as the public route
(§3 Portfolios).

`POST /internal/documents/system` (`api/internal.py`)
uploads a real file as a **system document** (no user owner, stored with
the system owner id, `is_system: true`), used by ai-service
`scripts/upload_seed_documents.py --as-system`. Multipart form fields:
`file` (required; same type, size and content validation as the user
upload, PDF included), `category` (required, a `DocumentCategory` value),
`ticker`, `title`, `content_date` (ISO date), `source_url` (all optional).
Answer 201 `{"id": str, "status": str, "duplicate": bool}`. It publishes
`document.uploaded` after the commit exactly like a user upload, so
ai-service indexes it as a system document (visible to every user's RAG
search). A duplicate (same ticker, category and filename among system
documents, not failed) returns the existing id with `duplicate: true` and
publishes nothing; a failed one is re-uploaded in place.

`GET /internal/stats` returns core's part of the admin site
statistics, a flat object of integer row counts: `portfolios`,
`transactions`, `notes`, `comments`, `documents`, `watchlists`.

## 4. ai-service

### Analyses
```
POST   /api/v1/analyses
GET    /api/v1/analyses
GET    /api/v1/analyses/{analysis_id}
GET    /api/v1/analyses/{analysis_id}/recommendation
GET    /api/v1/analyses/{analysis_id}/tiers
GET    /api/v1/analyses/{analysis_id}/debate
GET    /api/v1/analyses/{analysis_id}/reflection
POST   /api/v1/analyses/{analysis_id}/decisions
```

`POST /api/v1/analyses` returns immediately with `status: "pending"` — the
actual agent graph run happens in the single serial, in-process analysis
queue (`domains/analysis/queue.py`, one run at a time in arrival order,
running `ai/workflows/analysis_workflow.py`). There is no synchronous "wait for the
result" mode. It requires a logged-in user; the owner comes from
the JWT only, and a `portfolio_id` must be owned by the caller (checked
through core-service `GET /internal/portfolios/{id}/owner`): 404 when the
caller cannot see the portfolio, 403 when the caller can see it (a member
of the organization it is shared with) but is not the owner. System
callers (core-service's market-open job, the owner's run scripts)
use `POST /internal/analyses` on the docker network instead. Optional `portfolio_id` in
the request body: when set and the Judge's verdict is BUY/SELL (not HOLD), a
real position-sizing step runs and writes real, pending recommendation tiers
sized against that portfolio's actual cash/holdings; omitted entirely, the
analysis is review-only and produces no tiers regardless of the verdict.
Optional `requested_agents`: when given, it must name at least one
research agent (any role except portfolio, bull and bear), else 422.

`GET /api/v1/analyses` lists only the caller's own analyses (paginated,
newest first); system analyses on the caller's portfolios appear in
analytics `scope=mine` (CSV `analysis_id`) and can be opened by id, but
are not listed here.
`GET /api/v1/analyses/{id}` returns full detail: every agent run, the debate
transcript, reflection history, and the final recommendation, in one
response. In the debate transcript a side that failed for provider
capacity and was regenerated appears once, as the regenerated argument; a
side that stayed unavailable keeps its placeholder text
`[<side> argument unavailable (<kind>): ...]`. `POST .../decisions` confirms followed/ignored/partial/declined
against that analysis's recommendation. Every route in this
section requires login; an analysis is visible to its user, or (for a
system analysis with no user) to whoever may access its portfolio; anything
else returns 404. The same ownership rule applies to decisions (outcome and
review routes) and backtests (`GET /backtests/{id}` and `/metrics` match the
backtest's user; creates require login and portfolio access).

The recommendation read (and `/tiers`) includes the entry/exit
tiers the user is asked to approve (side, target price, quantity, status,
window). Organization access is checked against the user's live
organization memberships (auth-service), not a JWT claim. A user may have at
most `ANALYSIS_MAX_ACTIVE_PER_USER` (3) pending or running analyses (429);
the whole queue accepts at most `ANALYSIS_QUEUE_MAX_PENDING` (50) pending
analyses (503 for everyone). Posting a decision again for the same analysis
updates it (latest wins), resets its outcome scoring and
deletes its earlier reviews (`GET /decisions/{id}/review` is then 404
until a new review is generated); changing it to
declined cancels the recommendation's tiers that have not triggered.

`POST /api/v1/analyses` and `POST .../decisions` need
`analysis:create` (a guest gets 403; every read stays open to whoever may
see the analysis). Recording or changing a decision publishes
`decision.recorded`, and tiers it cancels publish `tier.cancelled` (reason
`declined` when declining or changing to ignored, `superseded` when
following closes other recommendations' open tiers), after the commit (§6). A new recommendation for the same portfolio and ticker
also cancels the older one's open tiers with reason `superseded`.

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
generates a new one on every call rather than returning a cached result;
a new review replaces the decision's earlier ones. `GET .../review`
retrieves the stored one. When the LLM call fails or its answer is not
usable, `POST .../review` answers **503** with `Retry-After: 60` and stores
nothing (an earlier review stays). A stored review publishes
`decision.reviewed` (notification `decision_reviewed`, §6).

`POST .../review` needs `analysis:create` (an LLM call and a
write; a guest gets 403). It is limited per user: at most
`DECISION_REVIEW_RATE_LIMIT_PER_MINUTE` (3) per clock minute and
`DECISION_REVIEW_RATE_LIMIT_PER_DAY` (20) per UTC day; over the limit the
answer is **429** with `Retry-After` and no LLM call is made (a Redis
outage lets requests through).

### Backtests
```
POST   /api/v1/backtests
POST   /api/v1/backtests/recommendation-replay
POST   /api/v1/backtests/portfolio
GET    /api/v1/backtests?limit=20&offset=0
GET    /api/v1/backtests/{backtest_id}
GET    /api/v1/backtests/{backtest_id}/metrics
DELETE /api/v1/backtests/{backtest_id}
```

`GET /api/v1/backtests` lists the caller's own backtests, newest first
(`limit` 1-100, default 20; `offset` 0-10000), as `data: [BacktestRead]`.
System backtests are never listed. The web app shows it at `/backtests`.

`DELETE /api/v1/backtests/{backtest_id}` (needs `analysis:create`)
removes the caller's own finished backtest (`completed` or `failed`) with all
its results -> 204 and publishes `backtest.deleted` (notification to the
owner); another user's backtest is 404, one still `pending` or
`running` is 409. `POST /api/v1/backtests/portfolio` needs at least two
daily snapshots in the requested window, counted from the first snapshot
with a positive total value (422 with the count otherwise; a new
portfolio can be backtested after its second market close; leading
unfunded days are skipped, and without `start_date` the backtest starts on
the first funded day), and a run that
fails alignment names the days without a benchmark close (the newest daily
bar can stay incomplete for hours after the close).

Same async-return pattern as analyses — `"pending"` immediately, real work in
a background task. Three distinct strategy types, one shared schema/polling
contract (`GET .../{backtest_id}` + `.../metrics`):

- `POST /api/v1/backtests` (`ai/workflows/backtest_workflow.py`) —
  not a hypothetical day-1-close buy-and-hold: it replays every real
  `RecommendationTier` that actually reached `TRIGGERED` for `ticker` within
  `[start_date, end_date]` (a ticker can have more than one real triggered
  tier — e.g. "buy 10 at $150, buy 15 more at $145" as two tiers of one
  recommendation). Only tiers triggered WITHIN the window count — a tier's
  full real history clipped to a display window is deliberately NOT the
  same thing; once triggered, a tier is considered fully settled right
  there. Optional `portfolio_id` field — `None` (default) means every
  real triggered tier for this ticker from the caller's own analyses; a real
  `portfolio_id` narrows to just that one portfolio's own triggers. If
  nothing triggered for this ticker (optionally, in this portfolio) in the
  window, the request fails fast with a real `422 ValidationAppError` —
  never a silent fall-back to a hypothetical trade.
- `POST /api/v1/backtests/recommendation-replay`
  (`ai/workflows/backtest_workflow.py`) — replays every
  `RecommendationTier` of the caller's own analyses that actually reached
  `TRIGGERED` (a backtest without an owner replays every one), as a real chronological multi-ticker trade sequence.
  No request body fields beyond `benchmark_symbol`/`initial_capital` — the
  trade universe and date range are discovered from real trigger history,
  not chosen by the caller (first trigger to the creation day). The
  workflow counts only fills whose `triggered_at` falls inside
  that stored window.
- `POST /api/v1/backtests/portfolio`
  (`ai/workflows/backtest_workflow.py`) — a real backtest of one
  real portfolio's actual composition over time, sourced from
  `core.portfolio_snapshots` (a real daily NAV time series already written
  by the `market-close` scheduled job). It computes a time-weighted return
  (TWR), excluding
  every real DEPOSIT/WITHDRAWAL cash flow in the window from the return
  calculation (a real dividend and every real buy/sell are still correctly
  included — they're genuine investment activity, already reflected in
  `total_value`). `ending_capital`/`strategy_return`/every other metric on
  the response therefore answer "how did the investing itself perform,"
  NOT "how much money is literally in the account," whenever a real
  deposit or withdrawal happened inside the window (the formula is in
  `handbook/19-ai-workflows-backtest.md` section 3.8).
  A ledger `adjustment` in the window (from
  `GET /internal/portfolios/{id}/cash-flows`, below) is an external flow
  too -- shares moved in count like a deposit, shares moved out like a
  withdrawal -- valued at that day's market close of the ticker (the cost
  basis only when no close is available, logged), never as return.

The three create routes need `analysis:create` (a guest gets
403). Every finished backtest publishes `backtest.completed` or
`backtest.failed` for its user (§6).

### Market data
```
GET    /api/v1/market/instruments/{ticker}/prices?interval=1d
```

The prices route requires login; core-service uses the internal
copy `GET /internal/market/instruments/{ticker}/prices`. A ticker the
provider has no data for returns 404 and creates no instrument row. When
the provider fails and nothing is cached, the route returns 502
(`upstream_service_error`). The prices route is the only public market
route.

### RAG assistant
```
POST   /api/v1/rag/ask
POST   /api/v1/rag/ask/stream        text/event-stream
```

Question answering over the research document corpus with numbered
citations (`domains/rag/`). Both need `rag:ask` (a guest gets 403).

Request body (both routes; unknown fields are 422):
```
{"question": str (1-2000 chars, not blank),
 "ticker": str | null (normalized to upper case, ^[A-Z0-9.-]{1,10}$),
 "categories": [DocumentCategory] | null,
 "mode": "ask" | "draft" (default "ask"),
 "top_k": int 1-10 | null (default RAG_TOP_K_RESULTS)}
```
`mode: "draft"` is writing help for a research note: a draft paragraph
grounded in the same sources, with the same citations.

Flow: per-user rate limit, then hybrid retrieval restricted to what the
caller may see (system documents, their own, and their organizations'
-- the same scope an analysis of theirs gets), optionally narrowed by
ticker and categories. When a ticker is given and filings are in scope,
up to two chunks of that ticker's newest filing the caller may see are
always among the sources (the same reservation the analysis agents use,
so "latest 10-Q" is not answered from the previous quarter), and the model is told to prefer the most recent source
for "latest/current" questions. No sources means an honest "no relevant documents"
answer without any LLM call (`grounded: false`). Otherwise one LLM call on
the cheap (Lite) tier (`RAG_ASK_LLM_TIER`), grounded only in the numbered
sources, which it cites as `[1]`, `[2]`. Non-English output is never
returned. The question text is never logged, only its length.

`POST /ask` -> 200 `{"data": {"answer", "citations": [{index,
document_id, title, category, ticker, content_date, snippet}], "model",
"latency_ms", "mode", "grounded"}}`.

`POST /ask/stream` -> 200 `text/event-stream`; each event is
`event: <name>` plus one `data:` line with a JSON object, in this order:
1. `sources` (always first): `{"citations": [...]}`.
2. `token` (zero or more): `{"text": "..."}`.
3. exactly one of `done` -- `{"answer", "citations", "model",
   "latency_ms", "mode", "grounded"}` -- or `error` -- `{"code",
   "message"}` with `code` one of `non_english_output`, `timeout`,
   `llm_unavailable`, `internal_error`, `empty_answer`.

If the LLM stream fails before its first token, the answer is produced once
with the non-streaming path (router retries and fallback model) and sent as
a single `token`. One deadline, `RAG_ASK_STREAM_TIMEOUT_SECONDS` (120 s
from the start of the stream), covers the whole answer after `sources`,
including that fallback; when it passes, the stream ends with an `error`
event, code `timeout`. Every stream ends with exactly one `done` or
`error`, except when the client disconnects: then it stops without a
final event.

Errors before any output: 422 (body), 403 (permission), **429** with
`Retry-After` (at most `RAG_ASK_RATE_LIMIT_PER_MINUTE` = 10 questions per
clock minute and `RAG_ASK_RATE_LIMIT_PER_DAY` = 200 per UTC day per user,
shared by both routes; a Redis outage lets requests through), **503** with
`Retry-After: 30` when retrieval fails (both routes) or, for `/ask`, when
the LLM fails or answers twice in another language. Other settings:
`RAG_ASK_MAX_OUTPUT_TOKENS` (1024), `RAG_ASK_SNIPPET_CHARS` (300).

### Analytics
```
GET    /api/v1/analytics/agents      (login required)
GET    /api/v1/analytics/overview
GET    /api/v1/analytics/timeseries
GET    /api/v1/analytics/export.csv
```

Dashboard (`domains/analytics/dashboard.py`). Shared query
parameters: `from`/`to` (UTC days, `to` inclusive; defaults: `to` = today,
`from` = `to` - 90 days; 422 when `from` > `to` or the range is longer than
2 years), `ticker`, `action` (`RecommendationAction`), `status`
(`WorkflowStatus`), `scope` = `mine` (default: the caller's analyses plus
system analyses on portfolios the caller may use, resolved once per
request; 503 when core or auth cannot answer the access check) or `all`
(every analysis; needs `site:stats`, else 403). `mine` is broader than
`GET /api/v1/analyses`, which lists only the caller's own analyses
(deliberate), so the analytics total can be larger than the list total.
- `overview` -> `range {from, to}, scope, total_analyses, status_counts,
  recommendations, action_distribution, average_confidence, accuracy
  {evaluated, rate}, by_agent[{agent, runs, completed, failed,
  success_rate, avg_score, avg_duration_ms}], by_ticker[{ticker, analyses,
  completed, failed, actions, average_confidence, accuracy}]` (accuracy =
  recommendations scored by the daily review).
- `timeseries` adds `metric` = `analyses` (default) / `accuracy` /
  `confidence` and `interval` = `day` (default) / `week` (weeks start
  Monday) -> `metric, interval, range, scope, points[{period_start, value,
  count}]`, gaps filled (analyses: value 0; means: value null, count 0).
- `export.csv` adds `dataset` = `analyses` (default) / `recommendations` /
  `decisions` -> `text/csv` attachment
  `alphamind_<dataset>_<from>_<to>.csv`, newest first, at most 50000 rows
  (`X-Export-Truncated: true` when cut).

There is no portfolio analytics route: portfolio-level performance
metrics come from `POST /api/v1/backtests/portfolio` +
`GET /api/v1/backtests/{id}` above.

### Internal (service-to-service / scheduler only)
```
POST   /internal/analyses
GET    /internal/analyses/{analysis_id}
GET    /internal/analyses/{analysis_id}/detail
GET    /internal/recommendations/latest?ticker=...&portfolio_id=...   (portfolio_id required)
GET    /internal/market/instruments/{ticker}/prices
POST   /internal/review/run
POST   /internal/scheduler/tier-monitoring
POST   /internal/scheduler/rag-ingest
POST   /internal/scheduler/deleted-user-sweep               (daily 04:40 New York)
GET    /internal/fx-rate?from_currency=...&to_currency=...
GET    /internal/stats
POST   /internal/sentiment
```

`POST /internal/analyses`: when the body carries both
an owning `user_id` and a `portfolio_id`, that user must own the portfolio
-- the same check as the public create (`deps.ensure_portfolio_owner`
through core-service): 404 when the user cannot see it, 403
when the user can see it but is not the owner, 503 when core cannot
answer. Without `user_id` the analysis is a system
analysis. The owner's run scripts use it through
`scripts/analysis_cli.py` inside the ai-service container:
```
docker compose --env-file .env.docker exec -T ai-service \
    python -m scripts.analysis_cli create TICKER [--portfolio ID] [--user UUID]
... python -m scripts.analysis_cli status ANALYSIS_ID
... python -m scripts.analysis_cli get ANALYSIS_ID
```
`--user` makes the analysis owned by that user (it appears in their
history and notifications; must be a valid UUID; with `--portfolio` the
access check above applies, and on an error the body is printed and the
exit code is 1). `scripts/run_analysis.sh` does not pass `--user` and stays a
system analysis.

`POST /internal/sentiment` (module A3; caller: core-service after a
note or comment is saved, and `scripts/backfill_sentiment.py`): body
`{"text": str, "kind": "note" | "comment"}` -> 200 `{"label": "positive" |
"neutral" | "negative", "score": float in [-1, 1], "model": str}`; 422 for
empty text; 503 (`Retry-After: 60`) when the LLM is unavailable or gives no
usable answer, or after `SENTIMENT_TIMEOUT_SECONDS` (240) in total (the
caller keeps the stored sentiment, empty for a new row, and tries again on
the next save; the backfill fills rows that have no label or a label
older than the last edit). core-service
waits up to 270 s for the answer. One cheap (Lite) tier call with strict
JSON, asked again once when the answer is not the requested JSON; only
the score is taken from the model (clamped), and the label is derived from
it (>= 0.15 positive, <= -0.15 negative, else neutral). Texts are cut to
`SENTIMENT_MAX_INPUT_CHARS` (6000) at a word boundary; the text is treated
as data and never logged (`ai/user_sentiment.py`).

`POST /internal/scheduler/deleted-user-sweep` (ai, daily 04:40 New
York) lists the users that still own analyses, decisions, backtests or
memories in ai, asks auth `POST /internal/users/deleted` which of them
are deleted, and runs the `user.deleted` cleanup for each one (analyses
with their recommendations and tiers, decisions, backtests, agent
memories and archived reports). Answer `{"candidates", "deleted",
"cleaned", "failed", "lookup_failed", "refused"}` (`refused` is always
false here); 500 with `"all_units_failed"` (`users` or `users_lookup`)
when every unit of that step failed.

`GET /internal/stats` returns ai's part of the admin site
statistics: `analyses_total`, `analyses_completed`, `analyses_failed`,
`recommendations`, `backtests` (flat integer counts).

`POST /internal/review/run` runs `ai/workflows/review_workflow.py`'s two
evaluation passes (AI-accuracy scoring for all recommendations, decision-
outcome evaluation for confirmed user decisions) and the agent metrics
rollup — wired to `infra/scheduler/call-job.sh review-run`, daily at 2:00 AM ET.
It answers through the scheduled-job rule (`shared/scheduled_jobs.py`):
`{"recommendations_scored", "recommendations_attempted",
"recommendations_failed", "decisions_evaluated", "decisions_attempted",
"decisions_failed", "agent_metrics_updated"}`, with status 500 and
`"all_units_failed": [<steps>]` when every attempted item of the
`recommendations` or `decisions` step failed (an item without price data
counts as failed).

`POST /internal/scheduler/tier-monitoring` runs the daily check of every
accepted (or still-acceptable) recommendation tier for a real trigger
(today's actual high/low crossing the target price) or expiry — the hook
is wired to `infra/scheduler/call-job.sh tier-monitoring` (weekdays 17:00 ET). A
trade core-service refuses for a business reason (4xx) moves the
tier to the terminal status `execution_failed`; 5xx/timeouts are retried on
the next run with the same idempotency key.
"Today" is the America/New_York date; on a weekend or NYSE
holiday the job is skipped with 200 `{"skipped": true, "reason":
"non_trading_day", "date": "YYYY-MM-DD"}`. It is the only ai-service job
tied to the trading session (`/review/run` and `/scheduler/rag-ingest` run
every day). Filled tiers publish `tier.executed`, business refusals
`tier.execution_failed` (§6). The answer is the run's counts, with status
500 when every checked tier failed or had no fresh price.

`POST /internal/scheduler/rag-ingest` runs both the per-ticker (filing +
news) and the separate macro-research auto-ingestion passes in one call —
wired to `infra/scheduler/call-job.sh rag-ingest`, daily. Steps `filings`,
`news` and `macro`; status 500 when every ticker a step attempted failed
or the macro step failed. A source without its key (SEC user agent,
NewsAPI, FRED) is disabled and attempts nothing; a NewsAPI quota refusal
defers that ticker's news; neither is a failure.

The price-bar cache (`domains/market`) is filled on demand: agents
refresh it on a cache miss or stale read (`ai/tools/market_data_cache.py`).
There is no warm-up or bulk price route.

`GET /internal/portfolios/{portfolio_id}/snapshots` returns one real
`{snapshot_date, total_value, cash_balance}` row per real trading day this
portfolio has a `core.portfolio_snapshots` row for (written daily by the
`market-close` scheduled job) — the raw data `POST /api/v1/backtests/
portfolio` above is built from. `start_date`/`end_date` query params are
optional; omitted, returns the portfolio's entire real snapshot history.

`GET /internal/portfolios/{portfolio_id}/cash-flows` (both
`start_date`/`end_date` required — unlike `snapshots` above, every real
caller always has a concrete backtest window in hand) returns every real
DEPOSIT/WITHDRAWAL transaction dated in the window (by snapshot
day -- dated after 17:30 New York on the day before `start_date` and at or
before 17:30 New York on `end_date`, matching each row's `snapshot_date`)
as `{transaction_date, amount}`, with a signed `amount` (positive for a DEPOSIT, negative for a
WITHDRAWAL) — feeds the TWR in `POST /api/v1/backtests/portfolio`
above; both endpoints are hosted by core-service but have no real callers
outside this workflow, which is why they're documented here rather than
under §3.
Ledger `adjustment` rows in the window are listed too,
and every row also carries `transaction_type`, `ticker`, `quantity`
(signed for an adjustment: positive = shares in) and `price` (per-share
cost basis of shares in, may be null for shares out). An adjustment's
`amount` is `0.0`, so a caller that only sums `amount` sees only the
cash flows; the portfolio backtest values the shares at that
day's market close itself (see Backtests above). Every
row also carries `snapshot_date`, the snapshot day the row belongs to (the
first 17:30 New York snapshot at or after it; a deposit entered in the New
York evening belongs to the next day).

`GET /internal/fx-rate` returns `{"rate": <float>}` — the
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

**Seed documents as system documents**:
`scripts/upload_seed_documents.py --as-system` uploads every seed file
through core-service `POST /internal/documents/system` (§3) instead of a
user account, so the documents are visible to every user's RAG searches.
Run inside the ai-service container (`CORE_SERVICE_URL` and the internal
token are configured there):
```
docker compose --env-file .env.docker exec ai-service \
    python -m scripts.upload_seed_documents --as-system [--dry-run] \
    [--categories filing,news] [--seed-root /path/to/rag_seed_documents]
```
Every category (earnings_call, analyst_report, macro_research, filing,
news) is read from one folder, `--seed-root` (default: the
repo-root `rag_seed_documents/` in a checkout, else
`/tmp/rag_seed_documents`). The image holds no seed files, so copy the
folder in first:
`docker compose cp rag_seed_documents ai-service:/tmp/rag_seed_documents`.
`content_date` comes from a trailing `_YYYY-MM-DD` in the file name, the
title from the file name without its extension; duplicates are reported
and counted, not re-added. Without `--as-system` the files are uploaded
through a user account.

## 5. The "public API" (developer API keys)

core-service's `/api/v1/public/*` routes are authenticated by an `X-API-Key`
header, not a JWT — keys are issued/managed via `/api/v1/api-keys`. "Public"
here means "the developer-facing programmatic API surface," not
"unauthenticated." `PublicAPIRateLimitMiddleware` validates the key and
resolves its owning user/organization before the request reaches the route
handler. It also applies the key owner's live account state
and role (deactivated owner 401, guest owner read-only 403; see §3 Public
API).

## 6. Cross-service events (Redis pub/sub, not HTTP)

Defined in `shared/events.py::EventName` (owners in
`EVENT_OWNERS`; payload shapes below and in
`handbook/24-events.md` section 3.1). Plain pub/sub events
are published via `shared/events.py::publish_cross_service` and consumed via
`CrossServiceSubscriber` (fire-and-forget: a subscriber that is down misses
the event). **Durable** events go through `publish_durable_event` (Redis
Streams, consumer groups, redelivered until acked) and are consumed via
`DurableEventSubscriber`. auth-service publishes `user.deleted` both
over pub/sub (core) and to a stream (ai). core-service's durable events
(`document.uploaded`, `document.deleted`, `portfolio.deleted`) are first
written to the outbox table `core.event_outbox` in the request's
transaction (`services/core-service/outbox.py`), published right after the
response, and republished by the outbox dispatcher (every 5 s, rows older
than 10 s) until Redis accepts them; delivery is at least once and
ai-service's handlers are idempotent. Not HTTP endpoints; listed here because they're as
much a part of the API surface between services as the internal REST calls.

Every publisher sends its events only **after** its own
transaction committed (routes via `BackgroundTasks`, workflows after
`session.commit()`), auth-service's `user.presence.changed` included; a
publish failure is logged and never undoes the work.
core-service creates each notification row once even with several replicas
(a Redis claim keyed by the publisher's `event_id`, falling back to a hash
of the payload, `core:event_claim:*`, 5 minutes; fails open), while WebSocket pushes run on every replica for its own sockets.

| Event | Published by | Transport | Consumed by | Effect |
|---|---|---|---|---|
| `document.uploaded` | core-service | durable | ai-service | drives RAG ingestion (user and system documents) |
| `document.deleted` | core-service | durable | ai-service | cleans up Qdrant/BM25 + `ai.document_chunks`/`ai.embedding_jobs` |
| `portfolio.deleted` | core-service | durable | ai-service | ai closes every unexecuted tier of the portfolio (status `expired`, note `portfolio_deleted`) and publishes `tier.cancelled` for each |
| `document.status_changed` | ai-service | durable + pub/sub | core-service | two independent subscribers: a real DB write (`Document.status`, via the durable subscriber; a transition to `ready`/`failed` notifies the owner, `document_ready`/`document_failed`, not for system documents) and a separate WebSocket broadcast (plain pub/sub, deliberately not durable: a missed live push is harmless, while a missed DB write would leave the status wrong) |
| `analysis.started` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `analysis_started` notification |
| `analysis.completed` | ai-service | pub/sub | core-service | WebSocket broadcast (`analysis:{id}` only) + `analysis_completed` notification (the row reaches `user:{id}`) |
| `analysis.failed` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `analysis_failed` notification (with the reason) |
| `recommendation.ready` | ai-service | pub/sub | core-service | WebSocket + `recommendation_ready` notification |
| `decision.recorded` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `decision_recorded` notification |
| `decision.reviewed` | ai-service | pub/sub | core-service | `decision_reviewed` notification to the user |
| `tier.executed` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `tier_executed` notification |
| `tier.execution_failed` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `tier_execution_failed` notification |
| `tier.cancelled` | ai-service | pub/sub | core-service | `analysis:{id}` broadcast + `tier_cancelled` notification |
| `backtest.completed` / `backtest.failed` | ai-service | pub/sub | core-service | `backtest_completed` / `backtest_failed` notification |
| `backtest.deleted` | ai-service | pub/sub | core-service | `backtest_deleted` notification to the owner |
| `decision.outcome.evaluated` | ai-service | pub/sub | core-service | `decision_evaluated` notification to the deciding user |
| `organization.created` / `.updated` / `.deleted` | auth-service | pub/sub | core-service | `organization_created` / `_updated` / `_deleted` for every id in `member_ids`; on `.deleted` core also deletes the organization's documents and notes and makes its shared portfolios private to their owners (deleted when the owner's account is deleted) |
| `organization.member.joined` | auth-service | pub/sub | core-service | welcome notification to the new member and `organization_member_joined` to the other members; published after the commit |
| `organization.member.left` / `.member.removed` / `.role.changed` | auth-service | pub/sub | core-service | the affected member (own wording) and the other members, `organization_member_left` / `_member_removed` / `organization_role_changed` |
| `organization.invitation.created` | auth-service | pub/sub | core-service | `organization_invite` to the actor, and to the invitee when the email already has an account |
| `organization.invitation.revoked` | auth-service | pub/sub | core-service | `organization_invitation_revoked` to the actor, and to the invitee when the email has an account |
| `friend.requested` / `.accepted` / `.removed` | auth-service | pub/sub | core-service | `friend_request` / `friend_accepted` / `friend_removed` to the actor, and to the other side for a request, an accept and `action` `removed`; `.removed` also re-checks both users' open sockets |
| `connection.blocked` | auth-service | pub/sub | core-service | `connection_blocked` to the actor only (never the blocked user); re-checks both users' open sockets |
| `user.updated` | auth-service | pub/sub | core-service | `account_updated` (lists the changed field names) to the user, and to the actor when another user acted |
| `user.role.changed` | auth-service | pub/sub | core-service | `account_role_changed` to the user, and to the actor |
| `user.deactivated` / `user.reactivated` | auth-service | pub/sub | core-service | `account_updated` ("deactivated" / "reactivated") to the user, and to the actor |
| `user.deleted` | auth-service | pub/sub + durable | core-service (pub/sub), ai-service (durable) | core closes the user's sockets, deletes their private documents, notes, portfolios, watchlists, API keys and notifications, and sends `account_updated` to the admin who deleted (the unverified-account purge, actor = user, notifies nobody); ai deletes the user's analyses, recommendations, decisions, backtests, memories and archived reports. The nightly deleted-user sweeps repeat both cleanups |
| `user.presence.changed` | auth-service | pub/sub | core-service | WebSocket relay to `presence:{user_id}` and to `user:{friend_id}` of each accepted friend |

Payloads (built by `services/auth-service/events_out.py`,
`services/ai-service/ai_events.py` and core's event code):
- `portfolio.deleted`: `{"portfolio_id", "user_id"}` (the owner; ai uses
  it as the recipient of the resulting `tier.cancelled` events, since core
  cannot resolve a deleted portfolio's owner).
- `analysis.started` / `.completed` / `.failed`: `{"analysis_id",
  "user_id" ("" for a system analysis), "ticker", "portfolio_id" | null}`;
  `.failed` adds `"failure_reason"`. (`recommendation.ready` keeps its own
  shape: `{"id", "analysis_id", "ticker", "action", "confidence",
  "user_id" | null}`.)
- `decision.recorded` and `decision.reviewed`: `{"decision_id",
  "analysis_id", "user_id", "ticker", "decision"}`.
- `decision.outcome.evaluated`: `{"decision_id", "analysis_id",
  "user_id", "ticker", "evaluation_days", "alpha" (the decision's return
  minus the benchmark's, a fraction) | null, "outperformed" | null}`
  (null when there was no price data).
- `tier.executed` / `.execution_failed` / `.cancelled`: `{"tier_id",
  "analysis_id", "user_id" ("" for a system analysis -- core then notifies
  the portfolio owner), "portfolio_id", "ticker", "action": "buy" | "sell",
  "quantity" (string), "price" (string), "reason" (failed/cancelled only;
  for cancelled: `declined`, `superseded` or `portfolio_deleted`)}`.
- `backtest.completed` / `.failed`: `{"backtest_id", "user_id", "kind",
  "status": "completed" | "failed", "error" | null}`.
- `backtest.deleted`: `{"backtest_id", "user_id", "kind"}`.
- every `organization.*` event (including `member.joined`):
  `{"organization_id", "organization_name", "actor_id", "user_id" (the
  affected member, or null), "role" | null, "member_ids": [...]}`. For
  `invitation.created` / `.revoked`, `user_id` is the invitee when the
  email has an account (else null) and `role` is the invitation's role.
- `friend.*`: `{"actor_id", "user_id" (the other side), "actor_username",
  "other_username"}`, plus `"action"` (`removed` | `declined` |
  `cancelled` | `unblocked`) on `friend.removed`.
- `connection.blocked`: `{"actor_id", "blocked_user_id",
  "blocked_username"}` (no `user_id`, on purpose).
- `user.*` (except presence): `{"user_id", "actor_id", "username" (for
  `user.deleted`: the username before the soft delete renamed it), "role",
  "is_active", "changed": [field names, never values]}`.

Every event has a consumer.
