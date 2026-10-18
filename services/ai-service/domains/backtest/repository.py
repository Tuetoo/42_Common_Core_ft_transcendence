from datetime import date as date_type
from uuid import UUID
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from domains.backtest.model import (Backtest, BacktestMetrics, Benchmark,
                                    BenchmarkResult)
from shared.enums import WorkflowStatus
from shared.exceptions import NotFoundError

_ACTIVE_STATUSES = (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)


class BacktestRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
            self, *, user_id: UUID | None, strategy_name: str,
            benchmark_symbol: str, start_date: date_type, end_date: date_type,
            initial_capital: float) -> Backtest:
        backtest = Backtest(
            user_id=user_id, strategy_name=strategy_name,
            benchmark_symbol=benchmark_symbol.upper(), start_date=start_date,
            end_date=end_date, initial_capital=initial_capital,
            status=WorkflowStatus.PENDING.value)
        self.session.add(backtest)
        await self.session.flush()
        return backtest

    async def get_by_id(
            self, backtest_id: UUID, *,
            with_details: bool = False) -> Backtest:
        stmt = select(Backtest).where(Backtest.id == backtest_id)
        if with_details:
            stmt = stmt.options(
                selectinload(Backtest.metrics),
                selectinload(Backtest.benchmark_results))
        result = await self.session.execute(stmt)
        backtest = result.scalar_one_or_none()
        if backtest is None:
            raise NotFoundError(f"Backtest {backtest_id} not found")
        return backtest

    async def list_for_user(
            self, user_id: UUID, *, limit: int, offset: int) -> list[Backtest]:
        result = await self.session.execute(
            select(Backtest).where(Backtest.user_id == user_id).order_by(
                Backtest.created_at.desc(),
                Backtest.id.desc()).limit(limit).offset(offset))
        return list(result.scalars().all())

    async def delete(self, backtest: Backtest) -> None:
        await self.session.delete(backtest)
        await self.session.flush()

    async def list_active_ids(self) -> list[UUID]:
        result = await self.session.execute(
            select(Backtest.id).where(Backtest.status.in_(_ACTIVE_STATUSES)))
        return list(result.scalars().all())

    async def count_active_for_user(
            self, user_id: UUID, *, lock: bool = False) -> int:
        if lock:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"backtests:{user_id}"})
        result = await self.session.execute(
            select(func.count()).select_from(Backtest).where(
                Backtest.user_id == user_id,
                Backtest.status.in_(_ACTIVE_STATUSES)))
        return int(result.scalar_one())

    async def update_status(self, backtest_id: UUID, status: str) -> None:
        backtest = await self.get_by_id(backtest_id)
        backtest.status = status
        await self.session.flush()

    async def finalize(
            self, backtest_id: UUID, *, ending_capital: float,
            strategy_return: float | None,
            benchmark_return: float | None) -> None:
        backtest = await self.get_by_id(backtest_id)
        backtest.ending_capital = ending_capital
        backtest.strategy_return = strategy_return
        backtest.benchmark_return = benchmark_return
        backtest.status = WorkflowStatus.COMPLETED.value
        await self.session.flush()


class BacktestPersistenceRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_metrics(
            self, *, backtest_id: UUID, sharpe_ratio: float | None,
            sortino_ratio: float | None, drawdown: float | None,
            win_rate: float | None) -> BacktestMetrics:
        metrics = BacktestMetrics(
            backtest_id=backtest_id, sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio, drawdown=drawdown, win_rate=win_rate)
        self.session.add(metrics)
        await self.session.flush()
        return metrics

    async def save_benchmark_result(
            self, *, backtest_id: UUID, benchmark_id: UUID,
            strategy_return: float | None, benchmark_return: float | None,
            alpha: float | None, beta: float | None,
            information_ratio: float | None, tracking_error: float | None,
            outperformed: bool | None) -> BenchmarkResult:
        result = BenchmarkResult(
            backtest_id=backtest_id, benchmark_id=benchmark_id,
            strategy_return=strategy_return, benchmark_return=benchmark_return,
            alpha=alpha, beta=beta, information_ratio=information_ratio,
            tracking_error=tracking_error, outperformed=outperformed)
        self.session.add(result)
        await self.session.flush()
        return result


class BenchmarkRepository:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(
            self, symbol: str, *, name: str | None = None,
            asset_type: str = "index") -> Benchmark:
        symbol = symbol.upper()
        insert_stmt = (
            pg_insert(Benchmark).values(
                symbol=symbol, name=name or symbol,
                asset_type=asset_type).on_conflict_do_nothing(
                    index_elements=["symbol"]))
        await self.session.execute(insert_stmt)
        await self.session.flush()
        result = await self.session.execute(
            select(Benchmark).where(Benchmark.symbol == symbol))
        return result.scalar_one()
