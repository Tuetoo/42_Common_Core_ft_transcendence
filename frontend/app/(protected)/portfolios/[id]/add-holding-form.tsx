"use client";

import { useActionState } from "react";
import { addHolding, type AddHoldingState } from "@/app/lib/actions/portfolios";

const initialState: AddHoldingState = {};

export function AddHoldingForm({ portfolioId }: { portfolioId: string }) {
  const addHoldingForPortfolio = addHolding.bind(null, portfolioId);
  const [state, formAction, pending] = useActionState(addHoldingForPortfolio, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="ticker">Ticker</label>
        <input id="ticker" name="ticker" type="text" required />
      </div>
      <div className="field">
        <label htmlFor="quantity">Quantity</label>
        <input id="quantity" name="quantity" type="number" step="any" min="0" required />
      </div>
      <div className="field">
        <label htmlFor="average_cost">Average cost</label>
        <input id="average_cost" name="average_cost" type="number" step="any" min="0" required />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Adding…" : "Add holding"}
      </button>
    </form>
  );
}
