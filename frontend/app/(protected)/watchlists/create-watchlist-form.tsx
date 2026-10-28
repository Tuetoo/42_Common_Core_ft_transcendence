"use client";

import { useActionState } from "react";
import { createWatchlist, type CreateWatchlistState } from "@/app/lib/actions/watchlists";
import { NAME_MAX_LENGTH } from "@/app/lib/format";
import { FormMessage } from "@/app/form-message";

const initialState: CreateWatchlistState = {};

export function CreateWatchlistForm() {
  const [state, formAction, pending] = useActionState(createWatchlist, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input id="name" name="name" autoComplete="off" type="text" required maxLength={NAME_MAX_LENGTH} defaultValue={state.name} />
      </div>
      <FormMessage text={state?.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create watchlist"}
      </button>
    </form>
  );
}
