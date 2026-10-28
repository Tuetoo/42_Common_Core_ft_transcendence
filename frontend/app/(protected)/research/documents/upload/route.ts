import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";
import {
  DOCUMENT_CATEGORIES,
  DOCUMENT_MAX_BYTES,
  DOCUMENT_TITLE_MAX,
  documentFileError,
  formatBytes,
  tickerError,
} from "../../limits";
import { DATE_RE, UUID_RE } from "@/app/lib/format";

// Room for the multipart boundaries and the small text fields.
const MULTIPART_OVERHEAD = 64 * 1024;

function fail(message: string, status: number) {
  return Response.json({ message }, { status });
}

// The upload form posts here with XMLHttpRequest (so the browser can show
// upload progress). This handler checks the session and the input, then
// forwards a freshly built multipart body to
// POST /research/documents (services/core-service/domains/research/api.py),
// which validates everything again (extension, size, PDF magic bytes,
// UTF-8 text) and owns the stored file.
export async function POST(request: Request) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return fail("You're not logged in", 401);
  }

  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (declaredLength > DOCUMENT_MAX_BYTES + MULTIPART_OVERHEAD) {
    return fail(`The file is too large; the limit is ${formatBytes(DOCUMENT_MAX_BYTES)}.`, 413);
  }

  let form: FormData;
  try {
    const body = await request.arrayBuffer();
    // Next's proxy (proxy.ts) buffers request bodies and silently cuts them
    // at experimental.proxyClientMaxBodySize (10MB by default). A cut PDF
    // still starts with %PDF- and would be accepted, so compare sizes.
    if (declaredLength > 0 && body.byteLength < declaredLength) {
      return fail(
        `The upload was cut off at ${formatBytes(body.byteLength)} by the web server's request size limit.`,
        413
      );
    }
    form = await new Response(body, {
      headers: { "Content-Type": request.headers.get("content-type") ?? "" },
    }).formData();
  } catch {
    return fail("The upload could not be read. Please try again.", 400);
  }

  const file = form.get("file");
  if (!(file instanceof File)) {
    return fail("Please choose a file", 400);
  }
  const fileError = documentFileError(file);
  if (fileError) {
    return fail(fileError, file.size > DOCUMENT_MAX_BYTES ? 413 : 400);
  }

  const category = form.get("category");
  if (typeof category !== "string" || !(DOCUMENT_CATEGORIES as readonly string[]).includes(category)) {
    return fail("Please choose a valid category", 400);
  }

  const outgoing = new FormData();
  outgoing.set("file", file, file.name);
  outgoing.set("category", category);

  const ticker = form.get("ticker");
  if (typeof ticker === "string" && ticker.trim()) {
    const error = tickerError(ticker);
    if (error) return fail(error, 400);
    outgoing.set("ticker", ticker.trim().toUpperCase());
  }

  const title = form.get("title");
  if (typeof title === "string" && title.trim()) {
    if (title.trim().length > DOCUMENT_TITLE_MAX) {
      return fail(`The title must be at most ${DOCUMENT_TITLE_MAX} characters`, 400);
    }
    outgoing.set("title", title.trim());
  }

  const organizationId = form.get("organization_id");
  if (typeof organizationId === "string" && organizationId) {
    if (!UUID_RE.test(organizationId)) return fail("Please choose a valid organization", 400);
    outgoing.set("organization_id", organizationId);
  }

  const contentDate = form.get("content_date");
  if (typeof contentDate === "string" && contentDate) {
    if (!DATE_RE.test(contentDate)) return fail("Invalid document date", 400);
    outgoing.set("content_date", `${contentDate}T00:00:00Z`);
  }

  let res: Response;
  try {
    // No Content-Type header: fetch writes the multipart boundary itself.
    res = await backendFetch("/research/documents", accessToken, { method: "POST", body: outgoing });
  } catch {
    return fail("Could not reach the server. Is the backend running?", 502);
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not upload the document");
    return fail(err.message, res.status);
  }

  const { data } = await res.json();
  return Response.json({ data }, { status: 201 });
}
