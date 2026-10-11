import type { NextRequest } from "next/server";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";

// The browser can't download the CSV from the backend itself: the access
// token is in an httpOnly cookie that page scripts can't read. This route
// runs on the Next.js server, adds the token, and passes the file through.
const FORWARDED_PARAMS = ["dataset", "from", "to", "ticker", "action"];

export async function GET(request: NextRequest) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return new Response("Not logged in", { status: 401 });
  }

  const query = new URLSearchParams();
  for (const name of FORWARDED_PARAMS) {
    const value = request.nextUrl.searchParams.get(name);
    if (value) query.set(name, value);
  }

  const res = await backendFetch(`/analytics/export.csv?${query}`, accessToken, { cache: "no-store" });
  if (!res.ok) {
    return new Response(`Export failed (${res.status})`, { status: res.status });
  }

  return new Response(res.body, {
    headers: {
      "Content-Type": res.headers.get("Content-Type") ?? "text/csv; charset=utf-8",
      "Content-Disposition": res.headers.get("Content-Disposition") ?? 'attachment; filename="alphamind_export.csv"',
    },
  });
}
