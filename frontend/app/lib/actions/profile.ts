"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";
import { avatarFileError } from "@/app/(public)/field-rules";

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

// Same limits as MeUpdate in services/auth-service/domains/auth/schema.py.
const USERNAME_RE = /^[A-Za-z0-9_.-]{3,32}$/;

function isTimezone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

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

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { values, error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/auth/me", accessToken, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: values.username,
        first_name: values.first_name || null,
        last_name: values.last_name || null,
        bio: values.bio || null,
        timezone: values.timezone || null,
        country: values.country || null,
      }),
    });
  } catch {
    return { values, error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { values, error: await extractErrorMessage(res, "Could not update profile") };
  }

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

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  const upload = new FormData();
  upload.append("file", file);

  let res: Response;
  try {
    // No Content-Type header: fetch sets multipart/form-data together with
    // the boundary string the backend needs to split the parts.
    res = await backendFetch("/auth/me/avatar", accessToken, { method: "POST", body: upload });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not upload the avatar") };
  }

  // The new picture shows on the profile and in the sidebar.
  revalidatePath("/", "layout");
  return { success: true };
}
