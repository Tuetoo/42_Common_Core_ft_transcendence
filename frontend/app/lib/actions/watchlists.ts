"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type CreateWatchlistState = {
  error?: string;
};

export async function createWatchlist(
  _prevState: CreateWatchlistState,
  formData: FormData
): Promise<CreateWatchlistState> {
  const name = formData.get("name");
  if (typeof name !== "string" || !name.trim()) {
    return { error: "Please enter a watchlist name" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/watchlists", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create watchlist") };
  }

  revalidatePath("/watchlists");
  return {};
}

export type AddWatchlistItemState = {
  error?: string;
};

export async function addWatchlistItem(
  watchlistId: string,
  _prevState: AddWatchlistItemState,
  formData: FormData
): Promise<AddWatchlistItemState> {
  const ticker = formData.get("ticker");
  if (typeof ticker !== "string" || !ticker.trim()) {
    return { error: "Please enter a ticker" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/watchlists/${watchlistId}/items`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: ticker.toUpperCase() }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not add ticker") };
  }

  revalidatePath("/watchlists");
  return {};
}

export async function removeWatchlistItem(watchlistId: string, ticker: string) {
  const accessToken = await getAccessToken();
  if (!accessToken) return;

  await backendFetch(`/watchlists/${watchlistId}/items/${ticker}`, accessToken, {
    method: "DELETE",
  });

  revalidatePath("/watchlists");
}
