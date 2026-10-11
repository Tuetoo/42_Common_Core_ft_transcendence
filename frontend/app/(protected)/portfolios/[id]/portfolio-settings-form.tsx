"use client";

import { useActionState } from "react";
import { updatePortfolio, type UpdatePortfolioState } from "@/app/lib/actions/portfolios";
import { NAME_MAX_LENGTH } from "@/app/lib/format";

const initialState: UpdatePortfolioState = {};

// Rename a portfolio and, when it belongs to an organization, choose whether
// that organization's members can see it.
export function PortfolioSettingsForm({
  portfolioId,
  name,
  visibility,
  organizationName,
}: {
  portfolioId: string;
  name: string;
  visibility: string;
  organizationName: string | null;
}) {
  const updateThisPortfolio = updatePortfolio.bind(null, portfolioId);
  const [state, formAction, pending] = useActionState(updateThisPortfolio, initialState);
  const values = state.values ?? {};

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="portfolio-name">Name</label>
        <input
          id="portfolio-name"
          name="name"
          type="text"
          required
          maxLength={NAME_MAX_LENGTH}
          defaultValue={values.name ?? name}
        />
      </div>
      {organizationName !== null && (
        <div className="field">
          <label htmlFor="portfolio-visibility">Who can see it</label>
          <select id="portfolio-visibility" name="visibility" defaultValue={values.visibility ?? visibility}>
            <option value="private">Only me</option>
            <option value="organization">Members of {organizationName}</option>
          </select>
        </div>
      )}
      {state.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state.success && (
        <p aria-live="polite" className="form-message success">
          Saved.
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-ghost">
        {pending ? "Saving…" : "Save settings"}
      </button>
    </form>
  );
}
