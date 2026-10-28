"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { call, jsonInit } from "@/app/lib/session";
import {
  COMMENT_MAX,
  NOTE_CONTENT_MAX,
  NOTE_TITLE_MAX,
  parseTags,
} from "@/app/(protected)/research/limits";
import { UUID_RE } from "@/app/lib/format";

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

  const result = await call(
    "/research/notes",
    jsonInit("POST", {
      title: title.trim(),
      content: content.trim() ? content : null,
      tags,
      organization_id: organizationId,
    }),
    "Could not create note"
  );
  if (!result.ok) return { values, error: result.error };

  const { data } = (await result.res.json()) as { data: { id: string } };
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

  const result = await call(`/research/notes/${noteId}/comments`, jsonInit("POST", { content }), "Could not add comment");
  if (!result.ok) return { values, error: result.error };

  revalidatePath(`/research/${noteId}`);
  return { posted: Date.now() };
}

// Deletes a note (only its author may; the backend checks again) and goes
// back to the notes list. Bound to the note id; used with ActionButton.
export async function deleteNote(
  noteId: string,
  _prevState: { error?: string; message?: string },
  _formData: FormData
): Promise<{ error?: string; message?: string }> {
  if (!UUID_RE.test(noteId)) return { error: "Invalid note id" };
  const result = await call(`/research/notes/${noteId}`, { method: "DELETE" }, "Could not delete the note");
  if (!result.ok && result.status === 401) redirect("/login");
  if (!result.ok && result.status !== 404) return { error: result.error };
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

  const result = await call(`/research/documents/${documentId}`, { method: "DELETE" }, "Could not delete the document");
  if (!result.ok && result.status !== 404) return { error: result.error };

  revalidatePath("/research/documents");
  return {};
}
