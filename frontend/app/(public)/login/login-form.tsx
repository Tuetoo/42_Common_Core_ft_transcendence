"use client";

import { useActionState, useState } from "react";
import Link from "next/link";
import { login, type LoginState } from "@/app/lib/actions/auth";
import { EMAIL_MAX, PASSWORD_MAX_BYTES, TOTP_CODE_PATTERN, TOTP_CODE_TITLE } from "../field-rules";
import { FormMessage } from "@/app/form-message";

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
          autoComplete="email"
          required
          maxLength={EMAIL_MAX}
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
          autoComplete="current-password"
          required
          maxLength={PASSWORD_MAX_BYTES}
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
            autoComplete="one-time-code"
            maxLength={6}
            pattern={TOTP_CODE_PATTERN}
            title={TOTP_CODE_TITLE}
            autoFocus
            required
          />
        </div>
      )}
      <FormMessage text={state?.error} />
      {/* The backend refuses an unverified email with this code; offer a
          way to get a fresh verification link. */}
      {state?.code === "email_not_verified" && (
        <Link href="/verify-email" className="link-btn">
          Resend the verification link
        </Link>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Logging in…" : "Log in"}
      </button>
      <div className="flex flex-wrap justify-between gap-x-4 gap-y-2">
        <Link href={next ? `/register?next=${encodeURIComponent(next)}` : "/register"} className="link-btn">
          Need an account? Register
        </Link>
        <Link href="/forgot-password" className="link-btn">
          Forgot password?
        </Link>
      </div>
    </form>
  );
}
