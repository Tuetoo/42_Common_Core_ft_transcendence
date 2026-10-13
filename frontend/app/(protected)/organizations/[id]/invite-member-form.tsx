"use client";

import { useActionState } from "react";
import { inviteMember, type InviteMemberState } from "@/app/lib/actions/organizations";

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
          maxLength={254}
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
      {state.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state.success && (
        <p aria-live="polite" className="form-message success">
          {state.success}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Sending…" : "Send invitation"}
      </button>
    </form>
  );
}
