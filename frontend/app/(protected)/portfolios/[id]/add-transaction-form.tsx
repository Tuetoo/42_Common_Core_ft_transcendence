"use client";

import { useActionState, useId, useState, type FormEvent } from "react";
import { addTransaction, type AddTransactionState } from "@/app/lib/actions/portfolios";
import { MIN_POSITIVE_AMOUNT, TICKER_PATTERN, TICKER_TITLE, upperCaseInput } from "@/app/lib/format";
import { FormMessage } from "@/app/form-message";

const initialState: AddTransactionState = {};

type TransactionKind = "buy" | "sell" | "deposit" | "withdrawal";

// The datetime-local value is the user's wall-clock time with no zone. The
// browser knows the zone, so it converts the value to an exact instant here
// and puts it in a hidden field; the server action sends that instant. React
// builds the action's FormData after onSubmit handlers run.
function fillInstant(event: FormEvent<HTMLFormElement>) {
  const form = event.currentTarget;
  const local = form.elements.namedItem("transaction_date");
  const instant = form.elements.namedItem("transaction_date_iso");
  if (!(local instanceof HTMLInputElement) || !(instant instanceof HTMLInputElement)) return;
  const date = local.value ? new Date(local.value) : null;
  instant.value = date && !Number.isNaN(date.getTime()) ? date.toISOString() : "";
}

export function AddTransactionForm({ portfolioId }: { portfolioId: string }) {
  const addTransactionForPortfolio = addTransaction.bind(null, portfolioId);
  const [state, formAction, pending] = useActionState(addTransactionForPortfolio, initialState);
  // Both portfolio forms sit on one page, so field ids must be unique.
  const uid = useId();
  const values = state.values ?? {};
  const initialKind = (values.transaction_type as TransactionKind | undefined) ?? "deposit";
  const [kind, setKind] = useState<TransactionKind>(initialKind);
  // React resets the form after each submit, which puts the type select back
  // to its default (the submitted type after an error, "deposit" after a
  // success). Follow that here, adjusting state during render.
  const [seenState, setSeenState] = useState(state);
  if (seenState !== state) {
    setSeenState(state);
    setKind(initialKind);
  }
  const isCash = kind === "deposit" || kind === "withdrawal";

  return (
    <form action={formAction} onSubmit={fillInstant} className="form">
      <div className="field">
        <label htmlFor={`${uid}-transaction_type`}>Type</label>
        <select
          id={`${uid}-transaction_type`}
          name="transaction_type"
          autoComplete="off"
          defaultValue={initialKind}
          onChange={(e) => setKind(e.target.value as TransactionKind)}
        >
          <option value="deposit">Deposit cash</option>
          <option value="withdrawal">Withdraw cash</option>
          <option value="buy">Buy</option>
          <option value="sell">Sell</option>
        </select>
      </div>
      {!isCash && (
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
      )}
      <div className="field">
        <label htmlFor={`${uid}-quantity`}>{isCash ? "Amount" : "Quantity"}</label>
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
      {!isCash && (
        <div className="field">
          <label htmlFor={`${uid}-price`}>Price</label>
          <input id={`${uid}-price`} name="price"
            autoComplete="off"
            type="number"
            inputMode="decimal"
            step="any"
            min={MIN_POSITIVE_AMOUNT}
            required
            defaultValue={values.price}
          />
        </div>
      )}
      <div className="field">
        <label htmlFor={`${uid}-transaction_date`}>Date</label>
        <input id={`${uid}-transaction_date`} name="transaction_date"
          autoComplete="off"
          type="datetime-local"
          required
          defaultValue={values.transaction_date}
        />
        <input type="hidden" name="transaction_date_iso" defaultValue="" />
      </div>
      {isCash ? null : (
        <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
          Buying needs enough cash in the portfolio — deposit first if needed.
        </p>
      )}
      <FormMessage text={state?.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Recording…" : "Record transaction"}
      </button>
    </form>
  );
}
