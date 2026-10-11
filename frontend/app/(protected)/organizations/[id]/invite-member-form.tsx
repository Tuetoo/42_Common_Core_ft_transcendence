"use client";

import { useActionState } from "react";
import { inviteMember, type InviteMemberState } from "@/app/lib/actions/organizations";

const initialState: InviteMemberState = {};

const ROLES = ["member", "admin", "viewer"];

export function InviteMemberForm({ orgId }: { orgId: string }) {
  const inviteForOrg = inviteMember.bind(null, orgId);
  const [state, formAction, pending] = useActionState(inviteForOrg, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="email">Email</label>
        <input id="email" name="email" type="email" required />
      </div>
      <div className="field">
        <label htmlFor="role">Role</label>
        <select id="role" name="role" defaultValue="member">
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state?.success && (
        <p aria-live="polite" className="form-message success">
          Invitation sent.
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Sending…" : "Send invitation"}
      </button>
    </form>
  );
}
