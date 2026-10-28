"use client";

import { useActionState, useState } from "react";
import Link from "next/link";
import {
  acceptInvitation,
  confirmPasswordReset,
  requestPasswordReset,
  resendVerification,
  verifyEmail,
  type LinkActionState,
  type ResendState,
} from "@/app/lib/actions/email-links";
import { EMAIL_MAX, PASSWORD_MAX_BYTES, PASSWORD_MIN, PASSWORD_TITLE, checkPasswordBytes } from "./field-rules";
import { FormMessage } from "@/app/form-message";

const initial: LinkActionState = {};

export function VerifyEmailForm({ token }: { token: string }) {
  const [state, formAction, pending] = useActionState(verifyEmail.bind(null, token), initial);

  if (state.success) {
    return (
      <div className="flex flex-col gap-4">
        <p className="form-message success">Your email is verified. You can log in now.</p>
        <Link href="/login" className="btn btn-primary">
          Go to login
        </Link>
      </div>
    );
  }
  return (
    <form action={formAction} className="form">
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Verifying…" : "Verify my email"}
      </button>
    </form>
  );
}

export function ForgotPasswordForm() {
  const [state, formAction, pending] = useActionState(requestPasswordReset, initial);
  // Controlled so the address survives a failed attempt (React resets
  // uncontrolled fields after a form action).
  const [email, setEmail] = useState("");

  if (state.success) {
    return (
      <p className="form-message success">
        If an account exists for that email, a reset link is on its way. Check your inbox.
      </p>
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
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Sending…" : "Send reset link"}
      </button>
    </form>
  );
}

export function ResetPasswordForm({ token }: { token: string }) {
  const [state, formAction, pending] = useActionState(confirmPasswordReset.bind(null, token), initial);
  // Controlled so a backend refusal (e.g. a too common password) keeps both fields.
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  if (state.success) {
    return (
      <div className="flex flex-col gap-4">
        <p className="form-message success">Your password has been changed. Every other session was signed out.</p>
        <Link href="/login" className="btn btn-primary">
          Log in
        </Link>
      </div>
    );
  }
  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="new_password">New password</label>
        <input
          id="new_password"
          name="new_password"
          type="password"
          autoComplete="new-password"
          required
          minLength={PASSWORD_MIN}
          maxLength={PASSWORD_MAX_BYTES}
          title={PASSWORD_TITLE}
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            checkPasswordBytes(e.target);
          }}
          autoFocus
        />
      </div>
      <div className="field">
        <label htmlFor="confirm_password">Confirm new password</label>
        <input
          id="confirm_password"
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          required
          minLength={PASSWORD_MIN}
          maxLength={PASSWORD_MAX_BYTES}
          title={PASSWORD_TITLE}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Saving…" : "Set new password"}
      </button>
    </form>
  );
}

export function AcceptInvitationForm({ token }: { token: string }) {
  const [state, formAction, pending] = useActionState(acceptInvitation.bind(null, token), initial);

  return (
    <form action={formAction} className="form">
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Joining…" : "Accept invitation"}
      </button>
    </form>
  );
}

const initialResend: ResendState = {};

// Asks for a new verification link. The backend answers the same way
// whether or not the email has an account, so the reply is neutral.
export function ResendVerificationForm({ autoFocus = false }: { autoFocus?: boolean }) {
  const [state, formAction, pending] = useActionState(resendVerification, initialResend);
  // Controlled so the address survives a failed attempt (React resets
  // uncontrolled fields after a form action).
  const [email, setEmail] = useState("");

  if (state.sent) {
    return (
      <p role="status" className="form-message success">
        If an unverified account uses that email, a new verification link is on its way. Check your inbox.
      </p>
    );
  }
  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="resend_email">Email</label>
        <input
          id="resend_email"
          name="email"
          type="email"
          autoComplete="email"
          required
          maxLength={EMAIL_MAX}
          autoFocus={autoFocus}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <FormMessage text={state.error} />
      <button type="submit" disabled={pending} className="btn btn-ghost">
        {pending ? "Sending…" : "Resend verification email"}
      </button>
    </form>
  );
}
