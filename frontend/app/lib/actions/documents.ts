"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";
import { UUID_RE } from "@/app/(protected)/research/limits";

// Uploads do not go through a Server Action (their body is capped at 6MB in
// next.config.ts, and they can't report progress). The browser posts the
// file to the route handler app/(protected)/research/documents/upload.

export type DeleteDocumentState = {
  error?: string;
};

// DELETE /research/documents/{id}: only the owner may delete; the backend
// removes the row, its RAG chunks and the stored object.
export async function deleteDocument(documentId: string): Promise<DeleteDocumentState> {
  if (!UUID_RE.test(documentId)) {
    return { error: "Invalid document" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/research/documents/${documentId}`, accessToken, { method: "DELETE" });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok && res.status !== 404) {
    return { error: await extractErrorMessage(res, "Could not delete the document") };
  }

  revalidatePath("/research/documents");
  return {};
}
