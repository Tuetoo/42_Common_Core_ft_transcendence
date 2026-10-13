import { LocalTime } from "@/app/(protected)/local-time";
import Link from "next/link";
import { notFound } from "next/navigation";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { changeUserRole, deleteUser, resetUserTwoFactor, setUserActive } from "@/app/lib/actions/admin";
import { Avatar } from "../../../avatar";
import { ActionButton, SelectActionForm } from "../../inline-actions";
import { EditUserForm } from "./edit-user-form";

type AdminUserDetail = {
  id: string;
  email: string;
  username: string;
  system_role: string;
  is_active: boolean;
  is_verified: boolean;
  is_online: boolean;
  avatar_url: string | null;
  created_at: string;
  updated_at: string;
  last_seen_at: string | null;
  deleted_at: string | null;
  permissions: string[];
  profile: {
    first_name: string | null;
    last_name: string | null;
    bio: string | null;
    timezone: string | null;
    country: string | null;
  } | null;
  two_factor_enabled: boolean;
  oauth_providers: string[];
  organizations_count: number;
};

const ROLES = ["guest", "user", "moderator", "admin"] as const;

export default async function AdminUserDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { me, accessToken } = await getMe();
  if (!can(me, "user:manage")) notFound();

  const { id } = await params;
  const res = await backendFetch(`/admin/users/${encodeURIComponent(id)}`, accessToken, { cache: "no-store" });
  requireOk(res, "Failed to load user");
  const { data: user }: { data: AdminUserDetail } = await res.json();

  const isSelf = user.id === me.id;
  const isDeleted = user.deleted_at !== null;
  const selfNote = isSelf ? "You cannot do this to your own account" : undefined;

  return (
    <div className="page">
      <div>
        <Link href="/admin/users" className="link-btn">
          ← All users
        </Link>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <Avatar url={user.avatar_url} username={user.username} size={56} online={user.is_online} />
        <div className="min-w-0">
          <p className="page-eyebrow">User</p>
          <h1 className="page-title break-all">{user.username}</h1>
        </div>
      </div>

      <div className="panel">
        <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 m-0 text-[13px]">
          <dt className="text-[var(--text-faint)]">Email</dt>
          <dd className="m-0 break-all">{user.email}</dd>
          <dt className="text-[var(--text-faint)]">Role</dt>
          <dd className="m-0">
            <span className="pill pill-accent">{user.system_role}</span>
          </dd>
          <dt className="text-[var(--text-faint)]">Status</dt>
          <dd className="m-0 flex flex-wrap gap-1">
            {isDeleted ? (
              <span className="pill pill-down">deleted</span>
            ) : (
              <span className={`pill ${user.is_active ? "pill-up" : "pill-down"}`}>
                {user.is_active ? "active" : "inactive"}
              </span>
            )}
            <span className={`pill ${user.is_verified ? "pill-up" : ""}`}>
              {user.is_verified ? "verified" : "unverified"}
            </span>
            {user.is_online && <span className="pill pill-accent">online</span>}
          </dd>
          <dt className="text-[var(--text-faint)]">2FA</dt>
          <dd className="m-0">{user.two_factor_enabled ? "Enabled" : "Off"}</dd>
          <dt className="text-[var(--text-faint)]">Sign-in providers</dt>
          <dd className="m-0">{user.oauth_providers.length ? user.oauth_providers.join(", ") : "Password only"}</dd>
          <dt className="text-[var(--text-faint)]">Organizations</dt>
          <dd className="m-0">{user.organizations_count}</dd>
          <dt className="text-[var(--text-faint)]">Joined</dt>
          <dd className="m-0 mono"><LocalTime iso={user.created_at} /></dd>
          <dt className="text-[var(--text-faint)]">Last seen</dt>
          <dd className="m-0 mono">{user.last_seen_at ? <LocalTime iso={user.last_seen_at} /> : "Never"}</dd>
          <dt className="text-[var(--text-faint)]">Permissions</dt>
          <dd className="m-0 flex flex-wrap gap-1">
            {user.permissions.length === 0 ? (
              <span className="text-[var(--text-faint)]">None (read only)</span>
            ) : (
              user.permissions.map((p) => (
                <span key={p} className="pill">
                  {p}
                </span>
              ))
            )}
          </dd>
        </dl>
      </div>

      {isDeleted ? (
        <p className="empty-state">This account was deleted and can no longer be changed.</p>
      ) : (
        <>
          <div className="section">
            <h2 className="section-title">Role and access</h2>
            <div className="flex flex-wrap items-start gap-3">
              <SelectActionForm
                key={`${user.id}-${user.system_role}`}
                action={changeUserRole.bind(null, user.id)}
                name="role"
                label="System role"
                options={ROLES}
                current={user.system_role}
                disabled={isSelf}
                title={isSelf ? "You cannot change your own role" : undefined}
              />
              <ActionButton
                action={setUserActive.bind(null, user.id, !user.is_active)}
                label={user.is_active ? "Deactivate" : "Reactivate"}
                confirmLabel={user.is_active ? "Confirm deactivate" : undefined}
                disabled={isSelf}
                title={selfNote}
              />
              {user.two_factor_enabled && (
                <ActionButton
                  action={resetUserTwoFactor.bind(null, user.id)}
                  label="Reset 2FA"
                  confirmLabel="Confirm reset 2FA"
                  disabled={isSelf}
                  title={isSelf ? "Turn off your own 2FA on your profile page" : undefined}
                />
              )}
              <ActionButton
                action={deleteUser.bind(null, user.id, true)}
                label="Delete user"
                confirmLabel="Confirm delete"
                pendingLabel="Deleting…"
                disabled={isSelf}
                title={selfNote}
              />
            </div>
            <p className="text-[12px] text-[var(--text-faint)] m-0">
              Deactivating signs the user out everywhere. Deleting anonymises the account; organizations it owns pass
              to another member.
            </p>
          </div>

          <div className="section">
            <h2 className="section-title">Edit account</h2>
            <EditUserForm
              userId={user.id}
              original={{ username: user.username, email: user.email, is_verified: user.is_verified }}
              defaults={{
                username: user.username,
                email: user.email,
                is_verified: user.is_verified,
                first_name: user.profile?.first_name ?? "",
                last_name: user.profile?.last_name ?? "",
                bio: user.profile?.bio ?? "",
                timezone: user.profile?.timezone ?? "",
                country: user.profile?.country ?? "",
              }}
            />
          </div>
        </>
      )}
    </div>
  );
}
