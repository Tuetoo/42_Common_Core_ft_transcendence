"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatMetric, type Metric, type TimeseriesPoint } from "@/app/lib/format";

// Chart colors are hex, not CSS variables: Recharts writes them into SVG
// attributes, where var() isn't reliably supported. SERIES is the single
// data color (validated against the --bg-raised card surface); the rest
// mirror the page's text and border tokens so the chrome stays recessive.
const SERIES = "#3987e5";
const SURFACE = "#12151b";
const GRID = "#232833";
const MUTED_TEXT = "#8b94a3";

// Analysis outcomes use the page's status colors: --up (completed),
// --down (failed), --accent (running) and --text-secondary (pending).
export const STATUS_COLORS: Record<string, string> = {
  completed: "#35c46b",
  failed: "#e5484d",
  running: "#e0a83d",
  pending: "#8b94a3",
};

function shortDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

type TooltipProps = {
  active?: boolean;
  label?: string | number;
  payload?: readonly { value?: unknown; payload?: TimeseriesPoint }[];
};

function TimeseriesTooltip({ active, label, payload, metric }: TooltipProps & { metric: Metric }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-label">{shortDate(String(label))}</div>
      <div>{formatMetric(metric, point?.value ?? null)}</div>
      {metric !== "analyses" && <div className="chart-tooltip-label">from {point?.count ?? 0} analyses</div>}
    </div>
  );
}

function DistributionTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload as unknown as { action: string; count: number };
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-label">{row.action}</div>
      <div>
        {row.count.toLocaleString("en-US")} {row.count === 1 ? "recommendation" : "recommendations"}
      </div>
    </div>
  );
}

export function TimeseriesChart({ points, metric }: { points: TimeseriesPoint[]; metric: Metric }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={points} margin={{ top: 12, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={GRID} />
        <XAxis
          dataKey="period_start"
          tickFormatter={shortDate}
          stroke={GRID}
          tick={{ fill: MUTED_TEXT, fontSize: 11 }}
          minTickGap={24}
        />
        <YAxis
          width={44}
          allowDecimals={metric !== "analyses"}
          domain={metric === "analyses" ? [0, "auto"] : [0, 1]}
          tickFormatter={(v: number) => formatMetric(metric, v)}
          stroke={GRID}
          tick={{ fill: MUTED_TEXT, fontSize: 11 }}
        />
        <Tooltip
          content={(props) => <TimeseriesTooltip {...(props as TooltipProps)} metric={metric} />}
          cursor={{ stroke: MUTED_TEXT, strokeWidth: 1 }}
        />
        <Area
          type="linear"
          dataKey="value"
          stroke={SERIES}
          strokeWidth={2}
          fill={SERIES}
          fillOpacity={0.1}
          // Days with no data stay as gaps instead of being drawn as zero.
          connectNulls={false}
          dot={false}
          activeDot={{ r: 5, fill: SERIES, stroke: SURFACE, strokeWidth: 2 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function ActionDistributionChart({ distribution }: { distribution: { action: string; count: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={40 * distribution.length + 24}>
      <BarChart data={distribution} layout="vertical" margin={{ top: 4, right: 40, bottom: 4, left: 0 }}>
        <XAxis type="number" hide allowDecimals={false} />
        <YAxis
          type="category"
          dataKey="action"
          width={48}
          axisLine={false}
          tickLine={false}
          // Recharts draws neither a bar nor its label for a zero, so the
          // axis tick writes that "0" at the baseline where the bar would end.
          tick={(props) => {
            const x = Number(props.x);
            const y = Number(props.y);
            const action = String(props.payload.value);
            const count = distribution.find((d) => d.action === action)?.count ?? 0;
            return (
              <g>
                <text x={x} y={y} dy={4} textAnchor="end" fill={MUTED_TEXT} fontSize={12}>
                  {action}
                </text>
                {count === 0 && (
                  <text x={x + 14} y={y} dy={4} fill={MUTED_TEXT} fontSize={12}>
                    0
                  </text>
                )}
              </g>
            );
          }}
        />
        <Tooltip
          content={(props) => <DistributionTooltip {...(props as TooltipProps)} />}
          cursor={{ fill: "rgba(255,255,255,0.04)" }}
        />
        <Bar dataKey="count" fill={SERIES} barSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}>
          <LabelList dataKey="count" position="right" fill={MUTED_TEXT} fontSize={12} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

type StatusSlice = { status: string; count: number };

function StatusTooltip({ active, payload, total }: TooltipProps & { total: number }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload as unknown as StatusSlice;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-label">{row.status}</div>
      <div>
        {row.count.toLocaleString("en-US")} {row.count === 1 ? "analysis" : "analyses"} (
        {Math.round((row.count / total) * 100)}%)
      </div>
    </div>
  );
}

// Donut of analyses by status. Statuses with no analyses are left out of
// the ring (a zero slice can't be seen or hovered); the page's data table
// still lists them.
export function StatusDonutChart({ slices }: { slices: StatusSlice[] }) {
  const data = slices
    .filter((s) => s.count > 0)
    .map((s) => ({ ...s, fill: STATUS_COLORS[s.status] ?? SERIES }));
  const total = data.reduce((sum, s) => sum + s.count, 0);
  return (
    <ResponsiveContainer width="100%" height={240}>
      <PieChart margin={{ top: 4, right: 4, bottom: 4, left: 4 }}>
        <Pie
          data={data}
          dataKey="count"
          nameKey="status"
          innerRadius="55%"
          outerRadius="85%"
          paddingAngle={data.length > 1 ? 2 : 0}
          stroke={SURFACE}
          strokeWidth={2}
          isAnimationActive={false}
        />
        <Tooltip content={(props) => <StatusTooltip {...(props as TooltipProps)} total={total} />} />
        <Legend
          verticalAlign="bottom"
          iconType="circle"
          iconSize={8}
          formatter={(value: string, entry) => {
            const count = (entry.payload as unknown as StatusSlice | undefined)?.count ?? 0;
            return (
              <span style={{ color: MUTED_TEXT, fontSize: 12 }}>
                {value} ({count.toLocaleString("en-US")})
              </span>
            );
          }}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
