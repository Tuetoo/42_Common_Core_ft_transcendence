import Link from "next/link";
import { VerifyEmailForm } from "../link-forms";

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
      </div>
      {token ? (
        <VerifyEmailForm token={token} />
      ) : (
        <>
          <p className="form-message error">This link is missing its verification token.</p>
          <Link href="/login" className="link-btn">
            Back to login
          </Link>
        </>
      )}
    </div>
  );
}
