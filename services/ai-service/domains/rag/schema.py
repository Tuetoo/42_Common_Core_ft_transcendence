from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shared.enums import DocumentCategory
from shared.tickers import OptionalTicker

MAX_QUESTION_CHARS = 2000
MAX_TOP_K = 10


class RagAskRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={
            "description": (
                '`mode="ask"`: answer the question from the sources. '
                '`mode="draft"`: writing help for a research note, a draft '
                "paragraph grounded in the sources, with the same citations.")
        })

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    ticker: OptionalTicker = None
    categories: list[DocumentCategory] | None = Field(
        default=None, max_length=len(DocumentCategory))
    mode: Literal["ask", "draft"] = "ask"
    top_k: int | None = Field(default=None, ge=1, le=MAX_TOP_K)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped

    @field_validator("categories")
    @classmethod
    def _dedupe_categories(
            cls, value: list[DocumentCategory] | None
    ) -> list[DocumentCategory] | None:
        if not value:
            return None
        return list(dict.fromkeys(value))


class RagCitation(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"description": "Source `[index]` of the answer."})

    index: int
    document_id: str | None
    title: str
    category: DocumentCategory
    ticker: str | None
    content_date: str | None
    snippet: str


class RagAnswer(BaseModel):
    answer: str
    citations: list[RagCitation]
    model: str | None
    latency_ms: int
    mode: Literal["ask", "draft"]
    grounded: bool
