import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { markAllNotificationsRead } from "@/app/lib/actions/notifications";
import { LiveFeed } from "./live-feed";

type Me = { id: string };

type Notification = {
  id: string;
  title: string;
  content: string | null;
  read_at: string | null;
  created_at: string;
};

export default async function NotificationsPage() {
  const accessToken = await getAccessToken();

  const [meRes, notificationsRes] = await Promise.all([
    backendFetch("/auth/me", accessToken!, { cache: "no-store" }),
    backendFetch("/notifications", accessToken!, { cache: "no-store" }),
  ]);

  requireOk(meRes, "Failed to load current user");
  requireOk(notificationsRes, "Failed to load notifications");

  const { data: me }: { data: Me } = await meRes.json();
  const { items: notifications }: { items: Notification[] } = await notificationsRes.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Notifications</h1>
      </div>

      <div className="section">
        <div className="flex items-center justify-between">
          <h2 className="section-title">History</h2>
          {notifications.some((n) => !n.read_at) && (
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
              <div key={n.id} className="row-item">
                <span className="row-title flex items-center gap-2">
                  {!n.read_at && (
                    <span
                      aria-label="Unread"
                      style={{ width: 6, height: 6, borderRadius: 999, background: "var(--accent)", flexShrink: 0 }}
                    />
                  )}
                  {n.title}
                </span>
                {n.content && <span className="row-meta">{n.content}</span>}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Live feed</h2>
        <LiveFeed accessToken={accessToken!} userId={me.id} />
      </div>
    </div>
  );
}
