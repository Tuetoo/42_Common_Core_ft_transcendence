import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { CreateOrganizationForm } from "./create-organization-form";

type Organization = {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  created_at: string;
};

export default async function OrganizationsPage() {
  const { me, accessToken } = await getMe();
  const res = await backendFetch("/organizations", accessToken, { cache: "no-store" });

  requireOk(res, "Failed to load organizations");

  const { data: organizations }: { data: Organization[] } = await res.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Workspace</p>
        <h1 className="page-title">Organizations</h1>
      </div>

      <div className="section">
        {organizations.length === 0 ? (
          <p className="empty-state">You&apos;re not part of any organization yet.</p>
        ) : (
          <div className="row-list">
            {organizations.map((org) => (
              <Link key={org.id} href={`/organizations/${org.id}`} className="row-item">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="row-title">{org.name}</span>
                  {org.owner_id === me.id && <span className="pill pill-accent">owner</span>}
                </span>
                {org.description && <span className="row-meta break-words">{org.description}</span>}
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Create a new organization</h2>
        {can(me, "organization:create") ? (
          <CreateOrganizationForm />
        ) : (
          <p className="empty-state">
            Your role (<span className="pill">{me.system_role}</span>) can view organizations you are invited to, but
            cannot create one. Ask an admin to upgrade your account.
          </p>
        )}
      </div>
    </div>
  );
}
