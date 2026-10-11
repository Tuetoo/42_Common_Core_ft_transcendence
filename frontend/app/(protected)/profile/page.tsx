import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { Avatar } from "../avatar";
import { AvatarForm } from "./avatar-form";
import { EditProfileForm } from "./edit-profile-form";
import { TwoFactorSection } from "./two-factor-section";

type Me = {
  email: string;
  username: string;
  system_role: string;
  is_verified: boolean;
  created_at: string;
  profile: {
    first_name: string | null;
    last_name: string | null;
    bio: string | null;
    avatar_url: string | null;
  } | null;
};

export default async function ProfilePage() {
  const accessToken = await getAccessToken();

  const res = await backendFetch("/auth/me", accessToken!, { cache: "no-store" });

  requireOk(res, "Failed to load profile");

  const { data: me }: { data: Me } = await res.json();

  return (
    <div className="page">
      <div className="flex items-center gap-4">
        <Avatar url={me.profile?.avatar_url} username={me.username} size={64} />
        <div>
          <p className="page-eyebrow">Account</p>
          <h1 className="page-title">{me.username}</h1>
        </div>
      </div>

      <div className="panel">
        <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 20px", margin: 0, fontSize: 13 }}>
          <dt style={{ color: "var(--text-faint)" }}>Email</dt>
          <dd style={{ margin: 0 }}>{me.email}</dd>
          <dt style={{ color: "var(--text-faint)" }}>Role</dt>
          <dd style={{ margin: 0 }}>{me.system_role}</dd>
          <dt style={{ color: "var(--text-faint)" }}>Email verified</dt>
          <dd style={{ margin: 0 }}>
            <span className={`pill ${me.is_verified ? "pill-up" : "pill-down"}`}>
              {me.is_verified ? "Verified" : "Unverified"}
            </span>
          </dd>
          <dt style={{ color: "var(--text-faint)" }}>Member since</dt>
          <dd style={{ margin: 0 }} className="mono">
            {new Date(me.created_at).toLocaleDateString()}
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
            first_name: me.profile?.first_name ?? null,
            last_name: me.profile?.last_name ?? null,
            bio: me.profile?.bio ?? null,
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
