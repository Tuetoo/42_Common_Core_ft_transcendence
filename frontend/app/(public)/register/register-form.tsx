"use client";

import { useActionState } from "react";
import Link from "next/link";
import { register, type RegisterState } from "@/app/lib/actions/auth";

const initialState: RegisterState = {};

export function RegisterForm() {
  const [state, formAction, pending] = useActionState(register, initialState);

  if (state?.success) {
    return (
      <div className="flex flex-col gap-4">
        <p className="form-message success">
          Registered! Check your email for a verification link, then come back and log in.
        </p>
        <Link href="/login" className="link-btn">
          Go to login
        </Link>
      </div>
    );
  }

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="email">Email</label>
        <input id="email" name="email" type="email" required autoFocus />
      </div>
      <div className="field">
        <label htmlFor="username">Username</label>
        <input id="username" name="username" type="text" required minLength={3} maxLength={32} />
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input id="password" name="password" type="password" required minLength={10} />
      </div>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Registering…" : "Register"}
      </button>
      <Link href="/login" className="link-btn">
        Already have an account? Log in
      </Link>
    </form>
  );
}
