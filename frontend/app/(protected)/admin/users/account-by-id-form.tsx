"use client";

import { useActionState, useState } from "react";
import { setUserActiveById, type AccountByIdState } from "@/app/lib/actions/admin";

const initialState: AccountByIdState = {};
const UUID_PATTERN = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

// For moderators, who cannot list users: the button pressed sends
// intent=deactivate or intent=reactivate together with the id.
export function AccountByIdForm() {
  const [state, formAction, pending] = useActionState(setUserActiveById, initialState);
  const [userId, setUserId] = useState("");

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="user_id">User id</label>
        <input
          id="user_id"
          name="user_id"
          autoComplete="off"
          type="text"
          required
          pattern={UUID_PATTERN}
          title="A user id: 32 hex digits in the form 8-4-4-4-12"
          maxLength={36}
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className="mono"
        />
      </div>
      {state.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state.message && (
        <p aria-live="polite" className="form-message success">
          {state.message}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" name="intent" value="deactivate" disabled={pending} className="btn btn-primary">
          Deactivate
        </button>
        <button type="submit" name="intent" value="reactivate" disabled={pending} className="btn btn-ghost">
          Reactivate
        </button>
      </div>
    </form>
  );
}
