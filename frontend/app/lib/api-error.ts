// The backend returns errors in two shapes (docs/handbook/01-architecture.md
// section 3.6b), and a client must accept both:
// - the project envelope: { error: { code, message, request_id } }
// - FastAPI's own: { detail: "text" } or { detail: [{ msg, loc, ... }] }
export type ApiError = {
  status: number;
  code: string | null;
  message: string;
};

export async function readApiError(res: Response, fallback: string): Promise<ApiError> {
  const result: ApiError = { status: res.status, code: null, message: fallback };
  try {
    const body = await res.json();
    if (typeof body?.error?.message === "string") {
      result.message = body.error.message;
      result.code = typeof body.error.code === "string" ? body.error.code : null;
    } else if (typeof body?.detail === "string") {
      result.message = body.detail;
    } else if (Array.isArray(body?.detail) && body.detail.length > 0) {
      const joined = body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ");
      if (joined) result.message = joined;
    }
  } catch {
    // response wasn't JSON, keep the fallback message
  }
  return result;
}

export async function extractErrorMessage(res: Response, fallback: string): Promise<string> {
  return (await readApiError(res, fallback)).message;
}
