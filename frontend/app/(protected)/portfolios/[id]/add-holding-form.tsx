"use client";

import { useActionState, useId } from "react";
import { addHolding, type AddHoldingState } from "@/app/lib/actions/portfolios";
import { MIN_POSITIVE_AMOUNT, TICKER_PATTERN, TICKER_TITLE, upperCaseInput } from "@/app/lib/format";

const initialState: AddHoldingState = {};

export function AddHoldingForm({ portfolioId }: { portfolioId: string }) {
  const addHoldingForPortfolio = addHolding.bind(null, portfolioId);
  const [state, formAction, pending] = useActionState(addHoldingForPortfolio, initialState);
  // Both portfolio forms sit on one page, so field ids must be unique.
  const uid = useId();
  const values = state.values ?? {};

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor={`${uid}-ticker`}>Ticker</label>
        <input id={`${uid}-ticker`} name="ticker"
          type="text"
          required
          maxLength={10}
          pattern={TICKER_PATTERN}
          title={TICKER_TITLE}
          autoCapitalize="characters"
          autoComplete="off"
          onInput={upperCaseInput}
          defaultValue={values.ticker}
        />
      </div>
      <div className="field">
        <label htmlFor={`${uid}-quantity`}>Quantity</label>
        <input id={`${uid}-quantity`} name="quantity"
          autoComplete="off"
          type="number"
          inputMode="decimal"
          step="any"
          min={MIN_POSITIVE_AMOUNT}
          required
          defaultValue={values.quantity}
        />
      </div>
      <div className="field">
        <label htmlFor={`${uid}-average_cost`}>Average cost</label>
        <input id={`${uid}-average_cost`} name="average_cost"
          autoComplete="off"
          type="number"
          inputMode="decimal"
          step="any"
          min={MIN_POSITIVE_AMOUNT}
          required
          defaultValue={values.average_cost}
        />
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
