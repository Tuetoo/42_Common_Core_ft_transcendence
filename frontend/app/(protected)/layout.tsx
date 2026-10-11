import { redirect } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { logout } from "@/app/lib/actions/auth";
import { Avatar } from "./avatar";
import { SidebarNav } from "./sidebar-nav";

type Me = {
  id: string;
  username: string;
  profile: { avatar_url: string | null } | null;
};

export default async function ProtectedLayout({ children }: { children: React.ReactNode }) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    redirect("/login");
  }

  const [meRes, unreadRes] = await Promise.all([
    backendFetch("/auth/me", accessToken, { cache: "no-store" }),
    backendFetch("/notifications?unread_only=true&page_size=1", accessToken, { cache: "no-store" }),
  ]);
  requireOk(meRes, "Failed to load current user");
  const { data: me }: { data: Me } = await meRes.json();
  // The badge is a nicety: if the count can't be loaded, show none.
  const unread: number = unreadRes.ok ? (await unreadRes.json()).total : 0;

  return (
    <div className="min-h-screen flex">
      <aside
        className="w-56 shrink-0 sticky top-0 h-screen overflow-y-auto flex flex-col justify-between p-4 print:hidden"
        style={{ background: "var(--bg-raised)", borderRight: "1px solid var(--border)" }}
      >
        <div className="flex flex-col gap-8">
          <div className="px-3 pt-2 font-mono text-sm tracking-widest uppercase" style={{ color: "var(--accent)" }}>
            AlphaMind
          </div>
          <SidebarNav accessToken={accessToken} userId={me.id} initialUnread={unread} />
        </div>
        <div className="flex items-center gap-2 px-3">
          <Avatar url={me.profile?.avatar_url} username={me.username} size={28} />
          <span className="text-sm truncate" style={{ color: "var(--text-secondary)", flex: 1 }}>
            {me.username}
          </span>
          <form action={logout}>
            <button type="submit" className="link-btn">
              Log out
            </button>
          </form>
        </div>
      </aside>
      <main className="flex-1 p-10 overflow-x-auto">{children}</main>
    </div>
  );
}
