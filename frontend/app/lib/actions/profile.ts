"use server";

import { revalidatePath } from "next/cache";
import { call, jsonInit } from "@/app/lib/session";
import { USERNAME_RE, avatarFileError, isTimezone } from "@/app/(public)/field-rules";

export type ProfileValues = {
  username: string;
  first_name: string;
  last_name: string;
  bio: string;
  timezone: string;
  country: string;
};

export type UpdateProfileState = {
  error?: string;
  success?: boolean;
  values?: ProfileValues;
};

function validateProfile(v: ProfileValues): string | null {
  if (!USERNAME_RE.test(v.username)) {
    return "Username: 3-32 characters, only letters, digits, underscore, dot or hyphen";
  }
  if (v.first_name.length > 100) return "First name: at most 100 characters";
  if (v.last_name.length > 100) return "Last name: at most 100 characters";
  if (v.bio.length > 500) return "Bio: at most 500 characters";
  if (v.timezone && (v.timezone.length > 64 || !isTimezone(v.timezone))) {
    return "Timezone must be an IANA name such as Europe/Paris";
  }
  if (v.country.length > 100) return "Country: at most 100 characters";
  return null;
}

// PATCH /auth/me. Empty optional fields are sent as null, which clears them.
// After an error the typed values come back so the form can show them again.
export async function updateProfile(
  _prevState: UpdateProfileState,
  formData: FormData
): Promise<UpdateProfileState> {
  const text = (name: string) => String(formData.get(name) ?? "").trim();
  const values: ProfileValues = {
    username: text("username"),
    first_name: text("first_name"),
    last_name: text("last_name"),
    bio: text("bio"),
    timezone: text("timezone"),
    country: text("country"),
  };
  const invalid = validateProfile(values);
  if (invalid) {
    return { values, error: invalid };
  }

  const result = await call(
    "/auth/me",
    jsonInit("PATCH", {
      username: values.username,
      first_name: values.first_name || null,
      last_name: values.last_name || null,
      bio: values.bio || null,
      timezone: values.timezone || null,
      country: values.country || null,
    }),
    "Could not update profile"
  );
  if (!result.ok) return { values, error: result.error };

  // The username also shows in the sidebar.
  revalidatePath("/", "layout");
  return { success: true };
}

export type UploadAvatarState = {
  error?: string;
  success?: boolean;
};

export async function uploadAvatar(
  _prevState: UploadAvatarState,
  formData: FormData
): Promise<UploadAvatarState> {
  const file = formData.get("avatar");
  if (!(file instanceof File) || file.size === 0) {
    return { error: "Please choose an image" };
  }
  // Same rules as the backend, checked here first for a quick, clear
  // message. The backend still verifies the bytes really are that image type.
  const fileError = avatarFileError(file);
  if (fileError) {
    return { error: fileError };
  }

  const upload = new FormData();
  upload.append("file", file);

  // No Content-Type header: fetch sets multipart/form-data together with
  // the boundary string the backend needs to split the parts.
  const result = await call("/auth/me/avatar", { method: "POST", body: upload }, "Could not upload the avatar");
  if (!result.ok) return { error: result.error };

  // The new picture shows on the profile and in the sidebar.
  revalidatePath("/", "layout");
  return { success: true };
}

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
  const result = await call("/auth/2fa/setup", { method: "POST" }, "Could not start 2FA setup");
  if (!result.ok) {
    if (result.status === 409) {
      return { alreadyEnabled: true, error: result.error };
    }
    if (result.code === "recent_login_required") {
      return { needsRelogin: true, error: result.error };
    }
    return { error: result.error };
  }

  const { data } = await result.res.json();
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

  const result = await call("/auth/2fa/verify", jsonInit("POST", { code }), "Invalid code");
  if (!result.ok) {
    return { error: result.error, needsRelogin: result.code === "recent_login_required" };
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

  const result = await call("/auth/2fa/disable", jsonInit("POST", { code }), "Invalid code");
  if (!result.ok) return { error: result.error };

  revalidatePath("/profile");
  return { success: true };
}
