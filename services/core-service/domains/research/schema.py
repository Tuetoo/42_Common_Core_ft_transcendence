from datetime import date, datetime
from typing import Literal
from uuid import UUID
from pydantic import (BaseModel, ConfigDict, Field, computed_field,
                      field_validator)
from shared.schemas import (SYSTEM_DOCUMENT_OWNER_ID, BaseSchema, IDMixin,
                            PaginationParams, UserRef)


class DocumentRead(BaseSchema, IDMixin):
    owner_id: UUID
    organization_id: UUID | None = None
    filename: str
    document_type: str
    category: str
    ticker: str | None = None
    storage_url: str
    status: str
    uploaded_at: datetime
    content_date: datetime | None = None
    title: str | None = None
    source_url: str | None = None

    @computed_field(  # type: ignore[prop-decorator]
        description="A system document (no user owner)."
    )
    @property
    def is_system(self) -> bool:
        return self.owner_id == SYSTEM_DOCUMENT_OWNER_ID


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Body of `PATCH /api/v1/research/documents/{id}`: only the "
                "display title can change (the file, category and ticker "
                "drive "
                "RAG indexing).")
        })

    title: str | None = Field(default=None, max_length=256)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, title: str | None) -> str | None:
        if title is None:
            return None
        return title.strip() or None


DocumentSort = Literal["created_at", "-created_at", "content_date",
                       "-content_date", "title", "-title", "status", "-status"]


class SystemDocumentCreate(BaseModel):
    filename: str = Field(min_length=1)
    content: str = Field(min_length=1)
    category: str = Field(min_length=1)
    ticker: str | None = None
    content_date: datetime | None = None


MAX_NOTE_CONTENT_CHARS = 200_000
MAX_COMMENT_CONTENT_CHARS = 10_000
MAX_TAG_CHARS = 50
MAX_TAGS_PER_NOTE = 20


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = [tag.strip() for tag in tags]
    cleaned = [tag for tag in cleaned if tag]
    if len(cleaned) > MAX_TAGS_PER_NOTE:
        raise ValueError(f"at most {MAX_TAGS_PER_NOTE} tags are allowed")
    for tag in cleaned:
        if len(tag) > MAX_TAG_CHARS:
            raise ValueError(f"tag names must be 1-{MAX_TAG_CHARS} characters")
    return cleaned


class ResearchCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_COMMENT_CONTENT_CHARS)


class ResearchCommentUpdate(ResearchCommentCreate):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Body of a comment PATCH; same validation as create.")
        })


SentimentLabel = Literal["positive", "neutral", "negative"]


class SentimentFields(BaseModel):
    sentiment_label: SentimentLabel | None = None
    sentiment_score: float | None = None
    sentiment_model: str | None = None
    sentiment_analyzed_at: datetime | None = None


class ResearchCommentRead(SentimentFields, BaseSchema, IDMixin):
    note_id: UUID
    author: UserRef
    content: str
    created_at: datetime
    updated_at: datetime | None = None
    is_hidden: bool = False
    hidden_at: datetime | None = None


class ResearchNoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    content: str | None = Field(
        default=None, max_length=MAX_NOTE_CONTENT_CHARS)
    organization_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: list[str]) -> list[str]:
        return _clean_tags(tags)


class ResearchNoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = Field(
        default=None, max_length=MAX_NOTE_CONTENT_CHARS)
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: list[str] | None) -> list[str] | None:
        return None if tags is None else _clean_tags(tags)


class ResearchNoteRead(SentimentFields, BaseSchema, IDMixin):
    author: UserRef
    organization_id: UUID | None = None
    title: str
    content: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    is_hidden: bool = False
    hidden_at: datetime | None = None
    can_edit: bool = False


class SentimentCounts(BaseModel):
    positive: int = 0
    neutral: int = 0
    negative: int = 0


class SentimentDay(BaseModel):
    date: date
    count: int
    counts: SentimentCounts
    average_score: float | None = None


class SentimentSummary(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Response of `GET /api/v1/research/sentiment/summary`.")
        })

    ticker: str | None = None
    date_from: date | None = Field(default=None, serialization_alias="from")
    date_to: date | None = Field(default=None, serialization_alias="to")
    total: int
    notes: int
    comments: int
    counts: SentimentCounts
    average_score: float | None = None
    series: list[SentimentDay] = Field(default_factory=list)


class ResearchNoteFilters(PaginationParams):
    q: str | None = None
    tag: str | None = None
    author_id: UUID | None = None
    organization_id: UUID | None = None
    sort: str = Field(default="-created_at", pattern=r"^-?(created_at|title)$")
