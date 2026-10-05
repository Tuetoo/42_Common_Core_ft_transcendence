# 11. auth-service

> auth-service owns everything about *who* a user is: accounts, passwords,
> email verification, login, access and refresh tokens, sessions, two-factor
> authentication (TOTP), Google and GitHub sign-in, password reset, the
> site-wide roles (admin, moderator, user, guest), organizations and their
> invitations, friends ("connections"), avatars and online presence. It is
> the only writer of the PostgreSQL schema `auth`. The other two services
> never read that schema; they trust the access tokens auth-service signs,
> read a live copy of each user's role and active flag from Redis, and ask
> auth-service's `/internal/*` routes for anything else.

---

## 1. The big picture

```
                         gateway (https://localhost:8443)
                                     |
     /api/v1/auth  /api/v1/organizations  /api/v1/connections  /api/v1/admin
     /static/avatars
                                     v
  +------------------------------------------------------------------+
  | auth-service (FastAPI, uvicorn, port 8000)                       |
  |                                                                  |
  |  domains/auth          register, verify, login, tokens, 2FA,     |
  |                        OAuth, password reset, /me, avatar        |
  |  domains/organization  organizations, members, invitations       |
  |  domains/social        connections (friends), block              |
  |  domains/admin         user administration, site statistics      |
  |  api/internal.py       /internal/* for core-service, ai-service  |
  |  rate_limit.py         Redis fixed-window limits, 2FA lockout    |
  |  events_out.py         events published after the commit         |
  |  user_state_sync.py    live role/active mirror in Redis          |
  +------------------------------------------------------------------+
     |                |                     |                 |
     v                v                     v                 v
  PostgreSQL       Redis                 MailHog / SMTP     avatar_data
  schema "auth"    db 0 cache (OAuth     (verification,     volume
  (users,          state, 2FA challenge  reset, invitation  /app/data/avatars
  sessions, ...)   use), db 1 rate       emails)
                   limits, db 2 events
                   + user_state mirror
```

How the other services use it:

```
 browser --Bearer access token--> core-service / ai-service
                                     |  shared/security.py:
                                     |   1. verify the JWT signature (same JWT_SECRET)
                                     |   2. read auth:user_state:{id} from Redis (db 2)
                                     |      (miss -> GET auth-service /internal/users/{id}/state)
                                     |   3. deactivated -> 401; live role replaces token role
                                     v
                                  route code (permissions from shared/permissions.py)
```

auth-service never has to be called on the normal request path of another
service. Access tokens are self-contained, and the role check reads Redis.

---

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| **JWT (JSON Web Token)** | A short text in three parts (header, payload, signature) separated by dots. The payload holds "claims" such as `sub` (subject, the user id) and `exp` (expiry time). The signature proves the server made it. | All tokens here are JWTs, made with the `python-jose` library. |
| **HS256** | HMAC with SHA-256: the token is signed and checked with one shared secret (`JWT_SECRET`). | Every service holds the same secret, so every service can check a token without calling auth-service. `JWT_ALGORITHM` defaults to `HS256` (`shared/config.py`). |
| **Access token** | A short-lived JWT (15 minutes) sent as `Authorization: Bearer <token>` on every API call. Claim `type: "access"`. | Proves who you are. Not stored anywhere on the server. |
| **Refresh token** | A long-lived JWT (30 days), claim `type: "refresh"`, used only to get a new token pair. | Stored on the server only as a SHA-256 hash, one row per session. |
| **Session** | One row in `auth.user_sessions`: one refresh token's life (who, when, from which address and browser, when and why it was revoked). | Logout and reuse detection work on these rows. |
| **Rotation** | Every refresh revokes the used refresh token and issues a new one. A refresh token works exactly once. | If an old token comes back, someone may have stolen it. |
| **bcrypt** | A slow, salted password hashing function (via `passlib`). Only the first 72 bytes of a password count. | Passwords are stored only as bcrypt hashes. |
| **TOTP** | Time-based one-time password: a 6-digit code computed from a shared secret and the current 30-second time step (RFC 6238). Authenticator apps make these codes. | Two-factor authentication, with the `pyotp` library. |
| **Provisioning URI** | An `otpauth://...` link (usually shown as a QR code) that loads the TOTP secret into an authenticator app. | Returned by `/2fa/setup`. |
| **OAuth 2.0 authorization code flow** | The browser goes to Google or GitHub, the user agrees, the provider redirects back with a one-time `code`, and the server swaps the code for a provider access token. A random `state` value proves the callback belongs to a sign-in this server started; a nonce cookie proves the same browser started it; PKCE ties the code to this server's flow. | "Sign in with Google/GitHub". |
| **Fixed-window rate limit** | Count requests per key in a time window that starts at the first request; refuse when the count passes the limit. | Login, registration, reset, resend, invitations, 2FA codes. |
| **Fail open** | When the protection system itself (Redis) is down, let the request through and log it. | A Redis outage must not lock every user out of login. |
| **Soft delete** | Keep the row, mark it deleted, and remove personal data. | Other services keep foreign references to user ids. |
| **Pub/sub event** | A Redis `PUBLISH` on a channel; every subscriber that is connected right now gets it, nobody else. | auth-service tells core-service about organization, friend and account changes. |
| **Alembic revision** | One migration step (a Python file with `upgrade()` and `downgrade()`) that changes the database schema. Alembic records the current revision in a table `alembic_version`. | The `auth` schema is created and changed only by Alembic revisions. |

---

## 3. How it works in AlphaMind

### 3.1 Start-up and structure

`services/auth-service/main.py`, function `lifespan`, runs once at start:

1. `configure_logging(settings)` (structured JSON logs).
2. `ensure_schema(engine)` (`db_bootstrap.py`): brings the `auth` schema to
   the newest Alembic revision (section 3.16).
3. `mirror_all_users_safely(SessionLocal)` (`user_state_sync.py`): writes
   every user's live role and active flag to Redis (section 3.10). A
   failure is logged, not fatal.
4. Creates `AVATAR_STORAGE_DIR` if missing.

Then the app is built:

- FastAPI app with `docs_url="/api/docs"` and
  `openapi_url="/api/openapi-auth.json"` (the gateway exposes them at
  `/docs/auth/` and `/api/openapi-auth.json`).
- CORS from `BACKEND_CORS_ORIGINS`.
- `RequestIdMiddleware` (`shared/request_id.py`): one request id per HTTP
  request, on every log line, in the error body, in every
  `{"data": ..., "request_id"}` success body and in the `X-Request-ID`
  response header.
- The shared error handlers (`shared/exceptions.py`): every `AppError`
  becomes `{"error": {"code", "message", "request_id"}}` (built by
  `error_response`). Codes and statuses: `not_found` 404, `conflict` 409,
  `validation_error` 422, `permission_denied` 403, `invalid_credentials`
  401, `rate_limit_exceeded` 429, `upstream_service_error` 502. A
  subclass keeps its own code and gets its nearest registered parent's
  status (`_status_for` walks the class hierarchy): auth's
  `TwoFactorRequiredError` (`two_factor_required`), `EmailNotVerifiedError`
  (`email_not_verified`) and `OAuthFailedError` (`oauth_failed`) are 401
  because they subclass `InvalidCredentialsError`;
  `UnsupportedOAuthProviderError` (`unsupported_oauth_provider`) is 422;
  `OAuthProviderError` (`oauth_provider_error`) is 502;
  `OAuthAccountLinkError` (`oauth_link_refused`) is 409. An unregistered
  `AppError` is 400. Any other exception is logged as
  `unhandled_exception` and answered 500 `internal_server_error`.
  `RateLimitedError` (in `rate_limit.py`, code `rate_limited`) has its
  own handler that adds `Retry-After`; it is more specific than the
  `AppError` handler (FastAPI picks the handler of the nearest class), so
  it wins. Two answers do **not** use this envelope: FastAPI's
  `HTTPException` (the token checks in `shared/security.py` answer
  `{"detail": "Missing bearer token"}` and similar, missing permissions
  `{"detail": "Insufficient permissions"}`) and FastAPI's request-body
  validation (422 `{"detail": [ ... ]}`). A client must handle both
  shapes; the full contract is in [chapter 01](01-architecture.md),
  section 3.6b.
- Prometheus metrics at `/metrics` (latency buckets
  0.1/0.25/0.5/1/2/5/10/30 s).
- `api_router` under `/api/v1`: `/auth`, `/organizations`,
  `/connections`, `/admin` (built in `main.py`).
- `internal_router` mounted directly at `/internal` (not under `/api/v1`,
  never proxied by the gateway).
- `StaticFiles` at `/static/avatars` serving `AVATAR_STORAGE_DIR`, with
  `check_dir=False`: the mount is built when `main.py` is imported, before
  the lifespan creates the folder, and a plain import (every test does
  one) must not fail on a missing `/app/data/avatars`. An avatar request
  before the folder exists is simply a 404. For the same reason the
  folder is created in the lifespan, not at import time.
- `GET /health`: 200 `{"status": "ok", "service", "database", "redis"}`
  or 503 `"degraded"`.

Every route uses `Depends(get_db, scope="function")`. This makes FastAPI
run the session's `commit()` **before** the response is sent, and before
any `BackgroundTasks` run (`shared/database.py`, `make_get_db`). This
ordering is the base of two rules used everywhere below: "send the email
after the commit" and "publish the event after the commit".

### 3.2 The data model (schema `auth`)

From `domains/auth/model.py`, `domains/organization/model.py` and
`domains/social/model.py`:

| Table | Main columns | Notes |
|---|---|---|
| `users` | `id`, `email`, `username`, `password_hash`, `system_role` (default `user`), `is_active`, `is_verified`, `last_seen_at`, `deleted_at`, `created_at`, `updated_at` | unique indexes on `lower(email)` and `lower(username)` (`uq_users_email_lower`, `uq_users_username_lower`) |
| `user_profiles` | `user_id` (1:1), `first_name`, `last_name`, `avatar_url`, `bio`, `timezone`, `country` | |
| `user_settings` | `user_id` (1:1), `preferred_currency` (`USD`), `theme` (`system`), `default_model`, `notification_preferences` (JSONB) | core-service reads `notification_preferences` through `/internal/users/{id}/notification-settings`; `preferred_currency`, `theme` and `default_model` are display preferences stored for the frontend: no backend service reads them (ai-service does not pick a per-user model, core-service does not convert amounts) |
| `user_sessions` | `user_id`, `refresh_token_hash` (unique), `expires_at`, `ip_address`, `device_info`, `created_at`, `revoked_at`, `revoked_reason` | one row per refresh token |
| `oauth_accounts` | `user_id`, `provider`, `provider_user_id` | unique (`provider`, `provider_user_id`) |
| `two_factor_secrets` | `user_id` (1:1), `secret`, `enabled`, `last_used_step` | `secret` is stored encrypted (`fernet:<token>`, `security.py`) |
| `organizations` | `name`, `description`, `owner_id` | |
| `organization_members` | `organization_id` (cascade), `user_id`, `role` (owner/admin/member/viewer), `joined_at` | unique (`organization_id`, `user_id`) |
| `organization_invitations` | `organization_id`, `email`, `role`, `invited_by`, `expires_at`, `accepted_at` | an open row for the same email and expired open rows are deleted when the organization sends a new invitation (section 3.13); accepted rows stay as history |
| `connections` | `requester_id`, `addressee_id`, `pair_key` (generated), `status` (pending/accepted/blocked), `created_at`, `responded_at`, `blocked_by` | `pair_key` = `LEAST(a,b) || ':' || GREATEST(a,b)`, unique, so A->B and B->A cannot both exist |

