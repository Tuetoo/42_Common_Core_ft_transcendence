import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
import structlog
from ai.providers.base import PriceBar
from ai.providers.registry import get_market_data_provider
from ai.tools.indicators import (alpha_beta, information_ratio, max_drawdown,
                                 paired_simple_returns, sharpe_ratio,
                                 simple_returns, sortino_ratio, total_return,
                                 tracking_error, win_rate)
from ai_events import backtest_event_payload, publish_event
from clients.core_client import (get_portfolio_cash_flows,
                                 get_portfolio_snapshots)
from database import SessionLocal
from domains.analysis.model import RecommendationTier
from domains.analysis.repository import RecommendationTierRepository
from domains.backtest.repository import (BacktestPersistenceRepository,
                                         BacktestRepository,
                                         BenchmarkRepository)
from shared.enums import TierSide, TransactionType, WorkflowStatus
from shared.events import EventName
from shared.exceptions import NotFoundError

logger = structlog.get_logger(__name__)

_EPSILON = 1e-9

TradeEvent = tuple[str, str, float, float, datetime]


def trade_event(
        tier: RecommendationTier) -> tuple[str, float, float, datetime] | None:
    if tier.triggered_at is None or tier.triggered_price is None:
        return None
    return (
        tier.side, float(tier.quantity), float(tier.triggered_price),
        tier.triggered_at)


async def _load_ticker_trade_events(
        ticker: str, *, start_date: date, end_date: date,
        portfolio_id: UUID | None,
        user_id: UUID | None = None) -> list[TradeEvent]:
    async with SessionLocal() as session:
        tiers = await RecommendationTierRepository(
            session).list_triggered_tiers_for_ticker(
                ticker, start_date=start_date, end_date=end_date,
                portfolio_id=portfolio_id, user_id=user_id)
        return [(ticker, *event) for tier in tiers
                if (event := trade_event(tier)) is not None]


class InsufficientCapitalError(ValueError):
    pass


@dataclass
class ReplayResult:
    equity_curve: list[float] = field(default_factory=list)
    realized_trade_returns: list[float] = field(default_factory=list)


def replay_trade_events(
        trade_events: list[TradeEvent], *,
        closes_by_ticker: dict[str, dict[date,
                                         float]], trading_days: list[date],
        initial_capital: float, log_prefix: str) -> ReplayResult:
    cash = initial_capital
    lowest_cash = cash
    positions: dict[str, float] = {}
    open_lots: dict[str, list[list[float]]] = {}
    last_close: dict[str, float] = {}
    result = ReplayResult()
    event_idx = 0
    unmatched_sells = 0

    for day in trading_days:
        while (event_idx < len(trade_events)
               and trade_events[event_idx][4].date() <= day):
            ticker, side, quantity, price, _ = trade_events[event_idx]
            event_idx += 1
            last_close.setdefault(ticker, price)
            if side == TierSide.BUY.value:
                cash -= quantity * price
                lowest_cash = min(lowest_cash, cash)
                positions[ticker] = positions.get(ticker, 0.0) + quantity
                open_lots.setdefault(ticker, []).append([quantity, price])
            elif side == TierSide.SELL.value:
                cash += quantity * price
                positions[ticker] = positions.get(ticker, 0.0) - quantity
                remaining = quantity
                lots = open_lots.setdefault(ticker, [])
                while remaining > _EPSILON and lots:
                    lot = lots[0]
                    matched = min(lot[0], remaining)
                    result.realized_trade_returns.append(
                        (price - lot[1]) / lot[1])
                    lot[0] -= matched
                    remaining -= matched
                    if lot[0] <= _EPSILON:
                        lots.pop(0)
                if remaining > _EPSILON:
                    unmatched_sells += 1
            else:
                logger.warning(
                    f"{log_prefix}.unknown_tier_side", ticker=ticker,
                    side=side)

        for ticker, closes in closes_by_ticker.items():
            close = closes.get(day)
            if close is not None:
                last_close[ticker] = close

        result.equity_curve.append(
            cash + sum(
                qty * last_close[ticker] for ticker, qty in positions.items()
                if abs(qty) > _EPSILON and ticker in last_close))

    if unmatched_sells:
        logger.warning(f"{log_prefix}.unmatched_sells", count=unmatched_sells)
    if lowest_cash < -_EPSILON:
        needed = initial_capital - lowest_cash
        raise InsufficientCapitalError(
            f"the replayed buys need {needed:,.2f} of cash but "
            f"initial_capital is {initial_capital:,.2f}; run the backtest "
            f"again with an initial_capital of at least {needed:,.2f}")
    return result


@dataclass
class BacktestMetricsResult:
    ending_capital: float
    strategy_return: float | None
    benchmark_return: float | None
    sharpe: float | None
    sortino: float | None
    drawdown: float | None
    win_rate: float | None
    alpha: float | None
    beta: float | None
    information_ratio: float | None
    tracking_error: float | None
    outperformed: bool | None


