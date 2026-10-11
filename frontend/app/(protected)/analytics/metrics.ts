// Plain module (no "use client") so both the server page and the client
// charts can call these. Anything exported from a "use client" file becomes
// a client reference that the server can render but never call.
export type Metric = "analyses" | "confidence" | "accuracy";

export type TimeseriesPoint = { period_start: string; value: number | null; count: number };

export function formatMetric(metric: Metric, value: number | null): string {
  if (value === null) return "—";
  return metric === "analyses" ? value.toLocaleString("en-US") : `${Math.round(value * 100)}%`;
}
