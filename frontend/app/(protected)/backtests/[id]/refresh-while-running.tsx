"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

const REFRESH_MS = 3000;

// The backtest runs in the background; reload the server data until it ends.
export function RefreshWhileRunning() {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => router.refresh(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [router]);
  return null;
}
