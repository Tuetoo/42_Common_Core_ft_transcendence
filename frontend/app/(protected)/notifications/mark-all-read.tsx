"use client";

import { useActionState } from "react";
import { markAllNotificationsRead, type MarkAllReadState } from "@/app/lib/actions/notifications";
import { FormMessage } from "@/app/form-message";

const initial: MarkAllReadState = {};

export function MarkAllReadButton() {
  const [state, formAction, pending] = useActionState(markAllNotificationsRead, initial);
  return (
    <form action={formAction} className="flex flex-col items-end gap-1">
      <button type="submit" className="link-btn" disabled={pending}>
        {pending ? "Marking…" : "Mark all as read"}
      </button>
      <FormMessage text={state.error} />
    </form>
  );
}
