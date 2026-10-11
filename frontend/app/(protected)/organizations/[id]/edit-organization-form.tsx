"use client";

import { useActionState } from "react";
import { updateOrganization, type OrganizationFormState } from "@/app/lib/actions/organizations";

const initialState: OrganizationFormState = {};

export function EditOrganizationForm({
  orgId,
  defaults,
}: {
  orgId: string;
  defaults: { name: string; description: string };
}) {
  const [state, formAction, pending] = useActionState(updateOrganization.bind(null, orgId), initialState);
  const v = state.values ?? defaults;

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="org-name">Name</label>
        <input id="org-name" name="name" type="text" required maxLength={128} defaultValue={v.name} />
      </div>
      <div className="field">
        <label htmlFor="org-description">Description</label>
        <textarea id="org-description" name="description" rows={3} maxLength={1000} defaultValue={v.description} />
      </div>
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
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Save changes"}
      </button>
    </form>
  );
}
