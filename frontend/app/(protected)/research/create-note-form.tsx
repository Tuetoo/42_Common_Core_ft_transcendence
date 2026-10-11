"use client";

import { useActionState } from "react";
import { createNote, type CreateNoteState } from "@/app/lib/actions/research";

const initialState: CreateNoteState = {};

export function CreateNoteForm() {
  const [state, formAction, pending] = useActionState(createNote, initialState);

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="title">Title</label>
        <input id="title" name="title" type="text" required />
      </div>
      <div className="field">
        <label htmlFor="content">Content</label>
        <textarea id="content" name="content" rows={4} />
      </div>
      <div className="field">
        <label htmlFor="tags">Tags (comma-separated)</label>
        <input id="tags" name="tags" type="text" placeholder="e.g. AAPL, tech" />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create note"}
      </button>
    </form>
  );
}
