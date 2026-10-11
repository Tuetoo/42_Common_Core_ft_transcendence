"use client";

import { useActionState } from "react";
import { uploadDocument, type UploadDocumentState } from "@/app/lib/actions/documents";

const initialState: UploadDocumentState = {};

const CATEGORIES = [
  "filing",
  "analyst_report",
  "earnings_call",
  "news",
  "macro_research",
  "risk_analysis",
];

export function UploadDocumentForm() {
  const [state, formAction, pending] = useActionState(uploadDocument, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="file">File</label>
        <input id="file" name="file" type="file" required />
      </div>
      <div className="field">
        <label htmlFor="category">Category</label>
        <select id="category" name="category" required>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="ticker">Ticker (optional)</label>
        <input id="ticker" name="ticker" type="text" />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Uploading…" : "Upload document"}
      </button>
    </form>
  );
}
