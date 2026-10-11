// Frontend health with its backend path: 200 only when this server can
// reach the gateway's internal listener and auth-service answers there.
// The stack smoke test in CI requests it through the public gateway, so a
// frontend that serves pages but cannot reach the API fails the check.
export const dynamic = "force-dynamic";

export async function GET() {
  const base = process.env.BACKEND_API_URL;
  if (!base) {
    return Response.json({ status: "error", detail: "BACKEND_API_URL is not set" }, { status: 503 });
  }
  try {
    const res = await fetch(`${new URL(base).origin}/health/auth`, { cache: "no-store" });
    if (res.ok) {
      return Response.json({ status: "ok" });
    }
    return Response.json({ status: "error", detail: `auth-service health ${res.status}` }, { status: 503 });
  } catch {
    return Response.json({ status: "error", detail: "backend unreachable" }, { status: 503 });
  }
}
