"use client";

import { useActionState, useState } from "react";
import { createNote, type CreateNoteState } from "@/app/lib/actions/research";
import { MAX_TAGS, NOTE_CONTENT_MAX, NOTE_TITLE_MAX, TAG_MAX_CHARS, parseTags } from "./limits";
import type { Organization } from "./types";
import { FormMessage } from "@/app/form-message";

const initialState: CreateNoteState = {};

// After an error the action returns what was typed, and the reset form
// shows it again.
export function CreateNoteForm({ organizations }: { organizations: Organization[] }) {
  const [state, formAction, pending] = useActionState(createNote, initialState);
  const [tagError, setTagError] = useState<string | null>(null);

  function checkTags(e: React.ChangeEvent<HTMLInputElement>) {
    const { error } = parseTags(e.target.value);
    setTagError(error);
    e.target.setCustomValidity(error ?? "");
  }

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="title">Title</label>
        <input id="title" name="title" autoComplete="off" type="text" required maxLength={NOTE_TITLE_MAX} defaultValue={state.values?.title ?? ""} />
      </div>
      <div className="field">
        <label htmlFor="content">Content</label>
        <textarea
          id="content"
          name="content"
          autoComplete="off"
          rows={4}
          maxLength={NOTE_CONTENT_MAX}
          defaultValue={state.values?.content ?? ""}
        />
      </div>
      <div className="field">
        <label htmlFor="tags">Tags (comma-separated)</label>
        <input
          id="tags"
          name="tags"
          autoComplete="off"
          type="text"
          placeholder="e.g. AAPL, tech"
          defaultValue={state.values?.tags ?? ""}
          onChange={checkTags}
          aria-describedby="tags-help"
          aria-invalid={tagError ? true : undefined}
        />
        <span id="tags-help" className="text-xs text-[var(--text-faint)]">
          Up to {MAX_TAGS} tags, each at most {TAG_MAX_CHARS} characters.
        </span>
        {tagError && (
          <span role="alert" className="form-message error">
            {tagError}
          </span>
        )}
      </div>
      <div className="field">
        <label htmlFor="note-organization">Share with</label>
        <select
          id="note-organization"
          name="organization_id"
          autoComplete="off"
          defaultValue={state.values?.organization_id ?? ""}
          aria-describedby="share-help"
        >
          <option value="">Nobody (private note)</option>
          {organizations.map((org) => (
            <option key={org.id} value={org.id}>
              Organization: {org.name}
            </option>
          ))}
        </select>
        <span id="share-help" className="text-xs text-[var(--text-faint)]">
          {organizations.length === 0
            ? "Join or create an organization to edit notes together."
            : "Members of the organization can read the note; owners, admins and members can also edit it live. This cannot be changed later."}
        </span>
      </div>
      <FormMessage text={state?.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Creating…" : "Create note"}
      </button>
    </form>
  );
}
