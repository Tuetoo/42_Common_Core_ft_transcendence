"use client";

import { useActionState, useState } from "react";
import { recordDecision, type RecordDecisionState } from "@/app/lib/actions/analyses";
import { DECISION_NOTES_MAX_LENGTH } from "@/app/lib/format";
import { FormMessage } from "@/app/form-message";

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
        <select id="action_taken" name="action_taken" autoComplete="off" value={action} onChange={(e) => setAction(e.target.value)}>
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
        <textarea
          id="notes"
          name="notes"
          autoComplete="off"
          rows={3}
          maxLength={DECISION_NOTES_MAX_LENGTH}
          defaultValue={state.notes}
          className="w-full"
        />
      </div>
      <FormMessage text={state?.error} />
      <FormMessage kind="success" text={state?.success ? "Decision recorded." : null} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Record decision"}
      </button>
    </form>
  );
}