def compute_backtest_metrics(
    equity_curve: list[float], benchmark_closes: list[float], *,
    strategy_returns: list[float] | None = None,
    realized_trade_returns: list[float] | None = None
) -> BacktestMetricsResult:
    if strategy_returns is None:
        strategy_returns, benchmark_returns = paired_simple_returns(
            equity_curve, benchmark_closes)
    else:
        benchmark_returns = simple_returns(benchmark_closes)
    strategy_total_return = total_return(equity_curve)
    benchmark_total_return = total_return(benchmark_closes)
    alpha, beta = alpha_beta(strategy_returns, benchmark_returns)
    return BacktestMetricsResult(
        ending_capital=equity_curve[-1], strategy_return=strategy_total_return,
        benchmark_return=benchmark_total_return,
        sharpe=sharpe_ratio(strategy_returns),
        sortino=sortino_ratio(strategy_returns),
        drawdown=max_drawdown(equity_curve), win_rate=(
            win_rate(realized_trade_returns)
            if realized_trade_returns is not None else None), alpha=alpha,
        beta=beta, information_ratio=information_ratio(
            strategy_returns,
            benchmark_returns), tracking_error=tracking_error(
                strategy_returns, benchmark_returns), outperformed=(
                    strategy_total_return > benchmark_total_return
                    if strategy_total_return is not None
                    and benchmark_total_return is not None else None))


async def persist_backtest_result(
        backtest_uuid: UUID, *, benchmark_symbol: str,
        result: BacktestMetricsResult) -> None:
    async with SessionLocal() as session:
        persist = BacktestPersistenceRepository(session)
        await persist.save_metrics(
            backtest_id=backtest_uuid, sharpe_ratio=result.sharpe,
            sortino_ratio=result.sortino, drawdown=result.drawdown,
            win_rate=result.win_rate)
        benchmark = await BenchmarkRepository(session).get_or_create(
            benchmark_symbol)
        await persist.save_benchmark_result(
            backtest_id=backtest_uuid, benchmark_id=benchmark.id,
            strategy_return=result.strategy_return,
            benchmark_return=result.benchmark_return, alpha=result.alpha,
            beta=result.beta, information_ratio=result.information_ratio,
            tracking_error=result.tracking_error,
            outperformed=result.outperformed)
        await BacktestRepository(session).finalize(
            backtest_uuid, ending_capital=result.ending_capital,
            strategy_return=result.strategy_return,
            benchmark_return=result.benchmark_return)
        await session.commit()


def closes_by_date(
        bars: list[PriceBar], *, ticker: str,
        event: str = "backtest.dropped_non_finite_bars") -> dict[date, float]:
    out: dict[date, float] = {}
    dropped = 0
    for bar in bars:
        if not math.isfinite(bar.close):
            dropped += 1
            continue
        out[bar.timestamp.date()] = float(bar.close)
    if dropped:
        logger.warning(event, ticker=ticker, dropped=dropped)
    return out


async def load_backtest_and_mark_running(
        backtest_uuid: UUID) -> tuple[date, date, float, str]:
    async with SessionLocal() as session:
        repo = BacktestRepository(session)
        backtest = await repo.get_by_id(backtest_uuid)
        loaded = (
            backtest.start_date, backtest.end_date,
            float(backtest.initial_capital), backtest.benchmark_symbol)
        await repo.update_status(backtest_uuid, WorkflowStatus.RUNNING.value)
        await session.commit()
    return loaded


async def _backtest_user_id(backtest_uuid: UUID) -> UUID | None:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).get_by_id(backtest_uuid)
        return backtest.user_id


async def mark_backtest_failed(
        backtest_uuid: UUID, *, error: str | None = None) -> None:
    try:
        async with SessionLocal() as session:
            await BacktestRepository(session).update_status(
                backtest_uuid, WorkflowStatus.FAILED.value)
            await session.commit()
    except NotFoundError:
        logger.error("backtest_row_missing", backtest_id=str(backtest_uuid))
        return
    await publish_backtest_finished(
        backtest_uuid, completed=False, error=error)


BACKTEST_ERROR_SERVICE_RESTART = "service_restart"


async def fail_stale_backtests_on_startup() -> int:
    async with SessionLocal() as session:
        stale = await BacktestRepository(session).list_active_ids()
    for backtest_uuid in stale:
        logger.warning(
            "backtest_failed_on_startup", backtest_id=str(backtest_uuid))
        await mark_backtest_failed(
            backtest_uuid, error=BACKTEST_ERROR_SERVICE_RESTART)
    logger.info(
        "backtest_startup_reconciliation_complete", failed_count=len(stale))
    return len(stale)


def backtest_error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


async def publish_backtest_finished(
        backtest_uuid: UUID, *, completed: bool,
        error: str | None = None) -> None:
    try:
        async with SessionLocal() as session:
            backtest = await BacktestRepository(session).get_by_id(
                backtest_uuid)
            user_id, strategy_name = backtest.user_id, backtest.strategy_name
        await publish_event(
            (
                EventName.BACKTEST_COMPLETED
                if completed else EventName.BACKTEST_FAILED),
            backtest_event_payload(
                backtest_id=backtest_uuid, user_id=user_id,
                strategy_name=strategy_name, completed=completed, error=error))
    except Exception:
        logger.warning(
            "backtest_event_publish_failed", backtest_id=str(backtest_uuid),
            completed=completed)


