import { getAccessToken } from "@/app/lib/session";

// The live editor's WebSocket authenticates with ?token=<access token>, and
// the server closes it (code 4401) when that token expires. Browser code
// can't read the httpOnly session cookie, so on reconnect it asks here.
// The request passes through proxy.ts first, which swaps a token that is
// about to expire for a fresh one, so this returns a usable token.
export async function GET() {
  const token = await getAccessToken();
  if (!token) {
    return Response.json({ message: "You're not logged in" }, { status: 401, headers: { "Cache-Control": "no-store" } });
  }
  return Response.json({ token }, { headers: { "Cache-Control": "no-store" } });
}
