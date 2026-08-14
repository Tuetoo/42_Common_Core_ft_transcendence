from uuid import UUID
from datetime import datetime
from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import UUIDPKMixin


class Notification(Base, UUIDPKMixin):
    __tablename__ = "notifications"
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False,
                                          index=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                     nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)
