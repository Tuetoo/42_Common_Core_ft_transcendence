"use client";

import { useActionState } from "react";
import { createOrganization, type OrganizationFormState } from "@/app/lib/actions/organizations";

const initialState: OrganizationFormState = {};

// On success the action opens the new organization. After an error it
// returns what was typed, and the reset form shows it again.
export function CreateOrganizationForm() {
  const [state, formAction, pending] = useActionState(createOrganization, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input id="name" name="name" autoComplete="off" type="text" required maxLength={128} defaultValue={state.values?.name ?? ""} />
      </div>
      <div className="field">
        <label htmlFor="description">Description (optional)</label>
        <textarea
          id="description"
          name="description"
          autoComplete="off"
          rows={3}
          maxLength={1000}
          defaultValue={state.values?.description ?? ""}
        />
      </div>
      {state.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create organization"}
      </button>
    </form>
  );
}
