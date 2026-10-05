from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, UUIDPKMixin


class EventOutbox(Base, UUIDPKMixin):
    __tablename__ = "event_outbox"
    __table_args__ = (
        Index(
            "ix_event_outbox_unpublished", "created_at",
            postgresql_where=text("published_at IS NULL")), )

    event_name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
