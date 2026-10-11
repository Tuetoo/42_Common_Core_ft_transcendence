import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { AddCommentForm } from "./add-comment-form";
import { CollabEditor } from "./collab-editor";

type Note = {
  id: string;
  title: string;
  content: string | null;
  tags: string[];
  author: { username: string };
  created_at: string;
};

type Comment = {
  id: string;
  author: { username: string };
  content: string;
  created_at: string;
  updated_at: string | null;
};

export default async function NoteDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const accessToken = await getAccessToken();

  const [noteRes, commentsRes] = await Promise.all([
    backendFetch(`/research/notes/${id}`, accessToken!, { cache: "no-store" }),
    backendFetch(`/research/notes/${id}/comments?page_size=100`, accessToken!, { cache: "no-store" }),
  ]);

  requireOk(noteRes, "Failed to load note");
  requireOk(commentsRes, "Failed to load comments");

  const { data: note }: { data: Note } = await noteRes.json();
  const { items: comments }: { items: Comment[] } = await commentsRes.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Research note</p>
        <h1 className="page-title">{note.title}</h1>
        <p className="page-subtitle">
          by {note.author.username}
          {note.tags.length > 0 && ` · ${note.tags.join(", ")}`}
        </p>
      </div>

      {note.content && <p style={{ color: "var(--text)", whiteSpace: "pre-wrap" }}>{note.content}</p>}

      <div className="section">
        <h2 className="section-title">Live collaborative editor</h2>
        <p style={{ fontSize: 12.5, color: "var(--text-secondary)" }}>
          Separate from the content above — this is a shared live document (Yjs). Open this note in
          another tab to see edits sync in real time.
        </p>
        <CollabEditor noteId={note.id} accessToken={accessToken!} />
      </div>

      <div className="section">
        <h2 className="section-title">Comments</h2>
        {comments.length === 0 ? (
          <p className="empty-state">No comments yet.</p>
        ) : (
          <div className="row-list">
            {comments.map((c) => (
              <div key={c.id} className="row-item">
                <span style={{ color: "var(--text)", whiteSpace: "pre-wrap", fontSize: 13 }}>{c.content}</span>
                <span className="row-meta">
                  {c.author.username} · {new Date(c.created_at).toLocaleString()}
                  {c.updated_at && " · edited"}
                </span>
              </div>
            ))}
          </div>
        )}
        <AddCommentForm noteId={note.id} />
      </div>
    </div>
  );
}
