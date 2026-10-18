"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { parseEnvelope, useChannel } from "@/app/lib/use-channel";

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

export function LiveStatus({
  analysisId,
  accessToken,
  shownStatus,
}: {
  analysisId: string;
  accessToken: string;
  shownStatus: string;
}) {
  const router = useRouter();

  // One connection only: if the socket can't be used (expired token,
  // untrusted certificate) or drops, fall back to re-fetching the page on
  // a timer so results still appear. It closes when the page stops
  // rendering this component (e.g. once the analysis has finished) or the
  // user navigates away.
  const { status } = useChannel({
    path: `/analysis:${analysisId}`,
    token: accessToken,
    retry: false,
    onMessage: (event) => {
      const envelope = parseEnvelope(event.data);
      if (!envelope) return;
      // The first message is a snapshot of the current status: the analysis
      // may have started or finished between the page loading and the
      // socket opening, so its events were sent before this subscription.
      const changedBeforeSubscribe =
        envelope.event === "analysis.status" && envelope.payload.status !== shownStatus;
      if (REFRESH_EVENTS.has(envelope.event) || changedBeforeSubscribe) {
        router.refresh();
      }
    },
  });
  const connection: ConnectionStatus = status === "open" ? "live" : status === "ended" ? "polling" : "connecting";

  useEffect(() => {
    if (connection !== "polling") return;
    const pollTimer = setInterval(() => router.refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(pollTimer);
  }, [connection, router]);

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
