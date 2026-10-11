import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/me";
import { Pager, pageParam } from "../pager";
import { LocalTime } from "@/app/(protected)/local-time";
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
  owner: { id: string };
};

const PAGE_SIZE = 20;

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function AnalysesPage({ searchParams }: { searchParams: SearchParams }) {
  const page = pageParam((await searchParams).page);
  const { me, accessToken } = await getMe();
  const canCreate = can(me, "analysis:create");

  const [analysesRes, portfoliosRes] = await Promise.all([
    backendFetch(`/analyses?page=${page}&page_size=${PAGE_SIZE}`, accessToken, { cache: "no-store" }),
    // Only the start form needs portfolios; 100 is the backend's largest page.
    canCreate ? backendFetch("/portfolios?page=1&page_size=100", accessToken, { cache: "no-store" }) : null,
  ]);

  requireOk(analysesRes, "Failed to load analyses");
  if (portfoliosRes) requireOk(portfoliosRes, "Failed to load portfolios");

  const { items: analyses, total }: { items: Analysis[]; total: number } = await analysesRes.json();
  // An analysis may only be tied to a portfolio the user owns (the backend
  // checks ensure_portfolio_owner), so shared ones are left out.
  const portfolios: Portfolio[] = portfoliosRes
    ? ((await portfoliosRes.json()).items as Portfolio[]).filter((p) => p.owner.id === me.id)
    : [];

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
        {canCreate ? (
          <StartAnalysisForm portfolios={portfolios.map((p) => ({ id: p.id, name: p.name }))} />
        ) : (
          <p className="empty-state">Your role ({me.system_role}) is read-only.</p>
        )}
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
                className="row-item min-w-0"
                style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 12 }}
              >
                <div className="min-w-0">
                  <span className="row-title mono">{a.ticker}</span>
                  <div className="row-meta">
                    <LocalTime iso={a.created_at} />
                  </div>
                </div>
                <span className={statusPillClass(a.status)}>{a.status}</span>
              </Link>
            ))}
          </div>
        )}
        <Pager basePath="/analyses" params={{}} page={page} pageSize={PAGE_SIZE} total={total} />
      </div>
    </div>
  );
}
