// Close code from services/core-service/websocket_manager.py: the token
// expired or is invalid, so the page needs a fresh one.
export const CLOSE_UNAUTHORIZED = 4401;

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
