import Link from "next/link";
import { SiteFooter } from "@/app/site-footer";

// Long-form pages that must be readable signed in or not: a wider reading
// column than the (public) sign-in card, and no sign-in check like
// (protected).
export default function LegalLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="px-4 pt-6 md:px-10">
        <Link
          href="/"
          className="font-mono text-sm tracking-widest uppercase no-underline"
          style={{ color: "var(--accent)" }}
        >
          AlphaMind
        </Link>
      </header>
      <main className="mx-auto w-full max-w-3xl flex-1 p-4 md:p-10">{children}</main>
      <SiteFooter />
    </div>
  );
}
