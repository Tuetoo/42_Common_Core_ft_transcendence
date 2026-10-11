"use client";

import { useActionState } from "react";
import { updateProfile, type ProfileValues, type UpdateProfileState } from "@/app/lib/actions/profile";

const initialState: UpdateProfileState = {};

// Limits match MeUpdate in services/auth-service/domains/auth/schema.py.
// React resets the form after each action; the fields then show what the
// action returned after an error, or the saved values after a success.
export function EditProfileForm({ defaults }: { defaults: ProfileValues }) {
  const [state, formAction, pending] = useActionState(updateProfile, initialState);
  const v = state.values ?? defaults;

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="username">Username</label>
        <input
          id="username"
          name="username"
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
        <label htmlFor="first_name">First name</label>
        <input id="first_name" name="first_name" type="text" maxLength={100} defaultValue={v.first_name} />
      </div>
      <div className="field">
        <label htmlFor="last_name">Last name</label>
        <input id="last_name" name="last_name" type="text" maxLength={100} defaultValue={v.last_name} />
      </div>
      <div className="field">
        <label htmlFor="bio">Bio</label>
        <textarea id="bio" name="bio" rows={3} maxLength={500} defaultValue={v.bio} />
      </div>
      <div className="field">
        <label htmlFor="timezone">Timezone</label>
        <input
          id="timezone"
          name="timezone"
          type="text"
          maxLength={64}
          pattern="[A-Za-z0-9_+\-\/]+"
          title="An IANA timezone name such as Europe/Paris"
          placeholder="Europe/Paris"
          defaultValue={v.timezone}
        />
      </div>
      <div className="field">
        <label htmlFor="country">Country</label>
        <input id="country" name="country" type="text" maxLength={100} defaultValue={v.country} />
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
