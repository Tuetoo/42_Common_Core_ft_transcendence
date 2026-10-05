from datetime import date
from uuid import UUID
from pydantic import Field
from shared.schemas import BaseSchema


class AgentPerformanceRead(BaseSchema):
    agent_id: UUID
    avg_score: float | None
    success_rate: float | None


class DateRangeRead(BaseSchema):
    date_from: date = Field(serialization_alias="from")
    date_to: date = Field(serialization_alias="to")


class AccuracyRead(BaseSchema):
    evaluated: int
    rate: float | None


class AgentBreakdownRead(BaseSchema):
    agent: str
    runs: int
    completed: int
    failed: int
    success_rate: float | None
    avg_score: float | None
    avg_duration_ms: float | None


class TickerBreakdownRead(BaseSchema):
    ticker: str
    analyses: int
    completed: int
    failed: int
    actions: dict[str, int]
    average_confidence: float | None
    accuracy: AccuracyRead


class AnalyticsOverviewRead(BaseSchema):
    range: DateRangeRead
    scope: str
    total_analyses: int
    status_counts: dict[str, int]
    recommendations: int
    action_distribution: dict[str, int]
    average_confidence: float | None
    accuracy: AccuracyRead
    by_agent: list[AgentBreakdownRead]
    by_ticker: list[TickerBreakdownRead]


class TimeseriesPointRead(BaseSchema):
    period_start: date
    value: float | None
    count: int


class AnalyticsTimeseriesRead(BaseSchema):
    metric: str
    interval: str
    range: DateRangeRead
    scope: str
    points: list[TimeseriesPointRead]
