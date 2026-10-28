"use client";

import { useActionState } from "react";
import { updateProfile, type ProfileValues, type UpdateProfileState } from "@/app/lib/actions/profile";
import { FormMessage } from "@/app/form-message";
import { TIMEZONE_PATTERN, TIMEZONE_TITLE, USERNAME_PATTERN, USERNAME_TITLE } from "@/app/(public)/field-rules";

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
          autoComplete="username"
          type="text"
          required
          minLength={3}
          maxLength={32}
          pattern={USERNAME_PATTERN}
          title={USERNAME_TITLE}
          defaultValue={v.username}
        />
      </div>
      <div className="field">
        <label htmlFor="first_name">First name</label>
        <input id="first_name" name="first_name" autoComplete="given-name" type="text" maxLength={100} defaultValue={v.first_name} />
      </div>
      <div className="field">
        <label htmlFor="last_name">Last name</label>
        <input id="last_name" name="last_name" autoComplete="family-name" type="text" maxLength={100} defaultValue={v.last_name} />
      </div>
      <div className="field">
        <label htmlFor="bio">Bio</label>
        <textarea id="bio" name="bio" autoComplete="off" rows={3} maxLength={500} defaultValue={v.bio} />
      </div>
      <div className="field">
        <label htmlFor="timezone">Timezone</label>
        <input
          id="timezone"
          name="timezone"
          autoComplete="off"
          type="text"
          maxLength={64}
          pattern={TIMEZONE_PATTERN}
          title={TIMEZONE_TITLE}
          placeholder="Europe/Paris"
          defaultValue={v.timezone}
        />
      </div>
      <div className="field">
        <label htmlFor="country">Country</label>
        <input id="country" name="country" autoComplete="country-name" type="text" maxLength={100} defaultValue={v.country} />
      </div>
      <FormMessage text={state.error} />
      <FormMessage kind="success" text={state.success ? "Saved." : null} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Save changes"}
      </button>
    </form>
  );
}
