import Form from "next/form";
import { backendFetch, readApiError, requireOk } from "@/app/lib/backend";
import { getMe } from "@/app/lib/session";
import { TICKER_PATTERN, TICKER_TITLE, formatMetric, type Metric, type TimeseriesPoint } from "@/app/lib/format";
import { ActionDistributionChart, StatusDonutChart, TimeseriesChart } from "./charts";
import { LiveRefresh } from "./live-refresh";
import { firstParam } from "../pager";
import { PrintButton } from "./print-button";

type Accuracy = { evaluated: number; rate: number | null };

type Overview = {
  range: { from: string; to: string };
  total_analyses: number;
  status_counts: Record<string, number>;
  recommendations: number;
  action_distribution: Record<string, number>;
  average_confidence: number | null;
  accuracy: Accuracy;
  by_agent: {
    agent: string;
    runs: number;
    completed: number;
    failed: number;
    success_rate: number | null;
    avg_score: number | null;
    avg_duration_ms: number | null;
  }[];
  by_ticker: {
    ticker: string;
    analyses: number;
    completed: number;
    failed: number;
    actions: Record<string, number>;
    average_confidence: number | null;
    accuracy: Accuracy;
  }[];
};

const METRICS: { value: Metric; label: string }[] = [
  { value: "analyses", label: "Analyses run" },
  { value: "confidence", label: "Average confidence" },
  { value: "accuracy", label: "Accuracy" },
];
const ACTIONS = ["buy", "hold", "sell"];
// shared/enums.py WorkflowStatus, in the order the donut and table use.
const STATUSES = ["completed", "failed", "running", "pending"];

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

function percent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

// When this render fetched its data; the live bar shows it as "last updated".
function renderedNow(): string {
  return new Date().toISOString();
}

