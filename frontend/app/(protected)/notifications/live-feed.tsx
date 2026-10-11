"use client";

import { useEffect, useState } from "react";
import { wsUrl } from "@/app/lib/ws-url";

type WebSocketEnvelope = {
  event: string;
  payload: Record<string, unknown>;
  emitted_at: string;
};

type ConnectionStatus = "connecting" | "open" | "closed";

const STATUS_COLOR: Record<ConnectionStatus, string> = {
  connecting: "var(--accent)",
  open: "var(--up)",
  closed: "var(--down)",
};

export function LiveFeed({ accessToken, userId }: { accessToken: string; userId: string }) {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [messages, setMessages] = useState<WebSocketEnvelope[]>([]);

  useEffect(() => {
    const url = wsUrl(`/user:${userId}?token=${accessToken}`);
    const ws = new WebSocket(url);

    ws.onopen = () => setStatus("open");
    ws.onclose = () => setStatus("closed");
    ws.onmessage = (event) => {
      const envelope: WebSocketEnvelope = JSON.parse(event.data);
      setMessages((prev) => [envelope, ...prev]);
    };

    // Closing on unmount stops the connection when you navigate away from
    // this page — otherwise it would keep running in the background forever.
    return () => ws.close();
  }, [accessToken, userId]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 mono" style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: 999,
            background: STATUS_COLOR[status],
            display: "inline-block",
          }}
        />
        {status}
      </div>
      {messages.length === 0 ? (
        <p className="empty-state">No live events yet.</p>
      ) : (
        <div className="row-list">
          {messages.map((m, i) => (
            <div key={i} className="row-item">
              <span className="row-title">{m.event}</span>
              <pre
                className="mono"
                style={{ margin: 0, fontSize: 12, color: "var(--text-secondary)", whiteSpace: "pre-wrap" }}
              >
                {JSON.stringify(m.payload, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
