"use server";

import { redirect } from "next/navigation";
import { clearSession, getRefreshToken, setSession } from "@/app/lib/session";
import { extractErrorMessage, readApiError } from "@/app/lib/api-error";

// Only same-site paths: "/x" yes, "//evil.com" or "https://evil.com" no —
// otherwise a crafted ?next= link could bounce a user to another site
// right after they log in (an open redirect).
function safeNext(next: FormDataEntryValue | null): string {
  if (typeof next === "string" && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\")) {
    return next;
  }
  return "/portfolios";
}

export type LoginState = {
  error?: string;
  needsTwoFactor?: boolean;
};

export async function login(
  _prevState: LoginState,
  formData: FormData
): Promise<LoginState> {
  const email = formData.get("email");
  const password = formData.get("password");
  const totpCode = formData.get("totp_code");

  if (typeof email !== "string" || typeof password !== "string" || !email || !password) {
    return { error: "Please enter your email and password" };
  }

  let res: Response;
  try {
    res = await fetch(`${process.env.BACKEND_API_URL}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        totp_code: typeof totpCode === "string" && totpCode ? totpCode : null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Login failed, please try again later");
    // two_factor_required means "ask for the 6-digit code and resubmit",
    // not a real failure.
    if (err.code === "two_factor_required") {
      return { needsTwoFactor: true };
    }
    // A wrong code should keep the code field on screen for another try.
    const wasTwoFactorStep = typeof totpCode === "string" && totpCode !== "";
    return { error: err.message, needsTwoFactor: wasTwoFactorStep };
  }

  const { data } = await res.json();
  await setSession(data.access_token, data.refresh_token);

  redirect(safeNext(formData.get("next")));
}

export type RegisterState = {
  error?: string;
  success?: boolean;
};

export async function register(
  _prevState: RegisterState,
  formData: FormData
): Promise<RegisterState> {
  const email = formData.get("email");
  const username = formData.get("username");
  const password = formData.get("password");

  if (
    typeof email !== "string" || typeof username !== "string" || typeof password !== "string" ||
    !email || !username || !password
  ) {
    return { error: "Please fill in all fields" };
  }

  let res: Response;
  try {
    res = await fetch(`${process.env.BACKEND_API_URL}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, username, password }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Registration failed, please try again later") };
  }

  return { success: true };
}

export async function logout() {
  const refreshToken = await getRefreshToken();

  if (refreshToken) {
    try {
      await fetch(`${process.env.BACKEND_API_URL}/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
    } catch {
      // Best-effort: even if the backend call fails, still clear the local session below.
    }
  }

  await clearSession();
  redirect("/login");
}