export default async function AnalyticsPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  // The URL is user input: keep only values the backend understands.
  const filters = {
    from: firstParam(params.from),
    to: firstParam(params.to),
    ticker: firstParam(params.ticker).toUpperCase(),
    action: ACTIONS.includes(firstParam(params.action)) ? firstParam(params.action) : "",
    status: STATUSES.includes(firstParam(params.status)) ? firstParam(params.status) : "",
  };
  // Set by the CSV export route when the backend refused the export.
  const exportError = firstParam(params.export_error).slice(0, 300);
  const metric: Metric = METRICS.some((m) => m.value === firstParam(params.metric))
    ? (firstParam(params.metric) as Metric)
    : "analyses";
  const interval = firstParam(params.interval) === "week" ? "week" : "day";

  const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== ""));
  const { accessToken } = await getMe();
  const [overviewRes, seriesRes] = await Promise.all([
    backendFetch(`/analytics/overview?${query}`, accessToken, { cache: "no-store" }),
    backendFetch(`/analytics/timeseries?${query}&metric=${metric}&interval=${interval}`, accessToken, {
      cache: "no-store",
    }),
  ]);

  // A bad date range comes back as 422 — show it next to the filters
  // rather than failing the whole page.
  const filterError =
    overviewRes.status === 422 ? (await readApiError(overviewRes, "Those filters aren't valid")).message : null;

  let overview: Overview | null = null;
  let points: TimeseriesPoint[] = [];
  if (!filterError) {
    requireOk(overviewRes, "Failed to load analytics overview");
    requireOk(seriesRes, "Failed to load analytics time series");
    overview = (await overviewRes.json()).data;
    points = (await seriesRes.json()).data.points;
  }

  const distribution = ACTIONS.map((action) => ({ action, count: overview?.action_distribution[action] ?? 0 }));
  const statusSlices = STATUSES.map((status) => ({ status, count: overview?.status_counts[status] ?? 0 }));
  const exportQuery = (dataset: string) => `/analytics/export?${new URLSearchParams({ dataset, ...Object.fromEntries(query) })}`;
  const metricLabel = METRICS.find((m) => m.value === metric)!.label;

  return (
    <div className="page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="page-eyebrow">Workspace</p>
          <h1 className="page-title">Analytics</h1>
          <p className="page-subtitle">
            How your AI analyses have performed
            {overview && ` · ${overview.range.from} to ${overview.range.to}`}
          </p>
          <div className="print:hidden mt-2">
            <LiveRefresh renderedAt={renderedNow()} />
          </div>
        </div>
        <div className="flex gap-2 print:hidden">
          <PrintButton />
        </div>
      </div>

      {/* One filter row scopes every number and chart below it. */}
      <Form action="/analytics" className="filter-row print:hidden">
        <div className="field">
          <label htmlFor="from">From</label>
          <input id="from" name="from" autoComplete="off" type="date" defaultValue={filters.from || overview?.range.from} />
        </div>
        <div className="field">
          <label htmlFor="to">To</label>
          <input id="to" name="to" autoComplete="off" type="date" defaultValue={filters.to || overview?.range.to} />
        </div>
        <div className="field">
          <label htmlFor="ticker">Ticker</label>
          <input
            id="ticker"
            name="ticker"
            autoComplete="off"
            type="text"
            maxLength={10}
            pattern={TICKER_PATTERN}
            title={TICKER_TITLE}
            autoCapitalize="characters"
            placeholder="All"
            defaultValue={filters.ticker}
          />
        </div>
        <div className="field">
          <label htmlFor="action">Recommendation</label>
          <select id="action" name="action" autoComplete="off" defaultValue={filters.action}>
            <option value="">All</option>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="status">Status</label>
          <select id="status" name="status" autoComplete="off" defaultValue={filters.status}>
            <option value="">All</option>
            {STATUSES.map((st) => (
              <option key={st} value={st}>
                {st}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="metric">Chart</label>
          <select id="metric" name="metric" autoComplete="off" defaultValue={metric}>
            {METRICS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="interval">Per</label>
          <select id="interval" name="interval" autoComplete="off" defaultValue={interval}>
            <option value="day">day</option>
            <option value="week">week</option>
          </select>
        </div>
        <button type="submit" className="btn btn-primary">
          Apply
        </button>
      </Form>

      {filterError && <p className="form-message error">{filterError}</p>}
      {exportError && (
        <p role="alert" className="form-message error print:hidden">
          CSV export failed: {exportError}
        </p>
      )}

      {overview && (
        <>
          <div className="stat-grid">
            <div className="stat-tile">
              <span className="stat-label">Analyses</span>
              <span className="stat-value">{overview.total_analyses.toLocaleString("en-US")}</span>
              <span className="stat-note">
                {overview.status_counts.completed ?? 0} completed · {overview.status_counts.failed ?? 0} failed
              </span>
            </div>
            <div className="stat-tile">
              <span className="stat-label">Recommendations</span>
              <span className="stat-value">{overview.recommendations.toLocaleString("en-US")}</span>
            </div>
            <div className="stat-tile">
              <span className="stat-label">Average confidence</span>
              <span className="stat-value">{percent(overview.average_confidence)}</span>
            </div>
            <div className="stat-tile">
              <span className="stat-label">Accuracy</span>
              <span className="stat-value">{percent(overview.accuracy.rate)}</span>
              <span className="stat-note">
                {overview.accuracy.evaluated > 0
                  ? `${overview.accuracy.evaluated} recommendations scored`
                  : "Scored 30 days after each recommendation"}
              </span>
            </div>
          </div>

          <div className="section">
            <h2 className="section-title">
              {metricLabel} per {interval}
            </h2>
            <div className="panel">
              <TimeseriesChart points={points} metric={metric} />
            </div>
            <details className="print:hidden">
              <summary className="link-btn" style={{ padding: 0 }}>
                Show data table
              </summary>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{interval === "week" ? "Week of" : "Day"}</th>
                      <th>{metricLabel}</th>
                      {metric !== "analyses" && <th>Analyses</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {points.map((p) => (
                      <tr key={p.period_start}>
                        <td className="mono">{p.period_start}</td>
                        <td className="mono">{formatMetric(metric, p.value)}</td>
                        {metric !== "analyses" && <td className="mono">{p.count}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </div>

          <div className="section">
            <h2 className="section-title" id="status-chart-title">
              Analyses by status
            </h2>
            {overview.total_analyses === 0 ? (
              <p className="empty-state">No analyses in this range.</p>
            ) : (
              <figure className="panel" aria-labelledby="status-chart-title" style={{ margin: 0 }}>
                {/* The drawing is for sighted users; the table says the same to screen readers. */}
                <div aria-hidden="true">
                  <StatusDonutChart slices={statusSlices} />
                </div>
                <table className="sr-only">
                  <caption>Analyses by status, {overview.total_analyses} in total</caption>
                  <thead>
                    <tr>
                      <th scope="col">Status</th>
                      <th scope="col">Analyses</th>
                      <th scope="col">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {statusSlices.map((s) => (
                      <tr key={s.status}>
                        <th scope="row">{s.status}</th>
                        <td>{s.count}</td>
                        <td>{Math.round((s.count / overview.total_analyses) * 100)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </figure>
            )}
          </div>

          <div className="section">
            <h2 className="section-title">Recommendations by action</h2>
            {overview.recommendations === 0 ? (
              <p className="empty-state">No recommendations in this range.</p>
            ) : (
              <div className="panel">
                <ActionDistributionChart distribution={distribution} />
              </div>
            )}
          </div>

          <div className="section">
            <h2 className="section-title">Agents</h2>
            {overview.by_agent.length === 0 ? (
              <p className="empty-state">No agent runs in this range.</p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th>Runs</th>
                      <th>Success rate</th>
                      <th>Avg evidence score</th>
                      <th>Avg time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.by_agent.map((a) => (
                      <tr key={a.agent}>
                        <td className="mono" style={{ textTransform: "uppercase" }}>
                          {a.agent}
                        </td>
                        <td className="mono">{a.runs}</td>
                        <td className="mono">{percent(a.success_rate)}</td>
                        <td className="mono">{percent(a.avg_score)}</td>
                        <td className="mono">
                          {a.avg_duration_ms === null ? "—" : `${(a.avg_duration_ms / 1000).toFixed(1)}s`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="section">
            <h2 className="section-title">Tickers</h2>
            {overview.by_ticker.length === 0 ? (
              <p className="empty-state">No tickers analyzed in this range.</p>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Ticker</th>
                      <th>Analyses</th>
                      <th>Buy / Hold / Sell</th>
                      <th>Avg confidence</th>
                      <th>Accuracy</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.by_ticker.map((t) => (
                      <tr key={t.ticker}>
                        <td className="mono">{t.ticker}</td>
                        <td className="mono">
                          {t.analyses} ({t.failed} failed)
                        </td>
                        <td className="mono">
                          {t.actions.buy ?? 0} / {t.actions.hold ?? 0} / {t.actions.sell ?? 0}
                        </td>
                        <td className="mono">{percent(t.average_confidence)}</td>
                        <td className="mono">{percent(t.accuracy.rate)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="section print:hidden">
            <h2 className="section-title">Export as CSV</h2>
            <p style={{ fontSize: 12.5, color: "var(--text-faint)", margin: 0 }}>
              Uses the same date range and filters as above.
            </p>
            <div className="flex gap-2 flex-wrap">
              {/* Plain <a>, not <Link>: these are file downloads, not pages. */}
              <a className="btn btn-ghost" href={exportQuery("analyses")}>
                Analyses
              </a>
              <a className="btn btn-ghost" href={exportQuery("recommendations")}>
                Recommendations
              </a>
              <a className="btn btn-ghost" href={exportQuery("decisions")}>
                Decisions
              </a>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
