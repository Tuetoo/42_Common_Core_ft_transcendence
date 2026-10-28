"use client";

import { useActionState, useEffect, useRef } from "react";
import { addComment, type AddCommentState } from "@/app/lib/actions/research";
import { COMMENT_MAX } from "../limits";
import { FormMessage } from "@/app/form-message";

const initialState: AddCommentState = {};

export function AddCommentForm({ noteId }: { noteId: string }) {
  const addCommentForNote = addComment.bind(null, noteId);
  const [state, formAction, pending] = useActionState(addCommentForNote, initialState);
  const formRef = useRef<HTMLFormElement>(null);

  // Clear the box after each successful post. After an error the action
  // returns the comment, and the reset form shows it again.
  useEffect(() => {
    if (state.posted) formRef.current?.reset();
  }, [state.posted]);

  return (
    <form ref={formRef} action={formAction} className="form">
      <div className="field">
        <label htmlFor="comment-content">Comment</label>
        <textarea
          id="comment-content"
          name="content"
          autoComplete="off"
          rows={3}
          required
          maxLength={COMMENT_MAX}
          defaultValue={state.values?.content ?? ""}
          aria-describedby="comment-help"
        />
        <span id="comment-help" className="text-xs text-[var(--text-faint)]">
          Up to {COMMENT_MAX.toLocaleString("en-US")} characters. Sentiment is analysed after posting.
        </span>
      </div>
      <FormMessage text={state?.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Posting…" : "Post comment"}
      </button>
    </form>
  );
}
