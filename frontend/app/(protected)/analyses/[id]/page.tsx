import Link from "next/link";
import { notFound } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { formatNumber } from "@/app/lib/format";
import { isFinished, statusPillClass } from "../status";
import { LiveStatus } from "./live-status";
import { DecisionForm } from "./decision-form";

type AgentRun = {
  id: string;
  agent_name: string;
  status: string;
  duration_ms: number | null;
  result: { score: number | null; summary: string | null } | null;
};

type DebateArgument = {
  round_number: number;
  side: string;
  content: string;
  citations: string[] | null;
};

type Reflection = {
  id: string;
  round_number: number;
  verified: boolean;
  issues: string[] | null;
  retry_agents: Record<string, string> | null;
};

type Tier = {
  id: string;
  side: string;
  target_price: string;
  quantity: string;
  status: string;
  window_expires_at: string | null;
};

type Recommendation = {
  recommendation: string;
  confidence: number;
  reasoning: string;
  confidence_adjustment_note: string | null;
  tier_generation_note: string | null;
  debate_converged: boolean;
  accepted_at: string | null;
  tiers: Tier[];
};

type AnalysisDetail = {
  id: string;
  ticker: string;
  status: string;
  portfolio_id: string | null;
  created_at: string;
  failure_reason: string | null;
  agent_runs: AgentRun[];
  debate: { arguments: DebateArgument[] } | null;
  reflections: Reflection[];
  recommendation: Recommendation | null;
};

