import Link from "next/link";

// Previous / next links for a paginated backend list
// ({ items, total, page, page_size }). Every other query parameter of the
// current page (filters, sorting) is kept in the links.
export function Pager({
  basePath,
  params,
  page,
  pageSize,
  total,
}: {
  basePath: string;
  params: Record<string, string | undefined>;
  page: number;
  pageSize: number;
  total: number;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;

  const href = (target: number) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value && key !== "page") query.set(key, value);
    }
    query.set("page", String(target));
    return `${basePath}?${query.toString()}`;
  };

  return (
    <nav aria-label="Pagination" className="flex items-center gap-3 text-sm mt-4">
      {page > 1 ? (
        <Link href={href(page - 1)} className="link-btn" rel="prev">
          Previous
        </Link>
      ) : (
        <span className="text-[var(--text-faint)]">Previous</span>
      )}
      <span className="text-[var(--text-secondary)]">
        Page {page} of {pages} ({total} total)
      </span>
      {page < pages ? (
        <Link href={href(page + 1)} className="link-btn" rel="next">
          Next
        </Link>
      ) : (
        <span className="text-[var(--text-faint)]">Next</span>
      )}
    </nav>
  );
}

// Reads ?page= from search params: a positive integer, else 1.
export function pageParam(value: string | string[] | undefined): number {
  const n = Number(Array.isArray(value) ? value[0] : value);
  return Number.isInteger(n) && n > 0 ? n : 1;
}
