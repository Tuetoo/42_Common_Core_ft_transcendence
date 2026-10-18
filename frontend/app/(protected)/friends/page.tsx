import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { Avatar } from "../avatar";
import { AddFriendForm, AutoRefresh, ConnectionButton, LivePresence } from "./friend-forms";

type Connection = {
  id: string;
  requester_id: string;
  addressee_id: string;
  status: "pending" | "accepted" | "blocked";
  created_at: string;
  other_user: { id: string; username: string; avatar_url: string | null; is_online: boolean };
};

function PersonRow({ connection, children }: { connection: Connection; children?: React.ReactNode }) {
  const user = connection.other_user;
  return (
    <div className="row-item" style={{ flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
      <Avatar
        url={user.avatar_url}
        username={user.username}
        size={36}
        online={connection.status === "accepted" ? user.is_online : undefined}
      />
      <div className="flex flex-col" style={{ flex: 1, minWidth: 0 }}>
        <span className="row-title">{user.username}</span>
        {connection.status === "accepted" && (
          <span className="row-meta">{user.is_online ? "Online" : "Offline"}</span>
        )}
      </div>
      <div className="flex gap-2 items-start">{children}</div>
    </div>
  );
}

export default async function FriendsPage() {
  const { me, accessToken } = await getMe();
  const connectionsRes = await backendFetch("/connections", accessToken, { cache: "no-store" });
  requireOk(connectionsRes, "Failed to load friends");
  // A guest may remove, decline, cancel and block, but not send or accept.
  const canWrite = can(me, "connection:write");

  const { data: connections }: { data: Connection[] } = await connectionsRes.json();

  // Online friends first, then alphabetical.
  const friends = connections
    .filter((c) => c.status === "accepted")
    .sort(
      (a, b) =>
        Number(b.other_user.is_online) - Number(a.other_user.is_online) ||
        a.other_user.username.localeCompare(b.other_user.username)
    );
  const incoming = connections.filter((c) => c.status === "pending" && c.addressee_id === me.id);
  const outgoing = connections.filter((c) => c.status === "pending" && c.requester_id === me.id);
  const blocked = connections.filter((c) => c.status === "blocked");

  return (
    <div className="page">
      <LivePresence />
      <AutoRefresh seconds={60} />
      <div>
        <p className="page-eyebrow">Account</p>
        <h1 className="page-title">Friends</h1>
      </div>

      {canWrite && (
        <div className="section">
          <h2 className="section-title">Add a friend</h2>
          <AddFriendForm />
        </div>
      )}

      {incoming.length > 0 && (
        <div className="section">
          <h2 className="section-title">Requests for you</h2>
          <div className="row-list">
            {incoming.map((c) => (
              <PersonRow key={c.id} connection={c}>
                {canWrite && <ConnectionButton connectionId={c.id} kind="accept" label="Accept" primary />}
                <ConnectionButton connectionId={c.id} kind="remove" label="Decline" />
                <ConnectionButton connectionId={c.id} kind="block" label="Block" />
              </PersonRow>
            ))}
          </div>
        </div>
      )}

      <div className="section">
        <h2 className="section-title">
          Friends{" "}
          <span className="mono" style={{ fontSize: 12, color: "var(--text-faint)" }}>
            {friends.filter((f) => f.other_user.is_online).length} online
          </span>
        </h2>
        {friends.length === 0 ? (
          <p className="empty-state">No friends yet.{canWrite && " Send a request by username above."}</p>
        ) : (
          <div className="row-list">
            {friends.map((c) => (
              <PersonRow key={c.id} connection={c}>
                <ConnectionButton connectionId={c.id} kind="remove" label="Remove" />
                <ConnectionButton connectionId={c.id} kind="block" label="Block" />
              </PersonRow>
            ))}
          </div>
        )}
      </div>

      {outgoing.length > 0 && (
        <div className="section">
          <h2 className="section-title">Waiting for a reply</h2>
          <div className="row-list">
            {outgoing.map((c) => (
              <PersonRow key={c.id} connection={c}>
                <ConnectionButton connectionId={c.id} kind="remove" label="Cancel" />
              </PersonRow>
            ))}
          </div>
        </div>
      )}

      {blocked.length > 0 && (
        <div className="section">
          <h2 className="section-title">Blocked</h2>
          <div className="row-list">
            {blocked.map((c) => (
              <PersonRow key={c.id} connection={c}>
                <ConnectionButton connectionId={c.id} kind="remove" label="Unblock" />
              </PersonRow>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
