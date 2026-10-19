"use client";

import { useCallback, useEffect, useEffectEvent, useRef, useState, useSyncExternalStore } from "react";
import { closeSocket, wsUrl } from "./ws-url";

// Close codes from services/core-service/websocket_manager.py.
export const CLOSE_BAD_REQUEST = 4400; // malformed channel name or message
export const CLOSE_UNAUTHORIZED = 4401; // token expired or invalid: get a new one
export const CLOSE_FORBIDDEN = 4403; // access revoked or reduced (or the resource is gone)
export const CLOSE_TOO_MANY = 4429; // too many open sockets for this account
// Retrying these cannot succeed, so the socket stays closed.
const FATAL_CLOSE_CODES = new Set([1003, 1008, CLOSE_BAD_REQUEST, CLOSE_FORBIDDEN]);
// The server accepts a socket before it checks the token or the socket
// cap, so an open alone proves nothing: the backoff resets only after a
// message arrives or the socket has stayed open this long.
const STABLE_OPEN_MS = 10_000;

export type ChannelStatus = "connecting" | "open" | "reconnecting" | "ended";

export type ChannelState = {
  status: ChannelStatus;
  // Code and reason of the last close, null while the socket is open.
  code: number | null;
  reason: string;
};

// The JSON envelope the backend sends on user:, analysis: and document:
// channels (websocket_manager.py WebSocketEnvelope).
export type ChannelEnvelope = {
  event: string;
  payload: Record<string, unknown>;
  emitted_at?: string;
};

// Sent to user:<id> of each accepted friend when a user comes online or
// goes offline (core-service event_listener.py).
export const PRESENCE_EVENT = "user.presence.changed";

export function parseEnvelope(data: unknown): ChannelEnvelope | null {
  if (typeof data !== "string") return null;
  try {
    const value = JSON.parse(data);
    return value && typeof value.event === "string" ? { ...value, payload: value.payload ?? {} } : null;
  } catch {
    return null;
  }
}

export type ChannelOptions = {
  // Channel path such as "/user:<id>", "/analysis:<id>" or "/yjs/<note id>".
  path: string;
  token: string;
  onMessage: (event: MessageEvent, socket: WebSocket) => void;
  // Runs on every open; `reconnect` is false only for the first one. A
  // page uses it to re-read what it may have missed while disconnected.
  onOpen?: (reconnect: boolean) => void;
  // Runs before reconnecting after a 4401 close. Pages that get their
  // token as a prop re-render here (router.refresh), which brings a fresh
  // one; a new `token` then restarts the connection.
  onUnauthorized?: () => void;
  // Returns a token before every reconnect instead of using the prop.
  // null: the session is over. undefined: try again later.
  refreshToken?: () => Promise<string | null | undefined>;
  baseDelayMs?: number;
  maxDelayMs?: number;
  // false: one connection only; any close ends it.
  retry?: boolean;
  binary?: boolean;
};

