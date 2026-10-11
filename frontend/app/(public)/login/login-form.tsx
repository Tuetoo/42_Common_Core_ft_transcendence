"use client";

import { useActionState, useState } from "react";
import Link from "next/link";
import { login, type LoginState } from "@/app/lib/actions/auth";

const initialState: LoginState = {};

export function LoginForm({ next }: { next: string }) {
  const [state, formAction, pending] = useActionState(login, initialState);
  // Controlled on purpose: React resets uncontrolled fields after a form
  // action returns, which would wipe the email and password when the
  // backend asks for the 2FA code. State-held values survive that reset.
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  return (
    <form action={formAction} className="form">
      <input type="hidden" name="next" value={next} />
      <div className="field">
        <label htmlFor="email">Email</label>
        <input
          id="email"
          name="email"
          type="email"
          required
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      {state?.needsTwoFactor && (
        <div className="field">
          <label htmlFor="totp_code">Authentication code</label>
          <input
            id="totp_code"
            name="totp_code"
            type="text"
            inputMode="numeric"
            maxLength={6}
            autoFocus
            required
          />
        </div>
      )}
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Logging in…" : "Log in"}
      </button>
      <div className="flex justify-between">
        <Link href="/register" className="link-btn">
          Need an account? Register
        </Link>
        <Link href="/forgot-password" className="link-btn">
          Forgot password?
        </Link>
      </div>
    </form>
  );
}
