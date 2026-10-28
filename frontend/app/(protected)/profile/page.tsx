import { LocalTime } from "@/app/(protected)/local-time";
import { getMe } from "@/app/lib/session";
import { Avatar } from "../avatar";
import { AvatarForm } from "./avatar-form";
import { EditProfileForm } from "./edit-profile-form";
import { TwoFactorSection } from "./two-factor-section";

// What each system role means for this user, in plain words.
const ROLE_SUMMARY: Record<string, string> = {
  guest: "Read-only: you can view shared content but not create or change it. You can still edit your own profile, avatar and two-factor settings.",
  user: "You can create portfolios, notes, comments, analyses, documents and organizations.",
  moderator: "Everything a user can do, plus moderating research content and deactivating accounts.",
  admin: "Full access, including managing users, roles and site statistics.",
};

export default async function ProfilePage() {
  // The layout already loaded the user; getMe() is cached per request.
  const { me } = await getMe();

  return (
    <div className="page">
      <div className="flex flex-wrap items-center gap-4">
        <Avatar url={me.profile?.avatar_url} username={me.username} size={64} />
        <div className="min-w-0">
          <p className="page-eyebrow">Account</p>
          <h1 className="page-title flex flex-wrap items-center gap-2">
            <span className="break-all">{me.username}</span>
            <span className="pill pill-accent">{me.system_role}</span>
          </h1>
        </div>
      </div>

      <div className="panel">
        <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 20px", margin: 0, fontSize: 13 }}>
          <dt style={{ color: "var(--text-faint)" }}>Email</dt>
          <dd style={{ margin: 0 }} className="break-all">{me.email}</dd>
          <dt style={{ color: "var(--text-faint)" }}>Role</dt>
          <dd style={{ margin: 0 }}>
            <span className="pill pill-accent">{me.system_role}</span>
            <span className="block mt-1 text-[var(--text-secondary)]">{ROLE_SUMMARY[me.system_role] ?? ""}</span>
          </dd>
          <dt style={{ color: "var(--text-faint)" }}>Permissions</dt>
          <dd style={{ margin: 0 }} className="flex flex-wrap gap-1">
            {me.permissions.length === 0 ? (
              <span style={{ color: "var(--text-faint)" }}>None</span>
            ) : (
              me.permissions.map((p) => (
                <span key={p} className="pill">
                  {p}
                </span>
              ))
            )}
          </dd>
          <dt style={{ color: "var(--text-faint)" }}>Email verified</dt>
          <dd style={{ margin: 0 }}>
            <span className={`pill ${me.is_verified ? "pill-up" : "pill-down"}`}>
              {me.is_verified ? "Verified" : "Unverified"}
            </span>
          </dd>
          <dt style={{ color: "var(--text-faint)" }}>Member since</dt>
          <dd style={{ margin: 0 }} className="mono">
            <LocalTime iso={me.created_at} dateOnly />
          </dd>
        </dl>
      </div>

      <div className="section">
        <h2 className="section-title">Profile picture</h2>
        <AvatarForm />
      </div>

      <div className="section">
        <h2 className="section-title">Edit profile</h2>
        <EditProfileForm
          defaults={{
            username: me.username,
            first_name: me.profile?.first_name ?? "",
            last_name: me.profile?.last_name ?? "",
            bio: me.profile?.bio ?? "",
            timezone: me.profile?.timezone ?? "",
            country: me.profile?.country ?? "",
          }}
        />
      </div>

      <div className="section">
        <h2 className="section-title">Two-factor authentication</h2>
        <TwoFactorSection />
      </div>
    </div>
  );
}
