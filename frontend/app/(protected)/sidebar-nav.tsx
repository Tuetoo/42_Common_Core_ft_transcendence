"use client";

import { useLiveUnread } from "./use-live-unread";
import { Brand, NavLinks, navGroups } from "./nav-links";
import { MobileNav } from "./mobile-nav";

// Renders both navigations: the top bar with a menu below `md`, the
// sidebar from `md` up. They live in one component so the page keeps a
// single live-unread WebSocket for both.
export function SidebarNav({
  accessToken,
  userId,
  unread: serverUnread,
  permissions,
  account,
}: {
  accessToken: string;
  userId: string;
  unread: { count: number };
  permissions: string[];
  account: React.ReactNode;
}) {
  const unread = useLiveUnread({ accessToken, userId, serverUnread });
  const groups = navGroups(permissions);

  return (
    <>
      <MobileNav groups={groups} unread={unread} account={account} />
      <aside
        className="hidden md:flex w-56 shrink-0 sticky top-0 h-screen overflow-y-auto flex-col justify-between gap-6 p-4 print:hidden"
        style={{ background: "var(--bg-raised)", borderRight: "1px solid var(--border)" }}
      >
        <div className="flex flex-col gap-8">
          <div className="pt-2">
            <Brand />
          </div>
          <NavLinks groups={groups} unread={unread} />
        </div>
        {account}
      </aside>
    </>
  );
}
