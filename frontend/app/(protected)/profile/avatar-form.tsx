"use client";

import { useActionState, useState } from "react";
import { uploadAvatar, type UploadAvatarState } from "@/app/lib/actions/profile";
import { AVATAR_TYPES, avatarFileError } from "@/app/(public)/field-rules";

const initialState: UploadAvatarState = {};

// A request the server refuses before the action runs (for example a body
// over Next's Server Action limit) rejects the call. Turn that into a form
// message instead of letting it replace the page with the error boundary.
async function uploadAvatarSafely(prev: UploadAvatarState, formData: FormData): Promise<UploadAvatarState> {
  try {
    return await uploadAvatar(prev, formData);
  } catch {
    return { error: "Could not upload the picture. Check that it is 5MB or smaller and try again." };
  }
}

export function AvatarForm() {
  const [state, formAction, pending] = useActionState(uploadAvatarSafely, initialState);
  const [fileError, setFileError] = useState<string | null>(null);

  // Checked as soon as a file is picked: an invalid file blocks the submit
  // with a clear message instead of being sent at all.
  function checkFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    const error = file ? avatarFileError(file) : null;
    setFileError(error);
    e.target.setCustomValidity(error ?? "");
  }

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="avatar">New picture</label>
        <input
          id="avatar"
          name="avatar"
          type="file"
          accept={AVATAR_TYPES.join(",")}
          required
          onChange={checkFile}
          aria-invalid={fileError ? true : undefined}
        />
        {fileError && (
          <span role="alert" className="form-message error">
            {fileError}
          </span>
        )}
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
