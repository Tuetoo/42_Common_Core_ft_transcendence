// Limits that mirror the backend's validation, so forms can reject bad input
// before a round trip and server code can re-check it before calling the API.
// Sources (the backend stays the source of truth):
// - services/core-service/domains/research/schema.py (notes, tags, comments)
// - services/core-service/config.py (document size, extensions, categories)
// - shared/enums.py (document enums); the ticker rule is in app/lib/format.ts
// - services/ai-service/domains/rag/schema.py (question length)

import { TICKER_RE, TICKER_TITLE } from "@/app/lib/format";

export const NOTE_TITLE_MAX = 256;
export const NOTE_CONTENT_MAX = 200_000;
export const MAX_TAGS = 20;
export const TAG_MAX_CHARS = 50;
export const COMMENT_MAX = 10_000;
export const QUESTION_MAX = 2000;
export const DOCUMENT_TITLE_MAX = 256;
export const SEARCH_TEXT_MAX = 200;

export const DOCUMENT_MAX_BYTES = 20 * 1024 * 1024;
export const DOCUMENT_EXTENSIONS = [".pdf", ".txt", ".md"] as const;
// Browsers report inconsistent MIME types for .md (often empty), so the
// extension is the main check and the MIME type only rules out obvious
// mismatches.
export const DOCUMENT_MIME_TYPES = ["application/pdf", "text/plain", "text/markdown", "text/x-markdown", ""];
export const DOCUMENT_ACCEPT = ".pdf,.txt,.md,application/pdf,text/plain,text/markdown";

export const DOCUMENT_CATEGORIES = [
  "filing",
  "analyst_report",
  "earnings_call",
  "news",
  "macro_research",
  "risk_analysis",
] as const;

export const DOCUMENT_STATUSES = ["uploaded", "parsing", "chunking", "embedding", "ready", "failed"] as const;
export const FINAL_DOCUMENT_STATUSES = ["ready", "failed"];

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Returns an error message, or null when the ticker is empty or valid.
export function tickerError(raw: string): string | null {
  const ticker = raw.trim().toUpperCase();
  if (!ticker) return null;
  return TICKER_RE.test(ticker) ? null : `Invalid ticker: use ${TICKER_TITLE}.`;
}

// Splits "a, b, c" into clean tags and checks the backend's limits.
export function parseTags(raw: string): { tags: string[]; error: string | null } {
  const tags = raw
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  if (tags.length > MAX_TAGS) return { tags, error: `At most ${MAX_TAGS} tags are allowed.` };
  if (tags.some((t) => t.length > TAG_MAX_CHARS)) {
    return { tags, error: `Each tag must be at most ${TAG_MAX_CHARS} characters.` };
  }
  return { tags, error: null };
}

// Checks a file before upload: extension, MIME type, size and emptiness.
// Used in the browser and again in the upload route handler.
export function documentFileError(file: { name: string; size: number; type: string }): string | null {
  const dot = file.name.lastIndexOf(".");
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!(DOCUMENT_EXTENSIONS as readonly string[]).includes(extension)) {
    return `Unsupported file type "${extension || "none"}". Allowed: ${DOCUMENT_EXTENSIONS.join(", ")}.`;
  }
  const mime = file.type.split(";")[0].trim().toLowerCase();
  if (!DOCUMENT_MIME_TYPES.includes(mime) && mime !== "application/octet-stream") {
    return `The file's type (${mime}) does not match a ${extension} file.`;
  }
  if (extension === ".pdf" && mime && mime !== "application/pdf" && mime !== "application/octet-stream") {
    return "A .pdf file must have the application/pdf type.";
  }
  if (file.size === 0) return "The file is empty.";
  if (file.size > DOCUMENT_MAX_BYTES) {
    return `The file is ${formatBytes(file.size)}; the limit is ${formatBytes(DOCUMENT_MAX_BYTES)}.`;
  }
  return null;
}
