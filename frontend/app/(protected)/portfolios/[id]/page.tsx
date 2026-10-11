import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { formatNumber } from "@/app/lib/format";
import { AddHoldingForm } from "./add-holding-form";
import { AddTransactionForm } from "./add-transaction-form";

type Portfolio = {
  id: string;
  name: string;
  currency: string;
  visibility: string;
  owner: { username: string };
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

export default async function PortfolioDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const accessToken = await getAccessToken();

  const [portfolioRes, holdingsRes, transactionsRes] = await Promise.all([
    backendFetch(`/portfolios/${id}`, accessToken!, { cache: "no-store" }),
    backendFetch(`/portfolios/${id}/holdings`, accessToken!, { cache: "no-store" }),
    backendFetch(`/portfolios/${id}/transactions`, accessToken!, { cache: "no-store" }),
  ]);

  requireOk(portfolioRes, "Failed to load portfolio");
  requireOk(holdingsRes, "Failed to load holdings");
  requireOk(transactionsRes, "Failed to load transactions");

  const { data: portfolio }: { data: Portfolio } = await portfolioRes.json();
  const { data: holdings }: { data: Holding[] } = await holdingsRes.json();
  const { items: transactions }: { items: Transaction[] } = await transactionsRes.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Portfolio</p>
        <h1 className="page-title">{portfolio.name}</h1>
        <p className="page-subtitle">
          {portfolio.currency} · owned by {portfolio.owner.username}
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
        <AddHoldingForm portfolioId={portfolio.id} />
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
                    <td className="mono">{new Date(t.transaction_date).toLocaleDateString()}</td>
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
      </div>

      <div className="section">
        <h2 className="section-title">Record a transaction</h2>
        <AddTransactionForm portfolioId={portfolio.id} />
      </div>
    </div>
  );
}