The models define no ORM relationships. With an async engine an implicit
lazy load fails anyway ("MissingGreenlet"), so the code reads profile,
settings and 2FA rows explicitly through `UserRepository`; and because
users are only soft-deleted, delete cascades in Python are never needed.
All foreign keys in this schema point inside it (auth-service is the
only writer), so ordinary foreign keys are safe here, unlike across
schemas ([chapter 01](01-architecture.md), section 3.3).

### 3.3 Tokens (`security.py`)

All tokens are made by one function, `_create_token(subject,
expires_delta, token_type, extra_claims)`. Every token has:

```
{"sub": <user id or invitation id>, "type": <token type>,
 "iat": <issued at>, "exp": <expiry>, "jti": <random uuid4>, ...extra}
```

signed with `settings.JWT_SECRET` and `settings.JWT_ALGORITHM` (HS256).

| Token | Function | `type` claim | Lifetime | Extra claims |
|---|---|---|---|---|
| access | `create_access_token` | `access` | `ACCESS_TOKEN_EXPIRE_MINUTES` = 15 min | `role` (the system role at issue time), `auth_time` (Unix time of the login that started the session) |
| refresh | `create_refresh_token` | `refresh` | `REFRESH_TOKEN_EXPIRE_DAYS` = 30 days | `auth_time` (same value) |
| email verification | `create_email_verification_token` | `email_verification` | `EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` = 24 h | `email` |
| password reset | `create_password_reset_token` | `password_reset` | `PASSWORD_RESET_TOKEN_EXPIRE_HOURS` = 1 h | `pwd` (fingerprint of the current password hash) |
| OAuth 2FA challenge | `create_oauth_2fa_token` | `oauth_2fa` | `OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES` = 5 min | `provider` |
| organization invitation | `create_organization_invitation_token` | `organization_invitation` | `ORGANIZATION_INVITATION_EXPIRE_DAYS` = 7 days | subject is the invitation id |

Checks when a token is used:

- Special tokens: `decode_special_token(token, expected_type)` verifies
  signature and expiry, requires the right `type`, and requires `sub` to
  be a UUID. Any problem raises `SpecialTokenError`, which the services
  turn into a 422 (or 401 for the OAuth challenge).
- Access tokens on every service: `shared/security.py`,
  `make_get_current_user`: missing header -> 401 "Missing bearer token";
  bad signature or expired -> 401 "Invalid or expired token"; `type` not
  `access` -> 401 "Wrong token type"; `sub` not a UUID -> 401. Then the
  live state check (section 3.10).

Because all special tokens share the secret, the `type` check is what
stops a password-reset token from being used as an access token, and so
on.

**`auth_time`: when the user really logged in.** `iat` is renewed by every
refresh, so it cannot tell a fresh login from a session kept alive for
days. `_issue_token_pair(..., auth_time=...)` therefore writes an
`auth_time` claim into both tokens: the current time for a password login,
an OAuth callback and the OAuth 2FA step (`_now_timestamp()`), and for a
refresh the value read back from the presented refresh token
(`security.refresh_token_auth_time`). A refresh keeps the login time; it
never renews it. A token issued before the claim existed has none, and a
refresh of such a token gives tokens without it (the user simply logs in
again when it matters). No database column is needed: the value travels
in the signed refresh token. `shared/security.py` exposes it as
`CurrentUser.auth_time` (an `int`, or `None`), so the rule of section 3.8
reads it from the access token.

`JWT_SECRET` safety: `shared/config.py` refuses to start when `APP_ENV` is
not `development` or `test` and the secret is empty, `CHANGE_ME`, or
shorter than 32 characters. In development it only logs a warning.

`python-jose` is the JWT library (`from jose import jwt` in
`security.py`); `passlib` with bcrypt hashes passwords
(`CryptContext(schemes=["bcrypt"])`), with `bcrypt<4.1` pinned in
`pyproject.toml` because newer bcrypt breaks passlib's self-test.

### 3.4 Registration and email verification

```
 client                 gateway        auth-service                 Postgres    Redis     SMTP (MailHog)
   | POST /api/v1/auth/register          |                              |          |            |
   |  {email, username, password}        |                              |          |            |
   |------------------------------------>| FastAPI validates the body   |          |            |
   |                                     |  first (422 on bad input):   |          |            |
   |                                     |  email lowercased,           |          |            |
   |                                     |  username ^[A-Za-z0-9_.-]{3,32}$          |            |
   |                                     |  password >= 10 chars, <= 72 bytes        |            |
   |                                     | rate_limit.hit("register:ip",|          |            |
   |                                     |   client ip, 10 per 3600 s) ------------>|            |
   |                                     | email/username taken? -> 409 |          |            |
   |                                     | bcrypt hash, INSERT user,    |          |            |
   |                                     |  profile, settings --------->|          |            |
   |                                     | mirror state (role user) ------------->  |            |
   |                                     | make verification JWT (24 h) |          |            |
   |                                     | COMMIT (get_db) ------------>|          |            |
   |<-------- 201 "Registered. Check your email..."                     |          |            |
   |                                     | BackgroundTask: send email ---------------------------->|
   |                                     |  link = FRONTEND_BASE_URL/verify-email?token=<jwt>       |
   |                                                                                               |
   | POST /api/v1/auth/verify-email {token}                                                        |
   |------------------------------------>| decode (type email_verification)                        |
   |                                     | user exists? (else 404)                                 |
   |                                     | email claim == current email? (else 422)                |
   |                                     | is_verified = true, COMMIT ->|                          |
   |<-------- 200 "Email verified."      |                              |                          |
```

Code: `domains/auth/api.py` (`register`, `verify_email`,
`resend_verification_email`), `domains/auth/service.py`
(`AuthService.register`, `verify_email`, `resend_verification`).

Details worth knowing:

- Emails are compared and stored lowercase (`normalize_email`), and
  usernames are unique ignoring case (repository queries use
  `func.lower`).
- Two concurrent registrations with the same email race on the unique
  index. The insert runs inside a savepoint (`begin_nested`), so the
  loser gets 409, not 500.
- The email is sent **after** the commit by a `BackgroundTask`
  (`send_verification_email_safely`, a failure is logged, never raised).
  Sending inside the request would turn an SMTP failure into a 500 for
  every registration, hold the database connection while a hanging SMTP
  server waits (up to 60 s), and mail a link to an account a failed
  commit never stored. A lost email is recovered with "resend".
- The `email` claim inside the token stops an old link from verifying a
  newer address after the email was changed.
- `POST /verify-email/resend {email}` is limited per email (3 per hour)
  and always answers the same 200 text, whether or not the account
  exists, so it cannot be used to discover accounts. It sends nothing for
  an unknown, already verified or deactivated account.
- The link points at the frontend (`FRONTEND_BASE_URL`, default
  `http://localhost:3000`). The frontend is expected to read `token` from
  the URL and `POST` it to `/api/v1/auth/verify-email`.
- **Unverified accounts are purged after 7 days.** If someone registers
  an address they do not own and never verifies it, the real owner gets
  409 on register and 409 `oauth_link_refused` on an OAuth sign-in with
  that address, until the nightly job `purge-unverified` (04:10 New York,
  chapter 26) deletes the account. It deletes every account that
  registered more than `UNVERIFIED_ACCOUNT_RETENTION_DAYS` (default 7)
  days ago, is not verified, has never signed in (`last_seen_at` is
  null) and has no OAuth link. Each one goes through the admin soft
  delete (section 3.19, `AdminService._delete`): the email and username
  are replaced, so the address can register again, and `user.deleted` is
  published with the user as `actor_id`. The database does not store
  "was verified once", so "never signed in" stands for "never verified":
  login needs a verified email, and every login sets `last_seen_at`. An
  account an admin moved to a new, unverified email after its owner had
  signed in is therefore kept. Before the purge, the owner can also
  recover the address with "resend verification" (the link goes to their
  mailbox and verifies the account) followed by "password reset", and an
  admin can change or delete the squatted account (section 3.19).

### 3.5 Login

```
 client                         auth-service                          Postgres     Redis
   | POST /api/v1/auth/login {email, password, totp_code?}               |           |
   |------------------------------->| (body validated first: email, password <= 72 B)  |
   |                                | hit("login:ip",  30 / 900 s) ------------------>|
   |                                | hit("login:email",10 / 900 s) ----------------->|
   |                                | user by lower(email) ------------->|           |
   |                                | unknown -> dummy bcrypt check, 401 |           |
   |                                | wrong password -> 401              |           |
   |                                | deleted -> 401; inactive -> 401    |           |
   |                                | not verified -> 401 email_not_verified          |
   |                                | 2FA enabled?                       |           |
   |                                |   no code -> 401 two_factor_required            |
   |                                |   locked? -> 429 ------------------------------>|
   |                                |   code matches step s, s > last_used_step?      |
   |                                |     no -> count failure, 401 ------------------->|
   |                                | access JWT (15 min, role claim)    |           |
   |                                | refresh JWT (30 d)                 |           |
   |                                | INSERT user_sessions (sha256(refresh), ip, UA)  |
   |                                |  (and delete this user's sessions expired > 7 d)|
   |                                | last_seen_at = now; offline->online? queue       |
   |                                |  user.presence.changed (pending_events)         |
   |                                | COMMIT --------------------------->|           |
   |<-- 200 {data: {access_token, refresh_token, token_type: "bearer"}}  |           |
   |                                | background task: publish        |           |
   |                                |  user.presence.changed ----------------------->|
```

Code: `AuthService.login`, `_issue_token_pair`, `_check_totp`,
`_mark_online`.

- **Timing safety:** for an unknown email the service still runs one
  bcrypt verification against a dummy hash (`dummy_verify_password`), so
  the answer time does not reveal whether the account exists.
- **Error codes** (the `code` field of the error body):
  `invalid_credentials`, `email_not_verified`, `two_factor_required`
  (all 401), `rate_limited` (429).
- `ip_address` is `request.client.host` (the real client address, because
  of the gateway's `X-Forwarded-For` and uvicorn's `--proxy-headers`, see
  chapter 10) and `device_info` is the `User-Agent` header.
- A deactivated account gets "This account has been deactivated." and a
  soft-deleted one the plain invalid-credentials answer.

### 3.6 Refresh, rotation and reuse detection

The refresh token itself is never stored. `security.hash_refresh_token`
stores `sha256(token)` (hex) in `user_sessions.refresh_token_hash`, which
has a unique index. A database leak therefore does not leak usable
refresh tokens.

`revoked_reason` values (`SessionRevokeReason` in
`domains/auth/model.py`):

| Value | Set by | Meaning |
|---|---|---|
| `rotated` | `claim_session_for_rotation` (every refresh) | replaced by a refresh; the token was single-use |
| `logout` | `revoke_session` (default reason), called by logout | the user logged out of this session |
| `revoked_all` | `revoke_all_sessions` (default reason) | password reset confirmed, account deactivated by an admin or moderator, account deleted |
| `reuse` | `revoke_all_sessions(..., reason=REUSE)` in `refresh` | revoked because a rotated token was presented again |
| `NULL` | rows revoked before the column existed (migration `f1190b000001`) | treated like `rotated` |

