"use client";

import { useActionState } from "react";
import { addWatchlistItem, type AddWatchlistItemState } from "@/app/lib/actions/watchlists";
import { TICKER_PATTERN, TICKER_TITLE, upperCaseInput } from "@/app/lib/format";
import { FormMessage } from "@/app/form-message";

const initialState: AddWatchlistItemState = {};

export function AddTickerForm({ watchlistId }: { watchlistId: string }) {
  const addItemToWatchlist = addWatchlistItem.bind(null, watchlistId);
  const [state, formAction, pending] = useActionState(addItemToWatchlist, initialState);

  return (
    <form action={formAction} className="flex flex-wrap items-end gap-2">
      <div className="field min-w-0" style={{ maxWidth: 160 }}>
        <label htmlFor={`ticker-${watchlistId}`}>Add ticker</label>
        <input
          id={`ticker-${watchlistId}`}
          name="ticker"
          type="text"
          required
          maxLength={10}
          pattern={TICKER_PATTERN}
          title={TICKER_TITLE}
          autoCapitalize="characters"
          autoComplete="off"
          onInput={upperCaseInput}
          defaultValue={state.ticker}
        />
      </div>
      <button type="submit" disabled={pending} className="btn btn-ghost">
        {pending ? "Adding…" : "Add"}
      </button>
      <FormMessage text={state?.error} className="w-full" />
    </form>
  );
}
