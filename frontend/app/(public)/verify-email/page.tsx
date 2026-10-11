import Link from "next/link";
import { ResendVerificationForm, VerifyEmailForm } from "../link-forms";

export default async function VerifyEmailPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Verify your email</h1>
        {!token && (
          <p className="page-subtitle">
            Open the link in the email we sent you. Didn&apos;t get it, or has it expired? Enter your email to get
            a new one.
          </p>
        )}
      </div>
      {token ? (
        <>
          <VerifyEmailForm token={token} />
          <div className="divider">link expired?</div>
          <ResendVerificationForm />
        </>
      ) : (
        <ResendVerificationForm autoFocus />
      )}
      <Link href="/login" className="link-btn">
        Back to login
      </Link>
    </div>
  );
}
