"use client";

import { useEffect, useEffectEvent, useRef } from "react";
import { LocalTime } from "@/app/(protected)/local-time";
import { useRouter } from "next/navigation";
import { parseEnvelope, useChannel } from "@/app/lib/use-channel";

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
  const lastRefresh = useRef(0);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function refreshNow() {
    refreshTimer.current = null;
    lastRefresh.current = Date.now();
    router.refresh();
  }

  // Debounced: the first event refreshes as soon as the gap allows, the
  // ones after it within the gap ride on that same refresh.
  function scheduleRefresh() {
    if (refreshTimer.current) return;
    const wait = Math.max(0, lastRefresh.current + MIN_REFRESH_GAP_MS - Date.now());
    refreshTimer.current = setTimeout(refreshNow, wait);
  }
  const onTick = useEffectEvent(scheduleRefresh);

  // The server closes the socket with 4401 when the access token expires,
  // so that retry first re-renders the page (which brings a fresh token;
  // the socket then reconnects with it). Other drops only reconnect; the
  // poll below catches up. Waits grow 2s, 4s, 8s ... 60s.
  const { status: channel } = useChannel({
    path: `/user:${userId}`,
    token: accessToken,
    baseDelayMs: 2000,
    onMessage: (event) => {
      const envelope = parseEnvelope(event.data);
      if (envelope?.event === "notification" && RELEVANT_TYPES.has(String(envelope.payload.type))) {
        scheduleRefresh();
      }
    },
    onUnauthorized: () => {
      lastRefresh.current = Date.now();
      router.refresh();
    },
  });
  const status: ConnectionStatus =
    channel === "open" ? "live" : channel === "connecting" ? "connecting" : "reconnecting";

  useEffect(() => {
    lastRefresh.current = Date.now();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") onTick();
    }, POLL_INTERVAL_MS);
    // Coming back to a tab that sat in the background catches up at once.
    const onVisible = () => {
      if (document.visibilityState === "visible" && Date.now() - lastRefresh.current > POLL_INTERVAL_MS) {
        onTick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisible);
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    };
  }, []);

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
