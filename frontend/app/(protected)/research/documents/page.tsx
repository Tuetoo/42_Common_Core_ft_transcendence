import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { UploadDocumentForm } from "./upload-document-form";

type Document = {
  id: string;
  filename: string;
  category: string;
  ticker: string | null;
  status: string;
  uploaded_at: string;
};

export default async function DocumentsPage() {
  const accessToken = await getAccessToken();
  const res = await backendFetch("/research/documents", accessToken!, { cache: "no-store" });

  requireOk(res, "Failed to load documents");

  const { items: documents }: { items: Document[] } = await res.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Research</p>
        <h1 className="page-title">Documents</h1>
      </div>

      <div className="section">
        {documents.length === 0 ? (
          <p className="empty-state">No documents yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Filename</th>
                  <th>Category</th>
                  <th>Ticker</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((d) => (
                  <tr key={d.id}>
                    <td>{d.filename}</td>
                    <td className="mono">{d.category}</td>
                    <td className="mono">{d.ticker ?? "—"}</td>
                    <td>
                      <span className="pill pill-accent">{d.status}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Upload a document</h2>
        <UploadDocumentForm />
      </div>
    </div>
  );
}
