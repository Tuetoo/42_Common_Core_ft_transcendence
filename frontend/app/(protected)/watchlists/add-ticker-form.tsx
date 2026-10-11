"use client";

import { useActionState } from "react";
import { addWatchlistItem, type AddWatchlistItemState } from "@/app/lib/actions/watchlists";

const initialState: AddWatchlistItemState = {};

export function AddTickerForm({ watchlistId }: { watchlistId: string }) {
  const addItemToWatchlist = addWatchlistItem.bind(null, watchlistId);
  const [state, formAction, pending] = useActionState(addItemToWatchlist, initialState);

  return (
    <form action={formAction} className="flex items-end gap-2">
      <div className="field" style={{ maxWidth: 140 }}>
        <label htmlFor={`ticker-${watchlistId}`}>Add ticker</label>
        <input id={`ticker-${watchlistId}`} name="ticker" type="text" required />
      </div>
      <button type="submit" disabled={pending} className="btn btn-ghost">
        {pending ? "Adding…" : "Add"}
      </button>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
    </form>
  );
}
