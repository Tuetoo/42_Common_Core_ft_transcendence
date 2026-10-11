"use client";

import { useActionState } from "react";
import { createWatchlist, type CreateWatchlistState } from "@/app/lib/actions/watchlists";

const initialState: CreateWatchlistState = {};

export function CreateWatchlistForm() {
  const [state, formAction, pending] = useActionState(createWatchlist, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input id="name" name="name" type="text" required />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create watchlist"}
      </button>
    </form>
  );
}