```
 client                     auth-service                                   Postgres
   | POST /refresh {refresh_token: R}                                          |
   |--------------------------->| UPDATE user_sessions                         |
   |                            |   SET revoked_at=now, revoked_reason='rotated'|
   |                            |   WHERE hash=sha256(R) AND revoked_at IS NULL |
   |                            |     AND expires_at > now  RETURNING *  ------>|
   |                            |                                              |
   |       row returned (R was valid and is now used up):                      |
   |                            | user active, not deleted? else 401           |
   |                            | new access + new refresh R' -> INSERT session|
   |                            | (copies ip_address and device_info)          |
   |<-- 200 {access_token, refresh_token: R'}  (commit)                        |
   |                                                                           |
   |       no row returned:                                                    |
   |                            | SELECT session by sha256(R)                  |
   |                            | found AND revoked AND reason in (NULL, rotated)?
   |                            |   yes -> "reuse": revoke ALL active sessions |
   |                            |          of that user, reason 'reuse',       |
   |                            |          COMMIT now, log                     |
   |                            |          refresh_token_reuse_detected        |
   |<-- 401 "Refresh token is invalid or expired."                             |
```

Why this design:

- **Atomic claim.** One `UPDATE ... WHERE revoked_at IS NULL ...
  RETURNING` decides the winner. Two concurrent refreshes with the same
  token cannot both succeed; the second sees no row.
- **The reuse rule.** If a token that was already *rotated* comes back,
  either the client has a bug or an attacker copied the token. The
  server cannot tell who is the real owner, so it logs the user out
  everywhere (every still-active session gets `reuse`). The commit happens
  inside the error path on purpose: the 401 that follows rolls back the
  request transaction, which would otherwise undo the revocation.
- **Only `rotated` (or `NULL`) triggers it.** A token revoked by a logout
  or a "revoke all" just gets a 401. Without this distinction, a second
  browser tab still holding a logged-out token would log the user out of
  every device the next time it tried to refresh. The `revoked_reason`
  column exists exactly to tell these cases apart.
- **Expired-session purge.** Every login and refresh adds a row. To stop
  unbounded growth, `create_session` first deletes that user's sessions
  whose `expires_at` is more than 7 days in the past
  (`_EXPIRED_SESSION_RETENTION` in `domains/auth/repository.py`). An
  expired token is refused anyway, so reuse detection only needs the rows
  before expiry. There is no separate scheduled purge job; the cleanup
  happens per user at the next login or refresh.
- A refresh also counts as "seen" (`_issue_token_pair` always marks the
  user online), which keeps
  presence up to date for an active client.
- A refresh carries the `auth_time` of the original login over into the
  new pair (section 3.3), so refreshing never counts as a recent login.

### 3.7 Logout

```
 client                       auth-service                      Postgres       Redis (db 2)
   | POST /logout {refresh_token: R}                                 |               |
   |----------------------------->| session by sha256(R)            |               |
   |                              | not found or already revoked:   |               |
   |                              |   nothing happens               |               |
   |                              | else revoked_at=now,            |               |
   |                              |   revoked_reason='logout' ----->|               |
   |                              | other session used in 15 min?   |               |
   |                              | no:                             |               |
   |                              |   last_seen_at = now - 5 min - 1 s              |
   |                              |   queue user.presence.changed   |               |
   |                              |   {is_online: false}            |               |
   |<-- 204 (commit)              |                                 |               |
   |                              | background task: publish ---------------------->|
```

`AuthService.logout`, `_mark_offline`. The user is marked offline unless
another session is still in use. "In use" means not revoked, not expired
and created within the access-token lifetime
(`ACCESS_TOKEN_EXPIRE_MINUTES`, 15): every refresh rotates the session
into a new row, so a client that is still open has a session row that
young (`UserRepository.count_live_sessions`). A session left behind by a
closed browser stays unrevoked until it expires, but it stops being
rotated, so it does not keep the user online. When no session is in use,
`last_seen_at` is moved back past the online threshold, exactly as the
WebSocket "offline" heartbeat does (section 3.15). If another device is in
fact still connected, its next presence heartbeat (every 60 s) marks the
user online again and announces it, so the live and REST views converge
within a minute. The REST views
(`is_online` in `/internal/users/{id}`, connection lists, the admin list)
then agree with the event, and the next login or heartbeat is seen as an
offline -> online change and announced again. No access token is needed: whoever holds the
refresh token may end its session. The access token issued with it stays
valid until it expires (at most 15 minutes); access tokens are not
tracked server-side. "Log out everywhere" exists only as a side effect of
password reset, deactivation, deletion and reuse detection; there is no
user-facing endpoint for it.

### 3.8 Two-factor authentication (TOTP with `pyotp`)

```
 client (logged in)                auth-service                              Postgres
   | POST /2fa/setup                     |                                        |
   |------------------------------------>| auth_time older than 10 min? -> 403    |
   |                                     | already enabled? -> 409                |
   |                                     | secret = pyotp.random_base32()         |
   |                                     | upsert two_factor_secrets              |
   |                                     |  (enabled=false, last_used_step=NULL;  |
   |                                     |   never overwrites an ENABLED row) --->|
   |<-- 200 {secret, provisioning_uri="otpauth://totp/AlphaMind:<email>?..."}     |
   |  (user scans the QR code into an authenticator app)                          |
   | POST /2fa/verify {code}             |                                        |
   |------------------------------------>| auth_time older than 10 min? -> 403    |
   |                                     | _check_totp: lock? -> 429              |
   |                                     | code matches step s in {now-1, now, now+1}?
   |                                     | UPDATE ... SET last_used_step=s        |
   |                                     |   WHERE last_used_step IS NULL OR < s  |
   |                                     | already enabled? -> 204, no change     |
   |                                     | enabled = true, event user.updated --->|
   |<-- 204                              |                                        |
   | POST /2fa/disable {code}            | same code check, enabled = false       |
```

Code: `AuthService.setup_two_factor`, `verify_two_factor`,
`disable_two_factor`, `_check_totp`; `security.match_totp_step`;
`UserRepository.upsert_two_factor_secret`, `consume_totp_step`.

- **Window:** the current 30-second step plus one step before and after
  (clock drift tolerance). The comparison uses `hmac.compare_digest`.
- **No replay:** the accepted step is stored in `last_used_step`, and the
  update only succeeds when the new step is larger. The same code (or an
  older one) cannot be used twice.
- **Lockout:** each failure calls `rate_limit.two_factor_failed`. After
  `RATE_LIMIT_2FA_FAILURES` = 5 failures within
  `RATE_LIMIT_2FA_WINDOW_SECONDS` = 300 s, the user's 2FA is locked for
  `RATE_LIMIT_2FA_LOCK_SECONDS` = 900 s; every check during the lock gets
  429 "Too many invalid codes" with `Retry-After`. The lock is per user,
  not per address.
- **Setup does not disable 2FA.** Calling setup on an enabled account
  would replace the secret and silently turn 2FA off, so it is a 409; you
  must disable with a valid code first. The database upsert (one atomic
  `INSERT ... ON CONFLICT`, so a double click cannot race) also refuses
  to overwrite an enabled row; the caller then sees `enabled=True` and
  answers 409.
- The issuer name in the app is `TOTP_ISSUER` = `AlphaMind`.
- `/2fa/verify` on an account whose 2FA is already enabled checks the
  code (and consumes its step) but changes nothing and publishes no
  `user.updated`.
