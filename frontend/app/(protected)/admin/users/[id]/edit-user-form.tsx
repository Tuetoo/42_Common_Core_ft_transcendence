"use client";

import { useActionState } from "react";
import { updateUserByAdmin, type EditUserState, type EditUserValues } from "@/app/lib/actions/admin";

const initialState: EditUserState = {};

// Limits match AdminUserUpdate in services/auth-service/domains/admin/schema.py.
// After a failed save the action returns what was typed; React resets the
// form after every action, so the fields take those values as defaults.
export function EditUserForm({
  userId,
  original,
  defaults,
}: {
  userId: string;
  original: { username: string; email: string; is_verified: boolean };
  defaults: EditUserValues;
}) {
  const action = updateUserByAdmin.bind(null, userId, original);
  const [state, formAction, pending] = useActionState(action, initialState);
  const v = state.values ?? defaults;

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="admin-username">Username</label>
        <input
          id="admin-username"
          name="username"
          autoComplete="off"
          type="text"
          required
          minLength={3}
          maxLength={32}
          pattern="[A-Za-z0-9_.\-]{3,32}"
          title="3-32 characters: letters, digits, underscore, dot or hyphen"
          defaultValue={v.username}
        />
      </div>
      <div className="field">
        <label htmlFor="admin-email">Email</label>
        <input id="admin-email" name="email" autoComplete="off" type="email" required maxLength={254} defaultValue={v.email} />
      </div>
      <label className="flex items-center gap-2 text-[13px] text-[var(--text-secondary)]">
        <input type="checkbox" name="is_verified" autoComplete="off" defaultChecked={v.is_verified} />
        Email verified
      </label>
      <div className="field">
        <label htmlFor="admin-first-name">First name</label>
        <input id="admin-first-name" name="first_name" autoComplete="off" type="text" maxLength={100} defaultValue={v.first_name} />
      </div>
      <div className="field">
        <label htmlFor="admin-last-name">Last name</label>
        <input id="admin-last-name" name="last_name" autoComplete="off" type="text" maxLength={100} defaultValue={v.last_name} />
      </div>
      <div className="field">
        <label htmlFor="admin-bio">Bio</label>
        <textarea id="admin-bio" name="bio" autoComplete="off" rows={3} maxLength={500} defaultValue={v.bio} />
      </div>
      <div className="field">
        <label htmlFor="admin-timezone">Timezone</label>
        <input
          id="admin-timezone"
          name="timezone"
          autoComplete="off"
          type="text"
          maxLength={64}
          pattern="[A-Za-z0-9_+\-\/]+"
          title="An IANA timezone name such as Europe/Paris"
          placeholder="Europe/Paris"
          defaultValue={v.timezone}
        />
      </div>
      <div className="field">
        <label htmlFor="admin-country">Country</label>
        <input id="admin-country" name="country" autoComplete="off" type="text" maxLength={100} defaultValue={v.country} />
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
