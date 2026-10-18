import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";
import { DOCUMENT_CATEGORIES, QUESTION_MAX, tickerError } from "../../limits";

const MODES = ["ask", "draft"];

// The backend streams the answer as Server-Sent Events from a POST that
// needs the access token. The browser's EventSource can only send GETs
// without headers, so this route adds the token and pipes the stream
// through unchanged, chunk by chunk.
export async function POST(request: Request) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return Response.json({ message: "You're not logged in" }, { status: 401 });
  }

  let input: Record<string, unknown>;
  try {
    input = await request.json();
  } catch {
    return Response.json({ message: "Invalid request" }, { status: 400 });
  }

  const question = typeof input.question === "string" ? input.question.trim() : "";
  if (!question) {
    return Response.json({ message: "Please enter a question" }, { status: 400 });
  }
  if (question.length > QUESTION_MAX) {
    return Response.json({ message: `The question must be at most ${QUESTION_MAX} characters` }, { status: 400 });
  }
  const badTicker = typeof input.ticker === "string" ? tickerError(input.ticker) : null;
  if (badTicker) {
    return Response.json({ message: badTicker }, { status: 400 });
  }
  // Forward only fields the backend expects, never the raw body.
  const body = {
    question,
    mode: MODES.includes(input.mode as string) ? input.mode : "ask",
    ticker: typeof input.ticker === "string" && input.ticker.trim() ? input.ticker.trim().toUpperCase() : null,
    categories: Array.isArray(input.categories)
      ? input.categories.filter((c): c is string => (DOCUMENT_CATEGORIES as readonly string[]).includes(c as string))
      : null,
  };

  let res: Response;
  try {
    res = await backendFetch("/rag/ask/stream", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      // If the user stops or leaves, cancel the upstream call too, so the
      // backend stops generating (and spending LLM quota) for nobody.
      signal: request.signal,
    });
  } catch {
    return Response.json({ message: "Could not reach the server. Is the backend running?" }, { status: 502 });
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not get an answer");
    const retryAfter = res.headers.get("Retry-After");
    return Response.json(
      { message: err.message, retryAfter: retryAfter ? Number(retryAfter) : null },
      { status: res.status }
    );
  }

  return new Response(res.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
