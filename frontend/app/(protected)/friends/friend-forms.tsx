"use client";

import { useActionState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import {
  respondToConnection,
  sendFriendRequest,
  type ConnectionResponse,
  type FriendActionState,
} from "@/app/lib/actions/friends";
import { PRESENCE_EVENT, useUserEvents } from "@/app/lib/use-channel";
import { FormMessage } from "@/app/form-message";
import { USERNAME_PATTERN, USERNAME_TITLE } from "@/app/(public)/field-rules";

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
            autoComplete="off"
            type="text"
            required
            minLength={3}
            maxLength={32}
            pattern={USERNAME_PATTERN}
            title={USERNAME_TITLE}
            defaultValue={state.username ?? ""}
          />
        </div>
        <button type="submit" disabled={pending} className="btn btn-primary">
          {pending ? "Sending…" : "Send request"}
        </button>
      </div>
      <FormMessage text={state?.error} />
      <FormMessage kind="success" text={state?.success ? "Request sent." : null} />
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
      <FormMessage text={state?.error} style={{ fontSize: 12 }} />
    </form>
  );
}

// The backend pushes a friend's presence change to this user's user:<id>
// channel; the sidebar's socket passes it on (useUserEvents), and the page
// re-renders with the new dots. Changes close together share one refresh.
export function LivePresence() {
  const router = useRouter();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useUserEvents((envelope) => {
    if (envelope.event !== PRESENCE_EVENT || timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      router.refresh();
    }, 1000);
  });
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    []
  );
  return null;
}

// Online status also expires on the backend after a while without
// activity, which sends no event; re-fetch the page now and then as well.
export function AutoRefresh({ seconds }: { seconds: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => router.refresh(), seconds * 1000);
    return () => clearInterval(timer);
  }, [router, seconds]);
  return null;
}
