// WebSocket URL for a channel path such as "/user:<id>" or "/yjs/<note>".
// In development the frontend runs on its own port, so NEXT_PUBLIC_WS_URL
// points at the gateway. When the frontend is served through the gateway
// itself (Docker), leave it empty and the page's own origin is used:
// https://host:8443 -> wss://host:8443/ws. Call it in the browser only
// (inside effects), where `window` exists.
export function wsUrl(path: string): string {
  const base =
    process.env.NEXT_PUBLIC_WS_URL ||
    `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;
  return `${base}${path}`;
}

// Calling close() on a socket that is still connecting makes Chrome log
// "WebSocket is closed before the connection is established". That happens
// on every fast navigation and on React's dev double-mount, so a socket
// that is still connecting is closed right after its handshake instead.
// All handlers are dropped first, so the component that owned the socket
// never hears from it again.
export function closeSocket(ws: WebSocket): void {
  ws.onmessage = null;
  ws.onclose = null;
  ws.onerror = null;
  if (ws.readyState === WebSocket.CONNECTING) {
    ws.onopen = () => ws.close();
  } else {
    ws.onopen = null;
    ws.close();
  }
}
