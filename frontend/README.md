# AlphaMind frontend

The web interface of AlphaMind: a Next.js 16 app (React 19, TypeScript,
Tailwind CSS 4). Pages are server components that fetch their data on the
server; client components are used only where a page is interactive
(forms, live updates, the shared note editor).

## Run it

The frontend is part of the Docker stack. From the repository root:

```
make up
```

This builds the image from `Dockerfile` and starts it with the rest of the
stack. The gateway serves it at https://localhost:8443/ (the frontend has
no host port of its own). The image is a multi-stage build: `npm ci`,
`next build` with `output: "standalone"`, then only the standalone server
and static files run as an unprivileged user.

For development outside Docker (the rest of the stack still running in
Docker): copy `.env.example` to `.env.local`, then `npm ci` and
`npm run dev`. `npm run dev` trusts the gateway's self-signed certificate
through `NODE_EXTRA_CA_CERTS` (see `package.json`). Google and GitHub
sign-in work only when the page runs on the origin set in the backend's
`FRONTEND_BASE_URL` (`https://localhost:8443` in `.env.example`), because the
sign-in finishes there and its marker is kept per origin. To sign
in with OAuth from `npm run dev`, point `FRONTEND_BASE_URL` at
`http://localhost:3000`, or use the password login.

## Environment variables

| Variable | Used by | Docker value | Meaning |
|---|---|---|---|
| `BACKEND_API_URL` | server code (`app/lib/backend.ts`, `proxy.ts`, `app/healthz`) | `http://gateway:8080/api/v1` (set in `docker-compose.yaml`) | The API as the frontend server reaches it: the gateway's internal listener |
| `NEXT_PUBLIC_WS_URL` | browser (`app/lib/ws-url.ts`) | not set: the page's own origin is used (`wss://host:8443/ws`) | WebSocket base URL |
| `NEXT_PUBLIC_GATEWAY_URL` | browser (OAuth buttons, avatars) | not set: same origin | The gateway as the browser sees it |

`NEXT_PUBLIC_*` values are fixed at build time. The frontend receives none
of the backend's secrets (`.env.docker` is not passed to it).

## Structure

| Path | Content |
|---|---|
| `app/(public)/` | Pages without a session: login, register, OAuth callback, password reset, email verification, invitations |
| `app/(protected)/` | Pages that need a session: analyses, backtests, portfolios, watchlists, research, analytics, organizations, friends, notifications, profile, admin. `loading.tsx` shows "Loading..." while a page's data loads, so these pages stream: `notFound()` or `redirect()` inside a page gives HTTP status 200 (the not-found page, or a redirect in the browser) instead of 404 / 307; what the user sees is the same |
| `app/(legal)/` | Privacy Policy and Terms of Service |
| `app/lib/` | Backend calls, session cookies, Server Actions (`actions/`), formatting helpers, the shared WebSocket hook (`use-channel.ts`) |
| `app/healthz/` | Health route: 200 only when the server reaches the API through the gateway |
| `proxy.ts` | Runs before each request: renews the access token before it expires |

## Checks

`npm run lint`, `npx tsc --noEmit` (after `npx next typegen`) and
`npm run build`. The frontend CI workflow (`.github/workflows/frontend-ci.yml`)
runs the same three. See `docs/MODULE_MAPPING.md` for how the pages map to
the project's modules.
