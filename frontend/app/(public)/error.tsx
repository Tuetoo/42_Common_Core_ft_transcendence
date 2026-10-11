"use client";

import Link from "next/link";

// Shown inside the public layout's panel when a page throws. Generic on
// purpose: production hides the real error, and no stack trace is shown.
export default function PublicError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Error</p>
        <h1 className="page-title">Something went wrong</h1>
        <p className="page-subtitle">
          Something went wrong loading this page. If you were doing many actions quickly, wait a moment and try
          again.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <button type="button" className="btn btn-primary" onClick={() => reset()}>
          Try again
        </button>
        <Link href="/login" className="link-btn">
          Back to login
        </Link>
      </div>
    </div>
  );
}
