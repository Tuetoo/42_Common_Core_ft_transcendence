// Uploaded avatars come back as gateway paths ("/static/avatars/x.png");
// paths need the gateway's origin while the frontend runs on its own port
// in development. Users without an upload get a full URL to a third-party
// default-image service: loading that would send their username to another
// site and log console errors when it is blocked or offline, so any
// absolute URL falls back to the initial letter instead.
export function avatarSrc(url: string | null | undefined): string | null {
  if (!url || !url.startsWith("/") || url.startsWith("//")) return null;
  return `${process.env.NEXT_PUBLIC_GATEWAY_URL ?? ""}${url}`;
}

export function Avatar({
  url,
  username,
  size = 32,
  online,
}: {
  url: string | null | undefined;
  username: string;
  size?: number;
  online?: boolean;
}) {
  const src = avatarSrc(url);
  return (
    <span className="avatar" style={{ width: size, height: size }}>
      {src ? (
        // A plain <img>: next/image would need every avatar host allow-listed
        // in next.config for little gain on a small, already-sized picture.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={`${username}'s avatar`} width={size} height={size} />
      ) : (
        <span aria-hidden="true">{username.slice(0, 1).toUpperCase()}</span>
      )}
      {online !== undefined && (
        <span
          className={`presence-dot ${online ? "is-online" : ""}`}
          role="img"
          aria-label={online ? "Online" : "Offline"}
          title={online ? "Online" : "Offline"}
        />
      )}
    </span>
  );
}
