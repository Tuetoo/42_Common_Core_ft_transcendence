"use server";

import { revalidatePath } from "next/cache";
import { call, jsonInit } from "@/app/lib/session";
import { USERNAME_RE } from "@/app/(public)/field-rules";

export type FriendActionState = {
  error?: string;
  success?: boolean;
  username?: string;
};

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

  const result = await call("/connections", jsonInit("POST", { addressee_username: username }), "Could not send the request");
  if (!result.ok) return { username, error: result.error };

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

  const result = await call(
    `/connections/${encodeURIComponent(connectionId)}${response.suffix}`,
    { method: response.method },
    "That didn't work"
  );
  if (!result.ok) return { error: result.error };

  revalidatePath("/friends");
  return { success: true };
}
