"use client";

import "./globals.css";

// Last resort when the root layout itself fails. It replaces the whole
// document, so it renders its own <html> and <body>.
export default function GlobalError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col items-center justify-center gap-6 p-4">
        <div className="panel flex w-full max-w-sm flex-col gap-6">
          <div>
            <p className="page-eyebrow">Error</p>
            <h1 className="page-title">AlphaMind could not load</h1>
            <p className="page-subtitle">
              Something went wrong loading this page. If you were doing many actions quickly, wait a moment and try
              again.
            </p>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => retry()}>
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
