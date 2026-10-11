"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { wsUrl } from "@/app/lib/ws-url";

// One WebSocket for as long as any signed-in page is open. It does two
// jobs: the backend counts a user as online while they have an open socket
// (friends see the green dot), and each new notification bumps the unread
// badge right away.
export function useLiveUnread({
  accessToken,
  userId,
  initialUnread,
}: {
  accessToken: string;
  userId: string;
  initialUnread: number;
}): number {
  const router = useRouter();
  // `base` follows the server's count; `extra` counts live arrivals since.
  const [base, setBase] = useState(initialUnread);
  const [extra, setExtra] = useState(0);

  // A new server count (e.g. after "mark all as read") replaces the
  // live tally — adjusting state during render, as React recommends,
  // instead of syncing it in an effect.
  if (base !== initialUnread) {
    setBase(initialUnread);
    setExtra(0);
  }

  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let ws: WebSocket | null = null;

    function connect() {
      ws = new WebSocket(wsUrl(`/user:${userId}?token=${accessToken}`));
      ws.onopen = () => {
        attempts = 0;
      };
      ws.onmessage = () => setExtra((n) => n + 1);
      // The server closes the socket when the access token expires. The
      // browser can't refresh the token itself (httpOnly cookie), so ask the
      // server to re-render: the proxy swaps in a fresh token, the layout
      // passes it down, and this effect reconnects with it. A drop for any
      // other reason (network blip) just reconnects. Waits grow (5s, 10s,
      // 20s ... 60s) so a socket that can't connect doesn't spin.
      ws.onclose = () => {
        const delay = Math.min(5000 * 2 ** attempts, 60_000);
        attempts += 1;
        retryTimer = setTimeout(() => {
          router.refresh();
          connect();
        }, delay);
      };
    }

    connect();
    return () => {
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [accessToken, userId, router]);

  return base + extra;
}
