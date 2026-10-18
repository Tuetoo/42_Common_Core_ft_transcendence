import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { getMe } from "@/app/lib/session";
import { statusPillClass } from "@/app/lib/format";
import { LocalTime } from "@/app/(protected)/local-time";
import { pageParam } from "../pager";

type Backtest = {
  id: string;
  strategy_name: string;
  benchmark_symbol: string;
  start_date: string;
  end_date: string;
  status: string;
  created_at: string;
};

const PAGE_SIZE = 20;
// The backend accepts offsets up to 10000.
const MAX_PAGE = Math.floor(10_000 / PAGE_SIZE) + 1;

const KIND_BY_STRATEGY: Record<string, string> = {
  portfolio_snapshot_backtest: "Portfolio",
  ai_recommendation_replay: "Recommendation replay",
};

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function BacktestsPage({ searchParams }: { searchParams: SearchParams }) {
  const page = Math.min(pageParam((await searchParams).page), MAX_PAGE);
  const { accessToken } = await getMe();
  // One extra row tells whether a next page exists; the list has no total.
  const res = await backendFetch(
    `/backtests?limit=${PAGE_SIZE + 1}&offset=${(page - 1) * PAGE_SIZE}`,
    accessToken,
    { cache: "no-store" }
  );
  requireOk(res, "Failed to load backtests");
  const { data }: { data: Backtest[] } = await res.json();
  const backtests = data.slice(0, PAGE_SIZE);
  const hasNext = data.length > PAGE_SIZE && page < MAX_PAGE;

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Workspace</p>
        <h1 className="page-title">Backtests</h1>
        <p className="page-subtitle">
          Your backtests, newest first. Start a new one from a portfolio&apos;s Backtest section.
        </p>
      </div>

      <div className="section">
        {backtests.length === 0 ? (
          <p className="empty-state">
            {page > 1 ? "No more backtests." : "You haven't run any backtests yet."}{" "}
            <Link href="/portfolios" className="link-btn">
              Go to portfolios
            </Link>
          </p>
        ) : (
          <div className="row-list">
            {backtests.map((b) => (
              <Link
                key={b.id}
                href={`/backtests/${b.id}`}
                className="row-item min-w-0"
                style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 12 }}
              >
                <div className="min-w-0">
                  <span className="row-title">
                    {KIND_BY_STRATEGY[b.strategy_name] ?? `Ticker strategy "${b.strategy_name}"`} vs{" "}
                    {b.benchmark_symbol}
                  </span>
                  <div className="row-meta">
                    {b.start_date} to {b.end_date} · started <LocalTime iso={b.created_at} />
                  </div>
                </div>
                <span className={statusPillClass(b.status)}>{b.status}</span>
              </Link>
            ))}
          </div>
        )}
        {(page > 1 || hasNext) && (
          <nav aria-label="Pagination" className="flex items-center gap-3 text-sm mt-4">
            {page > 1 ? (
              <Link href={`/backtests?page=${page - 1}`} className="link-btn" rel="prev">
                Previous
              </Link>
            ) : (
              <span className="text-[var(--text-faint)]">Previous</span>
            )}
            <span className="text-[var(--text-secondary)]">Page {page}</span>
            {hasNext ? (
              <Link href={`/backtests?page=${page + 1}`} className="link-btn" rel="next">
                Next
              </Link>
            ) : (
              <span className="text-[var(--text-faint)]">Next</span>
            )}
          </nav>
        )}
      </div>
    </div>
  );
}
