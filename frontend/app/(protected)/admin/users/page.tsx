import { LocalTime } from "@/app/(protected)/local-time";
import Form from "next/form";
import Link from "next/link";
import { notFound } from "next/navigation";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/me";
import { changeUserRole, deleteUser, setUserActive } from "@/app/lib/actions/admin";
import { Pager, pageParam } from "../../pager";
import { ActionButton, SelectActionForm } from "../inline-actions";
import { AccountByIdForm } from "./account-by-id-form";

type AdminUser = {
  id: string;
  email: string;
  username: string;
  system_role: string;
  is_active: boolean;
  is_verified: boolean;
  is_online: boolean;
  created_at: string;
  deleted_at: string | null;
};

type UserPage = { items: AdminUser[]; total: number; page: number; page_size: number };

type Stats = {
  auth: {
    users_total: number;
    users_active: number;
    users_deleted: number;
    users_by_role: Record<string, number>;
  };
  core: Record<string, number> | null;
  ai: Record<string, number> | null;
};

const ROLES = ["guest", "user", "moderator", "admin"] as const;
// Moderators may only deactivate/reactivate these roles (admin service).
const MODERATABLE = new Set(["guest", "user"]);
const PAGE_SIZE = 20;

type SearchParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value) ?? "";
}

export default async function AdminUsersPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const { me, accessToken } = await getMe();
  const canManage = can(me, "user:manage");
  const canDeactivate = can(me, "user:deactivate");
  if (!canManage && !canDeactivate) notFound();

  const sp = await searchParams;
  const q = first(sp.q).slice(0, 254);
  const role = ROLES.includes(first(sp.role) as (typeof ROLES)[number]) ? first(sp.role) : "";
  const status = ["active", "inactive"].includes(first(sp.status)) ? first(sp.status) : "";
  const deleted = first(sp.deleted) === "1" ? "1" : "";
  const page = pageParam(sp.page);

  let users: UserPage | null = null;
  let stats: Stats | null = null;
  if (canManage) {
    const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (q) query.set("q", q);
    if (role) query.set("role", role);
    if (status) query.set("is_active", status === "active" ? "true" : "false");
    if (deleted) query.set("include_deleted", "true");
    const [usersRes, statsRes] = await Promise.all([
      backendFetch(`/admin/users?${query}`, accessToken, { cache: "no-store" }),
      can(me, "site:stats") ? backendFetch("/admin/stats", accessToken, { cache: "no-store" }) : null,
    ]);
    requireOk(usersRes, "Failed to load users");
    users = (await usersRes.json()).data as UserPage;
    if (statsRes?.ok) stats = (await statsRes.json()).data as Stats;
  }

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Administration</p>
        <h1 className="page-title">Users</h1>
        <p className="page-subtitle">
          Signed in as <span className="pill pill-accent">{me.system_role}</span>
          {canManage
            ? " — you can change roles, deactivate, reactivate and delete accounts."
            : " — moderators can deactivate or reactivate users and guests."}
        </p>
      </div>

      {stats && <StatsTiles stats={stats} />}

      {users ? (
        <div className="section">
          <Form action="/admin/users" className="filter-row">
            <div className="field">
              <label htmlFor="q">Username or email</label>
              <input id="q" name="q" type="search" maxLength={254} defaultValue={q} placeholder="Search" />
            </div>
            <div className="field">
              <label htmlFor="role">Role</label>
              <select id="role" name="role" defaultValue={role}>
                <option value="">All</option>
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="status">Status</label>
              <select id="status" name="status" defaultValue={status}>
                <option value="">All</option>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
              </select>
            </div>
            <label className="flex items-center gap-2 text-[13px] text-[var(--text-secondary)] pb-2">
              <input type="checkbox" name="deleted" value="1" defaultChecked={deleted === "1"} />
              Include deleted
            </label>
            <button type="submit" className="btn btn-primary">
              Apply
            </button>
          </Form>

          {users.items.length === 0 ? (
            <p className="empty-state">No users match these filters.</p>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Joined</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.items.map((u) => (
                    <UserRow key={u.id} user={u} meId={me.id} canManage={canManage} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pager
            basePath="/admin/users"
            params={{ q, role, status, deleted }}
            page={users.page}
            pageSize={users.page_size}
            total={users.total}
          />
        </div>
      ) : (
        <div className="section">
          <h2 className="section-title">Deactivate or reactivate an account</h2>
          <p className="page-subtitle">
            Listing all users is for admins only. Paste the user id (shown, for example, in an organization&apos;s
            member list) to act on that account.
          </p>
          <AccountByIdForm />
        </div>
      )}
    </div>
  );
}

function UserRow({ user, meId, canManage }: { user: AdminUser; meId: string; canManage: boolean }) {
  const isSelf = user.id === meId;
  const isDeleted = user.deleted_at !== null;
  const mayToggle = !isSelf && !isDeleted && (canManage || MODERATABLE.has(user.system_role));
  const selfNote = isSelf ? "You cannot do this to your own account" : undefined;

  return (
    <tr>
      <td className="min-w-0">
        <div className="flex flex-col min-w-0">
          {canManage ? (
            <Link href={`/admin/users/${user.id}`} className="link-btn">
              {user.username}
            </Link>
          ) : (
            <span>{user.username}</span>
          )}
          <span className="text-[12px] text-[var(--text-faint)] break-all">{user.email}</span>
        </div>
      </td>
      <td>
        {canManage && !isDeleted ? (
          <SelectActionForm
            key={`${user.id}-${user.system_role}`}
            action={changeUserRole.bind(null, user.id)}
            name="role"
            label={`Role of ${user.username}`}
            options={ROLES}
            current={user.system_role}
            disabled={isSelf}
            title={isSelf ? "You cannot change your own role" : undefined}
          />
        ) : (
          <span className="pill">{user.system_role}</span>
        )}
      </td>
      <td>
        <div className="flex flex-wrap gap-1">
          {isDeleted ? (
            <span className="pill pill-down">deleted</span>
          ) : (
            <span className={`pill ${user.is_active ? "pill-up" : "pill-down"}`}>
              {user.is_active ? "active" : "inactive"}
            </span>
          )}
          {user.is_online && <span className="pill pill-accent">online</span>}
          {!user.is_verified && !isDeleted && <span className="pill">unverified</span>}
        </div>
      </td>
      <td className="mono whitespace-nowrap"><LocalTime iso={user.created_at} dateOnly /></td>
      <td>
        {isDeleted ? (
          <span className="text-[var(--text-faint)]">—</span>
        ) : (
          <div className="flex flex-wrap gap-2">
            <ActionButton
              action={setUserActive.bind(null, user.id, !user.is_active)}
              label={user.is_active ? "Deactivate" : "Reactivate"}
              confirmLabel={user.is_active ? "Confirm deactivate" : undefined}
              disabled={!mayToggle}
              title={selfNote}
            />
            {canManage && (
              <ActionButton
                action={deleteUser.bind(null, user.id, false)}
                label="Delete"
                confirmLabel="Confirm delete"
                pendingLabel="Deleting…"
                disabled={isSelf}
                title={selfNote}
              />
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

function StatsTiles({ stats }: { stats: Stats }) {
  const { auth, core, ai } = stats;
  const byRole = ROLES.map((r) => `${auth.users_by_role[r] ?? 0} ${r}`).join(" · ");
  return (
    <div className="stat-grid">
      <div className="stat-tile">
        <span className="stat-label">Users</span>
        <span className="stat-value">{auth.users_total.toLocaleString("en-US")}</span>
        <span className="stat-note">
          {auth.users_active} active · {auth.users_deleted} deleted
        </span>
      </div>
      <div className="stat-tile">
        <span className="stat-label">By role</span>
        <span className="stat-note">{byRole}</span>
      </div>
      <div className="stat-tile">
        <span className="stat-label">Research</span>
        {core ? (
          <>
            <span className="stat-value">{(core.notes ?? 0).toLocaleString("en-US")}</span>
            <span className="stat-note">
              notes · {core.comments ?? 0} comments · {core.documents ?? 0} documents
            </span>
          </>
        ) : (
          <span className="stat-note">Core service unavailable</span>
        )}
      </div>
      <div className="stat-tile">
        <span className="stat-label">Portfolios</span>
        {core ? (
          <>
            <span className="stat-value">{(core.portfolios ?? 0).toLocaleString("en-US")}</span>
            <span className="stat-note">
              {core.transactions ?? 0} transactions · {core.watchlists ?? 0} watchlists
            </span>
          </>
        ) : (
          <span className="stat-note">Core service unavailable</span>
        )}
      </div>
      <div className="stat-tile">
        <span className="stat-label">Analyses</span>
        {ai ? (
          <>
            <span className="stat-value">{(ai.analyses_total ?? 0).toLocaleString("en-US")}</span>
            <span className="stat-note">
              {ai.analyses_completed ?? 0} completed · {ai.analyses_failed ?? 0} failed · {ai.recommendations ?? 0}{" "}
              recommendations
            </span>
          </>
        ) : (
          <span className="stat-note">AI service unavailable</span>
        )}
      </div>
    </div>
  );
}
