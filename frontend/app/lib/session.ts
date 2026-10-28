import { cache } from "react";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ACCESS_TOKEN_COOKIE, COOKIE_OPTIONS, REFRESH_TOKEN_COOKIE } from "./session-cookies";
import { backendFetch, readApiError, requireOk } from "./backend";

export async function setSession(accessToken: string, refreshToken: string) {
  const cookieStore = await cookies();
  cookieStore.set(ACCESS_TOKEN_COOKIE, accessToken, COOKIE_OPTIONS);
  cookieStore.set(REFRESH_TOKEN_COOKIE, refreshToken, COOKIE_OPTIONS);
}

export async function clearSession() {
  const cookieStore = await cookies();
  cookieStore.delete(ACCESS_TOKEN_COOKIE);
  cookieStore.delete(REFRESH_TOKEN_COOKIE);
}

export async function getAccessToken() {
  const cookieStore = await cookies();
  return cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;
}

export async function getRefreshToken() {
  const cookieStore = await cookies();
  return cookieStore.get(REFRESH_TOKEN_COOKIE)?.value;
}

export async function isLoggedIn() {
  return Boolean(await getAccessToken());
}

// The signed-in user as GET /auth/me returns it. `permissions` comes from
// the user's system role (shared/permissions.py): a guest has none, a user
// has the write permissions, a moderator adds content:moderate and
// user:deactivate, an admin adds user:manage and site:stats.
export type Me = {
  id: string;
  username: string;
  email: string;
  system_role: string;
  permissions: string[];
  is_verified: boolean;
  created_at: string;
  profile: {
    first_name: string | null;
    last_name: string | null;
    bio: string | null;
    avatar_url: string | null;
    timezone: string | null;
    country: string | null;
  } | null;
};

// One /auth/me call per request, shared by the layout and every page.
export const getMe = cache(async (): Promise<{ me: Me; accessToken: string }> => {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    redirect("/login");
  }
  const res = await backendFetch("/auth/me", accessToken, { cache: "no-store" });
  requireOk(res, "Failed to load current user");
  const { data } = await res.json();
  return { me: data as Me, accessToken };
});

export function can(me: Me, permission: string): boolean {
  return me.permissions.includes(permission);
}

// The unread notification count, once per request (the layout's badge and
// the notifications page both need it). null: it could not be loaded.
export const getUnreadTotal = cache(async (accessToken: string): Promise<number | null> => {
  try {
    const res = await backendFetch("/notifications?unread_only=true&page_size=1", accessToken, { cache: "no-store" });
    if (!res.ok) return null;
    const { total }: { total: number } = await res.json();
    return total;
  } catch {
    return null;
  }
});

// One authenticated backend call for a server action: the result is either
// the OK response or a message to show (not logged in, unreachable,
// session expired, or the backend's own error). `status` is the HTTP status
// (401 when there is no token, 0 when the server could not be reached) and
// `code` the backend's error code, for the few callers that act on them.
type CallResult =
  | { ok: true; res: Response }
  | { ok: false; error: string; status: number; code: string | null };

export async function call(path: string, init: RequestInit, fallback: string): Promise<CallResult> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { ok: false, error: "You're not logged in", status: 401, code: null };
  }
  let res: Response;
  try {
    res = await backendFetch(path, accessToken, init);
  } catch {
    return { ok: false, error: "Could not reach the server. Is the backend running?", status: 0, code: null };
  }
  if (res.status === 401) {
    return { ok: false, error: "Your session expired. Please log in again.", status: 401, code: null };
  }
  if (!res.ok) {
    const err = await readApiError(res, fallback);
    return { ok: false, error: err.message, status: err.status, code: err.code };
  }
  return { ok: true, res };
}

export function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}
