"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLiveUnread } from "./use-live-unread";

const GROUPS = [
  {
    label: "Workspace",
    items: [
      { href: "/portfolios", label: "Portfolios" },
      { href: "/watchlists", label: "Watchlists" },
      { href: "/research", label: "Research" },
      { href: "/organizations", label: "Organizations" },
      { href: "/analyses", label: "Analyses" },
      { href: "/analytics", label: "Analytics" },
    ],
  },
  {
    label: "Account",
    items: [
      { href: "/notifications", label: "Notifications" },
      { href: "/friends", label: "Friends" },
      { href: "/profile", label: "Profile" },
    ],
  },
];

export function SidebarNav({
  accessToken,
  userId,
  initialUnread,
}: {
  accessToken: string;
  userId: string;
  initialUnread: number;
}) {
  const pathname = usePathname();
  const unread = useLiveUnread({ accessToken, userId, initialUnread });

  return (
    <nav className="flex flex-col gap-6">
      {GROUPS.map((group) => (
        <div key={group.label} className="flex flex-col gap-1">
          <div className="px-3 text-[11px] uppercase tracking-wider text-[var(--text-faint)] font-mono mb-1">
            {group.label}
          </div>
          {group.items.map((item) => {
            const active = pathname === item.href || pathname.startsWith(item.href + "/");
            const badge = item.href === "/notifications" && unread > 0 ? unread : null;
            return (
              <Link
                key={item.href}
                href={item.href}
                className="px-3 py-2 rounded-md text-sm transition-colors flex items-center"
                style={{
                  color: active ? "var(--text)" : "var(--text-secondary)",
                  background: active ? "var(--bg-hover)" : "transparent",
                  borderLeft: active ? "2px solid var(--accent)" : "2px solid transparent",
                }}
              >
                {item.label}
                {badge !== null && (
                  <span className="nav-badge" aria-label={`${badge} unread`}>
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
