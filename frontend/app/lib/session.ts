import { cache } from "react";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ACCESS_TOKEN_COOKIE, COOKIE_OPTIONS, REFRESH_TOKEN_COOKIE } from "./session-cookies";
import { backendFetch, requireOk } from "./backend";

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
  profile: { avatar_url: string | null } | null;
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
