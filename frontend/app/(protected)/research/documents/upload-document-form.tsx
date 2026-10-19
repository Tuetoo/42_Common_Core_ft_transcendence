"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  DOCUMENT_ACCEPT,
  DOCUMENT_CATEGORIES,
  DOCUMENT_EXTENSIONS,
  DOCUMENT_MAX_BYTES,
  DOCUMENT_TITLE_MAX,
  documentFileError,
  formatBytes,
  tickerError,
} from "../limits";
import { TICKER_PATTERN, TICKER_TITLE } from "@/app/lib/format";
import type { Organization } from "../types";

type Upload =
  | { state: "idle" }
  | { state: "uploading"; loaded: number; total: number }
  | { state: "processing" }
  | { state: "done"; message: string }
  | { state: "error"; message: string };

// Sends the file with XMLHttpRequest rather than fetch or a Server Action:
// only XHR reports upload progress (upload.onprogress). The target is a
// same-origin route handler that adds the session and forwards the file.
export function UploadDocumentForm({ organizations }: { organizations: Organization[] }) {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const xhrRef = useRef<XMLHttpRequest | null>(null);
  const [upload, setUpload] = useState<Upload>({ state: "idle" });
  const [fileError, setFileError] = useState<string | null>(null);

  function checkFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    const error = file ? documentFileError(file) : null;
    setFileError(error);
    e.target.setCustomValidity(error ?? "");
  }

  function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    const file = data.get("file");
    if (!(file instanceof File) || file.size === 0) {
      setUpload({ state: "error", message: "Please choose a file." });
      return;
    }
    const error = documentFileError(file) ?? tickerError(String(data.get("ticker") ?? ""));
    if (error) {
      setUpload({ state: "error", message: error });
      return;
    }

    const xhr = new XMLHttpRequest();
    xhrRef.current = xhr;
    xhr.open("POST", "/research/documents/upload");
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        setUpload({ state: "uploading", loaded: event.loaded, total: event.total });
      }
    };
    // All bytes are sent; the server is now checking and storing the file.
    xhr.upload.onload = () => setUpload({ state: "processing" });
    xhr.onload = () => {
      xhrRef.current = null;
      const body = (xhr.response ?? {}) as { message?: string; data?: { filename?: string } };
      if (xhr.status >= 200 && xhr.status < 300) {
        formRef.current?.reset();
        setFileError(null);
        setUpload({
          state: "done",
          message: `Uploaded ${body.data?.filename ?? file.name}. It is indexed in the background; the status below updates.`,
        });
        router.refresh();
      } else if (xhr.status === 401) {
        setUpload({ state: "error", message: "Your session has expired. Please log in again." });
      } else if (xhr.status === 413) {
        setUpload({ state: "error", message: body.message ?? "The file is too large." });
      } else {
        setUpload({ state: "error", message: body.message ?? `Upload failed (HTTP ${xhr.status}).` });
      }
    };
    xhr.onerror = () => {
      xhrRef.current = null;
      setUpload({ state: "error", message: "Network error: the upload did not reach the server." });
    };
    xhr.onabort = () => {
      xhrRef.current = null;
      setUpload({ state: "error", message: "Upload cancelled." });
    };
    setUpload({ state: "uploading", loaded: 0, total: file.size });
    xhr.send(data);
  }

  const busy = upload.state === "uploading" || upload.state === "processing";
  const percent =
    upload.state === "uploading" && upload.total > 0 ? Math.round((upload.loaded / upload.total) * 100) : 100;

  return (
    <form ref={formRef} onSubmit={submit} className="form">
      <div className="field">
        <label htmlFor="file">File</label>
        <input
          id="file"
          name="file"
          type="file"
          accept={DOCUMENT_ACCEPT}
          required
          disabled={busy}
          onChange={checkFile}
          aria-describedby="file-help"
          aria-invalid={fileError ? true : undefined}
          className="min-w-0"
        />
        <span id="file-help" className="text-xs text-[var(--text-faint)]">
          {DOCUMENT_EXTENSIONS.join(", ")} up to {formatBytes(DOCUMENT_MAX_BYTES)}. PDFs must be real PDFs; text
          files must be UTF-8.
        </span>
        {fileError && (
          <span role="alert" className="form-message error">
            {fileError}
          </span>
        )}
      </div>
      <div className="field">
        <label htmlFor="category">Category</label>
        <select id="category" name="category" autoComplete="off" required disabled={busy}>
          {DOCUMENT_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c.replaceAll("_", " ")}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="upload-ticker">Ticker (optional)</label>
        <input
          id="upload-ticker"
          name="ticker"
          autoComplete="off"
          type="text"
          maxLength={10}
          pattern={TICKER_PATTERN}
          title={TICKER_TITLE}
          placeholder="e.g. AAPL"
          disabled={busy}
          className="uppercase"
        />
      </div>
      <div className="field">
        <label htmlFor="upload-title">Title (optional)</label>
        <input
          id="upload-title"
          name="title"
          autoComplete="off"
          type="text"
          maxLength={DOCUMENT_TITLE_MAX}
          placeholder="Defaults to the file name"
          disabled={busy}
        />
      </div>
      <div className="field">
        <label htmlFor="upload-date">Document date (optional)</label>
        <input id="upload-date" name="content_date" autoComplete="off" type="date" disabled={busy} />
      </div>
      <div className="field">
        <label htmlFor="upload-org">Visible to</label>
        <select id="upload-org" name="organization_id" autoComplete="off" disabled={busy} defaultValue="">
          <option value="">Only me</option>
          {organizations.map((org) => (
            <option key={org.id} value={org.id}>
              Organization: {org.name}
            </option>
          ))}
        </select>
      </div>

      {busy && (
        <div className="flex flex-col gap-1">
          <div
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-hover)]"
          >
            <div
              className={`h-full rounded-full bg-[var(--accent)] transition-[width] duration-150 ${
                upload.state === "processing" ? "animate-pulse" : ""
              }`}
              style={{ width: `${percent}%` }}
            />
          </div>
          <span className="mono text-xs text-[var(--text-secondary)]" aria-live="polite">
            {upload.state === "uploading"
              ? `Uploading… ${percent}% (${formatBytes(upload.loaded)} of ${formatBytes(upload.total)})`
              : "Upload complete, the server is checking and storing the file…"}
          </span>
        </div>
      )}

      {(upload.state === "error" || upload.state === "done") && (
        <p aria-live="polite" className={`form-message ${upload.state === "error" ? "error" : "success"}`}>
          {upload.message}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy} className="btn btn-primary">
          {busy ? "Uploading…" : "Upload document"}
        </button>
        {upload.state === "uploading" && (
          <button type="button" className="btn btn-ghost" onClick={() => xhrRef.current?.abort()}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
