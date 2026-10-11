import { headers } from "next/headers";
import { notFound, redirect } from "next/navigation";

// Every server-side call goes to the gateway's internal listener
// (BACKEND_API_URL, http://gateway:8080/api/v1 in Docker). The gateway sets
// X-Real-IP to the browser's address on the public listener; passing it on
// lets the internal listener restore that address, so per-IP rate limits
// and session records see the real user instead of this container.
export async function clientIpHeaders(): Promise<Record<string, string>> {
  const incoming = await headers();
  const ip = incoming.get("x-real-ip");
  return ip ? { "X-Forwarded-For": ip } : {};
}

// Call without a session (login, register, email links).
export async function publicFetch(path: string, init?: RequestInit) {
  return fetch(`${process.env.BACKEND_API_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      ...(await clientIpHeaders()),
    },
  });
}

// Small helper so every authenticated call to the backend doesn't repeat
// the same Authorization header boilerplate.
export async function backendFetch(path: string, accessToken: string, init?: RequestInit) {
  return publicFetch(path, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${accessToken}`,
    },
  });
}

// Call this right after a backendFetch in a Server Component page before
// reading the body. A 401 means the access token expired (they only last
// 15 minutes) and sends the user back to log in. A 403, 404 or 422 (a
// malformed id in the URL) means there is nothing this user may see here,
// so the not-found page is shown. Any other non-2xx status is unexpected
// and still throws, which shows the error page.
export function requireOk(res: Response, context: string): void {
  if (res.status === 401) {
    redirect("/login");
  }
  if (res.status === 403 || res.status === 404 || res.status === 422) {
    notFound();
  }
  if (!res.ok) {
    throw new Error(`${context}: ${res.status}`);
  }
}
