import type { NextRequest } from "next/server";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";

// The browser can't download the CSV from the backend itself: the access
// token is in an httpOnly cookie that page scripts can't read. This route
// runs on the Next.js server, adds the token, and passes the file through.
const FORWARDED_PARAMS = ["dataset", "from", "to", "ticker", "action", "status"];
const FILTER_PARAMS = ["from", "to", "ticker", "action", "status"];

// A relative Location is valid (RFC 9110) and keeps the browser on the
// host it used, whatever host name this server sees behind the gateway.
function redirectTo(location: string): Response {
  return new Response(null, { status: 303, headers: { Location: location } });
}

export async function GET(request: NextRequest) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return redirectTo("/login");
  }

  const incoming = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  for (const name of FORWARDED_PARAMS) {
    const value = incoming.get(name);
    if (value) query.set(name, value);
  }

  let res: Response | null = null;
  try {
    res = await backendFetch(`/analytics/export.csv?${query}`, accessToken, { cache: "no-store" });
  } catch {
    // Backend unreachable: handled below like any other failure.
  }
  if (res?.status === 401) {
    return redirectTo("/login");
  }
  if (!res?.ok) {
    // Back to the dashboard with the same filters and a readable message,
    // instead of a bare error page in place of the download.
    const message = res
      ? (await readApiError(res, `the server answered ${res.status}`)).message
      : "could not reach the server";
    const back = new URLSearchParams();
    for (const name of FILTER_PARAMS) {
      const value = incoming.get(name);
      if (value) back.set(name, value);
    }
    back.set("export_error", message);
    return redirectTo(`/analytics?${back}`);
  }

  return new Response(res.body, {
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "text/csv; charset=utf-8",
      "Content-Disposition": res.headers.get("Content-Disposition") ?? 'attachment; filename="alphamind_export.csv"',
    },
  });
}
