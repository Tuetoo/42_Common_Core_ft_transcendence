import Link from "next/link";
import { ForgotPasswordForm } from "../link-forms";

export default function ForgotPasswordPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Reset your password</h1>
        <p className="page-subtitle">We&apos;ll email you a link to choose a new password.</p>
      </div>
      <ForgotPasswordForm />
      <Link href="/login" className="link-btn">
        Back to login
      </Link>
    </div>
  );
}
