from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import Field
from shared.schemas import BaseSchema, IDMixin


class InvestmentDecisionCreate(BaseSchema):
    action_taken: Literal["followed", "ignored", "partial", "declined"]
    portfolio_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class InvestmentDecisionRead(BaseSchema, IDMixin):
    user_id: UUID
    portfolio_id: UUID | None
    recommendation_id: UUID
    ticker: str
    action_taken: str
    notes: str | None
    created_at: datetime


class DecisionOutcomeRead(BaseSchema, IDMixin):
    decision_id: UUID
    evaluation_days: int
    strategy_return: float | None
    benchmark_return: float | None
    alpha: float | None
    outperformed: bool | None
    evaluated_at: datetime | None


class DecisionReviewRead(BaseSchema, IDMixin):
    decision_id: UUID
    review_text: str | None
    lessons_learned: str | None
    created_at: datetime
