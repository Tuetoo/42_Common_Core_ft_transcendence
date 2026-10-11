import Link from "next/link";

// Shown inside the app layout by notFound(): the item doesn't exist or
// this user may not see it. Both get the same answer on purpose, so the
// page doesn't reveal what exists.
export default function ProtectedNotFound() {
  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">404</p>
        <h1 className="page-title">Not found</h1>
        <p className="page-subtitle">This page doesn&apos;t exist, or you don&apos;t have access to it.</p>
      </div>
      <Link href="/portfolios" className="btn btn-primary">
        Go to portfolios
      </Link>
    </div>
  );
}
