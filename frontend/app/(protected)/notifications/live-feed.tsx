"use client";

import { useEffect, useRef, useState } from "react";
import { wsUrl } from "@/app/lib/ws-url";
import { closeSocket } from "../close-socket";

type WebSocketEnvelope = {
  event: string;
  payload: Record<string, unknown>;
  emitted_at: string;
};

// One line in the feed. `key` is a client-side counter: new events are
// added at the top, so an array index would not identify a row.
type FeedItem = {
  key: number;
  title: string;
  detail: string | null;
  at: string;
};

type ConnectionStatus = "connecting" | "open" | "reconnecting";

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "var(--accent)",
  open: "var(--up)",
  reconnecting: "var(--down)",
};

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Disconnected, reconnecting…",
};

// Keep the page light even if it stays open for hours.
const MAX_ITEMS = 50;

// "analysis.completed" -> "Analysis completed"
function humanize(event: string): string {
  const words = event.replace(/[._]/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function toFeedItem(envelope: WebSocketEnvelope, key: number): FeedItem {
  const { title, content } = envelope.payload ?? {};
  // Notifications carry their own title and text; other events only
  // have a name, which is turned into words.
  const isNotification = envelope.event === "notification" && typeof title === "string";
  const when = new Date(envelope.emitted_at);
  return {
    key,
    title: isNotification ? title : humanize(envelope.event),
    detail: isNotification && typeof content === "string" ? content : null,
    at: Number.isNaN(when.getTime()) ? "" : when.toLocaleTimeString(),
  };
}

export function LiveFeed({ accessToken, userId }: { accessToken: string; userId: string }) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [items, setItems] = useState<FeedItem[]>([]);
  const nextKey = useRef(0);

  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let ws: WebSocket | null = null;

    function connect() {
      ws = new WebSocket(wsUrl(`/user:${userId}?token=${accessToken}`));
      ws.onopen = () => {
        attempts = 0;
        setStatus("open");
      };
      ws.onmessage = (event) => {
        let envelope: WebSocketEnvelope;
        try {
          envelope = JSON.parse(event.data);
        } catch {
          return; // not an event this page understands
        }
        const item = toFeedItem(envelope, nextKey.current++);
        setItems((prev) => [item, ...prev].slice(0, MAX_ITEMS));
      };
      // Same backoff as the unread badge (5s, 10s, 20s ... 60s). An expired
      // token is renewed by the badge's page refresh, which re-renders this
      // component with a fresh token and restarts the effect.
      ws.onclose = () => {
        setStatus("reconnecting");
        const delay = Math.min(5000 * 2 ** attempts, 60_000);
        attempts += 1;
        retryTimer = setTimeout(connect, delay);
      };
    }

    connect();
    // Stop the connection when leaving the page; otherwise it would keep
    // running in the background.
    return () => {
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) closeSocket(ws);
    };
  }, [accessToken, userId]);

  return (
    <div className="flex flex-col gap-3">
      <div
        role="status"
        className="flex items-center gap-2 mono"
        style={{ fontSize: 12, color: "var(--text-secondary)" }}
      >
        <span
          aria-hidden="true"
          style={{
            width: 7,
            height: 7,
            borderRadius: 999,
            background: STATUS_COLOR[status],
            display: "inline-block",
          }}
        />
        {STATUS_LABEL[status]}
      </div>
      {items.length === 0 ? (
        <p className="empty-state">No live events yet. New notifications appear here as they arrive.</p>
      ) : (
        <ul className="row-list" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {items.map((item) => (
            <li key={item.key} className="row-item">
              <span className="row-title">{item.title}</span>
              {item.detail && <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{item.detail}</span>}
              {item.at && <span className="row-meta">{item.at}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
