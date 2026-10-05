from datetime import date as date_type
from datetime import datetime
from uuid import UUID
from sqlalchemy import Boolean, Date, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime
from shared.database import Base, UUIDPKMixin
from shared.enums import WorkflowStatus


class Backtest(Base, UUIDPKMixin):
    __tablename__ = "backtests"

    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    benchmark_symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    end_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[float] = mapped_column(
        Numeric(asdecimal=False), nullable=False)
    ending_capital: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    strategy_return: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    benchmark_return: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkflowStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    metrics: "Mapped[BacktestMetrics | None]" = relationship(
        uselist=False, cascade="all, delete-orphan")
    benchmark_results: "Mapped[list[BenchmarkResult]]" = relationship(
        cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_backtests_user_id_created_at", "user_id", "created_at"), )


class BacktestMetrics(Base, UUIDPKMixin):
    __tablename__ = "backtest_metrics"

    backtest_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("backtests.id"), nullable=False,
        unique=True)
    sharpe_ratio: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    sortino_ratio: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    drawdown: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    win_rate: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)


class Benchmark(Base, UUIDPKMixin):
    __tablename__ = "benchmarks"

    symbol: Mapped[str] = mapped_column(
        String(10), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class BenchmarkResult(Base, UUIDPKMixin):
    __tablename__ = "benchmark_results"

    backtest_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("backtests.id"), nullable=False)
    benchmark_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("benchmarks.id"), nullable=False)
    strategy_return: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    benchmark_return: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    alpha: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    beta: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    information_ratio: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    tracking_error: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True)
    outperformed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_benchmark_results_backtest_id", "backtest_id"), )
