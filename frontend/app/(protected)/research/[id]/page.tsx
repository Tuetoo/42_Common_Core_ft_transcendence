import { ActionButton } from "@/app/(protected)/admin/inline-actions";
import { deleteNote } from "@/app/lib/actions/research";
import { LocalTime } from "@/app/(protected)/local-time";
import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { Pager, pageParam } from "../../pager";
import { AddCommentForm } from "./add-comment-form";
import { CollabEditor } from "./collab-editor";
import { SentimentPill } from "../sentiment-pill";
import { loadOrganizations } from "../organizations";
import type { Comment, Note, Paginated } from "../types";

const COMMENTS_PAGE_SIZE = 50;

export default async function NoteDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const page = pageParam((await searchParams).page);
  const { me, accessToken } = await getMe();

  const [noteRes, commentsRes, organizations] = await Promise.all([
    backendFetch(`/research/notes/${encodeURIComponent(id)}`, accessToken, { cache: "no-store" }),
    backendFetch(
      `/research/notes/${encodeURIComponent(id)}/comments?page=${page}&page_size=${COMMENTS_PAGE_SIZE}`,
      accessToken,
      { cache: "no-store" }
    ),
    loadOrganizations(accessToken),
  ]);

  requireOk(noteRes, "Failed to load note");
  requireOk(commentsRes, "Failed to load comments");

  const { data: note }: { data: Note } = await noteRes.json();
  const { items: comments, total }: Paginated<Comment> = await commentsRes.json();

  const orgName = note.organization_id
    ? (organizations.find((o) => o.id === note.organization_id)?.name ?? "an organization")
    : null;
  const isAuthor = note.author.id === me.id;
  // Same rule as the backend's add_comment: the author, or an
  // organization member who may edit (can_edit).
  const mayComment = can(me, "comment:write") && (isAuthor || note.can_edit);

  return (
    <div className="page">
      <div className="min-w-0">
        <p className="page-eyebrow">
          <Link href="/research" className="link-btn" style={{ padding: 0 }}>
            Research
          </Link>{" "}
          / Note
        </p>
        <h1 className="page-title break-words">{note.title}</h1>
        <p className="page-subtitle break-words">
          by {note.author.username} · <LocalTime iso={note.created_at} />
          {note.tags.length > 0 && ` · ${note.tags.join(", ")}`}
        </p>
        {isAuthor && can(me, "note:write") && (
          <div className="mt-2">
            <ActionButton
              action={deleteNote.bind(null, note.id)}
              label="Delete note"
              confirmLabel="Confirm delete"
              pendingLabel="Deleting…"
            />
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <SentimentPill item={note} />
          <span className={orgName ? "pill pill-accent" : "pill"}>
            {orgName ? `shared with ${orgName}` : "private"}
          </span>
          <span className={note.can_edit ? "pill pill-up" : "pill"}>
            {note.can_edit ? "you can edit" : "read only for you"}
          </span>
          {note.is_hidden && <span className="pill pill-down">hidden by a moderator</span>}
        </div>
        <p className="mt-2 text-xs text-[var(--text-faint)]">
          {orgName
            ? `Who can edit: the author and the owners, admins and members of ${orgName}. Its viewers can only read.`
            : isAuthor
              ? "Only you can see and edit this note. To edit it together, create a note shared with an organization."
              : "Only the author can edit this note."}
        </p>
      </div>

      <div className="section">
        <h2 className="section-title">Content</h2>
        <p className="m-0 text-[12.5px] text-[var(--text-secondary)]">
          This is the note itself, edited live. Everyone who has the note open sees each change as it is
          typed, and the text is saved back to the note automatically (search and sentiment use the saved
          text). To try it, open this note as a second user who belongs to the note&apos;s organization.
        </p>
        <CollabEditor
          noteId={note.id}
          accessToken={accessToken}
          canEdit={note.can_edit}
          initialText={note.content ?? ""}
        />
      </div>

      <div className="section">
        <h2 className="section-title">Comments ({total})</h2>
        {comments.length === 0 ? (
          <p className="empty-state">No comments yet.</p>
        ) : (
          <div className="row-list">
            {comments.map((c) => (
              <div key={c.id} className="row-item min-w-0">
                <span className="whitespace-pre-wrap break-words text-[13px] text-[var(--text)]">{c.content}</span>
                <span className="flex flex-wrap items-center gap-2">
                  <span className="row-meta">
                    {c.author.username} · <LocalTime iso={c.created_at} />
                    {c.updated_at && " · edited"}
                  </span>
                  <SentimentPill item={c} />
                  {c.is_hidden && <span className="pill pill-down">hidden</span>}
                </span>
              </div>
            ))}
          </div>
        )}
        <Pager
          basePath={`/research/${note.id}`}
          params={{}}
          page={page}
          pageSize={COMMENTS_PAGE_SIZE}
          total={total}
        />
        {mayComment ? (
          <AddCommentForm noteId={note.id} />
        ) : (
          <p className="text-xs text-[var(--text-faint)]">
            Only the note&apos;s author and members of its organization (not viewers) can comment.
          </p>
        )}
      </div>
    </div>
  );
}
