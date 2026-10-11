// Uploaded avatars come back as gateway paths ("/static/avatars/x.png");
// users without one get a full default-image URL. Paths need the gateway's
// origin while the frontend runs on its own port in development.
export function avatarSrc(url: string | null | undefined): string | null {
  if (!url) return null;
  return url.startsWith("/") ? `${process.env.NEXT_PUBLIC_GATEWAY_URL ?? ""}${url}` : url;
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
        <img src={src} alt="" width={size} height={size} />
      ) : (
        <span aria-hidden="true">{username.slice(0, 1).toUpperCase()}</span>
      )}
      {online !== undefined && (
        <span className={`presence-dot ${online ? "is-online" : ""}`} title={online ? "Online" : "Offline"} />
      )}
    </span>
  );
}
