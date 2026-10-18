import Link from "next/link";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { formatNumber, isFinished, statusPillClass } from "@/app/lib/format";
import { getMe } from "@/app/lib/session";
import { LocalTime } from "@/app/(protected)/local-time";
import { RefreshWhileRunning } from "./refresh-while-running";

const PORTFOLIO_SNAPSHOT_STRATEGY = "portfolio_snapshot_backtest";

type Backtest = {
  id: string;
  strategy_name: string;
  benchmark_symbol: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  ending_capital: number | null;
  strategy_return: number | null;
  benchmark_return: number | null;
  status: string;
  created_at: string;
  metrics: {
    sharpe_ratio: number | null;
    sortino_ratio: number | null;
    drawdown: number | null;
    win_rate: number | null;
  } | null;
  benchmark_result: {
    alpha: number | null;
    beta: number | null;
    information_ratio: number | null;
    tracking_error: number | null;
    outperformed: boolean | null;
  } | null;
};

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`;
}

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

export default async function BacktestPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { accessToken } = await getMe();
  const res = await backendFetch(`/backtests/${encodeURIComponent(id)}`, accessToken, { cache: "no-store" });
  requireOk(res, "Failed to load backtest");
  const { data: b }: { data: Backtest } = await res.json();
  const finished = isFinished(b.status);
  const result = b.benchmark_result;
  const isPortfolio = b.strategy_name === PORTFOLIO_SNAPSHOT_STRATEGY;
  const subject = isPortfolio ? "Portfolio" : b.strategy_name;

  return (
    <div className="page">
      {!finished && <RefreshWhileRunning />}
      <div>
        <p className="page-eyebrow">
          <Link href="/backtests" className="link-btn" style={{ padding: 0 }}>
            Backtests
          </Link>{" "}
          / Backtest
        </p>
        <h1 className="page-title">{subject} vs {b.benchmark_symbol}</h1>
        <p className="page-subtitle">
          {b.start_date} to {b.end_date} · started <LocalTime iso={b.created_at} />{" "}
          <span className={statusPillClass(b.status)}>{b.status}</span>
        </p>
      </div>

      {!finished && <p className="empty-state">Running… this page updates by itself.</p>}
      {b.status === "failed" && (
        <p className="form-message error">The backtest failed. The reason is in your notifications.</p>
      )}

      {b.status === "completed" && (
        <>
          <div className="stat-grid">
            <div className="stat-tile">
              <span className="stat-label">{isPortfolio ? "Portfolio return" : "Strategy return"}</span>
              <span className="stat-value">{percent(b.strategy_return)}</span>
              <span className="stat-note">
                {formatNumber(b.initial_capital, 2)} → {formatNumber(b.ending_capital, 2)}
              </span>
            </div>
            <div className="stat-tile">
              <span className="stat-label">{b.benchmark_symbol} return</span>
              <span className="stat-value">{percent(b.benchmark_return)}</span>
              {result?.outperformed !== null && result?.outperformed !== undefined && (
                <span className="stat-note">{result.outperformed ? `${isPortfolio ? "Portfolio" : "Strategy"} beat the benchmark` : "Benchmark did better"}</span>
              )}
            </div>
            <div className="stat-tile">
              <span className="stat-label">Max drawdown</span>
              <span className="stat-value">{percent(b.metrics?.drawdown)}</span>
              <span className="stat-note">Largest peak-to-trough decline</span>
            </div>
            <div className="stat-tile">
              <span className="stat-label">Sharpe ratio</span>
              <span className="stat-value">{ratio(b.metrics?.sharpe_ratio)}</span>
              <span className="stat-note">Annualized return per unit of volatility</span>
            </div>
          </div>

          <div className="section">
            <h2 className="section-title">All metrics</h2>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Metric</th>
                    <th scope="col">Value</th>
                    <th scope="col">What it means</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Sortino ratio</td>
                    <td className="mono">{ratio(b.metrics?.sortino_ratio)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Annualized return per unit of downside volatility</td>
                  </tr>
                  <tr>
                    <td>Win rate</td>
                    <td className="mono">{percent(b.metrics?.win_rate)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Percentage of closed trades with a positive return</td>
                  </tr>
                  <tr>
                    <td>Alpha (per year)</td>
                    <td className="mono">{percent(result?.alpha)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Annualized return not explained by the benchmark (Jensen&apos;s alpha)</td>
                  </tr>
                  <tr>
                    <td>Beta</td>
                    <td className="mono">{ratio(result?.beta)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Sensitivity of the portfolio&apos;s returns to the benchmark&apos;s</td>
                  </tr>
                  <tr>
                    <td>Information ratio</td>
                    <td className="mono">{ratio(result?.information_ratio)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Active return divided by tracking error</td>
                  </tr>
                  <tr>
                    <td>Tracking error (per year)</td>
                    <td className="mono">{percent(result?.tracking_error)}</td>
                    <td className="text-[12px] text-[var(--text-faint)]">Annualized volatility of the return difference from the benchmark</td>
                  </tr>
                </tbody>
              </table>
            </div>
            {isPortfolio && (
              <p className="stat-note">
                Deposits and withdrawals are left out, so the return measures the investing itself (time-weighted).
                Win rate is empty for a portfolio, which has no separate trades.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
