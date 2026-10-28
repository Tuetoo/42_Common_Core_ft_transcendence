"use client";

import { useActionState } from "react";
import { updateOrganization, type OrganizationFormState } from "@/app/lib/actions/organizations";
import { FormMessage } from "@/app/form-message";
import { ORG_DESCRIPTION_MAX, ORG_NAME_MAX } from "@/app/lib/format";

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
        <input id="org-name" name="name" autoComplete="off" type="text" required maxLength={ORG_NAME_MAX} defaultValue={v.name} />
      </div>
      <div className="field">
        <label htmlFor="org-description">Description</label>
        <textarea id="org-description" name="description" autoComplete="off" rows={3} maxLength={ORG_DESCRIPTION_MAX} defaultValue={v.description} />
      </div>
      <FormMessage text={state.error} />
      <FormMessage kind="success" text={state.success ? "Saved." : null} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Save changes"}
      </button>
    </form>
  );
}
