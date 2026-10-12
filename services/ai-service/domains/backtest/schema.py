from datetime import date, datetime
from uuid import UUID
from pydantic import ConfigDict, Field, field_validator
from shared.schemas import BaseSchema, IDMixin, Ticker

RECOMMENDATION_REPLAY_STRATEGY = "ai_recommendation_replay"
PORTFOLIO_SNAPSHOT_STRATEGY = "portfolio_snapshot_backtest"
_BACKTEST_KIND_BY_STRATEGY = {
    RECOMMENDATION_REPLAY_STRATEGY: "recommendation_replay",
    PORTFOLIO_SNAPSHOT_STRATEGY: "portfolio"
}
RESERVED_STRATEGY_NAMES = frozenset(_BACKTEST_KIND_BY_STRATEGY)


def backtest_kind(strategy_name: str | None) -> str:
    return _BACKTEST_KIND_BY_STRATEGY.get(strategy_name or "", "ticker")


class BacktestCreate(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Replays every recommendation tier that triggered for "
                "`ticker` "
                "within `[start_date, end_date]`. `portfolio_id` is optional: "
                "null (the default) uses the caller's triggered tiers across "
                "portfolios; a portfolio id limits the replay to that "
                "portfolio's"
                " triggered tiers.")
        })

    strategy_name: str = Field(min_length=1, max_length=100)
    ticker: Ticker = Field(min_length=1, max_length=10)
    benchmark_symbol: str = Field(default="SPY", min_length=1, max_length=10)
    start_date: date
    end_date: date
    initial_capital: float = Field(gt=0)
    portfolio_id: UUID | None = None

    @field_validator("strategy_name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_STRATEGY_NAMES:
            raise ValueError(
                f"strategy_name {value!r} is reserved for another backtest "
                "kind")
        return value


class RecommendationReplayCreate(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Body of a recommendation replay. The trade universe is every "
                "recommendation tier that has reached TRIGGERED, found from "
                "history at request time; `benchmark_symbol` and "
                "`initial_capital` are the only inputs.")
        })

    benchmark_symbol: str = Field(default="SPY", min_length=1, max_length=10)
    initial_capital: float = Field(default=100_000.0, gt=0)


class PortfolioBacktestCreate(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Backtest of a portfolio's actual composition over time. "
                "`start_date`/`end_date` are optional; without them the "
                "portfolio's whole snapshot history is used. There is no "
                "initial "
                "capital field: the starting value is the portfolio's total "
                "value"
                " on `start_date`.")
        })

    portfolio_id: UUID
    benchmark_symbol: str = Field(default="SPY", min_length=1, max_length=10)
    start_date: date | None = None
    end_date: date | None = None


class BacktestRead(BaseSchema, IDMixin):
    strategy_name: str
    benchmark_symbol: str
    start_date: date
    end_date: date
    initial_capital: float
    ending_capital: float | None
    strategy_return: float | None
    benchmark_return: float | None
    status: str
    created_at: datetime


class BacktestMetricsRead(BaseSchema):
    sharpe_ratio: float | None
    sortino_ratio: float | None
    drawdown: float | None
    win_rate: float | None


class BenchmarkResultRead(BaseSchema, IDMixin):
    strategy_return: float | None
    benchmark_return: float | None
    alpha: float | None
    beta: float | None
    information_ratio: float | None
    tracking_error: float | None
    outperformed: bool | None


class BacktestDetailRead(BacktestRead):
    metrics: BacktestMetricsRead | None
    benchmark_result: BenchmarkResultRead | None
