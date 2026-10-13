import { backendFetch, requireOk } from "@/app/lib/backend";
import { formatNumber } from "@/app/lib/format";
import { can, getMe } from "@/app/lib/session";
import { Pager, pageParam } from "../../pager";
import { LocalTime } from "@/app/(protected)/local-time";
import { AddHoldingForm } from "./add-holding-form";
import { AddTransactionForm } from "./add-transaction-form";
import { BacktestForm } from "./backtest-form";
import { PortfolioSettingsForm } from "./portfolio-settings-form";

type Portfolio = {
  id: string;
  name: string;
  currency: string;
  visibility: string;
  organization_id: string | null;
  owner: { id: string; username: string };
};

type Holding = {
  id: string;
  ticker: string;
  quantity: string;
  average_cost: string;
};

type Transaction = {
  id: string;
  ticker: string;
  transaction_type: string;
  quantity: string | null;
  price: string | null;
  transaction_date: string;
};

const PAGE_SIZE = 20;

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function PortfolioDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: SearchParams;
}) {
  const { id } = await params;
  const page = pageParam((await searchParams).page);
  const { me, accessToken } = await getMe();
  const path = `/portfolios/${encodeURIComponent(id)}`;

  const [portfolioRes, holdingsRes, transactionsRes] = await Promise.all([
    backendFetch(path, accessToken, { cache: "no-store" }),
    backendFetch(`${path}/holdings`, accessToken, { cache: "no-store" }),
    backendFetch(`${path}/transactions?page=${page}&page_size=${PAGE_SIZE}`, accessToken, { cache: "no-store" }),
  ]);

  requireOk(portfolioRes, "Failed to load portfolio");
  requireOk(holdingsRes, "Failed to load holdings");
  requireOk(transactionsRes, "Failed to load transactions");

  const { data: portfolio }: { data: Portfolio } = await portfolioRes.json();
  const { data: holdings }: { data: Holding[] } = await holdingsRes.json();
  const { items: transactions, total }: { items: Transaction[]; total: number } = await transactionsRes.json();

  // Organization members may read a shared portfolio, but only its owner
  // may change it (the backend answers 403 otherwise), and only with the
  // portfolio:write permission (a guest has none).
  const isOwner = portfolio.owner.id === me.id;
  const canEdit = isOwner && can(me, "portfolio:write");
  const readOnlyNote = !isOwner
    ? `This portfolio belongs to ${portfolio.owner.username}; you can view it but not change it.`
    : `Your role (${me.system_role}) is read-only.`;

  let organizationName: string | null = null;
  if (portfolio.organization_id) {
    const orgRes = await backendFetch(`/organizations/${portfolio.organization_id}`, accessToken, {
      cache: "no-store",
    });
    organizationName = orgRes.ok ? (await orgRes.json()).data.name : "the organization";
  }

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Portfolio</p>
        <h1 className="page-title break-words">{portfolio.name}</h1>
        <p className="page-subtitle">
          {portfolio.currency} · owned by {portfolio.owner.username} ·{" "}
          {portfolio.visibility === "organization" && organizationName
            ? `shared with ${organizationName}`
            : "private"}
        </p>
      </div>

      <div className="section">
        <h2 className="section-title">Holdings</h2>
        {holdings.length === 0 ? (
          <p className="empty-state">No holdings yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Quantity</th>
                  <th>Average cost</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map((h) => (
                  <tr key={h.id}>
                    <td className="mono">{h.ticker}</td>
                    <td className="mono">{formatNumber(h.quantity)}</td>
                    <td className="mono">{formatNumber(h.average_cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Add a holding</h2>
        {canEdit ? <AddHoldingForm portfolioId={portfolio.id} /> : <p className="empty-state">{readOnlyNote}</p>}
      </div>

      <div className="section">
        <h2 className="section-title">Transactions</h2>
        {transactions.length === 0 ? (
          <p className="empty-state">No transactions yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Ticker</th>
                  <th>Quantity</th>
                  <th>Price</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((t) => (
                  <tr key={t.id}>
                    <td className="mono">
                      <LocalTime iso={t.transaction_date} dateOnly />
                    </td>
                    <td>
                      <span
                        className={`pill ${
                          t.transaction_type === "buy" ? "pill-up" : t.transaction_type === "sell" ? "pill-down" : ""
                        }`}
                      >
                        {t.transaction_type}
                      </span>
                    </td>
                    <td className="mono">{t.ticker}</td>
                    <td className="mono">{formatNumber(t.quantity)}</td>
                    <td className="mono">{formatNumber(t.price)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager basePath={`/portfolios/${portfolio.id}`} params={{}} page={page} pageSize={PAGE_SIZE} total={total} />
      </div>

      <div className="section">
        <h2 className="section-title">Record a transaction</h2>
        {canEdit ? (
          <AddTransactionForm portfolioId={portfolio.id} />
        ) : (
          <p className="empty-state">{readOnlyNote}</p>
        )}
      </div>

      {can(me, "analysis:create") && (
        <div className="section">
          <h2 className="section-title">Backtest</h2>
          <BacktestForm portfolioId={portfolio.id} />
        </div>
      )}

      {canEdit && (
        <div className="section">
          <h2 className="section-title">Settings</h2>
          <PortfolioSettingsForm
            portfolioId={portfolio.id}
            name={portfolio.name}
            visibility={portfolio.visibility}
            organizationName={organizationName}
          />
        </div>
      )}
    </div>
  );
}
