"use client";

import { useActionState } from "react";
import { createPortfolio, type CreatePortfolioState } from "@/app/lib/actions/portfolios";
import { CURRENCIES, NAME_MAX_LENGTH } from "@/app/lib/format";
import { FormMessage } from "@/app/form-message";

const initialState: CreatePortfolioState = {};

export function CreatePortfolioForm({ organizations }: { organizations: { id: string; name: string }[] }) {
  const [state, formAction, pending] = useActionState(createPortfolio, initialState);
  const values = state.values ?? {};

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input
          id="name"
          name="name"
          autoComplete="off"
          type="text"
          required
          maxLength={NAME_MAX_LENGTH}
          defaultValue={values.name}
        />
      </div>
      <div className="field">
        <label htmlFor="currency">Currency</label>
        <select id="currency" name="currency" autoComplete="off" defaultValue={values.currency ?? "USD"}>
          {CURRENCIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="sharing">Who can see it</label>
        <select id="sharing" name="sharing" autoComplete="off" defaultValue={values.sharing ?? "private"}>
          <option value="private">Only me</option>
          {organizations.map((org) => (
            <option key={org.id} value={org.id}>
              Members of {org.name}
            </option>
          ))}
        </select>
        {organizations.length === 0 && (
          <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
            Join or create an organization to share a portfolio with its members.
          </p>
        )}
      </div>
      <FormMessage text={state?.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create portfolio"}
      </button>
    </form>
  );
}
