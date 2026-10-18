"use client";

// Kept in this tab's sessionStorage and echoed back by auth-service in the
// /oauth/complete fragment. /oauth/complete accepts a result only when the
// two match, so a completion link made in another browser (login CSRF)
// cannot sign this browser in.
export const OAUTH_MARKER_KEY = "alphamind.oauth.marker";

function newMarker(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// These are links the browser follows, so they need the gateway address as
// the browser sees it — not BACKEND_API_URL, which is the server-side
// address (https://gateway/... inside Docker, unknown to the browser).
// Empty NEXT_PUBLIC_GATEWAY_URL = same origin, when served by the gateway.
export function OAuthButtons({ next = "" }: { next?: string }) {
  const base = `${process.env.NEXT_PUBLIC_GATEWAY_URL ?? ""}/api/v1/auth/oauth`;

  // Runs before the browser follows the link, so the link it follows
  // carries the marker that was just stored.
  function addMarker(event: React.MouseEvent<HTMLAnchorElement>) {
    const params = new URLSearchParams();
    try {
      const marker = newMarker();
      window.sessionStorage.setItem(OAUTH_MARKER_KEY, marker);
      params.set("marker", marker);
    } catch {
      // Without storage the completion is refused; the page says why.
    }
    if (next.startsWith("/")) params.set("next", next);
    event.currentTarget.href = `${event.currentTarget.href.split("?")[0]}?${params}`;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="divider">or continue with</div>
      <a href={`${base}/google/authorize`} onClick={addMarker} className="btn btn-ghost btn-block">
        Google
      </a>
      <a href={`${base}/github/authorize`} onClick={addMarker} className="btn btn-ghost btn-block">
        GitHub
      </a>
    </div>
  );
}
