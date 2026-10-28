// Shown inside the protected layout while a page's server data loads, so a
// click on a slow page (analytics, a large portfolio) gives feedback at once.
export default function Loading() {
  return (
    <div className="page">
      <p role="status" className="empty-state">
        Loading…
      </p>
    </div>
  );
}
