"use client";

import Link from "next/link";
import { useActionState } from "react";
import { runPortfolioBacktest, type RunBacktestState } from "@/app/lib/actions/backtests";

const initialState: RunBacktestState = {};

export function BacktestForm({ portfolioId }: { portfolioId: string }) {
  const [state, formAction, pending] = useActionState(runPortfolioBacktest.bind(null, portfolioId), initialState);
  const values = state.values ?? {};

  return (
    <form action={formAction} className="form">
      <div className="field">
        <label htmlFor="benchmark_symbol">Benchmark</label>
        <input
          id="benchmark_symbol"
          name="benchmark_symbol"
          type="text"
          maxLength={10}
          autoCapitalize="characters"
          autoComplete="off"
          placeholder="SPY"
          defaultValue={values.benchmark_symbol}
        />
      </div>
      <div className="field">
        <label htmlFor="start_date">Start date (optional)</label>
        <input id="start_date" name="start_date" type="date" autoComplete="off" defaultValue={values.start_date} />
      </div>
      <div className="field">
        <label htmlFor="end_date">End date (optional)</label>
        <input id="end_date" name="end_date" type="date" autoComplete="off" defaultValue={values.end_date} />
      </div>
      <p className="field-hint">
        Replays this portfolio&apos;s daily values against the benchmark. Without dates the whole history is used.{" "}
        <Link href="/backtests" className="link-btn">
          Your past backtests
        </Link>
      </p>
      {state?.error && (
        <p aria-live="polite" className="form-message error">
          {state.error}
        </p>
      )}
      <button type="submit" disabled={pending} className="btn btn-primary">
        {pending ? "Starting…" : "Run backtest"}
      </button>
    </form>
  );
}
