"use server";

import { redirect } from "next/navigation";
import { setSession } from "@/app/lib/session";
import { readApiError } from "@/app/lib/api-error";

// Called by /oauth/complete once it has read the tokens from the URL
// fragment. Setting the session cookies has to happen on the server
// (httpOnly cookies can't be written from browser JavaScript).
export async function completeOAuthLogin(accessToken: string, refreshToken: string) {
  if (!accessToken || !refreshToken) {
    redirect("/login");
  }
  await setSession(accessToken, refreshToken);
  redirect("/portfolios");
}

export type OAuthTwoFactorState = {
  error?: string;
};

export async function completeOAuthTwoFactor(
  challengeToken: string,
  _prevState: OAuthTwoFactorState,
  formData: FormData
): Promise<OAuthTwoFactorState> {
  const code = formData.get("code");
  if (typeof code !== "string" || !code.trim()) {
    return { error: "Please enter the 6-digit code" };
  }

  let res: Response;
  try {
    res = await fetch(`${process.env.BACKEND_API_URL}/auth/oauth/2fa`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_token: challengeToken, code }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: (await readApiError(res, "Invalid code")).message };
  }

  const { data } = await res.json();
  await setSession(data.access_token, data.refresh_token);
  redirect("/portfolios");
}