- **Setup and verify need a recent login.** Both routes use the
  dependency `deps.require_recent_login` instead of `get_current_user`.
  It answers **403** `recent_login_required` (`RecentLoginRequiredError`,
  a `PermissionDeniedError`; message "This action needs a recent login.
  Log in again (password or OAuth) and retry within the next few
  minutes.") unless the access token's `auth_time` (section 3.3) is at
  most `TWO_FACTOR_RECENT_LOGIN_MINUTES` (default 10) minutes old
  (`security.is_recent_login`). It applies to password and OAuth users
  alike, because both logins set `auth_time`; a token without the claim
  is refused. So a stolen access token, or a session kept alive by
  refreshes, cannot turn 2FA on with an attacker's secret: that needs
  the password (or the OAuth account) within the last 10 minutes. The
  frontend sends the user through login again and retries. Turning 2FA
  off always needs a valid code and is not affected.
- **Secrets are encrypted at rest** (`security.py`, Fernet through
  `cryptography`'s `MultiFernet`). The stored form is `fernet:<token>`.
  New values use `TOTP_ENCRYPTION_KEY` when it is set, otherwise a key
  derived from `JWT_SECRET`; reading tries both keys.
  `UserRepository.upsert_two_factor_secret` encrypts before it writes, and
  `_check_totp` decrypts. Fernet is AES-128-CBC with an HMAC, so a
  leaked database does not give away every user's second factor. A value
  without the prefix is a plain-text secret: it still works, and
  `_check_totp` encrypts it after the user's next successful check.
  Because reading tries both keys, secrets written in development before
  a `TOTP_ENCRYPTION_KEY` was set stay readable after it is set.
  If neither key can read a stored value (for example after both
  `TOTP_ENCRYPTION_KEY` and `JWT_SECRET` changed), `decrypt_secret`
  raises `UnreadableTotpSecretError`; `_check_totp` logs
  `totp_secret_unreadable` and answers **409** "Your 2FA settings cannot
  be read. Ask an administrator to reset your 2FA, then set it up again."
  (login, OAuth 2FA step, `/2fa/verify` and `/2fa/disable`; not counted
  as a failed code). An admin then calls
  `DELETE /api/v1/admin/users/{id}/2fa` (section 3.19).

### 3.9 OAuth sign-in (Google, GitHub) and the OAuth 2FA challenge

The OAuth flow is written by hand with `httpx` in
`domains/auth/service.py` (no OAuth library). The provider endpoints are
in `_OAUTH_PROVIDERS`, keyed by `shared.enums.OAuthProvider`; the
route's plain path parameter goes through `_oauth_provider_config`, so an
unknown provider is a `422` in the project's error body:

| Provider | Authorize URL | Token URL | User info | Scope |
|---|---|---|---|---|
| `google` | `https://accounts.google.com/o/oauth2/v2/auth` | `https://oauth2.googleapis.com/token` | `https://openidconnect.googleapis.com/v1/userinfo` | `openid email profile` |
| `github` | `https://github.com/login/oauth/authorize` | `https://github.com/login/oauth/access_token` | `https://api.github.com/user` (+ `https://api.github.com/user/emails`) | `read:user user:email` |

The redirect URI registered with the provider must be
`<OAUTH_REDIRECT_BASE_URL>/api/v1/auth/oauth/<provider>/callback`
(default base `https://localhost:8443`). `_oauth_redirect_uri` builds it
from the API prefix the router is mounted under, not from a hard-coded
`/api/v1`. `oauth_authorize_url` needs no database session.

Three values protect one sign-in, all created by the authorize step:

| Value | Where it lives | What it proves |
|---|---|---|
| `state` (24 random bytes) | URL parameter; Redis key `oauth:state:<state>` (TTL 300 s) | the callback answers a sign-in this server started; read and deleted in one `GETDEL`, so it works exactly once even under concurrent callbacks |
| nonce (32 random bytes) | HttpOnly cookie `alphamind_oauth_nonce` (`Secure`, `SameSite=Lax`, `Path=/api/v1/auth/oauth`, `Max-Age=300`); only its SHA-256 is stored in the Redis value | the callback arrives in **the same browser** that started the sign-in |
| PKCE `code_verifier` (48 random bytes) | Redis value only; the provider sees `code_challenge = BASE64URL(SHA-256(verifier))` with `code_challenge_method=S256` | the authorization code can only be redeemed by this server's flow; a stolen code is useless without the verifier |

The nonce cookie is what stops login CSRF. Without it, an attacker could
start a sign-in with their own provider account, stop at the redirect,
and send the victim the link `.../callback?code=<attacker code>&state=<attacker state>`;
the victim's browser would then be signed in to the attacker's account
(and, for example, upload documents into it). With the cookie, the
victim's browser has no nonce (or a different one), so the callback
answers 401 `oauth_failed` ("This sign-in was not started in this
browser") and logs `oauth_browser_binding_failed`. The state is consumed
by that failed attempt, so it cannot be retried. `SameSite=Lax` is
required (not `Strict`): the provider's redirect back is a cross-site
top-level `GET`, which `Lax` still sends the cookie with. The frontend
must open `/oauth/<provider>/authorize` as a top-level navigation (a
link or `window.location`), never with `fetch`, so the browser stores
the cookie. Starting a second sign-in in the same browser replaces the
cookie, so only the newest of two parallel sign-ins can finish. Google
and GitHub both accept PKCE with `S256` for web applications.

```
 browser             auth-service                       Redis (db 0)         provider
   | GET /oauth/google/authorize |                            |                  |
   |---------------------------->| state, nonce, code_verifier (random)          |
   |                             | SET oauth:state:<state> =                     |
   |                             |   {provider, sha256(nonce), verifier} EX 300->|
   |<-- 307 to provider authorize URL (client_id, redirect_uri, scope, state,    |
   |    code_challenge, code_challenge_method=S256)                              |
   |    + Set-Cookie: alphamind_oauth_nonce=<nonce>; HttpOnly; Secure; Lax       |
   |---------------------------------------------------------------------------->|
   |  user agrees                                                               |
   |<-- redirect to .../oauth/google/callback?code=..&state=.. ------------------|
   | GET /oauth/google/callback  (cookie alphamind_oauth_nonce sent)            |
   |---------------------------->| error param? -> 401 oauth_failed              |
   |                             | GETDEL oauth:state:<state>; provider matches? |
   |                             |   (single use, else 401) ---->|                 |
   |                             | sha256(cookie nonce) == stored hash?          |
   |                             |   (else 401, sign-in from another browser)    |
   |                             | POST token_url (code, code_verifier) -------->|
   |                             | GET userinfo (+ /user/emails for GitHub) ---->|
   |                             | resolve local user (see below)                |
   |                             | inactive or deleted -> 401                    |
   |                             | 2FA enabled?                                  |
   |<-- 200 {two_factor_required: true, challenge_token, expires_in: 300}        |
   |      or 200 {access_token, refresh_token}; the nonce cookie is deleted      |
   |                             |                                               |
   | POST /oauth/2fa {challenge_token, code}                                     |
   |---------------------------->| decode (type oauth_2fa), user still active    |
   |                             | _check_totp (lockout, no replay)              |
   |                             | SET oauth:2fa:used:<jti> NX (TTL = rest of    |
   |                             |   the challenge life) -- already set -> 401 ->|
   |<-- 200 {access_token, refresh_token}                                        |
```

Resolving the local user (`_resolve_oauth_user`):

1. An existing `oauth_accounts` row for (provider, provider user id) ->
   that user.
2. Otherwise, a local account with the same email is linked **only when
   both sides verified that email** (Google's `email_verified`, GitHub's
   primary *and* verified address from `/user/emails`, and the local
   `is_verified`). Otherwise 409 `oauth_link_refused` ("Log in with your
   password first"). Without this rule, anyone who controls an unverified
   provider account with your email could take over your local account.
3. Otherwise a new user is created. Its email is the provider email only
   if verified; else a placeholder `<provider>_<id>@users.alphamind.local`
   (an unverified email must not pre-claim an address). The username is
   derived from the email's local part (cleaned to `[A-Za-z0-9_.-]`, at
   most 24 characters, a number appended if taken). The password is a
   random 32-byte value nobody knows. `is_verified` is true only with a
   verified provider email.

Accounts created with a placeholder email have known limits. OAuth
sign-in does not require `is_verified` (password login does), so such an
account can be used, but: it can never accept an organization
invitation (the invitation email must match), it receives no email (the
domain `users.alphamind.local` does not exist; the
`notification-settings` route answers `email: null` for unverified
accounts, section 3.18), and the user cannot change the address
themselves (`PATCH /me` has no `email` field). Only an admin can set a
real address (`PATCH /api/v1/admin/users/{id}`, which sends a
verification link to it). The same placeholder is used when GitHub's
`/user/emails` call fails.

The session created by an OAuth sign-in records the client IP address
and the `User-Agent`, like a password login (`device_info` falls back to
`oauth:<provider>` when the request has no `User-Agent`).

Failure mapping: the provider says no or rejects the code -> 401
`oauth_failed`; the provider is down or answers garbage -> 502
`oauth_provider_error`; an unknown provider name -> 422
`unsupported_oauth_provider`; a concurrent callback for the same identity
-> 409.

The callback returns JSON (there is no frontend yet to redirect to). The
OAuth 2FA challenge exists because otherwise "Sign in with Google" would
have bypassed 2FA entirely.

With empty `GOOGLE_CLIENT_ID` / `GITHUB_CLIENT_ID` the flow fails at the
provider with `invalid_client`, which is the intended behaviour for an
unconfigured provider.

### 3.10 Roles and the live role mirror in Redis

**System roles** (`shared/enums.py`, `SystemRole`): `admin`,
`moderator`, `user`, `guest`. **Permissions** (`shared/permissions.py`):

| Permission | guest | user | moderator | admin |
|---|---|---|---|---|
| `portfolio:write`, `analysis:create`, `document:upload`, `note:write`, `comment:write`, `organization:create`, `api_key:manage`, `rag:ask` | - | yes | yes | yes |
| `content:moderate`, `user:deactivate` | - | - | yes | yes |
| `user:manage`, `site:stats` | - | - | - | yes |

An unknown role has no permissions (fail closed). Route code never compares
role names; it asks for a permission (`require_permission(...)` in
`shared/security.py`, used in `deps.py`). `GET /me` returns
`permissions` (sorted) so a frontend can hide what the user may not do.

**The problem the mirror solves.** An access token carries `role` and is
valid for 15 minutes. Without extra checks, a demoted admin or a
deactivated user would keep their power until the token expires.

**How it works** (`shared/user_state.py`, `user_state_sync.py`):

```
 auth-service (only writer)                     Redis db 2 (events)
   startup: mirror_all_users (batches of 500,   SET auth:user_state:<id>
            keyset pagination by id)  --------> '{"role": "user", "is_active": true}'
   register / OAuth create: mirror_user_safely   (no TTL)
   admin role/active/delete change,
   set_system_role script:
     commit_with_state: flush -> write Redis -> COMMIT
       (Redis write fails -> exception -> rollback, change not done)
       (commit fails -> previous state written back, best effort)

 every authenticated request in auth, core and ai (shared/security.py):
   GET auth:user_state:<id>
     hit  -> use it
     miss -> GET http://auth-service:8000/internal/users/<id>/state
             (404 -> treated as inactive), cache with
             SET ... NX EX USER_STATE_CACHE_TTL_SECONDS (300)
             (NX: only fills a missing key, never overwrites)
     both unreachable -> None -> fall back to the token's own claims (logged)
   is_active false -> 401 "Account is deactivated"
   otherwise the live role REPLACES the token's role claim
```

- A deleted user is stored as inactive and never removed, so that user's
  old tokens keep failing.
- The reader's write-back after an auth-service lookup uses `nx=True`.
  auth-service writes the key before its commit, so a reader that fetched
  the row just before that commit could otherwise overwrite a fresh
  deactivation with the old state for 300 s.
- Two writers with different failure rules: `mirror_user` (after a role,
  active or delete change) lets a Redis error propagate, because a
  deactivation that could not be mirrored must not be reported as done;
  `mirror_user_safely` (after a user is created) only logs, because a
  reader that misses the key asks `/internal/users/{id}/state`, which
  reads the database. `mirror_all_users` writes one Redis pipeline per
  batch of 500.
- The check is controlled by `USER_STATE_CHECK_ENABLED` (default true;
  test configurations turn it off).
- The fallback (both Redis and auth-service down -> trust the token) is a
  deliberate choice: an auth outage must not lock everybody out of core
  and ai.
- core-service applies the same rule to API keys (the key owner's live
  state) and to WebSockets (`api/ws_common.py`, `resolve_live_identity`).

### 3.11 Password reset

```
 client                         auth-service                       Redis     SMTP
   | POST /password-reset/request {email}                            |         |
   |------------------------------->| hit("reset:ip",   10 / 3600 s) ->|         |
   |                                | hit("reset:email", 3 / 3600 s) ->|         |
   |                                | user exists and not deleted?      |         |
   |                                |   token = JWT(type password_reset,|         |
   |                                |     pwd = sha256(password_hash)[:32], 1 h)  |
   |<-- 200 "If an account exists for this email, a reset link has been sent."   |
   |                                | BackgroundTask: email ----------------------->|
   |                                |   link = FRONTEND_BASE_URL/reset-password?token=...
   | POST /password-reset/confirm {token, new_password}                            |
   |------------------------------->| decode; user exists, not deleted              |
   |                                | pwd claim == fingerprint(current hash)?       |
   |                                |   no -> 422 "already been used"               |
   |                                | new bcrypt hash; revoke ALL sessions          |
   |                                |   (reason revoked_all); event user.updated    |
   |<-- 200 "Password has been reset." (commit, then event)                        |
```

- Same answer and same timing whether or not the account exists (the email
  goes out in a background task).
- **Single use without a database table:** the token carries a
  fingerprint of the password hash it was issued for. After the password
  changes, the fingerprint no longer matches, so every older reset link
  dies at once.
- A reset logs the account out everywhere.
- The new password follows the same rule as registration: at least
  `PASSWORD_MIN_LENGTH` = 10 characters, at most 72 bytes in UTF-8
  (`validate_new_password`; bcrypt ignores bytes after 72, so a longer
  password would be silently truncated).

### 3.12 The current user (`/me`) and avatars

- `GET /api/v1/auth/me`: id, email, username, `system_role`,
  `permissions`, `is_active`, `is_verified`, `last_seen_at`, `created_at`,
  `profile` (with a default avatar URL from DiceBear when none was
  uploaded, built by `default_avatar_url`) and `settings`.
- `PATCH /api/v1/auth/me` (`MeUpdate`): PATCH semantics (only fields sent
  change; an explicit `null` clears a nullable field; `null` for
  `username`, `theme` or `preferred_currency` is 422). Validation:
  username pattern, `timezone` must be a real IANA zone, `theme` in
  system/light/dark, `preferred_currency` three capital letters,
  `notification_preferences` keys in `in_app`/`email`/`websocket` with
  boolean values. Publishes `user.updated` with the *names* of changed
  fields (never values).
- `POST /api/v1/auth/me/avatar` (multipart `file`):

```
 client                     auth-service                                avatar_data volume
   | POST /me/avatar (file)       |                                            |
   |----------------------------->| declared Content-Type in png/jpeg/webp/gif? (else 422)
   |                              | read at most 5 MB + 1 byte; too big -> 422  |
   |                              | magic bytes -> real type; must equal declared
   |                              | file name <user id>-<random hex>.<ext> ---->|
   |                              | profile.avatar_url = /static/avatars/<name> |
   |                              | COMMIT (explicit, in the route)             |
   |                              |  (flush or commit error -> delete the new   |
   |                              |   file, re-raise)                           |
   |<-- 200 {avatar_url}          |                                            |
   |                              | background task, after the commit:          |
   |                              | delete the previous file (only names this   |
   |                              |  service produced, basename only) --------->|
```

The previous file is deleted by a background task after the commit, so
a failed commit never leaves the profile pointing at a deleted file. At
worst a failed delete leaves an unused file in the volume. The type
check is signature-only (`domains/auth/service.py::sniff_image_type`
reads the magic bytes; Pillow is not a dependency), and the delete
touches only URLs this service produced, by basename only, so a crafted
URL cannot reach another path.

Avatars live on local disk, in the named Docker volume `avatar_data`
mounted at `/app/data/avatars` (`docker-compose.yaml`). They are not in
object storage: they are small, few, and no other service reads them. The
nightly backup tars the volume (the scheduler mounts it read-only). The
gateway proxies `/static/avatars/...` to auth-service's `StaticFiles`
mount. A random file name per upload avoids a browser showing a cached old
image.

### 3.13 Organizations and invitations

Routes in `domains/organization/api.py` (prefix `/api/v1/organizations`):

| Method and path | Who may | Event |
|---|---|---|
| `POST ""` | permission `organization:create` (not guests) | `organization.created` |
| `GET ""` | any user (own memberships) | |
| `GET /{org_id}` | members | |
| `PATCH /{org_id}` | owner or admin | `organization.updated` |
| `DELETE /{org_id}` | owner only (404 for non-members, 403 for other members) | `organization.deleted` |
| `GET /{org_id}/members` | members | |
| `POST /{org_id}/members/{user_id}/remove` | owner or admin; only the owner removes an admin; the owner cannot be removed | `organization.member.removed` |
| `POST /{org_id}/leave` | any member except the owner | `organization.member.left` |
| `PATCH /{org_id}/members/{user_id}/role` | owner or admin; only the owner changes admin roles or makes admins; never `owner` (422) | `organization.role.changed` |
| `POST /{org_id}/invitations` | `organization:create` and owner/admin; only the owner invites admins | `organization.invitation.created` |
| `GET /{org_id}/invitations` | owner or admin: the open invitations (not accepted, not expired), newest expiry first | |
| `DELETE /{org_id}/invitations/{invitation_id}` | owner or admin; only the owner revokes an admin invitation; 404 for an unknown, accepted or foreign invitation | |
| `POST /invitations/accept` `{token}` | the invited email's account (guests too) | `organization.member.joined` |

Every route that needs membership answers 404 "Organization not found."
to a non-member, so an outsider cannot tell whether an organization id
exists. `DELETE /{org_id}` follows the same rule: a non-member gets 404,
a member who is not the owner gets 403.

Member roles (`OrganizationRole`): `owner`, `admin`, `member`, `viewer`.
There is no user-initiated ownership transfer: the owner cannot leave
(delete the organization instead). Ownership passes to another member
only when an admin deletes the owner's account (section 3.19).

Invitation flow:

```
 owner/admin          auth-service                              SMTP          invitee
   | POST /{org}/invitations {email, role}                        |              |
   |------------------->| role checks; hit("invite:org", 20/3600 s)               |
   |                    | already a member? -> 409                 |              |
   |                    | open admin invitation for this email and |              |
   |                    |   requester is not the owner? -> 403     |              |
   |                    | DELETE open invitations of this org for  |              |
   |                    |   the same email, and expired open ones  |              |
   |                    | INSERT invitation (expires now + 7 d)    |              |
   |                    | token = JWT(sub = invitation id, 7 d)    |              |
   |<-- 201 invitation  | (commit)                                 |              |
   |                    | BackgroundTask: email with               |              |
   |                    |   FRONTEND_BASE_URL/invitations/accept?token=... ------>|
   |                    | event organization.invitation.created    |              |
   |                    |   (user_id = invitee if they have an account)          |
   |                                                                              |
   |                    | POST /invitations/accept {token}  (invitee logged in) <--|
   |                    | decode; invitation exists, not accepted, not an owner  |
   |                    |   invitation, not expired                               |
   |                    | logged-in email == invitation email (case-insensitive)? |
   |                    |   else 403                                              |
   |                    | INSERT member (savepoint; double submit -> 409),       |
   |                    |   accepted_at = now; event organization.member.joined   |
```

The token's subject is the invitation row id, because the invitee may not
have an account yet. Invitations that grant `owner` (possible in old data)
are refused with 409.

- **The token travels in the body.** The accept route takes `{token}` in
  the JSON body, never in the URL path, so the invitation JWT does not
  appear in the gateway access log (which logs the path) or in the
  services' error logs (which log `request.url.path`). The email link
  carries it as a query parameter of the frontend page
  (`/invitations/accept?token=...`); the frontend reads it and posts it.
- **One open invitation per address.** Sending a new invitation to an
  address that already has an open one in the same organization deletes
  the old row, so the old link stops working (404
  "Invitation no longer exists.") and only the newest role applies. The
  same delete also removes the organization's expired, unaccepted rows;
  an expired link then answers 404 instead of "This invitation has
  expired." Replacing an open **admin** invitation counts as revoking it,
  so only the owner may do it: an admin who invites the same address
  gets 403 and the admin invitation stays (otherwise an admin could
  get around the owner-only revocation rule below by re-inviting the
  address as a member).
- **Revocation.** `DELETE /{org_id}/invitations/{id}` deletes an open
  invitation; its link answers 404 from then on. No event is published.
- Accepted rows are kept (the accept route uses them to answer "already
  accepted").

**Membership cache invalidation.** core-service and ai-service cache "which
organizations is this user in, with which role" in Redis
(`shared/org_membership_cache.py`): one key per user in DB 0 with a 60 s
TTL (`MEMBERSHIP_CACHE_TTL_SECONDS`). If the delete below fails because
Redis is down, the TTL bounds how long a removed member keeps access (at
most 60 s). For every membership-changing event
(created, deleted, joined, left, removed, role changed), auth-service
deletes the affected users' cache entries twice: once before the commit
(in `OrganizationService._org_event`) and once after it (in
`events_out.publish_events`), so an entry cached from old data in between
cannot survive. For a deleted organization, every former member's entry is
dropped.

### 3.14 Connections (friends)

`domains/social/` (prefix `/api/v1/connections`):

- `POST ""` `{addressee_username}`: a pending request (not to yourself; 409
  if any connection already exists; a blocked pair gets a neutral 409).
  Event `friend.requested`. Limited per sender to
  `RATE_LIMIT_FRIEND_REQUESTS_PER_USER` = 30 attempts per
  `RATE_LIMIT_FRIEND_REQUESTS_WINDOW_SECONDS` = 3600 s, counted before
  the username lookup, so the limit also covers "decline and re-send"
  loops and guessing usernames through the 404 answer.
- `GET ""` (optional `?status=pending|accepted|blocked`).
- `POST /{id}/accept`: only the addressee. Event `friend.accepted`.
  Accept, block and remove read the row's status and then write it, so
  they take a row lock first (`SELECT ... FOR UPDATE`); otherwise a
  concurrent accept and block both read "pending" and the later write
  could turn a block into a friendship.
- `POST /{id}/block`: either side; the blocked side cannot take over a
  block. Stores `blocked_by`.
- `DELETE /{id}`: cancel, decline, unfriend, or unblock (unblock only by
  the user who blocked). Only unfriending publishes `friend.removed`
  (an unblock notice would reveal the block).

Connections matter for presence: core-service lets a user subscribe to
another user's `presence:<id>` WebSocket channel only when
`/internal/connections/check` says the connection is `accepted`.

### 3.15 Presence

"Online" means `last_seen_at` is less than
`PRESENCE_ONLINE_THRESHOLD_MINUTES` = 5 minutes ago (`is_user_online`).

`last_seen_at` is updated by: login, OAuth login, the OAuth 2FA step,
refresh (all through `_issue_token_pair`, which always marks the user
online, because an active client is "seen"), and core-service's
WebSocket heartbeat:

```
 core-service (websocket_manager.py)                auth-service
   first socket of a user opens:
     POST /internal/users/{id}/presence {"online": true}  ----> last_seen_at = now
                                                               offline -> online? publish
                                                               user.presence.changed {is_online: true}
   every 60 s while any socket is open:
     POST ... {"online": true}  -------------------------------> last_seen_at = now
   last socket closes:
     POST ... {"online": false} -------------------------------> last_seen_at = now - 5 min - 1 s
                                                               publish {is_online: false}
```

`AuthService.set_presence`, `_mark_online`, `_mark_offline`. A logout
after which no other session was used within the access-token lifetime
(section 3.7) goes through the same `_mark_offline`
as the "offline" heartbeat: `last_seen_at` moves back past the threshold
and `is_online: false` is published. A later login or heartbeat is then
an offline -> online change and publishes `is_online: true` again. The service builds the event with
`presence_event()` and queues it on `AuthService.pending_events`; the
route (`login`, `refresh`, `logout`, `oauth_callback`, the OAuth 2FA step,
`POST /internal/users/{id}/presence`) publishes it with
`events_out.schedule_events` after the commit, so a rolled-back request
never announces a change. A presence publish failure is logged by
`events_out.publish_events` and never fails a login or refresh.

### 3.16 Rate limits (`rate_limit.py`)

All counters live in the rate-limit Redis database (db 1) under keys
`auth:rl:<scope>:<sha256 of the identity, 32 hex chars>`. The identity
(email or IP) is hashed so raw addresses never sit in Redis keys. The
counter itself is shared with core-service's public API
(`shared/rate_limit.py`, `fixed_window_hits`): one Redis transaction does
`SET key 0 EX window NX`, `INCR key`, `TTL key`, so the window starts at
the first hit and a key can never be left without an expiry. A Redis
error **fails open** (logged as `rate_limit_unavailable`).

| Scope (key) | Identity | Limit | Window | Where |
|---|---|---|---|---|
| `register:ip` | client IP | 10 | 3600 s | `POST /register` |
| `login:ip` | client IP | 30 | 900 s | `POST /login` |
| `login:email` | email | 10 | 900 s | `POST /login` |
| `reset:ip` | client IP | 10 | 3600 s | `POST /password-reset/request` |
| `reset:email` | email | 3 | 3600 s | same |
| `resend:email` | email | 3 | 3600 s | `POST /verify-email/resend` |
| `invite:org` | organization id | 20 | 3600 s | invitations |
| `friend:user` | requester user id | 30 | 3600 s | `POST /api/v1/connections` |
| `2fa:fail` / `2fa:lock` | user id | 5 failures | 300 s window, 900 s lock | every TOTP check |

Over the limit: 429 with body code `rate_limited`, the message "Too many
attempts. Please try again later." (the 2FA lock says "Too many invalid
codes. Please try again later.") and a `Retry-After` header (seconds
until the window ends). `RATE_LIMIT_ENABLED=false` turns
all of them off. The gateway adds its own coarse limit in front
(chapter 10).

### 3.17 Events published (`events_out.py`)

Pattern: a service method appends `(EventName, payload)` to
`self.pending_events`; the route calls `schedule_events(background_tasks,
service.pending_events)`; the publish runs as a `BackgroundTask`, i.e.
**after** the commit. A subscriber can never see an event for a change
that was rolled back. A publish failure is logged
(`event_publish_failed`) and never raised. Transport: Redis pub/sub
(`shared/events.py`, `publish_cross_service`) on channel
`events:<event name>`; each publish gets a fresh `event_id` so core-service
can de-duplicate across its replicas.

| Event | Payload | Published when |
|---|---|---|
| `user.updated` | `user_id, actor_id, role, is_active, changed` (field names) | own profile/settings/password/2FA/avatar change (any changed field, including `bio` or `theme`; a PATCH that changes nothing and a repeated `/2fa/verify` publish nothing); admin edit |
| `user.role.changed` | same | admin changes the role |
| `user.deactivated` / `user.reactivated` | same | admin or moderator |
| `user.deleted` | same | admin soft delete; the nightly unverified-account purge (`actor_id` is the user itself) |
| `organization.created` / `.updated` / `.deleted` | `organization_id, organization_name, actor_id, user_id, role, member_ids` | |
| `organization.member.joined` / `.left` / `.removed` | same | |
| `organization.role.changed` | same | |
| `organization.invitation.created` | same (`user_id` = invitee if registered) | |
| `friend.requested` / `.accepted` / `.removed` | `actor_id, user_id, actor_username` | |
| `user.presence.changed` | `user_id, is_online` | queued by login/refresh/presence/logout and published after the commit, like the other events |

Who listens: core-service turns them into notifications and live
WebSocket pushes (every `user.updated`, including a user's own profile
edit, becomes an "Account updated" notification for that user) (chapter 12, section 3.15; chapter 24). Note that a
deactivated user's tokens stop working through the live state check, not
through these events; the events are for notifications only.

### 3.18 Internal routes for other services (`api/internal.py`)

Mounted at `/internal` directly on the app (not under `/api/v1`: internal
routes stay unversioned and are never proxied by the gateway), hidden
from the OpenAPI document (`include_in_schema=False`), and guarded by
`require_internal_token`. auth-service has no `/internal/stats`: its own
admin page reads its counts from the database.

| Route | Used by | Returns |
|---|---|---|
| `GET /internal/users/{id}` | core `auth_client.get_user_ref` | `UserRef` {id, username, avatar_url, is_online} |
| `GET /internal/users/{id}/state` | `shared/user_state.py` on a Redis miss | `{role, is_active}` (deleted = inactive; 404 only for an unknown id) |
| `POST /internal/users/{id}/presence` | core WebSocket heartbeat | 204 |
| `GET /internal/users/{id}/organizations` | core and ai membership lookups | `[{id, name, role}]` |
| `GET /internal/connections/check?user_a=&user_b=` | core WebSocket presence channel | `{connected: bool}` |
| `GET /internal/users/{id}/notification-settings` | core notifications | `{email, notification_preferences}`; `email` is `null` when the account is deleted, deactivated or unverified (an anonymized, placeholder or unproven address), so core sends no email to it |
| `POST /internal/scheduler/purge-unverified` | the scheduler (`call-job.sh purge-unverified`, daily 04:10 New York) | `{retention_days, candidates, deleted, skipped, failed}`; 500 with `all_units_failed: ["accounts"]` when every attempted account failed (`shared/scheduled_jobs.py`) |

### 3.19 Administration (`domains/admin/`) and the first admin

Routes under `/api/v1/admin`:

| Route | Permission |
|---|---|
| `GET /users` (page, page_size up to 100, `q`, `role`, `is_active`, `include_deleted`, `sort`, `order`) | `user:manage` |
| `GET /users/{id}` | `user:manage` |
| `PATCH /users/{id}` (username, email, is_verified, profile fields, role, is_active) | `user:manage` |
| `DELETE /users/{id}` (soft delete) | `user:manage` |
| `DELETE /users/{id}/2fa` (2FA reset; 204 also when none was set up; 403 on one's own account) | `user:manage` |
| `POST /users/{id}/deactivate`, `/reactivate` | `user:deactivate` (moderators only on users and guests) |
| `GET /stats` (auth counts read from the database in two queries, the counts and `users_by_role`, plus core and ai `/internal/stats`; a part is `null` when that service is unreachable or answers garbage, logged `admin_stats.service_unavailable`, never an error) | `site:stats` |

Rules (`AdminService`): an admin cannot change their own role or
deactivate or delete themselves; the **last active admin** cannot be
demoted, deactivated or deleted (409; the check locks the active admin
rows with `SELECT ... FOR UPDATE` so two concurrent demotions cannot both
pass). Deactivation revokes all sessions (`revoked_all`). A changed email
becomes unverified and gets a new verification email. Soft delete sets
`deleted_at`, `is_active=false`, replaces email and username with
`deleted-<hex>@deleted.alphamind.local` / `deleted_<hex>`, sets a random
password, clears the profile, deletes OAuth links, 2FA secret and
connections, removes organization memberships except owner rows, revokes
all sessions, and deletes the avatar file after the commit. The steps
after the permission checks are one method, `AdminService._delete(user,
actor_id)`, shared with the nightly purge of unverified accounts
(`purge_unverified_accounts`, section 3.4): it lists the candidates
(`AdminRepository.unverified_account_ids`), then for each one locks the
row again with the same conditions (`lock_unverified_account`, `SELECT
... FOR UPDATE`, so an account verified or signed in meanwhile is
skipped) and deletes it in its own transaction. A failure rolls back
that account only, drops its queued events and is logged
(`unverified_account_purge_failed`); the last active admin is skipped
(`unverified_account_purge_skipped`). The run ends with the log line
`unverified_accounts_purged` (candidates, deleted, skipped, failed). An
organization the deleted user owned passes to its longest-standing
admin, else its longest-standing other member (`successor_member`), who
becomes owner (event `organization.role.changed`, log
`organization_owner_transferred`); without this nobody could manage it
any more. Only an organization with no other member keeps its deleted
owner row. A moderator (`user:deactivate` without `user:manage`) may
deactivate and reactivate only accounts whose role is `user` or `guest`.
When an admin changes an email and does not set `is_verified` in the
same request, the account becomes unverified and a verification link
goes to the new address (login needs a verified email). A 2FA reset
(`reset_two_factor`) deletes the user's `two_factor_secrets`
row, keeps the sessions, queues `user.updated` with `changed:
["two_factor_enabled"]` and logs `admin_two_factor_reset`; an admin turns
off their own 2FA only with a code (`/auth/2fa/disable`). Every role,
active or delete change goes through `commit_with_state`, so the Redis
mirror is updated before the request returns.

**How the first admin is created** (`scripts/set_system_role.py`). There
is no "make me admin" API: every admin route already needs an admin. So
the first admin is made from the command line, inside the container:

```
docker compose --env-file .env.docker exec auth-service \
    python -m scripts.set_system_role --email you@example.com --role admin --dry-run
docker compose --env-file .env.docker exec auth-service \
    python -m scripts.set_system_role --email you@example.com --role admin
```

The script finds the user by email (normalized), refuses an unknown or
deleted account and demoting the last active admin, then writes the role
with `commit_with_state` (database and Redis mirror together, so tokens
already issued get the new role at once). `--dry-run` prints the change
and writes nothing. Exit code 0 on success or no change, 1 when refused.
`--role` accepts `admin`, `moderator`, `user`, `guest`.

### 3.20 Migrations and schema bootstrap

- `migrations/versions/f1160b000001_initial_schema.py`: the baseline.
  It creates every table of section 3.2.
- `migrations/versions/f1190b000001_session_revoked_reason.py`: adds
  `user_sessions.revoked_reason VARCHAR(20) NULL` (nullable and additive;
  old revoked rows keep NULL).
- `migrations/versions/f1210b000001_drop_organization_roles.py`:
  drops the `organization_roles` table. Custom organization roles are
  not supported: authorization uses only `organization_members.role`, so
  a table of custom roles would be read by nothing. The table is dropped
  with its rows; the downgrade recreates it empty, as the baseline
  created it. Head: `f1210b000001`.
- `migrations/archive/`: an older migration chain, kept only so
  `python -m scripts.alembic_convert` can convert a database that still
  carries one of its revisions (preview by default, `--apply` to convert
  in one transaction; take a backup first). Both go once no backup from
  before the baseline is kept.
- `migrations/env.py` uses the synchronous URL (`postgresql+psycopg`) and
  keeps the `alembic_version` table inside the `auth` schema
  (`version_table_schema`), so the three services' migrations never
  touch each other. It skips the `alembic_version` table during
  comparison with `shared.alembic_bootstrap.exclude_alembic_version`, the
  same predicate the startup drift check uses. The details of `env.py` (search path, type and
  default comparison, the explicit commit) are the same in all three
  services and are explained in [chapter 20](20-postgresql.md).

On every start `db_bootstrap.ensure_schema` calls
`shared/alembic_bootstrap.py`, `bootstrap_schema`: create the schema if
missing; an empty schema or a known revision -> `alembic upgrade head`; a
revision from the archived chain -> refuse with the convert command;
tables but no `alembic_version` -> refuse (nothing can tell which state
they are in). There is no `create_all` path.

---

## 4. Configuration

From `services/auth-service/config.py` unless noted. Set them in
`.env.docker` (names listed in `.env.example`).

| Setting | Default | Meaning |
|---|---|---|
| `JWT_SECRET` | `CHANGE_ME` (`shared/config.py`) | HS256 signing secret; at least 32 characters outside development/test |
| `JWT_ALGORITHM` | `HS256` (`shared/config.py`) | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | access token life |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | refresh token and session life |
| `EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` | `24` | |
| `PASSWORD_RESET_TOKEN_EXPIRE_HOURS` | `1` | |
| `OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES` | `5` | |
| `TWO_FACTOR_RECENT_LOGIN_MINUTES` | `10` | `/2fa/setup` and `/2fa/verify` need a login (`auth_time`) at most this old, else 403 `recent_login_required` |
| `UNVERIFIED_ACCOUNT_RETENTION_DAYS` | `7` | the nightly `purge-unverified` job deletes accounts registered longer ago that never verified their email, never signed in and have no OAuth link |
| `ORGANIZATION_INVITATION_EXPIRE_DAYS` | `7` | |
| `PASSWORD_MIN_LENGTH` | `10` | (max 72 bytes is fixed in `security.PASSWORD_MAX_BYTES`) |
| `TOTP_ISSUER` | `AlphaMind` | name shown in authenticator apps |
| `TOTP_ENCRYPTION_KEY` | empty | Fernet key that encrypts stored TOTP secrets; must be a valid Fernet key when set; required (startup refuses) when `APP_ENV` is not development/test; when empty, a key derived from `JWT_SECRET` is used |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | empty | OAuth apps |
| `OAUTH_REDIRECT_BASE_URL` | `https://localhost:8443` | base of the callback URL registered with providers |
| `FRONTEND_BASE_URL` | `http://localhost:3000` (`shared/config.py`) | base of links in emails |
| `AVATAR_STORAGE_DIR` | `/app/data/avatars` | volume `avatar_data` |
| `AVATAR_MAX_SIZE_MB` | `5` | |
| `AVATAR_ALLOWED_CONTENT_TYPES` | png, jpeg, webp, gif | |
| `PRESENCE_ONLINE_THRESHOLD_MINUTES` | `5` | |
| `RATE_LIMIT_ENABLED` | `true` | all auth rate limits |
| `RATE_LIMIT_LOGIN_PER_EMAIL` / `_PER_IP` / `_WINDOW_SECONDS` | `10` / `30` / `900` | |
| `RATE_LIMIT_2FA_FAILURES` / `_WINDOW_SECONDS` / `_LOCK_SECONDS` | `5` / `300` / `900` | |
| `RATE_LIMIT_PASSWORD_RESET_PER_EMAIL` / `_PER_IP` / `_WINDOW_SECONDS` | `3` / `10` / `3600` | |
| `RATE_LIMIT_REGISTER_PER_IP` / `_WINDOW_SECONDS` | `10` / `3600` | |
| `RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL` / `_WINDOW_SECONDS` | `3` / `3600` | |
| `RATE_LIMIT_INVITATIONS_PER_ORG` / `_WINDOW_SECONDS` | `20` / `3600` | |
| `RATE_LIMIT_FRIEND_REQUESTS_PER_USER` / `_WINDOW_SECONDS` | `30` / `3600` | connection requests per sender |
| `USER_STATE_CHECK_ENABLED` | `true` (`shared/config.py`) | live role/active check on every request |
| `USER_STATE_CACHE_TTL_SECONDS` | `300` (`shared/config.py`) | cache of a state fetched after a Redis miss |
| `REDIS_DB_CACHE` / `_RATE_LIMIT` / `_EVENTS` | `0` / `1` / `2` (`shared/config.py`) | OAuth state and 2FA-challenge use in 0, limits in 1, events and user_state in 2 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, `SMTP_USE_TLS`, ... | `mailhog`, `1025`, `noreply@alphamind.local`, `false` (`shared/config.py`) | email (chapter 28) |
| `INTERNAL_API_TOKEN` | empty (`shared/config.py`) | `/internal/*` secret |
| `BACKEND_CORS_ORIGINS` | `http://localhost:3000` | CORS |

---

## 5. Design decisions and why

**Stateless access tokens plus a live state check.**
*Alternative:* look up every access token in a database table, or accept
up to 15 minutes of stale roles.
*Reason:* a JWT lets core-service and ai-service authenticate without
calling auth-service. The Redis mirror closes the one real gap
(demotion and deactivation must apply at once) for the cost of one Redis
`GET` per request.

**Refresh tokens hashed, rotated, single use, with reuse detection.**
*Alternative:* store the refresh token in plain text and allow reuse
until expiry.
*Reason:* a stolen database does not give working tokens; a stolen token
has a short useful life; reuse of an old token is a strong theft signal.

**Reuse means "rotated token came back", not "any revoked token came
back".**
*Alternative:* any revoked token triggers "log out everywhere".
*Reason:* with that rule, a second browser tab that still held a
logged-out token would log the user out on every device the next time it
tried to refresh. The `revoked_reason` column (migration `f1190b000001`)
lets the service tell a theft signal apart from an ordinary stale tab.

**Emails sent after the commit, in background tasks.**
*Alternative:* send inside the request.
*Reason:* sending inside the request turns SMTP failures into 500s for
registrations and invitations, holds a database connection while SMTP
hangs, and mails links to rows that a failed commit never stored.

**Same answer for unknown and known emails** (login timing, reset,
resend). *Reason:* prevents account enumeration.

**OAuth links by email only when both sides verified it.**
*Reason:* otherwise an attacker with an unverified provider email could
take over a local account.

**OAuth sign-in returns a 2FA challenge for 2FA accounts.**
*Reason:* social login must not bypass 2FA.

**Per-user 2FA lockout and no-replay TOTP steps.**
*Reason:* a six-digit code can be brute-forced without a lockout, and a
shoulder-surfed code must not work twice.

**Soft delete instead of a real delete.**
*Reason:* core and ai keep rows that reference user ids (portfolios,
analyses). The personal data goes, the id stays, and the Redis mirror
keeps the id blocked.

**First admin by command-line script.**
*Alternative:* "the first registered user is admin", or a setup endpoint.
*Reason:* no window in which a stranger can register first and become
admin; the operator already has shell access to the container.

**Rate limits fail open.**
*Reason:* a Redis outage must not lock every user out of login. The
gateway limit still stands in front.

**Avatars on a local volume, documents in object storage.**
*Reason:* avatars are small, rarely written and read only by this service.

**OAuth sign-ins are bound to the browser with a nonce cookie, plus
PKCE.**
*Alternative:* rely on the server-side `state` alone.
*Reason:* the `state` proves the server started a sign-in, but not that
this browser started it, so an attacker's own callback link would sign a
victim into the attacker's account (login CSRF). An HttpOnly nonce cookie
whose hash is stored with the state closes that gap without a frontend
session; PKCE makes a leaked authorization code useless. The callback
stays a JSON route.

**Invitation tokens travel in the request body.**
*Alternative:* `POST /invitations/{token}/accept`.
*Reason:* paths are written to the gateway access log and to error logs;
a body is not.

---

## 6. When things go wrong

| Symptom | Meaning / cause | How to diagnose |
|---|---|---|
| login 401 `email_not_verified` | account not verified | find the email in MailHog (`http://127.0.0.1:8025`) or call `/verify-email/resend` |
| login 401 `two_factor_required` | the account has 2FA; send `totp_code` | |
| `/2fa/setup` or `/2fa/verify` 403 `recent_login_required` | the session's login (`auth_time`) is more than 10 minutes old, or the token predates the claim; a refresh does not help | log in again (password or OAuth), then retry |
| an account the user registered a week ago is gone ("Email/password combination is incorrect.") | it was never verified, so the nightly `purge-unverified` job deleted it; log `unverified_accounts_purged` | register again with the same email |
| 429 `rate_limited` with `Retry-After` | auth-service limit | `bash scripts/redis-cli.sh -n 1 --scan --pattern 'auth:rl:*'` shows active counters (hashed identities) |
| 429 "Too many invalid codes" | 2FA lock (5 failures in 5 minutes, locked 15 minutes) | wait for `Retry-After` |
| refresh 401 and the user is logged out on every device | reuse detection fired | log event `refresh_token_reuse_detected` (`bash scripts/logs.sh auth-service grep refresh_token_reuse_detected`); sessions show `revoked_reason = 'reuse'` |
| a demoted admin still has admin rights | the mirror was not written | `bash scripts/redis-cli.sh -n 2 GET auth:user_state:<id>`; logs `user_state.mirror_failed`, `user_state.startup_sync_failed`; core/ai logs `user_state.redis_read_failed`, `user_state.auth_lookup_failed` |
| verification or reset email never arrives | SMTP problem; the request still succeeded | log events `verification_email_failed`, `password_reset_email_failed`, `invitation_email_failed`, `email_send_failed`; MailHog UI |
| OAuth callback 401 "Invalid or expired OAuth state" | the state is older than 300 s, was already used, or was for another provider | start again from `/oauth/<provider>/authorize` |
| OAuth callback 401 "This sign-in was not started in this browser" (log `oauth_browser_binding_failed`) | the `alphamind_oauth_nonce` cookie is missing or belongs to another sign-in: the authorize step was called with `fetch` instead of a navigation, the callback was opened in another browser, cookies are blocked, or a second sign-in started in the same browser | open `/oauth/<provider>/authorize` as a top-level navigation and finish in the same browser within 5 minutes |
| OAuth 409 `oauth_link_refused` | a local account with that email exists but one side is unverified | log in with the password first |
| OAuth 502 `oauth_provider_error` | provider unreachable or odd answer | log events `oauth_provider_http_error`, `oauth_github_emails_unavailable` |
| auth-service does not start, log says the schema was refused | old migration chain or tables without `alembic_version` | follow the command in the log (`python -m scripts.alembic_convert`) after a backup |
| auth-service refuses to start: "JWT_SECRET must be set..." | weak secret with `APP_ENV` not development/test | set a random secret of 32+ characters |
| 2FA login answers 409 "Your 2FA settings cannot be read..." | the stored secret cannot be decrypted with `TOTP_ENCRYPTION_KEY` or the `JWT_SECRET`-derived key (both changed, or a damaged row); log `totp_secret_unreadable` with `user_id` | an admin resets the user's 2FA: `DELETE /api/v1/admin/users/{id}/2fa`; the user then sets it up again |
| auth-service refuses to start: "TOTP_ENCRYPTION_KEY must be set..." or "...is not a valid Fernet key" | key missing with `APP_ENV` not development/test, or malformed | generate one: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `/health/auth` 503 | database or Redis unreachable | the JSON body says which (`"database": false` or `"redis": false`) |
| presence stays "online" | `last_seen_at` within 5 minutes; heartbeats from an open socket | `bash scripts/psql.sh -c "SELECT username, last_seen_at FROM auth.users ORDER BY last_seen_at DESC NULLS LAST LIMIT 5;"` |
| many old rows in `auth.user_sessions` | rows are purged per user only at that user's next login/refresh (expired more than 7 days) | normal; no global purge job exists |

---

## 7. Demo walkthrough

**This demo writes data.** It creates one demo user and a few session
rows. It does not change any existing user. Commands run on the host from
the repository root. Pick a unique email each time (registration is
limited to 10 per client address per hour, login to 30 per address per 15
minutes).

Set two shell variables first:

```
BASE=https://localhost:8443
EMAIL=demo-$(date +%s)@example.com
```

**Step 1: register through the gateway.**

```
curl -k -sS -X POST $BASE/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"username\": \"demo_$(date +%s)\", \"password\": \"correct-horse-42\"}"
```

Expected: `{"data": {"message": "Registered. Check your email to verify
your account."}, ...}` with HTTP 201. Say: "The password is hashed with
bcrypt, the user gets the role `user`, and the email is sent after the
commit."

Try logging in now to show the rule:

```
curl -k -sS -X POST $BASE/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"password\": \"correct-horse-42\"}"
```

Expected: 401 with `"code": "email_not_verified"`.

**Step 2: read the verification email in MailHog.** Open
`http://127.0.0.1:8025` in a browser. The newest message is "Verify your
AlphaMind account" to the demo address. The link is
`http://localhost:3000/verify-email?token=...` (the frontend URL). Copy the
`token` value. Or extract it with the helper the end-to-end script uses
(it decodes the email body):

```
VTOKEN=$(docker compose --env-file .env.docker exec -T core-service python -c "
import httpx
from scripts.e2e_check import token_from_mailhog
r = httpx.get('http://mailhog:8025/api/v2/search', params={'kind': 'to', 'query': '$EMAIL', 'limit': 50})
print(token_from_mailhog(r.json(), '/verify-email'))")
echo "$VTOKEN" | cut -c1-40
```

**Step 3: verify.**

```
curl -k -sS -X POST $BASE/api/v1/auth/verify-email \
  -H 'Content-Type: application/json' -d "{\"token\": \"$VTOKEN\"}"
```

Expected: `"message": "Email verified."`.

**Step 4: log in and call `/me`.**

```
json() { python3 -c "import sys, json; print(json.load(sys.stdin)['data']['$1'])"; }
LOGIN=$(curl -k -sS -X POST $BASE/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"password\": \"correct-horse-42\"}")
ACCESS=$(echo "$LOGIN" | json access_token)
R1=$(echo "$LOGIN" | json refresh_token)
curl -k -sS $BASE/api/v1/auth/me -H "Authorization: Bearer $ACCESS" | python3 -m json.tool
USER_ID=$(curl -k -sS $BASE/api/v1/auth/me -H "Authorization: Bearer $ACCESS" | json id)
```

Point out `system_role: "user"`, the `permissions` list, `is_verified:
true`, and the default DiceBear avatar URL. Decode the access token payload
to show its claims (no secret needed to *read* a JWT, only to *sign* it):

```
echo "$ACCESS" | cut -d. -f2 | python3 -c "import sys, base64, json; s = sys.stdin.read().strip(); print(json.dumps(json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))), indent=2))"
```

The audience sees `sub`, `type: "access"`, `iat`, `exp` (15 minutes
later), `jti` and `role`.

**Step 5: the live role mirror.**

```
bash scripts/redis-cli.sh -n 2 GET "auth:user_state:$USER_ID"
```

Expected: `{"role": "user", "is_active": true}`. Say: "Every service reads
this on every request; it is how a demotion applies instantly."

**Step 6: refresh (rotation), logout, and the "only a 401" case.**

```
R2=$(curl -k -sS -X POST $BASE/api/v1/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\": \"$R1\"}" | json refresh_token)
curl -k -sS -o /dev/null -w 'logout: %{http_code}\n' -X POST $BASE/api/v1/auth/logout \
  -H 'Content-Type: application/json' -d "{\"refresh_token\": \"$R2\"}"
curl -k -sS -o /dev/null -w 'refresh with logged-out token: %{http_code}\n' \
  -X POST $BASE/api/v1/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\": \"$R2\"}"
```

Expected: logout `204`, then `401`. Say: "R1 was used up by the refresh.
R2 was logged out. Presenting R2 again is just refused; it does not log
the user out elsewhere."

**Step 7: reuse detection.** This revokes every active session of the
demo user.

```
R3=$(curl -k -sS -X POST $BASE/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\": \"$EMAIL\", \"password\": \"correct-horse-42\"}" | json refresh_token)
R4=$(curl -k -sS -X POST $BASE/api/v1/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\": \"$R3\"}" | json refresh_token)
curl -k -sS -X POST $BASE/api/v1/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\": \"$R3\"}"
curl -k -sS -o /dev/null -w 'R4 after reuse: %{http_code}\n' -X POST $BASE/api/v1/auth/refresh \
  -H 'Content-Type: application/json' -d "{\"refresh_token\": \"$R4\"}"
```

Expected: the reused R3 gets 401 "Refresh token is invalid or expired.",
and the perfectly valid R4 is now refused too (401). Show the log line:

```
docker compose --env-file .env.docker logs --since 2m auth-service | grep refresh_token_reuse_detected
```

**Step 8: the session rows.**

```
bash scripts/psql.sh -c "SELECT s.created_at, s.revoked_at IS NOT NULL AS revoked, s.revoked_reason, left(s.refresh_token_hash, 12) AS hash_prefix, s.ip_address, left(s.device_info, 20) AS device FROM auth.user_sessions s JOIN auth.users u ON u.id = s.user_id WHERE u.email = '$EMAIL' ORDER BY s.created_at;"
```

Expected, in order: `rotated` (R1), `logout` (R2), `rotated` (R3),
`reuse` (R4). Point out that only a SHA-256 prefix of the token is stored
and that `ip_address` is the real client address forwarded by the gateway.

**Step 9: an admin token with `scripts/token.sh`.**

```
TOKEN=$(bash scripts/token.sh admin)
curl -k -sS $BASE/api/v1/admin/stats -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -k -sS -o /dev/null -w 'demo user on admin route: %{http_code}\n' \
  $BASE/api/v1/admin/stats -H "Authorization: Bearer $ACCESS"
```

Expected: the stats JSON (`auth.users_total`, `users_by_role`, plus
`core` and `ai` counts) for the admin token, and `403` for the demo user.
Explain how `token.sh` works: it runs `security._create_token(...)` inside
the auth-service container, so the token is signed with the real
`JWT_SECRET`, lives 2 hours (`HOURS=4 bash scripts/token.sh admin` for
longer), and has no session row (it cannot be refreshed). The `admin`
shortcut uses `ADMIN_USER_ID` from `scripts/lib.sh`.

Then show that the live role beats the token's claim:

```
FAKE=$(bash scripts/token.sh "$USER_ID" admin)
curl -k -sS -o /dev/null -w 'demo user with role=admin claim: %{http_code}\n' \
  $BASE/api/v1/admin/stats -H "Authorization: Bearer $FAKE"
```

Expected: `403`. The token *claims* `role: admin`, but `shared/security.py`
replaces it with the live role `user` from Redis. Say: "Signing a token is
not enough to become admin; the role lives in auth-service."

**Step 10 (optional): the first-admin script, dry run only.**

```
docker compose --env-file .env.docker exec -T auth-service \
  python -m scripts.set_system_role --email "$EMAIL" --role moderator --dry-run
```

Expected: `[dry run] would change <email> (<id>): 'user' -> 'moderator'.`
Nothing is written. (Without `--dry-run` it would really change the role,
in the database and in Redis.)

---

## 8. Check your understanding

1. Why can core-service check an access token without calling
   auth-service?
2. What exactly is stored in `user_sessions.refresh_token_hash`, and why
   not the token?
3. A user refreshes with token R and gets R'. Later R is presented again.
   What happens, and why is the commit done inside the error path?
4. The same user logs out with R' and later a second tab presents R'.
   What happens this time?
5. How does a password-reset link become single-use without a "used
   tokens" table?
6. An admin demotes a moderator while the moderator holds a valid access
   token with `role: moderator`. When does the demotion take effect, and
   through which mechanism?
7. What happens to authentication in core-service if Redis and
   auth-service are both down?
8. Why is the TOTP step stored after each successful check?
9. Under which conditions does an OAuth sign-in link to an existing local
   account?
10. How is the first admin created, and why not through the API?

<details>
<summary>Answers</summary>

1. Tokens are HS256 JWTs signed with `JWT_SECRET`, which every service
   has; signature, expiry and `type` are checked locally. Only the live
   role/active state is read from Redis.
2. `sha256(refresh_token)` in hex. A database leak then does not give
   usable refresh tokens; lookup is still one indexed equality query.
3. R's session has `revoked_reason = 'rotated'`, so it counts as reuse:
   every still-active session of the user is revoked with reason `reuse`,
   and the caller gets 401. The 401 rolls back the request transaction, so
   the revocation is committed explicitly before raising.
4. Only a 401. The session's reason is `logout`, which is not a theft
   signal, so nothing else is revoked.
5. The token carries `pwd`, a fingerprint of the password hash at issue
   time; after a reset the hash changes and every older token fails the
   comparison.
6. On the next request. `commit_with_state` writes the new role to
   `auth:user_state:<id>` before the request returns, and
   `shared/security.py` replaces the token's role with the live role.
7. `get_user_state` returns None and the token's own claims are used
   (logged), so users are not locked out.
8. So the same code (or an older one in the +/- 1 step window) cannot be
   replayed; the update only succeeds for a larger step.
9. When an `oauth_accounts` row already exists for that provider identity,
   or when the provider says the email is verified **and** the local
   account is verified.
10. Register and verify normally, then run `python -m
    scripts.set_system_role --email ... --role admin` inside the
    auth-service container. Every admin API needs an admin, and a public
    "become admin" route would be a takeover risk.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `services/auth-service/main.py` | app, lifespan (schema, user-state sync, avatar dir), routers, `/health`, avatar static mount |
| `services/auth-service/config.py` | all auth settings (token lifetimes, OAuth, avatars, presence, rate limits) |
| `services/auth-service/security.py` | bcrypt, JWT creation and decoding, token types, TOTP helpers, encrypt/decrypt stored TOTP secrets (Fernet), refresh-token hashing |
| `services/auth-service/rate_limit.py` | `hit`, 2FA lockout, `client_ip`, 429 handler |
| `services/auth-service/events_out.py` | after-commit event publishing, payload builders, membership cache invalidation |
| `services/auth-service/user_state_sync.py` | writes the live role/active mirror (`commit_with_state`, startup sync) |
| `services/auth-service/deps.py` | `get_current_user`, `require_recent_login` (2FA setup/verify) and permission-checked dependencies |
| `services/auth-service/database.py`, `db_bootstrap.py` | engine, sessions, Alembic bootstrap |
| `services/auth-service/api/internal.py` | `/internal/*` routes for core and ai, and the scheduler's `purge-unverified` |
| `services/auth-service/domains/auth/model.py` | users, profiles, settings, sessions (`SessionRevokeReason`), OAuth accounts, 2FA secrets |
| `services/auth-service/domains/auth/schema.py` | request/response models, password and username rules |
| `services/auth-service/domains/auth/repository.py` | data access, atomic rotation claim, session purge, TOTP step consume |
| `services/auth-service/domains/auth/service.py` | registration, verification, login, refresh, logout, 2FA, OAuth, reset, presence, `/me`; avatar files (magic-byte type check, safe file delete) |
| `services/auth-service/domains/auth/api.py` | `/api/v1/auth/*` routes, avatar upload |
| `services/auth-service/domains/organization/*` | organizations, members, invitations, owner successor |
| `services/auth-service/domains/social/*` | connections (friends), block |
| `services/auth-service/domains/admin/*` | user administration, site statistics |
| `services/auth-service/scripts/set_system_role.py` | set a user's system role (first admin) |
| `services/auth-service/scripts/alembic_convert.py` | one-time conversion from the archived migration chain |
| `services/auth-service/migrations/` | Alembic `env.py`, `versions/` (`f1160b000001` baseline -> `f1190b000001` `user_sessions.revoked_reason` -> `f1210b000001` drops `organization_roles`; head `f1210b000001`), `archive/` (until the conversion tooling is deleted) |
| `shared/security.py` | access-token check and live state on every service, permission dependencies, internal-token guard |
| `shared/user_state.py` | the Redis mirror key format and reader |
| `shared/permissions.py`, `shared/enums.py` | roles and permissions |
| `shared/rate_limit.py` | the shared fixed-window counter |
| `shared/email.py` | email templates and SMTP sender |
| `scripts/token.sh`, `scripts/lib.sh` | operator access tokens |

---

## 10. Related chapters

- [10-gateway.md](10-gateway.md): the gateway rate limit and the forwarded
  client address.
- [12-core-service.md](12-core-service.md): how core-service uses tokens,
  roles, memberships, presence and auth events.
- [23-redis.md](23-redis.md): the Redis databases and key families.
- [24-events.md](24-events.md): pub/sub and durable events between
  services.
- [28-email.md](28-email.md): SMTP, MailHog and the email templates.
- [29-security.md](29-security.md): the full security model.
- [30-shared-library.md](30-shared-library.md): `shared/security.py`,
  `shared/user_state.py`, `shared/permissions.py`.
- [../API_SPEC.md](../API_SPEC.md) section 2 and [../ERD.md](../ERD.md)
  part 1 (the `auth` schema).
