"use client";

import { useEffect, useState } from "react";
import { LocalTime } from "@/app/(protected)/local-time";
import { useRouter } from "next/navigation";
import { wsUrl } from "@/app/lib/ws-url";
import { CLOSE_UNAUTHORIZED, closeSocket } from "../close-socket";

type ConnectionStatus = "connecting" | "live" | "reconnecting";

// Notification types (shared/enums.py NotificationType) that change the
// numbers on this page. The backend sends every notification to the
// user:<id> channel as { event: "notification", payload: NotificationRead }.
const RELEVANT_TYPES = new Set([
  "analysis_started",
  "analysis_completed",
  "analysis_failed",
  "recommendation_ready",
]);

// At most one re-render per MIN_REFRESH_GAP_MS, however many events arrive.
const MIN_REFRESH_GAP_MS = 5_000;
// Fallback when no event comes (live delivery can be turned off in the
// notification settings, and accuracy is scored by a daily job).
const POLL_INTERVAL_MS = 60_000;

// Keeps the analytics page current. The socket only signals that something
// changed; router.refresh() re-runs the server page, which fetches the real
// numbers again with the filters in the URL.
export function LiveRefresh({
  accessToken,
  userId,
  renderedAt,
}: {
  accessToken: string;
  userId: string;
  renderedAt: string;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let lastRefresh = Date.now();

    function refresh() {
      refreshTimer = null;
      lastRefresh = Date.now();
      router.refresh();
    }

    // Debounced: the first event refreshes as soon as the gap allows, the
    // ones after it within the gap ride on that same refresh.
    function scheduleRefresh() {
      if (refreshTimer) return;
      const wait = Math.max(0, lastRefresh + MIN_REFRESH_GAP_MS - Date.now());
      refreshTimer = setTimeout(refresh, wait);
    }

    function connect() {
      const socket = new WebSocket(wsUrl(`/user:${userId}?token=${accessToken}`));
      ws = socket;
      socket.onopen = () => {
        attempts = 0;
        setStatus("live");
      };
      socket.onmessage = (event) => {
        try {
          const envelope = JSON.parse(event.data);
          if (envelope?.event === "notification" && RELEVANT_TYPES.has(envelope.payload?.type)) {
            scheduleRefresh();
          }
        } catch {
          // Not JSON: nothing this page understands.
        }
      };
      // The server closes the socket with 4401 when the access token
      // expires, so that retry first re-renders the page (which brings a
      // fresh token; the effect then reconnects with it). Other drops only
      // reconnect; the poll below catches up. Waits grow 2s, 4s, 8s ... 60s.
      socket.onclose = (event) => {
        setStatus("reconnecting");
        const delay = Math.min(2000 * 2 ** attempts, 60_000);
        attempts += 1;
        retryTimer = setTimeout(() => {
          retryTimer = null;
          if (event.code === CLOSE_UNAUTHORIZED) {
            lastRefresh = Date.now();
            router.refresh();
          }
          connect();
        }, delay);
      };
    }

    connect();

    const poll = setInterval(() => {
      if (document.visibilityState === "visible") scheduleRefresh();
    }, POLL_INTERVAL_MS);
    // Coming back to a tab that sat in the background catches up at once.
    const onVisible = () => {
      if (document.visibilityState === "visible" && Date.now() - lastRefresh > POLL_INTERVAL_MS) {
        scheduleRefresh();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisible);
      if (retryTimer) clearTimeout(retryTimer);
      if (refreshTimer) clearTimeout(refreshTimer);
      if (ws) closeSocket(ws);
    };
  }, [accessToken, userId, router]);

  const label = status === "live" ? "Live" : status === "reconnecting" ? "Reconnecting…" : "Connecting…";
  const color = status === "live" ? "var(--up)" : "var(--accent)";

  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1 mono"
      style={{ fontSize: 12, color: "var(--text-secondary)" }}
    >
      <span role="status" aria-live="polite" className="flex items-center gap-2">
        <span
          aria-hidden="true"
          style={{ width: 7, height: 7, borderRadius: 999, background: color, display: "inline-block" }}
        />
        {label}
      </span>
      <span>
        Last updated{" "}
        <LocalTime iso={renderedAt} />
      </span>
      <button type="button" className="link-btn" style={{ padding: 0 }} onClick={() => router.refresh()}>
        Refresh now
      </button>
    </div>
  );
}
