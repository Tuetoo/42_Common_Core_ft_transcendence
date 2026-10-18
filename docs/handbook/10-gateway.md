# 10. The gateway (nginx, TLS, routing)

> The gateway is one nginx 1.27 container (`nginx:1.27-alpine`) that sits in
> front of the whole system. It is the only container published on every
> network interface of the host. It ends TLS (HTTPS) on port 443 (published
> as 8443), turns plain HTTP on port 80 (published as 8000) into a redirect
> to HTTPS, sends each request path to the right backend service
> (auth-service, core-service, ai-service or Grafana), upgrades WebSocket
> connections, adds the security headers to every answer, rate-limits the
> login API, sends every other path to the Next.js frontend, and refuses
> the service-to-service `/internal/*` routes and `/metrics`. A second,
> internal listener on port 8080 (not published) serves only the API and
> health routes, for the frontend's server side. The configuration is
> `infra/gateway/nginx.conf` plus `infra/gateway/api_locations.conf` (the
> API and health locations, included by both listeners).

---

## 1. The big picture

A browser, `curl` or a script never talks to a service directly. It talks to
the gateway, and the gateway forwards the request inside the private Docker
network.

```
                          host machine
   browser / curl         (only the gateway listens on all interfaces)
        |
        |  https://localhost:8443  (TLS)        http://localhost:8000
        v                                              |
  +-------------------------------------------------------------+
  | gateway  (nginx 1.27, container ports 443, 80 and 8080)     |
  |                                                             |
  |  port 80  -> "301 Location: https://<host>:8443<uri>"       |
  |  port 443 -> TLS ends here, then match the path:            |
  |     /api/v1/auth, /organizations, /connections, /admin,     |
  |     /static/avatars                     -> auth-service:8000|
  |     /api/v1/portfolios, /watchlists, /api-keys, /research,  |
  |     /notifications, /public, /moderation, /ws -> core:8000  |
  |     /api/v1/analyses, /market, /analytics, /decisions,      |
  |     /backtests, /rag                     -> ai-service:8000 |
  |     /grafana/                            -> grafana:3000    |
  |     /health/{auth,core,ai}, /docs/{auth,core,ai}/,          |
  |     /api/openapi-{auth,core,ai}.json     -> each service    |
  |     /status                  -> static file (status.html)   |
  |     /internal/*, /metrics                -> 404             |
  |     anything else (pages, /healthz)      -> frontend:3000   |
  |  port 8080 (Docker network only, plain HTTP):               |
  |     /api/v1/..., /health/{auth,core,ai} -> same as above    |
  |     anything else                        -> 404             |
  +-------------------------------------------------------------+
        |   plain HTTP inside the Docker network
        v
  auth-service   core-service   ai-service   grafana   frontend
  (uvicorn, port 8000, started with --proxy-headers
   --forwarded-allow-ips=*, so they trust X-Forwarded-*)
  frontend server side --> http://gateway:8080/api/v1/...
```

Three ideas explain almost everything in this chapter:

1. **One door.** Only the gateway has a host port open to the world
   (`docker-compose.yaml`, service `gateway`, ports `"8443:443"` and
   `"8000:80"`). The three application services have no `ports:` at all.
   Developer tools (MailHog, Prometheus, Qdrant, Alertmanager, Adminer) are
   bound to `127.0.0.1` only.
2. **Allow-list routing.** Every backend path the outside world may use
   is written out as a `location` block. Everything else goes to the
   frontend (`location /`), which serves only its own pages and answers
   404 for the rest. `/internal/*` and `/metrics` have explicit 404
   blocks, so they cannot be reached from outside, even by accident.
3. **The gateway is the edge.** It is the first thing that sees a client,
   so it is the only place that knows the real client address and the real
   scheme (https). It writes that knowledge into `X-Forwarded-*` headers,
   and the services are told to trust those headers. The one exception is
   the internal listener: there the frontend passes the browser's address
   on, and nginx restores it (section 3.3).

---

## 2. Concepts you need

| Term | What it is | Why it matters here |
|---|---|---|
| **Reverse proxy** | A server that receives a request and forwards it to another server, then sends the answer back. | nginx is the reverse proxy in front of the three services. |
| **nginx `location`** | A rule that matches a request path. `location /a` is a prefix match, `location = /a` is an exact match, `location ^~ /a/` is a prefix match that stops nginx from also trying regex locations. The longest matching prefix wins. | All routing is written with these. |
| **`proxy_pass`** | The directive that forwards a request to a backend. | Every backend route uses it. |
| **TLS / HTTPS** | Encryption of the HTTP connection. The server proves its identity with a **certificate** and a private **key**. | The gateway is the TLS endpoint ("TLS termination"); behind it, traffic is plain HTTP inside Docker. |
| **Self-signed certificate** | A certificate that signs itself instead of being signed by a public authority. Browsers and `curl` do not trust it by default. | Used for local development. `curl -k` tells curl to skip the trust check. |
| **SAN (Subject Alternative Name)** | The list of names a certificate is valid for. | The gateway certificate is valid for `DNS:localhost` and `IP:127.0.0.1` only. |
| **HTTP 301** | "Moved permanently": the answer tells the client the new URL in the `Location` header. | Port 80 answers every request with a 301 to HTTPS. |
| **WebSocket** | A long-lived, two-way connection that starts as an HTTP request with `Upgrade: websocket` and then switches protocol. | Live notifications and collaborative editing use it (`/ws/...`). A proxy must pass the `Upgrade` and `Connection` headers on. |
| **Server-Sent Events (SSE)** | A one-way stream of text events over a normal HTTP response. | The RAG assistant streams tokens with it (`/api/v1/rag/ask/stream`). The proxy must not buffer the answer. |
| **`X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Real-IP`** | Headers a proxy adds so the backend knows the original client address, scheme and host. | Rate limits in auth-service key on the client address, so it must be the real one. |
| **HSTS** (`Strict-Transport-Security`) | A header that tells a browser "use HTTPS only for this site for N seconds". | Sent for one year. |
| **CSP** (`Content-Security-Policy`) | A header that tells the browser which scripts, styles and connections a page may use. | Only the `/status` page gets one, built from SHA-256 hashes of its inline script and style. |
| **`limit_req`** | nginx's request rate limiter (a "leaky bucket": a steady rate plus a small burst allowance). | Protects `/api/v1/auth` and `/api/v1/public`. |
| **`real_ip` module** (`set_real_ip_from`, `real_ip_header`) | Replaces `$remote_addr` with an address taken from a request header, but only when the peer is in a trusted range. | Used only on the internal listener, so the frontend's calls carry the browser's address. |
| **Docker embedded DNS** | Inside a user-defined Docker network, the resolver at `127.0.0.11` turns service names (`auth-service`) into container IP addresses. | The gateway re-resolves backend names every 10 seconds through it. |

---

## 3. How it works in AlphaMind

### 3.1 Where the files are and how the container gets them

`docker-compose.yaml`, service `gateway`:

| Mount (host -> container) | Purpose |
|---|---|
| `./infra/gateway/nginx.conf` -> `/etc/nginx/nginx.conf` (read-only) | the main configuration (`http` block and the servers) |
| `./infra/gateway/api_locations.conf` -> `/etc/nginx/api_locations.conf` (read-only) | the API and `/health/*` locations, included by the HTTPS server and the internal listener |
| `./infra/gateway/certs` -> `/etc/nginx/certs` (read-only) | `server.crt` and `server.key` |
| `./infra/gateway/status.html` -> `/usr/share/nginx/status/status.html` (read-only) | the public status page |

