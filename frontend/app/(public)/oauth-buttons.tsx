// These are links the browser follows, so they need the gateway address as
// the browser sees it — not BACKEND_API_URL, which is the server-side
// address (https://gateway/... inside Docker, unknown to the browser).
// Empty NEXT_PUBLIC_GATEWAY_URL = same origin, when served by the gateway.
export function OAuthButtons() {
  const base = `${process.env.NEXT_PUBLIC_GATEWAY_URL ?? ""}/api/v1/auth/oauth`;

  return (
    <div className="flex flex-col gap-3">
      <div className="divider">or continue with</div>
      <a href={`${base}/google/authorize`} className="btn btn-ghost btn-block">
        Google
      </a>
      <a href={`${base}/github/authorize`} className="btn btn-ghost btn-block">
        GitHub
      </a>
    </div>
  );
}
