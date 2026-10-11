"use client";

import { useActionState } from "react";
import { startAnalysis, type StartAnalysisState } from "@/app/lib/actions/analyses";

const initialState: StartAnalysisState = {};

// Same values as the backend's AnalysisType enum; "full" runs every
// research agent, the others a focused subset.
const ANALYSIS_TYPES = ["full", "technical", "fundamental", "sentiment", "risk", "macro", "news", "filing"];

export function StartAnalysisForm({ portfolios }: { portfolios: { id: string; name: string }[] }) {
  const [state, formAction, pending] = useActionState(startAnalysis, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="ticker">Ticker</label>
        <input id="ticker" name="ticker" type="text" required maxLength={10} placeholder="e.g. NVDA" />
      </div>
      <div className="field">
        <label htmlFor="analysis_type">Analysis type</label>
        <select id="analysis_type" name="analysis_type" defaultValue="full">
          {ANALYSIS_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="portfolio_id">Portfolio (optional)</label>
        <select id="portfolio_id" name="portfolio_id" defaultValue="">
          <option value="">None — analyze the ticker on its own</option>
          {portfolios.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
      <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
        A full analysis usually takes a few minutes. You can leave the page and come back.
      </p>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Starting…" : "Run analysis"}
      </button>
    </form>
  );
}
