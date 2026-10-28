"use client";

import { useActionState } from "react";
import { createOrganization, type OrganizationFormState } from "@/app/lib/actions/organizations";
import { FormMessage } from "@/app/form-message";
import { ORG_DESCRIPTION_MAX, ORG_NAME_MAX } from "@/app/lib/format";

const initialState: OrganizationFormState = {};

// On success the action opens the new organization. After an error it
// returns what was typed, and the reset form shows it again.
export function CreateOrganizationForm() {
  const [state, formAction, pending] = useActionState(createOrganization, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="name">Name</label>
        <input id="name" name="name" autoComplete="off" type="text" required maxLength={ORG_NAME_MAX} defaultValue={state.values?.name ?? ""} />
      </div>
      <div className="field">
        <label htmlFor="description">Description (optional)</label>
        <textarea
          id="description"
          name="description"
          autoComplete="off"
          rows={3}
          maxLength={ORG_DESCRIPTION_MAX}
          defaultValue={state.values?.description ?? ""}
        />
      </div>
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create organization"}
      </button>
    </form>
  );
}
