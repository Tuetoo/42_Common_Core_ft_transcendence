"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, extractErrorMessage } from "@/app/lib/backend";
import {
  COMMENT_MAX,
  NOTE_CONTENT_MAX,
  NOTE_TITLE_MAX,
  UUID_RE,
  parseTags,
} from "@/app/(protected)/research/limits";

export type CreateNoteState = {
  error?: string;
  // What was typed, returned with an error so the reset form shows it again.
  values?: { title: string; content: string; tags: string; organization_id: string };
};

// POST /research/notes (services/core-service/domains/research/api.py,
// body ResearchNoteCreate). organization_id shares the note with that
// organization: its members can read it, and owners/admins/members can
// edit it live (ResearchService.note_access).
export async function createNote(
  _prevState: CreateNoteState,
  formData: FormData
): Promise<CreateNoteState> {
  const values = {
    title: String(formData.get("title") ?? ""),
    content: String(formData.get("content") ?? ""),
    tags: String(formData.get("tags") ?? ""),
    organization_id: String(formData.get("organization_id") ?? ""),
  };
  const { title, content, organization_id: organizationRaw } = values;

  if (!title.trim()) {
    return { values, error: "Please enter a title" };
  }
  if (title.trim().length > NOTE_TITLE_MAX) {
    return { values, error: `The title must be at most ${NOTE_TITLE_MAX} characters` };
  }
  if (content.length > NOTE_CONTENT_MAX) {
    return { values, error: `The content must be at most ${NOTE_CONTENT_MAX.toLocaleString("en-US")} characters` };
  }
  const { tags, error: tagError } = parseTags(values.tags);
  if (tagError) {
    return { values, error: tagError };
  }
  let organizationId: string | null = null;
  if (organizationRaw) {
    if (!UUID_RE.test(organizationRaw)) {
      return { values, error: "Please choose a valid organization" };
    }
    organizationId = organizationRaw;
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { values, error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/research/notes", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title.trim(),
        content: content.trim() ? content : null,
        tags,
        organization_id: organizationId,
      }),
    });
  } catch {
    return { values, error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { values, error: await extractErrorMessage(res, "Could not create note") };
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
  // The comment, returned with an error so the reset form shows it again.
  values?: { content: string };
};

// POST /research/notes/{id}/comments (body ResearchCommentCreate:
// content 1..10000 characters).
export async function addComment(
  noteId: string,
  _prevState: AddCommentState,
  formData: FormData
): Promise<AddCommentState> {
  const content = String(formData.get("content") ?? "");
  const values = { content };

  if (!content.trim()) {
    return { values, error: "Please enter a comment" };
  }
  if (content.length > COMMENT_MAX) {
    return { values, error: `A comment must be at most ${COMMENT_MAX.toLocaleString("en-US")} characters` };
  }
  if (!UUID_RE.test(noteId)) {
    return { values, error: "Invalid note" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { values, error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/research/notes/${noteId}/comments`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  } catch {
    return { values, error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { values, error: await extractErrorMessage(res, "Could not add comment") };
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
  let res: Response;
  try {
    res = await backendFetch(`/research/notes/${noteId}`, accessToken, { method: "DELETE" });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }
  if (res.status === 401) redirect("/login");
  if (!res.ok && res.status !== 404) {
    return { error: await extractErrorMessage(res, "Could not delete the note") };
  }
  revalidatePath("/research");
  redirect("/research");
}

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