function actionPillClass(action: string): string {
  if (action === "buy") return "pill pill-up";
  if (action === "sell") return "pill pill-down";
  return "pill pill-accent";
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

export default async function AnalysisDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const accessToken = await getAccessToken();

  const res = await backendFetch(`/analyses/${id}`, accessToken!, { cache: "no-store" });
  // Someone else's analysis is "forbidden", a wrong id is "not found" — to
  // the user both just mean there's nothing here to see.
  if (res.status === 404 || res.status === 403 || res.status === 422) {
    notFound();
  }
  requireOk(res, "Failed to load analysis");

  const { data: analysis }: { data: AnalysisDetail } = await res.json();
  const rec = analysis.recommendation;

  // Group the flat debate transcript into rounds: round 1 → [bull, bear], ...
  const rounds = new Map<number, DebateArgument[]>();
  for (const arg of analysis.debate?.arguments ?? []) {
    rounds.set(arg.round_number, [...(rounds.get(arg.round_number) ?? []), arg]);
  }

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">
          <Link href="/analyses" className="link-btn" style={{ padding: 0 }}>
            Analyses
          </Link>{" "}
          / Analysis
        </p>
        <h1 className="page-title flex items-center gap-3">
          <span className="mono">{analysis.ticker}</span>
          <span className={statusPillClass(analysis.status)}>{analysis.status}</span>
        </h1>
        <p className="page-subtitle">Started {new Date(analysis.created_at).toLocaleString()}</p>
      </div>

      {!isFinished(analysis.status) && <LiveStatus analysisId={analysis.id} accessToken={accessToken!} />}

      {analysis.status === "failed" && (
        <div className="panel" style={{ fontSize: 13 }}>
          <p className="form-message error" style={{ margin: 0 }}>
            This analysis did not finish
            {analysis.failure_reason ? ` (${analysis.failure_reason.replaceAll("_", " ")})` : ""}. You can start a new
            one from the Analyses page.
          </p>
        </div>
      )}

      {rec && (
        <div className="section">
          <h2 className="section-title">Recommendation</h2>
          <div className="panel flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <span className={actionPillClass(rec.recommendation)} style={{ fontSize: 13, padding: "4px 12px" }}>
                {rec.recommendation}
              </span>
              <span className="mono" style={{ fontSize: 13, color: "var(--text-secondary)" }}>
                {percent(rec.confidence)} confidence
              </span>
            </div>
            <p style={{ margin: 0, fontSize: 13, whiteSpace: "pre-wrap" }}>{rec.reasoning}</p>
            {rec.confidence_adjustment_note && (
              <p style={{ margin: 0, fontSize: 12.5, color: "var(--text-faint)" }}>{rec.confidence_adjustment_note}</p>
            )}
            <p style={{ margin: 0, fontSize: 12.5, color: "var(--text-faint)" }}>
              The debate {rec.debate_converged ? "reached agreement" : "ended without full agreement"}.
            </p>
          </div>
        </div>
      )}

      {rec && (rec.tiers.length > 0 || rec.tier_generation_note) && (
        <div className="section">
          <h2 className="section-title">Price tiers</h2>
          {rec.tier_generation_note && (
            <p style={{ fontSize: 12.5, color: "var(--text-faint)", margin: 0 }}>{rec.tier_generation_note}</p>
          )}
          {rec.tiers.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Side</th>
                    <th>Target price</th>
                    <th>Quantity</th>
                    <th>Status</th>
                    <th>Window ends</th>
                  </tr>
                </thead>
                <tbody>
                  {rec.tiers.map((t) => (
                    <tr key={t.id}>
                      <td>
                        <span className={actionPillClass(t.side)}>{t.side}</span>
                      </td>
                      <td className="mono">{formatNumber(t.target_price, 2)}</td>
                      <td className="mono">{formatNumber(t.quantity)}</td>
                      <td className="mono">{t.status.replaceAll("_", " ")}</td>
                      <td className="mono">
                        {t.window_expires_at ? new Date(t.window_expires_at).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {rec && (
        <div className="section">
          <h2 className="section-title">Your decision</h2>
          {rec.accepted_at && (
            <p style={{ fontSize: 12.5, color: "var(--text-faint)", margin: 0 }}>
              You accepted this recommendation on {new Date(rec.accepted_at).toLocaleString()}.
            </p>
          )}
          <DecisionForm
            analysisId={analysis.id}
            portfolioId={analysis.portfolio_id}
            hasTiers={rec.tiers.length > 0}
          />
        </div>
      )}

      {analysis.agent_runs.length > 0 && (
        <div className="section">
          <h2 className="section-title">Research agents</h2>
          <div className="row-list">
            {analysis.agent_runs.map((run) => (
              <div key={run.id} className="row-item">
                <div className="flex items-center justify-between gap-3">
                  <span className="row-title mono" style={{ textTransform: "uppercase" }}>
                    {run.agent_name}
                  </span>
                  <span className="flex items-center gap-2">
                    <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>
                      evidence {percent(run.result?.score)}
                      {run.duration_ms !== null && ` · ${(run.duration_ms / 1000).toFixed(1)}s`}
                    </span>
                    <span className={statusPillClass(run.status)}>{run.status}</span>
                  </span>
                </div>
                {run.result?.summary && <div className="row-meta">{run.result.summary}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {rounds.size > 0 && (
        <div className="section">
          <h2 className="section-title">Debate</h2>
          <div className="flex flex-col gap-3">
            {[...rounds.entries()].map(([round, args]) => (
              <div key={round} className="panel flex flex-col gap-2">
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>
                  ROUND {round}
                </div>
                {args.map((arg, i) => (
                  <p key={i} style={{ margin: 0, fontSize: 13, whiteSpace: "pre-wrap" }}>
                    <strong style={{ color: arg.side === "bull" ? "var(--up)" : "var(--down)" }}>
                      {arg.side === "bull" ? "Bull" : "Bear"}:
                    </strong>{" "}
                    {arg.content}
                  </p>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {analysis.reflections.length > 0 && (
        <div className="section">
          <h2 className="section-title">Reflection</h2>
          <div className="row-list">
            {analysis.reflections.map((r) => (
              <div key={r.id} className="row-item">
                <div className="flex items-center justify-between gap-3">
                  <span className="row-title">Check {r.round_number}</span>
                  <span className={r.verified ? "pill pill-up" : "pill pill-accent"}>
                    {r.verified ? "verified" : "issues found"}
                  </span>
                </div>
                {/* REVIEW_META lines are the backend's own bookkeeping, not issues. */}
                {r.issues
                  ?.filter((issue) => !issue.startsWith("REVIEW_META:"))
                  .map((issue, i) => (
                  <div key={i} className="row-meta">
                    • {issue}
                  </div>
                ))}
                {r.retry_agents &&
                  Object.entries(r.retry_agents).map(([agent, reason]) => (
                    <div key={agent} className="row-meta">
                      Re-ran <span className="mono">{agent}</span>: {reason}
                    </div>
                  ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
