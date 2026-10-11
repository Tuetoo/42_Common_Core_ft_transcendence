import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { InviteMemberForm } from "./invite-member-form";

type Organization = {
  id: string;
  name: string;
  description: string | null;
};

type Member = {
  id: string;
  username: string;
  role: string;
  joined_at: string;
};

export default async function OrganizationDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const accessToken = await getAccessToken();

  const [orgRes, membersRes] = await Promise.all([
    backendFetch(`/organizations/${id}`, accessToken!, { cache: "no-store" }),
    backendFetch(`/organizations/${id}/members`, accessToken!, { cache: "no-store" }),
  ]);

  requireOk(orgRes, "Failed to load organization");
  requireOk(membersRes, "Failed to load members");

  const { data: org }: { data: Organization } = await orgRes.json();
  const { data: members }: { data: Member[] } = await membersRes.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Organization</p>
        <h1 className="page-title">{org.name}</h1>
        {org.description && <p className="page-subtitle">{org.description}</p>}
      </div>

      <div className="section">
        <h2 className="section-title">Members</h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id}>
                  <td>{m.username}</td>
                  <td>
                    <span className="pill">{m.role}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="section">
        <h2 className="section-title">Invite a member</h2>
        <InviteMemberForm orgId={org.id} />
      </div>
    </div>
  );
}
