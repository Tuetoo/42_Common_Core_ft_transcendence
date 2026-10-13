"use server";

import { redirect } from "next/navigation";
import { clearSession, getAccessToken } from "@/app/lib/session";
import { backendFetch, publicFetch, readApiError } from "@/app/lib/backend";

// Actions behind the three links auth-service puts in emails
// (verify-email, reset-password, invitations/accept). Each one is triggered
// by a button press rather than on page load: some mail scanners open
// links ahead of the user, which would otherwise burn a single-use token.

export type LinkActionState = {
  error?: string;
  success?: boolean;
};

function postJson(path: string, body: unknown) {
  return publicFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const UNREACHABLE = "Could not reach the server. Is the backend running?";

export async function verifyEmail(
  token: string,
  _prevState: LinkActionState,
  _formData: FormData
): Promise<LinkActionState> {
  let res: Response;
  try {
    res = await postJson("/auth/verify-email", { token });
  } catch {
    return { error: UNREACHABLE };
  }
  if (!res.ok) {
    return { error: (await readApiError(res, "This verification link is invalid or has expired.")).message };
  }
  return { success: true };
}

export async function requestPasswordReset(
  _prevState: LinkActionState,
  formData: FormData
): Promise<LinkActionState> {
  const email = formData.get("email");
  if (typeof email !== "string" || !email.trim()) {
    return { error: "Please enter your email" };
  }
  let res: Response;
  try {
    res = await postJson("/auth/password-reset/request", { email });
  } catch {
    return { error: UNREACHABLE };
  }
  if (!res.ok) {
    return { error: (await readApiError(res, "Could not send the reset email")).message };
  }
  return { success: true };
}

export async function confirmPasswordReset(
  token: string,
  _prevState: LinkActionState,
  formData: FormData
): Promise<LinkActionState> {
  const password = formData.get("new_password");
  const confirm = formData.get("confirm_password");
  if (typeof password !== "string" || !password) {
    return { error: "Please enter a new password" };
  }
  if (password !== confirm) {
    return { error: "The two passwords don't match" };
  }
  let res: Response;
  try {
    res = await postJson("/auth/password-reset/confirm", { token, new_password: password });
  } catch {
    return { error: UNREACHABLE };
  }
  if (!res.ok) {
    return { error: (await readApiError(res, "This reset link is invalid or has expired.")).message };
  }
  // A password reset revokes every session server-side; drop ours too.
  await clearSession();
  return { success: true };
}

export async function acceptInvitation(
  token: string,
  _prevState: LinkActionState,
  _formData: FormData
): Promise<LinkActionState> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }
  let res: Response;
  try {
    // The token goes in the body, never the URL, so it stays out of access logs.
    res = await backendFetch("/organizations/invitations/accept", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
  } catch {
    return { error: UNREACHABLE };
  }
  if (!res.ok) {
    return { error: (await readApiError(res, "Could not accept this invitation")).message };
  }
  const { data } = await res.json();
  redirect(`/organizations/${data.organization_id}`);
}

export type ResendState = {
  error?: string;
  sent?: boolean;
};

// POST /auth/verify-email/resend answers 200 whether or not the email
// belongs to an account, so `sent` never tells the user an account exists.
export async function resendVerification(
  _prevState: ResendState,
  formData: FormData
): Promise<ResendState> {
  const email = formData.get("email");
  if (typeof email !== "string" || !email.trim()) {
    return { error: "Please enter your email" };
  }

  let res: Response;
  try {
    res = await publicFetch("/auth/verify-email/resend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email.trim() }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (res.status === 429) {
    return { error: "Too many requests for this email. Please wait a while and try again." };
  }
  if (!res.ok) {
    return { error: (await readApiError(res, "Could not send the verification email")).message };
  }
  return { sent: true };
}
