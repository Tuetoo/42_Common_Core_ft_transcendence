import Link from "next/link";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { CreateNoteForm } from "./create-note-form";

type Note = {
  id: string;
  title: string;
  tags: string[];
  author: { username: string };
  created_at: string;
};

export default async function ResearchPage() {
  const accessToken = await getAccessToken();
  const res = await backendFetch("/research/notes", accessToken!, { cache: "no-store" });

  requireOk(res, "Failed to load notes");

  const { items: notes }: { items: Note[] } = await res.json();

  return (
    <div className="page">
      <div className="flex items-end justify-between">
        <div>
          <p className="page-eyebrow">Workspace</p>
          <h1 className="page-title">Research</h1>
        </div>
        <div className="flex gap-4">
          <Link href="/research/ask" className="link-btn">
            Ask →
          </Link>
          <Link href="/research/documents" className="link-btn">
            Documents →
          </Link>
        </div>
      </div>

      <div className="section">
        {notes.length === 0 ? (
          <p className="empty-state">No notes yet.</p>
        ) : (
          <div className="row-list">
            {notes.map((note) => (
              <Link key={note.id} href={`/research/${note.id}`} className="row-item">
                <span className="row-title">{note.title}</span>
                <span className="row-meta">
                  by {note.author.username}
                  {note.tags.length > 0 && ` · ${note.tags.join(", ")}`}
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">New note</h2>
        <CreateNoteForm />
      </div>
    </div>
  );
}
