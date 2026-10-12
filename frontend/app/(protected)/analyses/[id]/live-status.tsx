"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { wsUrl } from "@/app/lib/ws-url";
import { closeSocket } from "../../close-socket";

type ConnectionStatus = "connecting" | "live" | "polling";

// Events on the analysis:<id> channel that mean "the analysis page has new
// data". The socket only signals; the server page re-fetches the real data.
const REFRESH_EVENTS = new Set([
  "analysis.started",
  "analysis.completed",
  "analysis.failed",
  "recommendation.ready",
]);

const POLL_INTERVAL_MS = 15_000;

export function LiveStatus({ analysisId, accessToken }: { analysisId: string; accessToken: string }) {
  const router = useRouter();
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const ws = new WebSocket(wsUrl(`/analysis:${analysisId}?token=${accessToken}`));

    ws.onopen = () => setConnection("live");
    ws.onmessage = (event) => {
      const { event: name, payload } = JSON.parse(event.data);
      // The first message is a snapshot of the current status: the analysis
      // may have finished between the page loading and the socket opening.
      const finishedBeforeSubscribe =
        name === "analysis.status" && (payload?.status === "completed" || payload?.status === "failed");
      if (REFRESH_EVENTS.has(name) || finishedBeforeSubscribe) {
        router.refresh();
      }
    };
    // If the socket can't be used (expired token, untrusted certificate),
    // fall back to re-fetching the page on a timer so results still appear.
    ws.onclose = () => {
      setConnection("polling");
      if (!pollTimer) pollTimer = setInterval(() => router.refresh(), POLL_INTERVAL_MS);
    };

    // Runs when the page stops rendering this component (e.g. once the
    // analysis has finished) or the user navigates away.
    return () => {
      closeSocket(ws);
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [analysisId, accessToken, router]);

  return (
    <div className="panel flex items-center gap-3" style={{ fontSize: 13 }}>
      <span
        aria-hidden="true"
        style={{
          width: 8,
          height: 8,
          borderRadius: 999,
          background: "var(--accent)",
          display: "inline-block",
          animation: "pulse 1.4s ease-in-out infinite",
        }}
      />
      <span>
        The agents are working on this analysis. This page updates by itself when they finish.
        <span className="mono" style={{ marginLeft: 8, fontSize: 11, color: "var(--text-faint)" }}>
          {connection === "live" ? "live" : connection === "polling" ? `checking every ${POLL_INTERVAL_MS / 1000}s` : "connecting"}
        </span>
      </span>
    </div>
  );
}
