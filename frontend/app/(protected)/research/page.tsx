import { LocalTime } from "@/app/(protected)/local-time";
import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/me";
import { Pager, pageParam } from "../pager";
import { CreateNoteForm } from "./create-note-form";
import { SentimentPill } from "./sentiment-pill";
import { loadOrganizations } from "./organizations";
import { SEARCH_TEXT_MAX, TAG_MAX_CHARS, UUID_RE } from "./limits";
import type { Note, Paginated } from "./types";

const PAGE_SIZE = 20;
const SORTS = [
  ["-created_at", "Newest first"],
  ["created_at", "Oldest first"],
  ["title", "Title A–Z"],
  ["-title", "Title Z–A"],
] as const;

type SearchParams = Record<string, string | string[] | undefined>;

function one(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

// Only the parameters GET /research/notes supports
// (services/core-service/domains/research/api.py search_notes): q (full-text
// search over title and content), tag (exact tag name), author_id,
// organization_id, sort, page, page_size. Invalid values are dropped so a
// hand-edited URL never turns into a 422 error page.
function readFilters(sp: SearchParams) {
  const q = one(sp.q).slice(0, SEARCH_TEXT_MAX);
  const tag = one(sp.tag).slice(0, TAG_MAX_CHARS);
  const authorId = one(sp.author_id);
  const organizationId = one(sp.organization_id);
  const sort = one(sp.sort);
  return {
    q,
    tag,
    author_id: UUID_RE.test(authorId) ? authorId : "",
    organization_id: UUID_RE.test(organizationId) ? organizationId : "",
    sort: SORTS.some(([value]) => value === sort) ? sort : "-created_at",
  };
}

function snippet(content: string | null): string {
  if (!content) return "";
  const flat = content.replace(/\s+/g, " ").trim();
  return flat.length > 160 ? `${flat.slice(0, 159)}…` : flat;
}

export default async function ResearchPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const sp = await searchParams;
  const { me, accessToken } = await getMe();
  const filters = readFilters(sp);
  const page = pageParam(sp.page);

  const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }

  const [res, organizations] = await Promise.all([
    backendFetch(`/research/notes?${query.toString()}`, accessToken, { cache: "no-store" }),
    loadOrganizations(accessToken),
  ]);
  requireOk(res, "Failed to load notes");
  const { items: notes, total }: Paginated<Note> = await res.json();

  const orgName = new Map(organizations.map((o) => [o.id, o.name]));
  const filtered = Boolean(filters.q || filters.tag || filters.author_id || filters.organization_id);
  const canWrite = can(me, "note:write");

  return (
    <div className="page">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <p className="page-eyebrow">Workspace</p>
          <h1 className="page-title">Research</h1>
        </div>
        <div className="flex flex-wrap gap-4">
          <Link href="/research/ask" className="link-btn">
            Ask →
          </Link>
          <Link href="/research/documents" className="link-btn">
            Documents →
          </Link>
        </div>
      </div>

      <div className="section">
        <h2 className="section-title">Search notes</h2>
        <form method="get" action="/research" className="filter-row" role="search" aria-label="Search research notes">
          <div className="field min-w-0 flex-[1_1_14rem]">
            <label htmlFor="q">Search text</label>
            <input
              id="q"
              name="q"
              type="search"
              maxLength={SEARCH_TEXT_MAX}
              defaultValue={filters.q}
              placeholder="Words in the title or content"
            />
          </div>
          <div className="field">
            <label htmlFor="tag">Tag</label>
            <input
              id="tag"
              name="tag"
              type="text"
              maxLength={TAG_MAX_CHARS}
              defaultValue={filters.tag}
              placeholder="e.g. AAPL"
              title="Exact tag name"
            />
          </div>
          <div className="field">
            <label htmlFor="organization_id">Organization</label>
            <select id="organization_id" name="organization_id" defaultValue={filters.organization_id}>
              <option value="">All I can see</option>
              {organizations.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="sort">Sort by</label>
            <select id="sort" name="sort" defaultValue={filters.sort}>
              {SORTS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 pb-2 text-[13px]">
            <input type="checkbox" name="author_id" value={me.id} defaultChecked={filters.author_id === me.id} />
            Only my notes
          </label>
          <div className="flex flex-wrap items-center gap-3 pb-1">
            <button type="submit" className="btn btn-primary">
              Search
            </button>
            {filtered && (
              <Link href="/research" className="link-btn">
                Clear filters
              </Link>
            )}
          </div>
        </form>
        {filters.q && (
          <p className="text-xs text-[var(--text-faint)]">
            Results are ranked by relevance first, then by the chosen order.
          </p>
        )}

        {notes.length === 0 ? (
          <p className="empty-state">{filtered ? "No notes match these filters." : "No notes yet."}</p>
        ) : (
          <div className="row-list">
            {notes.map((note) => (
              <Link key={note.id} href={`/research/${note.id}`} className="row-item min-w-0">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="row-title min-w-0 break-words">{note.title}</span>
                  <SentimentPill item={note} />
                  <span className={note.organization_id ? "pill pill-accent" : "pill"}>
                    {note.organization_id ? `shared: ${orgName.get(note.organization_id) ?? "organization"}` : "private"}
                  </span>
                  {note.can_edit ? <span className="pill">can edit</span> : <span className="pill">read only</span>}
                  {note.is_hidden && <span className="pill pill-down">hidden</span>}
                </span>
                {note.content && (
                  <span className="break-words text-[13px] text-[var(--text-secondary)]">{snippet(note.content)}</span>
                )}
                <span className="row-meta break-words">
                  by {note.author.username} · <LocalTime iso={note.created_at} dateOnly />
                  {note.tags.length > 0 && ` · ${note.tags.join(", ")}`}
                </span>
              </Link>
            ))}
          </div>
        )}
        <Pager basePath="/research" params={filters} page={page} pageSize={PAGE_SIZE} total={total} />
      </div>

      {canWrite && (
        <div className="section">
          <h2 className="section-title">New note</h2>
          <CreateNoteForm organizations={organizations} />
        </div>
      )}
    </div>
  );
}
