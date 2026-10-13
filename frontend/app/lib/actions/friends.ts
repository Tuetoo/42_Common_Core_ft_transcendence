"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";

export type FriendActionState = {
  error?: string;
  success?: boolean;
  username?: string;
};

// Same rule as USERNAME_PATTERN in the auth service; the backend checks
// 3-32 characters (ConnectionCreate) and then looks the user up.
const USERNAME_RE = /^[A-Za-z0-9_.-]{3,32}$/;

export async function sendFriendRequest(
  _prevState: FriendActionState,
  formData: FormData
): Promise<FriendActionState> {
  const username = String(formData.get("username") ?? "").trim();
  if (!USERNAME_RE.test(username)) {
    return {
      username,
      error: "Usernames are 3-32 characters: letters, digits, underscore, dot or hyphen",
    };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { username, error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/connections", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ addressee_username: username }),
    });
  } catch {
    return { username, error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { username, error: (await readApiError(res, "Could not send the request")).message };
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

export async function respondToConnection(connectionId: string, kind: ConnectionResponse): Promise<FriendActionState> {
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
