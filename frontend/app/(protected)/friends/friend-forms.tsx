"use client";

import { useActionState, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  respondToConnection,
  sendFriendRequest,
  type ConnectionResponse,
  type FriendActionState,
} from "@/app/lib/actions/friends";

const initialState: FriendActionState = {};

export function AddFriendForm() {
  const [state, formAction, pending] = useActionState(sendFriendRequest, initialState);

  return (
    <form action={formAction} className="form">
      <div className="flex items-end gap-2 flex-wrap">
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
            defaultValue={state.username ?? ""}
          />
        </div>
        <button type="submit" disabled={pending} className="btn btn-primary">
          {pending ? "Sending…" : "Send request"}
        </button>
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state?.success && (
        <p aria-live="polite" className="form-message success">
          Request sent.
        </p>
      )}
    </form>
  );
}

export function ConnectionButton({
  connectionId,
  kind,
  label,
  primary = false,
}: {
  connectionId: string;
  kind: ConnectionResponse;
  label: string;
  primary?: boolean;
}) {
  const action = respondToConnection.bind(null, connectionId, kind);
  const [state, formAction, pending] = useActionState(action, initialState);

  return (
    <form action={formAction} className="flex flex-col items-end gap-1">
      <button type="submit" disabled={pending} className={primary ? "btn btn-primary" : "btn btn-ghost"}>
        {pending ? "…" : label}
      </button>
      {state?.error && (
        <span aria-live="polite" className="form-message error" style={{ fontSize: 12 }}>
          {state.error}
        </span>
      )}
    </form>
  );
}

// Online status is computed by the backend from recent activity; re-fetch
// the page now and then so the dots don't go stale while it stays open.
export function AutoRefresh({ seconds }: { seconds: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => router.refresh(), seconds * 1000);
    return () => clearInterval(timer);
  }, [router, seconds]);
  return null;
}
