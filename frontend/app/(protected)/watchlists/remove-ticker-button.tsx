"use client";

import { useActionState } from "react";
import { removeWatchlistItem, type RemoveWatchlistItemState } from "@/app/lib/actions/watchlists";
import { FormMessage } from "@/app/form-message";

const initialState: RemoveWatchlistItemState = {};

export function RemoveTickerButton({ watchlistId, ticker }: { watchlistId: string; ticker: string }) {
  const removeThis = removeWatchlistItem.bind(null, watchlistId, ticker);
  const [state, formAction, pending] = useActionState(removeThis, initialState);

  return (
    <form action={formAction} className="flex flex-col gap-1">
      <button
        type="submit"
        disabled={pending}
        aria-label={`Remove ${ticker}`}
        title={`Remove ${ticker}`}
        className="pill pill-accent mono"
        style={{ border: "none", cursor: "pointer" }}
      >
        {ticker} {pending ? "…" : "×"}
      </button>
      <FormMessage text={state.error} />
    </form>
  );
}
