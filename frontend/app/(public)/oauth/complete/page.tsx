"use client";

import { useActionState, useEffect, useRef, useState, startTransition } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  completeOAuthLogin,
  completeOAuthTwoFactor,
  type OAuthTwoFactorState,
} from "@/app/lib/actions/oauth";

type Outcome =
  | { kind: "login"; accessToken: string; refreshToken: string }
  | { kind: "challenge"; challengeToken: string }
  | { kind: "error"; message: string };

const initialTwoFactor: OAuthTwoFactorState = {};

// auth-service's OAuth callback redirects here with the result in the URL
// fragment (#...). A fragment is never sent to any server or written to an
// access log, so it's the one part of a redirect URL safe for tokens — but
// it also means only browser JavaScript can read it. The result is
// therefore rendered in the browser only (no server pre-render, where
// `window` doesn't exist), so it can be read while rendering.
const OAuthResult = dynamic(() => Promise.resolve(OAuthResultInBrowser), {
  ssr: false,
  loading: () => <p style={{ color: "var(--text-secondary)" }}>Completing sign-in…</p>,
});

export default function OAuthCompletePage() {
  return <OAuthResult />;
}

function readFragment(): Outcome {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const accessToken = params.get("access_token");
  const refreshToken = params.get("refresh_token");
  const challengeToken = params.get("challenge_token");
  const error = params.get("error");

  if (accessToken && refreshToken) return { kind: "login", accessToken, refreshToken };
  if (challengeToken) return { kind: "challenge", challengeToken };
  return {
    kind: "error",
    message: params.get("message") ?? (error ? `Sign-in failed (${error}).` : "Sign-in failed."),
  };
}

function OAuthResultInBrowser() {
  // Read once, on the first render; the effect below then clears the URL.
  const [outcome] = useState(readFragment);
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;
    // Drop the tokens from the address bar and browser history right away.
    window.history.replaceState(null, "", window.location.pathname);
    if (outcome.kind === "login") {
      startTransition(() => completeOAuthLogin(outcome.accessToken, outcome.refreshToken));
    }
  }, [outcome]);

  if (outcome.kind === "challenge") {
    return <TwoFactorStep challengeToken={outcome.challengeToken} />;
  }

  if (outcome.kind === "error") {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="page-title">Couldn&apos;t sign you in</h1>
        <p className="form-message error">{outcome.message}</p>
        <Link href="/login" className="link-btn">
          Back to login
        </Link>
      </div>
    );
  }

  return <p style={{ color: "var(--text-secondary)" }}>Completing sign-in…</p>;
}

function TwoFactorStep({ challengeToken }: { challengeToken: string }) {
  const [state, formAction, pending] = useActionState(
    completeOAuthTwoFactor.bind(null, challengeToken),
    initialTwoFactor
  );

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Two-factor authentication</p>
        <h1 className="page-title">Enter your code</h1>
        <p className="page-subtitle">This account has 2FA on. Enter the 6-digit code from your authenticator app.</p>
      </div>
      <form action={formAction} className="form">
        <div className="field">
          <label htmlFor="code">Authentication code</label>
          <input id="code" name="code" type="text" inputMode="numeric" maxLength={6} required autoFocus />
        </div>
        {state.error && (
          <p aria-live="polite" className="form-message error">
            {state.error}
          </p>
        )}
        <button type="submit" disabled={pending} className="btn btn-primary">
          {pending ? "Verifying…" : "Verify"}
        </button>
      </form>
    </div>
  );
}
