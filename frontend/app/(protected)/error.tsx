"use client";

import Link from "next/link";

// Shown inside the app layout when a page throws. In production the error
// text is replaced by a digest, so the message stays generic (it also
// covers rate limiting, which can't be told apart here). No stack traces.
export default function ProtectedError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Error</p>
        <h1 className="page-title">This page could not be loaded</h1>
        <p className="page-subtitle">
          Something went wrong loading this page. If you were doing many actions quickly, wait a moment and try
          again.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <button type="button" className="btn btn-primary" onClick={() => retry()}>
          Try again
        </button>
        <Link href="/portfolios" className="link-btn">
          Go to portfolios
        </Link>
      </div>
    </div>
  );
}
