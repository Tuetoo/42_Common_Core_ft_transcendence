from datetime import datetime
from uuid import UUID
from sqlalchemy import (DateTime, ForeignKey, String, Text, UniqueConstraint,
                        func)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from shared.database import Base, TimestampColumnsMixin, UUIDPKMixin


class Organization(Base, UUIDPKMixin, TimestampColumnsMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)


class OrganizationMember(Base, UUIDPKMixin):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_org_member_org_user"), )

    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        index=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrganizationInvitation(Base, UUIDPKMixin):
    __tablename__ = "organization_invitations"

    organization_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    invited_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
