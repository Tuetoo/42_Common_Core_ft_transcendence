import { LocalTime } from "@/app/(protected)/local-time";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import {
  changeMemberRole,
  deleteOrganization,
  leaveOrganization,
  removeMember,
  revokeInvitation,
} from "@/app/lib/actions/organizations";
import { Avatar } from "../../avatar";
import { ActionButton, SelectActionForm } from "../../admin/inline-actions";
import { EditOrganizationForm } from "./edit-organization-form";
import { InviteMemberForm } from "./invite-member-form";

type Organization = {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  created_at: string;
};

type Member = {
  id: string;
  user_id: string;
  username: string;
  avatar_url: string | null;
  role: "owner" | "admin" | "member" | "viewer";
  joined_at: string;
};

type Invitation = {
  id: string;
  email: string;
  role: string;
  expires_at: string;
};

// What each organization role may do (OrganizationService in
// services/auth-service/domains/organization/service.py):
// - owner: everything, and the only one who deletes the organization and
//   grants, changes, removes or revokes the admin role;
// - admin: edit details, invite, change roles of members/viewers, remove
//   members/viewers, list and revoke non-admin invitations;
// - member / viewer: read, and leave. The owner cannot leave.
// Every change (edit, delete, invite, roles, removal, revoke) also needs the
// system permission organization:create, so a guest only reads and leaves.
export default async function OrganizationDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { me, accessToken } = await getMe();
  const orgPath = `/organizations/${encodeURIComponent(id)}`;

  const [orgRes, membersRes] = await Promise.all([
    backendFetch(orgPath, accessToken, { cache: "no-store" }),
    backendFetch(`${orgPath}/members`, accessToken, { cache: "no-store" }),
  ]);
  requireOk(orgRes, "Failed to load organization");
  requireOk(membersRes, "Failed to load members");

  const { data: org }: { data: Organization } = await orgRes.json();
  const { data: members }: { data: Member[] } = await membersRes.json();

  const myRole = members.find((m) => m.user_id === me.id)?.role ?? "viewer";
  const isOwner = org.owner_id === me.id;
  const canWrite = can(me, "organization:create");
  const isManager = canWrite && (isOwner || myRole === "admin");
  const grantable = isOwner ? (["admin", "member", "viewer"] as const) : (["member", "viewer"] as const);

  let invitations: Invitation[] = [];
  if (isManager) {
    const invRes = await backendFetch(`${orgPath}/invitations`, accessToken, { cache: "no-store" });
    requireOk(invRes, "Failed to load invitations");
    invitations = (await invRes.json()).data as Invitation[];
  }

  // Owner row is untouchable; you act on yourself with Leave; only the
  // owner manages admins.
  const canManageMember = (m: Member) =>
    isManager && m.user_id !== org.owner_id && m.user_id !== me.id && (isOwner || m.role !== "admin");

  return (
    <div className="page">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <p className="page-eyebrow">Organization</p>
          <h1 className="page-title break-words">{org.name}</h1>
          {org.description && <p className="page-subtitle break-words whitespace-pre-wrap">{org.description}</p>}
          <p className="page-subtitle">
            Your role: <span className="pill pill-accent">{myRole}</span>
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {isOwner ? (
            canWrite && <ActionButton
              action={deleteOrganization.bind(null, org.id)}
              label="Delete organization"
              confirmLabel="Confirm delete"
              pendingLabel="Deleting…"
            />
          ) : (
            <ActionButton
              action={leaveOrganization.bind(null, org.id)}
              label="Leave organization"
              confirmLabel="Confirm leave"
              pendingLabel="Leaving…"
            />
          )}
        </div>
      </div>

      <div className="section">
        <h2 className="section-title">Members ({members.length})</h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Member</th>
                <th>Role</th>
                <th>Joined</th>
                {isManager && <th>Actions</th>}
              </tr>
            </thead>
            <tbody>
              {members.map((m) => {
                const manageable = canManageMember(m);
                return (
                  <tr key={m.id}>
                    <td>
                      <div className="flex items-center gap-2 min-w-0">
                        <Avatar url={m.avatar_url} username={m.username} size={24} />
                        <span className="break-all">
                          {m.username}
                          {m.user_id === me.id && <span className="text-[var(--text-faint)]"> (you)</span>}
                        </span>
                      </div>
                    </td>
                    <td>
                      {manageable ? (
                        <SelectActionForm
                          key={`${m.user_id}-${m.role}`}
                          action={changeMemberRole.bind(null, org.id, m.user_id)}
                          name="role"
                          label={`Role of ${m.username}`}
                          options={grantable}
                          current={m.role}
                        />
                      ) : (
                        <span className={`pill ${m.role === "owner" ? "pill-accent" : ""}`}>{m.role}</span>
                      )}
                    </td>
                    <td className="mono whitespace-nowrap"><LocalTime iso={m.joined_at} dateOnly /></td>
                    {isManager && (
                      <td>
                        {manageable ? (
                          <ActionButton
                            action={removeMember.bind(null, org.id, m.user_id)}
                            label="Remove"
                            confirmLabel="Confirm remove"
                          />
                        ) : (
                          <span className="text-[var(--text-faint)]">—</span>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {isManager && (
        <>
          <div className="section">
            <h2 className="section-title">Pending invitations</h2>
            {invitations.length === 0 ? (
              <p className="empty-state">No open invitations.</p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Role</th>
                      <th>Expires</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invitations.map((inv) => {
                      const revocable = isOwner || inv.role !== "admin";
                      return (
                        <tr key={inv.id}>
                          <td className="break-all">{inv.email}</td>
                          <td>
                            <span className="pill">{inv.role}</span>
                          </td>
                          <td className="mono whitespace-nowrap"><LocalTime iso={inv.expires_at} dateOnly /></td>
                          <td>
                            <ActionButton
                              action={revokeInvitation.bind(null, org.id, inv.id)}
                              label="Revoke"
                              confirmLabel="Confirm revoke"
                              disabled={!revocable}
                              title={revocable ? undefined : "Only the owner can revoke an admin invitation"}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="section">
            <h2 className="section-title">Invite a member</h2>
            <InviteMemberForm orgId={org.id} roles={grantable} />
          </div>

          <div className="section">
            <h2 className="section-title">Edit organization</h2>
            <EditOrganizationForm orgId={org.id} defaults={{ name: org.name, description: org.description ?? "" }} />
          </div>
        </>
      )}
    </div>
  );
}
