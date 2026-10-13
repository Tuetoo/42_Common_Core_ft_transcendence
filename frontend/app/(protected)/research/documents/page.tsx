import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { Pager, pageParam } from "../../pager";
import { UploadDocumentForm } from "./upload-document-form";
import { DocumentTable } from "./document-table";
import { DOCUMENT_CATEGORIES, DOCUMENT_STATUSES, TICKER_HINT, TICKER_HTML_PATTERN, TICKER_RE } from "../limits";
import { loadOrganizations } from "../organizations";
import type { Paginated, ResearchDocument } from "../types";

const PAGE_SIZE = 20;
const SORTS = [
  ["-created_at", "Newest upload"],
  ["created_at", "Oldest upload"],
  ["-content_date", "Newest document date"],
  ["content_date", "Oldest document date"],
  ["title", "Title A–Z"],
  ["-title", "Title Z–A"],
  ["status", "Status A–Z"],
  ["-status", "Status Z–A"],
] as const;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

type SearchParams = Record<string, string | string[] | undefined>;

function one(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

// Keeps only values the backend accepts (GET /research/documents in
// services/core-service/domains/research/api.py), so a hand-edited URL
// shows an unfiltered list instead of a 422 error page.
function readFilters(sp: SearchParams) {
  const ticker = one(sp.ticker).toUpperCase();
  const category = one(sp.category);
  const status = one(sp.status);
  const sort = one(sp.sort);
  const from = one(sp.created_from);
  const to = one(sp.created_to);
  const filters = {
    ticker: TICKER_RE.test(ticker) ? ticker : "",
    category: (DOCUMENT_CATEGORIES as readonly string[]).includes(category) ? category : "",
    status: (DOCUMENT_STATUSES as readonly string[]).includes(status) ? status : "",
    sort: SORTS.some(([value]) => value === sort) ? sort : "-created_at",
    created_from: DATE_RE.test(from) ? from : "",
    created_to: DATE_RE.test(to) ? to : "",
    include_system: one(sp.include_system) === "true" ? "true" : "",
  };
  // The backend rejects a reversed range; swap it instead.
  if (filters.created_from && filters.created_to && filters.created_from > filters.created_to) {
    [filters.created_from, filters.created_to] = [filters.created_to, filters.created_from];
  }
  return filters;
}

export default async function DocumentsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const sp = await searchParams;
  const { me, accessToken } = await getMe();
  const filters = readFilters(sp);
  const page = pageParam(sp.page);

  const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }

  const [res, organizations] = await Promise.all([
    backendFetch(`/research/documents?${query.toString()}`, accessToken, { cache: "no-store" }),
    loadOrganizations(accessToken),
  ]);
  requireOk(res, "Failed to load documents");
  const { items: documents, total }: Paginated<ResearchDocument> = await res.json();
  const filtered = Object.entries(filters).some(([key, value]) => key !== "sort" && value);

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">
          <Link href="/research" className="link-btn" style={{ padding: 0 }}>
            Research
          </Link>{" "}
          / Documents
        </p>
        <h1 className="page-title">Documents</h1>
        <p className="page-subtitle">
          Your uploads feed the Ask page. Files are private to you unless shared with an organization.
        </p>
      </div>

      {can(me, "document:upload") && (
        <div className="section">
          <h2 className="section-title">Upload a document</h2>
          <UploadDocumentForm organizations={organizations} />
        </div>
      )}

      <div className="section">
        <h2 className="section-title">Your documents</h2>
        <form method="get" action="/research/documents" className="filter-row" role="search" aria-label="Filter documents">
          <div className="field">
            <label htmlFor="f-ticker">Ticker</label>
            <input
              id="f-ticker"
              name="ticker"
              autoComplete="off"
              type="text"
              maxLength={10}
              pattern={TICKER_HTML_PATTERN}
              title={TICKER_HINT}
              defaultValue={filters.ticker}
              placeholder="All"
              className="uppercase"
            />
          </div>
          <div className="field">
            <label htmlFor="f-category">Category</label>
            <select id="f-category" name="category" autoComplete="off" defaultValue={filters.category}>
              <option value="">All</option>
              {DOCUMENT_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-status">Status</label>
            <select id="f-status" name="status" autoComplete="off" defaultValue={filters.status}>
              <option value="">All</option>
              {DOCUMENT_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-from">Uploaded from</label>
            <input id="f-from" name="created_from" autoComplete="off" type="date" defaultValue={filters.created_from} />
          </div>
          <div className="field">
            <label htmlFor="f-to">Uploaded to</label>
            <input id="f-to" name="created_to" autoComplete="off" type="date" defaultValue={filters.created_to} />
          </div>
          <div className="field">
            <label htmlFor="f-sort">Sort by</label>
            <select id="f-sort" name="sort" autoComplete="off" defaultValue={filters.sort}>
              {SORTS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 pb-2 text-[13px]">
            <input type="checkbox" name="include_system" autoComplete="off" value="true" defaultChecked={!!filters.include_system} />
            Include system documents
          </label>
          <div className="flex flex-wrap items-center gap-3 pb-1">
            <button type="submit" className="btn btn-primary">
              Apply
            </button>
            {filtered && (
              <Link href="/research/documents" className="link-btn">
                Clear filters
              </Link>
            )}
          </div>
        </form>

        {documents.length === 0 ? (
          <p className="empty-state">{filtered ? "No documents match these filters." : "No documents yet."}</p>
        ) : (
          <DocumentTable documents={documents} meId={me.id} />
        )}
        <Pager
          basePath="/research/documents"
          params={filters}
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
        />
      </div>
    </div>
  );
}
