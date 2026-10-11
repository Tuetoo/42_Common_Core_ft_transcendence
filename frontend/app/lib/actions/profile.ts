"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type UpdateProfileState = {
  error?: string;
  success?: boolean;
};

export async function updateProfile(
  _prevState: UpdateProfileState,
  formData: FormData
): Promise<UpdateProfileState> {
  const username = formData.get("username");
  const firstName = formData.get("first_name");
  const lastName = formData.get("last_name");
  const bio = formData.get("bio");

  if (typeof username !== "string" || !username.trim()) {
    return { error: "Username can't be empty" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/auth/me", accessToken, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        first_name: firstName || null,
        last_name: lastName || null,
        bio: bio || null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not update profile") };
  }

  revalidatePath("/profile");
  return { success: true };
}

export type UploadAvatarState = {
  error?: string;
  success?: boolean;
};

// Same rules as the backend, checked here first for a quick, clear message.
// The backend still verifies the bytes really are that kind of image.
const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const AVATAR_MAX_BYTES = 5 * 1024 * 1024;

export async function uploadAvatar(
  _prevState: UploadAvatarState,
  formData: FormData
): Promise<UploadAvatarState> {
  const file = formData.get("avatar");
  if (!(file instanceof File) || file.size === 0) {
    return { error: "Please choose an image" };
  }
  if (!AVATAR_TYPES.includes(file.type)) {
    return { error: "Use a PNG, JPEG, WebP or GIF image" };
  }
  if (file.size > AVATAR_MAX_BYTES) {
    return { error: "The image must be 5MB or smaller" };
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
