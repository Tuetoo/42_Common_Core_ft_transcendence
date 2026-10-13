import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { Pager, pageParam } from "../pager";
import { CreatePortfolioForm } from "./create-portfolio-form";

type Portfolio = {
  id: string;
  name: string;
  currency: string;
  visibility: string;
  organization_id: string | null;
  owner: { id: string; username: string };
};

type Organization = { id: string; name: string };

const PAGE_SIZE = 20;

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function PortfoliosPage({ searchParams }: { searchParams: SearchParams }) {
  const page = pageParam((await searchParams).page);
  const { me, accessToken } = await getMe();
  const canWrite = can(me, "portfolio:write");

  const [res, orgRes] = await Promise.all([
    backendFetch(`/portfolios?page=${page}&page_size=${PAGE_SIZE}`, accessToken, { cache: "no-store" }),
    backendFetch("/organizations", accessToken, { cache: "no-store" }),
  ]);
  requireOk(res, "Failed to load portfolios");

  const { items: portfolios, total }: { items: Portfolio[]; total: number } = await res.json();
  // Organizations only label shared portfolios and fill the sharing choice:
  // if they can't be loaded, the form still offers a private portfolio.
  const organizations: Organization[] = orgRes.ok ? (await orgRes.json()).data : [];
  const orgNames = new Map(organizations.map((o) => [o.id, o.name]));

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
              <Link key={p.id} href={`/portfolios/${p.id}`} className="row-item min-w-0">
                <span className="row-title break-words">{p.name}</span>
                <span className="row-meta">
                  {p.currency}
                  {p.owner.id !== me.id && ` · owned by ${p.owner.username}`}
                  {p.visibility === "organization" &&
                    ` · shared with ${(p.organization_id && orgNames.get(p.organization_id)) || "an organization"}`}
                </span>
              </Link>
            ))}
          </div>
        )}
        <Pager basePath="/portfolios" params={{}} page={page} pageSize={PAGE_SIZE} total={total} />
      </div>

      <div className="section">
        <h2 className="section-title">Create a new portfolio</h2>
        {canWrite ? (
          <CreatePortfolioForm organizations={organizations} />
        ) : (
          <p className="empty-state">Your role ({me.system_role}) is read-only.</p>
        )}
      </div>
    </div>
  );
}
