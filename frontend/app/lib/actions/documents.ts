"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type UploadDocumentState = {
  error?: string;
};

export async function uploadDocument(
  _prevState: UploadDocumentState,
  formData: FormData
): Promise<UploadDocumentState> {
  const file = formData.get("file");
  const category = formData.get("category");

  if (!(file instanceof File) || file.size === 0) {
    return { error: "Please choose a file" };
  }
  if (typeof category !== "string" || !category) {
    return { error: "Please choose a category" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    // formData already has the right field names (file, category, ticker) —
    // forward it as-is. Don't set a Content-Type header: fetch sets the
    // multipart boundary itself when the body is a FormData object.
    res = await backendFetch("/research/documents", accessToken, {
      method: "POST",
      body: formData,
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not upload document") };
  }

  revalidatePath("/research/documents");
  return {};
}
