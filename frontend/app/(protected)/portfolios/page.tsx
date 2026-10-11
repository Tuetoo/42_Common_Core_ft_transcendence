import Link from "next/link";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { CreatePortfolioForm } from "./create-portfolio-form";

type Portfolio = {
  id: string;
  name: string;
  currency: string;
  visibility: string;
};

export default async function PortfoliosPage() {
  const accessToken = await getAccessToken();
  const res = await backendFetch("/portfolios", accessToken!, { cache: "no-store" });

  requireOk(res, "Failed to load portfolios");

  const { items: portfolios }: { items: Portfolio[] } = await res.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Workspace</p>
        <h1 className="page-title">Portfolios</h1>
      </div>

      <div className="section">
        {portfolios.length === 0 ? (
          <p className="empty-state">You don&apos;t have any portfolios yet.</p>
        ) : (
          <div className="row-list">
            {portfolios.map((p) => (
              <Link key={p.id} href={`/portfolios/${p.id}`} className="row-item">
                <span className="row-title">{p.name}</span>
                <span className="row-meta">{p.currency}</span>
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Create a new portfolio</h2>
        <CreatePortfolioForm />
      </div>
    </div>
  );
}
