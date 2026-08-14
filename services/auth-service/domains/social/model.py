import uuid
from datetime import datetime
from sqlalchemy import (Computed, DateTime, ForeignKey, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base
from shared.models import UUIDPKMixin


class Connection(Base, UUIDPKMixin):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("requester_id", "addressee_id",
                         name="uq_connection_requester_addressee"),
        UniqueConstraint("pair_key", name="uq_connection_pair_key"))
    requester_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    addressee_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    pair_key: Mapped[str] = mapped_column(
        Text, Computed("LEAST(requester_id, addressee_id)::text || ':' || "
                       "GREATEST(requester_id, addressee_id)::text",
                       persisted=True))
    status: Mapped[str] = mapped_column(String, nullable=False,
                                        default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
