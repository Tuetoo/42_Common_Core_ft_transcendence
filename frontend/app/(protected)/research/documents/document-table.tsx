"use client";

import { LocalTime } from "@/app/(protected)/local-time";
import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { deleteDocument } from "@/app/lib/actions/documents";
import { FINAL_DOCUMENT_STATUSES } from "../limits";
import type { ResearchDocument } from "../types";

const REFRESH_WHILE_PROCESSING_MS = 5000;

function statusClass(status: string): string {
  if (status === "ready") return "pill pill-up";
  if (status === "failed") return "pill pill-down";
  return "pill pill-accent";
}

export function DocumentTable({ documents, meId }: { documents: ResearchDocument[]; meId: string }) {
  const router = useRouter();
  const [previewId, setPreviewId] = useState<string | null>(null);
  const preview = documents.find((d) => d.id === previewId) ?? null;

  // Indexing runs in the background (uploaded -> parsing -> chunking ->
  // embedding -> ready/failed). While any listed document is still on its
  // way, reload the server data now and then so the status pills move.
  const processing = documents.some((d) => !FINAL_DOCUMENT_STATUSES.includes(d.status));
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(() => router.refresh(), REFRESH_WHILE_PROCESSING_MS);
    return () => clearInterval(timer);
  }, [processing, router]);

  return (
    <div className="flex flex-col gap-4">
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Document</th>
              <th scope="col">Category</th>
              <th scope="col">Ticker</th>
              <th scope="col">Status</th>
              <th scope="col">Uploaded</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {documents.map((d) => (
              <tr key={d.id}>
                <td className="min-w-[10rem] max-w-[18rem]">
                  <div className="flex flex-col gap-0.5">
                    <span className="break-words font-semibold">{d.title ?? d.filename}</span>
                    {d.title && <span className="mono break-all text-xs text-[var(--text-faint)]">{d.filename}</span>}
                    <span className="flex flex-wrap gap-1">
                      <span className="pill">{d.document_type}</span>
                      {d.is_system && <span className="pill">system</span>}
                      {d.organization_id && <span className="pill">shared</span>}
                    </span>
                  </div>
                </td>
                <td className="mono whitespace-nowrap">{d.category.replaceAll("_", " ")}</td>
                <td className="mono">{d.ticker ?? "—"}</td>
                <td>
                  <span className={statusClass(d.status)}>{d.status}</span>
                </td>
                <td className="mono whitespace-nowrap text-xs"><LocalTime iso={d.uploaded_at} dateOnly /></td>
                <td>
                  <RowActions
                    document={d}
                    canDelete={d.owner_id === meId}
                    previewing={previewId === d.id}
                    onPreview={() => setPreviewId(previewId === d.id ? null : d.id)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview && <DocumentPreview key={preview.id} document={preview} onClose={() => setPreviewId(null)} />}
    </div>
  );
}

function RowActions({
  document,
  canDelete,
  previewing,
  onPreview,
}: {
  document: ResearchDocument;
  canDelete: boolean;
  previewing: boolean;
  onPreview: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const name = document.title ?? document.filename;

  function remove() {
    setError(null);
    startTransition(async () => {
      const result = await deleteDocument(document.id);
      if (result.error) {
        setError(result.error);
        setConfirming(false);
      }
    });
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button
          type="button"
          className="link-btn"
          onClick={onPreview}
          aria-expanded={previewing}
          aria-label={`${previewing ? "Hide preview of" : "Preview"} ${name}`}
        >
          {previewing ? "Hide preview" : "Preview"}
        </button>
        <a
          href={`/research/documents/${document.id}/download`}
          className="link-btn"
          aria-label={`Download ${name}`}
          download
        >
          Download
        </a>
        {canDelete &&
          (confirming ? (
            <span className="flex flex-wrap items-center gap-2" role="group" aria-label={`Confirm deleting ${name}`}>
              <button
                type="button"
                className="btn btn-ghost px-2 py-1 text-xs text-[var(--down)]"
                onClick={remove}
                disabled={pending}
                autoFocus
              >
                {pending ? "Deleting…" : "Confirm delete"}
              </button>
              <button
                type="button"
                className="link-btn"
                onClick={() => setConfirming(false)}
                disabled={pending}
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="link-btn text-[var(--down)]"
              onClick={() => setConfirming(true)}
              aria-label={`Delete ${name}`}
            >
              Delete
            </button>
          ))}
      </div>
      {error && (
        <span role="alert" className="form-message error">
          {error}
        </span>
      )}
    </div>
  );
}

type PreviewState =
  | { kind: "loading" }
  | { kind: "pdf"; url: string }
  | { kind: "text"; text: string }
  | { kind: "error"; message: string };

// Inline viewer. The file is fetched from the same-origin preview route and
// shown from a blob: URL (PDF in an iframe) or as plain text in a <pre>.
// A blob: URL is used because the gateway sends X-Frame-Options: DENY on
// every page, which would block framing the preview route directly.
function DocumentPreview({ document, onClose }: { document: ResearchDocument; onClose: () => void }) {
  const [state, setState] = useState<PreviewState>({ kind: "loading" });
  const previewUrl = `/research/documents/${document.id}/preview`;

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    (async () => {
      try {
        const res = await fetch(previewUrl, { signal: controller.signal, cache: "no-store" });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setState({ kind: "error", message: body.message ?? `Could not load the preview (HTTP ${res.status}).` });
          return;
        }
        const type = res.headers.get("content-type") ?? "";
        if (type.startsWith("application/pdf")) {
          objectUrl = URL.createObjectURL(await res.blob());
          setState({ kind: "pdf", url: objectUrl });
        } else {
          setState({ kind: "text", text: await res.text() });
        }
      } catch {
        if (!controller.signal.aborted) setState({ kind: "error", message: "Could not load the preview." });
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [previewUrl]);

  const name = document.title ?? document.filename;

  return (
    <section className="panel flex min-w-0 flex-col gap-3" aria-label={`Preview of ${name}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0 min-w-0 break-words text-sm font-semibold">Preview: {name}</h3>
        <div className="flex flex-wrap gap-3">
          <a href={previewUrl} target="_blank" rel="noopener" className="link-btn">
            Open in new tab
          </a>
          <button type="button" className="link-btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {state.kind === "loading" && (
        <p className="empty-state" aria-live="polite">
          Loading preview…
        </p>
      )}
      {state.kind === "error" && (
        <p role="alert" className="form-message error">
          {state.message}
        </p>
      )}
      {state.kind === "pdf" && (
        <iframe
          src={state.url}
          title={`PDF preview of ${name}`}
          className="h-[70vh] w-full rounded-md border border-[var(--border)] bg-white"
        />
      )}
      {state.kind === "text" && (
        <pre className="mono m-0 max-h-[70vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg)] p-3 text-xs leading-relaxed">
          {state.text}
        </pre>
      )}
    </section>
  );
}
