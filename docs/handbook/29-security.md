# 29. Security

> This chapter follows one request from the browser to the database and
> names every security control on the way: TLS and security headers at the
> gateway, JWT access and refresh tokens, password hashing, two-factor
> codes, OAuth, the live role check, the permission map, ownership rules,
> the internal-token wall between services, rate limits, API keys, input
> validation, CORS, secrets handling and container exposure. For each
> control it says where the code is, what it protects against, and where
> its limits are. The goal is that you can explain the whole security
> model to an evaluator and show each part live.

---

## 1. The big picture

```
 Internet / your laptop
        |
        |  HTTPS only (TLS 1.2/1.3); http://:8000 -> 301 to https://:8443
        v
 +-------------------------------- gateway (nginx) -----------------------------+
 |  security headers (HSTS, nosniff, X-Frame-Options, Referrer-Policy, CSP)      |
 |  /api/v1/auth: 10 req/s per address, burst 20 -> 429                          |
 |  /internal/*, /metrics, unknown paths -> 404 (never proxied)                  |
 |  X-Forwarded-For replaced with the real peer address                          |
 +-------------------------------------------------------------------------------+
        |  plain HTTP on the private Docker network
        v
 +------------------+   +------------------+   +------------------+
 | auth-service     |   | core-service     |   | ai-service       |
 |  issues JWTs     |   |  checks JWTs     |   |  checks JWTs     |
 |  bcrypt, 2FA,    |   |  API keys        |   |  RAG rate limit  |
 |  OAuth, limits   |   |  public API      |   |  owner checks    |
 +------------------+   +------------------+   +------------------+
        |   every authenticated request:                     |
        |   1. signature + expiry + type of the JWT          |
        |   2. live role/active flag (Redis mirror)          |
        |   3. permission for the role (shared/permissions)  |
        |   4. ownership / organization membership           |
        v                                                    v
   PostgreSQL, Redis, Qdrant, SeaweedFS: no host ports except
   Qdrant on 127.0.0.1:6333; Redis and Qdrant have no password.

 service -> service: /internal/* + X-Internal-Token (constant-time compare)
```

The model in one paragraph: **only the gateway is public**. It speaks
HTTPS and refuses internal paths. Behind it, every user request carries
a short-lived **access token** (a JWT) issued by auth-service. Each
service checks the token itself, then checks the user's **live** role and
active flag, then the **permission** for that role, then **ownership** of
the thing being touched. Services talk to each other on `/internal/*`
routes that require a shared secret header. Data stores sit on the
private Docker network.

---

## 2. Concepts you need

| Term | Meaning |
|---|---|
| TLS | Transport Layer Security, the encryption behind `https://`. The gateway terminates TLS: it decrypts, then forwards plain HTTP inside the Docker network. |
| HSTS | `Strict-Transport-Security` header: tells a browser to use HTTPS only for this site for a time. |
| CSP | Content Security Policy header: tells a browser which scripts, styles and connections a page may use; blocks injected scripts. |
| JWT | JSON Web Token: `header.payload.signature`, each part base64url text. The payload (claims such as `sub`, `exp`) is **readable by anyone**; the signature proves the server made it. AlphaMind signs with HS256 (HMAC-SHA256 and a shared secret, `JWT_SECRET`). |
| Access token | A JWT with `type: "access"`, valid 15 minutes, sent as `Authorization: Bearer <token>`. |
| Refresh token | A JWT with `type: "refresh"`, valid 30 days, used once to get a new token pair. |
| Rotation | Every refresh revokes the used refresh token and issues a new one. |
| Reuse detection | If a refresh token that was already rotated is presented again, someone may have stolen it; all of that user's sessions are revoked. |
| bcrypt | A deliberately slow password hashing function with a salt. A stolen hash is expensive to crack. |
| TOTP / 2FA | Time-based one-time password (the 6-digit code in an authenticator app); a second factor after the password. |
| OAuth `state` | A random value sent to Google/GitHub and checked when the user comes back. It proves the server started this sign-in; on its own it does not prove that the same browser did. |
| Login CSRF | An attacker makes the victim's browser finish the attacker's own sign-in, so the victim works inside the attacker's account. Stopped by binding the sign-in to the browser (a nonce cookie checked at the callback). |
| PKCE | Proof Key for Code Exchange: the client sends a hash of a secret verifier with the authorize request and the verifier itself with the token request, so a stolen authorization code cannot be redeemed. |
| Permission | A named right such as `portfolio:write`. Roles map to sets of permissions in `shared/permissions.py`. |
| Fail open / fail closed | When a check cannot run (its store is down), fail open = allow, fail closed = refuse. AlphaMind chooses per control (section 5). |
| Fixed-window rate limit | Count requests per key in a time window that starts at the first request; refuse above a limit with HTTP 429. |
| Constant-time compare | `hmac.compare_digest`: compares two secrets in a time that does not depend on where they differ, so timing does not leak the secret. |

---

## 3. How it works in AlphaMind

### 3.1 TLS at the gateway

`infra/gateway/nginx.conf`:

- `listen 443 ssl` with the certificate from `infra/gateway/gen-certs.sh`
  (`make certs`, run by `make up`): self-signed, RSA 2048, valid 825
  days, for `DNS:localhost` and `IP:127.0.0.1`. Files live in
  `infra/gateway/certs/` and are gitignored (only the README is tracked).
- `ssl_protocols TLSv1.2 TLSv1.3`, the Mozilla "intermediate" cipher list
  for TLS 1.2, `ssl_session_tickets off`.
- Port 80 (host 8000) only answers `301` to `https://<host>:8443`. A plain
  HTTP request sent to the HTTPS port (nginx status 497) is redirected
  too.
- `server_tokens off`: no nginx version in headers or error pages.
- The access log uses the `main_noargs` format, which logs `$uri` and
  never the query string. The WebSocket URL carries the JWT as
  `?token=...` and the OAuth callback carries `?code=...&state=...`, so
  the default `combined` format would write these secrets into the
  container logs (and through Vector into `./logs`). nginx's error log
  still prints the raw request line when an upstream fails; that rarer
  path is accepted.
- The services behind the gateway log the same URLs: uvicorn's access
  line (`uvicorn.access`) contains the path with its query string, and
  its WebSocket line `'<client> - "WebSocket <path>" [accepted]'`
  (`uvicorn.error`, INFO) does too. `configure_logging` in
  `shared/logging.py` therefore installs `QueryStringFilter` on both
  loggers in every service: any log argument that is a path with a query
  string is rewritten to `<path>?[redacted]`. This covers core-service's
  `/ws/...?token=<jwt>` and auth-service's OAuth callback, and needs no
  change to the uvicorn command line (`--no-access-log` would not cover
  the WebSocket line).
- No route carries a secret in its **path**, because paths are logged
  everywhere (gateway `$uri`, uvicorn, `request.url.path` in
  `shared/exceptions.py`). The organization invitation token is posted in
  the body of `POST /api/v1/organizations/invitations/accept`.
