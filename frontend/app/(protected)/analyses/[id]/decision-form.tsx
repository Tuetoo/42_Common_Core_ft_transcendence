"use client";

import { useActionState, useState } from "react";
import { recordDecision, type RecordDecisionState } from "@/app/lib/actions/analyses";

const initialState: RecordDecisionState = {};

const ACTIONS = [
  { value: "followed", label: "Followed — I acted on it" },
  { value: "partial", label: "Partially followed" },
  { value: "ignored", label: "Ignored" },
  { value: "declined", label: "Declined — I disagree" },
];

export function DecisionForm({
  analysisId,
  portfolioId,
  hasTiers,
}: {
  analysisId: string;
  portfolioId: string | null;
  hasTiers: boolean;
}) {
  const recordForAnalysis = recordDecision.bind(null, analysisId, portfolioId);
  const [state, formAction, pending] = useActionState(recordForAnalysis, initialState);
  const [action, setAction] = useState("followed");

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="action_taken">What did you do?</label>
        <select id="action_taken" name="action_taken" value={action} onChange={(e) => setAction(e.target.value)}>
          {ACTIONS.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </select>
      </div>
      {action === "followed" && portfolioId && hasTiers && (
        <p className="form-message" style={{ color: "var(--accent-strong)" }}>
          &quot;Followed&quot; accepts the price tiers above: when the price reaches a tier, the trade is recorded in
          your portfolio automatically.
        </p>
      )}
      <div className="field">
        <label htmlFor="notes">Notes (optional)</label>
        <textarea id="notes" name="notes" rows={3} maxLength={2000} />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state?.success && (
        <p aria-live="polite" className="form-message success">
          Decision recorded.
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Record decision"}
      </button>
    </form>
  );
}
