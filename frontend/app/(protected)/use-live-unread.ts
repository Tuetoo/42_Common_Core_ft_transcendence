"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { getUnreadCount } from "@/app/lib/actions/notifications";
import { parseEnvelope, publishUserChannelStatus, publishUserEvent, useChannel } from "@/app/lib/use-channel";

// One WebSocket for as long as any signed-in page is open. It does three
// jobs: the backend counts a user as online while they have an open socket
// (friends see the green dot), each new notification bumps the unread
// badge right away, and every message is passed on to other components
// (publishUserEvent), e.g. a friend's presence change to the friends page.
export function useLiveUnread({
  accessToken,
  userId,
  serverUnread,
}: {
  accessToken: string;
  userId: string;
  // The count the layout read; a new object means a new read.
  serverUnread: { count: number };
}): number {
  const router = useRouter();
  // `base` is the last count read from the server; `extra` counts live
  // arrivals since that read.
  const [snapshot, setSnapshot] = useState({ base: serverUnread.count, from: serverUnread });
  const [extra, setExtra] = useState(0);
  const arrivals = useRef(0);

  // A new server count (e.g. after "mark all as read") replaces the
  // live tally, even when the number is the same — adjusting state during
  // render, as React recommends, instead of syncing it in an effect.
  if (snapshot.from !== serverUnread) {
    setSnapshot({ base: serverUnread.count, from: serverUnread });
    setExtra(0);
  }

  const { status } = useChannel({
    path: `/user:${userId}`,
    token: accessToken,
    onMessage: (event) => {
      const envelope = parseEnvelope(event.data);
      if (!envelope) return;
      if (envelope.event === "notification") {
        arrivals.current += 1;
        setExtra((n) => n + 1);
      }
      publishUserEvent(envelope);
    },
    // Notifications sent while the socket was down never reach it, so
    // every open re-reads the count. Arrivals during the read are kept.
    onOpen: () => {
      const start = arrivals.current;
      getUnreadCount().then(
        (count) => {
          if (count === null) return;
          setSnapshot((prev) => ({ ...prev, base: count }));
          setExtra(arrivals.current - start);
        },
        () => {}
      );
    },
    // The server closes the socket with 4401 when the access token
    // expires. The browser can't refresh the token itself (httpOnly
    // cookie), so ask the server to re-render: the proxy swaps in a fresh
    // token, the layout passes it down, and the socket reconnects with it.
    // A drop for any other reason (network blip, core-service restart)
    // only reconnects: re-rendering then could turn the page into the
    // error page while the backend is down.
    onUnauthorized: () => router.refresh(),
  });

  useEffect(() => publishUserChannelStatus(status), [status]);
  // Back to the initial value on sign-out, so a later sign-in does not
  // show the old "Live" for one render.
  useEffect(() => () => publishUserChannelStatus("connecting"), []);

  return snapshot.base + extra;
}
