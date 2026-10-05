from datetime import date
from uuid import UUID
import structlog
from ai.providers.registry import get_market_data_provider
from ai.workflows.backtest_workflow import (TradeEvent, backtest_error_text,
                                            closes_by_date,
                                            compute_backtest_metrics,
                                            load_backtest_and_mark_running,
                                            mark_backtest_failed,
                                            persist_backtest_result,
                                            publish_backtest_finished,
                                            replay_trade_events, trade_event)
from database import SessionLocal
from domains.analysis.repository import RecommendationTierRepository
from domains.backtest.repository import BacktestRepository

logger = structlog.get_logger(__name__)

_DROPPED_BARS_EVENT = "recommendation_replay.dropped_non_finite_bars"


async def _load_trade_events(backtest_id: UUID) -> list[TradeEvent]:
    async with SessionLocal() as session:
        backtest = await BacktestRepository(session).get_by_id(backtest_id)
        tiers = await RecommendationTierRepository(
            session).list_triggered_tiers(
                user_id=backtest.user_id, start_date=backtest.start_date,
                end_date=backtest.end_date)
        return [(tier.recommendation.analysis.ticker, *event) for tier in tiers
                if (event := trade_event(tier)) is not None]


async def run_recommendation_replay_workflow(backtest_id: str) -> None:
    backtest_uuid = UUID(backtest_id)

    try:
        (start_date, end_date, initial_capital, benchmark_symbol
         ) = await load_backtest_and_mark_running(backtest_uuid)
        trade_events = await _load_trade_events(backtest_uuid)
        if not trade_events:
            raise ValueError(
                f"no triggered recommendation tiers between {start_date} "
                f"and {end_date} to replay")
        trade_events.sort(key=lambda event: event[4])

        provider = get_market_data_provider()
        tickers = sorted({ticker for ticker, *_ in trade_events})
        price_by_ticker: dict[str, dict[date, float]] = {}
        for ticker in tickers:
            bars = await provider.get_price_history_range(
                ticker, start=start_date, end=end_date)
            price_by_ticker[ticker] = closes_by_date(
                bars, ticker=ticker, event=_DROPPED_BARS_EVENT)

        benchmark_bars = await provider.get_price_history_range(
            benchmark_symbol, start=start_date, end=end_date)
        benchmark_closes_by_date = closes_by_date(
            benchmark_bars, ticker=benchmark_symbol, event=_DROPPED_BARS_EVENT)
        trading_days = sorted(benchmark_closes_by_date.keys())
        if len(trading_days) < 2:
            raise ValueError(
                f"insufficient benchmark price history for {benchmark_symbol} "
                f"between {start_date} and {end_date} ({len(trading_days)} "
                f"days)")

        replay = replay_trade_events(
            trade_events, closes_by_ticker=price_by_ticker,
            trading_days=trading_days, initial_capital=initial_capital,
            log_prefix="recommendation_replay")
        result = compute_backtest_metrics(
            replay.equity_curve,
            [benchmark_closes_by_date[day] for day in trading_days],
            realized_trade_returns=replay.realized_trade_returns)
        await persist_backtest_result(
            backtest_uuid, benchmark_symbol=benchmark_symbol, result=result)

        logger.info(
            "recommendation_replay_complete", backtest_id=backtest_id,
            trades=len(trade_events), strategy_return=result.strategy_return)

        await publish_backtest_finished(backtest_uuid, completed=True)

    except Exception as exc:
        logger.exception(
            "recommendation_replay_failed", backtest_id=backtest_id)
        await mark_backtest_failed(
            backtest_uuid, error=backtest_error_text(exc))
