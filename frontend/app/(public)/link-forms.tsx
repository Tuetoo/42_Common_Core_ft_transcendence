"use client";

import { useActionState } from "react";
import Link from "next/link";
import {
  acceptInvitation,
  confirmPasswordReset,
  requestPasswordReset,
  verifyEmail,
  type LinkActionState,
} from "@/app/lib/actions/email-links";

const initial: LinkActionState = {};

function ErrorLine({ error }: { error?: string }) {
  return error ? (
    <p aria-live="polite" className="form-message error">
      {error}
    </p>
  ) : null;
}

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
      <ErrorLine error={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Verifying…" : "Verify my email"}
      </button>
    </form>
  );
}

export function ForgotPasswordForm() {
  const [state, formAction, pending] = useActionState(requestPasswordReset, initial);

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
        <input id="email" name="email" type="email" required autoFocus />
      </div>
      <ErrorLine error={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Sending…" : "Send reset link"}
      </button>
    </form>
  );
}

export function ResetPasswordForm({ token }: { token: string }) {
  const [state, formAction, pending] = useActionState(confirmPasswordReset.bind(null, token), initial);

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
        <input id="new_password" name="new_password" type="password" minLength={10} required autoFocus />
      </div>
      <div className="field">
        <label htmlFor="confirm_password">Confirm new password</label>
        <input id="confirm_password" name="confirm_password" type="password" minLength={10} required />
      </div>
      <ErrorLine error={state.error} />
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
      <ErrorLine error={state.error} />
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Joining…" : "Accept invitation"}
      </button>
    </form>
  );
}
