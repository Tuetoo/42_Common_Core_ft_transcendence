"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, extractErrorMessage } from "@/app/lib/backend";
import { boundedText, NAME_MAX_LENGTH, normalizeTicker } from "@/app/lib/format";

export type CreateWatchlistState = {
  error?: string;
  name?: string;
};

export async function createWatchlist(
  _prevState: CreateWatchlistState,
  formData: FormData
): Promise<CreateWatchlistState> {
  const raw = formData.get("name");
  const submitted = typeof raw === "string" ? raw : "";
  const name = boundedText(raw, NAME_MAX_LENGTH);
  if (!name) {
    return { error: `Please enter a name of 1 to ${NAME_MAX_LENGTH} characters`, name: submitted };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in", name: submitted };
  }

  let res: Response;
  try {
    res = await backendFetch("/watchlists", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?", name: submitted };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create watchlist"), name: submitted };
  }

  revalidatePath("/watchlists");
  return {};
}

export type AddWatchlistItemState = {
  error?: string;
  ticker?: string;
};

export async function addWatchlistItem(
  watchlistId: string,
  _prevState: AddWatchlistItemState,
  formData: FormData
): Promise<AddWatchlistItemState> {
  const raw = formData.get("ticker");
  const submitted = typeof raw === "string" ? raw : "";
  const ticker = normalizeTicker(raw);
  if (!ticker) {
    return { error: "Ticker must be 1-10 characters: letters, digits, '.' or '-'", ticker: submitted };
  }
  if (ticker === "CASH") {
    return { error: "CASH is reserved and can't be watched", ticker: submitted };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in", ticker: submitted };
  }

  let res: Response;
  try {
    res = await backendFetch(`/watchlists/${encodeURIComponent(watchlistId)}/items`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?", ticker: submitted };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not add ticker"), ticker: submitted };
  }

  revalidatePath("/watchlists");
  return {};
}

export type RemoveWatchlistItemState = {
  error?: string;
};

// Used with useActionState by the remove buttons, so a failure (for example
// a read-only role, or the item already gone) is shown instead of ignored.
export async function removeWatchlistItem(
  watchlistId: string,
  ticker: string
): Promise<RemoveWatchlistItemState> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(
      `/watchlists/${encodeURIComponent(watchlistId)}/items/${encodeURIComponent(ticker)}`,
      accessToken,
      { method: "DELETE" }
    );
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  // 404: someone (another tab) already removed it; the refreshed list says so.
  if (!res.ok && res.status !== 404) {
    return { error: await extractErrorMessage(res, `Could not remove ${ticker}`) };
  }

  revalidatePath("/watchlists");
  return {};
}
