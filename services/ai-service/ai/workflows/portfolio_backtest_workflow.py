from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
import structlog
from ai.providers.registry import get_market_data_provider
from ai.workflows.backtest_workflow import (backtest_error_text,
                                            closes_by_date,
                                            compute_backtest_metrics,
                                            load_backtest_and_mark_running,
                                            mark_backtest_failed,
                                            persist_backtest_result,
                                            publish_backtest_finished)
from clients.core_client import (get_portfolio_cash_flows,
                                 get_portfolio_snapshots)
from shared.enums import TransactionType

logger = structlog.get_logger(__name__)


def _is_adjustment(flow: Mapping[str, Any]) -> bool:
    return (
        str(flow.get("transaction_type")
            or "").lower() == TransactionType.ADJUSTMENT.value)


def _adjustment_ticker(flow: Mapping[str, Any]) -> str | None:
    ticker = flow.get("ticker") or flow.get("symbol")
    return str(ticker).strip().upper() if ticker else None


def _flow_date(flow: Mapping[str, Any]) -> date:
    if flow.get("snapshot_date"):
        return date.fromisoformat(str(flow["snapshot_date"]))
    return datetime.fromisoformat(str(flow["transaction_date"])).date()


def _close_on_or_before(closes: dict[date, float], day: date) -> float | None:
    candidates = [d for d in closes if d <= day]
    return closes[max(candidates)] if candidates else None


