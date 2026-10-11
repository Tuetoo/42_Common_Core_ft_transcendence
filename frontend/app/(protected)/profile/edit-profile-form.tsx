"use client";

import { useActionState } from "react";
import { updateProfile, type UpdateProfileState } from "@/app/lib/actions/profile";

const initialState: UpdateProfileState = {};

type Defaults = {
  username: string;
  first_name: string | null;
  last_name: string | null;
  bio: string | null;
};

export function EditProfileForm({ defaults }: { defaults: Defaults }) {
  const [state, formAction, pending] = useActionState(updateProfile, initialState);

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
          defaultValue={defaults.username}
        />
      </div>
      <div className="field">
        <label htmlFor="first_name">First name</label>
        <input id="first_name" name="first_name" type="text" defaultValue={defaults.first_name ?? ""} />
      </div>
      <div className="field">
        <label htmlFor="last_name">Last name</label>
        <input id="last_name" name="last_name" type="text" defaultValue={defaults.last_name ?? ""} />
      </div>
      <div className="field">
        <label htmlFor="bio">Bio</label>
        <textarea id="bio" name="bio" rows={3} defaultValue={defaults.bio ?? ""} />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state?.success && (
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
