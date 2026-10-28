import Link from "next/link";
import { ResetPasswordForm } from "../link-forms";
import { firstParam, type SearchParam } from "@/app/lib/format";

export default async function ResetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: SearchParam }>;
}) {
  const token = firstParam((await searchParams).token);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Choose a new password</h1>
      </div>
      {token ? (
        <ResetPasswordForm token={token} />
      ) : (
        <>
          <p className="form-message error">This link is missing its reset token.</p>
          <Link href="/forgot-password" className="link-btn">
            Request a new link
          </Link>
        </>
      )}
    </div>
  );
}