async def _adjustment_closes(
        cash_flows: list[dict[str, Any]], *, start_date: date,
        end_date: date) -> dict[str, dict[date, float]]:
    tickers = sorted({
        ticker
        for flow in cash_flows
        if _is_adjustment(flow) and (ticker := _adjustment_ticker(flow))
    })
    if not tickers:
        return {}
    provider = get_market_data_provider()
    out: dict[str, dict[date, float]] = {}
    for ticker in tickers:
        try:
            bars = await provider.get_price_history_range(
                ticker, start=start_date - timedelta(days=10), end=end_date)
        except Exception as exc:
            logger.warning(
                "portfolio_backtest.adjustment_price_lookup_failed",
                ticker=ticker, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            continue
        out[ticker] = closes_by_date(
            bars, ticker=ticker,
            event="portfolio_backtest.dropped_non_finite_adjustment_bars")
    return out


def _net_flow_by_date(
    cash_flows: list[dict[str, Any]],
    adjustment_closes: dict[str, dict[date, float]] | None = None
) -> dict[date, float]:
    closes_by_ticker = adjustment_closes or {}
    out: dict[date, float] = {}
    for flow in cash_flows:
        flow_date = _flow_date(flow)
        if _is_adjustment(flow):
            amount = _adjustment_value(flow, flow_date, closes_by_ticker)
        else:
            amount = float(flow["amount"])
        out[flow_date] = out.get(flow_date, 0.0) + amount
    return out


def _adjustment_value(
        flow: Mapping[str, Any], flow_date: date,
        closes_by_ticker: dict[str, dict[date, float]]) -> float:
    quantity = float(flow.get("quantity") or 0)
    ticker = _adjustment_ticker(flow)
    close = (
        _close_on_or_before(closes_by_ticker.get(ticker, {}), flow_date)
        if ticker else None)
    if close is None:
        cost_basis = float(flow.get("price") or 0)
        logger.warning(
            "portfolio_backtest.adjustment_valued_at_cost_basis",
            ticker=ticker, day=flow_date.isoformat())
        return quantity * cost_basis
    return quantity * close


def _twr_daily_returns(
        strategy_values: list[float], aligned_dates: list[date],
        flow_by_date: dict[date, float]) -> list[float]:
    returns: list[float] = []
    for i in range(1, len(aligned_dates)):
        prev_day, curr_day = aligned_dates[i - 1], aligned_dates[i]
        window_flow = sum(
            amount for flow_date, amount in flow_by_date.items()
            if prev_day < flow_date <= curr_day)
        v_prev, v_curr = strategy_values[i - 1], strategy_values[i]
        if v_prev <= 0:
            logger.warning(
                "portfolio_backtest.twr_zero_or_negative_base",
                day=curr_day.isoformat())
            returns.append(0.0)
            continue
        returns.append((v_curr - window_flow - v_prev) / v_prev)
    return returns


async def run_portfolio_backtest_workflow(
        backtest_id: str, *, portfolio_id: str) -> None:
    backtest_uuid = UUID(backtest_id)

    try:
        (start_date, end_date, _, benchmark_symbol
         ) = await load_backtest_and_mark_running(backtest_uuid)
        snapshots = await get_portfolio_snapshots(
            portfolio_id, start_date=start_date, end_date=end_date)
        if len(snapshots) < 2:
            raise ValueError(
                f"insufficient real snapshot history for portfolio "
                f"{portfolio_id} between {start_date} and {end_date} "
                f"({len(snapshots)} snapshots) -- market-close needs to "
                f"have run at least twice for this portfolio")

        snapshot_by_date: dict[date, float] = {
            date.fromisoformat(s["snapshot_date"]): float(s["total_value"])
            for s in snapshots
        }

        provider = get_market_data_provider()
        benchmark_bars = await provider.get_price_history_range(
            benchmark_symbol, start=start_date, end=end_date)
        benchmark_by_date = closes_by_date(
            benchmark_bars, ticker=benchmark_symbol,
            event="portfolio_backtest.dropped_non_finite_benchmark_bars")

        aligned_dates = sorted(
            set(snapshot_by_date.keys()) & set(benchmark_by_date.keys()))
        skipped = len(snapshot_by_date) - len(aligned_dates)
        if skipped:
            logger.warning(
                "portfolio_backtest.skipped_unaligned_snapshot_days",
                portfolio_id=portfolio_id, skipped=skipped)
        if len(aligned_dates) < 2:
            missing = sorted(set(snapshot_by_date) - set(benchmark_by_date))
            raise ValueError(
                f"insufficient aligned snapshot/benchmark history for "
                f"portfolio {portfolio_id} ({len(aligned_dates)} aligned "
                f"days; "
                f"no usable {benchmark_symbol} close for "
                f"{', '.join(d.isoformat() for d in missing) or 'none'}; the "
                f"newest daily bar can be incomplete for hours after the "
                f"close)")

        strategy_values = [snapshot_by_date[d] for d in aligned_dates]
        benchmark_closes = [benchmark_by_date[d] for d in aligned_dates]

        cash_flows = await get_portfolio_cash_flows(
            portfolio_id, start_date=start_date, end_date=end_date)
        adjustment_closes = await _adjustment_closes(
            cash_flows, start_date=start_date, end_date=end_date)
        flow_by_date = _net_flow_by_date(cash_flows, adjustment_closes)
        strategy_returns = _twr_daily_returns(
            strategy_values, aligned_dates, flow_by_date)
        twr_curve = [strategy_values[0]]
        for r in strategy_returns:
            twr_curve.append(twr_curve[-1] * (1 + r))

        result = compute_backtest_metrics(
            twr_curve, benchmark_closes, strategy_returns=strategy_returns)
        await persist_backtest_result(
            backtest_uuid, benchmark_symbol=benchmark_symbol, result=result)

        logger.info(
            "portfolio_backtest_complete", backtest_id=backtest_id,
            portfolio_id=portfolio_id, days=len(aligned_dates),
            strategy_return=result.strategy_return)

        await publish_backtest_finished(backtest_uuid, completed=True)

    except Exception as exc:
        logger.exception(
            "portfolio_backtest_failed", backtest_id=backtest_id,
            portfolio_id=portfolio_id)
        await mark_backtest_failed(
            backtest_uuid, error=backtest_error_text(exc))
