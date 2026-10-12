"use client";

import { useActionState, useState } from "react";
import Link from "next/link";
import { register, type RegisterState } from "@/app/lib/actions/auth";
import {
  EMAIL_MAX,
  PASSWORD_MAX_BYTES,
  PASSWORD_MIN,
  PASSWORD_TITLE,
  USERNAME_PATTERN,
  USERNAME_TITLE,
  checkPasswordBytes,
} from "../field-rules";

const initialState: RegisterState = {};

export function RegisterForm() {
  const [state, formAction, pending] = useActionState(register, initialState);
  // Controlled on purpose, like the login form: React resets uncontrolled
  // fields after a form action returns, which would wipe what the user
  // typed when the server rejects it (e.g. username already taken).
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

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
        <label htmlFor="username">Username</label>
        <input
          id="username"
          name="username"
          type="text"
          autoComplete="username"
          required
          minLength={3}
          maxLength={32}
          pattern={USERNAME_PATTERN}
          title={USERNAME_TITLE}
          aria-describedby="username-hint"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <span id="username-hint" className="field-hint">
          {USERNAME_TITLE}
        </span>
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          minLength={PASSWORD_MIN}
          maxLength={PASSWORD_MAX_BYTES}
          title={PASSWORD_TITLE}
          aria-describedby="password-hint"
          value={password}
          onChange={(e) => {
            checkPasswordBytes(e.target);
            setPassword(e.target.value);
          }}
        />
        <span id="password-hint" className="field-hint">
          At least {PASSWORD_MIN} characters.
        </span>
      </div>
      <p aria-live="polite" className="form-message error">
        {state?.error}
      </p>
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Registering…" : "Register"}
      </button>
      <p className="field-hint" style={{ margin: 0 }}>
        By registering you agree to the <Link href="/terms">Terms of Service</Link> and{" "}
        <Link href="/privacy">Privacy Policy</Link>.
      </p>
      <Link href="/login" className="link-btn">
        Already have an account? Log in
      </Link>
    </form>
  );
}
