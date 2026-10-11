"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { readApiError } from "@/app/lib/api-error";

export type StartAnalysisState = {
  error?: string;
};

export async function startAnalysis(
  _prevState: StartAnalysisState,
  formData: FormData
): Promise<StartAnalysisState> {
  const ticker = formData.get("ticker");
  const analysisType = formData.get("analysis_type");
  const portfolioId = formData.get("portfolio_id");

  if (typeof ticker !== "string" || !ticker.trim()) {
    return { error: "Please enter a ticker" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/analyses", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: ticker.trim().toUpperCase(),
        analysis_type: typeof analysisType === "string" && analysisType ? analysisType : "full",
        // An empty select value means "not tied to a portfolio".
        portfolio_id: typeof portfolioId === "string" && portfolioId ? portfolioId : null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not start the analysis");
    // The backend's own messages for these are terse; say what to do instead.
    if (res.status === 429) {
      return { error: "You already have 3 analyses running. Wait for one to finish, then try again." };
    }
    if (res.status === 503) {
      return { error: "The analysis queue is full right now. Try again in a minute." };
    }
    return { error: err.message };
  }

  const { data } = await res.json();
  // The analysis now runs in the background; its page follows it live.
  redirect(`/analyses/${data.id}`);
}

export type RecordDecisionState = {
  error?: string;
  success?: boolean;
};

export async function recordDecision(
  analysisId: string,
  portfolioId: string | null,
  _prevState: RecordDecisionState,
  formData: FormData
): Promise<RecordDecisionState> {
  const actionTaken = formData.get("action_taken");
  const notes = formData.get("notes");

  if (typeof actionTaken !== "string" || !actionTaken) {
    return { error: "Please choose what you did with this recommendation" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/analyses/${analysisId}/decisions`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action_taken: actionTaken,
        portfolio_id: portfolioId,
        notes: typeof notes === "string" && notes.trim() ? notes.trim() : null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: (await readApiError(res, "Could not record your decision")).message };
  }

  // "followed" may change tier statuses shown on the analysis page.
  revalidatePath(`/analyses/${analysisId}`);
  return { success: true };
}
