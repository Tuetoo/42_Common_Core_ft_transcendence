import Link from "next/link";
import { redirect } from "next/navigation";
import { isLoggedIn } from "@/app/lib/session";
import { AcceptInvitationForm } from "../../link-forms";
import { firstParam, type SearchParam } from "@/app/lib/format";

export default async function AcceptInvitationPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: SearchParam }>;
}) {
  const token = firstParam((await searchParams).token);

  // Accepting needs a logged-in user whose email matches the invitation, so
  // send them through login and bring them straight back here afterwards.
  if (token && !(await isLoggedIn())) {
    redirect(`/login?next=${encodeURIComponent(`/invitations/accept?token=${token}`)}`);
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Organization</p>
        <h1 className="page-title">Join an organization</h1>
        <p className="page-subtitle">You&apos;ve been invited to an organization on AlphaMind.</p>
      </div>
      {token ? (
        <AcceptInvitationForm token={token} />
      ) : (
        <>
          <p className="form-message error">This link is missing its invitation token.</p>
          <Link href="/organizations" className="link-btn">
            Go to organizations
          </Link>
        </>
      )}
    </div>
  );
}
