// Cookie names and options shared by session.ts (pages and Server Actions)
// and proxy.ts (token refresh). Kept free of next/headers so proxy.ts can
// import it too.
export const ACCESS_TOKEN_COOKIE = "access_token";
export const REFRESH_TOKEN_COOKIE = "refresh_token";

export const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};
