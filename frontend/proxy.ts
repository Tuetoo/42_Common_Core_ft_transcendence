import { NextResponse, type NextRequest } from "next/server";
import { ACCESS_TOKEN_COOKIE, COOKIE_OPTIONS, REFRESH_TOKEN_COOKIE } from "@/app/lib/session-cookies";

// Access tokens live 15 minutes. Before each request reaches a page, swap a
// token that is about to expire for a fresh pair, so a user who stays on a
// page (e.g. waiting for an analysis) isn't sent back to the login screen.
//
// Server Components can only read cookies, so this has to happen here: the
// proxy can both rewrite this request's cookies (the page sees the new
// token now) and set them on the response (the browser keeps it).

const REFRESH_MARGIN_SECONDS = 60;

type TokenPair = { access_token: string; refresh_token: string };

// The backend rotates refresh tokens and treats a later use of an old one
// as theft — it then revokes the sessions descending from that token.
// A browser often sends several requests at once, so concurrent refreshes of
// the same token are merged into one. The new pair only reaches the browser
// with the response, which can be cancelled (reload, closed tab, restart), so
// requests that still carry the old cookie get the same new pair again until
// one arrives with the new refresh token (the browser has stored it), or for
// at most one access-token lifetime. Only a request from the same client IP
// gets the replay; any other goes to the backend, whose reuse detection then
// applies. This lives in this process's memory,
// which is enough for a single frontend instance; several replicas would
// need a shared store such as Redis instead.
type Result = TokenPair | null | undefined;
const inFlight = new Map<string, { result: Promise<Result>; clientIp: string | null }>();
const recent = new Map<string, { pair: TokenPair; clientIp: string | null; expiresAt: number }>();
const oldTokenOf = new Map<string, string>();
const RECENT_TTL_MS = 15 * 60_000;
const CONFIRMED_TTL_MS = 30_000;

// The browser sent the new refresh token, so it has stored it: keep the
// replay only briefly for requests with the old cookie still on the way.
function confirmStored(refreshToken: string): void {
  const old = oldTokenOf.get(refreshToken);
  if (old === undefined) return;
  oldTokenOf.delete(refreshToken);
  const entry = recent.get(old);
  if (entry) entry.expiresAt = Math.min(entry.expiresAt, Date.now() + CONFIRMED_TTL_MS);
}

// Reads the `exp` claim without verifying the signature. That's fine: it is
// only a hint for when to refresh — the backend still verifies every token.
function secondsLeft(token: string): number | null {
  try {
    const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));
    return typeof payload.exp === "number" ? payload.exp - Date.now() / 1000 : null;
  } catch {
    return null;
  }
}

// null: the backend refused the refresh token (expired, revoked).
// undefined: the backend couldn't be reached — leave the session alone.
// A 429 (rate limited) is also "try again later", not a dead session, and so
// is refresh_token_superseded: another request just rotated this token. The
// browser keeps its cookie; if it sends the token again after the backend's
// grace period, the backend revokes every session descending from it.
// clientIp is the browser's address (see app/lib/backend.ts).
async function callRefresh(refreshToken: string, clientIp: string | null): Promise<Result> {
  let res: Response;
  try {
    res = await fetch(`${process.env.BACKEND_API_URL}/auth/refresh`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(clientIp ? { "X-Forwarded-For": clientIp } : {}),
      },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
  } catch {
    return undefined;
  }
  if (res.status === 401) {
    const body = await res.json().catch(() => null);
    return body?.error?.code === "refresh_token_superseded" ? undefined : null;
  }
  if (!res.ok) return res.status >= 500 || res.status === 429 ? undefined : null;
  const { data } = await res.json();
  return { access_token: data.access_token, refresh_token: data.refresh_token };
}

function refreshOnce(refreshToken: string, clientIp: string | null): Promise<Result> {
  const now = Date.now();
  for (const [key, entry] of recent) {
    if (entry.expiresAt > now) continue;
    recent.delete(key);
    oldTokenOf.delete(entry.pair.refresh_token);
  }
  const cached = recent.get(refreshToken);
  if (cached && cached.clientIp === clientIp) {
    // A replayed access token close to expiry would fail on the page:
    // move forward along the chain with the newer refresh token instead.
    // If that fails for now, a still-valid cached pair beats an expired one.
    const left = secondsLeft(cached.pair.access_token);
    if (left === null || left > REFRESH_MARGIN_SECONDS) return Promise.resolve(cached.pair);
    return refreshOnce(cached.pair.refresh_token, clientIp).then((next) =>
      next === undefined && left > 0 ? cached.pair : next,
    );
  }

  const pending = inFlight.get(refreshToken);
  if (pending && pending.clientIp === clientIp) return pending.result;
  // Another client presents a token already rotated or being rotated: the
  // backend decides (grace period or reuse), nothing is shared or cached.
  if (cached || pending) return callRefresh(refreshToken, clientIp);

  const result = callRefresh(refreshToken, clientIp)
    .then((pair) => {
      if (pair) {
        recent.set(refreshToken, { pair, clientIp, expiresAt: Date.now() + RECENT_TTL_MS });
        oldTokenOf.set(pair.refresh_token, refreshToken);
      }
      return pair;
    })
    .finally(() => inFlight.delete(refreshToken));
  inFlight.set(refreshToken, { result, clientIp });
  return result;
}

export async function proxy(request: NextRequest) {
  const accessToken = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
  const refreshToken = request.cookies.get(REFRESH_TOKEN_COOKIE)?.value;

  if (!refreshToken) return NextResponse.next();
  confirmStored(refreshToken);

  const left = accessToken ? secondsLeft(accessToken) : null;
  if (left !== null && left > REFRESH_MARGIN_SECONDS) return NextResponse.next();

  const pair = await refreshOnce(refreshToken, request.headers.get("x-real-ip"));

  if (pair === undefined) return NextResponse.next();

  if (pair === null) {
    // The session is over: drop both cookies so protected pages send the
    // user to the login screen instead of failing on a dead token.
    request.cookies.delete(ACCESS_TOKEN_COOKIE);
    request.cookies.delete(REFRESH_TOKEN_COOKIE);
    const response = NextResponse.next({ request: { headers: request.headers } });
    response.cookies.delete(ACCESS_TOKEN_COOKIE);
    response.cookies.delete(REFRESH_TOKEN_COOKIE);
    return response;
  }

  request.cookies.set(ACCESS_TOKEN_COOKIE, pair.access_token);
  request.cookies.set(REFRESH_TOKEN_COOKIE, pair.refresh_token);
  const response = NextResponse.next({ request: { headers: request.headers } });
  response.cookies.set(ACCESS_TOKEN_COOKIE, pair.access_token, COOKIE_OPTIONS);
  response.cookies.set(REFRESH_TOKEN_COOKIE, pair.refresh_token, COOKIE_OPTIONS);
  return response;
}

export const config = {
  // Skip static files and images — they never need a session.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
