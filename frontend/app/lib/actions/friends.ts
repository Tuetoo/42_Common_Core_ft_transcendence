"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { readApiError } from "@/app/lib/api-error";

export type FriendActionState = {
  error?: string;
  success?: boolean;
};

export async function sendFriendRequest(
  _prevState: FriendActionState,
  formData: FormData
): Promise<FriendActionState> {
  const username = formData.get("username");
  if (typeof username !== "string" || username.trim().length < 3) {
    return { error: "Enter the other person's username (at least 3 characters)" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/connections", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ addressee_username: username.trim() }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: (await readApiError(res, "Could not send the request")).message };
  }

  revalidatePath("/friends");
  return { success: true };
}

// What each button does to a connection. Checked against this list because
// the value arrives from the browser and must not pick an arbitrary route.
const RESPONSES = {
  accept: { method: "POST", suffix: "/accept" },
  block: { method: "POST", suffix: "/block" },
  remove: { method: "DELETE", suffix: "" },
} as const;

export type ConnectionResponse = keyof typeof RESPONSES;

export async function respondToConnection(
  connectionId: string,
  kind: ConnectionResponse,
  _prevState: FriendActionState,
  _formData: FormData
): Promise<FriendActionState> {
  const response = RESPONSES[kind];
  if (!response) {
    return { error: "Unknown action" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/connections/${encodeURIComponent(connectionId)}${response.suffix}`, accessToken, {
      method: response.method,
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    if (res.status === 403 && kind === "remove") {
      return { error: "Only the person who blocked can unblock." };
    }
    return { error: (await readApiError(res, "That didn't work")).message };
  }

  revalidatePath("/friends");
  return { success: true };
}
