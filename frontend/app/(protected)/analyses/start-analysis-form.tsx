"use client";

import { useActionState } from "react";
import { startAnalysis, type StartAnalysisState } from "@/app/lib/actions/analyses";
import { ANALYSIS_TYPES, TICKER_PATTERN, TICKER_TITLE, upperCaseInput } from "@/app/lib/format";

const initialState: StartAnalysisState = {};

export function StartAnalysisForm({ portfolios }: { portfolios: { id: string; name: string }[] }) {
  const [state, formAction, pending] = useActionState(startAnalysis, initialState);
  const values = state.values ?? {};

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="ticker">Ticker</label>
        <input
          id="ticker"
          name="ticker"
          type="text"
          required
          maxLength={10}
          pattern={TICKER_PATTERN}
          title={TICKER_TITLE}
          autoCapitalize="characters"
          autoComplete="off"
          onInput={upperCaseInput}
          placeholder="e.g. NVDA"
          defaultValue={values.ticker}
        />
      </div>
      <div className="field">
        <label htmlFor="analysis_type">Analysis type</label>
        <select id="analysis_type" name="analysis_type" defaultValue={values.analysis_type ?? "full"}>
          {ANALYSIS_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="portfolio_id">Portfolio (optional)</label>
        <select id="portfolio_id" name="portfolio_id" defaultValue={values.portfolio_id ?? ""}>
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
