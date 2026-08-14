from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from shared.schemas.base import BaseSchema, IDMixin
from shared.schemas.refs import UserRef


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


class SystemDocumentCreate(BaseModel):
    filename: str = Field(min_length=1)
    content: str = Field(min_length=1)
    category: str = Field(min_length=1)
    ticker: str | None = None
    organization_id: UUID | None = None
    content_date: datetime | None = None


class ResearchCommentCreate(BaseModel):
    content: str = Field(min_length=1)


class ResearchCommentRead(BaseSchema, IDMixin):
    note_id: UUID
    author: UserRef
    content: str
    created_at: datetime


class ResearchNoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    content: str | None = None
    organization_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)


class ResearchNoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = None
    tags: list[str] | None = None


class ResearchNoteRead(BaseSchema, IDMixin):
    author: UserRef
    organization_id: UUID | None = None
    title: str
    content: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ResearchNoteFilters(BaseModel):
    q: str | None = None
    tag: str | None = None
    author_id: UUID | None = None
    organization_id: UUID | None = None
    sort: str = Field(default="-created_at",
                      pattern=r"^-?(created_at|title)$")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
