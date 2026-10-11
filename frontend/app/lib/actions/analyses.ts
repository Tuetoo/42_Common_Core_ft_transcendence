"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { readApiError } from "@/app/lib/api-error";
import { ANALYSIS_TYPES, DECISION_ACTIONS, DECISION_NOTES_MAX_LENGTH, normalizeTicker } from "@/app/lib/format";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in", values };
  }

  let res: Response;
  try {
    res = await backendFetch("/analyses", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, analysis_type: analysisType, portfolio_id: portfolioId }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?", values };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not start the analysis");
    // The backend's own messages for these are terse; say what to do instead.
    if (res.status === 429) {
      return { error: "You already have 3 analyses running. Wait for one to finish, then try again.", values };
    }
    if (res.status === 503) {
      return { error: "The analysis queue is full right now. Try again in a minute.", values };
    }
    return { error: err.message, values };
  }

  const { data } = await res.json();
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

  const accessToken = await getAccessToken();
  const keptNotes = typeof notes === "string" ? notes : undefined;
  if (!accessToken) {
    return { error: "You're not logged in", notes: keptNotes };
  }

  let res: Response;
  try {
    res = await backendFetch(`/analyses/${encodeURIComponent(analysisId)}/decisions`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action_taken: actionTaken,
        portfolio_id: portfolioId,
        notes: typeof notes === "string" && notes.trim() ? notes.trim() : null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?", notes: keptNotes };
  }

  if (!res.ok) {
    return { error: (await readApiError(res, "Could not record your decision")).message, notes: keptNotes };
  }

  // "followed" may change tier statuses shown on the analysis page.
  revalidatePath(`/analyses/${analysisId}`);
  return { success: true };
}
