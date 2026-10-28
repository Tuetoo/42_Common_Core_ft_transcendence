"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { call, jsonInit } from "@/app/lib/session";
import { ANALYSIS_TYPES, DECISION_ACTIONS, DECISION_NOTES_MAX_LENGTH, UUID_RE, normalizeTicker } from "@/app/lib/format";

export type StartAnalysisState = {
  error?: string;
  // The submitted values, so the form keeps them after an error.
  values?: Record<string, string>;
};

export async function startAnalysis(
  _prevState: StartAnalysisState,
  formData: FormData
): Promise<StartAnalysisState> {
  const rawTicker = formData.get("ticker");
  const analysisType = formData.get("analysis_type") || "full";
  const portfolioId = formData.get("portfolio_id") || null;
  const values: Record<string, string> = {
    ticker: typeof rawTicker === "string" ? rawTicker : "",
    analysis_type: typeof analysisType === "string" ? analysisType : "full",
    portfolio_id: typeof portfolioId === "string" ? portfolioId : "",
  };

  const ticker = normalizeTicker(rawTicker);
  if (!ticker) {
    return { error: "Ticker must be 1-10 characters: letters, digits, '.' or '-'", values };
  }
  if (typeof analysisType !== "string" || !ANALYSIS_TYPES.includes(analysisType)) {
    return { error: "Please choose an analysis type", values };
  }
  // An empty select value means "not tied to a portfolio".
  if (portfolioId !== null && (typeof portfolioId !== "string" || !UUID_RE.test(portfolioId))) {
    return { error: "Please choose one of your portfolios", values };
  }

  const result = await call(
    "/analyses",
    jsonInit("POST", { ticker, analysis_type: analysisType, portfolio_id: portfolioId }),
    "Could not start the analysis"
  );
  if (!result.ok) return { error: result.error, values };

  const { data } = await result.res.json();
  // The analysis now runs in the background; its page follows it live.
  redirect(`/analyses/${data.id}`);
}

export type RecordDecisionState = {
  error?: string;
  success?: boolean;
  // The submitted notes, so the form keeps them after an error.
  notes?: string;
};

export async function recordDecision(
  analysisId: string,
  portfolioId: string | null,
  _prevState: RecordDecisionState,
  formData: FormData
): Promise<RecordDecisionState> {
  const actionTaken = formData.get("action_taken");
  const notes = formData.get("notes");

  if (typeof actionTaken !== "string" || !DECISION_ACTIONS.includes(actionTaken)) {
    return { error: "Please choose what you did with this recommendation" };
  }
  if (typeof notes === "string" && notes.trim().length > DECISION_NOTES_MAX_LENGTH) {
    return { error: `Notes can be at most ${DECISION_NOTES_MAX_LENGTH} characters`, notes };
  }

  const result = await call(
    `/analyses/${encodeURIComponent(analysisId)}/decisions`,
    jsonInit("POST", {
      action_taken: actionTaken,
      portfolio_id: portfolioId,
      notes: typeof notes === "string" && notes.trim() ? notes.trim() : null,
    }),
    "Could not record your decision"
  );
  if (!result.ok) {
    return { error: result.error, notes: typeof notes === "string" ? notes : undefined };
  }

  // "followed" may change tier statuses shown on the analysis page.
  revalidatePath(`/analyses/${analysisId}`);
  return { success: true };
}