Other facts from the same service block:

- Image: `nginx:1.27-alpine`.
- Ports: `"8443:443"` (HTTPS) and `"8000:80"` (HTTP that only redirects).
  Port 8080 (the internal listener) is not published.
- `depends_on`: auth-service, core-service and ai-service with
  `condition: service_healthy`. The gateway starts only after all three
  answer `/health` with 200 (see their `healthcheck` blocks).
- `restart: unless-stopped`.
- Logging is capped by the shared `x-logging` anchor (json-file, 5 files of
  20 MB).

### 3.2 The `http` block: settings for every request

Read `infra/gateway/nginx.conf` from the top. Before the `http` block
stand two process settings: **`worker_processes auto;`** (one nginx
worker per CPU of the Docker VM) and **`events { worker_connections
1024; }`** (each worker holds at most 1024 connections at once, counting
both the client side and the upstream side, and every open WebSocket
counts). That is far above the load of this project; raise it only if
`worker_connections are not enough` appears in the gateway log. Then, in
the `http` block, in order:

1. **`server_tokens off;`** nginx does not print its version in the
   `Server` header or on error pages. An attacker learns less.
2. **`absolute_redirect off;`** When nginx itself builds a redirect (for
   example a missing trailing slash), it sends a relative `Location`. An
   absolute one would contain the container's own port (443), not the
   published one (8443), and the browser would go nowhere.
3. **`client_max_body_size 25m;`** The largest request body the gateway
   accepts. nginx's own default is 1 MB, which refuses an ordinary
   image-heavy earnings-deck PDF (about 3 MB) with a bare 413. The limit
   is set for the whole `http` block, not only for the research upload
   route, because avatar uploads have the same need and a tighter
   per-route limit gains nothing. It only controls what the gateway
   forwards. The service limits are lower and still apply on top:
   core-service `MAX_UPLOAD_SIZE_BYTES` = 20 MB for research documents
   (`services/core-service/config.py`), auth-service `AVATAR_MAX_SIZE_MB` =
   5 for avatars (`services/auth-service/config.py`).
4. **`resolver 127.0.0.11 valid=10s ipv6=off;`** Docker's embedded DNS. See
   section 3.4 for why this matters.
5. **`log_format main_noargs`** and **`access_log ... main_noargs`**. The
   access log line contains `$request_method $uri $server_protocol`, never
   the query string. The WebSocket endpoint receives the access token as
   `/ws/...?token=<jwt>` and the OAuth callback receives
   `?code=...&state=...`; nginx's default `combined` format logs
   `$request`, which includes the query string, so those secrets would
   reach the container log and from there `./logs` (the `vector` service
   copies container logs there). The path itself is logged, so no route
   carries a secret in its path: the organization invitation token, for
   example, is sent in the request body
   ([chapter 11](11-auth-service.md), section 3.13). The backends behind
   the gateway apply the same rule to their own uvicorn logs:
   `shared/logging.py` installs a filter on `uvicorn.access` and
   `uvicorn.error` that replaces every query string with `?[redacted]`
   ([chapter 29](29-security.md), section 3.1). nginx's error log can
   still print a raw request line when an upstream fails; that rare case
   is accepted.
6. **`limit_req_zone $binary_remote_addr zone=auth_zone:10m rate=10r/s;`**
   and **`limit_req_status 429;`** A rate-limit memory zone of 10 MB, keyed
   by the client address, refilling at 10 requests per second. Rejected
   requests get HTTP 429. The zone is used only in `location /api/v1/auth`
   (in `api_locations.conf`, so on both listeners). A second zone,
   **`limit_req_zone $binary_remote_addr zone=public_zone:10m rate=5r/s;`**,
   is used only in `location /api/v1/public`. It stops a client from
   trying many fake API keys, because each unknown key costs a database
   lookup before core-service's per-key limit applies. A real client
   uses at most 60 requests a minute per owner, so this limit never
   slows it down.
7. **`map $http_upgrade $connection_upgrade`**: `Connection: upgrade` when
   the client sent an `Upgrade` header, `Connection: close` otherwise.
   Used by `/ws` and `/grafana/`.
8. **`map $http_host $proxy_host_header`**: the `Host` header forwarded to
   the backend is the client's own `Host` *with the port* (for example
   `localhost:8443`). Plain `$host` drops the port, and URLs the backends
   build (FastAPI's trailing-slash redirects, Grafana's origin check at
   login) would then point at `https://localhost/` (port 443) instead of
   the published port.
9. **Security headers** (all with `always`, so they are also on error
   answers):

   | Header | Value |
   |---|---|
   | `Strict-Transport-Security` | `max-age=31536000` (one year) |
   | `X-Content-Type-Options` | `nosniff` |
   | `X-Frame-Options` | `$frame_options`: `SAMEORIGIN` for paths matching `^/grafana(/|$)`, `DENY` otherwise |
   | `Referrer-Policy` | `strict-origin-when-cross-origin` |
   | `Content-Security-Policy` | `$content_security_policy`: a strict policy for `/status` and `/status.html`, empty (header not sent) for every other path |

   They are set only at `http` level, on purpose. nginx has a surprising
   rule: if a `server` or `location` block contains *any* `add_header`, it
   inherits *none* of the outer ones. So per-path differences are made
   with `map` (a lookup table from a variable to a value), and an empty
   value means "do not send this header".

   About HSTS on a self-signed certificate: browsers ignore HSTS on plain
   HTTP and on connections with certificate errors (RFC 6797 section 8.1).
   So the development certificate does not lock `localhost` to HTTPS in
   your browser unless you explicitly trusted the certificate.

10. **Proxy headers for every proxied location**:

    ```
    proxy_set_header Host              $proxy_host_header;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $proxy_host_header;
    proxy_hide_header X-Frame-Options;
    proxy_hide_header X-Content-Type-Options;
    proxy_hide_header Strict-Transport-Security;
    ```

    `X-Forwarded-For` is **replaced** with the peer address, never
    appended to. This is a security decision, explained in section 5.
    `proxy_hide_header` removes the backends' own copies of the security
    headers, so the gateway's values are the only ones the client sees
    (otherwise the header would appear twice, possibly with conflicting
    values). Other client headers pass through unchanged. The gateway
    neither creates nor removes `X-Request-ID`: a client's own id reaches
    the service (which keeps it only when it is a short safe token), and
    the service's `X-Request-ID` answer header reaches the client
    ([chapter 01](01-architecture.md), section 3.6b).

    Like `add_header`, `proxy_set_header` has the "set one, lose all
    inherited" rule. That is why `/ws` and `/grafana/`, which must add
    `Upgrade` and `Connection`, repeat all five headers. The internal
    listener repeats them too, with `X-Forwarded-Proto https`.
    `proxy_hide_header` follows the same rule separately: `/ws` sets no
    `proxy_hide_header` of its own, so it still inherits the three from
    the `http` level; `/grafana/` repeats them only for readability.
    Nothing is missing in `/ws`.