async def _replay_backtest(
        backtest_uuid: UUID, *,
        load_events: Callable[[UUID, date, date], Awaitable[list[TradeEvent]]],
        no_events_reason: Callable[[date, date], str], log_prefix: str,
        **log_fields: Any) -> None:
    try:
        (start_date, end_date, initial_capital, benchmark_symbol
         ) = await load_backtest_and_mark_running(backtest_uuid)
        trade_events = await load_events(backtest_uuid, start_date, end_date)
        if not trade_events:
            raise ValueError(
                "no triggered recommendation tiers "
                f"{no_events_reason(start_date, end_date)}")
        trade_events.sort(key=lambda event: event[4])

        dropped_event = f"{log_prefix}.dropped_non_finite_bars"
        provider = get_market_data_provider()
        closes_by_ticker: dict[str, dict[date, float]] = {}
        for ticker in sorted({ticker for ticker, *_ in trade_events}):
            bars = await provider.get_price_history_range(
                ticker, start=start_date, end=end_date)
            closes_by_ticker[ticker] = closes_by_date(
                bars, ticker=ticker, event=dropped_event)
            if not closes_by_ticker[ticker]:
                raise ValueError(
                    f"no price history for {ticker} between {start_date} "
                    f"and {end_date}")
        benchmark_bars = await provider.get_price_history_range(
            benchmark_symbol, start=start_date, end=end_date)
        benchmark_closes_by_date = closes_by_date(
            benchmark_bars, ticker=benchmark_symbol, event=dropped_event)

        trading_days = sorted(benchmark_closes_by_date.keys())
        if len(trading_days) < 2:
            raise ValueError(
                f"insufficient benchmark price history for {benchmark_symbol} "
                f"between {start_date} and {end_date} ({len(trading_days)} "
                f"days)")

        replay = replay_trade_events(
            trade_events, closes_by_ticker=closes_by_ticker,
            trading_days=trading_days, initial_capital=initial_capital,
            log_prefix=log_prefix)
        result = compute_backtest_metrics(
            replay.equity_curve,
            [benchmark_closes_by_date[day] for day in trading_days],
            realized_trade_returns=replay.realized_trade_returns)
        await persist_backtest_result(
            backtest_uuid, benchmark_symbol=benchmark_symbol, result=result)

        logger.info(
            f"{log_prefix}_complete", backtest_id=str(backtest_uuid),
            **log_fields, trades=len(trade_events),
            strategy_return=result.strategy_return)

        await publish_backtest_finished(backtest_uuid, completed=True)

    except Exception as exc:
        logger.exception(
            f"{log_prefix}_failed", backtest_id=str(backtest_uuid))
        await mark_backtest_failed(
            backtest_uuid, error=backtest_error_text(exc))


async def run_backtest_workflow(
        backtest_id: str, *, ticker: str,
        portfolio_id: str | None = None) -> None:
    portfolio_uuid = UUID(portfolio_id) if portfolio_id else None

    async def _events(
            backtest_uuid: UUID, start_date: date,
            end_date: date) -> list[TradeEvent]:
        scope_user = (
            await _backtest_user_id(backtest_uuid)
            if portfolio_uuid is None else None)
        return await _load_ticker_trade_events(
            ticker, start_date=start_date, end_date=end_date,
            portfolio_id=portfolio_uuid, user_id=scope_user)

    await _replay_backtest(
        UUID(backtest_id), load_events=_events,
        no_events_reason=lambda start, end: (
            f"for {ticker} between {start} and {end}" +
            (f" in portfolio {portfolio_uuid}" if portfolio_uuid else "")),
        log_prefix="backtest", ticker=ticker)


async def _load_replay_trade_events(
        backtest_id: UUID, start_date: date,
        end_date: date) -> list[TradeEvent]:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).get_by_id(backtest_id)
        tiers = await RecommendationTierRepository(
            session).list_triggered_tiers(
                user_id=backtest.user_id, start_date=start_date,
                end_date=end_date)
        return [(tier.recommendation.analysis.ticker, *event) for tier in tiers
                if (event := trade_event(tier)) is not None]


async def run_recommendation_replay_workflow(backtest_id: str) -> None:
    await _replay_backtest(
        UUID(backtest_id), load_events=_load_replay_trade_events,
        no_events_reason=lambda start, end: (
            f"between {start} and {end} to replay"),
        log_prefix="recommendation_replay")


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
        unfunded = 0
        while (unfunded < len(aligned_dates)
               and snapshot_by_date[aligned_dates[unfunded]] <= 0):
            unfunded += 1
        if unfunded:
            logger.info(
                "portfolio_backtest.skipped_unfunded_leading_days",
                portfolio_id=portfolio_id, skipped=unfunded)
            aligned_dates = aligned_dates[unfunded:]
            if len(aligned_dates) < 2:
                raise ValueError(
                    f"portfolio {portfolio_id} has {len(aligned_dates)} "
                    f"aligned day(s) with a positive total value between "
                    f"{start_date} and {end_date}; a backtest needs at "
                    f"least 2")

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
