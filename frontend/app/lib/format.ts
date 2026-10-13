// The backend sends money and quantities as Decimal strings, sometimes in
// exponent form ("1E+4") or with long repeating decimals
// ("151.6666666666666666666666667"). For display only — never feed the
// result back into a calculation or a request.
export function formatNumber(value: string | number | null | undefined, maxDecimals = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString("en-US", { maximumFractionDigits: maxDecimals });
}

// ---------------------------------------------------------------------------
// Form input rules. They mirror the backend's Pydantic limits so the browser
// can stop bad input early; the server actions check them again, because a
// request can always skip the browser.

// shared/schemas.py: 1-10 characters of A-Z, 0-9, "." or "-". The inputs
// accept lower case too; the actions upper-case the value before sending.
export const TICKER_PATTERN = "[A-Za-z0-9.\\-]{1,10}";
export const TICKER_TITLE = "1-10 characters: letters, digits, '.' or '-' (for example BRK.B)";
const TICKER_RE = /^[A-Z0-9.-]{1,10}$/;

// Portfolio and watchlist names (domains/portfolio/schema.py).
export const NAME_MAX_LENGTH = 128;

// shared/enums.py Currency.
export const CURRENCIES = ["USD", "EUR", "GBP"] as const;

// The smallest amount the number inputs accept. The backend only requires
// "greater than 0" (with at most 18 decimal places), but an HTML `min` is
// inclusive, so "0" would let a zero through.
export const MIN_POSITIVE_AMOUNT = "0.00000001";

// Upper-cases a ticker input as the user types (uncontrolled inputs).
export function upperCaseInput(event: { currentTarget: HTMLInputElement }): void {
  const input = event.currentTarget;
  const upper = input.value.toUpperCase();
  if (upper !== input.value) {
    const caret = input.selectionStart;
    input.value = upper;
    if (caret !== null) input.setSelectionRange(caret, caret);
  }
}

// Returns the normalized ticker, or null when it breaks the backend rule.
export function normalizeTicker(value: FormDataEntryValue | null): string | null {
  if (typeof value !== "string") return null;
  const ticker = value.trim().toUpperCase();
  return TICKER_RE.test(ticker) ? ticker : null;
}

// Returns the trimmed text, or null when it is empty or too long.
export function boundedText(value: FormDataEntryValue | null, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text && text.length <= maxLength ? text : null;
}

// Checks a positive decimal the way the backend's PositiveAmount does:
// greater than 0, at most 30 digits, at most 18 of them after the point.
// Returns the plain decimal string to send, or null.
export function positiveDecimal(value: FormDataEntryValue | null): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  const match = /^(\d*)(?:\.(\d*))?$/.exec(text);
  if (!match || text === "" || text === ".") return null;
  const whole = match[1].replace(/^0+/, "");
  const fraction = (match[2] ?? "").replace(/0+$/, "");
  if (fraction.length > 18 || whole.length + fraction.length > 30) return null;
  if (!whole && !fraction) return null;
  return text;
}

// shared/enums.py AnalysisType; "full" runs every research agent, the
// others a focused subset.
export const ANALYSIS_TYPES = ["full", "technical", "fundamental", "sentiment", "risk", "macro", "news", "filing"];

// domains/decision/schema.py InvestmentDecisionCreate.
export const DECISION_ACTIONS = ["followed", "partial", "ignored", "declined"];
export const DECISION_NOTES_MAX_LENGTH = 2000;

// Analysis status, shared by the list and detail pages so a status
// (pending, running, completed, failed) is colored the same everywhere.
export function statusPillClass(status: string): string {
  if (status === "completed") return "pill pill-up";
  if (status === "failed") return "pill pill-down";
  if (status === "running" || status === "pending") return "pill pill-accent";
  return "pill";
}

export function isFinished(status: string): boolean {
  return status === "completed" || status === "failed";
}

// Analytics metrics. Kept in this plain module (no "use client") so both
// the server page and the client charts can call these. Anything exported
// from a "use client" file becomes a client reference that the server can
// render but never call.
export type Metric = "analyses" | "confidence" | "accuracy";

export type TimeseriesPoint = { period_start: string; value: number | null; count: number };

export function formatMetric(metric: Metric, value: number | null): string {
  if (value === null) return "—";
  return metric === "analyses" ? value.toLocaleString("en-US") : `${Math.round(value * 100)}%`;
}
