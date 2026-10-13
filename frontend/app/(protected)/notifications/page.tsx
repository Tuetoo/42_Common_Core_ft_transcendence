import { backendFetch, requireOk } from "@/app/lib/backend";
import { getMe } from "@/app/lib/session";
import { markAllNotificationsRead } from "@/app/lib/actions/notifications";
import { Pager, pageParam } from "../pager";
import { LocalTime } from "@/app/(protected)/local-time";
import { LiveFeed } from "./live-feed";

type Notification = {
  id: string;
  title: string;
  content: string | null;
  read_at: string | null;
  created_at: string;
};

const PAGE_SIZE = 20;

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function NotificationsPage({ searchParams }: { searchParams: SearchParams }) {
  const page = pageParam((await searchParams).page);
  const { me, accessToken } = await getMe();

  const [notificationsRes, unreadRes] = await Promise.all([
    backendFetch(`/notifications?page=${page}&page_size=${PAGE_SIZE}`, accessToken, { cache: "no-store" }),
    // "Mark all as read" covers every page, so ask whether any are unread at all.
    backendFetch("/notifications?unread_only=true&page_size=1", accessToken, { cache: "no-store" }),
  ]);
  requireOk(notificationsRes, "Failed to load notifications");

  const { items: notifications, total }: { items: Notification[]; total: number } =
    await notificationsRes.json();
  const hasUnread = unreadRes.ok
    ? (await unreadRes.json()).total > 0
    : notifications.some((n) => !n.read_at);

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Notifications</h1>
      </div>

      <div className="section">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="section-title">History</h2>
          {hasUnread && (
            <form action={markAllNotificationsRead}>
              <button type="submit" className="link-btn">
                Mark all as read
              </button>
            </form>
          )}
        </div>
        {notifications.length === 0 ? (
          <p className="empty-state">No notifications yet.</p>
        ) : (
          <div className="row-list">
            {notifications.map((n) => (
              <div key={n.id} className="row-item min-w-0">
                <span className="row-title flex items-center gap-2 break-words">
                  {!n.read_at && (
                    <span
                      role="img"
                      aria-label="Unread"
                      style={{ width: 6, height: 6, borderRadius: 999, background: "var(--accent)", flexShrink: 0 }}
                    />
                  )}
                  {n.title}
                </span>
                {n.content && <span className="row-meta break-words">{n.content}</span>}
                <span className="row-meta">
                  <LocalTime iso={n.created_at} />
                </span>
              </div>
            ))}
          </div>
        )}
        <Pager basePath="/notifications" params={{}} page={page} pageSize={PAGE_SIZE} total={total} />
      </div>

      <div className="section">
        <h2 className="section-title">Live feed</h2>
        <LiveFeed accessToken={accessToken} userId={me.id} />
      </div>
    </div>
  );
}
