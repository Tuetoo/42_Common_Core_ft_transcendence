import { redirect } from "next/navigation";
import { getAccessToken, getMe } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { logout } from "@/app/lib/actions/auth";
import { SiteFooter } from "@/app/site-footer";
import { Avatar } from "./avatar";
import { SidebarNav } from "./sidebar-nav";

export default async function ProtectedLayout({ children }: { children: React.ReactNode }) {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    redirect("/login");
  }

  // getMe() is cached per request, so pages that call it again don't
  // trigger a second /auth/me.
  const [{ me }, unreadRes] = await Promise.all([
    getMe(),
    backendFetch("/notifications?unread_only=true&page_size=1", accessToken, { cache: "no-store" }),
  ]);
  // The badge is a nicety: if the count can't be loaded, show none.
  const unread: number = unreadRes.ok ? (await unreadRes.json()).total : 0;

  const account = (
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
  );

  return (
    <div className="min-h-screen flex flex-col md:flex-row">
      <SidebarNav
        accessToken={accessToken}
        userId={me.id}
        initialUnread={unread}
        permissions={me.permissions ?? []}
        account={account}
      />
      {/* min-w-0 lets this column shrink below its content's width, so wide
          content scrolls inside its own box instead of widening the page. */}
      <div className="flex-1 min-w-0 flex flex-col">
        <main className="flex-1 min-w-0 p-4 md:p-10 overflow-x-auto">{children}</main>
        <SiteFooter />
      </div>
    </div>
  );
}
