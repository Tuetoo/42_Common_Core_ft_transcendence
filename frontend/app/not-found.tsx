import Link from "next/link";
import { SiteFooter } from "./site-footer";

// Any URL that matches no route. Styled like the public pages because the
// visitor may not be signed in.
export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col">
      <main className="flex flex-1 flex-col items-center justify-center gap-10 p-4 sm:p-8">
        <div className="font-mono text-sm tracking-widest text-[var(--text-faint)] uppercase">AlphaMind</div>
        <div className="panel flex w-full max-w-sm flex-col gap-6">
          <div>
            <p className="page-eyebrow">404</p>
            <h1 className="page-title">Page not found</h1>
            <p className="page-subtitle">The address may be mistyped, or the page has moved.</p>
          </div>
          <Link href="/" className="btn btn-primary">
            Go to the home page
          </Link>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
