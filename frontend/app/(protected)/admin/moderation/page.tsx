import { LocalTime } from "@/app/(protected)/local-time";
import Form from "next/form";
import Link from "next/link";
import { notFound } from "next/navigation";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/me";
import { moderateContent } from "@/app/lib/actions/moderation";
import { Pager, pageParam } from "../../pager";
import { ActionButton } from "../inline-actions";
import { ModerateByIdForm } from "./moderate-by-id-form";

type UserRef = { id: string; username: string };

type Note = {
  id: string;
  title: string;
  author: UserRef;
  organization_id: string | null;
  created_at: string;
  is_hidden: boolean;
};

type Comment = {
  id: string;
  author: UserRef;
  content: string;
  created_at: string;
  is_hidden: boolean;
};

type Paginated<T> = { items: T[]; total: number; page: number; page_size: number };

type SearchParams = Record<string, string | string[] | undefined>;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PAGE_SIZE = 20;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value) ?? "";
}

function HiddenPill({ hidden }: { hidden: boolean }) {
  return hidden ? <span className="pill pill-down">hidden</span> : <span className="pill pill-up">visible</span>;
}

function ModerationButtons({ kind, id, hidden }: { kind: "note" | "comment"; id: string; hidden: boolean }) {
  return (
    <div className="flex flex-wrap gap-2">
      <ActionButton
        action={moderateContent.bind(null, kind, id, hidden ? "unhide" : "hide")}
        label={hidden ? "Unhide" : "Hide"}
      />
      <ActionButton
        action={moderateContent.bind(null, kind, id, "delete")}
        label="Delete"
        confirmLabel="Confirm delete"
        pendingLabel="Deleting…"
      />
    </div>
  );
}

// GET /research/notes shows a moderator every note in their own scope
// (their notes and their organizations' notes), hidden ones included.
// The backend has no site-wide content list, so content outside that
// scope is moderated by id with the form at the bottom.
export default async function ModerationPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const { me, accessToken } = await getMe();
  if (!can(me, "content:moderate")) notFound();

  const sp = await searchParams;
  const q = first(sp.q).slice(0, 200);
  const page = pageParam(sp.page);
  const noteId = UUID_RE.test(first(sp.note)) ? first(sp.note) : "";

  const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
  if (q) query.set("q", q);
  const notesRes = await backendFetch(`/research/notes?${query}`, accessToken, { cache: "no-store" });
  requireOk(notesRes, "Failed to load notes");
  const notes = (await notesRes.json()) as Paginated<Note>;

  let selected: Note | null = null;
  let comments: Paginated<Comment> | null = null;
  let selectedError = "";
  if (noteId) {
    const [noteRes, commentsRes] = await Promise.all([
      backendFetch(`/research/notes/${noteId}`, accessToken, { cache: "no-store" }),
      // The shared Pager drives ?page= of the note list, so comments come in
      // one page of the largest size the backend allows (100).
      backendFetch(`/research/notes/${noteId}/comments?page=1&page_size=100`, accessToken, { cache: "no-store" }),
    ]);
    if (noteRes.ok && commentsRes.ok) {
      selected = (await noteRes.json()).data as Note;
      comments = (await commentsRes.json()) as Paginated<Comment>;
    } else if (noteRes.status === 401 || commentsRes.status === 401) {
      requireOk(noteRes.status === 401 ? noteRes : commentsRes, "Session expired");
    } else {
      selectedError =
        "This note does not exist or is outside your notes and organizations, so its comments cannot be listed. " +
        "You can still hide or delete it by id below.";
    }
  }

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Administration</p>
        <h1 className="page-title">Moderation</h1>
        <p className="page-subtitle">
          Hide, unhide or delete research notes and comments. Hidden content is only visible to its author and to
          moderators. You see notes you wrote and notes of your organizations, hidden ones included.
        </p>
      </div>

      <div className="section">
        <Form action="/admin/moderation" className="filter-row">
          <div className="field">
            <label htmlFor="q">Search notes</label>
            <input id="q" name="q" type="search" maxLength={200} defaultValue={q} placeholder="Words in title or text" />
          </div>
          <button type="submit" className="btn btn-primary">
            Search
          </button>
        </Form>

        {notes.items.length === 0 ? (
          <p className="empty-state">No notes found.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Note</th>
                  <th>Author</th>
                  <th>State</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {notes.items.map((note) => {
                  const params = new URLSearchParams({ note: note.id });
                  if (q) params.set("q", q);
                  if (page > 1) params.set("page", String(page));
                  return (
                    <tr key={note.id}>
                      <td className="min-w-0">
                        <div className="flex flex-col gap-1 min-w-0">
                          <Link href={`/research/${note.id}`} className="link-btn break-words">
                            {note.title}
                          </Link>
                          <Link href={`/admin/moderation?${params}`} className="text-[12px] text-[var(--text-faint)]">
                            Comments →
                          </Link>
                        </div>
                      </td>
                      <td>{note.author.username}</td>
                      <td>
                        <HiddenPill hidden={note.is_hidden} />
                      </td>
                      <td>
                        <ModerationButtons kind="note" id={note.id} hidden={note.is_hidden} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <Pager basePath="/admin/moderation" params={{ q }} page={notes.page} pageSize={notes.page_size} total={notes.total} />
      </div>

      {noteId && (
        <div className="section">
          <h2 className="section-title">Comments{selected ? ` on “${selected.title}”` : ""}</h2>
          {selectedError && <p className="form-message error">{selectedError}</p>}
          {comments &&
            (comments.items.length === 0 ? (
              <p className="empty-state">No comments on this note.</p>
            ) : (
              <div className="row-list">
                {comments.items.map((c) => (
                  <div key={c.id} className="row-item">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="row-title">{c.author.username}</span>
                      <HiddenPill hidden={c.is_hidden} />
                      <span className="row-meta mono"><LocalTime iso={c.created_at} /></span>
                    </div>
                    <p className="m-0 text-[13px] whitespace-pre-wrap break-words">{c.content}</p>
                    <ModerationButtons kind="comment" id={c.id} hidden={c.is_hidden} />
                  </div>
                ))}
              </div>
            ))}
          {comments && comments.total > comments.items.length && (
            <p className="stat-note">
              Showing the first {comments.items.length} of {comments.total} comments.
            </p>
          )}
        </div>
      )}

      <div className="section">
        <h2 className="section-title">Act by id</h2>
        <p className="page-subtitle">
          For a note or comment outside your scope (for example one reported to you), paste its id.
        </p>
        <ModerateByIdForm />
      </div>
    </div>
  );
}
