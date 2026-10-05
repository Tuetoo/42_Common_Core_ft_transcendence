from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from shared.enums import RecommendationAction, WorkflowStatus
from shared.request_id import current_request_id

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 100_000
SYSTEM_DOCUMENT_OWNER_ID = UUID("00000000-0000-0000-0000-0000000005ec")
T = TypeVar("T")


class BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class IDMixin(BaseModel):
    id: UUID


class TimestampMixin(BaseModel):
    created_at: datetime
    updated_at: datetime


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1, le=MAX_PAGE)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class PaginatedResponse(BaseSchema, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class APIResponse(BaseSchema, Generic[T]):
    data: T
    request_id: str | None = Field(default_factory=current_request_id)


class UserRef(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Reference to a user, produced by auth-service and used by "
                "core-service and ai-service.")
        })
    id: UUID
    username: str
    avatar_url: str | None = None
    is_online: bool = False


class OrganizationRef(BaseSchema):
    id: UUID
    name: str
    role: str | None = None


class AnalysisRef(BaseSchema):
    id: UUID
    ticker: str
    status: WorkflowStatus
    created_at: datetime
    user_id: UUID | None = None
    portfolio_id: UUID | None = None


class RecommendationRef(BaseSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Reference to a recommendation, produced by ai-service and "
                "used "
                "by core-service (notifications, portfolio).")
        })
    id: UUID
    analysis_id: UUID
    ticker: str
    action: RecommendationAction
    confidence: float
    created_at: datetime
