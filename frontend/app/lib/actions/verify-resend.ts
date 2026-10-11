"use server";

import { publicFetch } from "@/app/lib/backend";
import { readApiError } from "@/app/lib/api-error";

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
