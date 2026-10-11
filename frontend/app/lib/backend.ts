import { redirect } from "next/navigation";

// Small helper so every authenticated call to the backend doesn't repeat
// the same Authorization header boilerplate.
export async function backendFetch(path: string, accessToken: string, init?: RequestInit) {
  return fetch(`${process.env.BACKEND_API_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${accessToken}`,
    },
  });
}

// Call this right after a backendFetch in a Server Component page before
// reading the body. A 401 means the access token expired (it's normal —
// they only last 15 minutes) and sends the user back to log in instead of
// crashing the page; any other non-2xx status is a real, unexpected error
// and still throws.
export function requireOk(res: Response, context: string): void {
  if (res.status === 401) {
    redirect("/login");
  }
  if (!res.ok) {
    throw new Error(`${context}: ${res.status}`);
  }
}
