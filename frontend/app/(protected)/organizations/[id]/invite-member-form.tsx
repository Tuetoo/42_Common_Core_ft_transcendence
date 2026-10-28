"use client";

import { useActionState } from "react";
import { inviteMember, type InviteMemberState } from "@/app/lib/actions/organizations";
import { FormMessage } from "@/app/form-message";
import { EMAIL_MAX } from "@/app/(public)/field-rules";

const initialState: InviteMemberState = {};

// `roles` lists what this member may grant: only the owner invites admins.
export function InviteMemberForm({ orgId, roles }: { orgId: string; roles: readonly string[] }) {
  const inviteForOrg = inviteMember.bind(null, orgId);
  const [state, formAction, pending] = useActionState(inviteForOrg, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="invite-email">Email</label>
        <input
          id="invite-email"
          name="email"
          autoComplete="off"
          type="email"
          required
          maxLength={EMAIL_MAX}
          defaultValue={state.values?.email ?? ""}
          placeholder="name@example.com"
        />
      </div>
      <div className="field">
        <label htmlFor="invite-role">Role</label>
        <select id="invite-role" name="role" autoComplete="off" defaultValue={state.values?.role ?? "member"}>
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>
      <FormMessage text={state.error} />
      <FormMessage kind="success" text={state.success} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Sending…" : "Send invitation"}
      </button>
    </form>
  );
}
