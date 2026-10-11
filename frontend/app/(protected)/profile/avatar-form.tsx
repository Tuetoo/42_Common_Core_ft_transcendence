"use client";

import { useActionState } from "react";
import { uploadAvatar, type UploadAvatarState } from "@/app/lib/actions/profile";

const initialState: UploadAvatarState = {};

export function AvatarForm() {
  const [state, formAction, pending] = useActionState(uploadAvatar, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="avatar">New picture</label>
        <input id="avatar" name="avatar" type="file" accept="image/png,image/jpeg,image/webp,image/gif" required />
      </div>
      <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>PNG, JPEG, WebP or GIF, up to 5MB.</p>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state?.success && (
        <p aria-live="polite" className="form-message success">
          Picture updated.
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Uploading…" : "Upload picture"}
      </button>
    </form>
  );
}
