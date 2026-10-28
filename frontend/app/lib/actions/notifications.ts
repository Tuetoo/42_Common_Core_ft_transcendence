"use server";

import { revalidatePath } from "next/cache";
import { call, getAccessToken, getUnreadTotal, jsonInit } from "@/app/lib/session";

type Notification = { id: string };

export type MarkAllReadState = { error?: string };

// The list page size the backend allows (page_size at most 100), and the
// number of rounds one click may take.
const PAGE = 100;
const MAX_ROUNDS = 10;

// The backend marks notifications read by id, so "mark all" first lists the
// unread ones, then sends their ids back, a page at a time. Any failure, or
// unread ones left after the last round, comes back as a message to show.
export async function markAllNotificationsRead(): Promise<MarkAllReadState> {
  let error: string | undefined;
  for (let round = 0; ; round++) {
    const list = await call(
      `/notifications?unread_only=true&page_size=${PAGE}`,
      { cache: "no-store" },
      "Could not load the unread notifications"
    );
    if (!list.ok) {
      error = list.error;
      break;
    }
    let items: Notification[];
    try {
      ({ items } = (await list.res.json()) as { items: Notification[] });
    } catch {
      error = "Could not load the unread notifications";
      break;
    }
    if (items.length === 0) break;
    if (round === MAX_ROUNDS) {
      error = "Some notifications are still unread. Please try again.";
      break;
    }
    const mark = await call(
      "/notifications/read",
      jsonInit("POST", { notification_ids: items.map((n) => n.id) }),
      "Could not mark the notifications as read"
    );
    if (!mark.ok) {
      error = mark.error;
      break;
    }
  }

  // The unread badge lives in the shared layout.
  revalidatePath("/", "layout");
  return error ? { error } : {};
}

// The unread badge re-reads the count whenever its socket (re)opens, since
// notifications sent while it was down never reached it. null: unknown.
export async function getUnreadCount(): Promise<number | null> {
  const accessToken = await getAccessToken();
  return accessToken ? getUnreadTotal(accessToken) : null;
}
