"use client";

import { useActionState } from "react";
import Link from "next/link";
import {
  setupTwoFactor,
  verifyTwoFactor,
  disableTwoFactor,
  type SetupTwoFactorState,
  type VerifyTwoFactorState,
  type DisableTwoFactorState,
} from "@/app/lib/actions/twofa";

const setupInitial: SetupTwoFactorState = {};
const verifyInitial: VerifyTwoFactorState = {};
const disableInitial: DisableTwoFactorState = {};

const hint = { fontSize: 12.5, color: "var(--text-secondary)" } as const;

// The backend's /auth/me doesn't report whether 2FA is on, so both actions
// are always offered: turning it on reports "already enabled" (409) if it
// is, and turning it off simply needs a valid code.
export function TwoFactorSection() {
  const [setupState, setupAction, setupPending] = useActionState(setupTwoFactor, setupInitial);
  const [verifyState, verifyAction, verifyPending] = useActionState(verifyTwoFactor, verifyInitial);
  const [disableState, disableAction, disablePending] = useActionState(disableTwoFactor, disableInitial);

  const needsRelogin = setupState.needsRelogin || verifyState.needsRelogin;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h3 style={{ margin: 0, fontSize: 13 }}>Turn on</h3>

        {verifyState.success ? (
          <p aria-live="polite" className="form-message success">
            Two-factor authentication is on. You&apos;ll be asked for a code at every login.
          </p>
        ) : setupState.secret ? (
          <>
            <p style={hint}>
              Add this key manually in your authenticator app (Google Authenticator, Authy, etc. — look
              for &quot;enter a setup key&quot; instead of scanning a QR code):
            </p>
            <div className="panel mono" style={{ fontSize: 13, wordBreak: "break-all" }}>
              {setupState.secret}
            </div>
            <p style={hint}>Then enter the 6-digit code it shows to confirm setup.</p>
            <form action={verifyAction} className="form">
              <div className="field">
                <label htmlFor="verify-code">Authentication code</label>
                <input id="verify-code" name="code" type="text" inputMode="numeric" maxLength={6} required />
              </div>
              {verifyState.error && !verifyState.needsRelogin && (
                <p aria-live="polite" className="form-message error">
                  {verifyState.error}
                </p>
              )}
              <button type="submit" disabled={verifyPending} className="btn btn-primary">
                {verifyPending ? "Confirming…" : "Confirm and enable"}
              </button>
            </form>
          </>
        ) : (
          <form action={setupAction} className="flex flex-col gap-3">
            {setupState.alreadyEnabled && (
              <p aria-live="polite" style={hint}>
                Two-factor authentication is already on for this account. To set it up again, turn it
                off below first.
              </p>
            )}
            {setupState.error && !setupState.alreadyEnabled && !setupState.needsRelogin && (
              <p aria-live="polite" className="form-message error">
                {setupState.error}
              </p>
            )}
            <button type="submit" disabled={setupPending} className="btn btn-primary">
              {setupPending ? "Starting…" : "Enable 2FA"}
            </button>
          </form>
        )}

        {needsRelogin && (
          <p aria-live="polite" className="form-message error">
            For security, turning on 2FA needs a login from the last few minutes.{" "}
            <Link href="/login?next=/profile" className="link-btn">
              Log in again
            </Link>{" "}
            and retry.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-3">
        <h3 style={{ margin: 0, fontSize: 13 }}>Turn off</h3>
        {disableState.success ? (
          <p aria-live="polite" className="form-message success">
            Two-factor authentication is off.
          </p>
        ) : (
          <>
            <p style={hint}>If 2FA is on, enter your current 6-digit code to turn it off.</p>
            <form action={disableAction} className="form">
              <div className="field">
                <label htmlFor="disable-code">Authentication code</label>
                <input id="disable-code" name="code" type="text" inputMode="numeric" maxLength={6} required />
              </div>
              {disableState.error && (
                <p aria-live="polite" className="form-message error">
                  {disableState.error}
                </p>
              )}
              <button type="submit" disabled={disablePending} className="btn btn-ghost">
                {disablePending ? "Disabling…" : "Disable 2FA"}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