- `absolute_redirect off`: redirects that nginx makes itself (for example
  a missing trailing slash) stay relative. An absolute redirect would use
  the container's port 443, not the published 8443, and send the browser
  nowhere. For the same reason the `Host` header is forwarded with the
  external port (`$proxy_host_header`), so URLs the backends build
  (FastAPI slash redirects, Grafana's origin check) carry `:8443`.

Behind the gateway the services speak plain HTTP on the Docker network.
uvicorn runs with `--proxy-headers --forwarded-allow-ips=*` (each
service's `Dockerfile` and `docker-compose.override.yml`), so
`request.client.host` is the address the gateway reports
(`services/auth-service/rate_limit.py::client_ip` reads it). The gateway
**replaces** `X-Forwarded-For` with `$remote_addr` instead of appending.
uvicorn takes the left-most entry, so with an appended header a client
could choose its own address and so dodge, or aim at someone else, every
per-address rate limit. A location that sets any `proxy_set_header` of
its own (`/ws`, `/grafana/`) loses the inherited ones, so those locations
repeat all of them, including this replacement.

For production you replace the self-signed certificate with a real one
(for example from Let's Encrypt).

### 3.2 Security headers and CSP

Also in `infra/gateway/nginx.conf`, all set once at `http` level (any
`add_header` in a `server` or `location` block would silently drop the
inherited ones):

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000` (one year). Browsers ignore it over a certificate error, so the self-signed development certificate does not lock `localhost` to HTTPS. |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY`, except `SAMEORIGIN` under `/grafana` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | Only for the static status page (`/status` and `/status.html`; `try_files` rewrites the URI before the headers are sent, so both spellings are matched): `default-src 'none'; script-src 'sha256-...'; style-src 'sha256-...'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. Empty (not sent) elsewhere: the API returns JSON, and Swagger and Grafana ship their own inline scripts. |

The two hashes must match the exact `<script>` and `<style>` bodies in
`infra/gateway/status.html`. `python3 infra/gateway/csp_hashes.py --write`
updates them; the stack smoke CI job runs `--check`.

`proxy_hide_header` (set at `http` level and inherited by every location,
repeated in `/grafana/` for clarity) removes the upstreams' own copies of
`X-Frame-Options`, `X-Content-Type-Options` and
`Strict-Transport-Security`. So for these three headers only the
gateway's value reaches the browser, never a duplicated, conflicting
pair. HSTS is ignored by browsers on plain HTTP and over a certificate
error (RFC 6797 section 8.1), so the self-signed development certificate
does not pin `localhost` to HTTPS unless you trusted it explicitly.

### 3.3 Which paths the gateway serves

Anything not listed in `nginx.conf` returns 404 (`location / { return
404; }`). Explicit 404 blocks exist for `/internal/`, `/internal` and
`/metrics`. Public paths: `/api/v1/...` per service, `/ws`, `/grafana/`
(Grafana's own login; anonymous access off), `/status`, `/health/{auth,
core,ai}`, the Swagger pages `/docs/{auth,core,ai}/` and their OpenAPI
JSON files, and `/static/avatars`.

### 3.4 JWT access and refresh tokens

**Issuing** (auth-service, `services/auth-service/security.py`):
`_create_token(subject, expires_delta, token_type, extra_claims)` builds
`{"sub", "type", "iat", "exp", "jti"}` plus extras and signs it with
`JWT_SECRET` and `JWT_ALGORITHM` (HS256) using python-jose.

- `create_access_token(user_id, {"role": system_role, "auth_time": t})`:
  15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`).
- `create_refresh_token(user_id, auth_time)`: 30 days
  (`REFRESH_TOKEN_EXPIRE_DAYS`), with the same `auth_time` claim.

`auth_time` is the Unix time of the login that started the session: set
to "now" by a password login, an OAuth callback and the OAuth 2FA step,
and copied unchanged from the presented refresh token by a refresh
(`security.refresh_token_auth_time`). `iat` cannot serve this purpose,
because every refresh renews it. Every service reads the claim into
`CurrentUser.auth_time` (`shared/security.py`); auth-service uses it for
the recent-login rule (section 3.6).

`AuthService._issue_token_pair` stores a `user_sessions` row with the
**SHA-256 hash** of the refresh token (`hash_refresh_token`), never the
token itself, plus IP address and user agent (for password logins,
OAuth sign-ins and the OAuth 2FA step alike; a refresh copies them to
the new session).

**Checking** (every service, `shared/security.py`):

1. `HTTPBearer(auto_error=False)` reads the header; no header -> 401
   `Missing bearer token`.
2. `decode_token` verifies signature and expiry with `JWT_SECRET`; any
   failure -> 401 `Invalid or expired token`.
3. `_current_user_from_payload` requires `type == "access"` (a refresh or
   reset token is refused: `Wrong token type`) and requires `sub` to be a
   UUID string (`is_uuid_claim`; otherwise 401 `Invalid token subject`).
   Every token this system issues uses user or invitation ids, so
   anything else is a hand-made or foreign token; refusing it here gives
   a 401 instead of a later 500. A token without a `role` claim is
   treated as `user`. Access tokens carry no organization claim:
   organization rights are always looked up (section 3.9).
4. If `USER_STATE_CHECK_ENABLED`, the live state replaces the token's role
   (section 3.8).
5. Returns `CurrentUser(user_id, role)` with the id converted to
   `uuid.UUID` once. Claims are strings on the wire, and in Python a
   `UUID` never equals a `str`; converting at this single construction
   point makes every `owner_id == current_user.user_id` comparison in
   every service compare UUIDs (without it every ownership check would
   silently fail).
6. The answer body of every failure above is FastAPI's plain shape
   `{"detail": "..."}`, and so is a missing permission (403 `Insufficient
   permissions`, section 3.9). Ownership and business errors raised by the
   services use the project envelope
   `{"error": {"code": "permission_denied" | "not_found" | ..., "message",
   "request_id"}}` (`shared/exceptions.py`). So a 403 can come in either
   shape, and a client must accept both.

Each service builds its dependency with
`get_current_user = make_get_current_user(settings)` in its `deps.py`.

**Refresh rotation and reuse detection** (`AuthService.refresh`,
`UserRepository.claim_session_for_rotation`):

```
POST /api/v1/auth/refresh {"refresh_token": R}
  step 1, one atomic UPDATE ... RETURNING on user_sessions:
     find the row whose refresh_token_hash = sha256(R), that is not
     revoked and not expired; mark it revoked (reason 'rotated') and
     return it. Only one of two concurrent refreshes can win.
  row returned?  -> new access + refresh pair (new session row)
  no row:
    known session, revoked as 'rotated' (or no reason)?
        -> REUSE: revoke ALL sessions of that user (reason 'reuse'),
           commit, log refresh_token_reuse_detected, answer 401
    revoked by logout / "revoke all", expired, unknown?
        -> just 401
```

Logout (`POST /api/v1/auth/logout`) revokes only that session. A token
revoked by a logout does not trigger the "revoke everything" path, so a
second browser tab that still holds it cannot log the user out
everywhere.

Access tokens are not revocable one by one; they simply expire within 15
minutes. Deactivation and role changes still apply immediately through
the live state (3.7).

### 3.5 Password hashing

`services/auth-service/security.py` uses passlib:
`CryptContext(schemes=["bcrypt"], deprecated="auto")`.

- `hash_password` / `verify_password`.
- Passwords must be at least `PASSWORD_MIN_LENGTH` (10) characters and at
  most 72 bytes (`PASSWORD_MAX_BYTES`), checked by `validate_new_password`
  in `services/auth-service/domains/auth/schema.py`. bcrypt only uses the
  first 72 bytes; longer passwords would be silently truncated, so they
  are refused.
- Unknown email at login: `dummy_verify_password` runs a bcrypt check
  against a random hash, so the response time is the same as for a wrong
  password (no account enumeration by timing).

**Why `bcrypt<4.1` is pinned** (`pyproject.toml`): passlib 1.7.4 is no
longer maintained. bcrypt 4.1 removed the `__about__` attribute that
passlib reads to detect the version, and began raising an error for
inputs longer than 72 bytes, which breaks passlib's own self-test at
import time. The installed version is bcrypt 4.0.1, where hashing and
verifying work. The test configuration also silences passlib's warning
about Python's deprecated `crypt` module.

### 3.6 Two-factor authentication (TOTP)

Routes under `/api/v1/auth/2fa/`: `setup` returns a new secret and an
`otpauth://` URI (issuer `TOTP_ISSUER` = `AlphaMind`); `verify` enables
2FA with a valid code; `disable` needs a valid code. Setup on an account
that already has 2FA is refused (409).

**Recent login for setup and verify.** Both need a session whose login is
recent: `deps.require_recent_login` checks that the access token's
`auth_time` is at most `TWO_FACTOR_RECENT_LOGIN_MINUTES` (default 10)
minutes old (`security.is_recent_login`), otherwise **403**
`recent_login_required` ("This action needs a recent login. Log in again
(password or OAuth) and retry within the next few minutes."). It applies
to password and OAuth users alike; a token without `auth_time` (issued
before the rule) is refused. A refresh keeps the original `auth_time`, so
a session kept alive by refreshes, or a stolen access or refresh token,
cannot switch 2FA on with an attacker's secret unless the login itself
happened within the last 10 minutes. `disable` keeps needing a valid code
and is not covered by the rule.

`match_totp_step(secret, code)` accepts the current 30-second step and one
step either side, comparing with `hmac.compare_digest`. The step that
matched is stored (`two_factor_secrets.last_used_step`) and any step not
newer than it is refused, so a code cannot be replayed.

Brute force: `_check_totp` first checks a lock (`two_factor_guard`); each
wrong code is counted (`two_factor_failed`); after
`RATE_LIMIT_2FA_FAILURES` (5) failures within
`RATE_LIMIT_2FA_WINDOW_SECONDS` (300) the user is locked for
`RATE_LIMIT_2FA_LOCK_SECONDS` (900) and gets 429.

Login with 2FA enabled and no code returns 401 with code
`two_factor_required`; an unverified email returns 401
`email_not_verified`.

At rest, `two_factor_secrets.secret` is encrypted with Fernet
(`services/auth-service/security.py`; stored as `fernet:<token>`).
The key is `TOTP_ENCRYPTION_KEY`, or, when it is empty, a key derived
from `JWT_SECRET`; reading tries both. Outside development/test the
service refuses to start without a valid `TOTP_ENCRYPTION_KEY`. A
plain-text secret (no `fernet:` prefix) is accepted and is encrypted
after the user's next successful 2FA check. A stored value that no key can read (both keys changed)
gives a 409 asking the user to have an admin reset 2FA
(`DELETE /api/v1/admin/users/{id}/2fa`, `user:manage`); an admin cannot
reset their own 2FA this way (403), only disable it with a code.

### 3.7 OAuth (Google, GitHub)

`oauth_authorize_url` (a module function in `domains/auth/service.py`;
it needs no database session) creates three random values for one
sign-in and stores them in Redis for 300 seconds under
`oauth:state:<state>`, as JSON `{provider, nonce_hash, code_verifier}`:

- **`state`** goes to the provider and comes back in the callback URL.
  The callback reads and deletes the Redis value in one `GETDEL`, so a
  state works exactly once, and its provider must match the URL. This
  proves the server started the sign-in.
- **A nonce** goes to the browser only, in the cookie
  `alphamind_oauth_nonce` (`HttpOnly`, `Secure`, `SameSite=Lax`,
  `Path=/api/v1/auth/oauth`, `Max-Age=300`); Redis keeps only its
  SHA-256. The callback compares the hash of the cookie with the stored
  hash (`hmac.compare_digest`) and answers 401 `oauth_failed` when the
  cookie is missing or different (log `oauth_browser_binding_failed`).
  This proves **this browser** started the sign-in, which is what stops
  login CSRF: an attacker who sends a victim their own
  `callback?code=...&state=...` link cannot also plant their cookie in
  the victim's browser, so the victim is never signed in to the
  attacker's account. `Lax` (not `Strict`) is needed because the
  provider's redirect back is a cross-site top-level `GET`. A successful
  callback deletes the cookie. Known limit: there is one nonce cookie per
  browser, so when two sign-ins are started in two tabs within 5 minutes,
  the second overwrites the first tab's nonce and the first tab's
  callback gets 401 `oauth_failed`; the user simply starts the sign-in
  again (safe, never a wrong account).
- **A PKCE `code_verifier`** stays on the server. The authorize URL
  carries `code_challenge = BASE64URL(SHA-256(verifier))` with
  `code_challenge_method=S256`, and the token request sends the
  verifier, so an intercepted authorization code cannot be redeemed by
  anyone else. Google and GitHub both support PKCE with `S256`.

The callback still answers JSON (a token pair or a 2FA challenge). Other
rules in `oauth_callback` / `_resolve_oauth_user`:

- an existing local account is linked by email only if **both** the
  provider and the local account have verified that address
  (`OAuthAccountLinkError` otherwise);
- a new account from an unverified provider email gets a placeholder
  address `<provider>_<id>@users.alphamind.local`, so nobody can
  pre-claim someone else's address;
- an account with 2FA receives a challenge token (`type: "oauth_2fa"`,
  5 minutes) instead of tokens, exchanged with a TOTP code at
  `POST /api/v1/auth/oauth/2fa`; the challenge's `jti` is marked used in
  Redis so it works once;
- provider errors map to 401 (`oauth_failed`) or 502
  (`oauth_provider_error`), never a crash.

With the default empty `GOOGLE_CLIENT_ID` / `GITHUB_CLIENT_ID` the flow
fails at the provider. To use OAuth you register an app with each
provider. Its callback URL is
`<OAUTH_REDIRECT_BASE_URL>/api/v1/auth/oauth/{google,github}/callback`,
for example `https://localhost:8443/api/v1/auth/oauth/google/callback`.

### 3.8 The live role and active mirror

Problem: an access token carries the role it was issued with for up to
15 minutes. A demoted or deactivated user would keep their rights until
it expires.

Solution (`shared/user_state.py`, `services/auth-service/user_state_sync.py`):

- auth-service is the **only writer**. After every change of role, active
  flag or deletion it writes `auth:user_state:<user_id>` =
  `{"role": ..., "is_active": ...}` in the Redis **events** database
  (`REDIS_DB_EVENTS`, 2), with no expiry (auth is the writer, so the key
  is always current). At startup it writes every user
  (`mirror_all_users`: keyset pagination by id, one Redis pipeline per
  batch of 500; a failure is logged `user_state.startup_sync_failed` and
  is not fatal, because readers fall back to auth-service on a miss). A
  deleted user is stored as inactive, never removed, so the entry keeps
  blocking that user's old tokens.
- `mirror_user` (after a role, active or delete change) lets errors
  propagate: a deactivation that could not be mirrored must not be
  reported as done. `mirror_user_safely` (after a user is created, by
  registration or OAuth) only logs `user_state.mirror_failed`, because a
  reader that misses the key asks auth-service anyway.
- Admin changes use `commit_with_state`: flush, mirror, then commit; a
  mirror failure therefore rolls the change back, and if the commit
  itself fails the previous state is written back (best effort; a failure
  there logs `user_state.restore_failed`).
- Every service's `get_current_user` calls `get_user_state`: Redis first;
  on a miss, `GET /internal/users/{id}/state` on auth-service (2-second
  timeout), cached for `USER_STATE_CACHE_TTL_SECONDS` (300). A 404 means
  "inactive". The write-back uses `nx=True`: it only fills a missing key.
  auth-service writes the key *before* its commit, so a plain `SET` here
  could replace a fresh deactivation with the row that the reader fetched
  just before that commit.
- Inactive -> 401 `Account is deactivated`. Otherwise the live role
  replaces the token's role.
- If Redis **and** auth-service are both unreachable the check returns
  `None` and the token's own claims are used (logged as
  `user_state.redis_read_failed` / `user_state.auth_lookup_failed`). This
  is a deliberate fail-open: an auth outage must not lock everyone out of
  core and ai.

The same check runs for WebSockets (`services/core-service/api/ws_common.py`,
at connect time) and for API keys (the key owner's state, `middleware.py`).
Because a WebSocket is checked only when it connects, core-service also
re-checks open Yjs editor sockets when auth-service publishes a role,
deactivation, deletion or organization-membership event: the socket is
closed (4401 or 4403) or made read-only at once
([25-realtime-yjs.md](25-realtime-yjs.md)). The analysis queue also checks
the owner again before it starts a queued analysis, and the internal
order route refuses a trade for an owner who is deactivated or read-only.
`USER_STATE_CHECK_ENABLED` (default `true`) switches it off; the test
suites switch it off by default.

### 3.9 Permissions: roles, ownership, organizations

**System roles -> permissions** (`shared/permissions.py`, the only place
role names are mapped):

| Role | Permissions |
|---|---|
| `guest` | none (read-only; can still manage their own account and accept invitations) |
| `user` | `portfolio:write`, `analysis:create`, `document:upload`, `note:write`, `comment:write`, `organization:create`, `api_key:manage`, `rag:ask` |
| `moderator` | user's + `content:moderate`, `user:deactivate` |
| `admin` | moderator's + `user:manage`, `site:stats` |

An unknown role gets nothing (fail closed). Routes use
`require_permission(Permission.X)(get_current_user)` as a dependency, or
`ensure_permission(user, X)` inside the body; both answer 403
`Insufficient permissions`. auth-service returns `sorted_permissions(role)`
from `/me` so the frontend can hide what the user cannot do.

**Ownership** is a second, separate layer. A permission says "this role
may write portfolios"; ownership says "this user may write **this**
portfolio".

- core-service, `PortfolioService` (`services/core-service/domains/portfolio/service.py`):
  - `_require_readable`: the owner, or a member of the portfolio's
    organization when the portfolio is not `private`; otherwise **404**
    (existence is not revealed).
  - `_require_owner`: every change (update, delete, holdings,
    transactions) is **owner only**. Someone who cannot read the
    portfolio gets **404**; only an organization member who can read it
    gets **403** "Only the portfolio owner can perform this action."
  - `_require_watchlist_owner`: watchlists are never shared, so anyone
    but the owner gets **404**.
  - Creating something inside an organization needs organization role
    `owner`, `admin` or `member`; a `viewer` or non-member gets 403
    (`clients/auth_client.py`, `ensure_organization_writer`,
    `ORGANIZATION_WRITE_ROLES`).
- ai-service (`services/ai-service/deps.py`):
  - `ensure_portfolio_access`: the same read rule as core.
  - `ensure_portfolio_owner`: starting an analysis on a portfolio and
    recording a **decision** (which accepts tiers that the daily job may
    then trade) is **owner only**. Not readable -> 404; readable but not
    owned -> 403. With only the read rule, any organization member, even a
    viewer, could accept tiers that would then be executed in someone
    else's portfolio.
  - `ensure_analysis_access`: your own analysis, or a system analysis
    (no user) on a portfolio you may read; otherwise 404.
  - `accessible_portfolio_ids`: the bulk form of the read rule for the
    analytics dashboard: one owner lookup per distinct portfolio (at most
    `_OWNER_LOOKUP_CONCURRENCY` = 8 at once) and at most one membership
    lookup for the caller.
  - If core-service or auth-service cannot answer, these checks return
    **503**, never "allowed" (fail closed).
  - Every ai-service write route (analyses, decisions, backtests, LLM
    reviews) needs `analysis:create`, so a guest gets 403; the RAG
    assistant needs `rag:ask`. No ai-service route accepts anonymous
    callers; system callers use `/internal`.

The **admin role does not override ownership.** An admin can manage users
and see site statistics, but cannot change another user's portfolio.

Organization membership is read from auth-service
(`GET /internal/users/{id}/organizations`) through
`shared/org_membership_cache.py`, cached 60 seconds per user; auth-service
deletes the cache entry when a membership changes (once before its commit
and once more after it, see [24-events.md](24-events.md) section 3.5).

### 3.10 Internal routes and `X-Internal-Token`

Every service mounts an `/internal` router outside `/api/v1`, with
`dependencies=[Depends(require_internal_token)]`
(`services/*/api/internal.py`).
`require_internal_token` (`shared/security.py`) compares the
`X-Internal-Token` header with `INTERNAL_API_TOKEN` using
`hmac.compare_digest`; missing or wrong -> 401
`Missing or invalid internal token`. The internal routers are built with
`include_in_schema=False`, so they do not appear in the public Swagger
pages and OpenAPI files.

Senders add the header automatically: every client from
`shared/http_client.py` has a request hook that adds it **only** for
paths starting with `/internal/` on the host and port of
`AUTH_SERVICE_URL`, `CORE_SERVICE_URL` or `AI_SERVICE_URL`. External data
providers never see the token. The scheduler's shell scripts send it
too (it is in the root-only file `/scripts/env.sh` inside the scheduler
container, `chmod 600`). For any other HTTP client, `internal_headers()`
returns the header (empty when the token is unset).

Two walls: the gateway never proxies `/internal` (404), and the token
stops anything else on the Docker network. An empty `INTERNAL_API_TOKEN`
is allowed only with `APP_ENV` `development` or `test` (the check is then
skipped, with one warning per process); any other `APP_ENV` refuses to
start, and if the setting were somehow empty there anyway, the check
answers 401 `Internal token not configured` (fail closed).

### 3.11 Rate limits

| Where | Key | Limit | On failure of the store | Code |
|---|---|---|---|---|
| Gateway, `/api/v1/auth` | client address | 10 requests/s, burst 20, `nodelay` -> 429 | - | `infra/gateway/nginx.conf` (`auth_zone`) |
| Login | per address / per email | 30 / 10 per 15 min | fail open | `services/auth-service/domains/auth/api.py`, `rate_limit.py` |
| Registration | per address | 10 per hour | fail open | same |
| Password reset request | per address / per email | 10 / 3 per hour | fail open | same |
| Resend verification | per email | 3 per hour | fail open | same |
| Invitations | per organization | 20 per hour | fail open | `domains/organization/service.py` |
| 2FA failures | per user | 5 failures in 5 min -> 15 min lock | fail open | `rate_limit.two_factor_*` |
| Public API (`/api/v1/public`) | per key and per key owner | key: its `rate_limit_per_minute` (at most 60); owner: 60 per minute across all keys | fail open | `services/core-service/middleware.py` |
| RAG assistant (`/api/v1/rag/ask`, `/ask/stream`) | per user | 10 per clock minute, 200 per UTC day | fail open | `services/ai-service/domains/rag/qa.py`, `check_rate_limit` |
| Backtests (all three create routes) | per user | at most `BACKTEST_MAX_ACTIVE_PER_USER` (2) pending or running backtests -> 429 `rate_limit_exceeded` (log `backtest_rejected_limit`) | counted in PostgreSQL under a per-user advisory lock, so two concurrent creates cannot both pass | `services/ai-service/domains/backtest/api.py`, `repository.py::count_active_for_user` |

auth-service and the public API share one counter, `fixed_window_hits` in
`shared/rate_limit.py`: in one Redis transaction it runs `SET key 0 EX
window NX`, `INCR`, `TTL`, so a key can never be left without an expiry.
The 429 uses the project's error body and a `Retry-After` header
(`rate_limited_response`). auth-service hashes emails and addresses into
the key (`auth:rl:<scope>:<sha256 prefix>`), so raw addresses never sit
in Redis. Counters live in Redis database 1 (`REDIS_DB_RATE_LIMIT`).
`RATE_LIMIT_ENABLED` (auth-service) switches the auth limits off.

Every Redis-based limit **fails open**: if Redis is down, requests are
allowed and a warning is logged (`rate_limit_unavailable` for auth-service
and the public API, `rag_ask.rate_limit_unavailable` for the RAG
assistant). An outage of the limiter must not lock users out. The 2FA
lockout is built from two keys: a failure counter
(`auth:rl:2fa:fail:<hash>`, the shared fixed window) and, after the fifth
failure, a lock key with the lock time as its expiry (the counter is then
deleted). `two_factor_guard` refuses while the lock key has a positive
TTL, with `Retry-After` = that TTL. The RAG
limit is the one exception to the shared counter: `check_rate_limit` in
`services/ai-service/domains/rag/qa.py` keeps its own per-minute and
per-day keys (`ai:rag_ask:rate:...`) in the rate-limit database (1). The
gateway limit lives in nginx memory and needs no store.

### 3.12 API keys (the public API)

`services/core-service/apikeys.py` and `api/apikeys_api.py`:

- Created with `POST /api/v1/api-keys` (needs `api_key:manage`, so guests
  cannot). The raw key is `amk_` + `secrets.token_urlsafe(32)`, shown
  **once**; only `sha256(raw_key)` is stored (`api_keys.key_hash`,
  unique). The `amk_` prefix ("AlphaMind key") lets a leaked key be
  recognised in logs or code. A plain SHA-256 is enough because the key is long and random,
  unlike a password, and it keeps lookup a simple indexed equality.
- At most `MAX_ACTIVE_API_KEYS_PER_USER` (10) non-revoked keys per user
  (409 beyond), enforced under a Postgres advisory lock so two concurrent
  creates cannot both pass.
- `rate_limit_per_minute` is between 1 and `PUBLIC_API_RATE_LIMIT_PER_MINUTE`
  (60).
- **No scopes.** A key always belongs to one user and acts as its owner,
  limited by the owner's **live role**: a guest's key is read-only (403
  `insufficient_permissions` "This API key is read-only" for anything but
  GET/HEAD), a deactivated owner's key gets 401 `account_deactivated`. An
  unknown owner state (Redis and auth down) is allowed and logged
  (`public_api.owner_state_unknown`).
- `PublicAPIRateLimitMiddleware` (`middleware.py`) checks every
  `/api/v1/public/*` request, in this order: a CORS preflight (`OPTIONS`,
  which carries no key) passes through to the CORS middleware; missing key
  401 `missing_api_key`; unknown or revoked key 401 `invalid_api_key`;
  then the owner state; then the two rate counters (the owner check comes
  first, so a refused request does not use the owner's budget). Over a
  limit -> 429 `rate_limit_exceeded` with `Retry-After` from whichever
  counter was exceeded.
- The validation result (id, owner, per-key limit, revoked flag) is cached
  in Redis DB 0 for 30 seconds (`KEY_CACHE_TTL_SECONDS`), so a busy key
  does not cost a database query per request. A Redis error is treated as
  a cache miss (the key is read from PostgreSQL), not a 500.
- Revoking a key overwrites that cache entry with a "revoked" marker at
  once, so the key stops working immediately. A marker is used instead of
  a delete, so a request racing the revoke cannot re-cache the key as
  valid before the revoke commits. Changing a key's name or limit deletes
  the cache entry, so the new limit applies on the next request. If one of
  these Redis writes fails, the revoke or change still commits (log
  `api_key_cache_write_failed`); a surviving "valid" entry then expires
  within 30 seconds. Revoking twice keeps the first `revoked_at`.
- `last_used_at` is written at most once per minute per key: a
  `SET NX EX 60` gate key `apikey:last_used_gate:<key id>` (Redis DB 1)
  decides who writes, so a busy key does not turn into a hot row with many
  concurrent `UPDATE`s. When Redis is down the write is skipped.

### 3.13 Input validation

- Every request body is a pydantic model; a bad body is a 422 before any
  code runs. Free-text fields are bounded: for example `MeUpdate`
  (`first_name`/`last_name` 100, `bio` 500), API key `name` 128, a RAG
  question 2000 characters (`MAX_QUESTION_CHARS`), usernames
  `^[A-Za-z0-9_.-]{3,32}$`, a 2FA code at most 16 characters.
- Tickers: one rule for every service, `shared/tickers.py`:
  `TICKER_RE = ^[A-Z0-9.\-]{1,10}$` after strip and upper-case
  (`normalize_ticker`, ValueError -> 422).
- Pagination: `page <= 100000` (`MAX_PAGE`) and `page_size <= 100`
  (`shared/schemas.py`), so an offset always fits Postgres.
- Uploads: the gateway accepts bodies up to 25 MB
  (`client_max_body_size 25m`); core-service accepts research documents up
  to 20 MB (`MAX_UPLOAD_SIZE_BYTES`) with extensions `.pdf`, `.txt`, `.md`;
  avatars up to 5 MB, and their real type is checked from the file's first
  bytes, not only the declared Content-Type.
- Yjs collaboration frames above `YJS_MAX_UPDATE_BYTES` (1 MiB) close the
  socket with code 1008.

### 3.14 SQL injection

All queries go through SQLAlchemy: ORM queries and `text()` statements
with bound parameters (for example `text("SELECT pg_advisory_xact_lock(hashtext(:owner))")`).
Bound parameters are sent separately from the SQL, so user input is never
parsed as SQL. The only f-string SQL in application code builds schema
names from settings (`CREATE SCHEMA IF NOT EXISTS "{schema}"` in
`shared/alembic_bootstrap.py`, and the test fixtures), never from request
data.

### 3.15 CORS

`BACKEND_CORS_ORIGINS` (default `http://localhost:3000`, comma separated)
is read by `BaseServiceSettings.cors_origins()` and passed to FastAPI's
`CORSMiddleware` in each `main.py` with `allow_credentials=True` and all
methods and headers. CORS only matters for browsers: it decides which
other web origins may call the API from JavaScript. It is not an
authentication control.

### 3.16 Secrets handling

- `.env.docker` holds every secret. It is listed in `.gitignore`
  (`.env`, `.env.*`, except `.env.example`) and in `.dockerignore`, so it
  is never committed and never copied into an image. `.env.example` is
  the template with placeholders.
- `shared/config.py` refuses to start (`ValueError`) outside
  `APP_ENV=development|test` when `JWT_SECRET` is empty, the placeholder
  `CHANGE_ME`, or shorter than 32 characters, and when
  `INTERNAL_API_TOKEN` is empty. In development it only warns.
- Logs: `shared/logging.py` raises `httpx`, `httpcore`, `botocore`,
  `urllib3` and `s3transfer` to WARNING, because their INFO/DEBUG lines
  contain request URLs (a data provider sends its API key as a query
  parameter) or S3 access key ids. Query strings are removed from every
  URL the gateway and the services' uvicorn loggers write (section 3.1),
  and no route takes a secret in its path.
- Production images run as an unprivileged user (uid 10001) with the code
  read-only (`services/*/Dockerfile`, `production` stage).

### 3.17 Container exposure

From `docker-compose.yaml`:

| Port on the host | Service | Who can reach it |
|---|---|---|
| `8443`, `8000` (all interfaces) | gateway | anyone who can reach the machine |
| `127.0.0.1:6333` | Qdrant REST API and dashboard | only this machine |
| `127.0.0.1:8025` | MailHog UI/API | only this machine |
| `127.0.0.1:9090` | Prometheus | only this machine |
| `127.0.0.1:9093` | Alertmanager | only this machine |
| `127.0.0.1:8080` | Adminer (only with `make adminer`) | only this machine |
| none | auth/core/ai services, PostgreSQL, Redis, SeaweedFS, Grafana (through the gateway), exporters, scheduler | only containers on the Docker network |

SeaweedFS: its master, volume server and filer have no authentication, so
they listen on the container's own loopback (`-ip=127.0.0.1
-ip.bind=127.0.0.1`); only the S3 gateway (port 9000) is on the Docker
network, and it requires signed requests with `S3_ACCESS_KEY` /
`S3_SECRET_KEY`.

**Qdrant and Redis have no password.** Any container on the Docker
network can read and write them, and any process on the host can use
Qdrant on `127.0.0.1:6333`. The implication: the Docker network is a
trust boundary. A compromised container could read the user-state mirror,
rate counters, event streams and cached data from Redis, or change
vectors in Qdrant. This is acceptable for a single-machine deployment
where only the gateway is published; for a shared or cloud host you would
add `requirepass` for Redis and an API key for Qdrant. The Qdrant key
needs both sides: the client side exists already (ai-service
`QDRANT_API_KEY`, also used by backup and restore), but the `qdrant`
service in `docker-compose.yaml` sets no `QDRANT__SERVICE__API_KEY`, so
setting `QDRANT_API_KEY` alone secures nothing; set the same value in
both.

### 3.18 Backups and secrets

`infra/scheduler/backup.sh` (daily at 02:30 New York, 07:30 London) writes
`postgres.dump`, Qdrant snapshots, the object-storage documents and the
avatars into `./backups/<YYYY-MM-DD_HHMM>/`. It does **not** copy
`.env.docker`, and the SeaweedFS S3 identity file is written to
`/etc/seaweedfs/s3.json` at container start, outside the backed-up data
directory. But the database dump itself is sensitive:

- `auth.users.password_hash` (bcrypt), `auth.user_sessions` refresh-token
  hashes, `core.api_keys.key_hash` (SHA-256): hashes, not usable directly;
- `auth.two_factor_secrets.secret`: the TOTP secrets are encrypted
  (Fernet). A backup alone does not reveal them, but a backup together
  with `.env.docker` (`TOTP_ENCRYPTION_KEY`, `JWT_SECRET`) does. Rows not
  yet converted (see 3.6) are still plain text;
- every user's email and personal data.

Treat `./backups` like a secret. It is gitignored and dockerignored; keep
off-machine copies encrypted. See `../DISASTER_RECOVERY.md`.

---

## 4. Configuration

| Setting | Default | Meaning | File |
|---|---|---|---|
| `APP_ENV` | `development` | `development`, `staging`, `production` or `test`; outside development/test weak secrets refuse startup. | `shared/config.py` |
| `JWT_SECRET` | `CHANGE_ME` | HMAC key for every JWT; at least 32 characters outside development/test. | `shared/config.py` |
| `JWT_ALGORITHM` | `HS256` | JWT signature algorithm. | `shared/config.py` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifetime. | `services/auth-service/config.py` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Refresh token and session lifetime. | same |
| `PASSWORD_MIN_LENGTH` | `10` | Minimum password length (max is 72 bytes, fixed). | same |
| `TOTP_ISSUER` | `AlphaMind` | Name shown in the authenticator app. | same |
| `TOTP_ENCRYPTION_KEY` | `""` | Fernet key for stored TOTP secrets; must be valid when set; required outside development/test (empty in development: derived from `JWT_SECRET`). | same |
| `OAUTH_2FA_CHALLENGE_EXPIRE_MINUTES` | `5` | Lifetime of the OAuth 2FA challenge. | same |
| `TWO_FACTOR_RECENT_LOGIN_MINUTES` | `10` | `/2fa/setup` and `/2fa/verify` need a login (`auth_time`) at most this old, else 403 `recent_login_required`. | same |
| `GOOGLE_CLIENT_ID/SECRET`, `GITHUB_CLIENT_ID/SECRET` | `""` | OAuth apps; empty = not configured. | same |
| `OAUTH_REDIRECT_BASE_URL` | `https://localhost:8443` | Public base of the OAuth callback. | same |
| `RATE_LIMIT_ENABLED` | `true` | auth-service limits on/off. | same |
| `RATE_LIMIT_LOGIN_PER_EMAIL` / `_PER_IP` / `_WINDOW_SECONDS` | `10` / `30` / `900` | Login limits. | same |
| `RATE_LIMIT_2FA_FAILURES` / `_WINDOW_SECONDS` / `_LOCK_SECONDS` | `5` / `300` / `900` | 2FA lockout. | same |
| `RATE_LIMIT_PASSWORD_RESET_PER_EMAIL` / `_PER_IP` / `_WINDOW_SECONDS` | `3` / `10` / `3600` | Reset limits. | same |
| `RATE_LIMIT_REGISTER_PER_IP` / `_WINDOW_SECONDS` | `10` / `3600` | Registration limit. | same |
| `RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL` / `_WINDOW_SECONDS` | `3` / `3600` | Resend limit. | same |
| `RATE_LIMIT_INVITATIONS_PER_ORG` / `_WINDOW_SECONDS` | `20` / `3600` | Invitation limit. | same |
| `INTERNAL_API_TOKEN` | `""` | Shared secret for `/internal/*`; required outside development/test. | `shared/config.py` |
| `USER_STATE_CHECK_ENABLED` | `true` | Live role/active check on every request. | `shared/config.py` |
| `USER_STATE_CACHE_TTL_SECONDS` | `300` | Cache time for a state fetched from auth-service after a Redis miss. | `shared/config.py` |
| `BACKEND_CORS_ORIGINS` | `http://localhost:3000` | Allowed browser origins. | `shared/config.py` |
| `PUBLIC_API_RATE_LIMIT_PER_MINUTE` | `60` | Default and maximum per-key limit, and per-owner budget. | `services/core-service/config.py` |
| `MAX_ACTIVE_API_KEYS_PER_USER` | `10` | Live keys per user. | same |
| `MAX_UPLOAD_SIZE_BYTES` | `20971520` (20 MB) | Research document size. | same |
| `YJS_MAX_UPDATE_BYTES` | `1048576` | Largest collaborative-edit frame. | same |
| `RAG_ASK_RATE_LIMIT_PER_MINUTE` / `_PER_DAY` | `10` / `200` | RAG assistant limits. | `services/ai-service/config.py` |
| `AVATAR_MAX_SIZE_MB` | `5` | Avatar size. | `services/auth-service/config.py` |
| gateway `auth_zone` | `10r/s`, burst 20 | Gateway limit on `/api/v1/auth`. | `infra/gateway/nginx.conf` |
| `client_max_body_size` | `25m` | Largest request body the gateway forwards. | same |

---

## 5. Design decisions and why

**Short access tokens plus a live state check, not a token blacklist.**
Alternative: a list of revoked access tokens checked on each request.
The live mirror solves the real need (demotion and deactivation take
effect at once) with one Redis read per request, and also covers API keys
and WebSockets.

**Fail open for the user-state check and rate limits; fail closed for
ownership.** If Redis is down, blocking every request would turn a cache
outage into a full outage. So limits and the live state fall back to
"allow" (with logs). Ownership is different: a wrong "allow" there could
let someone trade in another user's portfolio, so ai-service answers 503
when it cannot verify ownership.

**Decisions only by the portfolio owner.** With the **read** rule on
writes, any member of a shared portfolio's organization, even a viewer,
could start an analysis and accept its tiers, which the daily tier job
would then execute in the owner's portfolio. So `ensure_portfolio_owner`
mirrors core-service's `_require_owner`.

**Replace `X-Forwarded-For` at the gateway.** Appending keeps whatever the
client sent first, and uvicorn trusts the left-most entry, so every
per-address limit could be bypassed by sending a fake header.

**Store hashes, not tokens.** Refresh tokens (SHA-256), API keys
(SHA-256) and passwords (bcrypt) are stored as hashes, so a database leak
does not hand out working credentials. Rotation is one atomic
`UPDATE ... RETURNING`, so two concurrent refreshes cannot both win, and
reuse of a rotated token revokes everything.

**One internal secret, checked in constant time.** Alternative: mutual
TLS between services. A shared header is simple to run in Docker Compose,
and with the gateway refusing `/internal` it gives two independent walls.

**Refuse weak secrets at startup.** `JWT_SECRET` defaults to the
placeholder `CHANGE_ME`, and anyone who knows the default could mint admin
tokens. So outside development/test the settings refuse to load with the
placeholder, an empty secret or one shorter than 32 characters.

**Same answers for unknown accounts.** Login burns a dummy bcrypt check;
reset and resend return the same message and send email in the
background, so neither content nor timing reveals which emails have
accounts.

**bcrypt through passlib, pinned.** passlib is unmaintained but stable;
pinning `bcrypt<4.1` keeps it working instead of rewriting hashing code.

---

## 6. When things go wrong

| Symptom | Likely cause | How to diagnose |
|---|---|---|
| Every request 401 `Invalid or expired token` after a deploy | `JWT_SECRET` changed (all old tokens invalid). | Expected; users log in again. |
| A service will not start: `JWT_SECRET must be set ...` or `INTERNAL_API_TOKEN must be set ...` | `APP_ENV` is not development/test and the secret is weak or empty. | Set a value from `openssl rand -hex 32` in `.env.docker`. |
| 401 `Account is deactivated` | The live state says inactive. | `bash scripts/redis-cli.sh -n 2 GET auth:user_state:<user_id>` |
| A demoted user still has old rights | Live check off, or Redis and auth both unreachable. | Look for `user_state.redis_read_failed` / `user_state.auth_lookup_failed`; check `USER_STATE_CHECK_ENABLED`. |
| 401 `Missing or invalid internal token` between services | Services started with different `INTERNAL_API_TOKEN` values. | Recreate all services and the scheduler from the same `.env.docker`. |
| Many 429 for everyone behind one address | Per-address limits; behind Docker Desktop all host traffic can share one address. | Wait for `Retry-After`; check keys with `bash scripts/redis-cli.sh -n 1 --scan --pattern 'auth:rl:*'`. |
| `/2fa/setup` or `/2fa/verify` answers 403 `recent_login_required` | The session's login (`auth_time`) is older than 10 minutes, or the token predates the claim; refreshing does not renew it. | Log in again (password or OAuth) and retry. |
| Users logged out everywhere unexpectedly | A rotated refresh token was reused (`refresh_token_reuse_detected`). | Search auth-service logs; check the client does not reuse old refresh tokens. |
| 503 "Portfolio ownership check is unavailable" | core-service or auth-service down. | `make health`. |
| Public API always 401 `invalid_api_key` right after creating a key | Wrong header or a typo; the key is shown only once. | Header must be `X-API-Key`. |
| Browser blocks API calls with a CORS error | Frontend origin not in `BACKEND_CORS_ORIGINS`. | Add it and recreate the services. |

---

## 7. Demo walkthrough

All commands run from the repository root with the stack up. `-k` is
needed because the development certificate is self-signed.

1. **TLS and headers.**

   ```bash
   curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' http://localhost:8000/status
   curl -skI https://localhost:8443/status
   ```

   Show `301 -> https://localhost:8443/status`, then the headers
   `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; ...`.

2. **401 without a token.**

   ```bash
   curl -sk -w '\n%{http_code}\n' https://localhost:8443/api/v1/portfolios
   ```

   Output: `{"detail":"Missing bearer token"}` and `401`. Then with a broken
   token:

   ```bash
   curl -sk -w '\n%{http_code}\n' -H 'Authorization: Bearer abc.def.ghi' \
     https://localhost:8443/api/v1/portfolios
   ```

   Output: `{"detail":"Invalid or expired token"}`, `401`.

3. **Decode a JWT payload without the secret.** Say: "The payload is only
   encoded, not encrypted. Anyone can read it; only the server can sign
   it."

   ```bash
   TOKEN=$(bash scripts/token.sh owner)
   python3 - "$TOKEN" <<'EOF'
   import base64, json, sys
   def part(p):
       return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
   header, payload, _signature = sys.argv[1].split(".")
   print(json.dumps(part(header), indent=2))
   print(json.dumps(part(payload), indent=2))
   EOF
   ```

   You see `{"alg": "HS256", "typ": "JWT"}` and a payload with `sub` (the
   user id), `type: "access"`, `iat`, `exp` (Unix times), `jti` and
   `role`. Change one character of the payload and send it: 401, because
   the signature no longer matches. (`scripts/token.sh` makes a 2-hour
   token inside the auth-service container with the real `JWT_SECRET`;
   tokens from login live 15 minutes.)

4. **The admin cannot touch someone else's portfolio.** The admin account
   tries to change the test owner's portfolio. The body is empty, and the
   request is refused before anything is changed (if your admin account
   happens to own that portfolio, pick another portfolio id).

   ```bash
   ADMIN=$(bash scripts/token.sh admin)
   curl -sk -w '\n%{http_code}\n' -X PATCH \
     -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
     -d '{}' https://localhost:8443/api/v1/portfolios/d72855ea-2776-41c3-bc77-ce1a4c1435d5
   ```

   Output: `404` with
   `{"error":{"code":"not_found","message":"Portfolio not found.", ...}}`
   when the admin cannot read the portfolio (the usual case: a private
   portfolio). Only if the admin is a member of the portfolio's
   organization and the portfolio is shared with it, the answer is `403`
   `permission_denied` "Only the portfolio owner can perform this action."
   Say: "Admin is a role for managing users, not a key to every portfolio;
   and a stranger cannot even learn that the portfolio exists. Permission
   and ownership are separate checks." (The id is the default
   `PORTFOLIO_ID` in `scripts/lib.sh`.)

5. **The internal route refused through the gateway, and by the token.**

   ```bash
   curl -sk -o /dev/null -w '%{http_code}\n' \
     https://localhost:8443/internal/users/551b898b-e87c-4872-8bca-70333bc9d00b/state
   ```

   Output `404`: the gateway never forwards `/internal`. Now from inside
   the Docker network, with a plain client that does not add the header:

   ```bash
   docker compose --env-file .env.docker exec -T core-service python -c "
   import httpx
   r = httpx.get('http://auth-service:8000/internal/users/551b898b-e87c-4872-8bca-70333bc9d00b/state')
   print(r.status_code, r.text)"
   ```

   Output `401 {"detail":"Missing or invalid internal token"}` (when
   `INTERNAL_API_TOKEN` is set; with an empty token in development the
   check is skipped and you get `200`). Then with the header the services
   add (the token is not printed):

   ```bash
   docker compose --env-file .env.docker exec -T core-service python -c "
   import httpx
   from shared.http_client import internal_headers
   r = httpx.get('http://auth-service:8000/internal/users/551b898b-e87c-4872-8bca-70333bc9d00b/state', headers=internal_headers())
   print(r.status_code, r.text)"
   ```

   Output `200 {"role":"admin","is_active":true}`: the live state record.

6. **A rate limit 429.** Login 12 times for an address that does not
   exist, then once more to read the header. This writes only Redis
   counters, but it uses 13 of your address's 30 logins per 15 minutes,
   so do not run it right before an end-to-end check.

   ```bash
   for i in $(seq 1 12); do
     curl -sk -o /dev/null -w '%{http_code} ' -X POST https://localhost:8443/api/v1/auth/login \
       -H 'Content-Type: application/json' \
       -d '{"email":"ratelimit-demo@example.com","password":"wrong-password"}'
   done; echo
   curl -sk -D - -o /dev/null -X POST https://localhost:8443/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"ratelimit-demo@example.com","password":"wrong-password"}' | grep -i retry-after
   ```

   Output: ten `401`, then `429 429`, then `Retry-After: <seconds>`. Say:
   "Ten tries per email per 15 minutes. The counter key is a hash of the
   email, so Redis never stores the address." Show the keys:
   `bash scripts/redis-cli.sh -n 1 --scan --pattern 'auth:rl:*'`.

7. **The live role mirror.**
   `bash scripts/redis-cli.sh -n 2 GET auth:user_state:551b898b-e87c-4872-8bca-70333bc9d00b`
   shows `{"role": "admin", "is_active": true}`. Say: "If an admin
   deactivates a user, this value changes and that user's next request is
   refused, even with a token that has not expired."

8. **Hashes, not secrets, in the database.**

   ```bash
   bash scripts/psql.sh -c "SELECT left(password_hash, 7) AS bcrypt_prefix, length(password_hash) FROM auth.users LIMIT 3"
   bash scripts/psql.sh -c "SELECT name, left(key_hash, 12) AS sha256_prefix, rate_limit_per_minute FROM core.api_keys LIMIT 3"
   ```

   bcrypt hashes start with `$2b$12$` and are 60 characters; API keys are
   only SHA-256 hex digests.

---

## 8. Check your understanding

<details>
<summary>1. A JWT is "signed, not encrypted". What does that mean for what you put in it?</summary>

Anyone can read the claims (base64url). Only a holder of `JWT_SECRET`
can make a valid signature. So never put secrets in a token; AlphaMind
puts only ids, the role, times and a `jti`.
</details>

<details>
<summary>2. An admin demotes a user to guest. Their access token still says "user" for 14 more minutes. What stops them from writing?</summary>

The live state check: `get_current_user` reads `auth:user_state:<id>`
and replaces the token's role with `guest`, which has no write
permissions, so write routes answer 403.
</details>

<details>
<summary>3. What happens when someone presents a refresh token that was already rotated?</summary>

The atomic claim finds no live session; the token is known and was
revoked as `rotated`, so every session of the user is revoked (reason
`reuse`) and the request gets 401.
</details>

<details>
<summary>4. Why is <code>bcrypt</code> pinned below 4.1?</summary>

bcrypt 4.1 removed `__about__` (passlib's version detection) and started
raising for inputs over 72 bytes, which breaks passlib's import-time self
test. passlib is unmaintained, so the pin keeps it working.
</details>

<details>
<summary>5. Name the two walls that protect <code>/internal/*</code>.</summary>

The gateway returns 404 for `/internal` (never proxied), and every
internal router requires `X-Internal-Token` equal to
`INTERNAL_API_TOKEN` (constant-time comparison).
</details>

<details>
<summary>6. Why do rate limits fail open but ownership checks fail closed?</summary>

A limiter outage should not lock everyone out (low harm in allowing). A
wrong ownership "allow" could let someone trade in another user's
portfolio (high harm), so ai-service answers 503 instead.
</details>

<details>
<summary>7. Can an organization viewer record a decision on a shared portfolio?</summary>

No. Recording a decision needs `ensure_portfolio_owner`: readable but not
owned gives 403.
</details>

<details>
<summary>8. What is sensitive in a backup, even though <code>.env.docker</code> is not in it?</summary>

The database dump: password and token hashes, encrypted TOTP secrets
(readable by anyone who also has `.env.docker`), users' emails and data.
Treat backups as secrets.
</details>

<details>
<summary>9. Redis has no password. Why is that acceptable here, and when would it not be?</summary>

Redis has no host port; only containers on the private Docker network can
reach it, and only the gateway is published. On a shared or cloud host,
or if any container could be compromised, you would add `requirepass`.
</details>

---

## 9. Known limitations

- HS256 with one shared secret: every service holds `JWT_SECRET` and
  could therefore mint tokens. An asymmetric algorithm (RS256, auth signs,
  others only verify) would limit that.
- Access tokens cannot be revoked one by one; logout ends the refresh
  session, and the live state covers deactivation, but a stolen access
  token works until it expires (15 minutes). If the login behind it
  is less than 10 minutes old, it can also switch 2FA on (setup and
  verify ask for no password, only a recent login, section 3.6).
- TOTP secrets are encrypted with a key from `.env.docker`; anyone with
  both a backup and `.env.docker` can still read them.
- Redis and Qdrant have no authentication; the Docker network is the trust
  boundary, and Qdrant is reachable from the host on `127.0.0.1:6333`.
- The live state check and every rate limit fail open when Redis (and,
  for the state, auth-service) are unavailable.
- The development certificate is self-signed; production needs a real
  one.
- An organization member who can read a shared portfolio gets 403 on a
  write, which is expected (they already know it exists); everyone else
  gets 404 on reads and writes alike.
- Two error-body shapes: token and missing-permission errors use
  FastAPI's `{"detail": ...}`, service errors the `{"error": {...}}`
  envelope (section 3.4).
- Open WebSocket JSON channels are not re-checked when rights change; they
  end at token expiry at the latest. Only Yjs editor sockets are
  re-checked, and only on the replica that receives the event (one
  core-service replica is assumed).
- nginx's error log can still contain a full request line (with a token
  query string) when an upstream fails.
- Swagger pages (`/docs/auth/`, `/docs/core/`, `/docs/ai/`) and
  `/health/*` are public through the gateway. They show the API shape and
  service health, not data.
- Per-address limits see one address for all host traffic behind Docker
  Desktop, which makes them coarse in development.
- The scheduler and operator scripts can mint tokens (`scripts/token.sh`
  runs `_create_token` inside auth-service). Whoever can run
  `docker compose exec` on the host is effectively an administrator.

---

## 10. Code map

| File | What it holds |
|---|---|
| `infra/gateway/nginx.conf` | TLS, redirects, security headers, CSP, path allow-list, `/internal` 404, auth rate zone, `X-Forwarded-For` replacement, log format. |
| `infra/gateway/gen-certs.sh`, `csp_hashes.py`, `status.html` | Development certificate; CSP hash tool; the status page. |
| `shared/security.py` | `CurrentUser`, `decode_token`, `make_get_current_user`, `require_permission`, `ensure_permission`, `require_internal_token`, `is_uuid_claim`. |
| `shared/exceptions.py` | The `{"error": {...}}` envelope and the application error classes. |
| `shared/permissions.py` | `Permission`, `ROLE_PERMISSIONS`, `permissions_for`, `has_permission`, `sorted_permissions`. |
| `shared/user_state.py` | Live role/active state: `UserState`, `store_user_state`, `get_user_state`. |
| `shared/rate_limit.py` | `fixed_window_hits`, `rate_limited_response`. |
| `shared/http_client.py` | Internal-token request hook, `internal_headers`. |
| `shared/config.py` | Security settings and the startup checks for weak secrets. |
| `shared/tickers.py` | The ticker rule. |
| `services/auth-service/security.py` | bcrypt, token creation, special tokens, TOTP, Fernet encryption of stored TOTP secrets. |
| `services/auth-service/domains/auth/service.py` | Login, refresh rotation and reuse detection, 2FA, OAuth. |
| `services/auth-service/domains/auth/repository.py` | `claim_session_for_rotation`, `revoke_all_sessions`, `consume_totp_step`. |
| `services/auth-service/rate_limit.py` | Auth limits and 2FA lockout. |
| `services/auth-service/user_state_sync.py` | Writer side of the live state. |
| `services/core-service/apikeys.py`, `api/apikeys_api.py`, `middleware.py` | API keys and the public API guard. |
| `services/core-service/domains/portfolio/service.py` | `_require_readable`, `_require_owner`. |
| `services/ai-service/deps.py` | `ensure_portfolio_access`, `ensure_portfolio_owner`, `ensure_analysis_access`. |
| `services/ai-service/domains/rag/qa.py` | RAG ask rate limit. |
| `docker-compose.yaml` | Port bindings, SeaweedFS loopback flags. |
| `.gitignore`, `.dockerignore` | Keep `.env.docker`, backups and logs out of git and images. |

---

## 11. Related chapters

- [10-gateway.md](10-gateway.md): nginx in detail.
- [11-auth-service.md](11-auth-service.md): accounts, sessions, OAuth, 2FA.
- [12-core-service.md](12-core-service.md): portfolios, API keys, public API.
- [16-ai-analysis-pipeline.md](16-ai-analysis-pipeline.md): decisions and tiers.
- [23-redis.md](23-redis.md): the Redis databases used by limits and the
  live state.
- [28-email.md](28-email.md): reset and verification links.
- [30-shared-library.md](30-shared-library.md): the shared modules named here.
- [../API_SPEC.md](../API_SPEC.md), [../DISASTER_RECOVERY.md](../DISASTER_RECOVERY.md).
