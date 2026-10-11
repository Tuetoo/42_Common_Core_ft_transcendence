import Link from "next/link";

// Shared by the signed-in and public layouts. Links wrap onto new lines
// on narrow screens instead of overflowing.
export function SiteFooter() {
  return (
    <footer className="site-footer">
      <span>AlphaMind, a school project. Not investment advice.</span>
      <nav aria-label="Legal" className="flex flex-wrap gap-x-4 gap-y-1">
        <Link href="/privacy">Privacy Policy</Link>
        <Link href="/terms">Terms of Service</Link>
      </nav>
    </footer>
  );
}
