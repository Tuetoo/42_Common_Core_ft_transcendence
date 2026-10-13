"use client";

import { useActionState, useState } from "react";
import { moderateById, type ModerateByIdState } from "@/app/lib/actions/admin";

const initialState: ModerateByIdState = {};
const UUID_PATTERN = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

// Kind and id are controlled so they survive the form reset React does
// after each action. Delete needs a second click on "Confirm delete".
export function ModerateByIdForm() {
  const [state, formAction, pending] = useActionState(moderateById, initialState);
  const [kind, setKind] = useState("note");
  const [id, setId] = useState("");
  const [armed, setArmed] = useState(false);

  return (
    <form
      action={(formData) => {
        setArmed(false);
        formAction(formData);
      }}
      className="form"
    >
      <div className="filter-row">
        <div className="field">
          <label htmlFor="mod-kind">Type</label>
          <select id="mod-kind" name="kind" autoComplete="off" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="note">Note</option>
            <option value="comment">Comment</option>
          </select>
        </div>
        <div className="field min-w-0">
          <label htmlFor="mod-id">Id</label>
          <input
            id="mod-id"
            name="id"
            autoComplete="off"
            type="text"
            required
            maxLength={36}
            pattern={UUID_PATTERN}
            title="An id: 32 hex digits in the form 8-4-4-4-12"
            value={id}
            onChange={(e) => {
              setId(e.target.value);
              setArmed(false);
            }}
            className="mono"
          />
        </div>
      </div>
      {state.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      {state.message && (
        <p aria-live="polite" className="form-message success">
          {state.message}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" name="intent" value="hide" disabled={pending} className="btn btn-primary">
          Hide
        </button>
        <button type="submit" name="intent" value="unhide" disabled={pending} className="btn btn-ghost">
          Unhide
        </button>
        {armed ? (
          <>
            <button type="submit" name="intent" value="delete" disabled={pending} className="btn btn-ghost">
              Confirm delete
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setArmed(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" disabled={pending || !id} className="btn btn-ghost" onClick={() => setArmed(true)}>
            Delete
          </button>
        )}
      </div>
    </form>
  );
}
