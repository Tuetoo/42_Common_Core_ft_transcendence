"use server";

import { revalidatePath } from "next/cache";
import { call, jsonInit } from "@/app/lib/session";
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

  const result = await call("/watchlists", jsonInit("POST", { name }), "Could not create watchlist");
  if (!result.ok) return { error: result.error, name: submitted };

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

  const result = await call(
    `/watchlists/${encodeURIComponent(watchlistId)}/items`,
    jsonInit("POST", { ticker }),
    "Could not add ticker"
  );
  if (!result.ok) return { error: result.error, ticker: submitted };

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
  const result = await call(
    `/watchlists/${encodeURIComponent(watchlistId)}/items/${encodeURIComponent(ticker)}`,
    { method: "DELETE" },
    `Could not remove ${ticker}`
  );
  // 404: someone (another tab) already removed it; the refreshed list says so.
  if (!result.ok && result.status !== 404) return { error: result.error };

  revalidatePath("/watchlists");
  return {};
}
