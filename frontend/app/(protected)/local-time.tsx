"use client";

import { useSyncExternalStore } from "react";

// false while the server renders and while React hydrates, true after.
const subscribe = () => () => {};
function useMounted(): boolean {
  return useSyncExternalStore(subscribe, () => true, () => false);
}

// Shows a backend timestamp in the viewer's own locale and time zone. The
// server (UTC in Docker) cannot know them, so the server render and the
// hydration render both show the same neutral UTC text; the browser then
// switches to local time. Rendering local text during hydration would
// differ from the server's HTML and make React report a hydration error.
export function LocalTime({ iso, dateOnly = false }: { iso: string; dateOnly?: boolean }) {
  const mounted = useMounted();
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return <time dateTime={iso}>{iso}</time>;
  }
  let text: string;
  if (mounted) {
    text = dateOnly ? date.toLocaleDateString() : date.toLocaleString();
  } else {
    const utc = date.toISOString();
    text = dateOnly ? utc.slice(0, 10) : `${utc.slice(0, 10)} ${utc.slice(11, 16)} UTC`;
  }
  return <time dateTime={iso}>{text}</time>;
}