// One WebSocket with the same reconnect rules everywhere: waits grow
// (base, 2x base ... max, plus jitter), fatal codes end it, coming back
// online retries at once, and leaving the page closes it.
export function useChannel({
  path,
  token,
  onMessage,
  onOpen,
  onUnauthorized,
  refreshToken,
  baseDelayMs = 5000,
  maxDelayMs = 60_000,
  retry = true,
  binary = false,
}: ChannelOptions): ChannelState & { send: (data: Uint8Array) => void } {
  const [state, setState] = useState<ChannelState>({ status: "connecting", code: null, reason: "" });
  const socketRef = useRef<WebSocket | null>(null);
  const tokenRef = useRef(token);
  const handleMessage = useEffectEvent(onMessage);
  const handleOpen = useEffectEvent((reconnect: boolean) => onOpen?.(reconnect));
  const handleUnauthorized = useEffectEvent(() => onUnauthorized?.());
  const nextToken = useEffectEvent(async () => (refreshToken ? refreshToken() : tokenRef.current));
  const selfRefreshing = refreshToken !== undefined;
  // With refreshToken a new prop token is kept for the next reconnect
  // without tearing down a working socket; otherwise it restarts it.
  const restartKey = selfRefreshing ? "" : token;

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  useEffect(() => {
    let disposed = false;
    let attempts = 0;
    let opened = false;
    let lastWasUnauthorized = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function connect(currentToken: string) {
      if (disposed) return;
      const socket = new WebSocket(wsUrl(`${path}?token=${encodeURIComponent(currentToken)}`));
      if (binary) socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      let openedAt: number | null = null;
      socket.onopen = () => {
        openedAt = Date.now();
        const reconnect = opened;
        opened = true;
        setState({ status: "open", code: null, reason: "" });
        handleOpen(reconnect);
      };
      socket.onmessage = (event) => {
        attempts = 0;
        lastWasUnauthorized = false;
        handleMessage(event, socket);
      };
      socket.onclose = (event) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (disposed) return;
        if (!retry || FATAL_CLOSE_CODES.has(event.code)) {
          setState({ status: "ended", code: event.code, reason: event.reason });
          return;
        }
        if (openedAt !== null && Date.now() - openedAt >= STABLE_OPEN_MS) attempts = 0;
        const unauthorized = event.code === CLOSE_UNAUTHORIZED;
        // A token that a fetch can renew is retried at once, but not twice
        // in a row: a second 4401 means the fresh token is refused too.
        const delay =
          unauthorized && selfRefreshing && !lastWasUnauthorized
            ? 0
            : Math.min(baseDelayMs * 2 ** attempts, maxDelayMs) + Math.random() * 500;
        lastWasUnauthorized = unauthorized;
        attempts += 1;
        setState({ status: "reconnecting", code: event.code, reason: event.reason });
        scheduleReconnect(delay, unauthorized);
      };
    }

    function scheduleReconnect(delay: number, unauthorized: boolean) {
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(async () => {
        retryTimer = null;
        if (unauthorized) handleUnauthorized();
        const fresh = await nextToken();
        if (disposed) return;
        if (fresh === null) {
          setState({ status: "ended", code: CLOSE_UNAUTHORIZED, reason: "session ended" });
          return;
        }
        if (fresh === undefined) {
          attempts += 1;
          scheduleReconnect(Math.min(baseDelayMs * 2 ** attempts, maxDelayMs), false);
          return;
        }
        connect(fresh);
      }, delay);
    }

    // Back online: don't wait for the rest of the backoff.
    function onOnline() {
      if (retryTimer) scheduleReconnect(0, false);
    }
    window.addEventListener("online", onOnline);

    connect(tokenRef.current);
    return () => {
      disposed = true;
      window.removeEventListener("online", onOnline);
      if (retryTimer) clearTimeout(retryTimer);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) closeSocket(socket);
    };
  }, [path, restartKey, selfRefreshing, baseDelayMs, maxDelayMs, retry, binary]);

  const send = useCallback((data: Uint8Array) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(data);
  }, []);

  return { ...state, send };
}

// The badge's user:<id> socket (use-live-unread.ts) is open on every
// signed-in page. It re-broadcasts each message here, so other components
// can follow the same channel without opening a socket of their own.
const USER_EVENT = "alphamind:user-event";

export function publishUserEvent(envelope: ChannelEnvelope): void {
  window.dispatchEvent(new CustomEvent<ChannelEnvelope>(USER_EVENT, { detail: envelope }));
}

export function useUserEvents(handler: (envelope: ChannelEnvelope) => void): void {
  const onEvent = useEffectEvent(handler);
  useEffect(() => {
    const listener = (event: Event) => onEvent((event as CustomEvent<ChannelEnvelope>).detail);
    window.addEventListener(USER_EVENT, listener);
    return () => window.removeEventListener(USER_EVENT, listener);
  }, []);
}

// The badge socket's status, kept here so a page can show a live
// indicator for the user: channel without opening a socket of its own.
const USER_STATUS_EVENT = "alphamind:user-status";
let userChannelStatus: ChannelStatus = "connecting";

export function publishUserChannelStatus(status: ChannelStatus): void {
  userChannelStatus = status;
  window.dispatchEvent(new Event(USER_STATUS_EVENT));
}

function subscribeUserStatus(onChange: () => void): () => void {
  window.addEventListener(USER_STATUS_EVENT, onChange);
  return () => window.removeEventListener(USER_STATUS_EVENT, onChange);
}

export function useUserChannelStatus(): ChannelStatus {
  return useSyncExternalStore(subscribeUserStatus, () => userChannelStatus, () => "connecting");
}
