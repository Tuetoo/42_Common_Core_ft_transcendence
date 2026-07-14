from datetime import datetime
from uuid import UUID
from shared.enums.recommendation import RecommendationAction
from shared.enums.workflow import WorkflowStatus
from shared.schemas.base import BaseSchema


class UserRef(BaseSchema):
    """produced by auth-service consumed by core-service, ai-service."""
    id: UUID
    username: str
    avatar_url: str | None = None
    is_online: bool = False


class OrganizationRef(BaseSchema):
    """produced by auth-service consumed by core-service, ai-service."""
    id: UUID
    name: str


class TickerRef(BaseSchema):
    """produced by ai-service (market) consumed by core-service (portfolio)."""
    ticker: str
    name: str | None = None


class AnalysisRef(BaseSchema):
    """produced by ai-service consumed by core-service(notification,
    portfolio)"""
    id: UUID
    ticker: str
    status: WorkflowStatus
    created_at: datetime


class RecommendationRef(BaseSchema):
    """produced by ai-service consumed by core-service(notification,
    portfolio)"""
    id: UUID
    analysis_id: UUID
    ticker: str
    action: RecommendationAction
    confidence: float
    created_at: datetime
