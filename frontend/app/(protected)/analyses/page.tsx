import Link from "next/link";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { StartAnalysisForm } from "./start-analysis-form";
import { statusPillClass } from "./status";

type Analysis = {
  id: string;
  ticker: string;
  status: string;
  created_at: string;
};

type Portfolio = {
  id: string;
  name: string;
};

export default async function AnalysesPage() {
  const accessToken = await getAccessToken();

  const [analysesRes, portfoliosRes] = await Promise.all([
    backendFetch("/analyses?page_size=50", accessToken!, { cache: "no-store" }),
    backendFetch("/portfolios", accessToken!, { cache: "no-store" }),
  ]);

  requireOk(analysesRes, "Failed to load analyses");
  requireOk(portfoliosRes, "Failed to load portfolios");

  const { items: analyses }: { items: Analysis[] } = await analysesRes.json();
  const { items: portfolios }: { items: Portfolio[] } = await portfoliosRes.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Workspace</p>
        <h1 className="page-title">Analyses</h1>
        <p className="page-subtitle">
          Specialist AI agents research a ticker, a Bull and a Bear debate it, and a Judge gives the final call.
        </p>
      </div>

      <div className="section">
        <h2 className="section-title">Run a new analysis</h2>
        <StartAnalysisForm portfolios={portfolios.map((p) => ({ id: p.id, name: p.name }))} />
      </div>

      <div className="section">
        <h2 className="section-title">Your analyses</h2>
        {analyses.length === 0 ? (
          <p className="empty-state">You haven&apos;t run any analyses yet.</p>
        ) : (
          <div className="row-list">
            {analyses.map((a) => (
              <Link
                key={a.id}
                href={`/analyses/${a.id}`}
                className="row-item"
                style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}
              >
                <div>
                  <span className="row-title mono">{a.ticker}</span>
                  <div className="row-meta">{new Date(a.created_at).toLocaleString()}</div>
                </div>
                <span className={statusPillClass(a.status)}>{a.status}</span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
