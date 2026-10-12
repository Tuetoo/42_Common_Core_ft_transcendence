"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";

type Notification = { id: string };

// The backend marks notifications read by id, so "mark all" first lists the
// unread ones, then sends their ids back, a page at a time. If the server
// can't be reached it stops quietly; the page then shows what is still unread.
export async function markAllNotificationsRead(): Promise<void> {
  const accessToken = await getAccessToken();
  if (!accessToken) return;

  try {
    for (let round = 0; round < 10; round++) {
      const listRes = await backendFetch("/notifications?unread_only=true&page_size=100", accessToken, {
        cache: "no-store",
      });
      if (!listRes.ok) break;
      const { items }: { items: Notification[] } = await listRes.json();
      if (items.length === 0) break;

      const markRes = await backendFetch("/notifications/read", accessToken, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notification_ids: items.map((n) => n.id) }),
      });
      if (!markRes.ok) break;
    }
  } catch {
    // Network error: nothing more to do here.
  }

  // The unread badge lives in the shared layout.
  revalidatePath("/", "layout");
}