11. **`map $host $external_https_port { default 8443; }`** nginx runs inside
    the container and cannot know which host port Docker published.
    The value is written here and must match `"8443:443"` in
    `docker-compose.yaml` (change both together). It is a `map`, not a
    `set`, so it is also available to `error_page 497` (which runs before
    nginx's rewrite phase, where `set` would run).

    The public URL `https://localhost:8443` is also written in these
    places, which must all change when the stack is served under another
    host or port (chapter 01, section 3.2 has the same list):

    | Place | Value |
    |---|---|
    | `docker-compose.yaml`, gateway `ports` | `"8443:443"` |
    | `infra/gateway/nginx.conf`, `map $host $external_https_port` | `8443` |
    | `docker-compose.yaml`, grafana `GF_SERVER_ROOT_URL` | `${OAUTH_REDIRECT_BASE_URL:-https://localhost:8443}/grafana/` (follows `OAUTH_REDIRECT_BASE_URL`) |
    | `.env.docker` / `.env.example`, `OAUTH_REDIRECT_BASE_URL` (default in `services/auth-service/config.py`) | `https://localhost:8443`; also the callback URLs registered with Google and GitHub |
    | `scripts/lib.sh`, `GATEWAY_URL` (used by `scripts/tiers.sh` for the decline call) | default `https://localhost:8443`, overridable by the environment |
    | `services/ai-service/scripts/upload_seed_documents.py`, `--base-url` default | `https://localhost:8443` (overridable) |
    | `.github/workflows/integration-ci.yml` | health and route checks against `https://localhost:8443` |
    | `infra/gateway/gen-certs.sh` | certificate names `localhost` and `127.0.0.1` (the host, not the port) |

### 3.3 The three servers

**Port 80 (published as 8000):**

```
server {
    listen 80;
    return 301 https://$host:$external_https_port$request_uri;
}
```

Every request, whatever the path, gets a 301 to the same path and query on
`https://<host>:8443`. `$host` is the host name without port, so
`http://localhost:8000/status` becomes `https://localhost:8443/status`.

**Port 443 (published as 8443):**

- `listen 443 ssl;` with HTTP/2 left off on purpose: WebSocket upgrades
  (`/ws`, Grafana Live) use HTTP/1.1, and the `http2` directive syntax
  differs between nginx 1.24 and 1.27.
- Certificate and key: `/etc/nginx/certs/server.crt`,
  `/etc/nginx/certs/server.key`.
- `ssl_protocols TLSv1.2 TLSv1.3;`
- `ssl_ciphers`: the Mozilla "intermediate" list for TLS 1.2 (ECDHE and DHE
  key exchange with AES-GCM or CHACHA20-POLY1305). TLS 1.3 suites are not
  configurable here and are all strong.
- `ssl_prefer_server_ciphers off;` (the client may pick its preferred
  suite; every suite on the list is strong),
  `ssl_session_cache shared:SSL:10m;` (a 10 MB cache shared by all
  workers, so a returning client can resume a TLS session without a full
  handshake), `ssl_session_timeout 1d;`, `ssl_session_tickets off;`.
- `error_page 497 =301 https://$host:$external_https_port$request_uri;`
  Status 497 is nginx's internal code for "plain HTTP was sent to an HTTPS
  port". Instead of an error page, the client gets a redirect to HTTPS.
- Backend names are stored in variables:

  ```
  # api_locations.conf (both listeners)
  set $auth_service     auth-service:8000;
  set $core_service     core-service:8000;
  set $ai_service       ai-service:8000;
  # HTTPS server only
  set $grafana_service  grafana:3000;
  set $frontend_service frontend:3000;
  ```

**Port 8080 (internal, not published):**

The frontend's server side (pages, Server Actions, route handlers) calls
`BACKEND_API_URL=http://gateway:8080/api/v1`. Only containers on the
Docker network reach this port. Connections inside the backend may be
unencrypted; every browser connection still uses the HTTPS server. Plain
HTTP here means the frontend needs no certificate that names `gateway`
(the cloud certificate names only the public domain).

```
server {
    listen 8080;
    set_real_ip_from 10.0.0.0/8;
    set_real_ip_from 172.16.0.0/12;
    set_real_ip_from 192.168.0.0/16;
    real_ip_header X-Forwarded-For;
    proxy_set_header X-Forwarded-Proto https;   # plus the other four
    include /etc/nginx/api_locations.conf;
    location / { return 404; }
}
```

- The HTTPS server sets `X-Real-IP` to the browser's address. The frontend
  copies it into `X-Forwarded-For` on its calls
  (`frontend/app/lib/backend.ts`, and the token refresh in
  `frontend/proxy.ts`).
- This listener trusts that header, and only from private Docker
  addresses, so `$remote_addr` becomes the browser's address. The gateway
  `auth_zone` limit, auth-service's per-IP limits and the session records
  then see each user, not the frontend container.
- It forwards `X-Forwarded-Proto https`, because the browser's own
  connection was HTTPS.
- Everything except the included API and health locations is `404`:
  no pages, no `/ws`, no `/internal/*`, no `/metrics`.

### 3.4 Why variables in `proxy_pass` (the stale-IP problem)

nginx resolves a plain host name in `proxy_pass http://auth-service:8000`
(or in a static `upstream {}` block) **once**, when the worker process
starts, and keeps that IP address for the worker's whole life. Docker gives
a container a new IP address when it is recreated. During development you
restart one service at a time. A gateway that keeps the old IP can reach
a *different* container, because Docker may hand that old IP to it.
Then `/health/ai`, `/health/auth` and `/health/core` answer from the
wrong services: no error, just the wrong service, which is hard to
diagnose from outside.

The configuration avoids this in two parts:

1. `resolver 127.0.0.11 valid=10s` tells nginx where Docker's DNS is and
   to cache answers for 10 seconds.
2. `proxy_pass http://$auth_service;` uses a **variable**. When
   `proxy_pass` contains a variable, nginx resolves the name at request
   time through `resolver`. Variables are request-scoped even when `set` at
   server level, so each request re-evaluates them.

There are no `upstream {}` blocks for the same reason: static upstreams do
not support this re-resolution.

One side effect of a variable in `proxy_pass` matters for the Swagger and
health aliases: when `proxy_pass` contains a variable *and* a URI part
(for example `http://$auth_service/health`), nginx sends exactly that URI
to the backend, replacing the client's path. That is what makes
`/health/auth` arrive at auth-service as `/health`.

### 3.5 Every location block

The order below is the order in `nginx.conf`. nginx does not pick the
first match; it picks exact matches (`=`) first, then the longest prefix.
The API locations of auth-service, core-service and ai-service and the
health aliases are in `api_locations.conf`, which both the HTTPS server
and the internal listener include. `/static/avatars`, `/ws`, `/status`,
Grafana and the Swagger locations are on the HTTPS server only.

**Refused (404) at the gateway:**

| Location | Answer | Why |
|---|---|---|
| `location ^~ /internal/` | `return 404` | Service-to-service routes (see 3.7). `^~` means no regex location can override it. |
| `location = /internal` | `return 404` | The bare path without slash. |
| `location = /metrics` | `return 404` | Prometheus scrapes each service's `/metrics` directly inside the Docker network. |

**Frontend (`$frontend_service`):**

| Location | Behaviour |
|---|---|
| `location /` | `proxy_buffering off; proxy_read_timeout 180s; proxy_pass http://$frontend_service;` The catch-all: every path not matched above is a frontend page (including `/healthz`). Unknown pages get the frontend's 404 page. `proxy_buffering off` lets streamed pages reach the browser chunk by chunk; the 180 s read timeout lets the streamed document answer (frontend route `/research/ask/stream`, up to 120 s in the backend) finish. |

**Static:**

| Location | Behaviour |
|---|---|
| `location = /status` | `root /usr/share/nginx/status; try_files /status.html =404; default_type text/html; charset utf-8; expires -1;` No backend is called by nginx. `expires -1` makes the answer not cacheable. `/status.html` itself is *not* a location, so it is a 404 from outside. |

**auth-service (`$auth_service`):**

| Location | Notes |
|---|---|
| `/api/v1/auth` | `limit_req zone=auth_zone burst=20 nodelay;` 10 requests/second per client address, a burst of 20 extra requests served immediately, then 429. |
| `/api/v1/organizations` | organizations and invitations |
| `/api/v1/connections` | friends (connections) |
| `/api/v1/admin` | user administration and site statistics |
| `/static/avatars` | avatar image files (served by auth-service's `StaticFiles` mount) |

**core-service (`$core_service`):**

| Location | Notes |
|---|---|
| `/api/v1/portfolios` | portfolios, holdings, transactions, snapshots, CSV exports |
| `/api/v1/watchlists` | watchlists |
| `/api/v1/api-keys` | API key management (JWT) |
| `/api/v1/research` | notes, comments, documents, sentiment summary |
| `/api/v1/notifications` | notifications |
| `/api/v1/public` | the public API (authenticated by `X-API-Key`). `limit_req zone=public_zone burst=20 nodelay;` 5 requests/second per client address, a burst of 20, then 429. |
| `/api/v1/moderation` | hide, unhide and delete of notes and comments |
| `/ws` | WebSockets: `proxy_http_version 1.1`, `Upgrade $http_upgrade`, `Connection $connection_upgrade`, the five proxy headers repeated, `proxy_read_timeout 3600s` |

**ai-service (`$ai_service`):**

| Location | Notes |
|---|---|
| `/api/v1/analyses`, `/api/v1/market`, `/api/v1/analytics`, `/api/v1/decisions`, `/api/v1/backtests` | plain proxy |
| `/api/v1/rag` | `proxy_buffering off; proxy_cache off; proxy_read_timeout 180s;` |

`/api/v1/rag/ask/stream` is a Server-Sent Events stream. With nginx's
default buffering, tokens would collect in the gateway and reach the
browser in large lumps. With `proxy_buffering off` each chunk is forwarded
as it arrives. The 180-second read timeout (default 60 s) lets a long
answer finish. ai-service also sends `X-Accel-Buffering: no` (see
chapter 17).

**Grafana (`$grafana_service`):**

| Location | Notes |
|---|---|
| `location = /grafana` | `return 301 /grafana/;` (relative, because of `absolute_redirect off`) |
| `location /grafana/` | `proxy_http_version 1.1`, upgrade headers (Grafana Live uses WebSockets), the five proxy headers repeated, and `proxy_hide_header` for the three security headers repeated for clarity |

Grafana is configured to live under the sub-path
(`GF_SERVER_ROOT_URL: ${OAUTH_REDIRECT_BASE_URL:-https://localhost:8443}/grafana/`,
`GF_SERVER_SERVE_FROM_SUB_PATH: "true"` in `docker-compose.yaml`), so
nginx passes the `/grafana` prefix through unchanged. Anonymous access and
sign-up are off (`GF_AUTH_ANONYMOUS_ENABLED: "false"`,
`GF_USERS_ALLOW_SIGN_UP: "false"`); you log in with the admin password from
`GRAFANA_ADMIN_PASSWORD`. Grafana has no host port of its own.

**Swagger UI and OpenAPI documents:**

| Location | Sent to |
|---|---|
| `/docs/auth/` | `http://$auth_service/api/docs` |
| `/docs/core/` | `http://$core_service/api/docs` |
| `/docs/ai/` | `http://$ai_service/api/docs` |
| `= /api/openapi-auth.json` | auth-service |
| `= /api/openapi-core.json` | core-service |
| `= /api/openapi-ai.json` | ai-service |

Why three different JSON names: the Swagger page fetches the OpenAPI
document from the URL in FastAPI's `openapi_url`, and that URL is
absolute (from the gateway root), not relative to `/docs/auth/`. One
gateway location can only send a path to one backend, so if two services
used the same `openapi_url` (FastAPI's default is `/openapi.json`),
one of their Swagger pages would hang forever. Each service therefore has
a unique `openapi_url` (`main.py` of each service:
`openapi_url="/api/openapi-auth.json"` and so on). Keep it unique when a
service is added.

**Health aliases:**

| Location | Sent to |
|---|---|
| `= /health/auth` | `http://$auth_service/health` |
| `= /health/core` | `http://$core_service/health` |
| `= /health/ai` | `http://$ai_service/health` |

Each `/health` answers 200 with `"status": "ok"` when all its dependencies
are reachable, and 503 with `"status": "degraded"` otherwise. The
dependencies are: auth `database`, `redis`; core `database`, `redis`,
`storage`; ai `database`, `redis`, `qdrant`, `storage` (each service's
`main.py`, function `health`).

### 3.6 The public status page (`infra/gateway/status.html`)

The page is one self-contained HTML file: no external resources, no inline
event handlers, no `style=` attributes. Its single `<script>` block:

- calls `/health/auth`, `/health/core` and `/health/ai` with
  `fetch(..., {cache: "no-store"})`;
- aborts a call after `TIMEOUT_MS = 8000`;
- refreshes every `REFRESH_MS = 15000` (15 seconds);
- shows each service as "Operational", "Degraded" (an answer, but not 200
  with status ok) or "Down" (no answer), and lists each dependency
  (`database`, `redis`, ...) as up or down;
- shows "All systems operational" only when every service is ok.

The CSP sent for `/status` (and `/status.html`, because `try_files`
rewrites `$uri` to `/status.html` before headers are sent, so the map
matches both spellings):

```
default-src 'none';
script-src 'sha256-2bwQFs4FJqRRCcoHEdIxrH9Ivb20j84jwZsAaZZICpM=';
style-src  'sha256-UhRAusrh4IV8C/aXR7OqIYTQUf+lU6fWGdtaucwCyME=';
connect-src 'self'; img-src 'self' data:; base-uri 'none';
form-action 'none'; frame-ancestors 'none'
```

A hash source means: "run this inline script only if the SHA-256 of its
exact text equals this value". Change one character in the script and the
browser silently refuses to run it. `connect-src 'self'` lets the script
call `/health/*` on the same origin and nothing else.

**`infra/gateway/csp_hashes.py`** keeps the two hashes in sync:

| Command (from the repo root) | Effect |
|---|---|
| `python3 infra/gateway/csp_hashes.py` | prints the two hash sources |
| `python3 infra/gateway/csp_hashes.py --write` | rewrites the `script-src` and `style-src` hashes in `nginx.conf` (this edits a file) |
| `python3 infra/gateway/csp_hashes.py --check` | exit code 1 when `nginx.conf` is out of date |

How it works: it removes HTML comments first (the page's header comment
mentions the tag names), requires exactly one `<script>` and one `<style>`
block, hashes each block body with SHA-256, base64-encodes the digest and
substitutes it with regular expressions. The CI workflow
`.github/workflows/integration-ci.yml` runs `--check`.

Why the CSP is only on `/status`: the API returns JSON (a CSP does nothing
for it), and Swagger UI and Grafana ship their own inline scripts, which a
strict CSP would break.

### 3.7 Why `/internal/*` is not proxied

Every service has routes under `/internal/...` for other services and for
the scheduler: for example auth-service `GET /internal/users/{id}/state`,
core-service `POST /internal/scheduler/market-close`,
`POST /internal/portfolios/{id}/transactions`. These routes trust their
caller. Several have no ownership check at all, because the caller is
another service acting for a user it already checked (see chapter 12,
section 3.13).

So they are protected in two layers:

1. **Not reachable from outside.** The gateway answers 404 for
   `/internal` and everything under `/internal/`. The internal listener
   answers 404 for everything except the API and health locations. There
   is no host port on any service, so the gateway is the only way in.
2. **A shared secret inside the network.** Every `/internal` router has
   `dependencies=[Depends(require_internal_token)]`
   (`shared/security.py`). It compares the `X-Internal-Token` header with
   `INTERNAL_API_TOKEN` in constant time (`hmac.compare_digest`) and
   answers 401 on a missing or wrong token. An empty token is allowed only
   when `APP_ENV` is `development` or `test`; any other `APP_ENV` refuses
   to start without it (`shared/config.py`,
   `BaseServiceSettings._check_internal_token`). The services' own HTTP
   clients add the header automatically for `/internal/` calls to the
   other services (`shared/http_client.py`, `_add_internal_token`), and
   the scheduler scripts send it with `curl -H "X-Internal-Token: ..."`.

The second layer is "defence in depth": it protects against anything else
on the Docker network (a compromised container, a mistake in a future
gateway change).

### 3.8 Self-signed certificates (`infra/gateway/gen-certs.sh`)

```
sh infra/gateway/gen-certs.sh             # create if missing
sh infra/gateway/gen-certs.sh --force     # always regenerate
sh infra/gateway/gen-certs.sh --out DIR   # write somewhere else
```

What the script does, step by step:

1. Default output directory: `infra/gateway/certs` (next to the script).
   `DAYS=825`.
2. **Idempotent**: if `server.crt` and `server.key` both exist and are not
   empty, it prints "already exists (use --force to regenerate)" and exits
   0. `--force` skips this check.
3. Checks that `openssl` is installed.
4. Writes a temporary OpenSSL config file (removed by a `trap` on exit)
   with:
   - subject `CN = localhost`, `O = AlphaMind development`;
   - `basicConstraints = critical, CA:FALSE` (it cannot sign other
     certificates);
   - `keyUsage = critical, digitalSignature, keyEncipherment`;
   - `extendedKeyUsage = serverAuth`;
   - `subjectAltName = DNS:localhost, IP:127.0.0.1`.
   A config file is used instead of `-addext` so it also works with the
   LibreSSL that ships as `/usr/bin/openssl` on macOS.
5. `umask 077`, then
   `openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825`: a new
   RSA 2048-bit key without a passphrase (`-nodes`) and a self-signed
   certificate, written to `server.key.tmp` and `server.crt.tmp` first. On
   failure both temporary files are deleted, so a half-written pair never
   passes the idempotency check next time.
6. Moves the temporary files into place, `chmod 644 server.crt` (public),
   `chmod 600 server.key` (owner only; nginx's master process reads it as
   root before dropping privileges).

The certificate does not name `gateway`: the frontend reaches the API
on the internal plain-HTTP listener (section 3.3), not over TLS.

`make certs` runs the script, and `make up` depends on `certs`, so a fresh
checkout gets a certificate automatically (`Makefile`). The certificate
files are not committed (`infra/gateway/certs/README.md`: everything in
that directory except the README is git-ignored).

After `--force` the running gateway still has the old certificate in
memory. Restart it: `make rebuild S=gateway RESTART_ONLY=1` (or
`docker compose --env-file .env.docker restart gateway`).

### 3.9 uvicorn `--proxy-headers --forwarded-allow-ips=*`

Every service starts uvicorn with:

```
uvicorn main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
```

(the `CMD` of both stages in `services/*/Dockerfile`; the development
override does not set `command`).

- `--proxy-headers` tells uvicorn to read `X-Forwarded-For` and
  `X-Forwarded-Proto` and to set `request.client.host` and
  `request.url.scheme` from them.
- `--forwarded-allow-ips=*` says which proxies to trust: all of them.

Without these flags, every request in auth-service would appear to come
from the gateway's container address. auth-service's per-IP limits
(`security.py::client_ip`, used for register, login and password reset)
would then put every user in the world in one bucket: ten registrations
per hour for everyone together.

**Why trusting every forwarder is safe here:**

1. The services publish no host port. The only path from outside to a
   service is through the gateway.
2. The gateway **replaces** `X-Forwarded-For` with `$remote_addr` (the
   address of the TCP peer that connected to nginx). Whatever the client
   wrote in its own `X-Forwarded-For` header is thrown away.
3. uvicorn, trusting all forwarders, uses the left-most address in
   `X-Forwarded-For`. Because the header has exactly one entry, written by
   the gateway, that entry is the real peer.

The order of items 2 and 3 is the important lesson. If the gateway
*appended* to the header (nginx's usual `$proxy_add_x_forwarded_for`), a
client could send `X-Forwarded-For: 1.2.3.4` and nginx would produce
`1.2.3.4, <real address>`. uvicorn takes the left-most entry, so the
client would choose its own address: it could dodge every per-IP limit
in auth-service or aim a lockout at someone else's address. Replacing
the header closes that hole.

The internal listener is the one place where a forwarded address is
kept: `set_real_ip_from` trusts `X-Forwarded-For` from private Docker
addresses only, and nginx then writes the restored address as the single
`X-Forwarded-For` entry. A browser cannot reach that listener; on the
HTTPS server, `X-Real-IP` is always overwritten with the real peer, so
the address the frontend passes on cannot be chosen by the client.

The remaining trust is "anything on the Docker network can claim any
address". That is the same trust boundary as `/internal/*`, and it is
accepted for this deployment.

---

## 4. Configuration

| Setting | Value / default | Meaning | Where |
|---|---|---|---|
| HTTPS host port | `8443` (`"8443:443"`) | public HTTPS port | `docker-compose.yaml` (gateway `ports`) |
| HTTP host port | `8000` (`"8000:80"`) | redirect-only port | `docker-compose.yaml` |
| Internal listener | `8080`, plain HTTP, not published | API and health routes for the frontend's server side | `nginx.conf` (third `server`) |
| `set_real_ip_from` / `real_ip_header` | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` / `X-Forwarded-For` | trusted forwarders on the internal listener only | `nginx.conf` |
| `BACKEND_API_URL` (frontend) | `http://gateway:8080/api/v1` | where the frontend's server side sends API calls | `docker-compose.yaml` (frontend) |
| `$external_https_port` | `8443` | port written into redirects; keep equal to the HTTPS host port | `infra/gateway/nginx.conf` (`map $host ...`) |
| `client_max_body_size` | `25m` | largest request body the gateway forwards | `nginx.conf` |
| `resolver` | `127.0.0.11 valid=10s ipv6=off` | Docker DNS, 10 s cache | `nginx.conf` |
| `limit_req_zone` | `rate=10r/s`, zone `auth_zone:10m`, key `$binary_remote_addr` | login API rate | `nginx.conf` |
| `limit_req` (auth) | `burst=20 nodelay`, status 429 | burst allowance | `nginx.conf`, `location /api/v1/auth` |
| `limit_req_zone` (public) | `rate=5r/s`, zone `public_zone:10m`, key `$binary_remote_addr` | public API rate per client address | `nginx.conf` |
| `limit_req` (public) | `burst=20 nodelay`, status 429 | burst allowance | `api_locations.conf`, `location /api/v1/public` |
| `worker_processes` / `worker_connections` | `auto` / `1024` | one worker per CPU; connections per worker (client and upstream sides) | `nginx.conf` (top level) |
| `proxy_read_timeout` `/ws` | `3600s` | an idle WebSocket closes after one hour without traffic | `nginx.conf` |
| `proxy_read_timeout` `/api/v1/rag` | `180s` | long streamed answers | `nginx.conf` |
| `proxy_read_timeout` `/` (frontend) | `180s` | the streamed document answers through the frontend route | `nginx.conf` |
| other proxied paths | nginx default (60 s read timeout) | | |
| `ssl_protocols` | `TLSv1.2 TLSv1.3` | | `nginx.conf` |
| HSTS | `max-age=31536000` | one year | `nginx.conf` |
| Certificate validity | `825` days (`DAYS`) | | `infra/gateway/gen-certs.sh` |
| Certificate names | `DNS:localhost, IP:127.0.0.1` | SAN | `gen-certs.sh` |
| Key | RSA 2048, SHA-256 signature, no passphrase | | `gen-certs.sh` |
| `REFRESH_MS`, `TIMEOUT_MS` | `15000`, `8000` | status page refresh and per-call timeout | `infra/gateway/status.html` |
| `GF_SERVER_ROOT_URL` | `${OAUTH_REDIRECT_BASE_URL:-https://localhost:8443}/grafana/` | Grafana's public URL, derived from the public base URL | `docker-compose.yaml` (grafana) |
| `GF_SERVER_SERVE_FROM_SUB_PATH` | `"true"` | Grafana serves under `/grafana/` | `docker-compose.yaml` |
| `INTERNAL_API_TOKEN` | empty (must be set outside development/test) | `X-Internal-Token` secret for `/internal/*` | `.env.example`, `shared/config.py` |
| `OAUTH_REDIRECT_BASE_URL` | `https://localhost:8443` | public base URL used in OAuth redirect URIs (must match the gateway) | `services/auth-service/config.py`, `.env.example` |
| uvicorn flags | `--proxy-headers --forwarded-allow-ips=*` | trust `X-Forwarded-*` | `services/*/Dockerfile` |

---

## 5. Design decisions and why

**One gateway with an allow-list, not "proxy everything to the right
service".**
*Alternative:* a few broad rules such as "everything under `/api/` to the
service that owns it" plus a pass-through default.
*Reason:* with an allow-list, a new internal or debug route is private by
default. The catch-all goes to the frontend, which has no route to any
backend path, so `/internal/*`, `/metrics` and each service's raw
`/health` and `/api/docs` stay unreachable from outside. The explicit
`/internal` and `/metrics` 404 blocks document the intent.

**An internal plain-HTTP listener for the frontend's server side.**
*Alternative:* the frontend calls `https://gateway` with a certificate
that names `gateway` and a trusted CA in Node (`NODE_EXTRA_CA_CERTS`).
*Reason:* the cloud certificate (Let's Encrypt) names only
`alphamind-research.duckdns.org`, so `https://gateway` fails TLS there.
Connections inside the backend may be unencrypted; every browser
connection is HTTPS. One shared `api_locations.conf` keeps both listeners
routing the same paths the same way. For local `npm run dev` the frontend
still uses `https://localhost:8443/api/v1` with `NODE_EXTRA_CA_CERTS`
(`frontend/.env.example`).

**TLS at the gateway, plain HTTP inside.**
*Alternative:* TLS on every service.
*Reason:* one certificate, one place to configure ciphers, and the internal
network is private to Docker. The services learn the original scheme from
`X-Forwarded-Proto`.

**Port 80 only redirects.**
*Alternative:* serve the API on both ports.
*Reason:* tokens and passwords must never travel unencrypted. A client that
types `http://` gets sent to HTTPS before anything else happens. A plain
HTTP request sent to the HTTPS port (status 497) is redirected too.

**`$external_https_port` written in the config.**
*Alternative:* `$server_port` (it would be 443, the container port) or an
absolute redirect.
*Reason:* nginx cannot see Docker's port mapping. A hard-coded map value is
simple and visible; the price is that it must be changed together with the
compose file.

**Variables in `proxy_pass` plus `resolver`.**
*Alternative:* `upstream` blocks, or restarting the gateway after every
backend restart.
*Reason:* the stale-IP problem in section 3.4. Re-resolving every 10
seconds costs almost nothing.

**Replace `X-Forwarded-For`, do not append.**
*Alternative:* the common `$proxy_add_x_forwarded_for`.
*Reason:* the gateway is the edge; there is no trusted proxy in front of
it whose entries should be kept. Appending would let a client choose its
own address for auth-service's per-IP limits (section 3.9). If a trusted
load balancer is ever put in front of the gateway, this decision must be
revisited (nginx's `real_ip` module would then be the right tool). The
`real_ip` module is used today only on the internal listener, where the
trusted forwarder is the frontend.

**Security headers only at `http` level, per-path values with `map`.**
*Alternative:* `add_header` inside each location.
*Reason:* nginx drops all inherited `add_header` lines as soon as a block
has one of its own. One misplaced `add_header` would have silently removed
HSTS and friends from a whole location.

**Strict CSP only on the status page, with hashes.**
*Alternative:* `'unsafe-inline'`, or a CSP on every path.
*Reason:* `'unsafe-inline'` would allow any injected script. Hashes allow
exactly the shipped script and style. Other paths are JSON or ship their
own inline code (Swagger, Grafana). The hash-sync script and the CI check
stop the classic failure ("someone edited the page and it went blank").

**Access log without query strings.**
*Alternative:* the default `combined` log format.
*Reason:* access tokens in `?token=` (WebSockets) and OAuth `?code=` would
otherwise be written to disk and kept 14 days by the log pipeline.

**Gateway rate limit on `/api/v1/auth` in addition to auth-service's own
limits.**
*Alternative:* rely on auth-service only.
*Reason:* defence in depth: the gateway drops a flood before it reaches
Python or Redis. The burst (20) is generous because behind Docker Desktop
all host traffic can arrive from one address, so a strict limit would hit
every local user at once.

**HTTP/2 off.**
*Reason:* WebSockets (`/ws`, Grafana Live) are proxied over HTTP/1.1, and
the `http2` directive syntax differs between nginx versions. There is no
measured need for HTTP/2 at this scale.

**Self-signed certificate generated locally, not committed.**
*Alternative:* a committed development certificate, or a local certificate
authority tool.
*Reason:* a committed private key is a bad habit even for development; a
script with no dependencies except `openssl` works on Linux and macOS.

---

## 6. When things go wrong

| Symptom | Likely cause | What to check |
|---|---|---|
| `curl: (7) Failed to connect to localhost port 8443` | the gateway is not running, usually because a backend is not healthy yet (`depends_on: condition: service_healthy`) | `make health`, or `docker compose --env-file .env.docker ps`; then `make service-logs S=auth-service` (or core / ai) |
| gateway container exits at start with `cannot load certificate "/etc/nginx/certs/server.crt"` | no certificate generated | `make certs` (or `sh infra/gateway/gen-certs.sh`), then start the gateway again |
| `502 Bad Gateway` on one route | that backend is down or restarting; nginx resolved the name but the connection failed | `curl -k https://localhost:8443/health/core` (or auth / ai); `make service-logs S=gateway` shows `connect() failed` lines |
| `502`/`504` right after restarting one backend | normally fixed by the 10-second resolver cache; wait 10 s | if it persists, `make rebuild S=gateway RESTART_ONLY=1` |
| `413 Request Entity Too Large` from nginx | body above 25 MB | the file is too big for the gateway; note that core-service would refuse anything above 20 MB anyway (422 from the service) |
| `429` on `/api/v1/auth/...` with an nginx HTML body | gateway limit (10 r/s, burst 20) | wait a second; this is not auth-service's 429 (auth-service answers JSON with `Retry-After`) |
| `429` with a JSON `{"error": {"code": "rate_limited", ...}}` body | auth-service's own per-email or per-IP limit (see chapter 11) | `Retry-After` header says how long |
| browser shows a certificate warning | the certificate is self-signed | expected; accept it for `localhost`, or trust the certificate in the OS keychain |
| `/status` page stays blank or "Checking..." | the inline script was edited and its hash no longer matches the CSP | browser console shows a CSP violation; run `python3 infra/gateway/csp_hashes.py --check`, then `--write` and restart the gateway |
| Swagger page at `/docs/core/` spins forever | the OpenAPI JSON path does not match a gateway location | each service's `main.py` `openapi_url` must be `/api/openapi-<service>.json` |
| redirect goes to `https://localhost/...` (no port) | a backend built an absolute URL from `Host` without port, or `$external_https_port` does not match the compose mapping | check `map $http_host $proxy_host_header` and `map $host $external_https_port` |
| WebSocket closes after exactly one hour of silence | `proxy_read_timeout 3600s` on `/ws` | clients should send pings or reconnect (the project's end-to-end script uses a 20 s ping interval) |
| RAG stream arrives in one lump at the end | buffering somewhere | `/api/v1/rag` must keep `proxy_buffering off` |
| every page is `502 Bad Gateway` | the `frontend` container is down or still starting | `docker compose --env-file .env.docker ps frontend`; `make service-logs S=frontend` |
| `/healthz` answers 503 | the frontend cannot reach `/health/auth` on the internal listener | `BACKEND_API_URL` must be `http://gateway:8080/api/v1`; check `curl -k https://localhost:8443/health/auth` |
| all users share one rate limit after logging in through the pages | the frontend did not pass `X-Forwarded-For`, or the internal listener lost its `real_ip` lines | `frontend/app/lib/backend.ts` (`clientIpHeaders`) and the third `server` in `nginx.conf` |
| all users seem to share one rate-limit bucket in auth-service | uvicorn started without `--proxy-headers`, so `request.client.host` is the gateway | check the running command: `docker compose --env-file .env.docker exec -T auth-service cat /proc/1/cmdline \| tr '\0' ' '` |

Useful log sources:

- `make service-logs S=gateway` (access log format `main_noargs`: client
  address, method, path without query, status, bytes, user agent,
  `rt=<seconds>`).
- The saved daily files: `bash scripts/logs.sh gateway grep ' 429 '`.

---

## 7. Demo walkthrough

All commands run from the repository root on the host. None of them change
data. `-k` tells curl to accept the self-signed certificate.

**Step 1: plain HTTP is only a redirect.**

```
curl -sS -o /dev/null -D - http://localhost:8000/status | head -n 5
```

The audience sees `HTTP/1.1 301 Moved Permanently` and
`Location: https://localhost:8443/status`. Say: "Port 8000 never serves
content; it only sends you to HTTPS on 8443. The port number in the
redirect comes from one line in nginx.conf."

Bonus: send plain HTTP to the HTTPS port.

```
curl -sS -o /dev/null -D - http://localhost:8443/status | head -n 5
```

Again a 301 to `https://localhost:8443/status` (nginx's internal status
497 turned into a redirect).

**Step 2: the status page and the security headers.**

```
curl -k -sS -o /dev/null -D - https://localhost:8443/status
```

Point out: `Strict-Transport-Security: max-age=31536000`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`, the long
`Content-Security-Policy` with two `sha256-...` sources, and a `Server`
header that says only `nginx` (no version). Then open
`https://localhost:8443/status` in a browser: three cards, one per
service, with their dependencies, refreshed every 15 seconds.

Show that the hashes match the page:

```
python3 infra/gateway/csp_hashes.py --check
```

Expected: `status.html CSP hashes are up to date`.

**Step 3: a health route through the gateway.**

```
curl -k -sS https://localhost:8443/health/core | python3 -m json.tool
```

Expected:

```
{
    "status": "ok",
    "service": "core-service",
    "database": true,
    "redis": true,
    "storage": true
}
```

Say: "The gateway maps `/health/core` to core-service's own `/health`.
The raw `/health` of a service is not reachable from outside."

The frontend's own check goes through the internal listener:

```
curl -k -sS https://localhost:8443/healthz
```

Expected: `{"status":"ok"}`. A 503 means the frontend cannot reach the
API.

**Step 4: `/internal/...` and `/metrics` are refused.**

```
curl -k -sS -o /dev/null -w '%{http_code}\n' \
  https://localhost:8443/internal/users/00000000-0000-0000-0000-000000000000/state
curl -k -sS -o /dev/null -w '%{http_code}\n' https://localhost:8443/metrics
curl -k -sS -o /dev/null -w '%{http_code}\n' https://localhost:8443/anything-else
```

All three print `404`. Say: "`/internal/*` and `/metrics` are 404 at the
gateway; an unknown path is the frontend's 404 page. The internal routes
exist, but only inside the Docker network."

Then show the second layer, inside the network but without the secret:

```
docker compose --env-file .env.docker exec -T core-service python -c \
  "import httpx; r = httpx.get('http://auth-service:8000/internal/users/00000000-0000-0000-0000-000000000000/state'); print(r.status_code, r.text)"
```

With `INTERNAL_API_TOKEN` set, this prints
`401 ... Missing or invalid internal token`. (A plain `httpx.get` does not
add the header; the services' own client in `shared/http_client.py` does.)

**Step 5: the access log does not keep query strings.**

```
curl -k -sS -o /dev/null "https://localhost:8443/health/core?secret=should-not-appear"
docker compose --env-file .env.docker logs --tail 3 gateway
```

The log line shows `GET /health/core HTTP/1.1 200 ...` without
`?secret=...`. Say: "This is why WebSocket tokens and OAuth codes never
reach our log files."

**Step 6: the gateway rate limit on the auth API.**

```
for i in $(seq 1 40); do
  curl -k -sS -o /dev/null -w '%{http_code} ' https://localhost:8443/api/v1/auth/me
done; echo
```

Expected: a run of `401` (no token; auth-service answered), then `429`
once the burst of 20 is used up faster than 10 per second refills it. If
your machine is slow, all 40 may be `401`; that also proves the limit is
generous. This does not write anything (`/me` without a token does not
reach the database).

**Step 7: inspect the certificate.**

From the live server:

```
openssl s_client -connect localhost:8443 -servername localhost </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -fingerprint -sha256
```

The audience sees `subject` and `issuer` both containing `CN = localhost`
and `O = AlphaMind development` (self-signed: subject equals issuer), and
`notAfter` about 825 days after `notBefore`.

The names it is valid for:

```
openssl x509 -in infra/gateway/certs/server.crt -noout -text \
  | grep -A1 "Subject Alternative Name"
```

Expected: `DNS:localhost, IP Address:127.0.0.1`.

The negotiated protocol:

```
openssl s_client -connect localhost:8443 -servername localhost </dev/null 2>/dev/null \
  | grep -E "Protocol|Cipher"
```

Expected: TLSv1.3 with a strong suite. Try to force an old protocol to
show it is refused:

```
openssl s_client -connect localhost:8443 -tls1_1 </dev/null
```

It fails the handshake (your local OpenSSL may also refuse TLS 1.1 on its
own side, which proves the same point).

**Step 8 (optional): regenerating the certificate.** This replaces the
files in `infra/gateway/certs/` (it writes data):

```
sh infra/gateway/gen-certs.sh            # prints "already exists" -- idempotent
sh infra/gateway/gen-certs.sh --force    # new key and certificate
make rebuild S=gateway RESTART_ONLY=1    # nginx loads the new pair
```

`make rebuild` refuses to run inside the scheduler's busy windows
(09:00-10:30 and 16:55-17:50 New York, about 14:00-15:30 and
21:55-22:50 London) unless `FORCE=1`.

---

## 8. Check your understanding

1. Why does `http://localhost:8000/x` redirect to port 8443 and not to 443?
2. What would happen if someone added `add_header X-Test 1;` inside
   `location /api/v1/research`?
3. Why does `proxy_pass` use `$core_service` instead of
   `core-service:8000`?
4. A client sends `X-Forwarded-For: 10.0.0.9`. Which address does
   auth-service see as `request.client.host`, and why?
5. Why do `/ws` and `/grafana/` repeat all five `proxy_set_header` lines?
6. Which two layers stop an outsider from calling
   `POST /internal/scheduler/market-close`?
7. You edit one word in the `<script>` of `status.html`. What breaks, and
   which command fixes it?
8. Why do `/api/v1/rag` and the frontend `location /` turn buffering off?
9. Why does the access log use `$uri` instead of `$request`?
10. How long is the development certificate valid and for which names?
11. Why does the frontend call `http://gateway:8080` and not
    `https://gateway`?

<details>
<summary>Answers</summary>

1. nginx inside the container cannot see Docker's port mapping; the
   redirect uses `$external_https_port`, a `map` hard-coded to 8443 to
   match `"8443:443"`.
2. That location would stop inheriting every `http`-level `add_header`:
   no HSTS, no `nosniff`, no `X-Frame-Options`, no CSP on its answers.
3. A variable forces nginx to resolve the name at request time through
   `resolver 127.0.0.11 valid=10s`; a literal name is resolved once at
   worker start and can go stale when Docker recreates a container.
4. The real TCP peer address of the client. The gateway replaces
   `X-Forwarded-For` with `$remote_addr`; uvicorn then takes that single
   entry.
5. Setting any `proxy_set_header` in a block drops all inherited ones;
   both blocks must add `Upgrade` and `Connection` for WebSockets.
6. The gateway answers 404 for `/internal/*` (and services have no host
   ports); inside the network, `require_internal_token` demands the
   `X-Internal-Token` secret.
7. The script's SHA-256 no longer matches the CSP, so the browser refuses
   to run it and the page stays on "Checking...".
   `python3 infra/gateway/csp_hashes.py --write`, then restart the gateway.
8. `/api/v1/rag/ask/stream` sends Server-Sent Events, and the frontend
   streams its own answer page (`/research/ask/stream`); buffering would
   hold the tokens back. Both also need a longer read timeout (180 s).
9. `$request` contains the query string, which can hold a WebSocket access
   token or an OAuth code.
10. 825 days, for `DNS:localhost` and `IP:127.0.0.1`.
11. The cloud certificate names only the public domain, so TLS to the
    name `gateway` would fail. The internal listener is on the Docker
    network only, where unencrypted connections are allowed.

</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `infra/gateway/nginx.conf` | the main gateway configuration: TLS, redirects, routing, headers, rate limit, log format, the internal listener |
| `infra/gateway/api_locations.conf` | API and `/health/*` locations, included by the HTTPS server and the internal listener |
| `infra/gateway/gen-certs.sh` | creates the self-signed certificate and key (`--force`, `--out`) |
| `infra/gateway/certs/README.md` | what belongs in the (git-ignored) certificate directory |
| `infra/gateway/status.html` | the public status page (one inline script, one inline style) |
| `infra/gateway/csp_hashes.py` | computes, checks and writes the CSP hashes of the status page |
| `docker-compose.yaml` (service `gateway`, `grafana`) | image, mounts, ports, `depends_on`, Grafana sub-path settings |
| `docker-compose.override.yml` | development target, bind mounts and `LOG_LEVEL` for the services (the uvicorn flags are in the Dockerfile development `CMD`) |
| `services/*/Dockerfile` | production and development `CMD` with `--proxy-headers --forwarded-allow-ips=*` |
| `services/*/main.py` | each service's `/health` and unique `openapi_url` |
| `shared/security.py` | `require_internal_token` (the `/internal/*` guard) |
| `shared/http_client.py` | adds `X-Internal-Token` to service-to-service `/internal/` calls |
| `shared/config.py` | `INTERNAL_API_TOKEN` and its start-up check |
| `Makefile` | `make certs`, `make up` (depends on `certs`) |
| `.github/workflows/integration-ci.yml` | runs `csp_hashes.py --check`; the stack smoke test checks `/login` and `/healthz` through the gateway |
| `frontend/app/lib/backend.ts`, `frontend/proxy.ts` | pass the browser's address (`X-Real-IP`) as `X-Forwarded-For` to the internal listener |
| `frontend/app/healthz/route.ts` | `GET /healthz`: 200 only when the frontend reaches `/health/auth` through the internal listener |
| `tests/core-service/test_platform.py` | checks that `X-Forwarded-For` is replaced, never appended (4 places: http level, `/ws`, `/grafana/`, internal listener) |

---

## 10. Related chapters

- [01-architecture.md](01-architecture.md): where the gateway sits in the
  whole system; section 3.11 for the frontend.
- [02-operations.md](02-operations.md): starting the stack, `make up`,
  `make rebuild`.
- [11-auth-service.md](11-auth-service.md): the per-IP and per-email
  limits that depend on the forwarded client address.
- [12-core-service.md](12-core-service.md) and
  [25-realtime-yjs.md](25-realtime-yjs.md): the WebSocket endpoints behind
  `/ws`.
- [17-ai-rag.md](17-ai-rag.md): the streamed RAG answer behind
  `/api/v1/rag`.
- [27-observability.md](27-observability.md): Grafana behind `/grafana/`,
  `/metrics` scraping.
- [29-security.md](29-security.md): the full security picture.
- [../API_SPEC.md](../API_SPEC.md): section 1, base paths and gateway
  routing.
