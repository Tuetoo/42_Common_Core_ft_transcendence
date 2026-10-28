"use client";

import { useActionState, useState } from "react";
import type { ActionResult } from "@/app/lib/actions/admin";
import { FormMessage } from "@/app/form-message";

type BoundAction = (prev: ActionResult, formData: FormData) => Promise<ActionResult>;

const initialState: ActionResult = {};

// One button that runs a server action. With `confirmLabel` the first click
// only arms it: a second button ("Confirm ...") and a Cancel button appear,
// so a destructive action always takes two deliberate clicks.
export function ActionButton({
  action,
  label,
  pendingLabel,
  confirmLabel,
  primary = false,
  disabled = false,
  title,
}: {
  action: BoundAction;
  label: string;
  pendingLabel?: string;
  confirmLabel?: string;
  primary?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const [state, formAction, pending] = useActionState(action, initialState);
  const [armed, setArmed] = useState(false);
  const className = primary ? "btn btn-primary" : "btn btn-ghost";

  return (
    <form
      action={(formData) => {
        setArmed(false);
        formAction(formData);
      }}
      className="flex flex-col items-start gap-1 min-w-0"
    >
      <div className="flex flex-wrap gap-2">
        {confirmLabel && !armed ? (
          // Distinct keys make React mount a new element instead of
          // turning this button into the submit button below: otherwise the
          // click that arms it would also submit the form.
          <button
            key="arm"
            type="button"
            className={className}
            disabled={disabled || pending}
            title={title}
            onClick={() => setArmed(true)}
          >
            {pending ? (pendingLabel ?? "…") : label}
          </button>
        ) : (
          <button key="submit" type="submit" className={className} disabled={disabled || pending} title={title}>
            {pending ? (pendingLabel ?? "…") : armed ? confirmLabel : label}
          </button>
        )}
        {armed && (
          <button type="button" className="btn btn-ghost" onClick={() => setArmed(false)}>
            Cancel
          </button>
        )}
      </div>
      <FormMessage text={state.error} style={{ fontSize: 12 }} />
      <FormMessage kind="success" text={state.message} style={{ fontSize: 12 }} />
    </form>
  );
}

// A small select plus a Save button, for changing one value (a role).
// The select is controlled so it keeps the chosen value after an error;
// give the component a `key` that includes the current value so it starts
// over when the server sends a new one.
export function SelectActionForm({
  action,
  name,
  label,
  options,
  current,
  disabled = false,
  title,
}: {
  action: BoundAction;
  name: string;
  label: string;
  options: readonly string[];
  current: string;
  disabled?: boolean;
  title?: string;
}) {
  const [state, formAction, pending] = useActionState(action, initialState);
  const [value, setValue] = useState(current);

  return (
    <form action={formAction} className="flex flex-col items-start gap-1 min-w-0">
      <div className="flex flex-wrap items-center gap-2">
        <select
          name={name}
          autoComplete="off"
          aria-label={label}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled || pending}
          title={title}
          className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-[13px] text-[var(--text)]"
        >
          {/* The current value stays selectable even when it is not one of the assignable options (e.g. "owner"). */}
          {!options.includes(current) && <option value={current}>{current}</option>}
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <button type="submit" className="btn btn-ghost" disabled={disabled || pending || value === current}>
          {pending ? "Saving…" : "Save"}
        </button>
      </div>
      <FormMessage text={state.error} style={{ fontSize: 12 }} />
    </form>
  );
}
