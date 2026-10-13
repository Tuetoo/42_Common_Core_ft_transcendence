"use server";

import { redirect } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, readApiError } from "@/app/lib/backend";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const BENCHMARK_RE = /^[A-Za-z0-9.\-^]{1,10}$/;

export type RunBacktestState = {
  error?: string;
  // The submitted values, so the form keeps them after an error.
  values?: Record<string, string>;
};

export async function runPortfolioBacktest(
  portfolioId: string,
  _prevState: RunBacktestState,
  formData: FormData
): Promise<RunBacktestState> {
  const text = (name: string) => {
    const value = formData.get(name);
    return typeof value === "string" ? value.trim() : "";
  };
  const values = {
    benchmark_symbol: text("benchmark_symbol"),
    start_date: text("start_date"),
    end_date: text("end_date"),
  };

  const benchmark = (values.benchmark_symbol || "SPY").toUpperCase();
  if (!BENCHMARK_RE.test(benchmark)) {
    return { error: "Benchmark must be a ticker of 1-10 characters, e.g. SPY", values };
  }
  for (const key of ["start_date", "end_date"] as const) {
    if (values[key] && !DATE_RE.test(values[key])) {
      return { error: "Dates must be in the form YYYY-MM-DD", values };
    }
  }
  if (values.start_date && values.end_date && values.start_date > values.end_date) {
    return { error: "The start date must be on or before the end date", values };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in", values };
  }

  let res: Response;
  try {
    res = await backendFetch("/backtests/portfolio", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        portfolio_id: portfolioId,
        benchmark_symbol: benchmark,
        start_date: values.start_date || null,
        end_date: values.end_date || null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?", values };
  }

  if (!res.ok) {
    const err = await readApiError(res, "Could not start the backtest");
    return { error: err.message, values };
  }

  const { data } = await res.json();
  // The backtest runs in the background; its page refreshes until it ends.
  redirect(`/backtests/${data.id}`);
}
