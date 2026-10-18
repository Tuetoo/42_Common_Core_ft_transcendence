"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export type NavGroup = {
  label: string;
  items: { href: string; label: string }[];
};

const BASE_GROUPS: NavGroup[] = [
  {
    label: "Workspace",
    items: [
      { href: "/portfolios", label: "Portfolios" },
      { href: "/watchlists", label: "Watchlists" },
      { href: "/research", label: "Research" },
      { href: "/organizations", label: "Organizations" },
      { href: "/analyses", label: "Analyses" },
      { href: "/backtests", label: "Backtests" },
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

// Admin pages only appear for roles that may use them. The backend still
// checks every request; hiding the links just avoids dead ends.
export function navGroups(permissions: string[]): NavGroup[] {
  const admin: NavGroup["items"] = [];
  if (permissions.includes("user:manage") || permissions.includes("user:deactivate")) {
    admin.push({ href: "/admin/users", label: "Admin" });
  }
  if (permissions.includes("content:moderate")) {
    admin.push({ href: "/admin/moderation", label: "Moderation" });
  }
  return admin.length > 0 ? [...BASE_GROUPS, { label: "Admin", items: admin }] : BASE_GROUPS;
}

export function Brand() {
  return (
    <span className="px-3 font-mono text-sm tracking-widest uppercase" style={{ color: "var(--accent)" }}>
      AlphaMind
    </span>
  );
}

// The unread count, with words for screen readers ("3 unread").
export function UnreadBadge({ count, className = "nav-badge" }: { count: number; className?: string }) {
  return (
    <span className={className}>
      {count > 99 ? "99+" : count}
      <span className="sr-only"> unread</span>
    </span>
  );
}

export function NavLinks({
  groups,
  unread,
  onNavigate,
}: {
  groups: NavGroup[];
  unread: number;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="flex flex-col gap-6">
      {groups.map((group) => (
        <div key={group.label} className="flex flex-col gap-1">
          <div className="px-3 text-[11px] uppercase tracking-wider text-[var(--text-faint)] font-mono mb-1">
            {group.label}
          </div>
          {group.items.map((item) => {
            const active = pathname === item.href || pathname.startsWith(item.href + "/");
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                aria-current={active ? "page" : undefined}
                className="px-3 py-2 rounded-md text-sm transition-colors flex items-center"
                style={{
                  color: active ? "var(--text)" : "var(--text-secondary)",
                  background: active ? "var(--bg-hover)" : "transparent",
                  borderLeft: active ? "2px solid var(--accent)" : "2px solid transparent",
                }}
              >
                {item.label}
                {item.href === "/notifications" && unread > 0 && <UnreadBadge count={unread} />}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
