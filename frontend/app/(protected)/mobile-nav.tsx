"use client";

import { useId, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Brand, NavLinks, UnreadBadge, type NavGroup } from "./nav-links";

// The top bar shown below the `md` breakpoint. The menu button opens the
// same links as the desktop sidebar in a panel under the bar.
export function MobileNav({
  groups,
  unread,
  account,
}: {
  groups: NavGroup[];
  unread: number;
  account: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Close the menu after any navigation, including the back button.
  // Adjusting state during render, as React recommends, not in an effect.
  const pathname = usePathname();
  const [lastPath, setLastPath] = useState(pathname);
  if (pathname !== lastPath) {
    setLastPath(pathname);
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape" && open) {
      setOpen(false);
      buttonRef.current?.focus();
    }
  }

  return (
    <header
      className="md:hidden sticky top-0 z-20 print:hidden"
      style={{ background: "var(--bg-raised)", borderBottom: "1px solid var(--border)" }}
      onKeyDown={onKeyDown}
    >
      <div className="flex items-center justify-between gap-3 h-14 pr-4 pl-1">
        <Brand />
        <div className="flex items-center gap-3">
          {unread > 0 && (
            <Link href="/notifications" aria-label={`Notifications, ${unread} unread`} className="flex">
              <UnreadBadge count={unread} className="nav-badge nav-badge-standalone" />
            </Link>
          )}
          <button
            ref={buttonRef}
            type="button"
            className="btn btn-ghost"
            style={{ padding: "6px 12px" }}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "Close" : "Menu"}
          </button>
        </div>
      </div>
      <div
        id={panelId}
        hidden={!open}
        className="flex flex-col gap-6 px-2 pb-4 overflow-y-auto"
        style={{ maxHeight: "calc(100dvh - 3.5rem)" }}
      >
        <NavLinks groups={groups} unread={unread} onNavigate={() => setOpen(false)} />
        {account}
      </div>
    </header>
  );
}
