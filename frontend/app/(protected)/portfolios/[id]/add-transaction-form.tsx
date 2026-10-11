"use client";

import { useActionState, useState } from "react";
import { addTransaction, type AddTransactionState } from "@/app/lib/actions/portfolios";

const initialState: AddTransactionState = {};

type TransactionKind = "buy" | "sell" | "deposit" | "withdrawal";

export function AddTransactionForm({ portfolioId }: { portfolioId: string }) {
  const addTransactionForPortfolio = addTransaction.bind(null, portfolioId);
  const [state, formAction, pending] = useActionState(addTransactionForPortfolio, initialState);
  const [kind, setKind] = useState<TransactionKind>("deposit");
  const isCash = kind === "deposit" || kind === "withdrawal";

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="transaction_type">Type</label>
        <select
          id="transaction_type"
          name="transaction_type"
          value={kind}
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
          <label htmlFor="ticker">Ticker</label>
          <input id="ticker" name="ticker" type="text" required />
        </div>
      )}
      <div className="field">
        <label htmlFor="quantity">{isCash ? "Amount" : "Quantity"}</label>
        <input id="quantity" name="quantity" type="number" step="any" min="0" required />
      </div>
      {!isCash && (
        <div className="field">
          <label htmlFor="price">Price</label>
          <input id="price" name="price" type="number" step="any" min="0" required />
        </div>
      )}
      <div className="field">
        <label htmlFor="transaction_date">Date</label>
        <input id="transaction_date" name="transaction_date" type="datetime-local" required />
      </div>
      {isCash ? null : (
        <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
          Buying needs enough cash in the portfolio — deposit first if needed.
        </p>
      )}
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Recording…" : "Record transaction"}
      </button>
    </form>
  );
}
