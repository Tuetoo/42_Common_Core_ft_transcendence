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

// The backend rotates refresh tokens and treats a second use of an old one
// as theft — it then signs the account out everywhere. A browser often sends
// several requests at once, so concurrent refreshes of the same token are
// merged into one, and the result is reused for a short while by requests
// that still carry the old cookie. This lives in this process's memory,
// which is enough for a single frontend instance; several replicas would
// need a shared store such as Redis instead.
const inFlight = new Map<string, Promise<TokenPair | null | undefined>>();
const recent = new Map<string, { pair: TokenPair; expiresAt: number }>();
const RECENT_TTL_MS = 30_000;

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
async function callRefresh(refreshToken: string): Promise<TokenPair | null | undefined> {
  let res: Response;
  try {
    res = await fetch(`${process.env.BACKEND_API_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
  } catch {
    return undefined;
  }
  if (!res.ok) return res.status >= 500 ? undefined : null;
  const { data } = await res.json();
  return { access_token: data.access_token, refresh_token: data.refresh_token };
}

function refreshOnce(refreshToken: string): Promise<TokenPair | null | undefined> {
  const now = Date.now();
  for (const [key, entry] of recent) {
    if (entry.expiresAt <= now) recent.delete(key);
  }
  const cached = recent.get(refreshToken);
  if (cached) return Promise.resolve(cached.pair);

  let pending = inFlight.get(refreshToken);
  if (!pending) {
    pending = callRefresh(refreshToken)
      .then((pair) => {
        if (pair) recent.set(refreshToken, { pair, expiresAt: Date.now() + RECENT_TTL_MS });
        return pair;
      })
      .finally(() => inFlight.delete(refreshToken));
    inFlight.set(refreshToken, pending);
  }
  return pending;
}

export async function proxy(request: NextRequest) {
  const accessToken = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
  const refreshToken = request.cookies.get(REFRESH_TOKEN_COOKIE)?.value;

  if (!refreshToken) return NextResponse.next();

  const left = accessToken ? secondsLeft(accessToken) : null;
  if (left !== null && left > REFRESH_MARGIN_SECONDS) return NextResponse.next();

  const pair = await refreshOnce(refreshToken);

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
