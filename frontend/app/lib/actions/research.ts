"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type CreateNoteState = {
  error?: string;
};

export async function createNote(
  _prevState: CreateNoteState,
  formData: FormData
): Promise<CreateNoteState> {
  const title = formData.get("title");
  const content = formData.get("content");
  const tagsRaw = formData.get("tags");

  if (typeof title !== "string" || !title.trim()) {
    return { error: "Please enter a title" };
  }

  const tags =
    typeof tagsRaw === "string" && tagsRaw.trim()
      ? tagsRaw.split(",").map((t) => t.trim()).filter(Boolean)
      : [];

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/research/notes", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        content: typeof content === "string" && content.trim() ? content : null,
        tags,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create note") };
  }

  revalidatePath("/research");
  return {};
}

export type AddCommentState = {
  error?: string;
};

export async function addComment(
  noteId: string,
  _prevState: AddCommentState,
  formData: FormData
): Promise<AddCommentState> {
  const content = formData.get("content");

  if (typeof content !== "string" || !content.trim()) {
    return { error: "Please enter a comment" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/research/notes/${noteId}/comments`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not add comment") };
  }

  revalidatePath(`/research/${noteId}`);
  return {};
}
