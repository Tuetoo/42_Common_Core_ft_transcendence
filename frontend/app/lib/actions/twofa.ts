"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage, readApiError } from "@/app/lib/api-error";

export type SetupTwoFactorState = {
  error?: string;
  secret?: string;
  provisioningUri?: string;
  // /auth/me doesn't say whether 2FA is on; a 409 from /2fa/setup does.
  alreadyEnabled?: boolean;
  // /2fa/setup and /2fa/verify need a login from the last few minutes.
  needsRelogin?: boolean;
};

export async function setupTwoFactor(
  _prevState: SetupTwoFactorState,
  _formData: FormData
): Promise<SetupTwoFactorState> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/auth/2fa/setup", accessToken, { method: "POST" });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not start 2FA setup");
    if (err.status === 409) {
      return { alreadyEnabled: true, error: err.message };
    }
    if (err.code === "recent_login_required") {
      return { needsRelogin: true, error: err.message };
    }
    return { error: err.message };
  }

  const { data } = await res.json();
  return { secret: data.secret, provisioningUri: data.provisioning_uri };
}

export type VerifyTwoFactorState = {
  error?: string;
  success?: boolean;
  needsRelogin?: boolean;
};

export async function verifyTwoFactor(
  _prevState: VerifyTwoFactorState,
  formData: FormData
): Promise<VerifyTwoFactorState> {
  const code = formData.get("code");
  if (typeof code !== "string" || !code.trim()) {
    return { error: "Please enter the 6-digit code" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/auth/2fa/verify", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Invalid code");
    return { error: err.message, needsRelogin: err.code === "recent_login_required" };
  }

  revalidatePath("/profile");
  return { success: true };
}

export type DisableTwoFactorState = {
  error?: string;
  success?: boolean;
};

export async function disableTwoFactor(
  _prevState: DisableTwoFactorState,
  formData: FormData
): Promise<DisableTwoFactorState> {
  const code = formData.get("code");
  if (typeof code !== "string" || !code.trim()) {
    return { error: "Please enter the 6-digit code" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/auth/2fa/disable", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Invalid code") };
  }

  revalidatePath("/profile");
  return { success: true };
}
