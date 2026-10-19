import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";
import { UUID_RE } from "@/app/lib/format";

// Headers passed through from the backend file response.
const PASS_THROUGH = ["content-type", "content-length", "content-security-policy"];

// Streams a stored document from the backend to the browser. The backend
// checks access (owner, organization member, or system document) and sends
// 404 otherwise; the access token never reaches the browser.
//   kind "preview":  GET /research/documents/{id}/preview (inline; .md as text/plain)
//   kind "download": GET /research/documents/{id}/content (as attachment)
export async function proxyDocumentFile(id: string, kind: "preview" | "download"): Promise<Response> {
  if (!UUID_RE.test(id)) {
    return Response.json({ message: "Document not found" }, { status: 404 });
  }
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return Response.json({ message: "You're not logged in" }, { status: 401 });
  }

  let res: Response;
  try {
    res = await backendFetch(`/research/documents/${id}/${kind === "preview" ? "preview" : "content"}`, accessToken, {
      cache: "no-store",
    });
  } catch {
    return Response.json({ message: "Could not reach the server. Is the backend running?" }, { status: 502 });
  }
  if (!res.ok) {
    const err = await readApiError(res, "Could not load the document");
    return Response.json({ message: err.message }, { status: res.status });
  }

  const headers = new Headers({
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
  });
  for (const name of PASS_THROUGH) {
    const value = res.headers.get(name);
    if (value) headers.set(name, value);
  }
  // The backend always says "inline"; a download asks the browser to save.
  const disposition = res.headers.get("content-disposition") ?? 'inline; filename="document"';
  headers.set(
    "Content-Disposition",
    kind === "download" ? disposition.replace(/^inline/i, "attachment") : disposition
  );
  return new Response(res.body, { status: 200, headers });
}
