"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";
import {
  COMMENT_MAX,
  NOTE_CONTENT_MAX,
  NOTE_TITLE_MAX,
  UUID_RE,
  parseTags,
} from "@/app/(protected)/research/limits";

export type CreateNoteState = {
  error?: string;
};

// POST /research/notes (services/core-service/domains/research/api.py,
// body ResearchNoteCreate). organization_id shares the note with that
// organization: its members can read it, and owners/admins/members can
// edit it live (ResearchService.note_access).
export async function createNote(
  _prevState: CreateNoteState,
  formData: FormData
): Promise<CreateNoteState> {
  const title = formData.get("title");
  const content = formData.get("content");
  const tagsRaw = formData.get("tags");
  const organizationRaw = formData.get("organization_id");

  if (typeof title !== "string" || !title.trim()) {
    return { error: "Please enter a title" };
  }
  if (title.trim().length > NOTE_TITLE_MAX) {
    return { error: `The title must be at most ${NOTE_TITLE_MAX} characters` };
  }
  if (typeof content === "string" && content.length > NOTE_CONTENT_MAX) {
    return { error: `The content must be at most ${NOTE_CONTENT_MAX.toLocaleString("en-US")} characters` };
  }
  const { tags, error: tagError } = parseTags(typeof tagsRaw === "string" ? tagsRaw : "");
  if (tagError) {
    return { error: tagError };
  }
  let organizationId: string | null = null;
  if (typeof organizationRaw === "string" && organizationRaw) {
    if (!UUID_RE.test(organizationRaw)) {
      return { error: "Please choose a valid organization" };
    }
    organizationId = organizationRaw;
  }

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
        title: title.trim(),
        content: typeof content === "string" && content.trim() ? content : null,
        tags,
        organization_id: organizationId,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create note") };
  }

  const { data } = (await res.json()) as { data: { id: string } };
  revalidatePath("/research");
  // Open the new note right away: that is where the live editor is.
  redirect(`/research/${data.id}`);
}

export type AddCommentState = {
  error?: string;
  // Changes after every successful post so the form can reset itself.
  posted?: number;
};

// POST /research/notes/{id}/comments (body ResearchCommentCreate:
// content 1..10000 characters).
export async function addComment(
  noteId: string,
  _prevState: AddCommentState,
  formData: FormData
): Promise<AddCommentState> {
  const content = formData.get("content");

  if (typeof content !== "string" || !content.trim()) {
    return { error: "Please enter a comment" };
  }
  if (content.length > COMMENT_MAX) {
    return { error: `A comment must be at most ${COMMENT_MAX.toLocaleString("en-US")} characters` };
  }
  if (!UUID_RE.test(noteId)) {
    return { error: "Invalid note" };
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
  return { posted: Date.now() };
}

const NOTE_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Deletes a note (only its author may; the backend checks again) and goes
// back to the notes list. Bound to the note id; used with ActionButton.
export async function deleteNote(
  noteId: string,
  _prevState: { error?: string; message?: string },
  _formData: FormData
): Promise<{ error?: string; message?: string }> {
  if (!NOTE_ID_RE.test(noteId)) return { error: "Invalid note id" };
  const accessToken = await getAccessToken();
  if (!accessToken) redirect("/login");
  const res = await backendFetch(`/research/notes/${noteId}`, accessToken, { method: "DELETE" });
  if (res.status === 401) redirect("/login");
  if (!res.ok && res.status !== 404) {
    return { error: await extractErrorMessage(res, "Could not delete the note") };
  }
  revalidatePath("/research");
  redirect("/research");
}
