"use client";

import { useActionState } from "react";
import { addComment, type AddCommentState } from "@/app/lib/actions/research";

const initialState: AddCommentState = {};

export function AddCommentForm({ noteId }: { noteId: string }) {
  const addCommentForNote = addComment.bind(null, noteId);
  const [state, formAction, pending] = useActionState(addCommentForNote, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="content">Comment</label>
        <textarea id="content" name="content" rows={3} required />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Posting…" : "Post comment"}
      </button>
    </form>
  );
}
