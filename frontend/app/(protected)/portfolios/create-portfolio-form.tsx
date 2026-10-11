"use client";

import { useActionState } from "react";
import { createPortfolio, type CreatePortfolioState } from "@/app/lib/actions/portfolios";

const initialState: CreatePortfolioState = {};

export function CreatePortfolioForm() {
  const [state, formAction, pending] = useActionState(createPortfolio, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input id="name" name="name" type="text" required />
      </div>
      <div className="field">
        <label htmlFor="currency">Currency</label>
        <select id="currency" name="currency" defaultValue="USD">
          <option value="USD">USD</option>
          <option value="EUR">EUR</option>
          <option value="GBP">GBP</option>
        </select>
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create portfolio"}
      </button>
    </form>
  );
}
